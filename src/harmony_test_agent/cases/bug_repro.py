"""缺陷复现（bug reproduction）计划与用例构建。

计划 D1：把自然语言缺陷报告归一化为**可回放**的用例 IR。

职责边界：

* ``BugReproPlan`` / ``CheckpointDraft`` 是**计划阶段**的产物（模型或 Mock 产出）；
  ``steps`` 复用 ``models.PlannedStep``，因此 orchestrator 能原样执行 ``plan.steps``。
* :func:`build_bug_repro_case` 是唯一的「计划 → ``TestCaseSpec``」转换入口，集成方
  （API / orchestrator / CLI）通过 ``CaseBuilder.from_bug_repro``（已薄委托到本函数）
  调用，不再自己写转换逻辑。
* **判定语义是确定性的**：用例断言的是**期望行为**，所以回放**通过 = 缺陷未复现**、
  回放**失败 = 候选复现**（最终由执行结果分析 ``ExecutionAnalysis.symptom_reproduced``
  确认）。

``BugReproRequest`` 定义在 ``models.py``（``RunRequest.bug_report`` 需要它，而 ``models.py``
不得 import ``cases/``），本模块原样再导出，因此
``from harmony_test_agent.cases.bug_repro import BugReproRequest`` 可用。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models import (
    BugReproRequest,
    LocatorKind,
    PlannedStep,
    ScenarioKind,
    StableLocator,
    TargetAppProfile,
    ToolName,
)
from .builder import (
    PLACEHOLDER_ABILITY,
    PLACEHOLDER_BUNDLE,
    SWIPE_DIRECTIONS,
    CaseBuilder,
    CaseBuildResult,
    new_case_id,
    slugify,
)
from .spec import (
    CHECKPOINT_PROPERTIES,
    BugReproSpec,
    CaseProvenance,
    CheckpointKind,
    CheckpointSpec,
    LocatorEvidence,
    LocatorSpec,
    SetupSpec,
    StepAction,
    TeardownSpec,
    TestCaseSpec,
    TestStepSpec,
)
from .titles import checkpoint_title_zh, step_title_zh

BUG_REPRO_PROMPT = """你是一个 OpenHarmony 缺陷复现用例规划器。用户会给出一份缺陷报告（标题、症状、
前置条件、自然语言复现步骤、期望行为、实际行为）。请把它翻译为可回放的原子步骤与检查点。

硬性规则：
1. `steps` 只能使用这些工具：inspect_screen, open_app, click_element, click_coordinate, input_text,
   swipe, back, wait, assert_visible, assert_not_visible, assert_text, finish。
2. 优先使用 Planning context 里 stable_locator_names 列出的稳定定位器名作为 click_element / input_text
   的 target；只有确实无法用语义定位器表达时才用 click_coordinate。
3. 禁止规划登录、支付、删除、授权（权限授予）、验证码、卸载类步骤，也不要生成任意 shell 命令。
4. 把每条前置条件转译为最前置的步骤（必要时先用 wait 等待页面稳定）。
5. 每条自然语言复现步骤转译为 1-2 个原子步骤，不要合并多条复现步骤，也不要新增用户未描述的业务动作。
6. `checkpoints` 必须断言**期望行为**（因此：用例通过 = 缺陷未复现；用例失败 = 候选复现）。
   为 `expected` 里描述的文本/元素给出对应检查点（text_contains / text_equals / element_exists / property_equals）。
7. 按 `symptom_kind` 追加一个**症状哨兵**检查点：
   crash → current_app（前台仍是被测应用）；
   white_screen / layout → page_signature（页面锚点元素存在）；
   freeze / unresponsive → element_exists（配合长等待，例如 wait_seconds=8）；
   functional → text/property 断言（针对 expected）；
   other → 回退为针对 expected 的 text_contains。
