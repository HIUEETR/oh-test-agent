"""DC Provider 实时步骤流测试：``agent.iter()`` + emit 回调解剖。

覆盖方案 §2.2 / §2.3：THINKING / AGENT_TEXT 事件按序产生、历史不重复 emit、
工具调用轮次的事件交错、emit 为 None 时不崩溃；并锁定「终态回答文本不发
AGENT_TEXT」这一修复（否则前端会重复显示最终回复）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import pytest
from pydantic_ai import Tool
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaThinkingPart, DeltaToolCall, FunctionModel

from harmony_test_agent.config import Settings
from harmony_test_agent.dc.models import (
    DcChatRequest,
    DcEventType,
    DcModelTimeout,
    DcTokenUsage,
    DcTurnTimeout,
    DcUsageLimitReached,
)
from harmony_test_agent.dc.provider import DC_SYSTEM_PROMPT, DcChatProvider, MockDcChatProvider, _usage_limits
from harmony_test_agent.models import utc_now

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "openai_api_key": "test-key",
        "agent_model": "test-model",
        "agent_vision_model": "test-vision-model",
        "agent_provider": "openai",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_provider(model: Any, **settings_overrides: Any) -> DcChatProvider:
    """构造 DcChatProvider 并把 ``_model`` 替换为测试模型。"""
    provider = DcChatProvider(make_settings(**settings_overrides))
    provider._model = lambda vision=False: model  # type: ignore[method-assign]
    return provider


def collect(events: list[tuple[str, str, dict[str, Any]]]) -> Any:
    return lambda event_type, message, payload: events.append((event_type, message, payload))


def visible(events: list[tuple[str, str, dict[str, Any]]]) -> list[tuple[str, str, dict[str, Any]]]:
    """只保留思考/叙述事件。

    Phase 2 起每次模型推进还会发 ``model_call_started/progress/finished``，
    这些事件的断言在 ``TestModelCallProgress`` 中单独覆盖。
    """
    keep = {DcEventType.THINKING.value, DcEventType.AGENT_TEXT.value}
    return [event for event in events if event[0] in keep]


async def ping_tool() -> str:
    """Return pong."""
    return "pong"


def ping_registry() -> list[Tool]:
    return [Tool(ping_tool, name="ping", description="Return pong.")]


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


class TestIterEmitsSteps:
    """逐节点差分 all_messages()，实时 emit 思考与叙述。

    终态回答的文本只通过 ``ASSISTANT_MESSAGE`` 呈现，不在此处 emit：
    否则前端会出现两条内容相同的「任务完成」（叙述气泡 + 助手气泡）。
    """

    async def test_thinking_and_narration_emitted_before_tool_call(self) -> None:
        calls = {"count": 0}

        def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            calls["count"] += 1
            if calls["count"] == 1:
                return ModelResponse(
                    parts=[
                        ThinkingPart(content="先看看当前界面有哪些元素"),
                        TextPart(content="我来点击搜索框"),
                        ToolCallPart(tool_name="ping", args={}),
                    ]
                )
            return ModelResponse(parts=[TextPart(content="任务完成")])

        provider = make_provider(FunctionModel(model_fn))
        events: list[tuple[str, str, dict[str, Any]]] = []
        request = DcChatRequest(user_prompt="搜索 OpenHarmony", tools=ping_registry(), emit=collect(events))

        response = await provider.chat(request)

        assert [event[0] for event in visible(events)] == [
            DcEventType.THINKING.value,
            DcEventType.AGENT_TEXT.value,
        ]
        assert visible(events)[0][2]["text"] == "先看看当前界面有哪些元素"
        assert visible(events)[0][2]["step"] == 1
        assert visible(events)[1][2]["text"] == "我来点击搜索框"
        assert visible(events)[1][2]["step"] == 2
        assert response.output_text == "任务完成"
        assert response.tool_call_count == 1

    async def test_terminal_text_is_not_emitted_as_narration(self) -> None:
        """纯文本回答（无工具调用）只属于 ASSISTANT_MESSAGE。"""

        def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(parts=[ThinkingPart(content="想一下"), TextPart(content="任务完成")])

        provider = make_provider(FunctionModel(model_fn))
        events: list[tuple[str, str, dict[str, Any]]] = []

        response = await provider.chat(DcChatRequest(user_prompt="你好", emit=collect(events)))

        assert [event[0] for event in visible(events)] == [DcEventType.THINKING.value]
        assert "任务完成" not in [event[2]["text"] for event in visible(events)]
        assert response.output_text == "任务完成"

    async def test_multi_step_tool_loop_emits_in_order(self) -> None:
        calls = {"count": 0}

        def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            calls["count"] += 1
            if calls["count"] == 1:
                return ModelResponse(
                    parts=[
                        TextPart(content="先探测设备状态"),
                        ToolCallPart(tool_name="ping", args={}),
                    ]
                )
            return ModelResponse(parts=[TextPart(content="任务完成")])

        provider = make_provider(FunctionModel(model_fn))
        events: list[tuple[str, str, dict[str, Any]]] = []
        request = DcChatRequest(user_prompt="探测", tools=ping_registry(), emit=collect(events))

        response = await provider.chat(request)

        assert [event[0] for event in visible(events)] == [DcEventType.AGENT_TEXT.value]
        assert visible(events)[0][2]["text"] == "先探测设备状态"
        assert response.output_text == "任务完成"
        assert response.tool_call_count == 1

    async def test_identical_narration_is_emitted_once(self) -> None:
        """retries/重放可能重复产生同一 part，按 (kind, text) 去重。"""
        calls = {"count": 0}

        def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            calls["count"] += 1
            if calls["count"] <= 2:
                return ModelResponse(
                    parts=[
                        TextPart(content="再次探测"),
                        ToolCallPart(tool_name="ping", args={}),
                    ]
                )
            return ModelResponse(parts=[TextPart(content="任务完成")])

        provider = make_provider(FunctionModel(model_fn))
        events: list[tuple[str, str, dict[str, Any]]] = []
        request = DcChatRequest(user_prompt="探测", tools=ping_registry(), emit=collect(events))

        await provider.chat(request)

        assert [event[2]["text"] for event in visible(events)] == ["再次探测"]

    async def test_history_text_is_not_re_emitted(self) -> None:
        def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(parts=[TextPart(content="本轮新叙述")])

        provider = make_provider(FunctionModel(model_fn))
        history: list[ModelMessage] = [
            ModelRequest(parts=[UserPromptPart(content="上一轮问题")]),
            ModelResponse(parts=[ThinkingPart(content="上一轮思考"), TextPart(content="上一轮旧叙述")]),
        ]
        events: list[tuple[str, str, dict[str, Any]]] = []
        request = DcChatRequest(user_prompt="继续", history=history, emit=collect(events))

        await provider.chat(request)

        assert visible(events) == []

    async def test_blank_parts_are_skipped(self) -> None:
        calls = {"count": 0}

        def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            calls["count"] += 1
            if calls["count"] == 1:
                return ModelResponse(
                    parts=[
                        ThinkingPart(content="   "),
                        TextPart(content="有内容"),
                        ToolCallPart(tool_name="ping", args={}),
                    ]
                )
            return ModelResponse(parts=[TextPart(content="完成")])

        provider = make_provider(FunctionModel(model_fn))
        events: list[tuple[str, str, dict[str, Any]]] = []

        await provider.chat(DcChatRequest(user_prompt="hi", tools=ping_registry(), emit=collect(events)))

        assert [event[0] for event in visible(events)] == [DcEventType.AGENT_TEXT.value]
        assert visible(events)[0][2]["text"] == "有内容"

    async def test_emit_none_does_not_crash(self) -> None:
        def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(parts=[TextPart(content="无回调")])

        provider = make_provider(FunctionModel(model_fn))
        response = await provider.chat(DcChatRequest(user_prompt="hi"))

        assert response.output_text == "无回调"


class TestMockProviderEmits:
    """Mock Provider 也要发出思考/叙述，供无模型配置时联调前端。"""

    async def test_mock_emits_thinking_and_text(self) -> None:
        provider = MockDcChatProvider()
        events: list[tuple[str, str, dict[str, Any]]] = []

        response = await provider.chat(DcChatRequest(user_prompt="你好", emit=collect(events)))

        assert [event[0] for event in visible(events)] == [
            DcEventType.THINKING.value,
            DcEventType.AGENT_TEXT.value,
        ]
        assert "Mock" in response.output_text

    async def test_mock_without_emit(self) -> None:
        provider = MockDcChatProvider()
        response = await provider.chat(DcChatRequest(user_prompt="你好"))

        assert "任务完成（Mock）" in response.output_text


class TestModelCallProgress:
    """Phase 2：模型调用的 started/progress/finished/failed 与 deadline 约束。"""

    async def test_model_call_events_wrap_each_advance(self) -> None:
        def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(parts=[TextPart(content="完成")])

        provider = make_provider(FunctionModel(model_fn))
        events: list[tuple[str, str, dict[str, Any]]] = []

        await provider.chat(DcChatRequest(user_prompt="hi", turn_id="turn-1", emit=collect(events)))

        started = [event for event in events if event[0] == DcEventType.MODEL_CALL_STARTED.value]
        finished = [event for event in events if event[0] == DcEventType.MODEL_CALL_FINISHED.value]
        assert started and finished
        assert started[0][2]["model_call_id"] == finished[0][2]["model_call_id"]
        assert started[0][2]["turn_id"] == "turn-1"
        assert started[0][2]["phase"] == "waiting_model"
        assert started[0][2]["remaining_budget_ms"] == 90_000
        assert "deadline_at" in started[0][2]

    async def test_slow_model_raises_timeout_and_emits_failed(self) -> None:
        async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            await asyncio.sleep(5)
            return ModelResponse(parts=[TextPart(content="太慢")])

        provider = make_provider(FunctionModel(model_fn))
        events: list[tuple[str, str, dict[str, Any]]] = []
        request = DcChatRequest(
            user_prompt="hi",
            turn_id="turn-slow",
            model_timeout=0.2,
            progress_interval=0.05,
            emit=collect(events),
        )

        with pytest.raises(DcModelTimeout):
            await provider.chat(request)

        failed = [event for event in events if event[0] == DcEventType.MODEL_CALL_FAILED.value]
        assert failed, "模型超时必须发出 model_call_failed"
        assert failed[0][2]["error_code"] == "model_timeout"
        started_ids = {
            event[2]["model_call_id"] for event in events if event[0] == DcEventType.MODEL_CALL_STARTED.value
        }
        assert failed[0][2]["model_call_id"] in started_ids

    async def test_turn_deadline_short_circuits_before_model_call(self) -> None:
        def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(parts=[TextPart(content="不应执行")])

        provider = make_provider(FunctionModel(model_fn))
        events: list[tuple[str, str, dict[str, Any]]] = []
        request = DcChatRequest(
            user_prompt="hi",
            turn_deadline=utc_now() - timedelta(seconds=1),
            emit=collect(events),
        )

        with pytest.raises(DcTurnTimeout):
            await provider.chat(request)

        assert [event for event in events if event[0] == DcEventType.MODEL_CALL_STARTED.value] == []

    async def test_continuation_prompt_is_injected(self) -> None:
        seen: list[str] = []

        def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            for message in messages:
                for part in getattr(message, "parts", []):
                    content = getattr(part, "content", "")
                    if isinstance(content, str):
                        seen.append(content)
                    elif isinstance(content, list):
                        seen.extend(item for item in content if isinstance(item, str))
            return ModelResponse(parts=[TextPart(content="完成")])

        provider = make_provider(FunctionModel(model_fn))
        request = DcChatRequest(
            user_prompt="继续",
            continuation_prompt="上一轮公开连续性摘要：原始目标=打开日历",
        )

        await provider.chat(request)

        assert any("上一轮公开连续性摘要" in text and "打开日历" in text for text in seen)


class TestUsageLimits:
    """预算上限必须可配置，且耗尽时要变成可读的 DC 领域异常。

    回归背景：``request_limit`` 曾被硬编码为 30（DC 每轮一个工具 ⇒ 30 次请求 ≈ 30 次
    工具往返），一次真实日历任务在第 31 次模型请求前抛 ``UsageLimitExceeded``，
    并把英文裸异常直接写进 ``turn.error``。
    """

    def test_limits_come_from_settings(self) -> None:
        settings = make_settings()

        limits = _usage_limits(settings)

        assert limits.request_limit == settings.dc_model_request_limit
        assert limits.tool_calls_limit == settings.dc_model_tool_calls_limit

    def test_default_request_limit_is_relaxed(self) -> None:
        """默认值必须明显高于历史事故的 30，否则多步任务仍会被提前打断。"""
        limits = _usage_limits(make_settings())

        assert limits.request_limit is not None and limits.request_limit > 30

    def test_limits_follow_overrides(self) -> None:
        limits = _usage_limits(make_settings(dc_model_request_limit=7, dc_model_tool_calls_limit=9))

        assert limits.request_limit == 7
        assert limits.tool_calls_limit == 9

    async def test_exhausted_request_limit_raises_domain_error(self) -> None:
        """模型持续请求工具时，达到上限必须转为 ``DcUsageLimitReached`` 并发失败事件。"""

        def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(
                parts=[
                    TextPart(content="继续探测"),
                    ToolCallPart(tool_name="ping", args={}),
                ]
            )

        provider = make_provider(
            FunctionModel(model_fn),
            dc_model_request_limit=3,
            dc_model_tool_calls_limit=2,
        )
        events: list[tuple[str, str, dict[str, Any]]] = []
        request = DcChatRequest(user_prompt="探测", tools=ping_registry(), emit=collect(events))

        with pytest.raises(DcUsageLimitReached) as excinfo:
            await provider.chat(request)

        error = excinfo.value
        assert error.request_limit == 3
        assert error.tool_calls_limit == 2
        assert "request_limit=3" in str(error)

        failed = [event for event in events if event[0] == DcEventType.MODEL_CALL_FAILED.value]
        assert failed, "预算耗尽必须发出 model_call_failed，否则前端看不到失败"
        assert failed[0][2]["error_code"] == "usage_limit"
        assert failed[0][2]["request_limit"] == 3
        assert failed[0][2]["tool_calls_limit"] == 2

    async def test_tool_calls_limit_is_enforced_too(self) -> None:
        """工具调用上限独立生效（模型一次回复内发多个工具调用时可能先命中）。"""

        def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(
                parts=[
                    ToolCallPart(tool_name="ping", args={}),
                    ToolCallPart(tool_name="ping", args={}),
                ]
            )

        provider = make_provider(
            FunctionModel(model_fn),
            dc_model_request_limit=10,
            dc_model_tool_calls_limit=2,
        )
        request = DcChatRequest(user_prompt="探测", tools=ping_registry())

        with pytest.raises(DcUsageLimitReached) as excinfo:
            await provider.chat(request)

        assert excinfo.value.tool_calls_limit == 2
        # 文案必须说明真正命中的是哪一个上限，而不是笼统地说「请求超限」
        assert "tool_calls_limit of 2" in str(excinfo.value)


class TestTokenUsageCapture:
    """Token 用量：必须从 provider 真实响应的 RunUsage 取到，并计算本轮增量。"""

    def test_from_run_usage_maps_fields(self) -> None:
        class FakeRunUsage:
            requests = 3
            tool_calls = 2
            input_tokens = 1721
            output_tokens = 16
            cache_read_tokens = 1536
            cache_write_tokens = 5
            details = {"reasoning_tokens": 16}

        usage = DcTokenUsage.from_run_usage(FakeRunUsage())

        assert usage.requests == 3
        assert usage.tool_calls == 2
        assert usage.input_tokens == 1721
        assert usage.output_tokens == 16
        assert usage.cache_read_tokens == 1536
        assert usage.cache_write_tokens == 5
        assert usage.details == {"reasoning_tokens": 16}
        assert usage.total_tokens == 1737

    def test_cache_hit_rate_uses_input_as_denominator(self) -> None:
        """分母是 input_tokens（含缓存命中）；无输入 token 时返回 None 而非除零。"""
        assert DcTokenUsage(input_tokens=1721, cache_read_tokens=1536).cache_hit_rate == pytest.approx(0.8925, abs=1e-4)
        assert DcTokenUsage(input_tokens=0, cache_read_tokens=100).cache_hit_rate is None
        assert DcTokenUsage().has_values is False

    def test_delta_is_cumulative_difference(self) -> None:
        """run 的累计值跨轮递增，本轮消耗 = 相邻两次累计之差。"""
        first = DcTokenUsage(requests=1, input_tokens=100, output_tokens=10, cache_read_tokens=0)
        second = DcTokenUsage(requests=3, input_tokens=250, output_tokens=25, cache_read_tokens=40)

        delta = second.minus(first)

        assert delta.requests == 2
        assert delta.input_tokens == 150
        assert delta.output_tokens == 15
        assert delta.cache_read_tokens == 40

        # 计数不会回退：负值截断为 0
        assert first.minus(second).input_tokens == 0

    async def test_chat_reports_usage_and_delta(self) -> None:
        """chat() 必须回传本次累计用量与本轮增量，并给增量打上轮次标记。"""

        def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(parts=[TextPart(content="完成")])

        provider = make_provider(FunctionModel(model_fn))
        response = await provider.chat(DcChatRequest(user_prompt="hi", turn_id="turn-1"))

        assert response.usage_delta.tool_calls == 0
        assert response.usage.has_values is True
        assert provider.usage_delta_turn_id == "turn-1"
        assert provider.last_usage_delta == response.usage_delta

    async def test_mock_provider_reports_no_usage(self) -> None:
        """Mock 不调用真实模型：返回空用量，前端据此显示空态而不是 0%。"""
        provider = MockDcChatProvider()

        response = await provider.chat(DcChatRequest(user_prompt="hi"))

        assert response.usage.has_values is False
        assert response.usage_delta.has_values is False


class TestSystemPromptIdentityAndAssertions:
    """改动 B：提示词必须要求「身份记录」与「断言 checkpoint」。

    回归背景：规则 3 原先明令「无断言」，与 generator 的 replay_eligible 判定
    （需 ≥1 显式断言）自相矛盾；提示词也从未要求记录 foreground_app/start_app，
    导致会话录制里没有应用身份，脚本与蒸馏都拿不到真实 bundle。
    """

    def test_prompt_requires_identity_recording(self) -> None:
        assert "身份记录" in DC_SYSTEM_PROMPT
        assert "foreground_app" in DC_SYSTEM_PROMPT
        assert "start_app" in DC_SYSTEM_PROMPT
        assert "ability_name" in DC_SYSTEM_PROMPT
        assert "禁止猜测占位值" in DC_SYSTEM_PROMPT

    def test_prompt_requires_assertion_checkpoints(self) -> None:
        assert "断言 checkpoint" in DC_SYSTEM_PROMPT
        assert "assert_text/assert_visible" in DC_SYSTEM_PROMPT
        # 旧的「无断言」规则必须彻底消失，否则模型仍不会留证
        assert "无断言" not in DC_SYSTEM_PROMPT
        assert "不要尝试使用 assert_visible/assert_text" not in DC_SYSTEM_PROMPT

    def test_prompt_records_identity_after_target_app_is_foreground(self) -> None:
        """身份必须在目标应用进入前台**之后**记录，并排除桌面身份。

        回归背景（30e 复盘）：原文案要求「任务开始、首次操作目标应用前」记录一次，
        模型照做后记到的是桌面（com.ohos.sceneboard / ability=unknown），
        脚本生成与蒸馏因此始终拿不到真实 bundle/ability。
        """
        assert "目标应用进入前台之后" in DC_SYSTEM_PROMPT
        assert "com.ohos.sceneboard" in DC_SYSTEM_PROMPT
        assert "ability=unknown" in DC_SYSTEM_PROMPT
        # start_app 已下放到 L2，提示词可以要求模型用它显式声明身份
        assert "MainAbility" in DC_SYSTEM_PROMPT

    def test_other_execution_rules_stay_intact(self) -> None:
        """只改规则 3/9：其余规则与回复格式不动。"""
        assert "**自主执行**" in DC_SYSTEM_PROMPT
        assert "**每轮一个工具**" in DC_SYSTEM_PROMPT
        assert "**先看清再动手**" in DC_SYSTEM_PROMPT
        assert "## 回复格式" in DC_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# token 级流式（改动 D）
# ---------------------------------------------------------------------------


def make_stream_provider(stream_fn: Any, **settings_overrides: Any) -> DcChatProvider:
    """构造 provider：非流式走 function，流式走 stream_function。

    ``DcChatProvider._model`` 被替换为同一个 FunctionModel，因此
    ``dc_token_streaming=False`` 时走 ``function``、开启时走 ``stream_function``。
    """
    model = FunctionModel(
        lambda messages, info: ModelResponse(parts=[TextPart(content="整块输出")]),
        stream_function=stream_fn,
    )
    return make_provider(model, **settings_overrides)


def deltas(events: list[tuple[str, str, dict[str, Any]]]) -> list[tuple[str, str, dict[str, Any]]]:
    return [event for event in events if event[0] == DcEventType.MESSAGE_DELTA.value]


class TestTokenStreaming:
    """改动 D：token 级增量 + 全量事件收口 + 关闭开关 + 不支持流式的回退。"""

    async def test_deltas_stream_then_full_events_close_drafts(self) -> None:
        calls = {"stream": 0}

        async def stream_fn(messages: list[ModelMessage], info: AgentInfo) -> Any:
            calls["stream"] += 1
            if calls["stream"] == 1:
                for chunk in ["先", "看"]:
                    yield {"0": DeltaThinkingPart(content=chunk)}
                    await asyncio.sleep(0.1)
                # 工具调用先出现：之后到达的文本属于「中途叙述」
                yield {"1": DeltaToolCall(name="ping", json_args="{}", tool_call_id="call-1")}
                for chunk in ["我来", "点击"]:
                    yield chunk
                    await asyncio.sleep(0.1)
            else:
                for chunk in ["任务", "完成"]:
                    yield chunk
                    await asyncio.sleep(0.1)

        provider = make_stream_provider(stream_fn)
        events: list[tuple[str, str, dict[str, Any]]] = []
        request = DcChatRequest(user_prompt="搜索", turn_id="turn-1", tools=ping_registry(), emit=collect(events))

        response = await provider.chat(request)

        emitted = deltas(events)
        thinking_deltas = [event for event in emitted if event[2]["stream_key"] == "turn-1:m1:thinking"]
        text_deltas = [event for event in emitted if event[2]["stream_key"] == "turn-1:m1:text"]
        assert thinking_deltas and text_deltas
        # 增量按序拼接 == 全量文本
        assert "".join(event[2]["delta"] for event in thinking_deltas) == "先看"
        assert "".join(event[2]["delta"] for event in text_deltas) == "我来点击"
        # 思考增量与叙述增量的 role 由 part 种类/是否已有工具调用决定
        assert {event[2]["role"] for event in thinking_deltas} == {"thinking"}
        assert {event[2]["role"] for event in text_deltas} == {"narration"}
        # delta 与全量事件共用同一个 stream_key（前端据此就地收口草稿）
        thinking_event = next(event for event in events if event[0] == DcEventType.THINKING.value)
        agent_text_event = next(event for event in events if event[0] == DcEventType.AGENT_TEXT.value)
        assert thinking_deltas[0][2]["stream_key"] == "turn-1:m1:thinking"
        assert thinking_event[2]["stream_key"] == thinking_deltas[0][2]["stream_key"]
        assert agent_text_event[2]["stream_key"] == text_deltas[0][2]["stream_key"]
        assert agent_text_event[2]["text"] == "".join(event[2]["delta"] for event in text_deltas)
        assert thinking_event[2]["text"] == "".join(event[2]["delta"] for event in thinking_deltas)
        # 终态回答（第二条模型消息）只有增量草稿，没有 AGENT_TEXT 收口
        text_keys = [event[2]["stream_key"] for event in emitted if event[2]["stream_key"].endswith(":text")]
        assert list(dict.fromkeys(text_keys)) == ["turn-1:m1:text", "turn-1:m3:text"]
        # 手动消费节点不能让模型被请求两次
        assert calls["stream"] == 2
        assert response.tool_call_count == 1

    async def test_text_delta_role_is_assistant_before_tool_call_appears(self) -> None:
        """文本先于工具调用到达时 role=assistant，由后续全量事件改判为叙述。"""
        calls = {"stream": 0}

        async def stream_fn(messages: list[ModelMessage], info: AgentInfo) -> Any:
            calls["stream"] += 1
            if calls["stream"] == 1:
                yield "我来点击"
                await asyncio.sleep(0.1)
                yield {"0": DeltaToolCall(name="ping", json_args="{}", tool_call_id="call-1")}
                await asyncio.sleep(0.1)
            else:
                yield "任务完成"
                await asyncio.sleep(0.1)

        provider = make_stream_provider(stream_fn)
        events: list[tuple[str, str, dict[str, Any]]] = []
        request = DcChatRequest(user_prompt="搜索", turn_id="turn-1", tools=ping_registry(), emit=collect(events))

        await provider.chat(request)

        text_deltas = [event for event in deltas(events) if event[2]["stream_key"] == "turn-1:m1:text"]
        assert text_deltas
        assert {event[2]["role"] for event in text_deltas} == {"assistant"}
        # 全量叙述事件仍然发出，供前端把草稿改判为 narration
        agent_text_event = next(event for event in events if event[0] == DcEventType.AGENT_TEXT.value)
        assert agent_text_event[2]["stream_key"] == text_deltas[0][2]["stream_key"]

    async def test_terminal_text_deltas_are_not_re_emitted_as_full_events(self) -> None:
        """终态回答只发增量草稿，仍不发 AGENT_TEXT（否则前端出现两条重复回复）。"""
        calls = {"stream": 0}

        async def stream_fn(messages: list[ModelMessage], info: AgentInfo) -> Any:
            calls["stream"] += 1
            if calls["stream"] == 1:
                yield "先探测"
                await asyncio.sleep(0.1)
                yield {"0": DeltaToolCall(name="ping", json_args="{}", tool_call_id="call-1")}
                await asyncio.sleep(0.1)
            else:
                yield "任务完成"
                await asyncio.sleep(0.1)

        provider = make_stream_provider(stream_fn)
        events: list[tuple[str, str, dict[str, Any]]] = []
        request = DcChatRequest(user_prompt="探测", turn_id="turn-1", tools=ping_registry(), emit=collect(events))

        response = await provider.chat(request)

        assert response.output_text == "任务完成"
        assert [event[2]["text"] for event in visible(events)] == ["先探测"]
        # 终态文本仍有增量（前端逐字显示，由 ASSISTANT_MESSAGE 收口）
        assert any(event[2]["delta"] == "任务完成" for event in deltas(events))

    async def test_streaming_emits_progress_heartbeat(self) -> None:
        """流式等待期间也要发 model_call_progress（复用进度心跳），并标记 streaming。"""

        async def stream_fn(messages: list[ModelMessage], info: AgentInfo) -> Any:
            yield "开始输出"
            # 模型静默期：这段没有任何 delta，靠心跳让「最近进展」与剩余预算继续走
            await asyncio.sleep(0.6)
            yield "结束"

        provider = make_stream_provider(stream_fn)
        events: list[tuple[str, str, dict[str, Any]]] = []
        request = DcChatRequest(
            user_prompt="hi",
            turn_id="turn-1",
            progress_interval=0.05,
            emit=collect(events),
        )

        await provider.chat(request)

        started = [event for event in events if event[0] == DcEventType.MODEL_CALL_STARTED.value]
        progress = [event for event in events if event[0] == DcEventType.MODEL_CALL_PROGRESS.value]
        # 流式路径自己发的 started 带 streaming 标记（节点推进那条不带，属既有事件）
        assert any(event[2].get("streaming") is True for event in started)
        # 静默期的心跳同样是流式路径发出的 progress 事件
        heartbeat = [event for event in progress if event[2].get("streaming") is True]
        assert heartbeat, "流式等待期间必须有进度心跳"
        assert all(event[2]["turn_id"] == "turn-1" for event in heartbeat)

    async def test_streaming_disabled_emits_no_deltas(self) -> None:
        async def stream_fn(messages: list[ModelMessage], info: AgentInfo) -> Any:
            yield "不应被调用"

        provider = make_stream_provider(stream_fn, dc_token_streaming=False)
        events: list[tuple[str, str, dict[str, Any]]] = []

        response = await provider.chat(DcChatRequest(user_prompt="hi", turn_id="turn-1", emit=collect(events)))

        assert deltas(events) == []
        assert response.output_text == "整块输出"

    async def test_model_without_stream_support_falls_back_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """模型不支持流式：记录 warning、退回整块推进，轮次照常完成。"""

        def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(parts=[ThinkingPart(content="想一下"), TextPart(content="任务完成")])

        provider = make_provider(FunctionModel(model_fn))  # 只有 function，没有 stream_function
        events: list[tuple[str, str, dict[str, Any]]] = []

        with caplog.at_level(logging.WARNING, logger="harmony_test_agent.dc.provider"):
            response = await provider.chat(DcChatRequest(user_prompt="hi", emit=collect(events)))

        assert response.output_text == "任务完成"
        assert deltas(events) == []
        # 全量思考事件仍按原路径发出
        assert [event[0] for event in visible(events)] == [DcEventType.THINKING.value]
        assert any("streaming unavailable" in record.getMessage() for record in caplog.records)

    async def test_streaming_timeout_emits_failed_and_raises(self) -> None:
        async def stream_fn(messages: list[ModelMessage], info: AgentInfo) -> Any:
            yield "开始输出"
            await asyncio.sleep(5)

        provider = make_stream_provider(stream_fn)
        events: list[tuple[str, str, dict[str, Any]]] = []
        request = DcChatRequest(
            user_prompt="hi",
            turn_id="turn-slow",
            model_timeout=0.3,
            progress_interval=0.05,
            emit=collect(events),
        )

        with pytest.raises(DcModelTimeout):
            await provider.chat(request)

        failed = [event for event in events if event[0] == DcEventType.MODEL_CALL_FAILED.value]
        assert failed, "流式超时必须发出 model_call_failed"
        assert failed[-1][2]["error_code"] == "model_timeout"
        assert deltas(events), "超时前已到达的增量不应被丢弃"
