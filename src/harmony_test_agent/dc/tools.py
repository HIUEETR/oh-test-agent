"""DC 模式工具注册与执行。

23 个工具通过 pydantic-ai ``Tool`` 暴露给 Agent，完全绕开
``runtime/tools.py::ToolExecutor``，确保 DC 与 Live Mode 不共享执行路径。

所有工具调用经 ``DcActionRecorder.run`` 单一 chokepoint 完成：
录制 → 计时 → 事件发射 → 错误处理 → 返回 LLM 可读字符串。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext, Tool

from ..devices.base import DeviceError
from ..devices.harmony import HarmonyDeviceAdapter
from ..models import CommandResult, ScreenSnapshot, StableLocator, ToolName, utc_now
from ..perception.normalizer import normalize_layout, page_path
from ..runtime.tools import evaluate_assertion
from ..storage.artifacts import ArtifactStore
from ..targets.catalog import parse_bundle_list
from .hdc import DcHdcExecutor
from .models import (
    SIDE_EFFECT_TOOLS,
    TOOL_TIER,
    DcEffectStatus,
    DcEventType,
    DcToolInvocation,
    DcToolName,
    DcToolStatus,
    DcToolTier,
    tools_up_to,
)
from .safety import DcShellPolicy

# 终态中文标签（事件 message 用）
_STATUS_LABEL: dict[DcToolStatus, str] = {
    DcToolStatus.SUCCEEDED: "成功",
    DcToolStatus.FAILED: "失败",
    DcToolStatus.TIMED_OUT: "超时",
    DcToolStatus.CANCELLED: "已取消",
    DcToolStatus.UNKNOWN: "状态未知",
    DcToolStatus.RUNNING: "执行中",
}

# ---------------------------------------------------------------------------
# 快照持有器
# ---------------------------------------------------------------------------


@dataclass
class DcSnapshotHolder:
    """可变单元，持有最新 ScreenSnapshot 和 JPEG 字节缓存。

    SHA256 去重：连续相同截图不重复传输给 LLM。
    ``history`` 保留会话内采集过的帧（有界），供 DC 蒸馏重建可回放核心流。
    """

    latest: ScreenSnapshot | None = None
    latest_jpeg: bytes | None = None
    latest_sha256: str | None = None
    latest_path: Path | None = None
    history: list[ScreenSnapshot] = field(default_factory=list[ScreenSnapshot])
    max_history: int = 60

    def update_jpeg(self, path: Path, jpeg_bytes: bytes, width: int, height: int) -> bool:
        """更新 JPEG 缓存；返回 True 表示内容变化（SHA 不同）。"""
        sha = DcHdcExecutor.sha256_bytes(jpeg_bytes)
        changed = sha != self.latest_sha256
        self.latest_jpeg = jpeg_bytes
        self.latest_sha256 = sha
        self.latest_path = path
        return changed

    def record(self, snapshot: ScreenSnapshot) -> None:
        """记录一帧到有界历史并更新 ``latest``。"""
        self.latest = snapshot
        self.history.append(snapshot)
        if len(self.history) > self.max_history:
            del self.history[: len(self.history) - self.max_history]


# ---------------------------------------------------------------------------
# 工具上下文
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DcToolContext:
    """通过 ``RunContext[DcToolContext]`` 注入到每个工具。"""

    session_id: str
    device: HarmonyDeviceAdapter
    hdc: DcHdcExecutor
    safety: DcShellPolicy
    recorder: DcActionRecorder
    artifacts: ArtifactStore
    session_dir: Path
    snapshot_holder: DcSnapshotHolder
    tier: DcToolTier
    turn_id: str = ""
    ui_tree_top_k: int = 60
    action_timeout: float = 30.0
    progress_interval: float = 1.0
    stable_locators: list[StableLocator] = field(default_factory=list[StableLocator])
    """当前会话关联 Profile 的稳定定位器证据；无 Profile 时为空列表（断言退化为模糊文本匹配）。"""


def relative_artifact_path(abs_path: Path | None, base_dir: Path | None) -> str | None:
    """把绝对产物路径转为会话目录相对 POSIX 路径（供前端 artifact URL 使用）。

    返回 ``None`` 表示路径缺失或不在会话目录内（例如设备侧路径），
    前端据此保持上一帧截图而不是拼出非法 URL。
    """
    if abs_path is None or base_dir is None:
        return None
    try:
        return abs_path.resolve().relative_to(base_dir.resolve()).as_posix()
    except ValueError, OSError:
        return None


# ---------------------------------------------------------------------------
# 动作录制器（单一 chokepoint）
# ---------------------------------------------------------------------------


class DcActionRecorder:
    """所有工具调用的单一 chokepoint：录制 + 计时 + 进度 + 事件发射 + 错误处理。

    Phase 1 的关键变化：``run()`` **先**把一条 ``running`` 记录加入账本并发
    ``tool_call_started``，**然后**才执行设备调用；完成后就地更新同一条记录。
    因此 ``DcSession.to_view()`` 在工具执行期间就能看到该调用，前端不再出现
    「工具在跑但日志显示 0 步」的时间窗口。
    """

    def __init__(self, progress_interval: float = 1.0) -> None:
        self.invocations: list[DcToolInvocation] = []
        self.progress_interval = progress_interval
        self._emit: Callable[[DcEventType, str, dict[str, Any]], None] | None = None
        self._active: DcToolInvocation | None = None

    def set_emitter(self, emit: Callable[[DcEventType, str, dict[str, Any]], None]) -> None:
        """绑定事件发射回调（由 DcSession 在创建时注入）。"""
        self._emit = emit

    def _emit_event(self, event_type: DcEventType, message: str, payload: dict[str, Any] | None = None) -> None:
        if self._emit:
            self._emit(event_type, message, payload or {})

    # ------------------------------------------------------------------
    # 账本查询
    # ------------------------------------------------------------------

    def by_id(self, invocation_id: str) -> DcToolInvocation | None:
        """按 invocation_id 查找账本记录。"""
        for invocation in self.invocations:
            if invocation.invocation_id == invocation_id:
                return invocation
        return None

    def running(self) -> list[DcToolInvocation]:
        """返回仍在执行中的调用。"""
        return [inv for inv in self.invocations if inv.status == DcToolStatus.RUNNING]

    def turn_invocations(self, turn_id: str) -> list[DcToolInvocation]:
        """返回某一轮次的全部调用（含运行中）。"""
        return [inv for inv in self.invocations if inv.turn_id == turn_id]

    def note_phase(self, phase: str) -> None:
        """记录当前调用的阶段变化（截图/UI 层级等内部分步）。"""
        active = self._active
        if active is None or active.status != DcToolStatus.RUNNING:
            return
        active.phase = phase
        active.last_progress_at = utc_now()
        self._emit_event(
            DcEventType.TOOL_CALL_PROGRESS,
            f"工具 {active.tool.value} 阶段：{phase}",
            self._progress_payload(active),
        )

    def _progress_payload(self, invocation: DcToolInvocation) -> dict[str, Any]:
        """构造 tool_call_progress 载荷（已耗时/剩余时间/阶段/可取消性）。"""
        now = utc_now()
        elapsed_ms = round((now - invocation.started_at).total_seconds() * 1000)
        remaining_ms: int | None = None
        if invocation.deadline_at is not None:
            remaining_ms = round((invocation.deadline_at - now).total_seconds() * 1000)
        return {
            "invocation_id": invocation.invocation_id,
            "turn_id": invocation.turn_id,
            "tool": invocation.tool.value,
            "status": invocation.status.value,
            "phase": invocation.phase,
            "elapsed_ms": elapsed_ms,
            "remaining_ms": remaining_ms,
            "last_progress_at": now.isoformat(),
            "cancellable": invocation.cancellable,
        }

    async def _heartbeat(self, invocation: DcToolInvocation) -> None:
        """工具执行期间按固定间隔发出进度事件，避免长时间静默。"""
        interval = max(0.2, self.progress_interval)
        try:
            while invocation.status == DcToolStatus.RUNNING:
                await asyncio.sleep(interval)
                if invocation.status != DcToolStatus.RUNNING:
                    return
                invocation.last_progress_at = utc_now()
                self._emit_event(
                    DcEventType.TOOL_CALL_PROGRESS,
                    f"工具 {invocation.tool.value} 仍在执行（{invocation.phase or 'running'}）",
                    self._progress_payload(invocation),
                )
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    async def run(
        self,
        ctx: RunContext[DcToolContext],
        tool: DcToolName,
        args: dict[str, Any],
        device_fn: Callable[[], CommandResult | Any],
        *,
        phase: str = "",
        cancellable: bool = True,
    ) -> str:
        """执行一次工具调用并完整录制。

        1. 创建 running 记录并入账本（``to_view`` 立即可见）
        2. emit TOOL_CALL_STARTED
        3. 执行设备调用（带超时与心跳）
        4. 就地更新同一记录（不新增第二条）
        5. emit TOOL_CALL_FINISHED
        6. 返回 LLM 可读短字符串
        """
        deps = ctx.deps
        invocation_id = f"inv-{uuid.uuid4().hex[:10]}"
        started_at = utc_now()
        started_mono = time.monotonic()
        timeout = float(deps.action_timeout)
        has_side_effect = tool in SIDE_EFFECT_TOOLS
        # 页面归属：取调用前已知的最新帧 page_path。DC 蒸馏 Profile 用它做页面覆盖校验，
        # 因此必须在调用前补录（调用后的帧可能已切页）。
        latest = getattr(deps, "snapshot_holder", None)
        latest_snapshot = getattr(latest, "latest", None)
        invocation = DcToolInvocation(
            invocation_id=invocation_id,
            turn_id=deps.turn_id,
            tool=tool,
            tier=TOOL_TIER.get(tool, DcToolTier.L1),
            args=args,
            success=False,
            status=DcToolStatus.RUNNING,
            started_at=started_at,
            ended_at=None,
            duration_ms=0,
            last_progress_at=started_at,
            deadline_at=started_at + timedelta(seconds=timeout),
            effect_status=DcEffectStatus.UNKNOWN if has_side_effect else DcEffectStatus.NONE,
            command_id=f"cmd-{invocation_id}",
            phase=phase or tool.value,
            cancellable=cancellable,
            page_path=(latest_snapshot.page_path if latest_snapshot is not None else "") or "",
        )
        self.invocations.append(invocation)
        self._active = invocation

        self._emit_event(
            DcEventType.TOOL_CALL_STARTED,
            f"调用工具 {tool.value}",
            {
                "invocation_id": invocation_id,
                "turn_id": deps.turn_id,
                "tool": tool.value,
                "args": args,
                "status": DcToolStatus.RUNNING.value,
                "phase": invocation.phase,
                "started_at": started_at.isoformat(),
                "deadline_at": invocation.deadline_at.isoformat() if invocation.deadline_at else None,
                "command_id": invocation.command_id,
                "cancellable": cancellable,
            },
        )

        heartbeat = asyncio.create_task(self._heartbeat(invocation), name=f"dc-tool-progress-{invocation_id}")
        status = DcToolStatus.UNKNOWN
        error: str | None = None
        error_code: str | None = None
        command: CommandResult | None = None
        result_text = ""

        try:
            raw = await asyncio.wait_for(asyncio.to_thread(device_fn), timeout=timeout)
            if isinstance(raw, CommandResult):
                command = raw
                if raw.timed_out:
                    status = DcToolStatus.TIMED_OUT
                    error_code = "tool_timeout"
                    error = f"tool {tool.value} timed out after {timeout}s"
                elif raw.ok:
                    status = DcToolStatus.SUCCEEDED
                else:
                    status = DcToolStatus.FAILED
                    error_code = "device_error"
                    error = raw.stderr or raw.stdout or f"returncode={raw.returncode}"
                result_text = self._summarize_command(tool, raw)
            else:
                status = DcToolStatus.SUCCEEDED
                result_text = str(raw) if raw is not None else "ok"
        except TimeoutError:
            status = DcToolStatus.TIMED_OUT
            error_code = "tool_timeout"
            error = f"tool {tool.value} timed out after {timeout}s"
            result_text = error
        except asyncio.CancelledError:
            status = DcToolStatus.CANCELLED
            error_code = "cancelled"
            error = f"tool {tool.value} cancelled"
            result_text = error
            self._finalize(
                invocation,
                status=status,
                started_mono=started_mono,
                command=command,
                error=error,
                error_code=error_code,
                result_text=result_text,
            )
            heartbeat.cancel()
            self._active = None
            raise
        except Exception as exc:
            status = DcToolStatus.FAILED
            error_code = "tool_error"
            error = f"{type(exc).__name__}: {exc}"
            result_text = error

        heartbeat.cancel()
        self._finalize(
            invocation,
            status=status,
            started_mono=started_mono,
            command=command,
            error=error,
            error_code=error_code,
            result_text=result_text,
        )
        self._active = None
        return result_text

    def _finalize(
        self,
        invocation: DcToolInvocation,
        *,
        status: DcToolStatus,
        started_mono: float,
        command: CommandResult | None,
        error: str | None,
        error_code: str | None,
        result_text: str,
    ) -> None:
        """就地写入终态并发射 tool_call_finished（同一调用只能有一个终态）。"""
        if invocation.status != DcToolStatus.RUNNING:
            return
        ended_at = utc_now()
        invocation.status = status
        invocation.success = status == DcToolStatus.SUCCEEDED
        invocation.ended_at = ended_at
        invocation.duration_ms = round((time.monotonic() - started_mono) * 1000)
        invocation.last_progress_at = ended_at
        invocation.phase = "finished"
        invocation.command = command
        invocation.error = error
        invocation.error_code = error_code
        invocation.result_summary = result_text[:500]
        if invocation.tool in SIDE_EFFECT_TOOLS:
            invocation.effect_status = (
                DcEffectStatus.CONFIRMED if status == DcToolStatus.SUCCEEDED else DcEffectStatus.UNKNOWN
            )
        self._emit_event(
            DcEventType.TOOL_CALL_FINISHED,
            f"工具 {invocation.tool.value} {_STATUS_LABEL[status]}",
            {
                "invocation_id": invocation.invocation_id,
                "turn_id": invocation.turn_id,
                "tool": invocation.tool.value,
                "args": invocation.args,
                "success": invocation.success,
                "status": status.value,
                "duration_ms": invocation.duration_ms,
                "result_summary": invocation.result_summary,
                "error": error,
                "error_code": error_code,
                "effect_status": invocation.effect_status.value,
                "ended_at": ended_at.isoformat(),
            },
        )

    @staticmethod
    def _summarize_command(tool: DcToolName, result: CommandResult) -> str:
        """把 CommandResult 压缩为 LLM 可读的短字符串。"""
        if not result.ok:
            return f"FAILED: {result.stderr or result.stdout or result.returncode}"
        stdout = result.stdout.strip()
        if tool == DcToolName.LIST_APPS:
            lines = stdout.splitlines()
            return f"ok: {len(lines)} apps installed"
        if tool == DcToolName.FOREGROUND_APP:
            return f"ok: foreground={stdout[:200]}" if stdout else "ok: no foreground info"
        if tool == DcToolName.FILE_LIST:
            lines = stdout.splitlines()
            return f"ok: {len(lines)} entries"
        if len(stdout) > 300:
            return f"ok: {stdout[:300]}..."
        return f"ok: {stdout}" if stdout else "ok"


# ---------------------------------------------------------------------------
# L1 工具 — UI 交互 (8)
# ---------------------------------------------------------------------------


async def tool_click(ctx: RunContext[DcToolContext], x: int, y: int) -> str:
    """Click absolute screen coordinates (x, y) in pixels."""
    return await ctx.deps.recorder.run(ctx, DcToolName.CLICK, {"x": x, "y": y}, lambda: ctx.deps.device.click(x, y))


async def tool_swipe(
    ctx: RunContext[DcToolContext],
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    duration: float = 0.5,
) -> str:
    """Swipe from (start_x, start_y) to (end_x, end_y) over duration seconds."""
    args = {"start": [start_x, start_y], "end": [end_x, end_y], "duration": duration}
    return await ctx.deps.recorder.run(
        ctx, DcToolName.SWIPE, args, lambda: ctx.deps.device.swipe((start_x, start_y), (end_x, end_y), duration)
    )


async def tool_input_text(ctx: RunContext[DcToolContext], text: str, x: int | None = None, y: int | None = None) -> str:
    """Input text into the focused field; optionally tap (x, y) first to focus."""
    args: dict[str, Any] = {"text": text}
    if x is not None and y is not None:
        args["coordinate"] = [x, y]
    return await ctx.deps.recorder.run(ctx, DcToolName.INPUT_TEXT, args, lambda: ctx.deps.device.input_text(text, x, y))


async def tool_key_event(ctx: RunContext[DcToolContext], key: str) -> str:
    """Send a system key event. Common keys: Home, Back, Power, VolumeUp, VolumeDown, Enter."""
    return await ctx.deps.recorder.run(ctx, DcToolName.KEY_EVENT, {"key": key}, lambda: ctx.deps.hdc.key_event(key))


async def tool_back(ctx: RunContext[DcToolContext]) -> str:
    """Press the system Back key."""
    return await ctx.deps.recorder.run(ctx, DcToolName.BACK, {}, lambda: ctx.deps.device.back())


async def tool_wait(ctx: RunContext[DcToolContext], seconds: float) -> str:
    """Wait for the given number of seconds (max 30)."""
    clamped = max(0, min(seconds, 30))
    return await ctx.deps.recorder.run(
        ctx,
        DcToolName.WAIT,
        {"seconds": clamped},
        lambda: ctx.deps.device.wait(clamped),
    )


async def tool_screenshot(ctx: RunContext[DcToolContext]) -> str:
    """Capture the current device screen. Returns a summary of visible UI elements."""
    deps = ctx.deps

    def _capture() -> str:
        screens_dir = deps.session_dir / "screens"
        # JPEG 快速路径（用于 LLM 上传）；分步上报阶段，定位慢在 snapshot/recv/decode
        jpeg_path, jpeg_bytes, width, height = deps.hdc.screenshot_jpeg(
            screens_dir,
            f"dc_{int(time.time())}",
            on_phase=deps.recorder.note_phase,
        )
        changed = deps.snapshot_holder.update_jpeg(jpeg_path, jpeg_bytes, width, height)
        # PNG 存档（用于产物）
        deps.recorder.note_phase("archive_png")
        snapshot = deps.device.screenshot(screens_dir, deps.session_id, f"dc_{int(time.time())}")
        deps.snapshot_holder.record(snapshot)
        # 发射截图事件（snapshot_path 为会话相对 POSIX 路径，供前端拼 artifact URL）
        deps.recorder._emit_event(
            DcEventType.SCREENSHOT_CAPTURED,
            "已采集设备截图",
            {
                "snapshot_path": relative_artifact_path(jpeg_path, deps.session_dir),
                "snapshot_id": snapshot.snapshot_id,
                "width": width,
                "height": height,
                "sha256": deps.snapshot_holder.latest_sha256,
                "changed": changed,
                "element_count": len(snapshot.elements),
                "page_path": snapshot.page_path,
                "source": "tool",
            },
        )
        # 返回 top-K 元素摘要
        top_k = deps.ui_tree_top_k
        elements = snapshot.elements[:top_k]
        summary_lines = [f"Screenshot {width}x{height}, page={snapshot.page_path}, {len(snapshot.elements)} elements"]
        for el in elements:
            bbox = f"[{el.bbox.left},{el.bbox.top},{el.bbox.right},{el.bbox.bottom}]" if el.bbox else "no-bbox"
            flags = "".join(
                [
                    "C" if el.clickable else "",
                    "E" if el.editable else "",
                    "S" if el.scrollable else "",
                ]
            )
            summary_lines.append(f"  {el.element_id}: {el.type} {bbox} {flags} key={el.key!r} text={el.content!r}")
        return "\n".join(summary_lines)

    return await deps.recorder.run(ctx, DcToolName.SCREENSHOT, {}, _capture)


async def tool_inspect_screen(ctx: RunContext[DcToolContext]) -> str:
    """Inspect the current screen: capture screenshot + UI hierarchy, return element summary."""
    return await tool_screenshot(ctx)


# ---------------------------------------------------------------------------
# L2 工具 — 观测诊断 (6)
# ---------------------------------------------------------------------------


async def tool_dump_ui_hierarchy(ctx: RunContext[DcToolContext]) -> str:
    """Dump the current UI hierarchy and return top-K interactive elements."""
    deps = ctx.deps

    def _dump() -> str:
        deps.recorder.note_phase("ui_hierarchy")
        hierarchy = deps.device.collect_ui_hierarchy()
        # 落盘完整 JSON
        deps.recorder.note_phase("persist_layout")
        layout_dir = deps.artifacts.run_dir(deps.session_id) / "layouts"
        layout_dir.mkdir(parents=True, exist_ok=True)
        layout_path = layout_dir / f"dc_{int(time.time())}.json"
        layout_path.write_text(json.dumps(hierarchy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # 标准化 + top-K
        elements = normalize_layout(hierarchy, 1080, 2232)  # 默认分辨率，实际由截图校正
        top_k = deps.ui_tree_top_k
        pp = page_path(hierarchy)
        deps.recorder._emit_event(
            DcEventType.UI_TREE_CAPTURED,
            "已采集 UI 层级",
            {"page_path": pp, "element_count": len(elements), "layout_path": str(layout_path)},
        )
        lines = [f"UI hierarchy: page={pp}, {len(elements)} elements (showing top {min(top_k, len(elements))})"]
        for el in elements[:top_k]:
            bbox = f"[{el.bbox.left},{el.bbox.top},{el.bbox.right},{el.bbox.bottom}]" if el.bbox else "no-bbox"
            flags = "".join(["C" if el.clickable else "", "E" if el.editable else "", "S" if el.scrollable else ""])
            lines.append(f"  {el.element_id}: {el.type} {bbox} {flags} key={el.key!r} text={el.content!r}")
        return "\n".join(lines)

    return await deps.recorder.run(ctx, DcToolName.DUMP_UI_HIERARCHY, {}, _dump)


async def tool_collect_logs(ctx: RunContext[DcToolContext]) -> str:
    """Collect device logs (hilog) and save to artifacts. Returns a short summary."""
    deps = ctx.deps

    def _collect() -> str:
        log_path = deps.artifacts.run_dir(deps.session_id) / "commands" / f"dc_logs_{int(time.time())}.txt"
        result = deps.device.collect_logs(log_path)
        line_count = len(result.stdout.splitlines()) if result.stdout else 0
        return f"ok: {line_count} log lines saved to {log_path.name}"

    return await deps.recorder.run(ctx, DcToolName.COLLECT_LOGS, {}, _collect)


async def tool_foreground_app(ctx: RunContext[DcToolContext]) -> str:
    """Return the current foreground application bundle name and ability."""
    deps = ctx.deps

    def _fg() -> str:
        fg = deps.device.current_foreground_app()
        if fg is None:
            return "no foreground app detected"
        return f"bundle={fg.bundle_name}, ability={fg.ability_name or 'unknown'}"

    return await deps.recorder.run(ctx, DcToolName.FOREGROUND_APP, {}, _fg)


# 单次 list_apps 返回的 bundle 上限（防止超长输出吃满 prompt token）
_MAX_LISTED_APPS = 80


async def tool_list_apps(ctx: RunContext[DcToolContext], query: str | None = None) -> str:
    """List installed application bundle names, optionally filtered by a substring.

    Only one ``bm dump -a`` call is made: enumerating metadata for every installed
    bundle (``list_installed_apps``) costs one ``bm dump -n`` per bundle and blows
    through the tool timeout on real devices.
    """
    deps = ctx.deps
    args: dict[str, Any] = {"query": query} if query else {}

    def _list() -> str:
        result = deps.hdc.list_bundle_names()
        if not result.ok:
            raise DeviceError(f"bm dump -a failed: {result.stderr or result.stdout or result.returncode}")
        bundles = parse_bundle_list(result.stdout)
        if query:
            needle = query.strip().casefold()
            bundles = [bundle for bundle in bundles if needle in bundle.casefold()]
        if not bundles:
            return f"0 apps installed matching {query!r}" if query else "0 apps installed"
        shown = bundles[:_MAX_LISTED_APPS]
        header = f"{len(bundles)} apps installed"
        if query:
            header += f" matching {query!r}"
        if len(shown) < len(bundles):
            header += f" (showing first {len(shown)})"
        return header + ":\n" + "\n".join(shown)

    return await deps.recorder.run(ctx, DcToolName.LIST_APPS, args, _list)


async def tool_inspect_app(ctx: RunContext[DcToolContext], bundle_name: str) -> str:
    """Inspect a specific installed application's metadata."""
    deps = ctx.deps

    def _inspect() -> str:
        app = deps.device.inspect_app(bundle_name)
        return f"bundle={app.bundle_name}, name={app.display_name}, version={app.version_name}"

    return await deps.recorder.run(ctx, DcToolName.INSPECT_APP, {"bundle_name": bundle_name}, _inspect)