8. 把最能判定「症状是否复现」的那一个草案填进 `symptom_checkpoint`。
9. `title_zh` 用中文（可取缺陷标题）；`notes` 记录你的判定假设；`model_used` 填配置的模型名，`mock` 置 false。
"""

#: 计划的 ``steps`` 只允许这十二个工具（与 ``PLANNING_PROMPT`` 一致）。
ALLOWED_TOOLS: frozenset[ToolName] = frozenset(
    {
        ToolName.INSPECT_SCREEN,
        ToolName.OPEN_APP,
        ToolName.CLICK_ELEMENT,
        ToolName.CLICK_COORDINATE,
        ToolName.INPUT_TEXT,
        ToolName.SWIPE,
        ToolName.BACK,
        ToolName.WAIT,
        ToolName.ASSERT_VISIBLE,
        ToolName.ASSERT_NOT_VISIBLE,
        ToolName.ASSERT_TEXT,
        ToolName.FINISH,
    }
)

#: ``symptom_kind`` 的全部取值（与 ``BugReproRequest`` 的 Literal 保持一致）。
SYMPTOM_KINDS: tuple[str, ...] = (
    "crash",
    "freeze",
    "white_screen",
    "unresponsive",
    "layout",
    "functional",
    "other",
)

#: 「卡死/无响应」哨兵的长等待（秒）：短等待无法把卡死与正常动画区分开。
SYMPTOM_LONG_WAIT_SECONDS = 8.0

#: 一个 ``page_signature`` 检查点最多保留的入口页锚点数。
#: 与 ``orchestrator._legacy_entry_revalidate`` 的 ``stable_locator_inventory[:3]`` 对齐。
ANCHOR_LIMIT = 3

#: Mock 的默认等待秒数（「其余步骤 → WAIT 1 秒」）。
DEFAULT_WAIT_SECONDS = 1.0

#: 单步等待上限（与 ``runtime/safety.py`` 的 30 秒上限一致）。
MAX_WAIT_SECONDS = 30.0

#: 从自然语言里抽取显式等待时长，例如「等待 3 秒」。
_WAIT_SECONDS = re.compile(r"(\d+(?:\.\d+)?)\s*秒")

#: 抽取引号内文本（用于 input_text 的输入值）。
_QUOTED = re.compile(r"[「『\"'《]([^」』\"'》]+)[」』\"'》]")


class BugReproBuildError(ValueError):
    """缺陷复现计划无法构建为合法用例（例如草案缺少 anchor / property_name）。

    继承 ``ValueError``：调用方按 ``ValueError`` 捕获即可（API 可据此返回 422），
    同时可以在需要时精确区分「计划不合法」与其它参数错误。
    """


class CheckpointDraft(BaseModel):
    """计划阶段的检查点草案；由 :func:`checkpoint_from_draft` 解析为 ``CheckpointSpec``。

    ``wait_seconds`` 是相对计划 D1 表的**附加**字段：`freeze/unresponsive` 哨兵要求
    「element_exists + 长等待」，而冻结的语义只能靠等待时长表达。
    """

    kind: CheckpointKind
    target: str = ""
    expected: Any = None
    property_name: str | None = None
    message_zh: str = ""
    soft: bool = False
    wait_seconds: float | None = None


class BugReproPlan(BaseModel):
    """缺陷复现计划：可执行步骤 + 检查点草案 + 症状哨兵。"""

    title_zh: str = ""
    steps: list[PlannedStep] = Field(default_factory=list)
    checkpoints: list[CheckpointDraft] = Field(default_factory=list)
    symptom_checkpoint: CheckpointDraft | None = None
    notes: str = ""
    model_used: str = "mock"
    mock: bool = True


# ---------------------------------------------------------------------------
# 症状哨兵（计划 D1 表）
# ---------------------------------------------------------------------------


def symptom_sentinel(symptom_kind: str, expected: str = "") -> CheckpointDraft:
    """按 ``symptom_kind`` 给出「症状是否复现」的哨兵检查点草案。

    表（与计划 D1 一字对应）：

    ``crash`` → ``current_app``（前台仍是被测应用）；
    ``white_screen`` / ``layout`` → ``page_signature``（页面锚点元素存在）；
    ``freeze`` / ``unresponsive`` → ``element_exists``（长等待）；
    ``functional`` → 针对 ``expected`` 的文本断言；
    ``other`` → 回退为针对 ``expected`` 的 ``text_contains``。

    ``expected`` 是可选补充：文本类哨兵用它作为断言内容，``freeze``/``unresponsive``
    在调用方拿不到稳定定位器名时用它作为兜底 target。
    """
    normalized = (symptom_kind or "").strip().casefold()
    if normalized == "crash":
        return CheckpointDraft(
            kind=CheckpointKind.CURRENT_APP,
            message_zh="症状哨兵：前台应用应仍为被测应用（崩溃时应用已退出或回到桌面）",
        )
    if normalized in {"white_screen", "layout"}:
        return CheckpointDraft(
            kind=CheckpointKind.PAGE_SIGNATURE,
            message_zh="症状哨兵：页面锚点元素应可见（白屏或布局异常时锚点缺失）",
        )
    if normalized in {"freeze", "unresponsive"}:
        return CheckpointDraft(
            kind=CheckpointKind.ELEMENT_EXISTS,
            target=expected,
            wait_seconds=SYMPTOM_LONG_WAIT_SECONDS,
            message_zh=f"症状哨兵：等待 {SYMPTOM_LONG_WAIT_SECONDS:g} 秒后目标元素仍应可见（卡死时界面不再刷新）",
        )
    if normalized == "functional":
        return CheckpointDraft(
            kind=CheckpointKind.TEXT_CONTAINS,
            expected=expected,
            message_zh=f"症状哨兵：应出现期望的功能文案「{expected}」",
        )
    return CheckpointDraft(
        kind=CheckpointKind.TEXT_CONTAINS,
        expected=expected,
        message_zh=f"症状哨兵：应出现期望的文案「{expected}」（症状类别未知，回退为文本包含断言）",
    )


def expected_behaviour_checkpoint(request: BugReproRequest) -> CheckpointDraft:
    """由 ``request.expected`` 生成「期望行为」检查点草案（通过 = 缺陷未复现）。"""
    return CheckpointDraft(
        kind=CheckpointKind.TEXT_CONTAINS,
        expected=request.expected,
        message_zh=f"期望行为：界面应包含「{request.expected}」",
    )


# ---------------------------------------------------------------------------
# 确定性 Mock 计划（完全离线）
# ---------------------------------------------------------------------------


def mock_step_from_text(text: str, *, step_id: str, instruction: str | None = None) -> PlannedStep:
    """把一条中文自然语言步骤确定性翻译为 ``PlannedStep``。

    关键词表（计划 D1）：点击 → ``CLICK_ELEMENT``、输入 → ``INPUT_TEXT``、滑动 → ``SWIPE``、
    返回/退出 → ``BACK``，其余 → ``WAIT``。判定按「点击 > 输入 > 滑动 > 返回/退出」的固定顺序，
    因此同一文本永远得到同一步骤；``WAIT`` 的秒数取文本里显式的「N 秒」，没有则 1 秒。
    """
    body = (text or "").strip()
    label = instruction if instruction is not None else body

    if "点击" in body:
        return PlannedStep(
            step_id=step_id,
            instruction=label,
            tool=ToolName.CLICK_ELEMENT,
            target=_after_keyword(body, "点击") or body,
        )
    if "输入" in body:
        value = _first_match(_QUOTED, body) or _after_keyword(body, "输入")
        return PlannedStep(
            step_id=step_id,
            instruction=label,
            tool=ToolName.INPUT_TEXT,
            target=_between(body, "在", "输入") or "输入框",
            text=value,
        )
    if "滑动" in body:
        return PlannedStep(
            step_id=step_id,
            instruction=label,
            tool=ToolName.SWIPE,
            direction=_swipe_direction(body),
        )
    if "返回" in body or "退出" in body:
        return PlannedStep(step_id=step_id, instruction=label, tool=ToolName.BACK)
    return PlannedStep(
        step_id=step_id,
        instruction=label,
        tool=ToolName.WAIT,
        wait_seconds=_parse_wait_seconds(body),
    )


def mock_bug_repro_plan(
    request: BugReproRequest,
    *,
    bundle_name: str = "",
    stable_locator_names: Sequence[str] = (),
    max_steps: int = 20,
    model_used: str = "mock",
) -> BugReproPlan:
    """确定性、完全离线的缺陷复现计划（``MockAgentProvider.plan_bug_repro`` 的内核）。

    刻意不依赖 ``PlanningContext``：``cases`` 包不得 import ``agents``（会与
    ``agents.providers`` 反向依赖成环），因此这里只收下真正需要的那几个字段。
    """
    limit = max(1, int(max_steps or request.max_steps or 1))
    pre_steps = [
        mock_step_from_text(text, step_id=f"pre-{index:02d}", instruction=f"前置条件：{text}")
        for index, text in enumerate(request.preconditions, start=1)
    ]
    repro_steps = [
        mock_step_from_text(text, step_id=f"step-{index:02d}")
        for index, text in enumerate(request.repro_steps_nl, start=1)
    ]
    body = (pre_steps + repro_steps)[: max(0, limit - 1)]
    steps = [*body, PlannedStep(step_id="finish", instruction="结束任务", tool=ToolName.FINISH)]

    checkpoints: list[CheckpointDraft] = [expected_behaviour_checkpoint(request)]
    sentinel = symptom_sentinel(request.symptom_kind, request.expected)
    if sentinel.kind == CheckpointKind.CURRENT_APP and bundle_name:
        sentinel = sentinel.model_copy(update={"expected": bundle_name})
    if sentinel.kind == CheckpointKind.ELEMENT_EXISTS and stable_locator_names:
        sentinel = sentinel.model_copy(update={"target": stable_locator_names[0]})
    if not any(sentinel == item for item in checkpoints):
        checkpoints.append(sentinel)

    return BugReproPlan(
        title_zh=request.title,
        steps=steps,
        checkpoints=checkpoints,
        symptom_checkpoint=sentinel,
        notes=(
            f"Mock 确定性计划：症状类别 {request.symptom_kind}，"
            f"{len(body)} 个复现步骤 + finish；用例通过 = 缺陷未复现。"
        ),
        model_used=model_used,
        mock=True,
    )


# ---------------------------------------------------------------------------
# 计划 → 用例 IR
# ---------------------------------------------------------------------------


def build_bug_repro_case(
    builder: CaseBuilder,
    plan: BugReproPlan,
    request: BugReproRequest,
    profile: TargetAppProfile | None,
) -> CaseBuildResult:
    """把缺陷复现计划构建为用例 IR。

    集成方通过 ``CaseBuilder.from_bug_repro`` 调用（``builder.py`` 里是薄委托），
    语义与 ``CaseBuilder.from_trace`` 的 tool → action 表保持一致：

    * ``inspect_screen`` / ``finish`` 是 agent 控制动作，直接省略；
    * ``open_app`` 由确定性的「停应用 → 启动 → 等待」setup 取代；
    * ``click_element`` / ``input_text`` 的 target 先在 ``profile.stable_locator_inventory``
      里按 name 精确 → 包含解析，解析不到才回退成精确文本定位器；
    * ``click_coordinate`` 必须带解析边界与警告（IR 不变式）；
    * 检查点草案全部挂到最后一个步骤上（等价于在复现步骤之后追加断言）。

    plan 一个硬检查点都没给时，合成 ``CURRENT_APP``（崩溃哨兵）与入口页 ``PAGE_SIGNATURE``，
    并记一条警告 —— IR 不变式 5 要求非 draft 用例至少有 1 个硬检查点。
    """
    warnings: list[str] = []
    omitted: list[dict[str, str]] = []
    steps: list[TestStepSpec] = []
    coordinate_fallbacks = 0
    explicit_assertions = 0

    bundle_name = profile.bundle_name if profile is not None else PLACEHOLDER_BUNDLE
    main_ability = profile.main_ability if profile is not None else PLACEHOLDER_ABILITY
    if profile is None:
        warnings.append("缺少 TargetAppProfile：bundle/ability 使用占位身份，用例仅作诊断用途")

    def add_step(action: StepAction, **kwargs: Any) -> TestStepSpec:
        step = TestStepSpec(
            step_id=kwargs.pop("step_id", f"step-{len(steps) + 1}"),
            index=len(steps) + 1,
            action=action,
            title_zh="",
            **kwargs,
        )
        steps.append(step)
        return step

    def attach(checkpoint: CheckpointSpec) -> None:
        """检查点挂到最后一个步骤；没有步骤时新建一个纯 CHECK 步骤承载它。"""
        checkpoint.message_zh = checkpoint.message_zh or checkpoint_title_zh(checkpoint)
        if steps:
            steps[-1].checkpoints.append(checkpoint)
        else:
            add_step(StepAction.CHECK, checkpoints=[checkpoint], locator=checkpoint.locator)

    for planned in plan.steps:
        tool = planned.tool
        step_id = planned.step_id
        if tool in {ToolName.INSPECT_SCREEN, ToolName.FINISH}:
            omitted.append({"step_id": step_id, "tool": str(tool), "reason": f"{tool} is an agent-control action"})
        elif tool == ToolName.OPEN_APP:
            omitted.append(
                {
                    "step_id": step_id,
                    "tool": str(tool),
                    "reason": "OPEN_APP is replaced by deterministic stop/start/wait setup",
                }
            )
        elif tool == ToolName.CLICK_ELEMENT:
            add_step(
                StepAction.CLICK, step_id=step_id, locator=_step_locator(profile, planned.target, warnings, step_id)
            )
        elif tool == ToolName.CLICK_COORDINATE:
            point = planned.coordinate or (0, 0)
            coordinate = (int(point[0]), int(point[1]))
            add_step(
                StepAction.CLICK,
                step_id=step_id,
                coordinate=coordinate,
                locator=builder.coordinate_locator(
                    coordinate,
                    bound=_resolution_bound(profile),
                    label="",
                    warning=f"{step_id}: click uses a coordinate fallback",
                ),
            )
            warnings.append(f"{step_id}: click uses a coordinate fallback")
            coordinate_fallbacks += 1
        elif tool == ToolName.INPUT_TEXT:
            target = planned.target or "输入框"
            add_step(
                StepAction.INPUT_TEXT,
                step_id=step_id,
                locator=_step_locator(profile, target, warnings, step_id),
                text=planned.text or "",
            )
        elif tool == ToolName.SWIPE:
            direction = str(planned.direction or "up").upper()
            if direction not in SWIPE_DIRECTIONS:
                warnings.append(f"{step_id}: unknown swipe direction {planned.direction!r}, fallback to UP")
                direction = "UP"
            add_step(
                StepAction.SWIPE,
                step_id=step_id,
                direction=direction.lower(),  # type: ignore[arg-type]
            )
        elif tool == ToolName.BACK:
            add_step(StepAction.BACK, step_id=step_id)
        elif tool == ToolName.WAIT:
            add_step(StepAction.WAIT, step_id=step_id, wait_seconds=float(planned.wait_seconds or 1))
        elif tool == ToolName.ASSERT_VISIBLE:
            locator = _step_locator(profile, planned.target or planned.text, warnings, step_id)
            attach(CheckpointSpec(kind=CheckpointKind.ELEMENT_EXISTS, message_zh="", locator=locator))
            explicit_assertions += 1
        elif tool == ToolName.ASSERT_NOT_VISIBLE:
            locator = _step_locator(profile, planned.target or planned.text, warnings, step_id)
            attach(CheckpointSpec(kind=CheckpointKind.ELEMENT_ABSENT, message_zh="", locator=locator))
            explicit_assertions += 1
        elif tool == ToolName.ASSERT_TEXT:
            expected = planned.text or planned.target or ""
            candidate = _resolve_locator(profile, planned.target or "")
            locator = (
                candidate if candidate is not None and candidate.kind in {LocatorKind.KEY, LocatorKind.ID} else None
            )
            attach(
                CheckpointSpec(
                    kind=CheckpointKind.TEXT_EQUALS,
                    message_zh="",
                    locator=locator,
                    expected=str(expected),
                )
            )
            explicit_assertions += 1
        else:
            omitted.append({"step_id": step_id, "tool": str(tool), "reason": f"unsupported replay tool: {tool}"})

    drafts = list(plan.checkpoints)
    if plan.symptom_checkpoint is not None and not any(plan.symptom_checkpoint == item for item in drafts):
        # 计划 D1：症状哨兵必须出现在用例的检查点里（即使模型忘了把它放进 checkpoints）。
        drafts.append(plan.symptom_checkpoint)
    if not any(not draft.soft for draft in drafts):
        warnings.append("计划未提供任何硬检查点，已合成崩溃哨兵（CURRENT_APP）与入口页 PAGE_SIGNATURE")
        drafts = _fallback_drafts(profile, bundle_name, warnings) + drafts

    for draft in drafts:
        attach(checkpoint_from_draft(draft, profile=profile, bundle_name=bundle_name))
        if not draft.soft:
            explicit_assertions += 1

    if not steps:
        # IR 不变式 3：非压测用例至少一步；历史模板对空脚本发射裸 ``pass``。
        add_step(StepAction.NOOP_COMMENT, step_id="empty-body", comment="")

    for position, step in enumerate(steps, start=1):
        step.index = position
        step.title_zh = step.title_zh or step_title_zh(step)

    hard = sum(1 for step in steps for checkpoint in step.checkpoints if not checkpoint.soft)
    soft = sum(1 for step in steps for checkpoint in step.checkpoints if checkpoint.soft)
    checkpoint_count = sum(len(step.checkpoints) for step in steps)
    counts = {
        "source_actions": len(plan.steps),
        "source_assertions": len(plan.checkpoints),
        "generated_actions": sum(1 for step in steps if step.action != StepAction.CHECK),
        "generated_assertions": checkpoint_count,
        "hard_checkpoints": hard,
        "soft_checkpoints": soft,
        "omitted_actions": len(omitted),
        "failed_actions": 0,
        "coordinate_fallbacks": coordinate_fallbacks,
    }

    title = (plan.title_zh.strip() or request.title.strip())[:200]
    incomplete_reasons: list[str] = []
    if hard == 0:
        incomplete_reasons.append("no hard checkpoint was produced for the bug reproduction")
    if bundle_name == PLACEHOLDER_BUNDLE:
        incomplete_reasons.append("placeholder bundle supplied; the case is diagnostic only")
    replay_eligible = not incomplete_reasons
    spec = TestCaseSpec(
        case_id=new_case_id(),
        slug=slugify(title, fallback="bug-repro"),
        title_zh=title,
        scenario=ScenarioKind.BUG_REPRODUCTION,
        status="active" if hard else "draft",
        tags=["bug-repro", f"symptom-{request.symptom_kind}"],
        bundle_name=bundle_name,
        main_ability=main_ability,
        device_sn=request.device_id,
        setup=SetupSpec(
            stop_app_first=True,
            start_app=True,
            startup_wait_seconds=_startup_wait(profile),
            listen_toast=any(
                checkpoint.kind == CheckpointKind.TOAST for step in steps for checkpoint in step.checkpoints
            ),
        ),
        steps=steps,
        teardown=TeardownSpec(capture_final_screenshot=True, stop_app=False),
        bug_repro=BugReproSpec(
            symptom=request.symptom,
            symptom_kind=request.symptom_kind,
            preconditions=list(request.preconditions),
            repro_steps_nl=list(request.repro_steps_nl),
            expected=request.expected,
            actual=request.actual,
        ),
        provenance=CaseProvenance(
            source_kind="bug_report",
            source_id=request.title,
            profile_target_app_id=profile.target_app_id if profile is not None else None,
        ),
    )
    return CaseBuildResult(
        spec=spec,
        omitted_actions=omitted,
        warnings=warnings,
        counts=counts,
        incomplete_reasons=incomplete_reasons,
        replay_eligible=replay_eligible,
        purpose="acceptance" if replay_eligible else "diagnostic",
        explicit_assertions=explicit_assertions,
        source_agent_outcome="unknown",
    )


def checkpoint_from_draft(
    draft: CheckpointDraft,
    *,
    profile: TargetAppProfile | None,
    bundle_name: str = "",
) -> CheckpointSpec:
    """把检查点草案解析为可渲染的 ``CheckpointSpec``。

    草案字段与 ``CheckpointSpec`` 的对应关系是**映射**而不是同名复制：
    ``current_app`` 用 bundle 名填 ``expected``；``page_signature`` 填 ``anchors``；
    ``property_equals`` 填 ``property_name``；``element_exists`` / ``element_absent`` 填 ``locator``。
    无法满足的草案会抛中文 ``ValueError``，绝不产出「字段齐全但语义非法」的 spec。
    """
    kind = draft.kind
    wait_seconds = draft.wait_seconds

    if kind == CheckpointKind.CURRENT_APP:
        expected = _as_text(draft.expected) or bundle_name or PLACEHOLDER_BUNDLE
        if not expected.strip():
            raise BugReproBuildError(
                "current_app 检查点需要被测应用的 bundle 名称：草案未填写 expected，也拿不到 Profile"
            )
        return _titled(
            CheckpointSpec(
                kind=kind,
                message_zh=draft.message_zh,
                expected=expected,
                soft=draft.soft,
                wait_seconds=wait_seconds,
            )
        )
    if kind == CheckpointKind.PAGE_SIGNATURE:
        page_path = str(draft.target or "").strip()
        anchors = _anchors_for_page(profile, page_path)
        if not anchors:
            raise BugReproBuildError(
                f"page_signature 检查点无法解析页面锚点：草案 target={draft.target!r}，"
                "而 Profile 里没有可用的稳定定位器证据"
            )
        return _titled(
            CheckpointSpec(
                kind=kind,
                message_zh=draft.message_zh,
                page_path=page_path,
                anchors=anchors,
                soft=draft.soft,
                wait_seconds=wait_seconds,
            )
        )
    if kind in {CheckpointKind.ELEMENT_EXISTS, CheckpointKind.ELEMENT_ABSENT}:
        locator = _resolve_locator(profile, str(draft.target or ""))
        if locator is None:
            raise BugReproBuildError(f"{kind.value} 检查点缺少 target，无法解析定位器")
        return _titled(
            CheckpointSpec(
                kind=kind,
                message_zh=draft.message_zh,
                locator=locator,
                soft=draft.soft,
                wait_seconds=wait_seconds,
            )
        )
    if kind in {CheckpointKind.TEXT_EQUALS, CheckpointKind.TEXT_CONTAINS, CheckpointKind.TOAST}:
        expected = _as_text(draft.expected)
        if not expected.strip():
            raise BugReproBuildError(f"{kind.value} 检查点缺少 expected，无法断言期望行为")
        locator = None
        if kind == CheckpointKind.TEXT_EQUALS:
            candidate = _resolve_locator(profile, str(draft.target or ""))
            if candidate is not None and candidate.kind in {LocatorKind.KEY, LocatorKind.ID}:
                locator = candidate
        return _titled(
            CheckpointSpec(
                kind=kind,
                message_zh=draft.message_zh,
                locator=locator,
                expected=expected,
                soft=draft.soft,
                wait_seconds=wait_seconds,
            )
        )
    if kind == CheckpointKind.PROPERTY_EQUALS:
        if not draft.property_name:
            raise BugReproBuildError("property_equals 检查点缺少 property_name")
        if draft.property_name not in CHECKPOINT_PROPERTIES:
            raise BugReproBuildError(
                f"property_equals 检查点的 property_name={draft.property_name!r} 不在受支持属性表内"
            )
        locator = _resolve_locator(profile, str(draft.target or ""))
        if locator is None:
            raise BugReproBuildError("property_equals 检查点缺少 target，无法解析定位器")
        return _titled(
            CheckpointSpec(
                kind=kind,
                message_zh=draft.message_zh,
                locator=locator,
                property_name=draft.property_name,  # type: ignore[arg-type]
                expected=draft.expected,
                soft=draft.soft,
                wait_seconds=wait_seconds,
            )
        )
    if kind == CheckpointKind.SCREENSHOT_CAPTURED:
        return _titled(
            CheckpointSpec(
                kind=kind,
                message_zh=draft.message_zh,
                expected=_as_text(draft.expected) or "final.jpeg",
                soft=draft.soft,
                wait_seconds=wait_seconds,
            )
        )
    raise BugReproBuildError(f"不支持的检查点类型：{kind}")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _titled(checkpoint: CheckpointSpec) -> CheckpointSpec:
    """补齐中文标题（IR 要求每个检查点都能推导出人类可读标题）。"""
    checkpoint.message_zh = checkpoint.message_zh or checkpoint_title_zh(checkpoint)
    return checkpoint


def _fallback_drafts(
    profile: TargetAppProfile | None,
    bundle_name: str,
    warnings: list[str],
) -> list[CheckpointDraft]:
    """计划没给硬检查点时合成「崩溃哨兵 + 入口页页面特征」。"""
    drafts = [
        CheckpointDraft(
            kind=CheckpointKind.CURRENT_APP,
            expected=bundle_name,
            message_zh="兜底检查点：前台应用应仍为被测应用（缺陷复现时应用可能已崩溃退出）",
        )
    ]
    signature = _entry_page_signature(profile)
    if _anchors_for_page(profile, signature):
        drafts.append(
            CheckpointDraft(
                kind=CheckpointKind.PAGE_SIGNATURE,
                target=signature,
                message_zh="兜底检查点：被测应用入口页的锚点元素应可见",
            )
        )
    else:
        warnings.append("Profile 缺少入口页锚点证据，无法合成 PAGE_SIGNATURE 检查点")
    return drafts


def _step_locator(
    profile: TargetAppProfile | None,
    target: str | None,
    warnings: list[str],
    step_id: str,
) -> LocatorSpec:
    """步骤定位器：Profile 稳定定位器优先，否则回退为精确文本（并记警告）。"""
    label = (target or "").strip()
    matched = _match_stable_locator(profile, label)
    if matched is not None:
        locator = _anchor_locator(matched)
        if locator is not None:
            return locator
    warnings.append(f"{step_id}: semantic target {label!r} fell back to exact text")
    return LocatorSpec(kind=LocatorKind.TEXT, value=label, target_label=label)


def _resolve_locator(profile: TargetAppProfile | None, target: str) -> LocatorSpec | None:
    """按稳定定位器 ``name`` 精确 → 包含解析；解析不到时回退为精确文本定位器。

    包含匹配的方向与 ``perception/normalizer.py`` 一致：目标的字面值出现在定位器名里即命中。
    """
    name = (target or "").strip()
    if not name:
        return None
    matched = _match_stable_locator(profile, name)
    if matched is not None:
        locator = _anchor_locator(matched)
        if locator is not None:
            return locator
    return LocatorSpec(kind=LocatorKind.TEXT, value=name, target_label=name)


def _match_stable_locator(profile: TargetAppProfile | None, name: str) -> StableLocator | None:
    """在 ``profile.stable_locator_inventory`` 里按 name 精确 → 包含查找。"""
    if profile is None or not name:
        return None
    folded = name.casefold()
    inventory = profile.stable_locator_inventory
    item = next((entry for entry in inventory if entry.name.casefold() == folded), None)
    if item is None:
        item = next((entry for entry in inventory if folded in entry.name.casefold()), None)
    return item


def _anchors_for_page(profile: TargetAppProfile | None, signature: str) -> list[LocatorSpec]:
    """解析某个页签名（空字符串表示入口页）的锚点定位器，最多 ``ANCHOR_LIMIT`` 个。"""
    if profile is None:
        return []
    page = (signature or "").strip()
    if page:
        anchors = _anchors_of(item for item in profile.stable_locator_inventory if item.page_signature == page)
        if anchors:
            return anchors[:ANCHOR_LIMIT]
    entry = _entry_page_signature(profile)
    if entry and entry != page:
        anchors = _anchors_of(item for item in profile.stable_locator_inventory if item.page_signature == entry)
        if anchors:
            return anchors[:ANCHOR_LIMIT]
    # 旧版 Profile 的 core_flows.pages 与验证期定位器不在同一哈希空间，
    # 因此兜底为「Profile 头部定位器即入口页锚点」（与 _legacy_entry_revalidate 同口径）。
    return _anchors_of(profile.stable_locator_inventory)[:ANCHOR_LIMIT]


def _anchors_of(items: Iterable[StableLocator]) -> list[LocatorSpec]:
    anchors: list[LocatorSpec] = []
    for item in items:
        locator = _anchor_locator(item)
        if locator is not None:
            anchors.append(locator)
    return anchors


def _anchor_locator(item: StableLocator) -> LocatorSpec | None:
    """把 Profile 的稳定定位器转成锚点；没有任何可用身份时返回 ``None``。"""
    if item.key:
        kind, value = LocatorKind.KEY, item.key
    elif item.id:
        kind, value = LocatorKind.ID, item.id
    elif item.text:
        kind, value = LocatorKind.TEXT, item.text
    else:
        return None
    return LocatorSpec(
        kind=kind,
        value=value,
        target_label=item.name or value,
        evidence=LocatorEvidence(
            observed_rounds=item.observed_rounds,
            unique_match_rounds=item.unique_match_rounds,
            dynamic_pattern=item.dynamic_pattern,
            page_signature=item.page_signature,
            confidence=item.confidence,
            source="profile_stable_locator",
        ),
    )


def _entry_page_signature(profile: TargetAppProfile | None) -> str:
    """入口页页签名：优先 ``core_flows[0].pages[0]``，否则取第一个有页签的稳定定位器。"""
    if profile is None:
        return ""
    for flow in profile.core_flows:
        pages = flow.get("pages") or []
        if pages:
            return str(pages[0])
    for item in profile.stable_locator_inventory:
        if item.page_signature:
            return item.page_signature
    return ""


def _resolution_bound(profile: TargetAppProfile | None) -> tuple[int, int] | None:
    """坐标步骤的解析边界：优先 Profile 已验证过的分辨率。"""
    if profile is None:
        return None
    resolutions = profile.device_compatibility.validated_resolutions
    if not resolutions:
        return None
    width, height = resolutions[-1]
    return (int(width), int(height))


def _startup_wait(profile: TargetAppProfile | None) -> float:
    if profile is None:
        return 2.0
    return float(profile.launch_strategy.get("wait_seconds", 2))


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


def _after_keyword(text: str, keyword: str) -> str:
    """取关键词之后的剩余文本（去掉中英文标点空白）。"""
    position = text.find(keyword)
    if position < 0:
        return ""
    return text[position + len(keyword) :].strip(" ，,。.：:；;、!！?？\"'“”")


def _between(text: str, left: str, right: str) -> str:
    """取 ``left`` 与 ``right`` 之间的文本，例如「在搜索框输入 X」→「搜索框」。"""
    start = text.find(left)
    if start < 0:
        return ""
    end = text.find(right, start + len(left))
    if end < 0:
        return ""
    return text[start + len(left) : end].strip(" 的")


def _first_match(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def _parse_wait_seconds(text: str) -> float:
    """显式「N 秒」→ N（夹到 1..30），否则默认 1 秒。"""
    match = _WAIT_SECONDS.search(text or "")
    if not match:
        return DEFAULT_WAIT_SECONDS
    return min(MAX_WAIT_SECONDS, max(1.0, float(match.group(1))))


def _swipe_direction(text: str) -> Literal["up", "down", "left", "right"]:
    if any(marker in text for marker in ("向下", "下滑", "下翻")):
        return "down"
    if any(marker in text for marker in ("向左", "左滑")):
        return "left"
    if any(marker in text for marker in ("向右", "右滑")):
        return "right"
    return "up"


__all__ = [
    "ALLOWED_TOOLS",
    "ANCHOR_LIMIT",
    "BUG_REPRO_PROMPT",
    "DEFAULT_WAIT_SECONDS",
    "MAX_WAIT_SECONDS",
    "SYMPTOM_KINDS",
    "SYMPTOM_LONG_WAIT_SECONDS",
    "BugReproBuildError",
    "BugReproPlan",
    "BugReproRequest",
    "CheckpointDraft",
    "build_bug_repro_case",
    "checkpoint_from_draft",
    "expected_behaviour_checkpoint",
    "mock_bug_repro_plan",
    "mock_step_from_text",
    "symptom_sentinel",
]
