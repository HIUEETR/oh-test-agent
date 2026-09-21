"""Phase 5.1 回归：每步的两次视觉调用合并为一次（G3 的主要来源）。

原实现每步调用 ``provider.analyze``（``_capture``）与 ``provider.decide``（``_decide_and_execute``）
各一次，实测任务期 15 次 capture / 14 个动作 ≈ 2 次视觉调用/步、≈24 s/次。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeLiveDevice, FixedPlanProvider
from pydantic_ai import PromptedOutput
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from harmony_test_agent.agents import AgentOrchestrator
from harmony_test_agent.agents.providers import (
    OBSERVE_AND_DECIDE_PROMPT,
    AgentProvider,
    ObservationAndDecision,
    OpenAICompatibleProvider,
    PlanningContext,
    render_decision_history,
)
from harmony_test_agent.config import Settings
from harmony_test_agent.models import (
    EventType,
    ExplorationPolicy,
    PlannedStep,
    PlanResult,
    RunRequest,
    ScreenSnapshot,
    StepHistoryEntry,
    TargetQuery,
    ToolDecision,
    ToolName,
    VisionObservation,
)
from harmony_test_agent.storage import ArtifactStore, RunRepository

STEPS = [
    PlannedStep(step_id="step-01", instruction="启动日历", tool=ToolName.OPEN_APP),
    PlannedStep(step_id="step-02", instruction="切换到月视图", tool=ToolName.CLICK_ELEMENT, target="tabs_month"),
    PlannedStep(step_id="step-03", instruction="结束任务", tool=ToolName.FINISH),
]


class SpyProvider(FixedPlanProvider):
    """非 mock 的计数 provider：区分 analyze / decide / 合并三条路径。"""

    name = "spy"
    mock = False

    def __init__(self, steps: list[PlannedStep]) -> None:
        super().__init__(steps)
        self.analyze_calls = 0
        self.decide_calls = 0
        self.combined_calls = 0
        self.histories: list[list[StepHistoryEntry]] = []

    async def analyze(self, snapshot: ScreenSnapshot) -> VisionObservation | None:
        self.analyze_calls += 1
        return None

    def supports_combined_observation(self) -> bool:
        return True

    async def decide(
        self,
        step: PlannedStep,
        snapshot: ScreenSnapshot | None,
        feedback: str | None = None,
    ) -> ToolDecision:
        self.decide_calls += 1
        return await super().decide(step, snapshot, feedback)

    async def observe_and_decide(
        self,
        step: PlannedStep,
        snapshot: ScreenSnapshot | None,
        *,
        feedback: str | None = None,
        history: list[StepHistoryEntry] | None = None,
    ) -> tuple[VisionObservation | None, ToolDecision]:
        self.combined_calls += 1
        self.histories.append(list(history or []))
        decision = await FixedPlanProvider.decide(self, step, snapshot, feedback)
        return VisionObservation(page_title="日历", summary=f"step {step.step_id}", elements=[]), decision


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        agent_provider="mock",
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "home",
        target_profile_path=None,
        exploration_policy=None,
        **overrides,
    )


async def _run(tmp_path: Path, provider: AgentProvider, **overrides: Any):
    settings = _settings(tmp_path, **overrides)
    device = FakeLiveDevice()
    orchestrator = AgentOrchestrator(
        settings,
        provider=provider,
        repository=RunRepository(settings.resolved_database_path),
        artifacts=ArtifactStore(settings.resolved_runtime_dir),
        device_factory=lambda _: device,
        settle_seconds=0,
        launch_settle_seconds=0,
    )
    return await orchestrator.run(
        RunRequest(
            target=TargetQuery(app_name="日历"),
            task="打开日历并切换到月视图",
            auto_generate=False,
            exploration_policy=ExplorationPolicy(enabled=False, settle_timeout_seconds=0),
        )
    )


async def test_merged_vision_uses_one_call_per_step(tmp_path: Path) -> None:
    provider = SpyProvider(STEPS)

    trace = await _run(tmp_path, provider, merge_observe_and_decide=True)

    assert trace.state.value == "completed", trace.error
    # 每步恰好一次合并请求；不再出现单独的 analyze / decide 往返。
    assert provider.combined_calls == len(trace.plan)
    assert provider.analyze_calls == 0
    assert provider.decide_calls == 0
    captured = [event for event in trace.events if event.type == EventType.SCREEN_CAPTURED]
    detected = [event for event in trace.events if event.type == EventType.ELEMENTS_DETECTED]
    # 复用的帧不重复发 SCREEN_CAPTURED，只补发一条带摘要的 ELEMENTS_DETECTED（前端按 snapshot_id 回填）。
    assert len(captured) == 4
    assert len(detected) >= len(captured)
    assert len(captured) == len({event.payload["snapshot_id"] for event in captured})
    assert any("step " in str(event.payload.get("summary", "")) for event in detected)


async def test_merged_vision_disabled_keeps_two_calls_per_step(tmp_path: Path) -> None:
    provider = SpyProvider(STEPS)
    provider.supports_combined_observation = lambda: False  # type: ignore[method-assign]

    trace = await _run(tmp_path, provider, merge_observe_and_decide=False)

    assert trace.state.value == "completed", trace.error
    assert provider.combined_calls == 0
    assert provider.analyze_calls >= len(trace.plan)
    assert provider.decide_calls >= 1


async def test_decision_history_is_carried_into_later_steps(tmp_path: Path) -> None:
    provider = SpyProvider(STEPS)

    await _run(tmp_path, provider, live_decision_history_steps=4)

    assert provider.histories
    # 第一步没有历史；后续步骤带上此前执行过的动作摘要。
    assert provider.histories[0] == []
    assert provider.histories[-1], "后续步骤必须携带跨步历史"
    assert provider.histories[-1][-1].tool == str(ToolName.CLICK_ELEMENT)
    assert provider.histories[-1][-1].ok is True


async def test_decision_history_can_be_disabled(tmp_path: Path) -> None:
    provider = SpyProvider(STEPS)

    await _run(tmp_path, provider, live_decision_history_steps=0)

    assert all(history == [] for history in provider.histories)


def test_render_decision_history_is_compact() -> None:
    rendered = render_decision_history(
        [
            StepHistoryEntry(index=1, instruction="启动", tool="open_app", ok=True, page_path="pages/EntryPage"),
            StepHistoryEntry(index=2, tool="click_element", target="tabs_month", ok=False, note="not found"),
        ]
    )

    assert rendered.splitlines()[0].startswith("Previously executed steps")
    assert "#2 click_element target='tabs_month' -> failed" in rendered
    assert "note=not found" in rendered
    assert render_decision_history([]) == ""


# ---------------------------------------------------------------------------
# OpenAI 兼容 provider：单次结构化输出 + 失败回退
# ---------------------------------------------------------------------------


def _provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        Settings(
            _env_file=None,
            openai_api_key="test-key",
            agent_model="test-model",
            agent_vision_model="test-vision-model",
            agent_provider="openai",
        )
    )


@pytest.fixture
def snapshot(tmp_path: Path) -> ScreenSnapshot:
    from PIL import Image

    image_path = tmp_path / "snap.png"
    Image.new("RGB", (100, 200), "navy").save(image_path)
    return ScreenSnapshot(
        snapshot_id="snap-merged",
        run_id="run-merged",
        image_path=image_path.resolve(),
        image_sha256="hash",
        width=100,
        height=200,
        page_path="/page/0",
        elements=[],
    )


def test_observe_and_decide_uses_single_prompted_output(monkeypatch, snapshot: ScreenSnapshot) -> None:
    import pydantic_ai

    recorded: list[Any] = []
    requested: list[type] = []
    runs: list[int] = []
    payload = {
        "page_title": "日历",
        "summary": "月视图",
        "elements": [],
        "decision": {"tool": ToolName.CLICK_ELEMENT.value, "target": "tabs_month"},
    }
    _spy_structured_output(monkeypatch, requested)

    class _RecordingAgent:
        def __init__(self, model: object, **kwargs: Any) -> None:
            recorded.append(kwargs.get("output_type"))

        async def run(self, *args: Any, **kwargs: Any) -> Any:
            runs.append(1)
            assert requested[-1] is ObservationAndDecision
            return type("R", (), {"output": ObservationAndDecision.model_validate(payload)})()

    monkeypatch.setattr(pydantic_ai, "Agent", _RecordingAgent)

    observation, decision = asyncio.run(
        _provider().observe_and_decide(
            STEPS[1], snapshot, history=[StepHistoryEntry(index=1, tool="open_app", ok=True)]
        )
    )

    assert len(runs) == 1
    assert requested == [ObservationAndDecision]
    assert isinstance(recorded[0], PromptedOutput)
    assert "one JSON object" in OBSERVE_AND_DECIDE_PROMPT
    assert observation is not None and observation.summary == "月视图"
    assert decision.tool is ToolName.CLICK_ELEMENT


def _spy_structured_output(monkeypatch, requested: list[type]) -> None:
    """记录每条结构化输出路径请求的模型类型，便于断言「只发了一次组合请求」。"""
    real = OpenAICompatibleProvider._structured_output

    def spy(output_type: type, description: str) -> Any:
        requested.append(output_type)
        return real(output_type, description)

    monkeypatch.setattr(OpenAICompatibleProvider, "_structured_output", staticmethod(spy))


def test_observe_and_decide_falls_back_to_two_calls(monkeypatch, snapshot: ScreenSnapshot) -> None:
    """组合模型输出不可用时必须回退为既有两次调用，而不是让整步失败。"""
    import pydantic_ai

    requested: list[type] = []
    runs: list[str] = []
    _spy_structured_output(monkeypatch, requested)

    class _FlakyAgent:
        def __init__(self, model: object, **kwargs: Any) -> None:
            pass

        async def run(self, *args: Any, **kwargs: Any) -> Any:
            target = requested[-1]
            runs.append(target.__name__)
            if target is ObservationAndDecision:
                raise RuntimeError("combined output rejected")
            if target is VisionObservation:
                return type("R", (), {"output": VisionObservation(page_title="日历", summary="回退", elements=[])})()
            return type("R", (), {"output": ToolDecision(tool=ToolName.CLICK_ELEMENT, target="tabs_month")})()

    monkeypatch.setattr(pydantic_ai, "Agent", _FlakyAgent)

    observation, decision = asyncio.run(_provider().observe_and_decide(STEPS[1], snapshot))

    assert runs == ["ObservationAndDecision", "VisionObservation", "ToolDecision"]
    assert observation is not None and observation.summary == "回退"
    assert decision.tool is ToolName.CLICK_ELEMENT


def test_base_provider_observe_and_decide_is_sequential(snapshot: ScreenSnapshot) -> None:
    """基类默认实现保持「先 analyze 再 decide」，向后兼容自定义 provider。"""

    class _Sequential(FixedPlanProvider):
        name = "seq"
        mock = False

        def __init__(self) -> None:
            super().__init__(STEPS)
            self.calls: list[str] = []

        async def analyze(self, snapshot: ScreenSnapshot) -> VisionObservation | None:
            self.calls.append("analyze")
            return VisionObservation(summary="seq")

        async def decide(self, step, snapshot, feedback=None):
            self.calls.append("decide")
            return await super().decide(step, snapshot, feedback)

    provider = _Sequential()
    observation, decision = asyncio.run(AgentProvider.observe_and_decide(provider, STEPS[0], snapshot))

    assert provider.calls == ["analyze", "decide"]
    assert observation is not None and observation.summary == "seq"
    assert decision.tool is ToolName.OPEN_APP


def test_vision_prompt_materializes_json_handler(snapshot: ScreenSnapshot) -> None:
    """机制层不变量：组合路径同样走 prompted 模式（不注册输出工具）。"""
    provider = _provider()
    seen: dict[str, Any] = {}

    def handler(messages: list[Any], info: AgentInfo) -> ModelResponse:
        params = info.model_request_parameters
        seen["output_mode"] = params.output_mode
        seen["output_tools"] = [tool.name for tool in params.output_tools]
        return ModelResponse(
            parts=[
                TextPart(
                    '{"page_title": "日历", "summary": "s", "elements": [], '
                    '"decision": {"tool": "click_element", "target": "tabs_month"}}'
                )
            ]
        )

    provider._model = lambda vision=False: FunctionModel(handler, model_name="test-model")  # type: ignore[method-assign]

    observation, decision = asyncio.run(provider.observe_and_decide(STEPS[1], snapshot))

    assert seen["output_mode"] == "prompted"
    assert seen["output_tools"] == []
    assert decision.tool is ToolName.CLICK_ELEMENT
    assert observation is not None and observation.summary == "s"


def test_plan_result_contract_unchanged() -> None:
    """additive 字段不得改变既有 PlanResult/ToolDecision 反序列化行为。"""
    plan = PlanResult(goal="g", steps=STEPS, model_used="m")
    assert plan.steps[0].start is None and plan.steps[0].steps is None
    decision = ToolDecision(tool=ToolName.SWIPE, direction="up", steps=4, distance=400)
    assert decision.steps == 4 and decision.distance == 400
    assert ToolDecision(tool=ToolName.SWIPE).start is None
    assert PlanningContext  # 保持 import 被使用（规划上下文未受本次改动影响）
