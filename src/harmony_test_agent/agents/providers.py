from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import Settings
from ..models import (
    PlannedStep,
    PlanResult,
    ScreenSnapshot,
    TargetAppProfile,
    ToolDecision,
    ToolName,
    VisionObservation,
)

PLANNING_PROMPT = """You are an OpenHarmony UI test planner. Convert the user's task into no more than 20
atomic steps. Only use these tools: inspect_screen, open_app, click_element, click_coordinate, input_text, swipe,
back, wait, assert_visible, assert_not_visible, assert_text, finish. Prefer semantic element targets over coordinates.
Never plan login, payment, captcha, deletion, permission grant, or arbitrary shell commands. Include explicit assertions
and end with finish.
"""

VISION_PROMPT = """Analyze this OpenHarmony screenshot. Return a concise page title and summary plus actionable
elements. For every visual-only element provide pixel bbox coordinates relative to the supplied image and a confidence
score. Do not invent elements. UI hierarchy elements are supplied separately and remain higher priority than visual
detections.
"""

DECISION_PROMPT = """Choose exactly one allowed tool for the current planned step. Prefer the planned tool unless the
current screenshot proves it inappropriate. Prefer element targets from the UI hierarchy. Never emit shell commands,
multiple actions, login, payment, captcha, deletion, or permission-grant actions.
"""


class AgentProvider(ABC):
    name: str
    mock: bool = False

    @abstractmethod
    async def plan(self, task: str, profile: TargetAppProfile, max_steps: int) -> PlanResult: ...

    @abstractmethod
    async def analyze(self, snapshot: ScreenSnapshot) -> VisionObservation | None: ...

    @abstractmethod
    async def decide(self, step: PlannedStep, snapshot: ScreenSnapshot | None) -> ToolDecision: ...


class MockAgentProvider(AgentProvider):
    name = "mock"
    mock = True

    async def plan(self, task: str, profile: TargetAppProfile, max_steps: int) -> PlanResult:
        steps: list[PlannedStep] = [
            PlannedStep(step_id="step-01", instruction=f"启动{profile.display_name}", tool=ToolName.OPEN_APP),
            PlannedStep(step_id="step-02", instruction="检查首页", tool=ToolName.INSPECT_SCREEN),
        ]
        lowered = task.casefold()
        if "搜索" in task or "search" in lowered or "openharmony" in lowered:
            steps.extend(
                [
                    PlannedStep(
                        step_id="step-03", instruction="点击搜索入口", tool=ToolName.CLICK_ELEMENT, target="搜索"
                    ),
                    PlannedStep(
                        step_id="step-04",
                        instruction="在搜索输入框输入 OpenHarmony",
                        tool=ToolName.INPUT_TEXT,
                        target="输入框",
                        text="OpenHarmony",
                        expected="OpenHarmony",
                    ),
                    PlannedStep(
                        step_id="step-05",
                        instruction="确认搜索输入状态",
                        tool=ToolName.ASSERT_TEXT,
                        target="OpenHarmony",
                    ),
                    PlannedStep(step_id="step-06", instruction="关闭输入法或退出搜索结果", tool=ToolName.BACK),
                    PlannedStep(step_id="step-07", instruction="返回首页", tool=ToolName.BACK),
                    PlannedStep(
                        step_id="step-08",
                        instruction="确认已返回首页",
                        tool=ToolName.ASSERT_VISIBLE,
                        target="p2_home_titlebar_search",
                    ),
                ]
            )
        if "详情" in task or "内容" in task or "detail" in lowered:
            steps.extend(
                [
                    PlannedStep(
                        step_id="step-09",
                        instruction="打开一条内容详情",
                        tool=ToolName.CLICK_ELEMENT,
                        target="详情",
                    ),
                    PlannedStep(
                        step_id="step-10",
                        instruction="确认详情存在可见内容",
                        tool=ToolName.ASSERT_VISIBLE,
                        target="内容",
                    ),
                    PlannedStep(step_id="step-11", instruction="返回首页", tool=ToolName.BACK),
                ]
            )
        steps.append(PlannedStep(step_id="step-99", instruction="结束任务", tool=ToolName.FINISH))
        return PlanResult(goal=task, steps=steps[:max_steps], model_used=self.name, mock=True)

    async def analyze(self, snapshot: ScreenSnapshot) -> VisionObservation | None:
        return None

    async def decide(self, step: PlannedStep, snapshot: ScreenSnapshot | None) -> ToolDecision:
        tool = step.tool
        if tool == ToolName.BACK and "返回首页" in step.instruction and snapshot:
            already_home = any(item.key == "p2_home_titlebar_search" for item in snapshot.elements)
            if already_home:
                tool = ToolName.INSPECT_SCREEN
        return ToolDecision(
            tool=tool,
            target=step.target,
            text=step.text,
            coordinate=step.coordinate,
            direction=step.direction,
            wait_seconds=step.wait_seconds,
            reasoning="deterministic mock decision",
        )