async def tool_memory_dump(ctx: RunContext[DcToolContext], bundle_name: str) -> str:
    """Dump memory usage for a specific application."""
    deps = ctx.deps

    def _dump() -> str:
        output_path = deps.artifacts.run_dir(deps.session_id) / "commands" / f"dc_mem_{int(time.time())}.txt"
        result = deps.hdc.memory_dump(bundle_name, output_path)
        return f"ok: memory dump saved to {output_path.name}" if result.ok else f"FAILED: {result.stderr}"

    return await deps.recorder.run(ctx, DcToolName.MEMORY_DUMP, {"bundle_name": bundle_name}, _dump)


# ---------------------------------------------------------------------------
# L3 工具 — 应用管理 (5)
# ---------------------------------------------------------------------------


async def tool_start_app(
    ctx: RunContext[DcToolContext], bundle_name: str, ability_name: str, module_name: str | None = None
) -> str:
    """Start an application's ability."""
    deps = ctx.deps
    args: dict[str, Any] = {"bundle_name": bundle_name, "ability_name": ability_name}
    if module_name:
        args["module_name"] = module_name
    return await deps.recorder.run(
        ctx, DcToolName.START_APP, args, lambda: deps.device.start_app(bundle_name, ability_name, module_name)
    )


async def tool_force_stop_app(ctx: RunContext[DcToolContext], bundle_name: str) -> str:
    """Force stop an application."""
    return await ctx.deps.recorder.run(
        ctx, DcToolName.FORCE_STOP_APP, {"bundle_name": bundle_name}, lambda: ctx.deps.device.stop_app(bundle_name)
    )


