"""DC 模式会话管理：DcEventBus + DcSession + DcSessionManager。

会话仅存在于内存中（服务重启后丢失），事件通过 asyncio.Queue 推送（非 SQLite 轮询），
端到端延迟 <20ms。与 Live Mode 的 RunManager/RunEventEmitter 完全隔离。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from ..cases.builder import NON_REPLAYABLE_DC_TOOLS
from ..config import Settings
from ..devices.harmony import HarmonyDeviceAdapter
from ..discovery import ProfileVerifier
from ..models import AnomalyFinding, ResolvedTarget, ScreenSnapshot, StableLocator, utc_now
from ..profiles import ProfileRegistry, ProfileRegistryError
from ..runner import make_hypium_runner
from ..storage.artifacts import ArtifactStore
from ..targets.catalog import parse_foreground_hierarchy
from .distill import DcProfileDistiller, infer_session_identity
from .generator import DcHypiumGenerator, _format_args
from .hdc import DcHdcExecutor
from .models import (
    DcChatRequest,
    DcChatResponse,
    DcContinuationContext,
    DcDistillResult,
    DcEffectStatus,
    DcEvent,
    DcEventType,
    DcModelTimeout,
    DcProviderUnsupported,
    DcScriptArtifact,
    DcSessionNotFound,
    DcSessionSnapshot,
    DcSessionSummary,
    DcSessionView,
    DcStepKind,
    DcStepRecord,
    DcTokenUsage,
    DcToolInvocation,
    DcToolName,
    DcToolStatus,
    DcToolTier,
    DcTurnBudgetExceeded,
    DcTurnRecord,
    DcTurnStatus,
    DcTurnTimeout,
    DcUsageLimitReached,
    is_system_foreground_bundle,
)
from .provider import DcChatProvider, MockDcChatProvider, create_dc_provider
from .safety import DcShellPolicy
from .store import DcSessionStore, copy_message_with_parts
from .tools import (
    DcActionRecorder,
    DcSnapshotHolder,
    DcToolContext,
    build_tools,
    relative_artifact_path,
)

logger = logging.getLogger(__name__)

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
        # token 级增量只是前端草稿，不进 Last-Event-ID 回放缓冲：
        # 否则一轮上千条 delta 会挤掉工具/终态事件的回放能力（buffer 只有 500 条），
        # 而重连客户端本来就靠 refreshSession 的全量快照重建消息与账本。
        # 实时订阅者照常收到（下面的广播不受影响）。
        if event_type != DcEventType.MESSAGE_DELTA:
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
    return copy_message_with_parts(message, new_parts)


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


class DcSession:
    """一个 DC 模式会话的完整生命周期。

    - 内存态：history、turns、invocations、events 全部在内存中
    - 持久化：``store`` 存在时把状态写入 ``<session_dir>/dc_session.json``，
      服务重启/空闲淘汰后仍可从历史会话列表恢复
    - 隔离：不触碰 RunRepository、RunEventEmitter、AgentOrchestrator
    - 并发：同一 session 同时只有一个 turn（``lock``）；同一设备同时只允许
      一个会话的 turn（``device_lock``，由 DcSessionManager 注入）
    """

    def __init__(
        self,
        session_id: str,
        device_id: str,
        tier: DcToolTier,
        settings: Settings,
        artifacts: ArtifactStore,
        provider: DcChatProvider | MockDcChatProvider,
        store: DcSessionStore | None = None,
        profile_registry: ProfileRegistry | None = None,
    ):
        self.session_id = session_id
        self.device_id = device_id
        self.tier = tier
        self.settings = settings
        self.artifacts = artifacts
        self.provider = provider
        self.store = store
        # Profile 资产访问：断言工具读稳定定位器，蒸馏写 Profile（2026-09-17 重构）。
        self.profile_registry = profile_registry
        self.created_at = utc_now()
        self.last_active_at = utc_now()

        # 设备层
        self.device = HarmonyDeviceAdapter(device_id, settings.hdc_path, settings.agent_action_timeout)
        self.hdc = DcHdcExecutor(device_id, settings.hdc_path, settings.agent_action_timeout)
        self.safety = DcShellPolicy()

        # 录制与事件
        self.recorder = DcActionRecorder(progress_interval=settings.dc_progress_interval)
        self.bus = DcEventBus(buffer_size=settings.dc_event_buffer_size)
        self.recorder.set_emitter(self._emit)

        # 对话状态
        self.history: list[Any] = []  # pydantic-ai ModelMessage 列表
        self.turns: list[DcTurnRecord] = []
        self.invocations: list[DcToolInvocation] = self.recorder.invocations  # 共享引用
        self.latest_snapshot: ScreenSnapshot | None = None
        self.snapshot_holder = DcSnapshotHolder()
        self.script: DcScriptArtifact | None = None

        # 公开连续性摘要与实时可观测状态（Phase 3）
        self.continuation: DcContinuationContext | None = None
        self.active_turn_id: str | None = None
        self.last_page_path: str | None = None
        self.last_foreground_app: str | None = None
        # 轮次结束时自动推断出的可复用身份（计划 7）：供生成脚本/蒸馏直接复用，不必手填。
        self.suggested_identity: tuple[str, str] | None = None
        # 会话自己观测到的「目标应用身份」(bundle, ability)：
        # 每次上下文采集都会 dump 一次 UI 层级，里面就带 focused 窗口的 bundleName/abilityName。
        # 只认第一个「非系统界面」的观测结果（后续可能切到桌面/输入法等，不应劫持会话身份）。
        # 这是脚本生成与 Profile 蒸馏的身份来源中优先级最高的一条（见 dc/distill.py）。
        self.observed_identity: tuple[str, str] | None = None
        # 副作用未确认的动作：新轮次必须先对账（Phase 4）
        self.pending_attention: DcToolInvocation | None = None
        # 会话内累计 token 用量（来自 provider 真实响应；前端输入框下方展示）
        self.token_usage = DcTokenUsage()
        # 运行中即时发现的应用异常（Phase 2）：由工具上下文写回，收尾落进 dc_session.json。
        self.defects: list[AnomalyFinding] = []
        self.cancel_requested = False
        self._last_checkpoint = 0.0
        self._context_version = 0
        self._checkpoint_futures: set[Any] = set()

        # 历史恢复标记（供前端提示上下文还原方式）
        self._restored = False
        self._restored_context: Literal["none", "full", "text"] = "none"

        # 并发控制
        self.status: Literal["idle", "thinking", "acting", "closed"] = "idle"
        self.lock = asyncio.Lock()
        self.cancel_event = asyncio.Event()
        self._current_task: asyncio.Task | None = None
        # 同一设备的轮次互斥锁（DcSessionManager 注入；None 表示不做设备级互斥）
        self.device_lock: asyncio.Lock | None = None

        # 产物目录（dc- 前缀与 run- 天然分离）
        self.dir: Path = artifacts.run_dir(session_id)

    def _emit(self, event_type: DcEventType, message: str, payload: dict[str, Any] | None = None) -> None:
        """发射事件到总线，并维护实时可观测状态与有界 checkpoint。"""
        payload = payload or {}
        if event_type == DcEventType.UI_TREE_CAPTURED:
            self.last_page_path = payload.get("page_path") or self.last_page_path
        if event_type == DcEventType.TOOL_CALL_FINISHED:
            self._track_invocation_outcome(payload.get("invocation_id"))
        self.bus.emit(self.session_id, event_type, message, payload)
        if event_type in (DcEventType.TOOL_CALL_STARTED, DcEventType.TOOL_CALL_FINISHED):
            # 工具开始/结束都写一个有界 checkpoint，进程中断后仍保留可继续的事实
            self._schedule_checkpoint()

    def _track_invocation_outcome(self, invocation_id: Any) -> None:
        """工具结束后更新前台应用/副作用状态，决定是否需要人工对账。"""
        if not isinstance(invocation_id, str):
            return
        invocation = self.recorder.by_id(invocation_id)
        if invocation is None:
            return
        if invocation.tool == DcToolName.FOREGROUND_APP and invocation.success:
            summary = invocation.result_summary
            if summary.startswith("bundle="):
                self.last_foreground_app = summary.split(",", 1)[0].removeprefix("bundle=").strip()
        if invocation.effect_status == DcEffectStatus.UNKNOWN and invocation.status != DcToolStatus.RUNNING:
            self.pending_attention = invocation

    def _schedule_checkpoint(self, *, force: bool = False) -> None:
        """按最小间隔写快照；写盘放到线程池，避免阻塞事件循环。"""
        now = time.monotonic()
        interval = float(self.settings.dc_checkpoint_interval)
        if not force and now - self._last_checkpoint < interval:
            return
        self._last_checkpoint = now
        self._context_version += 1
        self._emit_context_checkpoint()
        snapshot = self.snapshot()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write_snapshot(snapshot)
            return
        future = loop.run_in_executor(None, self._write_snapshot, snapshot)
        self._checkpoint_futures.add(future)
        future.add_done_callback(self._checkpoint_futures.discard)

    def _write_snapshot(self, snapshot: DcSessionSnapshot) -> None:
        if self.store is None:
            return
        try:
            self.store.save(snapshot)
        except (OSError, ValueError) as exc:  # noqa: BLE001 - 持久化失败不影响对话
            logger.warning("failed to checkpoint DC session %s: %s", self.session_id, exc)

    def _emit_context_checkpoint(self) -> None:
        self._emit(
            DcEventType.CONTEXT_CHECKPOINT,
            "已保存上下文检查点",
            {"turn_id": self.active_turn_id, "context_version": self._context_version},
        )

    def _rel_artifact(self, abs_path: Path | None) -> str | None:
        """把绝对产物路径转为会话目录相对 POSIX 路径（供前端 artifact URL 使用）。"""
        return relative_artifact_path(abs_path, self.dir)

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

        1. 加锁；检查设备是否被其他会话的轮次占用（占用则明确失败，不排队）
        2. 取设备轮次锁；status=thinking；emit TURN_STARTED
        3. 采集当前截图(JPEG)+UI树(top-K摘要)
        4. 历史裁剪
        5. 构造 DcChatRequest，调用 provider.chat()
        6. pydantic-ai 自动多轮调用 tools
        7. history = response.history；emit ASSISTANT_MESSAGE + TURN_FINISHED
        8. status=idle；落盘会话快照
        """
        async with self.lock:
            if self.status == "closed":
                raise DcSessionNotFound(f"session {self.session_id} is closed")
            if self.pending_attention is not None:
                return self._reject_needs_attention(text, self.pending_attention)
            device_lock = self.device_lock
            if device_lock is not None and device_lock.locked():
                return self._reject_busy_device(text)
            if device_lock is None:
                return await self._execute_turn(text)
            async with device_lock:
                return await self._execute_turn(text)

    def turn_gate(self) -> str | None:
        """返回阻止新轮次的原因（``None`` 表示可以开始）。"""
        if self.status == "closed":
            return "session is closed"
        if self.status in ("thinking", "acting"):
            return "session is busy processing a previous message"
        if self.pending_attention is not None:
            invocation = self.pending_attention
            return (
                f"device effect of {invocation.tool.value} ({invocation.invocation_id}) is unconfirmed; "
                "resolve it before starting a new turn"
            )
        return None

    def attention_payload(self) -> dict[str, Any]:
        """构造 needs_attention 事件载荷（含可处置选项）。"""
        invocation = self.pending_attention
        return {
            "turn_id": invocation.turn_id if invocation else self.active_turn_id,
            "invocation_id": invocation.invocation_id if invocation else None,
            "tool": invocation.tool.value if invocation else None,
            "reason": (
                f"{invocation.tool.value} 的结果未确认（{invocation.status.value}），设备状态可能与最后一次观测不一致"
            )
            if invocation
            else "上一轮存在未确认的设备副作用",
            "options": ["reobserve", "confirm_effect", "retry", "terminate"],
        }

    def _reject_needs_attention(self, text: str, invocation: DcToolInvocation) -> DcTurnRecord:
        """设备副作用未确认：记录一轮 NEEDS_ATTENTION 并发事件，不驱动设备。"""
        turn = DcTurnRecord(
            turn_id=f"turn-{uuid.uuid4().hex[:8]}",
            user_message=text,
            status=DcTurnStatus.NEEDS_ATTENTION,
            ended_at=utc_now(),
        )
        turn.error = f"{invocation.tool.value} ({invocation.invocation_id}) 的副作用未确认，请先重新观测或确认设备状态"
        self.turns.append(turn)
        self._emit(DcEventType.NEEDS_ATTENTION, turn.error, self.attention_payload())
        self._emit(
            DcEventType.TURN_FINISHED,
            "轮次未开始：需要人工确认设备状态",
            {"turn_id": turn.turn_id, "status": turn.status.value},
        )
        self.save_state()
        return turn

    def _reject_busy_device(self, text: str) -> DcTurnRecord:
        """设备已被其他会话的轮次占用：记录并发出可读错误，不排队。"""
        turn = DcTurnRecord(
            turn_id=f"turn-{uuid.uuid4().hex[:8]}",
            user_message=text,
            status=DcTurnStatus.BLOCKED,
            ended_at=utc_now(),
        )
        turn.error = f"device {self.device_id} is busy with another DC session"
        self.turns.append(turn)
        self._emit(DcEventType.ERROR, turn.error, {"turn_id": turn.turn_id, "status": "blocked"})
        self.save_state()
        return turn

    async def _execute_turn(self, text: str) -> DcTurnRecord:
        """执行一轮对话（调用方已持有会话锁与设备轮次锁）。"""
        turn_id = f"turn-{uuid.uuid4().hex[:8]}"
        turn = DcTurnRecord(turn_id=turn_id, user_message=text, status=DcTurnStatus.RUNNING)
        self.turns.append(turn)
        self.status = "thinking"
        self.last_active_at = utc_now()
        self.cancel_event.clear()
        self.cancel_requested = False
        self.active_turn_id = turn_id
        turn_deadline = utc_now() + timedelta(seconds=self.settings.dc_turn_timeout)

        self._emit(
            DcEventType.TURN_STARTED,
            f"用户消息：{text[:100]}",
            {
                "turn_id": turn_id,
                "user_message": text,
                "turn_deadline_at": turn_deadline.isoformat(),
            },
        )

        outcome = DcTurnStatus.FAILED
        # Provider 每轮的用量增量恰好被消费一次：成功路径立即入账，失败路径走 fallback
        # 读取尚未被消费的增量；两条路径不会重复计数（见 _account_provider_usage）。
        try:
            # 采集当前截图和 UI 树（恢复/继续时这一步就是「先重新观测」）
            self._emit(
                DcEventType.CONTEXT_CAPTURE_STARTED,
                "正在采集当前设备上下文",
                {"turn_id": turn_id},
            )
            jpeg_bytes, ui_tree_digest = await self._capture_context()
            self._emit(
                DcEventType.CONTEXT_CAPTURE_FINISHED,
                "设备上下文采集完成",
                {"turn_id": turn_id, "page_path": self.last_page_path},
            )

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
                session_dir=self.dir,
                snapshot_holder=self.snapshot_holder,
                tier=self.tier,
                turn_id=turn_id,
                ui_tree_top_k=self.settings.dc_ui_tree_top_k,
                action_timeout=self.settings.agent_action_timeout,
                progress_interval=self.settings.dc_progress_interval,
                stable_locators=self._linked_stable_locators(),
                bundle_name=self.last_foreground_app or "",
                defects=self.defects,
            )

            # 构造工具列表
            tools = build_tools(self.tier)

            # 构造请求；emit 回调让 provider 在执行的每一步实时回传
            # thinking/agent_text 事件，并附加 turn_id 供前端按轮分组。
            request = DcChatRequest(
                user_prompt=text,
                screenshot_jpeg=jpeg_bytes,
                ui_tree_digest=ui_tree_digest,
                history=pruned_history,
                tools=tools,
                tool_context=tool_context,
                emit=lambda etype, msg, payload: self._emit_from_provider(turn, etype, msg, payload),
                turn_id=turn_id,
                model_timeout=self.settings.agent_model_timeout,
                progress_interval=self.settings.dc_progress_interval,
                turn_deadline=turn_deadline,
                continuation_prompt=self._continuation_prompt(),
            )

            # 调用 provider（外层再用轮次剩余预算兜底）
            self.status = "acting"
            remaining = max(0.1, (turn_deadline - utc_now()).total_seconds())
            try:
                response: DcChatResponse | None = await asyncio.wait_for(
                    self.provider.chat(request),
                    timeout=remaining,
                )
            except TimeoutError as exc:
                raise DcTurnTimeout(f"DC turn exceeded {self.settings.dc_turn_timeout}s budget") from exc

            if response is None:
                raise DcProviderUnsupported(
                    f"provider {self.provider.name} does not support DC chat; "
                    "configure OPENAI_API_KEY and AGENT_MODEL or use mock provider"
                )

            # 更新历史
            self.history = response.history

            # 更新 turn 记录
            turn.agent_summary = response.output_text
            outcome = DcTurnStatus.COMPLETED

            # 用量事件先于终态回复发出：前端在同一批次里就能刷新输入框下方的统计行
            self._account_provider_usage(turn_id)

            self._emit(
                DcEventType.ASSISTANT_MESSAGE,
                response.output_text[:200],
                {"turn_id": turn_id, "summary": response.output_text, "tool_calls": response.tool_call_count},
            )

        except DcProviderUnsupported:
            turn.error = "provider does not support DC chat"
            self._emit(DcEventType.ERROR, turn.error, {"turn_id": turn_id})
            raise

        except DcTurnBudgetExceeded as exc:
            outcome = DcTurnStatus.BLOCKED
            turn.error = str(exc)
            self._emit(DcEventType.ERROR, str(exc), {"turn_id": turn_id})

        except DcUsageLimitReached as exc:
            # 预算不足不是设备/模型故障：归为 blocked，并在文案里给出可调大的环境变量，
            # 让用户能把失败原因和下一步动作对应起来（历史事故里这一层只留下英文裸异常）。
            outcome = DcTurnStatus.BLOCKED
            # 预算耗尽前已经发生的请求同样要计入用量，否则最需要看数字的场景反而没有数据
            self._account_provider_usage(turn_id)
            env_names = []
            if exc.request_limit is not None:
                env_names.append("DC_MODEL_REQUEST_LIMIT")
            if exc.tool_calls_limit is not None:
                env_names.append("DC_MODEL_TOOL_CALLS_LIMIT")
            hint = f"，可调大 {' / '.join(env_names)} 后重试，或把任务拆成更小的步骤继续" if env_names else ""
            turn.error = f"{exc}{hint}"
            self._emit(
                DcEventType.ERROR,
                turn.error,
                {
                    "turn_id": turn_id,
                    "error_code": "usage_limit",
                    "request_limit": exc.request_limit,
                    "tool_calls_limit": exc.tool_calls_limit,
                },
            )

        except (DcModelTimeout, DcTurnTimeout) as exc:
            outcome = DcTurnStatus.FAILED
            turn.error = f"{type(exc).__name__}: {exc}"
            self._emit(
                DcEventType.ERROR,
                turn.error,
                {
                    "turn_id": turn_id,
                    "error_code": ("model_timeout" if isinstance(exc, DcModelTimeout) else "turn_timeout"),
                },
            )

        except asyncio.CancelledError:
            outcome = DcTurnStatus.CANCELLED if self.cancel_requested else DcTurnStatus.INTERRUPTED
            turn.error = "轮次已取消" if outcome == DcTurnStatus.CANCELLED else "轮次被中断"
            self._emit(
                DcEventType.TURN_INTERRUPTED,
                turn.error,
                {"turn_id": turn_id, "reason": outcome.value},
            )
            self._finalize_turn(turn, outcome)
            raise

        except Exception as exc:
            outcome = DcTurnStatus.FAILED
            turn.error = f"{type(exc).__name__}: {exc}"
            self._emit(DcEventType.ERROR, turn.error, {"turn_id": turn_id})

        self._finalize_turn(turn, outcome)
        return turn

    def _account_provider_usage(self, turn_id: str) -> DcTokenUsage:
        """把 provider 的「本轮用量增量」并入会话累计，并发用量更新事件。

        增量恰好被消费一次：成功路径在拿到 response 后立即调用，失败路径
        （预算耗尽/超时/取消）调用时该增量尚未被消费，因此不会漏记也不会重复。
        Mock provider（无真实模型）或 provider 未上报时返回全 0，前端据此显示空态。

        Returns:
            本轮增量（已并入 ``self.token_usage``）。
        """
        delta = DcTokenUsage()
        last_delta = getattr(self.provider, "last_usage_delta", None)
        delta_turn_id = getattr(self.provider, "usage_delta_turn_id", None)
        # 仅接受属于本轮的增量：否则「本轮在调用模型前就失败」会把上一轮的增量重复入账
        if isinstance(last_delta, DcTokenUsage) and last_delta.has_values and delta_turn_id == turn_id:
            delta = last_delta
            self.token_usage = self.token_usage.plus(delta)
        # 无增量时也发一次：让前端拿到权威累计值（幂等覆盖），避免只能靠刷新才能对账
        self._emit(
            DcEventType.TOKEN_USAGE_UPDATED,
            f"本轮用量：{delta.total_tokens} tokens",
            {
                "turn_id": turn_id,
                "session": self.token_usage.model_dump(mode="json"),
                "turn": delta.model_dump(mode="json"),
                "context_window": self.settings.dc_model_context_window,
                "request_limit": self.settings.dc_model_request_limit,
            },
        )
        return delta

    def _finalize_turn(self, turn: DcTurnRecord, outcome: DcTurnStatus) -> None:
        """统一收尾：invocation_ids、连续性摘要、终态事件、落盘。

        正常完成、失败、超时、取消、异常五条路径都走这里，因此
        ``turn.invocation_ids`` 与实际发生的工具调用始终一致。
        """
        turn.ended_at = utc_now()
        turn.invocation_ids = [inv.invocation_id for inv in self.recorder.turn_invocations(turn.turn_id)]
        self._flag_unresolved_effects(turn.turn_id)
        turn.status = DcTurnStatus.NEEDS_ATTENTION if self.pending_attention is not None else outcome
        self.continuation = self._build_continuation(turn)
        self.active_turn_id = None
        self.status = "idle"
        self.last_active_at = utc_now()

        if self.pending_attention is not None:
            self._emit(DcEventType.NEEDS_ATTENTION, "存在未确认的设备副作用", self.attention_payload())
        self._emit(
            DcEventType.TURN_FINISHED,
            f"轮次结束：{turn.status.value}",
            {
                "turn_id": turn.turn_id,
                "status": turn.status.value,
                "tool_calls": len(turn.invocation_ids),
                "error": turn.error,
            },
        )
        self._schedule_checkpoint(force=True)
        self._suggest_reusable_case(turn)
        self.save_state()

    def _suggest_reusable_case(self, turn: DcTurnRecord) -> None:
        """轮次成功且有可回放录制时自动推断身份并发 ``CASE_SUGGESTED``（计划 7/R16）。

        本次 DC 会话跑通了任务却 ``bundle_name: None``、``generated/`` 为空：任务完成却没留下
        任何可复用产物，用户点「生成脚本」还要手填 bundle。这里只做「推断 + 提示」，
        **不**自动写盘生成脚本（避免产物膨胀），身份同时写回会话供生成/蒸馏直接使用。
        """
        if not self.settings.dc_auto_resolve_identity:
            return
        if turn.status != DcTurnStatus.COMPLETED:
            return
        invocations = self.recorder.turn_invocations(turn.turn_id)
        replayable = [inv for inv in invocations if inv.tool not in NON_REPLAYABLE_DC_TOOLS and inv.success]
        if not replayable:
            return
        identity = infer_session_identity(self)
        if identity is None:
            return
        bundle_name, main_ability = identity
        self.suggested_identity = identity
        self._emit(
            DcEventType.CASE_SUGGESTED,
            f"本轮可沉淀为用例：{bundle_name}/{main_ability}（{len(replayable)} 步）",
            {
                "session_id": self.session_id,
                "turn_id": turn.turn_id,
                "bundle_name": bundle_name,
                "main_ability": main_ability,
                "replayable_steps": len(replayable),
            },
        )

    def _flag_unresolved_effects(self, turn_id: str) -> None:
        """兜底扫描：本轮仍存在运行中或副作用未知的调用时必须人工对账。

        事件驱动的 ``_track_invocation_outcome`` 覆盖正常路径；这里再按账本兜底，
        避免取消/进程中断导致事件缺失时把未知副作用当成可以继续。
        """
        if self.pending_attention is not None:
            return
        for invocation in reversed(self.recorder.turn_invocations(turn_id)):
            if invocation.status == DcToolStatus.RUNNING or invocation.effect_status == DcEffectStatus.UNKNOWN:
                self.pending_attention = invocation
                return

    def _continuation_prompt(self) -> str:
        """返回注入下一轮 prompt 的公开连续性摘要。"""
        if self.continuation is None:
            return ""
        return self.continuation.to_prompt()

    def _build_continuation(self, turn: DcTurnRecord) -> DcContinuationContext:
        """从轮次记录与工具账本生成公开连续性摘要（不含隐藏推理）。"""
        invocations = self.recorder.turn_invocations(turn.turn_id)
        completed = [
            f"{inv.tool.value}({_format_args(inv.args)}) -> {inv.status.value}"
            for inv in invocations
            if inv.status == DcToolStatus.SUCCEEDED
        ]
        unresolved = [
            inv
            for inv in invocations
            if inv.status == DcToolStatus.RUNNING or inv.effect_status == DcEffectStatus.UNKNOWN
        ]
        unknown_effect = next((inv for inv in unresolved if inv.effect_status == DcEffectStatus.UNKNOWN), None)
        marker = unknown_effect or (unresolved[0] if unresolved else None)
        steps = [rec.text for rec in turn.steps if rec.kind == DcStepKind.AGENT_TEXT and rec.text.strip()]
        if unknown_effect is not None:
            effect = DcEffectStatus.UNKNOWN
        elif completed:
            effect = DcEffectStatus.CONFIRMED
        else:
            effect = DcEffectStatus.NONE
        return DcContinuationContext(
            previous_turn_id=turn.turn_id,
            previous_status=turn.status,
            original_user_goal=turn.user_message,
            public_agent_summary=turn.agent_summary,
            public_agent_steps=steps[-12:],
            completed_operations=completed[-20:],
            active_or_unknown_operation=(
                f"{marker.tool.value}({_format_args(marker.args)}) 状态={marker.status.value}" if marker else None
            ),
            last_snapshot_path=self._rel_artifact(self.snapshot_holder.latest_path),
            last_page_path=self.last_page_path,
            last_foreground_app=self.last_foreground_app,
            last_progress_at=utc_now(),
            effect_status=effect,
            reconcile_required=unknown_effect is not None,
            context_version=self._context_version,
        )

    def request_stop(self) -> bool:
        """请求停止当前轮次：先置取消标记，再取消任务。

        Returns:
            True 表示确实取消了一个正在执行的轮次。
        """
        self.cancel_requested = True
        self._emit(
            DcEventType.TURN_CANCEL_REQUESTED,
            "已请求停止当前轮次",
            {"turn_id": self.active_turn_id, "requested_at": utc_now().isoformat()},
        )
        task = self._current_task
        if task is not None and not task.done():
            task.cancel()
            return True
        return False

    async def resolve_attention(self, action: str) -> DcSessionView:
        """处置未确认的设备副作用（Phase 4）。

        ``reobserve`` 会重新采集截图/UI 层级并清除阻塞；其余动作只清除阻塞
        （``confirm_effect``/``retry``/``terminate`` 由用户语义决定下一步）。
        """
        if action not in ("reobserve", "confirm_effect", "retry", "terminate"):
            raise ValueError(f"unsupported attention action: {action}")
        if action == "reobserve":
            self._emit(DcEventType.CONTEXT_CAPTURE_STARTED, "正在重新观测设备", {"turn_id": None})
            await self._capture_context()
            self._emit(
                DcEventType.CONTEXT_CAPTURE_FINISHED,
                "设备重新观测完成",
                {"turn_id": None, "page_path": self.last_page_path},
            )
        self.pending_attention = None
        if self.continuation is not None:
            self.continuation = self.continuation.model_copy(
                update={"reconcile_required": False, "effect_status": DcEffectStatus.CONFIRMED}
            )
        self._emit(DcEventType.CONTEXT_CHECKPOINT, f"已处置未确认动作：{action}", {"turn_id": None})
        self._schedule_checkpoint(force=True)
        self.save_state()
        return self.to_view()

    def _emit_from_provider(self, turn: DcTurnRecord, event_type: str, message: str, payload: dict[str, Any]) -> None:
        """Provider 执行期回调：附加 turn_id 后发射，并把步骤记入 turn。

        ``THINKING`` / ``AGENT_TEXT`` 同时写入 ``turn.steps``，使刷新历史
        （``to_view`` + 前端全量重建）后仍能还原思考/叙述块。
        """
        try:
            resolved = DcEventType(event_type)
        except ValueError:
            return
        text = str(payload.get("text", ""))
        if resolved in (DcEventType.THINKING, DcEventType.AGENT_TEXT):
            kind = DcStepKind.THINKING if resolved == DcEventType.THINKING else DcStepKind.AGENT_TEXT
            turn.steps.append(DcStepRecord(step=int(payload.get("step", 0) or 0), kind=kind, text=text))
        self._emit(resolved, message, {**payload, "turn_id": turn.turn_id})

    async def _capture_context(self) -> tuple[bytes, str]:
        """采集当前截图(JPEG)和 UI 树摘要。

        Returns:
            (jpeg_bytes, ui_tree_digest_string)
        """
        screens_dir = self.dir / "screens"
        screens_dir.mkdir(parents=True, exist_ok=True)

        # JPEG 快速路径
        width = 0
        height = 0
        try:
            jpeg_path, jpeg_bytes, width, height = await asyncio.to_thread(
                self.hdc.screenshot_jpeg, screens_dir, f"dc_{int(time.time())}"
            )
            self.snapshot_holder.update_jpeg(jpeg_path, jpeg_bytes, width, height)
            self._emit(
                DcEventType.SCREENSHOT_CAPTURED,
                "已采集设备截图",
                {
                    "snapshot_path": self._rel_artifact(jpeg_path),
                    "width": width,
                    "height": height,
                    "sha256": self.snapshot_holder.latest_sha256,
                    "source": "context",
                },
            )
        except Exception as exc:
            jpeg_bytes = self.snapshot_holder.latest_jpeg or b""
            # 失败时明确告知前端：保持上一帧，不要拼出非法 URL
            self._emit(
                DcEventType.SCREENSHOT_CAPTURED,
                "截图采集失败",
                {
                    "snapshot_path": None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "source": "context",
                },
            )

        # UI 树摘要
        ui_tree_digest = ""
        try:
            hierarchy = await asyncio.to_thread(self.device.collect_ui_hierarchy)
            from ..perception.normalizer import normalize_layout, page_path

            self._observe_foreground_identity(hierarchy)
            fallback_width = width or 1080
            fallback_height = height or 2232
            elements = normalize_layout(hierarchy, fallback_width, fallback_height)
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

    def _observe_foreground_identity(self, hierarchy: Any) -> None:
        """从刚采到的 UI 层级里记住「目标应用身份」``(bundle, ability)``。

        设备对 ``foreground_app`` 的 ability 常报 ``unknown``（桌面尤其如此），而脚本生成与
        Profile 蒸馏都需要真实 bundle+ability 才能产出可回放资产。focused 窗口的
        ``bundleName``/``abilityName`` 本来就在每次采集的层级里，所以这里直接解析并记住，
        不依赖模型主动调用工具（30e 复盘：模型只在任务开始时调了一次 foreground_app，
        那一次记到的是桌面 + ability=unknown，导致整条身份链失效）。

        只认第一次命中的非系统界面：会话中途可能回到桌面、弹出输入法，它们不应改写会话身份。
        观测失败静默跳过，绝不影响上下文采集主流程。
        """
        if self.observed_identity is not None:
            return
        try:
            foreground = parse_foreground_hierarchy(hierarchy)
        except Exception:  # noqa: BLE001 - 观测是尽力而为，失败不影响采集
            return
        if foreground is None:
            return
        bundle = (foreground.bundle_name or "").strip()
        ability = (foreground.ability_name or "").strip()
        if not ability or is_system_foreground_bundle(bundle):
            return
        self.observed_identity = (bundle, ability)

    @property
    def snapshots(self) -> list[ScreenSnapshot]:
        """会话内采集过的帧（有界历史）；DC 蒸馏据此重建可回放核心流。"""
        return self.snapshot_holder.history

    def _linked_stable_locators(self) -> list[StableLocator]:
        """返回当前会话关联 Profile 的稳定定位器；无 registry/Profile 时为空列表。

        关联键是最近一次 ``foreground_app`` 观测到的 bundle name：会话尚未观测前台
        应用时无从判断关联 Profile，此时断言语义退化为模糊文本匹配（与 Live Mode
        在没有稳定定位器时的行为一致）。
        """
        registry = self.profile_registry
        bundle_name = self.last_foreground_app
        if registry is None or not bundle_name:
            return []
        try:
            profile = registry.get_any(bundle_name=bundle_name)
        except ProfileRegistryError:
            return []
        if profile is None:
            return []
        return list(profile.stable_locator_inventory)

    def generate_script(
        self,
        bundle_name: str | None = None,
        main_ability: str | None = None,
    ) -> DcScriptArtifact:
        """从录制的操作生成 Hypium 脚本。

        应用身份缺省（或只给了一半）时按会话录制推断，与蒸馏端点共用同一推断器；
        推断不出则回退占位身份 ``com.example.app`` / ``EntryAbility``——脚本仍会生成，
        但只作为诊断脚本（占位警告保留，行为与改动前一致）。
        """
        explicit = (bundle_name, main_ability) if (bundle_name and main_ability) else None
        # 计划 7：优先用轮次结束自动推断出的身份，其次现场推断，最后才是占位身份。
        identity = explicit or self.suggested_identity or infer_session_identity(self)
        resolved_bundle, resolved_ability = identity or ("com.example.app", "EntryAbility")
        generator = DcHypiumGenerator(self.artifacts, min_observed_rounds=self.settings.profile_verification_rounds)
        snapshots = [self.snapshot_holder.latest] if self.snapshot_holder.latest else []
        self.script = generator.generate(
            session_id=self.session_id,
            device_id=self.device_id,
            invocations=list(self.recorder.invocations),
            snapshots=snapshots,
            bundle_name=resolved_bundle,
            main_ability=resolved_ability,
        )
        self._emit(
            DcEventType.SCRIPT_GENERATED,
            "已生成 Hypium 脚本",
            {"included": self.script.included_operations, "omitted": len(self.script.omitted_operations)},
        )
        self.save_state()
        return self.script

    async def distill_profile(self, bundle_name: str, main_ability: str) -> DcDistillResult:
        """从当前会话蒸馏 Profile 资产（1 轮设备验证 + 1 次 Hypium 回放）。

        与用户轮次互斥（``self.lock``）：蒸馏要驱动设备，不能与正在执行的轮次并发。
        任何失败都会发射 ``PROFILE_DISTILL_FAILED`` 并向上抛 ``DcError``。
        """
        self._emit(
            DcEventType.PROFILE_DISTILL_STARTED,
            "开始蒸馏 Profile",
            {"session_id": self.session_id, "bundle_name": bundle_name, "main_ability": main_ability},
        )
        try:
            async with self.lock:
                distiller = DcProfileDistiller(
                    self.artifacts,
                    self.profile_registry,
                    self.settings,
                    verifier_factory=self._profile_verifier,
                    runner=make_hypium_runner(
                        self.settings,
                        bundle_name=bundle_name,
                        device_id=self.device_id,
                        subject="dc_script",
                        session_id=self.session_id,
                    ),
                )
                result = await distiller.distill(self, bundle_name, main_ability)
        except Exception as exc:
            self._emit(
                DcEventType.PROFILE_DISTILL_FAILED,
                f"Profile 蒸馏失败：{exc}",
                {"session_id": self.session_id, "error": str(exc), "error_type": type(exc).__name__},
            )
            raise
        self._emit(
            DcEventType.PROFILE_DISTILL_FINISHED,
            f"Profile 蒸馏完成：{result.profile_id}（{result.status}）",
            result.model_dump(mode="json"),
        )
        self.save_state()
        return result

    def _profile_verifier(self, target: ResolvedTarget, output_dir: Path, run_id: str) -> ProfileVerifier:
        """构造绑定本会话设备的 ProfileVerifier（轮次取 Settings）。"""
        return ProfileVerifier(
            self.device,
            target,
            output_dir,
            run_id,
            rounds=self.settings.profile_verification_rounds,
            should_stop=lambda: self.cancel_requested,
        )

    def change_tier(self, tier: DcToolTier) -> None:
        """变更工具层级（下轮生效）。"""
        old = self.tier
        self.tier = tier
        self._emit(
            DcEventType.TIER_CHANGED,
            f"工具层级从 L{old.value} 变更为 L{tier.value}",
            {"old_tier": old.value, "new_tier": tier.value},
        )
        self.save_state()

    def to_view(self) -> DcSessionView:
        """返回公开投影（含运行中的工具记录与连续性摘要）。"""
        return DcSessionView(
            session_id=self.session_id,
            device_id=self.device_id,
            tier=self.tier,
            status=self.status,
            created_at=self.created_at,
            turns=list(self.turns),
            invocations=list(self.recorder.invocations),
            latest_snapshot_path=self._rel_artifact(self.snapshot_holder.latest_path),
            script=self.script,
            restored=self._restored,
            restored_context=self._restored_context,
            active_turn_id=self.active_turn_id,
            continuation=self.continuation,
            token_usage=self.token_usage,
            suggested_bundle_name=self.suggested_identity[0] if self.suggested_identity else None,
            suggested_main_ability=self.suggested_identity[1] if self.suggested_identity else None,
            suggested_step_count=self._suggested_step_count() if self.suggested_identity else 0,
        )

    def _suggested_step_count(self) -> int:
        """当前录制里可回放的步骤数（与生成脚本时的口径一致）。"""
        return sum(
            1
            for invocation in self.recorder.invocations
            if invocation.tool not in NON_REPLAYABLE_DC_TOOLS and invocation.success
        )

    # ------------------------------------------------------------------
    # 会话快照：落盘 / 恢复
    # ------------------------------------------------------------------

    def snapshot(self) -> DcSessionSnapshot:
        """构造可落盘的会话快照（history 已剥离图片与 thinking）。"""
        history = self._history_payload()
        return DcSessionSnapshot(
            session_id=self.session_id,
            device_id=self.device_id,
            tier=self.tier,
            status=self.status,
            created_at=self.created_at,
            last_active_at=self.last_active_at,
            turns=list(self.turns),
            invocations=list(self.recorder.invocations),
            script=self.script,
            latest_snapshot_path=self._rel_artifact(self.snapshot_holder.latest_path),
            history=history,
            history_kind="model_messages" if history else "none",
            continuation=self._synced_continuation(),
            token_usage=self.token_usage,
            defects=self.defects,
        )

    def _synced_continuation(self) -> DcContinuationContext | None:
        """把会话级实时状态同步进连续性摘要后再落盘，保证恢复后面板/提示一致。"""
        if self.continuation is None:
            return None
        return self.continuation.model_copy(
            update={
                "last_snapshot_path": self._rel_artifact(self.snapshot_holder.latest_path),
                "last_page_path": self.last_page_path,
                "last_foreground_app": self.last_foreground_app,
                "context_version": self._context_version,
            }
        )

    def _history_payload(self) -> list[Any]:
        """按 history 上限裁剪并序列化模型上下文；无 store 时返回空。"""
        if self.store is None or not self.history:
            return []
        limit = self.settings.dc_history_turns * 2 + 1
        history = self.history[-limit:] if len(self.history) > limit else self.history
        return self.store.dump_history(history)

    def save_state(self) -> None:
        """把当前会话状态写入磁盘快照；失败只记日志，绝不影响对话主流程。"""
        if self.store is None:
            return
        try:
            self.store.save(self.snapshot())
        except (OSError, ValueError) as exc:
            logger.warning("failed to persist DC session %s: %s", self.session_id, exc)

    def apply_snapshot(self, snapshot: DcSessionSnapshot) -> None:
        """从磁盘快照恢复历史会话状态（turns / 工具记录 / 脚本 / 模型上下文）。"""
        self.created_at = snapshot.created_at
        self.last_active_at = utc_now()
        self.status = "idle"
        self.turns = list(snapshot.turns)
        self.recorder.invocations = list(snapshot.invocations)
        self.script = snapshot.script
        self.continuation = snapshot.continuation
        self.token_usage = snapshot.token_usage or DcTokenUsage()
        self.defects = list(snapshot.defects)
        self._context_version = snapshot.continuation.context_version if snapshot.continuation else 0
        self.history = self._restore_history(snapshot)
        self._restored = True
        # 恢复的最后一帧截图只是历史证据，不代表当前设备状态：恢复后必须先重新观测
        self.last_page_path = snapshot.continuation.last_page_path if snapshot.continuation else None
        self.last_foreground_app = snapshot.continuation.last_foreground_app if snapshot.continuation else None
        self.pending_attention = self._restore_unresolved(snapshot)
        if self.continuation is not None and self.pending_attention is not None:
            self.continuation = self.continuation.model_copy(update={"reconcile_required": True})
        # 恢复最近一帧截图：相对路径 → 会话目录内的绝对路径（供 to_view 复用）
        self.snapshot_holder.latest = None
        self.snapshot_holder.latest_path = self._restore_snapshot_path(snapshot.latest_snapshot_path)

    def _restore_unresolved(self, snapshot: DcSessionSnapshot) -> DcToolInvocation | None:
        """恢复时把「副作用未知」的调用重新列为待对账，禁止自动重放。"""
        for invocation in reversed(snapshot.invocations):
            if invocation.effect_status == DcEffectStatus.UNKNOWN and invocation.status != DcToolStatus.RUNNING:
                return invocation
            if invocation.status == DcToolStatus.RUNNING:
                return invocation
        return None

    def _restore_snapshot_path(self, relative_path: str | None) -> Path | None:
        if not relative_path or self.store is None:
            return None
        candidate = (self.dir / relative_path).resolve()
        return candidate if candidate.is_file() and candidate.is_relative_to(self.dir.resolve()) else None

    def _restore_history(self, snapshot: DcSessionSnapshot) -> list[Any]:
        """优先还原完整模型上下文，失败时按 turns 重建纯文本上下文。"""
        if self.store is not None and snapshot.history_kind == "model_messages":
            restored = self.store.load_history(snapshot.history)
            if restored:
                self._restored_context = "full"
                return restored
        rebuilt = self._rebuild_history_from_turns()
        self._restored_context = "text" if rebuilt else "none"
        return rebuilt

    def _rebuild_history_from_turns(self) -> list[Any]:
        """用录制的轮次重建纯文本上下文（图片与工具原始输出不重放）。"""
        from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

        rebuilt: list[Any] = []
        for turn in self.turns:
            if not turn.user_message:
                continue
            rebuilt.append(ModelRequest(parts=[UserPromptPart(content=turn.user_message)]))
            summary = turn.agent_summary or self._turn_digest(turn)
            if summary:
                rebuilt.append(ModelResponse(parts=[TextPart(content=summary)]))
        return rebuilt

    def _turn_digest(self, turn: DcTurnRecord) -> str:
        """把一轮的工具调用压缩成一行摘要，作为缺失总结时的上下文替身。"""
        lines: list[str] = []
        for inv in self.recorder.invocations:
            if inv.turn_id != turn.turn_id:
                continue
            outcome = "ok" if inv.success else f"failed: {inv.error or ''}"
            lines.append(f"{inv.tool.value}({_format_args(inv.args)}) -> {outcome}")
        if not lines:
            return ""
        return "工具调用记录：\n" + "\n".join(lines)

    async def close(self) -> None:
        """关闭会话：有界等待当前轮次收尾、释放设备、落盘快照、发射事件。

        等待超时后把仍在运行的轮次明确标为 ``interrupted``，不伪装成正常结束。
        """
        if self.status == "closed":
            return
        self.status = "closed"
        task = self._current_task
        if task is not None and not task.done():
            self.cancel_requested = True
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self.settings.dc_close_timeout)
            except TimeoutError, asyncio.CancelledError, Exception:  # noqa: BLE001 - 关闭路径不抛出
                self._mark_interrupted()
        try:
            await asyncio.to_thread(self.device.close)
        except Exception:  # noqa: BLE001 - 设备已断开时忽略
            pass
        # 保留磁盘快照：关闭后仍可从历史会话列表选择并恢复
        self.save_state()
        self._emit(DcEventType.SESSION_CLOSED, "DC 会话已关闭", {"session_id": self.session_id})

    def _mark_interrupted(self) -> None:
        """把仍在运行的轮次标为 interrupted 并记录连续性摘要。"""
        turn = next(
            (item for item in reversed(self.turns) if item.status == DcTurnStatus.RUNNING),
            None,
        )
        if turn is None:
            return
        turn.error = "会话关闭时轮次未在等待时间内收尾"
        self._finalize_turn(turn, DcTurnStatus.INTERRUPTED)
        self._emit(
            DcEventType.TURN_INTERRUPTED,
            "轮次被中断（会话关闭）",
            {"turn_id": turn.turn_id, "reason": "interrupted"},
        )


