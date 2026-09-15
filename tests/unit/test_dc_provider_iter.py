"""DC Provider 实时步骤流测试：``agent.iter()`` + emit 回调解剖。

覆盖方案 §2.2 / §2.3：THINKING / AGENT_TEXT 事件按序产生、历史不重复 emit、
工具调用轮次的事件交错、emit 为 None 时不崩溃；并锁定「终态回答文本不发
AGENT_TEXT」这一修复（否则前端会重复显示最终回复）。
"""

from __future__ import annotations

import asyncio
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
from pydantic_ai.models.function import AgentInfo, FunctionModel

from harmony_test_agent.config import Settings
from harmony_test_agent.dc.models import DcChatRequest, DcEventType, DcModelTimeout, DcTurnTimeout
from harmony_test_agent.dc.provider import DcChatProvider, MockDcChatProvider
from harmony_test_agent.models import utc_now

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def make_settings() -> Settings:
    return Settings(
        _env_file=None,
        openai_api_key="test-key",
        agent_model="test-model",
        agent_vision_model="test-vision-model",
        agent_provider="openai",
    )


def make_provider(model: Any) -> DcChatProvider:
    """构造 DcChatProvider 并把 ``_model`` 替换为测试模型。"""
    provider = DcChatProvider(make_settings())
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
