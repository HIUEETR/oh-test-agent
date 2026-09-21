"""定义代理模型提供方接口，并实现 Mock 与 OpenAI 兼容提供方。"""

from __future__ import annotations

import io
import logging
import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from ..cases.bug_repro import BUG_REPRO_PROMPT, BugReproPlan, BugReproRequest, mock_bug_repro_plan
from ..config import Settings
from ..discovery.advisor import ADVISOR_PROMPT, AdvisorTurnResult, AdvisorVerdict
from ..models import (
    PlannedStep,
    PlanResult,
    ScreenSnapshot,
    StepHistoryEntry,
    TargetAppProfile,
    ToolDecision,
    ToolName,
    VisionElement,
    VisionObservation,
)
from ..targets import ResolvedTarget

if TYPE_CHECKING:
    from openai.types import chat

logger = logging.getLogger(__name__)


class PlanningContext(BaseModel):
    """Minimal immutable application context exposed to the planner."""

    target_app_id: str
    display_name: str
    bundle_name: str
    main_ability: str
    stable_locator_names: list[str] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)

    @classmethod
    def from_profile(cls, profile: TargetAppProfile) -> PlanningContext:
        return cls(
            target_app_id=profile.target_app_id,
            display_name=profile.display_name,
            bundle_name=profile.bundle_name,
            main_ability=profile.main_ability,
            stable_locator_names=[item.name for item in profile.stable_locator_inventory],
            known_limitations=list(profile.known_limitations),
        )

    @classmethod
    def from_resolved(cls, resolved: ResolvedTarget) -> PlanningContext:
        """无 Profile 实时模式的规划上下文：只有解析出的应用身份。"""
        return cls(
            target_app_id=resolved.target_app_id,
            display_name=resolved.display_name,
            bundle_name=resolved.bundle_name,
            main_ability=resolved.main_ability,
        )


PLANNING_PROMPT = """You are an OpenHarmony UI test planner. Convert the user's task into no more than 20
atomic steps. Only use these tools: inspect_screen, open_app, click_element, click_coordinate, input_text, swipe,
back, wait, assert_visible, assert_not_visible, assert_text, finish. Prefer semantic element targets over coordinates.
Plan the shortest sufficient sequence: the executor re-observes the screen after every step, so a separate
inspect_screen step is only warranted to establish the starting screen or to check a state that no previous step
produced. Never insert inspect_screen between consecutive actions, and do not plan two consecutive inspect_screen
steps. Follow the requested behavior exactly: entering text does not imply submitting a search, and you must not add
a search submission or search-result assertion unless the user explicitly asks for it. When navigating away after
text input,
account for the soft keyboard: one back may dismiss the keyboard before another back changes the app page. In a form,
the on-screen keyboard covers the lower rows: plan a back step to dismiss it before operating any control that sits
below the text input, because covered rows collapse to a few pixels and cannot be clicked (real case: the calendar
reminder row shrinks to 5px while the keyboard is up, so a click aimed at it lands on the start-time row instead).
Never plan
login, payment, captcha, deletion, permission grant, or arbitrary shell commands. Include explicit assertions and end
with finish. The expected text of assert_text must be copied verbatim from text you actually observed on screen in the
most recent observation; never invent a display format from the task wording (a task that says "1pm" may render as
"01:00 PM"). If you cannot be sure of the exact text, use assert_visible with a locator instead.
When a step must CHANGE a value shown on a wheel or scroll picker (hour, minute, AM/PM, date or duration
columns), plan that step as swipe with the column as target and state the required final value in the
instruction; never plan a click on an individual picker row, and never plan a blind click_coordinate for a
picker value. Use click_element only to open the picker.
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
the supplied image. For wheel pickers (hour/minute/AM-PM columns), set swipe target to the column's element_id to
swipe inside that column instead of at the screen center. When operating a wheel or time picker: first read the y
coordinates of two adjacent visible rows to derive the row pitch, then compute how many rows you must move, choose the
shorter direction, and express it with steps (or explicit start/end coordinates); never click a wheel row with blind
coordinates. This overrides the planned tool: when the planned step asks you to change a value that is displayed in a
wheel or time-picker column (including when the plan says click_element or click_coordinate), answer with swipe on that
column and the computed steps instead of a picker-row click. When recovery feedback is provided, the previous attempt at
this step failed: you may first take corrective actions (for example an anchored swipe inside a wheel column or
clicking another control) and re-attempt the planned goal, including re-issuing its assertion once the state matches.
Do not guess that a hierarchy element represents a visual
control when its content or bbox does not support that conclusion. When the planned control's bbox is collapsed (a few
pixels tall) or missing while a soft keyboard is visible, the keyboard is covering it: dismiss the keyboard with back
first and then interact with the row, instead of clicking a nearby visible row. If the planned target is present in
Current
elements, always answer with click_element and its element_id — even when that element is not marked clickable, because
the runtime clicks its bounds centre (and the enclosing row) anyway. Never replace a present target with a guessed
click_coordinate: a coordinate guess lands on whatever happens to be at that point, which in a form with several
similar rows means changing the wrong field. Reserve click_coordinate for controls that are genuinely absent from
Current elements. If a back step intends to navigate while a soft
keyboard is visible, prefer the visible
in-app back control because a system back may only dismiss the keyboard. If the desired destination is already visible,
use inspect_screen instead of navigating away. Never emit shell commands, multiple actions, login, payment, captcha,
deletion, or permission-grant actions. Never choose finish unless the planned step tool is finish.
If a control you click produces no visible change in the page structure, do NOT silently retry it or work around it
with a different control: that may be a real defect in the application under test. State explicitly in your reasoning
"疑似无响应控件：<target>" and try it once more; if the structure still does not change, treat the step as failed
instead of hiding it behind a workaround.
"""