# ---------------------------------------------------------------------------
# 会话管理器
# ---------------------------------------------------------------------------


class DcSessionManager:
    """管理 DC 会话的内存字典、磁盘快照、设备轮次互斥与 LRU 淘汰。

    与 ``RunManager`` 完全隔离：不共享 tasks/orchestrators/repository。

    设备互斥从「创建期独占」放宽为「轮次期互斥」：同一设备可以存在多个会话
    （否则下拉列表永远只有 1 条、关闭即彻底消失），但同一时刻只允许一个会话
    真正驱动设备，冲突方会收到明确的 BLOCKED 轮次而不是排队。
    """

    def __init__(self, settings: Settings, artifacts: ArtifactStore):
        self.settings = settings
        self.artifacts = artifacts
        self.sessions: dict[str, DcSession] = {}
        self.store = DcSessionStore(artifacts.runtime_dir, DcToolTier(settings.dc_default_tier))
        # Profile 资产访问：断言工具读取稳定定位器；DC 蒸馏写入 Profile（2026-09-17 重构）。
        self.profile_registry = ProfileRegistry(
            settings.resolved_profiles_dir,
            promotion_replay_attempts=settings.hypium_replay_attempts,
            min_evidence_rounds=settings.profile_verification_rounds,
        )
        self._device_locks: dict[str, asyncio.Lock] = {}
        self._reaper_task: asyncio.Task | None = None

    def device_turn_lock(self, device_id: str) -> asyncio.Lock:
        """返回设备级轮次互斥锁（同设备的所有会话共享）。"""
        lock = self._device_locks.get(device_id)
        if lock is None:
            lock = asyncio.Lock()
            self._device_locks[device_id] = lock
        return lock

    def create(self, device_id: str | None = None, tier: DcToolTier | None = None) -> DcSession:
        """创建新会话。

        同一设备可以创建多个会话（仅轮次期互斥），因此不再因设备占用而拒绝。
        """
        resolved_device = device_id or self.settings.harmony_device
        resolved_tier = tier or DcToolTier(self.settings.dc_default_tier)

        # LRU 淘汰
        if len(self.sessions) >= self.settings.dc_max_sessions:
            self._evict_oldest()

        session_id = f"dc-{utc_now():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        provider = create_dc_provider(self.settings)
        session = self._build_session(
            session_id=session_id,
            device_id=resolved_device,
            tier=resolved_tier,
            provider=provider,
        )
        self.sessions[session_id] = session
        return session

    def _build_session(
        self,
        *,
        session_id: str,
        device_id: str,
        tier: DcToolTier,
        provider: DcChatProvider | MockDcChatProvider | None = None,
    ) -> DcSession:
        """构造会话并注入共享 store 与设备轮次锁。"""
        session = DcSession(
            session_id=session_id,
            device_id=device_id,
            tier=tier,
            settings=self.settings,
            artifacts=self.artifacts,
            provider=provider or create_dc_provider(self.settings),
            store=self.store,
            profile_registry=self.profile_registry,
        )
        session.device_lock = self.device_turn_lock(device_id)
        return session

    async def resume(self, session_id: str) -> DcSession:
        """从磁盘快照恢复历史会话（服务重启、空闲淘汰或已关闭的会话）。

        Raises:
            DcSessionNotFound: 内存与磁盘都没有该会话。
        """
        existing = self.sessions.get(session_id)
        if existing is not None:
            return existing
        snapshot = self.store.load(session_id)
        if snapshot is None:
            raise DcSessionNotFound(f"session {session_id} not found")
        if len(self.sessions) >= self.settings.dc_max_sessions:
            self._evict_oldest()
        session = self._build_session(
            session_id=snapshot.session_id,
            device_id=snapshot.device_id or self.settings.harmony_device,
            tier=snapshot.tier,
        )
        session.apply_snapshot(snapshot)
        await session.start()
        self.sessions[session_id] = session
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

    def list_sessions(self) -> list[DcSessionSummary]:
        """列出活跃会话 + 磁盘历史会话（活跃优先，其次按最近活跃时间倒序）。"""
        summaries: dict[str, DcSessionSummary] = {}
        for snapshot in self.store.list_summaries():
            if snapshot.session_id in self.sessions:
                continue
            summaries[snapshot.session_id] = snapshot.summary(active=False)
        for session in self.sessions.values():
            summaries[session.session_id] = DcSessionSummary(
                session_id=session.session_id,
                device_id=session.device_id,
                tier=session.tier.value,
                status=session.status,
                created_at=session.created_at,
                last_active_at=session.last_active_at,
                turn_count=len(session.turns),
                invocation_count=len(session.recorder.invocations),
                active=True,
                script_available=session.script is not None,
            )
        return sorted(summaries.values(), key=lambda item: (not item.active, -item.last_active_at.timestamp()))

    async def close(self, session_id: str) -> bool:
        """关闭并移除内存会话（磁盘快照保留，仍可在历史列表中恢复）。"""
        session = self.sessions.pop(session_id, None)
        if session is None:
            return False
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
        """每 60s 扫描一次，淘汰 idle_ttl 到期的会话（快照已在磁盘，可恢复）。"""
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
        """淘汰最旧的 idle 会话以腾出内存空间（磁盘快照保留）。"""
        if not self.sessions:
            return
        oldest_id = min(
            self.sessions,
            key=lambda sid: self.sessions[sid].last_active_at,
        )
        oldest = self.sessions.pop(oldest_id, None)
        if oldest:
            # 异步关闭（fire-and-forget，reaper 会兜底）
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(oldest.close())
            except RuntimeError:
                pass
