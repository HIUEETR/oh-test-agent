"""锁定「结构化输出不得使用工具」的回归测试。

背景：网关对 thinking 模型拒绝任何强制 ``tool_choice``（``required`` 或指定函数），
返回 ``400 Thinking mode does not support this tool_choice``。pydantic-ai 在
``output_type`` 直接传 ``BaseModel`` 时会走 ``tool`` 模式、注册 ``final_result`` 工具，
OpenAI 适配层因而发送 ``tool_choice='required'``，整轮调用失败。实测记录见
``docs/STARTUP_GUIDE.md`` §15.4。

本文件锁死两条不变量：

1. 四条结构化输出路径都把 ``PromptedOutput`` 交给 ``Agent``；
2. 该规格在 pydantic-ai 内部解析为 ``output_mode='prompted'`` 且不注册输出工具。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import PromptedOutput
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from harmony_test_agent.agents.providers import OpenAICompatibleProvider, PlanningContext
from harmony_test_agent.config import Settings
from harmony_test_agent.discovery.advisor import AdvisorVerdict
from harmony_test_agent.models import (
    PlannedStep,
    PlanResult,
    ScreenSnapshot,
    ToolDecision,
    ToolName,
    VisionObservation,
)

# 每条路径各自的最小合法输出，供 Agent 替身按注册的 output_type 返回。
PAYLOADS: dict[type[Any], dict[str, Any]] = {
    PlanResult: {"goal": "打开应用", "steps": [], "model_used": "test-model", "mock": False},
    VisionObservation: {"page_title": "首页", "summary": "摘要", "elements": []},
    ToolDecision: {"tool": ToolName.CLICK_ELEMENT.value, "target": "搜索"},
    AdvisorVerdict: {"page_summary": "首页", "recommended": [0], "avoid": [], "reason": "r"},
}


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        openai_api_key="test-key",
        agent_model="test-model",
        agent_vision_model="test-vision-model",
        agent_provider="openai",
    )


@pytest.fixture
def image_bytes(monkeypatch: pytest.MonkeyPatch) -> bytes:
    """让快照图片读取不落盘，测试因而与临时目录权限无关。"""
    payload = b"\x89PNG-fake-image-bytes"
    monkeypatch.setattr(Path, "read_bytes", lambda self: payload)
    return payload


@pytest.fixture
def snapshot() -> ScreenSnapshot:
    return ScreenSnapshot(
        snapshot_id="snap-structured-output",
        run_id="run-structured-output",
        image_path=Path("snap-structured-output.png"),
        image_sha256="hash",
        width=100,
        height=200,
        page_path="/page/0",
        elements=[],
    )


def _planning_context() -> PlanningContext:
    return PlanningContext(
        target_app_id="demo",
        display_name="Demo",
        bundle_name="com.example.demo",
        main_ability="EntryAbility",
    )


class _RunResult:
    """``Agent.run`` 的最小替身：只需暴露被调用方读取的 ``output`` 与消息历史。"""

    def __init__(self, output: Any) -> None:
        self.output = output

    def all_messages(self) -> list[Any]:
        return []


def _record_agent_output_types(
    monkeypatch: pytest.MonkeyPatch,
    output_data_type: type[Any],
) -> list[Any]:
    """把 ``pydantic_ai.Agent`` 换成记录 output_type 的替身，返回记录列表。

    替身按 ``output_data_type`` 返回该路径的最小合法载荷，因此没有模型调用、
    也没有网络访问。
    """
    import pydantic_ai

    recorded: list[Any] = []

    class _RecordingAgent:
        def __init__(self, model: object, **kwargs: Any) -> None:
            self.model = model
            self.kwargs = kwargs
            recorded.append(kwargs.get("output_type"))

        async def run(self, *args: Any, **kwargs: Any) -> _RunResult:
            assert isinstance(self.kwargs.get("output_type"), PromptedOutput)
            return _RunResult(output_data_type.model_validate(PAYLOADS[output_data_type]))

    monkeypatch.setattr(pydantic_ai, "Agent", _RecordingAgent)
    return recorded


def test_plan_uses_prompted_output(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _record_agent_output_types(monkeypatch, PlanResult)
    provider = OpenAICompatibleProvider(_settings())

    plan = asyncio.run(provider.plan("打开应用", _planning_context(), max_steps=5))

    assert isinstance(recorded[0], PromptedOutput)
    assert plan.goal == "打开应用"


def test_analyze_uses_prompted_output(
    monkeypatch: pytest.MonkeyPatch, snapshot: ScreenSnapshot, image_bytes: bytes
) -> None:
    recorded = _record_agent_output_types(monkeypatch, VisionObservation)
    provider = OpenAICompatibleProvider(_settings())

    observation = asyncio.run(provider.analyze(snapshot))

    assert isinstance(recorded[0], PromptedOutput)
    assert observation is not None and observation.page_title == "首页"


def test_decide_uses_prompted_output(
    monkeypatch: pytest.MonkeyPatch, snapshot: ScreenSnapshot, image_bytes: bytes
) -> None:
    recorded = _record_agent_output_types(monkeypatch, ToolDecision)
    provider = OpenAICompatibleProvider(_settings())
    step = PlannedStep(step_id="1", instruction="点击搜索", tool=ToolName.CLICK_ELEMENT, target="搜索")

    decision = asyncio.run(provider.decide(step, snapshot))

    assert isinstance(recorded[0], PromptedOutput)
    assert decision.tool is ToolName.CLICK_ELEMENT


def test_advisor_turn_uses_prompted_output(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _record_agent_output_types(monkeypatch, AdvisorVerdict)
    provider = OpenAICompatibleProvider(_settings())

    turn = asyncio.run(provider.advise_turn([], b"\x89PNG-fake", "payload"))

    assert isinstance(recorded[0], PromptedOutput)
    assert turn is not None and turn.verdict.recommended == [0]


def test_prompted_output_spec_resolves_to_prompted_mode() -> None:
    """机制层不变量：prompted 模式不注册输出工具，因而不会发送强制 tool_choice。"""
    provider = OpenAICompatibleProvider(_settings())
    seen: dict[str, Any] = {}

    def handler(messages: list[Any], info: AgentInfo) -> ModelResponse:
        params = info.model_request_parameters
        seen["output_mode"] = params.output_mode
        seen["output_tools"] = [tool.name for tool in params.output_tools]
        seen["allow_text_output"] = params.allow_text_output
        return ModelResponse(parts=[TextPart(json.dumps(PAYLOADS[AdvisorVerdict]))])

    model = FunctionModel(handler, model_name="test-model")
    provider._model = lambda vision=False: model  # type: ignore[method-assign]

    result = asyncio.run(provider.advise_turn([], b"\x89PNG-fake", "payload"))

    assert result is not None
    assert result.verdict.recommended == [0]
    assert seen["output_mode"] == "prompted"
    assert seen["output_tools"] == []
    assert seen["allow_text_output"] is True
