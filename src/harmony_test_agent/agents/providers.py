"""定义代理模型提供方接口，并实现 Mock 与 OpenAI 兼容提供方。"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

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



class PlanningContext(BaseModel):
    """Minimal immutable application context exposed to the planner."""

    target_app_id: str
    display_name: str
    bundle_name: str
    main_ability: str
    stable_locator_names: list[str] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)

    @classmethod
    def from_profile(cls, profile: TargetAppProfile) -> "PlanningContext":
        return cls(
            target_app_id=profile.target_app_id,
            display_name=profile.display_name,
            bundle_name=profile.bundle_name,
            main_ability=profile.main_ability,
            stable_locator_names=[item.name for item in profile.stable_locator_inventory],
            known_limitations=list(profile.known_limitations),
        )


PLANNING_PROMPT = """You are an OpenHarmony UI test planner. Convert the user's task into no more than 20
atomic steps. Only use these tools: inspect_screen, open_app, click_element, click_coordinate, input_text, swipe,
back, wait, assert_visible, assert_not_visible, assert_text, finish. Prefer semantic element targets over coordinates.
Follow the requested behavior exactly: entering text does not imply submitting a search, and you must not add a search
submission or search-result assertion unless the user explicitly asks for it. When navigating away after text input,
account for the soft keyboard: one back may dismiss the keyboard before another back changes the app page. Never plan
login, payment, captcha, deletion, permission grant, or arbitrary shell commands. Include explicit assertions and end
with finish.
"""

VISION_PROMPT = """Analyze this OpenHarmony screenshot. Return a concise page title and summary plus actionable
elements. Include visible actionable controls that are absent from the UI hierarchy, especially soft-keyboard action
keys and icon-only controls. For every visual-only element provide pixel bbox coordinates relative to the supplied
image and a confidence score. Do not invent elements. UI hierarchy elements are supplied separately and remain higher
priority than visual detections.
"""

DECISION_PROMPT = """Choose exactly one allowed tool for the current planned step. Prefer the planned tool unless the
current screenshot proves it inappropriate. For click_element and input_text, use the exact element_id from Current
elements as target; do not return a descriptive label when an exact element_id exists. If a visible control is absent
from Current elements but is unambiguous in the screenshot, use click_coordinate with pixel coordinates relative to
the supplied image. Do not guess that a hierarchy element represents a visual control when its content or bbox does
not support that conclusion. If a back step intends to navigate while a soft keyboard is visible, prefer the visible
in-app back control because a system back may only dismiss the keyboard. If the desired destination is already visible,
use inspect_screen instead of navigating away. Never emit shell commands, multiple actions, login, payment, captcha,
deletion, or permission-grant actions. Never choose finish unless the planned step tool is finish.
"""


_SEARCH_SUBMISSION_REQUESTS = (
    "提交搜索",
    "执行搜索",
    "发起搜索",
    "查看搜索结果",
    "打开搜索结果",
    "确认搜索结果",
    "search results",
    "submit search",
)
_SEARCH_FLOW_MARKERS = (
    "提交搜索",
    "执行搜索",
    "发起搜索",
    "搜索按钮",
    "搜索键",
    "搜索结果",
    "submit search",
    "search results",
)
_HOME_RETURN_MARKERS = ("返回首页", "回到首页", "return home")


def _step_text(step: PlannedStep) -> str:
    return " ".join(value for value in (step.instruction, step.target, step.text, step.expected) if value).casefold()


def _search_submission_requested(task_text: str) -> bool:
    return any(marker in task_text for marker in _SEARCH_SUBMISSION_REQUESTS) or bool(
        re.search(r"(?:搜索|查找)\s+[a-z0-9]", task_text)
    )


def align_plan_with_task(task: str, steps: list[PlannedStep], max_steps: int) -> list[PlannedStep]:
    """Remove invented search submission and make keyboard-aware return steps deterministic."""
    task_text = task.casefold()
    submission_requested = _search_submission_requested(task_text)
    aligned: list[PlannedStep] = []
    after_input = False
    dropping_search_flow = False

    for step in steps:
        text = _step_text(step)
        if step.tool == ToolName.INPUT_TEXT:
            after_input = True
            dropping_search_flow = False
            aligned.append(step)
            continue
        if after_input and not submission_requested and any(marker in text for marker in _SEARCH_FLOW_MARKERS):
            dropping_search_flow = True
            continue
        if after_input and dropping_search_flow and step.tool == ToolName.WAIT:
            continue
        if step.tool == ToolName.BACK:
            dropping_search_flow = False
        aligned.append(step)

    keyboard_aware: list[PlannedStep] = []
    after_input = False
    for step in aligned:
        text = _step_text(step)
        if step.tool == ToolName.INPUT_TEXT:
            after_input = True
        returning_home_after_input = (
            after_input and step.tool == ToolName.BACK and any(marker in text for marker in _HOME_RETURN_MARKERS)
        )
        previous_step_dismisses_keyboard = (
            keyboard_aware and keyboard_aware[-1].tool == ToolName.BACK and "键盘" in _step_text(keyboard_aware[-1])
        )
        if returning_home_after_input and not previous_step_dismisses_keyboard:
            keyboard_aware.append(
                PlannedStep(
                    step_id=f"{step.step_id}-keyboard",
                    instruction="关闭软键盘（若未显示则保持当前页面）",
                    tool=ToolName.BACK,
                )
            )
        if returning_home_after_input:
            after_input = False
        keyboard_aware.append(step)

    finish = next((step for step in reversed(keyboard_aware) if step.tool == ToolName.FINISH), None)
    finish = finish or PlannedStep(step_id="finish", instruction="结束任务", tool=ToolName.FINISH)
    body = [step for step in keyboard_aware if step.tool != ToolName.FINISH]
    return [*body[: max(0, max_steps - 1)], finish]


class AgentProvider(ABC):
    """规划、视觉分析与动作决策所遵循的异步提供方协议。"""

    name: str
    mock: bool = False

    @abstractmethod
    async def plan(self, task: str, profile: TargetAppProfile, max_steps: int) -> PlanResult:
        """将用户任务规划为不超过上限的原子步骤。"""
        ...

    @abstractmethod
    async def analyze(self, snapshot: ScreenSnapshot) -> VisionObservation | None:
        """分析屏幕快照并返回可选的视觉观察结果。"""
        ...

    @abstractmethod
    async def decide(self, step: PlannedStep, snapshot: ScreenSnapshot | None) -> ToolDecision:
        """结合计划步骤和当前快照选择一个受支持的工具动作。"""
        ...


class MockAgentProvider(AgentProvider):
    """提供确定性离线计划和决策，用于不调用真实模型的开发流程。"""

    name = "mock"
    mock = True

    async def plan(self, task: str, profile: TargetAppProfile, max_steps: int) -> PlanResult:
        """将用户任务规划为不超过上限的原子步骤。"""
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
        """分析屏幕快照并返回可选的视觉观察结果。"""
        return None

    async def decide(self, step: PlannedStep, snapshot: ScreenSnapshot | None) -> ToolDecision:
        """结合计划步骤和当前快照选择一个受支持的工具动作。"""
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
    """通过 OpenAI 兼容接口完成规划、视觉分析和动作决策。"""

    mock = False

    def __init__(self, settings: Settings):
        if not settings.openai_api_key or not settings.agent_model:
            raise ValueError("OPENAI_API_KEY and AGENT_MODEL are required for the OpenAI provider")
        self.settings = settings
        self.name = settings.agent_model

    def _model_settings(self) -> dict[str, object] | None:
        if not self.settings.agent_disable_thinking:
            return None
        return {"extra_body": {"thinking": {"type": "disabled"}}}

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
        """将用户任务规划为不超过上限的原子步骤。"""
        from pydantic_ai import Agent

        agent = Agent(self._model(), output_type=PlanResult, system_prompt=PLANNING_PROMPT, retries=2)
        context = PlanningContext.from_profile(profile)
        prompt = (
            f"Planning context: {context.model_dump_json()}\n"
            f"Maximum steps: {max_steps}\nUser task: {task}\n"
            "Set model_used to the configured model name and mock to false."
        )
        result = await agent.run(prompt, model_settings=self._model_settings())
        plan = result.output
        plan.model_used = self.name
        plan.mock = False
        plan.steps = align_plan_with_task(task, plan.steps, max_steps)
        return plan

    async def analyze(self, snapshot: ScreenSnapshot) -> VisionObservation | None:
        """分析屏幕快照并返回可选的视觉观察结果。"""
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
            ],
            model_settings=self._model_settings(),
        )
        return result.output

    async def decide(self, step: PlannedStep, snapshot: ScreenSnapshot | None) -> ToolDecision:
        """结合计划步骤和当前快照选择一个受支持的工具动作。"""
        from pydantic_ai import Agent, BinaryContent, PromptedOutput

        agent = Agent(
            self._model(vision=True),
            output_type=PromptedOutput(
                ToolDecision,
                description="Return exactly one allowed UI tool decision as a JSON object.",
            ),
            system_prompt=DECISION_PROMPT,
            retries=2,
        )
        if snapshot is None:
            result = await agent.run(f"Planned step: {step.model_dump_json()}", model_settings=self._model_settings())
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
            ],
            model_settings=self._model_settings(),
        )
        return result.output


def create_provider(settings: Settings) -> AgentProvider:
    """根据配置选择 Mock 或 OpenAI 兼容提供方，并拒绝缺少必要配置的模式。"""
    if settings.agent_provider == "mock":
        return MockAgentProvider()
    if settings.agent_provider == "openai":
        return OpenAICompatibleProvider(settings)
    return OpenAICompatibleProvider(settings) if settings.model_configured else MockAgentProvider()