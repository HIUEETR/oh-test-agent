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
    DcTokenUsage,
    DcToolInvocation,
    DcToolName,
    DcToolStatus,
    DcToolTier,
    DcTurnStatus,
    DcUsageLimitReached,
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


class TestUsageLimitTurnOutcome:
    """预算耗尽必须落到 blocked + 可读文案，并且下一轮仍能带着目标继续。

    回归背景：真实 run ``dc-20260916T160556Z-d23f9684`` 里 ``UsageLimitExceeded``
    裸穿到轮次层，``turn.error`` 只留下英文异常，用户既看不懂也无法续跑。
    """

    async def test_usage_limit_marks_turn_blocked_with_actionable_error(self, tmp_path: Path) -> None:
        class ExhaustedProvider:
            name = "exhausted"

            async def chat(self, request: Any) -> DcChatResponse:
                raise DcUsageLimitReached(
                    "[turn=x] 本轮模型请求预算已用尽（request_limit=30，tool_calls=50）",
                    request_limit=30,
                    tool_calls_limit=50,
                )

        with patched_device():
            session = make_session(tmp_path, provider=ExhaustedProvider())
            events: list[tuple[str, dict[str, Any]]] = []
            original = session._emit

            def spy(event_type: DcEventType, message: str, payload: dict[str, Any] | None = None) -> None:
                events.append((event_type.value, payload or {}))
                original(event_type, message, payload)

            session._emit = spy  # type: ignore[method-assign]
            turn = await session.handle_user_message("打开日历，为 9 月 22 日创建生日日程")

            assert turn.status == DcTurnStatus.BLOCKED
            assert turn.error is not None
            # 文案必须给出可调大的环境变量，而不是英文裸异常
            assert "DC_MODEL_REQUEST_LIMIT" in turn.error
            assert "UsageLimitExceeded" not in turn.error

            errors = [(event, payload) for event, payload in events if event == DcEventType.ERROR.value]
            assert errors, "预算耗尽必须发出 ERROR 事件"
            assert errors[-1][1]["error_code"] == "usage_limit"
            assert errors[-1][1]["request_limit"] == 30
            assert errors[-1][1]["tool_calls_limit"] == 50

            # 失败轮次仍需留下连续性摘要，用户直接发下一条消息即可继续
            assert session.continuation is not None
            assert session.continuation.original_user_goal == "打开日历，为 9 月 22 日创建生日日程"


class TestTokenUsageAccounting:
    """会话级 token 用量：累计、事件下发、落盘恢复。

    回归背景：pydantic-ai 对自建 OpenAI 兼容端点会把 token/缓存计数丢成 0，
    修正 `_map_usage` 后需要保证会话层正确累计，并且刷新页面/重启后数字一致。
    """

    class _UsageProvider:
        """返回固定用量增量的 provider（不调用真实模型）。"""

        name = "usage-stub"

        def __init__(self, delta: DcTokenUsage) -> None:
            self._delta = delta
            self.usage_delta_turn_id: str | None = None

        @property
        def last_usage_delta(self) -> DcTokenUsage:
            return self._delta

        async def chat(self, request: Any) -> DcChatResponse:
            # 模拟 provider：给增量打上本轮 turn_id
            self.usage_delta_turn_id = request.turn_id
            return DcChatResponse(
                output_text="任务完成",
                history=list(request.history or []),
                usage=self._delta,
                usage_delta=self._delta,
            )

    async def test_turn_accumulates_usage_and_emits_event(self, tmp_path: Path) -> None:
        delta = DcTokenUsage(requests=2, input_tokens=1000, output_tokens=50, cache_read_tokens=800)
        with patched_device():
            session = make_session(tmp_path, provider=self._UsageProvider(delta))
            events: list[tuple[str, dict[str, Any]]] = []
            original = session._emit

            def spy(event_type: DcEventType, message: str, payload: dict[str, Any] | None = None) -> None:
                events.append((event_type.value, payload or {}))
                original(event_type, message, payload)

            session._emit = spy  # type: ignore[method-assign]
            await session.handle_user_message("打开日历")

            # 会话累计 = 本轮增量
            assert session.token_usage.input_tokens == 1000
            assert session.token_usage.cache_read_tokens == 800
            assert session.token_usage.cache_hit_rate == 0.8
            assert session.to_view().token_usage is not None
            assert session.to_view().token_usage.input_tokens == 1000

            updates = [(event, payload) for event, payload in events if event == DcEventType.TOKEN_USAGE_UPDATED.value]
            assert updates, "必须发出 token_usage_updated，前端才能实时刷新统计行"
            payload = updates[-1][1]
            assert payload["session"]["input_tokens"] == 1000
            assert payload["turn"]["requests"] == 2
            assert payload["context_window"] == session.settings.dc_model_context_window
            assert payload["request_limit"] == session.settings.dc_model_request_limit

    async def test_second_turn_accumulates_on_top(self, tmp_path: Path) -> None:
        delta = DcTokenUsage(requests=1, input_tokens=100, output_tokens=10)
        with patched_device():
            session = make_session(tmp_path, provider=self._UsageProvider(delta))

            await session.handle_user_message("第一步")
            await session.handle_user_message("第二步")

            assert session.token_usage.requests == 2
            assert session.token_usage.input_tokens == 200

    async def test_snapshot_round_trip_keeps_token_usage(self, tmp_path: Path) -> None:
        delta = DcTokenUsage(requests=4, input_tokens=2048, output_tokens=128, cache_read_tokens=1024)
        with patched_device():
            session = make_session(tmp_path, provider=self._UsageProvider(delta))
            await session.handle_user_message("打开日历")
            session.save_state()

            restored = make_session(tmp_path)
            snapshot = restored.store.load("dc-continuation")  # type: ignore[union-attr]
            assert snapshot is not None
            restored.apply_snapshot(snapshot)

            assert restored.token_usage.input_tokens == 2048
            assert restored.token_usage.cache_read_tokens == 1024

    async def test_stale_delta_is_not_double_counted(self, tmp_path: Path) -> None:
        """上一轮的增量不能被本轮重复入账（provider 在调用模型前就失败的情况）。"""
        delta = DcTokenUsage(requests=3, input_tokens=900, output_tokens=30)
        with patched_device():
            provider = self._UsageProvider(delta)
            session = make_session(tmp_path, provider=provider)

            await session.handle_user_message("第一轮")
            before = session.token_usage.input_tokens
            # 模拟本轮在调用 provider 前就失败：增量仍属于上一轮
            session._account_provider_usage("turn-other")

            assert session.token_usage.input_tokens == before

    async def test_mock_provider_emits_zero_usage_event(self, tmp_path: Path) -> None:
        """Mock 模式没有真实用量：事件仍发出，但累计保持 0（前端显示空态）。"""
        with patched_device():
            session = make_session(tmp_path)
            await session.handle_user_message("你好")

            assert session.token_usage.has_values is False
            assert session.to_view().token_usage is not None


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