async def tool_install_app(ctx: RunContext[DcToolContext], hap_path: str) -> str:
    """Install a HAP/APP package to the device."""
    deps = ctx.deps
    deps.safety.validate_install(hap_path)
    return await deps.recorder.run(
        ctx, DcToolName.INSTALL_APP, {"hap_path": hap_path}, lambda: deps.hdc.install_app(Path(hap_path))
    )


async def tool_uninstall_app(ctx: RunContext[DcToolContext], bundle_name: str) -> str:
    """Uninstall an application from the device."""
    deps = ctx.deps
    deps.safety.validate_uninstall(bundle_name)
    return await deps.recorder.run(
        ctx, DcToolName.UNINSTALL_APP, {"bundle_name": bundle_name}, lambda: deps.hdc.uninstall_app(bundle_name)
    )


async def tool_clear_app_data(ctx: RunContext[DcToolContext], bundle_name: str) -> str:
    """Clear an application's data."""
    return await ctx.deps.recorder.run(
        ctx, DcToolName.CLEAR_APP_DATA, {"bundle_name": bundle_name}, lambda: ctx.deps.hdc.clear_app_data(bundle_name)
    )


# ---------------------------------------------------------------------------
# L4 工具 — 文件操作 (3)
# ---------------------------------------------------------------------------