# 合并观测里的摘要长度上限：摘要只用于前端思考流与恢复提示，长摘要纯属输出 token 浪费。
_OBSERVATION_SUMMARY_LIMIT = 400

OBSERVE_AND_DECIDE_PROMPT = (
    VISION_PROMPT
    + "\n"
    + DECISION_PROMPT
    + """
Return one JSON object containing BOTH the page observation (page_title, summary, elements) and the single tool
decision (decision). The observation must describe the screenshot you are looking at; the decision must be valid for
the planned step against that same screenshot.
Keep the observation compact: the UI hierarchy is already supplied as Current elements, so return at most 5 entries in
`elements` and only for visual-only controls that are absent from Current elements (for example soft-keyboard keys).
Never echo hierarchy elements back. `summary` must be at most two short sentences stating what matters for the planned
step; do not enumerate the screen.
"""
)


class ObservationAndDecision(BaseModel):
    """一次视觉请求同时返回页面观测与工具决策（计划 5.1，把每步两次调用压成一次）。"""

    page_title: str = ""
    summary: str = ""
    elements: list[VisionElement] = Field(default_factory=list)
    decision: ToolDecision

    def observation(self) -> VisionObservation:
        """把组合模型里的观测部分还原为 ``VisionObservation``。"""
        return VisionObservation(page_title=self.page_title, summary=self.summary, elements=list(self.elements))


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


def render_decision_history(history: list[StepHistoryEntry] | None) -> str:
    """把跨步历史渲染为紧凑提示（计划 5.4，格式借鉴 DC 的 ``DcContinuationContext.to_prompt``）。"""
    if not history:
        return ""
    lines = ["Previously executed steps on this run (oldest first):"]
    for item in history:
        outcome = "ok" if item.ok else "failed"
        detail = f" target={item.target!r}" if item.target else ""
        page = f" page={item.page_path}" if item.page_path else ""
        note = f" note={item.note}" if item.note else ""
        lines.append(f"- #{item.index} {item.tool}{detail} -> {outcome}{page}{note}")
    return "\n".join(lines)


def _with_history(feedback: str | None, history: list[StepHistoryEntry] | None) -> str | None:
    """把历史与恢复反馈合并为单一 feedback 文本（历史在前，失败反馈在后）。"""
    rendered = render_decision_history(history)
    if not rendered:
        return feedback
    if not feedback:
        return rendered
    return f"{rendered}\nRecovery feedback: {feedback}"


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


