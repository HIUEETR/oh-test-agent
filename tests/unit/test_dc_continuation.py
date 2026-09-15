"""Phase 3/4：取消后的公开连续性摘要与设备副作用对账门禁。

覆盖计划第 6 节 Phase 3 与 Phase 4 的关键行为：
- 取消/失败轮次仍保存原始目标、已完成操作与公开步骤（不依赖 ``response.history``）；
- 下一轮 prompt 总是带上公开连续性摘要，且**不包含**隐藏 thinking 文本；
- 副作用未确认时新轮次被明确阻塞（``needs_attention``），``reobserve`` 后解除；
- 快照往返后仍要求「先重新观测」，不自动重放旧动作。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from harmony_test_agent.config import Settings
from harmony_test_agent.dc.models import (
    DcChatResponse,
    DcContinuationContext,
    DcEffectStatus,
    DcEventType,
    DcStepKind,
    DcStepRecord,
    DcToolInvocation,
    DcToolName,
    DcToolStatus,
    DcToolTier,
    DcTurnStatus,
)
from harmony_test_agent.dc.provider import MockDcChatProvider
from harmony_test_agent.dc.session import DcSession
from harmony_test_agent.dc.store import DcSessionStore
from harmony_test_agent.models import CommandResult
from harmony_test_agent.storage.artifacts import ArtifactStore

SECRET_THINKING = "隐藏推理：用户可能想新建纪念事件，先猜一下坐标"


def _fake_screenshot_jpeg(
    self: Any,
    output_dir: Path,
    label: str = "screen",
    on_phase: Any = None,
) -> tuple[Path, bytes, int, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "dc_test.jpeg"
    path.write_bytes(b"fake-jpeg-bytes")
    return path, b"fake-jpeg-bytes", 1080, 2232


@contextmanager
def patched_device() -> Iterator[None]:
    with (
        patch("harmony_test_agent.dc.session.HarmonyDeviceAdapter.connect", return_value=None),
        patch(
            "harmony_test_agent.dc.session.HarmonyDeviceAdapter.health_check",
            return_value={"connected": True, "id": "mock-device"},
        ),
        patch(
            "harmony_test_agent.dc.session.HarmonyDeviceAdapter.collect_ui_hierarchy",
            return_value={"attributes": {"pagePath": "pages/CalendarNewEvent"}},
        ),
        patch("harmony_test_agent.dc.hdc.DcHdcExecutor.screenshot_jpeg", _fake_screenshot_jpeg),
    ):
        yield


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
    )


def make_session(tmp_path: Path, provider: Any | None = None) -> DcSession:
    settings = make_settings(tmp_path)
    return DcSession(
        session_id="dc-continuation",
        device_id="mock-device",
        tier=DcToolTier.L1,
        settings=settings,
        artifacts=ArtifactStore(settings.resolved_runtime_dir),
        provider=provider or MockDcChatProvider(),
        store=DcSessionStore(settings.resolved_runtime_dir),
    )


class BlockingProvider:
    """``chat`` 一直等待，用于制造取消/中断场景，并记录收到的请求。"""

    name = "blocking"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.requests: list[Any] = []

    async def chat(self, request: Any) -> DcChatResponse:
        self.requests.append(request)
        self.started.set()
        await asyncio.sleep(30)
        return DcChatResponse()


class CapturingProvider(MockDcChatProvider):
    """立即返回，但记录收到的请求，用于检查下一轮 prompt。"""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def chat(self, request: Any) -> DcChatResponse:
        self.requests.append(request)
        return await super().chat(request)


def completed_invocation(turn_id: str, tool: DcToolName = DcToolName.SCREENSHOT) -> DcToolInvocation:
    return DcToolInvocation(
        invocation_id=f"inv-{tool.value}",
        turn_id=turn_id,
        tool=tool,
        tier=DcToolTier.L1,
        args={},
        success=True,
        status=DcToolStatus.SUCCEEDED,
        effect_status=DcEffectStatus.NONE if tool == DcToolName.SCREENSHOT else DcEffectStatus.CONFIRMED,
        result_summary="ok",
    )


def unknown_effect_invocation(turn_id: str) -> DcToolInvocation:
    return DcToolInvocation(
        invocation_id="inv-click",
        turn_id=turn_id,
        tool=DcToolName.CLICK,
        tier=DcToolTier.L1,
        args={"x": 540, "y": 1200},
        success=False,
        status=DcToolStatus.TIMED_OUT,
        effect_status=DcEffectStatus.UNKNOWN,
        error_code="tool_timeout",
        error="tool click timed out after 30s",
    )


class TestCancelledTurnContinuity:
    async def test_cancelled_turn_keeps_goal_operations_and_steps(self, tmp_path: Path) -> None:
        with patched_device():
            provider = BlockingProvider()
            session = make_session(tmp_path, provider=provider)
            task = asyncio.create_task(session.handle_user_message("打开日历并新建 9/18 纪念事件"))
            session._current_task = task
            await asyncio.wait_for(provider.started.wait(), timeout=5)

            turn = session.turns[-1]
            turn.steps.append(DcStepRecord(step=1, kind=DcStepKind.THINKING, text=SECRET_THINKING))
            turn.steps.append(DcStepRecord(step=2, kind=DcStepKind.AGENT_TEXT, text="我先打开日历应用"))
            session.recorder.invocations.append(completed_invocation(turn.turn_id, DcToolName.SCREENSHOT))
            session.recorder.invocations.append(completed_invocation(turn.turn_id, DcToolName.START_APP))

            assert session.request_stop() is True
            with pytest.raises(asyncio.CancelledError):
                await task

            assert turn.status == DcTurnStatus.CANCELLED
            assert turn.invocation_ids == ["inv-screenshot", "inv-start_app"]

            continuation = session.continuation
            assert continuation is not None
            assert continuation.original_user_goal == "打开日历并新建 9/18 纪念事件"
            assert continuation.previous_status == DcTurnStatus.CANCELLED
            assert any("screenshot" in item for item in continuation.completed_operations)
            assert continuation.public_agent_steps == ["我先打开日历应用"]
            assert continuation.effect_status == DcEffectStatus.CONFIRMED

            prompt = session._continuation_prompt()
            assert "打开日历并新建 9/18 纪念事件" in prompt
            assert SECRET_THINKING not in prompt, "隐藏推理不得进入下一轮上下文"

    async def test_cancelled_turn_with_unknown_effect_requires_reconcile(self, tmp_path: Path) -> None:
        with patched_device():
            provider = BlockingProvider()
            session = make_session(tmp_path, provider=provider)
            task = asyncio.create_task(session.handle_user_message("点击保存"))
            session._current_task = task
            await asyncio.wait_for(provider.started.wait(), timeout=5)

            turn = session.turns[-1]
            session.recorder.invocations.append(unknown_effect_invocation(turn.turn_id))

            assert session.request_stop() is True
            with pytest.raises(asyncio.CancelledError):
                await task

            assert session.pending_attention is not None
            assert session.continuation is not None
            assert session.continuation.reconcile_required is True
            assert session.continuation.effect_status == DcEffectStatus.UNKNOWN
            prompt = session._continuation_prompt()
            assert "重新采集截图" in prompt, "未确认副作用必须要求先重新观测"

    async def test_next_turn_prompt_includes_previous_goal(self, tmp_path: Path) -> None:
        with patched_device():
            blocking = BlockingProvider()
            session = make_session(tmp_path, provider=blocking)
            task = asyncio.create_task(session.handle_user_message("打开日历经由月视图选择 9/18"))
            session._current_task = task
            await asyncio.wait_for(blocking.started.wait(), timeout=5)
            assert session.request_stop() is True
            with pytest.raises(asyncio.CancelledError):
                await task

            capturing = CapturingProvider()
            session.provider = capturing
            await session.handle_user_message("你刚刚在执行什么卡了这么久，现在继续")

            assert capturing.requests, "第二轮必须调用 provider"
            continuation_prompt = capturing.requests[0].continuation_prompt
            assert "打开日历经由月视图选择 9/18" in continuation_prompt
            assert SECRET_THINKING not in continuation_prompt


class TestAttentionGate:
    async def test_pending_attention_blocks_new_turn(self, tmp_path: Path) -> None:
        with patched_device():
            capturing = CapturingProvider()
            session = make_session(tmp_path, provider=capturing)
            invocation = unknown_effect_invocation("turn-old")
            session.recorder.invocations.append(invocation)
            session.pending_attention = invocation

            blocked = session.turn_gate()
            assert blocked is not None and "unconfirmed" in blocked

            turn = await session.handle_user_message("继续")

            assert turn.status == DcTurnStatus.NEEDS_ATTENTION
            assert capturing.requests == [], "副作用未确认时不得驱动设备"

    async def test_reobserve_clears_attention_and_reconcile_flag(self, tmp_path: Path) -> None:
        with patched_device():
            session = make_session(tmp_path)
            invocation = unknown_effect_invocation("turn-old")
            session.recorder.invocations.append(invocation)
            session.pending_attention = invocation
            session.continuation = DcContinuationContext(
                previous_turn_id="turn-old",
                previous_status=DcTurnStatus.NEEDS_ATTENTION,
                original_user_goal="点击保存",
                reconcile_required=True,
                effect_status=DcEffectStatus.UNKNOWN,
            )

            view = await session.resolve_attention("reobserve")

            assert session.turn_gate() is None
            assert session.pending_attention is None
            assert view.continuation is not None
            assert view.continuation.reconcile_required is False

    async def test_resolve_rejects_unknown_action(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        with pytest.raises(ValueError):
            await session.resolve_attention("explode")


class TestRestoreRequiresReobserve:
    def test_snapshot_round_trip_keeps_continuation_and_pending_attention(self, tmp_path: Path) -> None:
        with patched_device():
            session = make_session(tmp_path)
            session.recorder.invocations.append(unknown_effect_invocation("turn-old"))
            session.continuation = DcContinuationContext(
                previous_turn_id="turn-old",
                previous_status=DcTurnStatus.NEEDS_ATTENTION,
                original_user_goal="点击保存",
                reconcile_required=True,
                effect_status=DcEffectStatus.UNKNOWN,
            )
            session.last_page_path = "pages/CalendarNewEvent"
            session.save_state()

            restored = make_session(tmp_path)
            snapshot = restored.store.load("dc-continuation")  # type: ignore[union-attr]
            assert snapshot is not None
            restored.apply_snapshot(snapshot)

            assert restored.pending_attention is not None
            assert restored.pending_attention.tool == DcToolName.CLICK
            assert restored.continuation is not None
            assert restored.continuation.reconcile_required is True
            assert restored.last_page_path == "pages/CalendarNewEvent"
            assert restored.to_view().continuation is not None


class TestEventContract:
    async def test_turn_emits_context_and_checkpoint_events(self, tmp_path: Path) -> None:
        with patched_device():
            session = make_session(tmp_path)
            seen: list[str] = []
            original = session._emit

            def spy(event_type: DcEventType, message: str, payload: dict[str, Any] | None = None) -> None:
                seen.append(event_type.value)
                original(event_type, message, payload)

            session._emit = spy  # type: ignore[method-assign]
            await session.handle_user_message("你好")

            assert DcEventType.CONTEXT_CAPTURE_STARTED.value in seen
            assert DcEventType.CONTEXT_CAPTURE_FINISHED.value in seen
            assert DcEventType.CONTEXT_CHECKPOINT.value in seen
            assert DcEventType.TURN_FINISHED.value in seen


def command_ok() -> CommandResult:
    return CommandResult(command="hdc shell rm -f /data/local/tmp/x", returncode=0, stdout="ok")