async def tool_file_send(ctx: RunContext[DcToolContext], local_path: str, remote_path: str) -> str:
    """Push a local file to the device."""
    deps = ctx.deps
    deps.safety.validate_file_path(remote_path, "send")
    return await deps.recorder.run(
        ctx,
        DcToolName.FILE_SEND,
        {"local": local_path, "remote": remote_path},
        lambda: deps.hdc.file_send(Path(local_path), remote_path),
    )


async def tool_file_recv(ctx: RunContext[DcToolContext], remote_path: str, local_path: str) -> str:
    """Pull a file from the device to local."""
    deps = ctx.deps
    deps.safety.validate_file_path(remote_path, "recv")
    return await deps.recorder.run(
        ctx,
        DcToolName.FILE_RECV,
        {"remote": remote_path, "local": local_path},
        lambda: deps.hdc.file_recv(remote_path, Path(local_path)),
    )


async def tool_file_list(ctx: RunContext[DcToolContext], remote_dir: str) -> str:
    """List files in a device directory."""
    deps = ctx.deps
    deps.safety.validate_file_path(remote_dir, "list")
    return await deps.recorder.run(
        ctx, DcToolName.FILE_LIST, {"remote_dir": remote_dir}, lambda: deps.hdc.file_list(remote_dir)
    )