def render_bug_repro_task(request: BugReproRequest) -> str:
    """把缺陷报告渲染成一段任务描述，供 :func:`align_plan_with_task` 判定「用户真正要什么」。"""
    lines = [
        f"缺陷标题：{request.title}",
        f"症状（{request.symptom_kind}）：{request.symptom}",
        f"期望行为：{request.expected}",
        f"实际行为：{request.actual}",
    ]
    if request.preconditions:
        lines.append("前置条件：" + "；".join(request.preconditions))
    lines.append("复现步骤：" + "；".join(request.repro_steps_nl))
    return "\n".join(lines)


class AgentProvider(ABC):
    """规划、视觉分析与动作决策所遵循的异步提供方协议。"""

    name: str
    mock: bool = False

    @abstractmethod
    async def plan(self, task: str, context: PlanningContext, max_steps: int) -> PlanResult:
        """将用户任务规划为不超过上限的原子步骤。"""
        ...

    @abstractmethod
    async def analyze(self, snapshot: ScreenSnapshot) -> VisionObservation | None:
        """分析屏幕快照并返回可选的视觉观察结果。"""
        ...

    @abstractmethod
    async def decide(
        self,
        step: PlannedStep,
        snapshot: ScreenSnapshot | None,
        feedback: str | None = None,
    ) -> ToolDecision:
        """结合计划步骤和当前快照选择一个受支持的工具动作；feedback 为恢复尝试的失败反馈。"""
        ...

    async def observe_and_decide(
        self,
        step: PlannedStep,
        snapshot: ScreenSnapshot | None,
        *,
        feedback: str | None = None,
        history: list[StepHistoryEntry] | None = None,
    ) -> tuple[VisionObservation | None, ToolDecision]:
        """一次拿到当前帧的观测与工具决策（计划 5.1）。

        基类默认实现是顺序调用 ``analyze`` + ``decide``，保持向后兼容；支持组合结构化输出的
        provider 覆写为**单次**模型请求，把 Live 每步两次视觉往返压成一次。
        """
        observation = await self.analyze(snapshot) if snapshot is not None else None
        decision = await self.decide(step, snapshot, feedback=_with_history(feedback, history))
        return observation, decision

    def supports_combined_observation(self) -> bool:
        """编排器是否应把每步路由到 :meth:`observe_and_decide`（计划 5.1）。

        默认 ``False``：只有真正实现了单次组合请求的 provider 才返回 True。这样自定义
        provider（以及未开启合并的配置）继续走既有的「观测 → 决策」两次调用路径，
        连带保留其各自的失败语义（例如视觉分析失败 = ``vision analysis failed``）。
        """
        return False

    async def advise_turn(
        self,
        history: list[Any],
        screenshot: bytes,
        payload: str,
    ) -> AdvisorTurnResult | None:
        """探索顾问单轮对话：把当前页追加进连续会话并返回结构化建议；默认不支持。"""
        return None

    async def plan_bug_repro(
        self,
        request: BugReproRequest,
        context: PlanningContext,
        max_steps: int,
    ) -> BugReproPlan | None:
        """把自然语言缺陷报告规划为可执行的复现计划；默认不支持（照 ``advise_turn`` 的模式）。"""
        return None

    async def chat(self, request: Any) -> Any | None:
        """DC 模式多轮工具对话；默认不支持。"""
        return None


