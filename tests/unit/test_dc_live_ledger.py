"""Phase 1：工具调用实时账本（先入账、就地更新、进度心跳、副作用状态）。

回归背景：旧 ``DcActionRecorder.run()`` 在设备调用**完成后**才把
``DcToolInvocation`` 追加进账本，因此工具执行期间 ``DcSession.to_view()``
看不到任何调用，前端操作日志长期显示「0 步」。本文件锁定修复后的契约：
- 工具一开始就有一条 ``running`` 记录（含 invocation_id / deadline / 阶段）；
- 结束时就地更新同一条记录（绝不新增第二条）；
- 执行期间按间隔发 ``tool_call_progress``；
- 超时/取消/失败分别产生正确状态，副作用工具标记 ``unknown``。
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from harmony_test_agent.dc.models import (
    DcEffectStatus,
    DcEventType,
    DcToolName,
    DcToolStatus,
    DcToolTier,
)
from harmony_test_agent.dc.tools import (
    DcActionRecorder,
    DcSnapshotHolder,
    DcToolContext,
)
from harmony_test_agent.models import CommandResult
from harmony_test_agent.storage.artifacts import ArtifactStore


def make_context(
    tmp_path: Path,
    *,
    timeout: float = 5.0,
    progress_interval: float = 0.05,
) -> tuple[Any, DcActionRecorder, list[tuple[str, str, dict[str, Any]]]]:
    events: list[tuple[str, str, dict[str, Any]]] = []
    recorder = DcActionRecorder(progress_interval=progress_interval)
    recorder.set_emitter(lambda etype, msg, payload: events.append((etype.value, msg, payload)))
    deps = DcToolContext(
        session_id="dc-test",
        device=SimpleNamespace(),
        hdc=SimpleNamespace(),
        safety=SimpleNamespace(),
        recorder=recorder,
        artifacts=ArtifactStore(tmp_path / "runs"),
        session_dir=tmp_path / "runs" / "dc-test",
        snapshot_holder=DcSnapshotHolder(),
        tier=DcToolTier.L1,
        turn_id="turn-1",
        action_timeout=timeout,
        progress_interval=progress_interval,
    )
    return SimpleNamespace(deps=deps), recorder, events


def ok_command(stdout: str = "ok") -> CommandResult:
    return CommandResult(command="hdc shell click 1 2", returncode=0, stdout=stdout)


class TestRunningLedger:
    async def test_running_invocation_is_visible_before_tool_finishes(self, tmp_path: Path) -> None:
        ctx, recorder, events = make_context(tmp_path)
        gate = threading.Event()

        def slow_tool() -> CommandResult:
            gate.wait(timeout=5)
            return ok_command()

        task = asyncio.create_task(recorder.run(ctx, DcToolName.CLICK, {"x": 1, "y": 2}, slow_tool))
        await asyncio.sleep(0.15)

        running = recorder.running()
        assert len(running) == 1, "工具执行期间账本必须已有一条 running 记录"
        assert running[0].status == DcToolStatus.RUNNING
        assert running[0].ended_at is None
        assert running[0].is_running is True
        assert running[0].deadline_at is not None
        assert running[0].command_id is not None
        assert events[0][0] == DcEventType.TOOL_CALL_STARTED.value
        assert events[0][2]["status"] == "running"
        assert events[0][2]["invocation_id"] == running[0].invocation_id

        gate.set()
        await task

        assert len(recorder.invocations) == 1, "结束后必须是同一条记录，不能新增第二条"
        finished = recorder.invocations[0]
        assert finished.status == DcToolStatus.SUCCEEDED
        assert finished.success is True
        assert finished.ended_at is not None
        finished_events = [event for event in events if event[0] == DcEventType.TOOL_CALL_FINISHED.value]
        assert len(finished_events) == 1
        assert finished_events[0][2]["invocation_id"] == finished.invocation_id
        assert finished_events[0][2]["status"] == "succeeded"

    async def test_progress_heartbeat_is_emitted_while_running(self, tmp_path: Path) -> None:
        ctx, recorder, events = make_context(tmp_path, progress_interval=0.05)
        gate = threading.Event()

        def slow_tool() -> CommandResult:
            gate.wait(timeout=5)
            return ok_command()

        task = asyncio.create_task(recorder.run(ctx, DcToolName.SCREENSHOT, {}, slow_tool))
        await asyncio.sleep(0.3)

        progress = [event for event in events if event[0] == DcEventType.TOOL_CALL_PROGRESS.value]
        assert progress, "执行期间必须发出进度心跳，不能长时间静默"
        assert progress[-1][2]["status"] == "running"
        assert progress[-1][2]["elapsed_ms"] >= 0
        assert progress[-1][2]["remaining_ms"] is not None
        assert progress[-1][2]["cancellable"] is True

        gate.set()
        await task

    async def test_note_phase_updates_current_invocation(self, tmp_path: Path) -> None:
        ctx, recorder, events = make_context(tmp_path)
        gate = threading.Event()

        def slow_tool() -> CommandResult:
            ctx.deps.recorder.note_phase("snapshot_display")
            gate.wait(timeout=5)
            return ok_command()

        task = asyncio.create_task(recorder.run(ctx, DcToolName.SCREENSHOT, {}, slow_tool))
        await asyncio.sleep(0.15)

        assert recorder.invocations[0].phase == "snapshot_display"
        phases = [event[2]["phase"] for event in events if event[0] == DcEventType.TOOL_CALL_PROGRESS.value]
        assert "snapshot_display" in phases

        gate.set()
        await task


class TestFinalStates:
    async def test_side_effect_tool_success_confirms_effect(self, tmp_path: Path) -> None:
        ctx, recorder, _ = make_context(tmp_path)

        await recorder.run(ctx, DcToolName.CLICK, {"x": 1, "y": 2}, ok_command)

        assert recorder.invocations[0].effect_status == DcEffectStatus.CONFIRMED

    async def test_observation_tool_has_no_effect(self, tmp_path: Path) -> None:
        ctx, recorder, _ = make_context(tmp_path)

        await recorder.run(ctx, DcToolName.SCREENSHOT, {}, lambda: "ok: 12 elements")

        assert recorder.invocations[0].status == DcToolStatus.SUCCEEDED
        assert recorder.invocations[0].effect_status == DcEffectStatus.NONE

    async def test_timeout_marks_timed_out_and_effect_unknown(self, tmp_path: Path) -> None:
        ctx, recorder, events = make_context(tmp_path, timeout=0.2)

        def hanging() -> CommandResult:
            threading.Event().wait(timeout=5)
            return ok_command()

        await recorder.run(ctx, DcToolName.CLICK, {"x": 1, "y": 2}, hanging)

        invocation = recorder.invocations[0]
        assert invocation.status == DcToolStatus.TIMED_OUT
        assert invocation.success is False
        assert invocation.error_code == "tool_timeout"
        assert invocation.effect_status == DcEffectStatus.UNKNOWN, "点击超时后副作用未知，禁止自动重放"
        finished = [event for event in events if event[0] == DcEventType.TOOL_CALL_FINISHED.value][-1]
        assert finished[2]["status"] == "timed_out"
        assert finished[2]["effect_status"] == "unknown"

    async def test_command_failure_is_failed_with_readable_summary(self, tmp_path: Path) -> None:
        ctx, recorder, _ = make_context(tmp_path)
        failure = CommandResult(command="hdc shell click 1 2", returncode=1, stderr="device offline")

        result = await recorder.run(ctx, DcToolName.CLICK, {"x": 1, "y": 2}, lambda: failure)

        invocation = recorder.invocations[0]
        assert invocation.status == DcToolStatus.FAILED
        assert invocation.error == "device offline"
        assert result.startswith("FAILED")

    async def test_exception_is_reported_as_tool_error(self, tmp_path: Path) -> None:
        ctx, recorder, _ = make_context(tmp_path)

        def boom() -> CommandResult:
            raise RuntimeError("boom")

        result = await recorder.run(ctx, DcToolName.SCREENSHOT, {}, boom)

        invocation = recorder.invocations[0]
        assert invocation.status == DcToolStatus.FAILED
        assert invocation.error_code == "tool_error"
        assert invocation.error == "boom" or "boom" in invocation.error
        assert "boom" in result

    async def test_cancellation_marks_cancelled_and_unknown_effect(self, tmp_path: Path) -> None:
        ctx, recorder, events = make_context(tmp_path, timeout=30)
        gate = threading.Event()

        def hanging() -> CommandResult:
            gate.wait(timeout=5)
            return ok_command()

        task = asyncio.create_task(recorder.run(ctx, DcToolName.CLICK, {"x": 1, "y": 2}, hanging))
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        gate.set()

        invocation = recorder.invocations[0]
        assert invocation.status == DcToolStatus.CANCELLED
        assert invocation.error_code == "cancelled"
        assert invocation.effect_status == DcEffectStatus.UNKNOWN
        assert recorder.running() == []
        finished = [event for event in events if event[0] == DcEventType.TOOL_CALL_FINISHED.value]
        assert finished and finished[-1][2]["status"] == "cancelled"


class TestTurnInvocationIds:
    async def test_turn_invocations_filters_by_turn_id(self, tmp_path: Path) -> None:
        ctx, recorder, _ = make_context(tmp_path)

        await recorder.run(ctx, DcToolName.CLICK, {"x": 1, "y": 2}, ok_command)
        await recorder.run(ctx, DcToolName.SCREENSHOT, {}, lambda: "ok")

        turn_ids = [inv.invocation_id for inv in recorder.turn_invocations("turn-1")]
        assert len(turn_ids) == 2
        assert turn_ids == [inv.invocation_id for inv in recorder.invocations]
        assert recorder.turn_invocations("turn-other") == []