# ---------------------------------------------------------------------------
# L1 工具 — 断言（3，2026-09-17 新增）
# ---------------------------------------------------------------------------


async def _run_assertion(
    ctx: RunContext[DcToolContext],
    tool: DcToolName,
    kind: ToolName,
    target: str,
) -> str:
    """断言工具公共路径：必要时自动采集帧 → 在 recorder chokepoint 内评估断言。

    断言必须作为一条 ``DcToolInvocation`` 落账（``tool=assert_*``）：DC 脚本生成器
    据此统计 ``explicit_assertions`` 并决定 ``replay_eligible``，Profile 蒸馏也据此
    提取应用级断言证据。断言失败时设备调用函数抛错，账本记为 failed、``success=False``，
    工具向模型返回错误摘要而不是静默通过。
    """
    deps = ctx.deps
    if deps.snapshot_holder.latest is None:
        # 无帧可判：先经 recorder 采集一帧（自动获得事件/账本/超时语义）。
        await tool_screenshot(ctx)

    def _assert() -> str:
        result, _ = evaluate_assertion(deps.snapshot_holder.latest, kind, target, deps.stable_locators)
        if not result.passed:
            raise DeviceError(f"{tool.value} failed: {result.message}")
        return result.message

    return await deps.recorder.run(ctx, tool, {"target": target}, _assert)