class MockAgentProvider(AgentProvider):
    """提供确定性离线计划和决策，用于不调用真实模型的开发流程。"""

    name = "mock"
    mock = True

    async def plan(self, task: str, context: PlanningContext, max_steps: int) -> PlanResult:
        """将用户任务规划为不超过上限的原子步骤。"""
        steps: list[PlannedStep] = [
            PlannedStep(step_id="step-01", instruction=f"启动{context.display_name}", tool=ToolName.OPEN_APP),
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

    async def decide(
        self,
        step: PlannedStep,
        snapshot: ScreenSnapshot | None,
        feedback: str | None = None,
    ) -> ToolDecision:
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

    async def chat(self, request: Any) -> Any | None:
        """DC 模式 Mock 对话：返回固定单轮响应，不调用工具。"""
        from ..dc.models import DcChatResponse

        return DcChatResponse(
            output_text="Mock DC response: task acknowledged (no real model configured).",
            history=list(getattr(request, "history", []) or []),
            tool_call_count=0,
        )

    async def plan_bug_repro(
        self,
        request: BugReproRequest,
        context: PlanningContext,
        max_steps: int,
    ) -> BugReproPlan:
        """确定性、完全离线的缺陷复现计划：关键词翻译 + 期望行为断言 + 症状哨兵。"""
        return mock_bug_repro_plan(
            request,
            bundle_name=context.bundle_name,
            stable_locator_names=context.stable_locator_names,
            max_steps=max_steps,
            model_used=self.name,
        )


def usage_aware_chat_model_class() -> type[Any]:
    """返回「能正确读取 usage」的 ``OpenAIChatModel`` 子类（惰性定义并缓存）。

    pydantic-ai 体积大（模块级 import 约 4.6s），本模块刻意保持零 pydantic-ai
    顶层依赖，因此子类在首次真正需要模型时才定义。

    背景（实测，pydantic-ai 1.73.0）：自建 OpenAI 兼容端点返回的 usage 是完整的——

        {"prompt_tokens": 1721, "completion_tokens": 16,
         "prompt_tokens_details": {"cached_tokens": 1536}, ...}

    但上游 ``_map_usage`` 会把它全部丢掉，原因有两个：
    1. 它只保留顶层且 ``isinstance(v, int)`` 的字段，``prompt_tokens_details``
       是嵌套 dict，被直接忽略 —— 于是缓存命中数永远为 0；
    2. 真正赋值 input/output tokens 的是 ``RequestUsage.extract()``，它依赖
       genai-prices 的 provider 快照；自建端点（如 commandcode.ai）不在快照里，
       兜底到 openai 后一个字段都没提取到 —— 于是 token 数永远是 0。

    这里只覆盖上游明确留作扩展点的 ``_map_usage``（``_process_response`` 调用它），
    直接从响应对象取数字，不再经过 genai-prices；没有 usage 时仍委托 ``super()``。
    """
    global _USAGE_AWARE_MODEL_CLASS
    if _USAGE_AWARE_MODEL_CLASS is not None:
        return _USAGE_AWARE_MODEL_CLASS

    from pydantic_ai import usage
    from pydantic_ai.models.openai import OpenAIChatModel

    class UsageAwareOpenAIChatModel(OpenAIChatModel):
        def _map_usage(self, response: chat.ChatCompletion) -> usage.RequestUsage:
            raw = getattr(response, "usage", None)
            prompt_tokens = getattr(raw, "prompt_tokens", None)
            if raw is None or prompt_tokens is None:
                # 没有 usage（或字段缺失）时保留上游行为，避免把「无数据」伪装成 0 成本
                return super()._map_usage(response)

            details: dict[str, int] = {}
            completion_details = getattr(raw, "completion_tokens_details", None)
            if completion_details is not None:
                for key, value in completion_details.model_dump(exclude_none=True).items():
                    if isinstance(value, int):
                        details[key] = value
            prompt_details = getattr(raw, "prompt_tokens_details", None)
            if prompt_details is not None:
                for key, value in prompt_details.model_dump(exclude_none=True).items():
                    if isinstance(value, int):
                        details[key] = value
            cache_creation = getattr(raw, "cache_creation_input_tokens", None)
            if isinstance(cache_creation, int):
                details["cache_creation_input_tokens"] = cache_creation

            # prompt_tokens 已包含缓存命中部分（实测：共享前缀两次请求均为 1721，
            # 第二次 cached_tokens=1536），因此 input_tokens 直接取 prompt_tokens，
            # cache_read_tokens 单独记录，命中率 = cache_read / input。
            return usage.RequestUsage(
                input_tokens=int(prompt_tokens),
                output_tokens=int(getattr(raw, "completion_tokens", 0) or 0),
                cache_read_tokens=int(getattr(prompt_details, "cached_tokens", 0) or 0),
                cache_write_tokens=int(cache_creation or 0),
                details=details,
            )

    _USAGE_AWARE_MODEL_CLASS = UsageAwareOpenAIChatModel
    return _USAGE_AWARE_MODEL_CLASS


_USAGE_AWARE_MODEL_CLASS: type[Any] | None = None


class OpenAICompatibleProvider(AgentProvider):
    """通过 OpenAI 兼容接口完成规划、视觉分析和动作决策。"""

    mock = False

    def __init__(self, settings: Settings):
        if not settings.openai_api_key or not settings.agent_model:
            raise ValueError("OPENAI_API_KEY and AGENT_MODEL are required for the OpenAI provider")
        self.settings = settings
        self.name = settings.agent_model

    def _model_settings(self) -> dict[str, object] | None:
        """返回供应商兼容参数。

        注意：``thinking.type=disabled`` 只有 DeepSeek 官方端点认识。对 OpenAI 兼容的
        自建端点（例如 ``api.commandcode.ai/provider/v1``）它会被忽略，thinking 依然
        开启，因此**不能**依赖本开关规避 ``tool_choice`` 冲突；结构化输出必须改用
        :meth:`_structured_output`。详见 ``docs/STARTUP_GUIDE.md`` §15.4。
        """
        if not self.settings.agent_disable_thinking:
            return None
        return {"extra_body": {"thinking": {"type": "disabled"}}}

    @staticmethod
    def _structured_output(output_type: type[BaseModel], description: str) -> Any:
        """构造不使用工具的结构化输出规格（``PromptedOutput``）。

        裸传 ``output_type=<BaseModel>`` 时 pydantic-ai 会走 ``tool`` 模式并注册
        ``final_result`` 工具，OpenAI 适配层因而发送 ``tool_choice='required'``。
        thinking 模型拒绝任何强制 tool_choice（``required`` 或指定函数），网关会返回
        ``400 Thinking mode does not support this tool_choice``，整轮调用失败。

        ``PromptedOutput`` 改为 ``prompted`` 模式：不注册工具、不发送 tool_choice，
        由模型直接返回 JSON 文本、客户端解析校验。所有结构化输出都必须经由此方法。
        详见 ``docs/STARTUP_GUIDE.md`` §15.4。
        """
        from pydantic_ai import PromptedOutput

        return PromptedOutput(output_type, description=description)

    def _model(self, vision: bool = False):
        from pydantic_ai.providers.openai import OpenAIProvider

        configured_name = self.settings.agent_vision_model if vision else self.settings.agent_model
        model_name = configured_name or self.settings.agent_model
        provider = OpenAIProvider(
            base_url=self.settings.openai_base_url,
            api_key=self.settings.openai_api_key.get_secret_value(),
        )
        return usage_aware_chat_model_class()(model_name, provider=provider)

    def _image_payload(self, snapshot: ScreenSnapshot) -> tuple[bytes, str]:
        """按配置选择上传格式；JPEG 显著降低上传字节数与端到端延迟（计划 5.2/5.3）。"""
        source = snapshot.image_path
        if self.settings.model_image_format == "jpeg":
            if snapshot.model_image_path is not None and snapshot.model_image_path.exists():
                # 设备已返回 JPEG：直接上传，省掉一次 PNG 解码 + JPEG 编码。
                source = snapshot.model_image_path
            if source.suffix.lower() in {".jpeg", ".jpg"}:
                return source.read_bytes(), "image/jpeg"
            try:
                from PIL import Image

                with Image.open(source) as image:
                    buffer = io.BytesIO()
                    image.convert("RGB").save(buffer, format="JPEG", quality=80)
                return buffer.getvalue(), "image/jpeg"
            except Exception as exc:  # noqa: BLE001 - 解码失败回退 PNG，绝不因此中断运行
                logger.warning("JPEG conversion failed (%s); falling back to PNG bytes", exc)
        return snapshot.image_path.read_bytes(), "image/png"

    def _element_payload(self, snapshot: ScreenSnapshot, *, with_identity: bool) -> list[dict[str, Any]]:
        """按得分排序取前 N 个元素：整页全量列表是单次延迟的另一主因（计划 5.2）。"""
        limit = max(int(self.settings.model_element_limit), 1)
        ranked = sorted(snapshot.elements, key=lambda item: (-item.score, item.element_id))[:limit]
        payload: list[dict[str, Any]] = []
        for item in ranked:
            entry: dict[str, Any] = {
                "element_id": item.element_id,
                "content": item.content,
                "type": item.type,
                "bbox": item.bbox.model_dump() if item.bbox else None,
                "clickable": item.clickable,
                "editable": item.editable,
            }
            if with_identity:
                entry["key"] = item.key
                entry["id"] = item.id
            payload.append(entry)
        return payload

    async def plan(self, task: str, context: PlanningContext, max_steps: int) -> PlanResult:
        """将用户任务规划为不超过上限的原子步骤。"""
        from pydantic_ai import Agent

        agent = Agent(
            self._model(),
            output_type=self._structured_output(
                PlanResult, "The plan as a JSON object with goal, steps, model_used and mock."
            ),
            system_prompt=PLANNING_PROMPT,
            retries=2,
        )
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

    async def plan_bug_repro(
        self,
        request: BugReproRequest,
        context: PlanningContext,
        max_steps: int,
    ) -> BugReproPlan | None:
        """用结构化输出规划缺陷复现；模型不可用时返回 ``None``（照 ``advise_turn`` 的降级模式）。

        与 :meth:`plan` 同构：``PLANNING_PROMPT + "\\n" + BUG_REPRO_PROMPT`` 作 system prompt，
        user prompt 嵌入 Planning context 与序列化后的缺陷报告；结构化输出**必须**走
        :meth:`_structured_output`（``PromptedOutput``），thinking 模型拒绝 tool-mode。
        产出再过 :func:`align_plan_with_task`：去掉臆造的搜索提交步骤并以 finish 收尾。
        """
        from pydantic_ai import Agent

        if not self.settings.model_configured:
            return None
        try:
            model = self._model()
        except Exception:  # noqa: BLE001 - 模型不可用时降级为 None（端点据此返回 503）
            return None
        agent = Agent(
            model,
            output_type=self._structured_output(
                BugReproPlan,
                "The defect reproduction plan as a JSON object with title_zh, steps, checkpoints, "
                "symptom_checkpoint, notes, model_used and mock.",
            ),
            system_prompt=PLANNING_PROMPT + "\n" + BUG_REPRO_PROMPT,
            retries=2,
        )
        prompt = (
            f"Planning context: {context.model_dump_json()}\n"
            f"Maximum steps: {max_steps}\n"
            f"Defect report: {request.model_dump_json()}\n"
            "Set model_used to the configured model name and mock to false."
        )
        result = await agent.run(prompt, model_settings=self._model_settings())
        plan = result.output
        plan.model_used = self.name
        plan.mock = False
        plan.steps = align_plan_with_task(render_bug_repro_task(request), plan.steps, max_steps)
        return plan

    async def analyze(self, snapshot: ScreenSnapshot) -> VisionObservation | None:
        """分析屏幕快照并返回可选的视觉观察结果。"""
        from pydantic_ai import Agent, BinaryContent

        agent = Agent(
            self._model(vision=True),
            output_type=self._structured_output(
                VisionObservation,
                "The page observation as a JSON object with page_title, summary and elements.",
            ),
            system_prompt=VISION_PROMPT,
            retries=2,
        )
        hierarchy = self._element_payload(snapshot, with_identity=False)
        prompt = f"Image size: {snapshot.width}x{snapshot.height}. Existing UI hierarchy: {hierarchy}"
        data, media_type = self._image_payload(snapshot)
        result = await agent.run(
            [prompt, BinaryContent(data=data, media_type=media_type)],
            model_settings=self._model_settings(),
        )
        return result.output

    async def decide(
        self,
        step: PlannedStep,
        snapshot: ScreenSnapshot | None,
        feedback: str | None = None,
    ) -> ToolDecision:
        """结合计划步骤和当前快照选择一个受支持的工具动作。"""
        from pydantic_ai import Agent, BinaryContent

        agent = Agent(
            self._model(vision=True),
            output_type=self._structured_output(
                ToolDecision,
                "Return exactly one allowed UI tool decision as a JSON object.",
            ),
            system_prompt=DECISION_PROMPT,
            retries=2,
        )
        if snapshot is None:
            result = await agent.run(f"Planned step: {step.model_dump_json()}", model_settings=self._model_settings())
            return result.output
        elements = self._element_payload(snapshot, with_identity=True)
        prompt = f"Planned step: {step.model_dump_json()}\nCurrent elements: {elements}"
        if feedback:
            prompt += f"\nRecovery feedback: {feedback}"
        data, media_type = self._image_payload(snapshot)
        result = await agent.run(
            [prompt, BinaryContent(data=data, media_type=media_type)],
            model_settings=self._model_settings(),
        )
        return result.output

    def supports_combined_observation(self) -> bool:
        """开启 ``MERGE_OBSERVE_AND_DECIDE`` 时把每步压成一次视觉请求（计划 5.1）。"""
        return bool(self.settings.merge_observe_and_decide)

    async def observe_and_decide(
        self,
        step: PlannedStep,
        snapshot: ScreenSnapshot | None,
        *,
        feedback: str | None = None,
        history: list[StepHistoryEntry] | None = None,
    ) -> tuple[VisionObservation | None, ToolDecision]:
        """单次请求同时得到观测与决策（计划 5.1）；组合输出失败时自动回退为两次调用。"""
        if snapshot is None:
            return None, await self.decide(step, None, _with_history(feedback, history))
        try:
            return await self._observe_and_decide_once(step, snapshot, feedback, history)
        except Exception as exc:  # noqa: BLE001 - 组合模型不可靠时回退，保证决策质量与可用性
            logger.warning("combined observe+decide failed (%s); falling back to two calls", exc)
            return await super().observe_and_decide(step, snapshot, feedback=feedback, history=history)

    async def _observe_and_decide_once(
        self,
        step: PlannedStep,
        snapshot: ScreenSnapshot,
        feedback: str | None,
        history: list[StepHistoryEntry] | None,
    ) -> tuple[VisionObservation | None, ToolDecision]:
        from pydantic_ai import Agent, BinaryContent

        agent = Agent(
            self._model(vision=True),
            output_type=self._structured_output(
                ObservationAndDecision,
                "The page observation and the single tool decision in one JSON object with page_title, summary, "
                "elements and decision.",
            ),
            system_prompt=OBSERVE_AND_DECIDE_PROMPT,
            retries=2,
        )
        elements = self._element_payload(snapshot, with_identity=True)
        prompt = (
            f"Image size: {snapshot.width}x{snapshot.height}.\n"
            f"Planned step: {step.model_dump_json()}\n"
            f"Current elements: {elements}"
        )
        rendered_history = render_decision_history(history)
        if rendered_history:
            prompt += f"\n{rendered_history}"
        if feedback:
            prompt += f"\nRecovery feedback: {feedback}"
        data, media_type = self._image_payload(snapshot)
        result = await agent.run(
            [prompt, BinaryContent(data=data, media_type=media_type)],
            model_settings=self._model_settings(),
        )
        combined: ObservationAndDecision = result.output
        return self._compact_observation(combined), combined.decision

    def _compact_observation(self, combined: ObservationAndDecision) -> VisionObservation:
        """截断合并观测：元素表已是输入，回吐的视觉元素只保留极少数（见 Settings 注释）。"""
        limit = max(int(self.settings.model_observation_element_limit), 0)
        observation = combined.observation()
        if limit == 0:
            observation.elements = []
        elif len(observation.elements) > limit:
            observation.elements = observation.elements[:limit]
        if len(observation.summary) > _OBSERVATION_SUMMARY_LIMIT:
            observation.summary = observation.summary[:_OBSERVATION_SUMMARY_LIMIT].rstrip()
        return observation

    async def advise_turn(
        self,
        history: list[Any],
        screenshot: bytes,
        payload: str,
    ) -> AdvisorTurnResult | None:
        """探索顾问单轮对话：携带既有历史继续追问，返回累计后的完整消息历史。"""
        from pydantic_ai import Agent, BinaryContent

        agent = Agent(
            self._model(vision=True),
            output_type=self._structured_output(
                AdvisorVerdict,
                "The exploration verdict as a JSON object with page_summary, recommended, avoid and reason.",
            ),
            system_prompt=ADVISOR_PROMPT,
            retries=2,
        )
        result = await agent.run(
            [
                payload,
                BinaryContent(data=screenshot, media_type="image/png"),
            ],
            message_history=list(history) if history else None,
            model_settings=self._model_settings(),
        )
        return AdvisorTurnResult(verdict=result.output, history=result.all_messages())


def create_provider(settings: Settings) -> AgentProvider:
    """根据配置选择 Mock 或 OpenAI 兼容提供方，并拒绝缺少必要配置的模式。"""
    if settings.agent_provider == "mock":
        return MockAgentProvider()
    if settings.agent_provider == "openai":
        return OpenAICompatibleProvider(settings)
    return OpenAICompatibleProvider(settings) if settings.model_configured else MockAgentProvider()