class OpenAICompatibleProvider(AgentProvider):
    mock = False

    def __init__(self, settings: Settings):
        if not settings.openai_api_key or not settings.agent_model:
            raise ValueError("OPENAI_API_KEY and AGENT_MODEL are required for the OpenAI provider")
        self.settings = settings
        self.name = settings.agent_model

    def _model(self, vision: bool = False):
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        configured_name = self.settings.agent_vision_model if vision else self.settings.agent_model
        model_name = configured_name or self.settings.agent_model
        provider = OpenAIProvider(
            base_url=self.settings.openai_base_url,
            api_key=self.settings.openai_api_key.get_secret_value(),
        )
        return OpenAIChatModel(model_name, provider=provider)

    async def plan(self, task: str, profile: TargetAppProfile, max_steps: int) -> PlanResult:
        from pydantic_ai import Agent

        agent = Agent(self._model(), output_type=PlanResult, system_prompt=PLANNING_PROMPT, retries=2)
        prompt = (
            f"Target app: {profile.display_name} ({profile.bundle_name})\n"
            f"Maximum steps: {max_steps}\nUser task: {task}\n"
            "Set model_used to the configured model name and mock to false."
        )
        result = await agent.run(prompt)
        plan = result.output
        plan.model_used = self.name
        plan.mock = False
        plan.steps = plan.steps[:max_steps]
        if not plan.steps or plan.steps[-1].tool != ToolName.FINISH:
            plan.steps.append(PlannedStep(step_id="finish", instruction="结束任务", tool=ToolName.FINISH))
        return plan

    async def analyze(self, snapshot: ScreenSnapshot) -> VisionObservation | None:
        from pydantic_ai import Agent, BinaryContent

        agent = Agent(self._model(vision=True), output_type=VisionObservation, system_prompt=VISION_PROMPT, retries=2)
        hierarchy = [
            {
                "element_id": item.element_id,
                "content": item.content,
                "type": item.type,
                "bbox": item.bbox.model_dump() if item.bbox else None,
                "clickable": item.clickable,
                "editable": item.editable,
            }
            for item in snapshot.elements[:120]
        ]
        prompt = f"Image size: {snapshot.width}x{snapshot.height}. Existing UI hierarchy: {hierarchy}"
        result = await agent.run(
            [
                prompt,
                BinaryContent(data=snapshot.image_path.read_bytes(), media_type="image/png"),
            ]
        )
        return result.output

    async def decide(self, step: PlannedStep, snapshot: ScreenSnapshot | None) -> ToolDecision:
        from pydantic_ai import Agent, BinaryContent

        agent = Agent(self._model(vision=True), output_type=ToolDecision, system_prompt=DECISION_PROMPT, retries=2)
        if snapshot is None:
            result = await agent.run(f"Planned step: {step.model_dump_json()}")
            return result.output
        elements = [
            {
                "element_id": item.element_id,
                "content": item.content,
                "key": item.key,
                "id": item.id,
                "bbox": item.bbox.model_dump() if item.bbox else None,
                "clickable": item.clickable,
                "editable": item.editable,
            }
            for item in snapshot.elements[:120]
        ]
        result = await agent.run(
            [
                f"Planned step: {step.model_dump_json()}\nCurrent elements: {elements}",
                BinaryContent(data=snapshot.image_path.read_bytes(), media_type="image/png"),
            ]
        )
        return result.output


def create_provider(settings: Settings) -> AgentProvider:
    if settings.agent_provider == "mock":
        return MockAgentProvider()
    if settings.agent_provider == "openai":
        return OpenAICompatibleProvider(settings)
    return OpenAICompatibleProvider(settings) if settings.model_configured else MockAgentProvider()