async def tool_assert_visible(ctx: RunContext[DcToolContext], target: str) -> str:
    """Assert that a UI element matching target is visible on the current screen.

    Uses the same fuzzy target variants and page-summary fallback as Live Mode.
    """
    return await _run_assertion(ctx, DcToolName.ASSERT_VISIBLE, ToolName.ASSERT_VISIBLE, target)


async def tool_assert_not_visible(ctx: RunContext[DcToolContext], target: str) -> str:
    """Assert that no UI element matching target is visible on the current screen."""
    return await _run_assertion(ctx, DcToolName.ASSERT_NOT_VISIBLE, ToolName.ASSERT_NOT_VISIBLE, target)


async def tool_assert_text(ctx: RunContext[DcToolContext], target: str) -> str:
    """Assert that exact text target is present as a UI element (strict, no summary fallback)."""
    return await _run_assertion(ctx, DcToolName.ASSERT_TEXT, ToolName.ASSERT_TEXT, target)


# ---------------------------------------------------------------------------
# L5 工具 — 受控 Shell (1)
# ---------------------------------------------------------------------------


async def tool_execute_shell(ctx: RunContext[DcToolContext], argv: list[str]) -> str:
    """Execute a controlled shell command on the device.

    argv must be a list of strings (never a single shell string).
    Destructive commands are blocked by DcShellPolicy.
    """
    deps = ctx.deps
    deps.safety.validate_shell(argv)
    return await deps.recorder.run(ctx, DcToolName.EXECUTE_SHELL, {"argv": argv}, lambda: deps.hdc.execute_shell(argv))


