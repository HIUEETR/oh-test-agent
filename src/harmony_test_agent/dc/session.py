"""DC 模式会话管理：DcEventBus + DcSession + DcSessionManager。

会话仅存在于内存中（服务重启后丢失），事件通过 asyncio.Queue 推送（非 SQLite 轮询），
端到端延迟 <20ms。与 Live Mode 的 RunManager/RunEventEmitter 完全隔离。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Literal

from ..config import Settings
from ..devices.harmony import HarmonyDeviceAdapter
from ..models import ScreenSnapshot, utc_now
from ..storage.artifacts import ArtifactStore
from .generator import DcHypiumGenerator
from .hdc import DcHdcExecutor
from .models import (
    DcChatRequest,
    DcChatResponse,
    DcDeviceBusy,
    DcEvent,
    DcEventType,
    DcProviderUnsupported,
    DcScriptArtifact,
    DcSessionNotFound,
    DcSessionView,
    DcToolInvocation,
    DcToolTier,
    DcTurnBudgetExceeded,
    DcTurnRecord,
    DcTurnStatus,
)
from .provider import DcChatProvider, MockDcChatProvider, create_dc_provider
from .safety import DcShellPolicy
from .tools import DcActionRecorder, DcSnapshotHolder, DcToolContext, build_tools

# ---------------------------------------------------------------------------
# 事件总线（push-based，非轮询）
# ---------------------------------------------------------------------------


class DcEventBus:
    """每 session 一个 asyncio.Queue 列表；push-based SSE fanout。

    - ``emit``：广播到所有订阅者；QueueFull 时踢慢消费者而非阻塞发射方。
    - ``recent``：deque(maxlen=N) 用于 Last-Event-ID 回放。
    """

    def __init__(self, buffer_size: int = 500):
        self._subscribers: set[asyncio.Queue[DcEvent]] = set()
        self._recent: deque[DcEvent] = deque(maxlen=buffer_size)
        self._counter = 0

    def emit(
        self,
        session_id: str,
        event_type: DcEventType,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> DcEvent:
        """创建并广播一个事件。"""
        self._counter += 1
        event = DcEvent(
            event_id=self._counter,
            session_id=session_id,
            type=event_type,
            timestamp=utc_now(),
            message=message,
            payload=payload or {},
        )
        self._recent.append(event)
        dead: list[asyncio.Queue[DcEvent]] = []
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(queue)
        # 踢掉慢消费者，永不阻塞发射方
        for queue in dead:
            self._subscribers.discard(queue)
        return event

    def subscribe(self) -> asyncio.Queue[DcEvent]:
        """返回一个独立的订阅队列（maxsize=1000）。"""
        queue: asyncio.Queue[DcEvent] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[DcEvent]) -> None:
        """移除订阅者。"""
        self._subscribers.discard(queue)

    def recent(self, after_id: int = 0) -> list[DcEvent]:
        """返回 event_id > after_id 的最近事件（用于 Last-Event-ID 回放）。"""
        return [event for event in self._recent if event.event_id > after_id]


# ---------------------------------------------------------------------------
# 历史裁剪
# ---------------------------------------------------------------------------


def _prune_history(history: list[Any], max_turns: int) -> list[Any]:
    """裁剪 pydantic-ai message_history：保留 system 头 + 最近 N 轮。

    被裁旧轮次中的 BinaryContent 图片替换为文本占位符，防止 vision token 爆炸。
    借鉴 ``ExplorationAdvisor._prune``（advisor.py:157-162）。
    """
    if not history or len(history) <= max_turns * 2 + 1:
        return history

    # 保留第一条（system prompt）
    head = history[:1]
    tail = history[-(max_turns * 2) :]

    # 把被裁部分中的图片替换为文本占位符
    pruned_middle: list[Any] = []
    for msg in history[1 : -len(tail)]:
        pruned_middle.append(_replace_images_with_placeholder(msg))

    return head + pruned_middle + tail


def _replace_images_with_placeholder(message: Any) -> Any:
    """把消息中的 BinaryContent 图片替换为文本占位符。"""
    parts = getattr(message, "parts", None)
    if parts is None:
        return message
    new_parts = []
    changed = False
    for part in parts:
        part_type = type(part).__name__
        is_binary = part_type == "BinaryContent"
        has_image_media = hasattr(part, "media_type") and "image" in str(getattr(part, "media_type", ""))
        if is_binary or has_image_media:
            # 替换为文本占位符
            from pydantic_ai.messages import TextPart

            new_parts.append(TextPart(content="[screenshot omitted from history]"))
            changed = True
        else:
            new_parts.append(part)
    if not changed:
        return message
    # 创建消息副本（不修改原始消息）
    try:
        return message.model_copy(update={"parts": new_parts})
    except Exception:
        return message


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


class DcSession:
    """一个 DC 模式会话的完整生命周期。

    - 内存态：history、turns、invocations、events 全部在内存中
    - 隔离：不触碰 RunRepository、RunEventEmitter、AgentOrchestrator
    - 并发：同一 session 同时只有一个 turn（asyncio.Lock）
    """

    def __init__(
        self,
        session_id: str,
        device_id: str,
        tier: DcToolTier,
        settings: Settings,
        artifacts: ArtifactStore,
        provider: DcChatProvider | MockDcChatProvider,
    ):
        self.session_id = session_id
        self.device_id = device_id
        self.tier = tier
        self.settings = settings
        self.artifacts = artifacts
        self.provider = provider
        self.created_at = utc_now()
        self.last_active_at = utc_now()

        # 设备层
        self.device = HarmonyDeviceAdapter(device_id, settings.hdc_path, settings.agent_action_timeout)
        self.hdc = DcHdcExecutor(device_id, settings.hdc_path, settings.agent_action_timeout)
        self.safety = DcShellPolicy()

        # 录制与事件
        self.recorder = DcActionRecorder()
        self.bus = DcEventBus(buffer_size=settings.dc_event_buffer_size)
        self.recorder.set_emitter(self._emit)

        # 对话状态
        self.history: list[Any] = []  # pydantic-ai ModelMessage 列表
        self.turns: list[DcTurnRecord] = []
        self.invocations: list[DcToolInvocation] = self.recorder.invocations  # 共享引用
        self.latest_snapshot: ScreenSnapshot | None = None
        self.snapshot_holder = DcSnapshotHolder()
        self.script: DcScriptArtifact | None = None

        # 并发控制
        self.status: Literal["idle", "thinking", "acting", "closed"] = "idle"
        self.lock = asyncio.Lock()
        self.cancel_event = asyncio.Event()
        self._current_task: asyncio.Task | None = None

        # 产物目录（dc- 前缀与 run- 天然分离）
        self.dir: Path = artifacts.run_dir(session_id)

    def _emit(self, event_type: DcEventType, message: str, payload: dict[str, Any] | None = None) -> None:
        """发射事件到总线。"""
        self.bus.emit(self.session_id, event_type, message, payload)

    async def start(self) -> None:
        """连接设备并发射 SESSION_CREATED 事件。"""
        await asyncio.to_thread(self.device.connect)
        health = await asyncio.to_thread(self.device.health_check)
        self._emit(
            DcEventType.SESSION_CREATED,
            "DC 会话已创建",
            {"device_id": self.device_id, "tier": self.tier.value, "health": health},
        )

    async def handle_user_message(self, text: str) -> DcTurnRecord:
        """处理一条用户消息：自主多步执行工具直到完成或阻塞。

        1. 加锁；status=thinking；emit TURN_STARTED
        2. 采集当前截图(JPEG)+UI树(top-K摘要)
        3. 历史裁剪
        4. 构造 DcChatRequest，调用 provider.chat()
        5. pydantic-ai 自动多轮调用 tools
        6. history = response.history；emit ASSISTANT_MESSAGE + TURN_FINISHED
        7. status=idle
        """
        async with self.lock:
            if self.status == "closed":
                raise DcSessionNotFound(f"session {self.session_id} is closed")

            turn_id = f"turn-{uuid.uuid4().hex[:8]}"
            turn = DcTurnRecord(turn_id=turn_id, user_message=text, status=DcTurnStatus.RUNNING)
            self.turns.append(turn)
            self.status = "thinking"
            self.last_active_at = utc_now()
            self.cancel_event.clear()

            self._emit(
                DcEventType.TURN_STARTED,
                f"用户消息：{text[:100]}",
                {"turn_id": turn_id, "user_message": text},
            )

            try:
                # 采集当前截图和 UI 树
                jpeg_bytes, ui_tree_digest = await self._capture_context()

                # 历史裁剪
                pruned_history = _prune_history(self.history, self.settings.dc_history_turns)

                # 构造工具上下文
                tool_context = DcToolContext(
                    session_id=self.session_id,
                    device=self.device,
                    hdc=self.hdc,
                    safety=self.safety,
                    recorder=self.recorder,
                    artifacts=self.artifacts,
                    snapshot_holder=self.snapshot_holder,
                    tier=self.tier,
                    turn_id=turn_id,
                    ui_tree_top_k=self.settings.dc_ui_tree_top_k,
                    action_timeout=self.settings.agent_action_timeout,
                )

                # 构造工具列表
                tools = build_tools(self.tier)

                # 构造请求
                request = DcChatRequest(
                    user_prompt=text,
                    screenshot_jpeg=jpeg_bytes,
                    ui_tree_digest=ui_tree_digest,
                    history=pruned_history,
                    tools=tools,
                    tool_context=tool_context,
                )

                # 调用 provider
                self.status = "acting"
                response: DcChatResponse | None = await self.provider.chat(request)

                if response is None:
                    raise DcProviderUnsupported(
                        f"provider {self.provider.name} does not support DC chat; "
                        "configure OPENAI_API_KEY and AGENT_MODEL or use mock provider"
                    )

                # 更新历史
                self.history = response.history

                # 更新 turn 记录
                turn.status = DcTurnStatus.COMPLETED
                turn.agent_summary = response.output_text
                turn.ended_at = utc_now()
                turn.invocation_ids = [inv.invocation_id for inv in self.recorder.invocations if inv.turn_id == turn_id]

                self._emit(
                    DcEventType.ASSISTANT_MESSAGE,
                    response.output_text[:200],
                    {"turn_id": turn_id, "summary": response.output_text, "tool_calls": response.tool_call_count},
                )
                self._emit(
                    DcEventType.TURN_FINISHED,
                    f"轮次完成：{response.tool_call_count} 次工具调用",
                    {"turn_id": turn_id, "status": turn.status.value, "tool_calls": response.tool_call_count},
                )

            except DcProviderUnsupported:
                turn.status = DcTurnStatus.FAILED
                turn.error = "provider does not support DC chat"
                turn.ended_at = utc_now()
                self._emit(DcEventType.ERROR, turn.error, {"turn_id": turn_id})
                raise

            except DcTurnBudgetExceeded as exc:
                turn.status = DcTurnStatus.BLOCKED
                turn.error = str(exc)
                turn.ended_at = utc_now()
                self._emit(DcEventType.ERROR, str(exc), {"turn_id": turn_id})

            except asyncio.CancelledError:
                turn.status = DcTurnStatus.CANCELLED
                turn.ended_at = utc_now()
                self._emit(DcEventType.TURN_FINISHED, "轮次已取消", {"turn_id": turn_id, "status": "cancelled"})
                raise

            except Exception as exc:
                turn.status = DcTurnStatus.FAILED
                turn.error = f"{type(exc).__name__}: {exc}"
                turn.ended_at = utc_now()
                self._emit(DcEventType.ERROR, turn.error, {"turn_id": turn_id})

            finally:
                self.status = "idle"
                self.last_active_at = utc_now()

            return turn

    async def _capture_context(self) -> tuple[bytes, str]:
        """采集当前截图(JPEG)和 UI 树摘要。

        Returns:
            (jpeg_bytes, ui_tree_digest_string)
        """
        screens_dir = self.dir / "screens"
        screens_dir.mkdir(parents=True, exist_ok=True)

        # JPEG 快速路径
        try:
            jpeg_path, jpeg_bytes, width, height = await asyncio.to_thread(
                self.hdc.screenshot_jpeg, screens_dir, f"dc_{int(time.time())}"
            )
            self.snapshot_holder.update_jpeg(jpeg_path, jpeg_bytes, width, height)
            self._emit(
                DcEventType.SCREENSHOT_CAPTURED,
                "已采集设备截图",
                {"width": width, "height": height, "sha256": self.snapshot_holder.latest_sha256},
            )
        except Exception:
            jpeg_bytes = self.snapshot_holder.latest_jpeg or b""

        # UI 树摘要
        ui_tree_digest = ""
        try:
            hierarchy = await asyncio.to_thread(self.device.collect_ui_hierarchy)
            from ..perception.normalizer import normalize_layout, page_path

            elements = normalize_layout(hierarchy, width if jpeg_bytes else 1080, height if jpeg_bytes else 2232)
            pp = page_path(hierarchy)
            top_k = self.settings.dc_ui_tree_top_k
            lines = [f"page={pp}, {len(elements)} elements (top {min(top_k, len(elements))})"]
            for el in elements[:top_k]:
                bbox = f"[{el.bbox.left},{el.bbox.top},{el.bbox.right},{el.bbox.bottom}]" if el.bbox else "no-bbox"
                flags = "".join(["C" if el.clickable else "", "E" if el.editable else "", "S" if el.scrollable else ""])
                lines.append(f"  {el.element_id}: {el.type} {bbox} {flags} key={el.key!r} text={el.content!r}")
            ui_tree_digest = "\n".join(lines)
            self._emit(
                DcEventType.UI_TREE_CAPTURED,
                "已采集 UI 层级",
                {"page_path": pp, "element_count": len(elements)},
            )
        except Exception:
            ui_tree_digest = "(UI hierarchy unavailable)"

        return jpeg_bytes, ui_tree_digest

    def generate_script(
        self,
        bundle_name: str = "com.example.app",
        main_ability: str = "EntryAbility",
    ) -> DcScriptArtifact:
        """从录制的操作生成 Hypium 脚本。"""
        generator = DcHypiumGenerator(self.artifacts)
        snapshots = [self.snapshot_holder.latest] if self.snapshot_holder.latest else []
        self.script = generator.generate(
            session_id=self.session_id,
            device_id=self.device_id,
            invocations=list(self.recorder.invocations),
            snapshots=snapshots,
            bundle_name=bundle_name,
            main_ability=main_ability,
        )
        self._emit(
            DcEventType.SCRIPT_GENERATED,
            "已生成 Hypium 脚本",
            {"included": self.script.included_operations, "omitted": len(self.script.omitted_operations)},
        )
        return self.script

    def change_tier(self, tier: DcToolTier) -> None:
        """变更工具层级（下轮生效）。"""
        old = self.tier
        self.tier = tier
        self._emit(
            DcEventType.TIER_CHANGED,
            f"工具层级从 L{old.value} 变更为 L{tier.value}",
            {"old_tier": old.value, "new_tier": tier.value},
        )

    def to_view(self) -> DcSessionView:
        """返回公开投影。"""
        return DcSessionView(
            session_id=self.session_id,
            device_id=self.device_id,
            tier=self.tier,
            status=self.status,
            created_at=self.created_at,
            turns=list(self.turns),
            invocations=list(self.recorder.invocations),
            latest_snapshot_path=str(self.snapshot_holder.latest_path) if self.snapshot_holder.latest_path else None,
            script=self.script,
        )

    async def close(self) -> None:
        """关闭会话：释放设备、发射事件。"""
        if self.status == "closed":
            return
        self.status = "closed"
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
            try:
                await self._current_task
            except asyncio.CancelledError, Exception:
                pass
        try:
            await asyncio.to_thread(self.device.close)
        except Exception:
            pass
        self._emit(DcEventType.SESSION_CLOSED, "DC 会话已关闭", {"session_id": self.session_id})


# ---------------------------------------------------------------------------
# 会话管理器
# ---------------------------------------------------------------------------


class DcSessionManager:
    """管理 DC 会话的内存字典、设备争用检查和 LRU 淘汰。

    与 ``RunManager`` 完全隔离：不共享 tasks/orchestrators/repository。
    """

    def __init__(self, settings: Settings, artifacts: ArtifactStore):
        self.settings = settings
        self.artifacts = artifacts
        self.sessions: dict[str, DcSession] = {}
        self._device_lock: dict[str, str] = {}  # device_id → session_id
        self._reaper_task: asyncio.Task | None = None

    def create(self, device_id: str | None = None, tier: DcToolTier | None = None) -> DcSession:
        """创建新会话。

        Raises:
            DcDeviceBusy: 设备已被其他会话占用。
        """
        resolved_device = device_id or self.settings.harmony_device
        resolved_tier = tier or DcToolTier(self.settings.dc_default_tier)

        # 设备争用检查
        existing = self._device_lock.get(resolved_device)
        if existing and existing in self.sessions:
            raise DcDeviceBusy(f"device {resolved_device} is already in use by session {existing}")

        # LRU 淘汰
        if len(self.sessions) >= self.settings.dc_max_sessions:
            self._evict_oldest()

        session_id = f"dc-{utc_now():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        provider = create_dc_provider(self.settings)
        session = DcSession(
            session_id=session_id,
            device_id=resolved_device,
            tier=resolved_tier,
            settings=self.settings,
            artifacts=self.artifacts,
            provider=provider,
        )
        self.sessions[session_id] = session
        self._device_lock[resolved_device] = session_id
        return session

    def get(self, session_id: str) -> DcSession:
        """获取会话；不存在时抛出 DcSessionNotFound。"""
        session = self.sessions.get(session_id)
        if session is None:
            raise DcSessionNotFound(f"session {session_id} not found")
        return session

    def get_or_none(self, session_id: str) -> DcSession | None:
        """获取会话；不存在时返回 None。"""
        return self.sessions.get(session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        """列出活跃会话摘要。"""
        return [
            {
                "session_id": s.session_id,
                "device_id": s.device_id,
                "tier": s.tier.value,
                "status": s.status,
                "created_at": s.created_at.isoformat(),
                "turn_count": len(s.turns),
                "invocation_count": len(s.recorder.invocations),
            }
            for s in self.sessions.values()
        ]

    async def close(self, session_id: str) -> bool:
        """关闭并移除会话。"""
        session = self.sessions.pop(session_id, None)
        if session is None:
            return False
        self._device_lock.pop(session.device_id, None)
        await session.close()
        return True

    async def shutdown(self, timeout: float = 8.0) -> None:
        """关闭所有会话（在 FastAPI lifespan 中调用）。"""
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError, Exception:
                pass
        sessions = list(self.sessions.values())
        self.sessions.clear()
        self._device_lock.clear()
        if not sessions:
            return
        await asyncio.wait_for(
            asyncio.gather(*(s.close() for s in sessions), return_exceptions=True),
            timeout=timeout,
        )

    def start_reaper(self) -> None:
        """启动后台 LRU 淘汰任务。"""
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reap_idle_sessions(), name="dc-session-reaper")

    async def _reap_idle_sessions(self) -> None:
        """每 60s 扫描一次，淘汰 idle_ttl 到期的会话。"""
        while True:
            await asyncio.sleep(60)
            ttl = self.settings.dc_idle_ttl_seconds
            to_close: list[str] = []
            for session_id, session in list(self.sessions.items()):
                if session.status == "closed":
                    to_close.append(session_id)
                    continue
                idle_seconds = (utc_now() - session.last_active_at).total_seconds()
                if idle_seconds > ttl:
                    to_close.append(session_id)
            for session_id in to_close:
                await self.close(session_id)

    def _evict_oldest(self) -> None:
        """淘汰最旧的 idle 会话以腾出空间。"""
        if not self.sessions:
            return
        oldest_id = min(
            self.sessions,
            key=lambda sid: self.sessions[sid].last_active_at,
        )
        oldest = self.sessions.pop(oldest_id, None)
        if oldest:
            self._device_lock.pop(oldest.device_id, None)
            # 异步关闭（fire-and-forget，reaper 会兜底）
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(oldest.close())
            except RuntimeError:
                pass