# ---------------------------------------------------------------------------
# 工具注册表
# ---------------------------------------------------------------------------

# 工具名 → (函数, 描述) 的映射
_TOOL_REGISTRY: dict[DcToolName, tuple[Callable[..., Any], str]] = {
    DcToolName.CLICK: (tool_click, "Click absolute screen coordinates (x, y) in pixels."),
    DcToolName.SWIPE: (tool_swipe, "Swipe from start to end coordinates over duration seconds."),
    DcToolName.INPUT_TEXT: (tool_input_text, "Input text into focused field; optionally tap (x,y) first."),
    DcToolName.KEY_EVENT: (tool_key_event, "Send a system key event (Home, Back, Power, Enter, etc.)."),
    DcToolName.BACK: (tool_back, "Press the system Back key."),
    DcToolName.WAIT: (tool_wait, "Wait for given seconds (max 30)."),
    DcToolName.SCREENSHOT: (tool_screenshot, "Capture device screen and return UI element summary."),
    DcToolName.INSPECT_SCREEN: (tool_inspect_screen, "Inspect current screen: screenshot + UI hierarchy."),
    DcToolName.ASSERT_VISIBLE: (tool_assert_visible, "Assert UI element matching target is visible."),
    DcToolName.ASSERT_NOT_VISIBLE: (tool_assert_not_visible, "Assert no UI element matching target is visible."),
    DcToolName.ASSERT_TEXT: (tool_assert_text, "Assert exact text target is present as a UI element."),
    DcToolName.DUMP_UI_HIERARCHY: (tool_dump_ui_hierarchy, "Dump UI hierarchy and return top-K interactive elements."),
    DcToolName.COLLECT_LOGS: (tool_collect_logs, "Collect device hilog and save to artifacts."),
    DcToolName.FOREGROUND_APP: (tool_foreground_app, "Return current foreground app bundle and ability."),
    DcToolName.LIST_APPS: (
        tool_list_apps,
        "List installed application bundle names (single bm dump call; optional case-insensitive query).",
    ),
    DcToolName.INSPECT_APP: (tool_inspect_app, "Inspect a specific installed app's metadata."),
    DcToolName.MEMORY_DUMP: (tool_memory_dump, "Dump memory usage for a specific application."),
    DcToolName.START_APP: (tool_start_app, "Start an application's ability."),
    DcToolName.FORCE_STOP_APP: (tool_force_stop_app, "Force stop an application."),
    DcToolName.INSTALL_APP: (tool_install_app, "Install a HAP/APP package to device."),
    DcToolName.UNINSTALL_APP: (tool_uninstall_app, "Uninstall an application from device."),
    DcToolName.CLEAR_APP_DATA: (tool_clear_app_data, "Clear an application's data."),
    DcToolName.FILE_SEND: (tool_file_send, "Push a local file to device."),
    DcToolName.FILE_RECV: (tool_file_recv, "Pull a file from device to local."),
    DcToolName.FILE_LIST: (tool_file_list, "List files in a device directory."),
    DcToolName.EXECUTE_SHELL: (
        tool_execute_shell,
        "Execute a controlled shell command (argv list, destructive commands blocked).",
    ),
}


def build_tools(tier: DcToolTier) -> list[Tool]:
    """返回 level ≤ tier 的全部 pydantic-ai Tool 对象。

    Agent 每轮构造时调用；tier 变更下轮生效，无需额外机制。
    """
    allowed_names = tools_up_to(tier)
    tools: list[Tool] = []
    for name in allowed_names:
        entry = _TOOL_REGISTRY.get(name)
        if entry is None:
            continue
        func, description = entry
        tools.append(Tool(func, name=name.value, description=description))
    return tools
