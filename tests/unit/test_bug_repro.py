"""``cases/bug_repro.py`` 与 provider 接入的单元测试（计划 D1 + 「新增测试文件与必测断言」表）。

覆盖：

* Mock 计划的确定性（同一请求两次 ``model_dump()`` 完全一致）、关键词翻译、
  ``max_steps`` 上限、步骤工具全部落在允许工具集内；
* 每个 ``symptom_kind`` 都产出**期望行为检查点**与**症状哨兵**（按 D1 表核对 ``CheckpointKind``）；
* 计划 → ``TestCaseSpec``：构造不抛、索引严格 1..N、``scenario=bug_reproduction``、
  ``bug_repro`` 非空、定位器从 Profile 稳定定位器解析；
* 零硬检查点的兜底合成（CURRENT_APP + PAGE_SIGNATURE + 警告）与无法满足的草案抛中文错误；
* 基类 ``plan_bug_repro`` 默认返回 ``None``；``OpenAICompatibleProvider`` 路径用替身
  （monkeypatch ``_structured_output`` 与 ``pydantic_ai.Agent``）验证，**不发起任何网络调用**。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from harmony_test_agent.agents.providers import (
    PLANNING_PROMPT,
    AgentProvider,
    MockAgentProvider,
    OpenAICompatibleProvider,
    PlanningContext,
    align_plan_with_task,
)
from harmony_test_agent.cases.bug_repro import (
    ALLOWED_TOOLS,
    BUG_REPRO_PROMPT,
    SYMPTOM_KINDS,
    SYMPTOM_LONG_WAIT_SECONDS,
    BugReproBuildError,
    BugReproPlan,
    BugReproRequest,
    CheckpointDraft,
    build_bug_repro_case,
    checkpoint_from_draft,
    mock_bug_repro_plan,
    mock_step_from_text,
    symptom_sentinel,
)
from harmony_test_agent.cases.builder import PLACEHOLDER_BUNDLE, CaseBuilder
from harmony_test_agent.cases.spec import CheckpointKind, ScenarioKind, StepAction
from harmony_test_agent.cases.titles import step_title_zh
from harmony_test_agent.config import Settings
from harmony_test_agent.models import (
    ConfidenceLevel,
    LocatorKind,
    PlannedStep,
    PlanResult,
    ProfileStatus,
    ScreenSnapshot,
    StableLocator,
    TargetAppProfile,
    ToolDecision,
    ToolName,
    VisionObservation,
)

#: 计划 D1 的症状哨兵表：每个 symptom_kind 期望的哨兵类型。
SENTINEL_KINDS: dict[str, CheckpointKind] = {
    "crash": CheckpointKind.CURRENT_APP,
    "freeze": CheckpointKind.ELEMENT_EXISTS,
    "white_screen": CheckpointKind.PAGE_SIGNATURE,
    "unresponsive": CheckpointKind.ELEMENT_EXISTS,
    "layout": CheckpointKind.PAGE_SIGNATURE,
    "functional": CheckpointKind.TEXT_CONTAINS,
    "other": CheckpointKind.TEXT_CONTAINS,
}


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def make_request(**overrides: Any) -> BugReproRequest:
    """构造一份最小可用的缺陷报告；用例只覆盖它真正关心的字段。"""
    payload: dict[str, Any] = {
        "title": "搜索页偶现白屏",
        "symptom": "进入搜索页后整屏空白",
        "symptom_kind": "white_screen",
        "preconditions": ["已登录"],
        "repro_steps_nl": ["点击首页搜索入口", "在搜索框输入 OpenHarmony", "等待 3 秒"],
        "expected": "显示搜索输入框与热词列表",
        "actual": "白屏，无任何控件",
        "max_steps": 20,
    }
    payload.update(overrides)
    return BugReproRequest(**payload)


def make_profile() -> TargetAppProfile:
    """入口页 + 搜索页 + 详情页各有稳定定位器的 Profile（status=draft，跳过晋级门禁）。"""
    return TargetAppProfile(
        target_app_id="zhihu-plus",
        display_name="知乎++",
        bundle_name="com.zhihu.hmos",
        main_ability="EntryAbility",
        status=ProfileStatus.DRAFT,
        launch_strategy={"wait_seconds": 2},
        stable_locator_inventory=[
            StableLocator(
                name="首页搜索入口",
                page_signature="/home",
                key="p2_home_titlebar_search",
                observed_rounds=5,
                unique_match_rounds=5,
                confidence=ConfidenceLevel.HIGH,
            ),
            StableLocator(
                name="搜索输入框",
                page_signature="/search",
                id="search_input",
                observed_rounds=4,
                unique_match_rounds=4,
            ),
            StableLocator(
                name="热词列表",
                page_signature="/search",
                text="热词",
                observed_rounds=4,
                unique_match_rounds=4,
            ),
            StableLocator(
                name="详情标题",
                page_signature="/detail",
                key="p2_detail_title",
                observed_rounds=4,
                unique_match_rounds=4,
            ),
        ],
        core_flows=[
            {"pages": ["/home", "/search", "/detail"], "steps": [], "interaction_types": ["click", "input"]},
        ],
    )


def make_context(profile: TargetAppProfile | None = None) -> PlanningContext:
    return PlanningContext.from_profile(profile or make_profile())


def mock_plan(request: BugReproRequest, context: PlanningContext | None = None, max_steps: int = 20) -> BugReproPlan:
    """走真正的 provider 入口拿 Mock 计划（而不是直接调内核函数）。"""
    ctx = context or make_context()
    return asyncio.run(MockAgentProvider().plan_bug_repro(request, ctx, max_steps))


def checkpoint_kinds(plan: BugReproPlan) -> list[CheckpointKind]:
    return [item.kind for item in plan.checkpoints]


# ---------------------------------------------------------------------------
# Mock 计划的确定性与关键词翻译
# ---------------------------------------------------------------------------


def test_mock_plan_is_deterministic() -> None:
    """同一 request 两次规划必须逐字节一致（``model_dump()`` 相等）。"""
    request = make_request()
    context = make_context()

    first = mock_plan(request, context)
    second = mock_plan(request, context)

    assert first.model_dump() == second.model_dump()
    assert first.mock is True
    assert first.model_used == "mock"


@pytest.mark.parametrize(
    ("text", "tool", "target", "value"),
    [
        ("点击首页搜索入口", ToolName.CLICK_ELEMENT, "首页搜索入口", None),
        ("在搜索框输入 OpenHarmony", ToolName.INPUT_TEXT, "搜索框", "OpenHarmony"),
        ("在搜索框输入「知乎」", ToolName.INPUT_TEXT, "搜索框", "知乎"),
        ("向上滑动页面", ToolName.SWIPE, None, None),
        ("按下返回键", ToolName.BACK, None, None),
        ("退出当前页面", ToolName.BACK, None, None),
    ],
)
def test_mock_step_keyword_translation(text: str, tool: ToolName, target: str | None, value: str | None) -> None:
    step = mock_step_from_text(text, step_id="step-01")

    assert step.tool is tool
    assert step.target == target
    assert step.text == value


def test_mock_step_falls_back_to_one_second_wait() -> None:
    step = mock_step_from_text("确认页面已经加载完成", step_id="step-01")

    assert step.tool is ToolName.WAIT
    assert step.wait_seconds == 1.0


def test_mock_plan_translates_every_step_and_keeps_preconditions_first() -> None:
    request = make_request(
        preconditions=["已登录"],
        repro_steps_nl=[
            "点击首页搜索入口",
            "在搜索框输入 OpenHarmony",
            "向上滑动页面",
            "按下返回键",
            "退出当前页面",
            "确认页面已加载",
        ],
    )

    plan = mock_plan(request)
    steps = plan.steps

    assert [step.tool for step in steps] == [
        ToolName.WAIT,  # 前置条件「已登录」无关键词 → WAIT 1 秒，且排在最前
        ToolName.CLICK_ELEMENT,
        ToolName.INPUT_TEXT,
        ToolName.SWIPE,
        ToolName.BACK,
        ToolName.BACK,
        ToolName.WAIT,
        ToolName.FINISH,
    ]
    assert steps[0].instruction == "前置条件：已登录"
    assert steps[0].wait_seconds == 1.0
    assert steps[3].direction == "up"
    assert steps[6].wait_seconds == 1.0


def test_mock_plan_honours_explicit_wait_seconds() -> None:
    request = make_request(preconditions=[], repro_steps_nl=["等待 3 秒"])

    plan = mock_plan(request)

    assert plan.steps[0].tool is ToolName.WAIT
    assert plan.steps[0].wait_seconds == 3.0


@pytest.mark.parametrize("max_steps", [1, 2, 3, 4, 20])
def test_mock_plan_respects_max_steps(max_steps: int) -> None:
    request = make_request(
        preconditions=["已登录", "已进入首页"],
        repro_steps_nl=["点击首页搜索入口", "在搜索框输入 OpenHarmony", "等待 3 秒", "按下返回键"],
    )

    plan = mock_plan(request, max_steps=max_steps)

    assert 1 <= len(plan.steps) <= max_steps
    assert plan.steps[-1].tool is ToolName.FINISH


def test_mock_plan_only_uses_allowed_tools() -> None:
    request = make_request(
        symptom_kind="crash",
        preconditions=["已登录"],
        repro_steps_nl=["点击首页搜索入口", "在搜索框输入 OpenHarmony", "向上滑动页面", "退出当前页面", "确认页面"],
    )

    plan = mock_plan(request)

    used = {step.tool for step in plan.steps}
    assert used <= ALLOWED_TOOLS
    assert used == {
        ToolName.WAIT,
        ToolName.CLICK_ELEMENT,
        ToolName.INPUT_TEXT,
        ToolName.SWIPE,
        ToolName.BACK,
        ToolName.FINISH,
    }


def test_mock_plan_always_contains_the_expected_behaviour_checkpoint() -> None:
    request = make_request()

    plan = mock_plan(request)

    expected_drafts = [
        item
        for item in plan.checkpoints
        if item.kind is CheckpointKind.TEXT_CONTAINS and item.expected == request.expected
    ]
    assert expected_drafts, "由 request.expected 生成的期望行为检查点必须存在"
    assert expected_drafts[0].soft is False


# ---------------------------------------------------------------------------
# 症状哨兵表
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("symptom_kind", SYMPTOM_KINDS)
def test_symptom_sentinel_follows_the_plan_table(symptom_kind: str) -> None:
    sentinel = symptom_sentinel(symptom_kind, "期望文案")

    assert sentinel.kind is SENTINEL_KINDS[symptom_kind]
    assert sentinel.soft is False
    if symptom_kind in {"freeze", "unresponsive"}:
        assert sentinel.wait_seconds is not None
        assert sentinel.wait_seconds >= SYMPTOM_LONG_WAIT_SECONDS
        assert sentinel.target == "期望文案"
    if symptom_kind in {"functional", "other"}:
        assert sentinel.expected == "期望文案"


def test_symptom_sentinel_defaults_to_text_contains_for_unknown_kind() -> None:
    sentinel = symptom_sentinel("something-new", "期望文案")

    assert sentinel.kind is CheckpointKind.TEXT_CONTAINS
    assert sentinel.expected == "期望文案"


@pytest.mark.parametrize("symptom_kind", SYMPTOM_KINDS)
def test_mock_plan_carries_both_expected_behaviour_and_symptom_sentinel(symptom_kind: str) -> None:
    request = make_request(symptom_kind=symptom_kind)

    plan = mock_plan(request)
    kinds = checkpoint_kinds(plan)

    assert CheckpointKind.TEXT_CONTAINS in kinds
    assert SENTINEL_KINDS[symptom_kind] in kinds
    assert plan.symptom_checkpoint is not None
    assert plan.symptom_checkpoint.kind is SENTINEL_KINDS[symptom_kind]
    assert any(item == plan.symptom_checkpoint for item in plan.checkpoints)
    if symptom_kind == "crash":
        assert plan.symptom_checkpoint.expected == make_profile().bundle_name
    if symptom_kind in {"freeze", "unresponsive"}:
        assert plan.symptom_checkpoint.target == "首页搜索入口"


# ---------------------------------------------------------------------------
# 计划 → 用例 IR
# ---------------------------------------------------------------------------


def test_build_bug_repro_case_produces_a_valid_spec() -> None:
    request = make_request()
    profile = make_profile()
    plan = mock_plan(request, make_context(profile))

    built = build_bug_repro_case(CaseBuilder(), plan, request, profile)
    spec = built.spec

    assert spec.scenario is ScenarioKind.BUG_REPRODUCTION
    assert spec.bug_repro is not None
    assert spec.bug_repro.symptom_kind == "white_screen"
    assert spec.bug_repro.repro_steps_nl == list(request.repro_steps_nl)
    assert spec.title_zh == request.title
    assert spec.bundle_name == profile.bundle_name
    assert spec.main_ability == profile.main_ability
    assert spec.status == "active"
    assert spec.provenance.source_kind == "bug_report"
    assert spec.provenance.source_id == request.title
    assert spec.provenance.profile_target_app_id == profile.target_app_id
    assert [step.index for step in spec.steps] == list(range(1, len(spec.steps) + 1))
    assert all(step.title_zh for step in spec.steps)
    assert all(checkpoint.message_zh for step in spec.steps for checkpoint in step.checkpoints)
    assert spec.steps[0].title_zh == step_title_zh(spec.steps[0]) == "等待 1 秒"

    # 关键词翻译后的动作序列（inspect_screen/finish 被省略、open_app 进 setup）。
    assert [step.action for step in spec.steps] == [
        StepAction.WAIT,
        StepAction.CLICK,
        StepAction.INPUT_TEXT,
        StepAction.WAIT,
    ]
    click_locator = spec.steps[1].locator
    assert click_locator is not None
    assert (click_locator.kind, click_locator.value) == (LocatorKind.KEY, "p2_home_titlebar_search")
    assert spec.steps[3].wait_seconds == 3.0


@pytest.mark.parametrize("symptom_kind", SYMPTOM_KINDS)
def test_build_bug_repro_case_for_every_symptom_kind(symptom_kind: str) -> None:
    request = make_request(symptom_kind=symptom_kind)
    profile = make_profile()
    plan = mock_plan(request, make_context(profile))

    built = build_bug_repro_case(CaseBuilder(), plan, request, profile)
    checkpoints = [item for step in built.spec.steps for item in step.checkpoints]

    assert built.spec.scenario is ScenarioKind.BUG_REPRODUCTION
    assert any(item.kind is SENTINEL_KINDS[symptom_kind] for item in checkpoints)
    assert any(not item.soft for item in checkpoints)
    sentinel = next(item for item in checkpoints if item.kind is SENTINEL_KINDS[symptom_kind])
    if symptom_kind == "crash":
        assert sentinel.expected == profile.bundle_name
    if symptom_kind in {"white_screen", "layout"}:
        assert sentinel.anchors
        assert sentinel.anchors[0].kind is LocatorKind.KEY
    if symptom_kind in {"freeze", "unresponsive"}:
        assert sentinel.wait_seconds is not None and sentinel.wait_seconds >= SYMPTOM_LONG_WAIT_SECONDS
        assert sentinel.locator is not None
    if symptom_kind in {"functional", "other"}:
        assert sentinel.expected == request.expected


def test_case_builder_from_bug_repro_delegates_to_the_module_factory() -> None:
    """集成方走 ``CaseBuilder.from_bug_repro``（builder.py 的薄委托）必须得到同样的 IR。"""
    request = make_request()
    profile = make_profile()
    plan = mock_plan(request, make_context(profile))

    direct = build_bug_repro_case(CaseBuilder(), plan, request, profile)
    delegated = CaseBuilder().from_bug_repro(plan, request, profile)

    assert delegated.spec.scenario is direct.spec.scenario
    assert delegated.spec.bug_repro is not None
    assert delegated.spec.bug_repro.symptom == request.symptom
    assert [step.action for step in delegated.spec.steps] == [step.action for step in direct.spec.steps]
    assert len(delegated.spec.steps) == len(direct.spec.steps)
    assert delegated.explicit_assertions == direct.explicit_assertions


def test_build_bug_repro_case_synthesises_fallbacks_without_hard_checkpoints() -> None:
    """计划一个硬检查点都没给：合成 CURRENT_APP（崩溃哨兵）+ 入口页 PAGE_SIGNATURE，并记警告。"""
    request = make_request()
    profile = make_profile()
    plan = BugReproPlan(
        title_zh="",
        steps=[
            PlannedStep(
                step_id="step-01",
                instruction="点击首页搜索入口",
                tool=ToolName.CLICK_ELEMENT,
                target="首页搜索入口",
            ),
            PlannedStep(step_id="finish", instruction="结束任务", tool=ToolName.FINISH),
        ],
        checkpoints=[],
        symptom_checkpoint=None,
        notes="没有检查点",
    )

    built = build_bug_repro_case(CaseBuilder(), plan, request, profile)
    checkpoints = [item for step in built.spec.steps for item in step.checkpoints]
    kinds = [item.kind for item in checkpoints]

    assert any("硬检查点" in warning for warning in built.warnings)
    assert CheckpointKind.CURRENT_APP in kinds
    assert CheckpointKind.PAGE_SIGNATURE in kinds
    assert [step.index for step in built.spec.steps] == list(range(1, len(built.spec.steps) + 1))
    assert built.spec.title_zh == request.title
    assert built.counts["hard_checkpoints"] >= 2
    assert built.spec.status == "active"
    current_app = next(item for item in checkpoints if item.kind is CheckpointKind.CURRENT_APP)
    assert current_app.expected == profile.bundle_name
    page_signature = next(item for item in checkpoints if item.kind is CheckpointKind.PAGE_SIGNATURE)
    assert page_signature.anchors and page_signature.anchors[0].value == "p2_home_titlebar_search"
    assert built.replay_eligible is True


def test_build_bug_repro_case_without_profile_uses_placeholder_identity() -> None:
    request = make_request(symptom_kind="functional")
    plan = mock_bug_repro_plan(request, bundle_name="", stable_locator_names=(), max_steps=20)

    built = build_bug_repro_case(CaseBuilder(), plan, request, None)

    assert built.spec.bundle_name == PLACEHOLDER_BUNDLE
    assert any("TargetAppProfile" in warning for warning in built.warnings)
    assert built.replay_eligible is False
    assert built.purpose == "diagnostic"
    assert built.spec.status == "active"


def test_build_bug_repro_case_keeps_the_plan_sentinel_even_if_it_is_not_listed() -> None:
    """计划把哨兵只放在 ``symptom_checkpoint`` 里时，构建结果仍必须包含它。"""
    request = make_request(symptom_kind="crash")
    profile = make_profile()
    sentinel = symptom_sentinel("crash", request.expected).model_copy(update={"expected": profile.bundle_name})
    plan = BugReproPlan(
        title_zh=request.title,
        steps=[PlannedStep(step_id="finish", instruction="结束任务", tool=ToolName.FINISH)],
        checkpoints=[CheckpointDraft(kind=CheckpointKind.TEXT_CONTAINS, expected=request.expected)],
        symptom_checkpoint=sentinel,
    )

    built = build_bug_repro_case(CaseBuilder(), plan, request, profile)
    kinds = [item.kind for step in built.spec.steps for item in step.checkpoints]

    assert CheckpointKind.CURRENT_APP in kinds


def test_build_bug_repro_case_omits_agent_control_and_setup_tools() -> None:
    request = make_request()
    profile = make_profile()
    plan = BugReproPlan(
        title_zh=request.title,
        steps=[
            PlannedStep(step_id="step-00", instruction="启动应用", tool=ToolName.OPEN_APP),
            PlannedStep(step_id="step-01", instruction="检查首页", tool=ToolName.INSPECT_SCREEN),
            PlannedStep(
                step_id="step-02",
                instruction="点击坐标",
                tool=ToolName.CLICK_COORDINATE,
                coordinate=(540, 1200),
            ),
            PlannedStep(step_id="finish", instruction="结束任务", tool=ToolName.FINISH),
        ],
        checkpoints=[CheckpointDraft(kind=CheckpointKind.TEXT_CONTAINS, expected=request.expected)],
        symptom_checkpoint=symptom_sentinel("functional", request.expected),
    )

    built = build_bug_repro_case(CaseBuilder(), plan, request, profile)
    reasons = {item["tool"]: item["reason"] for item in built.omitted_actions}

    assert reasons["open_app"].startswith("OPEN_APP is replaced")
    assert reasons["inspect_screen"].endswith("is an agent-control action")
    assert reasons["finish"].endswith("is an agent-control action")
    assert [step.action for step in built.spec.steps] == [StepAction.CLICK]
    coordinate_step = built.spec.steps[0]
    assert coordinate_step.coordinate == (540, 1200)
    assert coordinate_step.locator is not None
    assert coordinate_step.locator.resolution_bound is not None
    assert coordinate_step.locator.warning
    assert built.counts["coordinate_fallbacks"] == 1


# ---------------------------------------------------------------------------
# 检查点草案 → CheckpointSpec 的字段映射
# ---------------------------------------------------------------------------


def test_checkpoint_from_draft_maps_kind_specific_fields() -> None:
    profile = make_profile()

    current_app = checkpoint_from_draft(
        CheckpointDraft(kind=CheckpointKind.CURRENT_APP), profile=profile, bundle_name=profile.bundle_name
    )
    assert current_app.expected == profile.bundle_name
    assert current_app.message_zh

    page_signature = checkpoint_from_draft(
        CheckpointDraft(kind=CheckpointKind.PAGE_SIGNATURE), profile=profile, bundle_name=profile.bundle_name
    )
    assert page_signature.anchors and page_signature.anchors[0].value == "p2_home_titlebar_search"

    element_exists = checkpoint_from_draft(
        CheckpointDraft(kind=CheckpointKind.ELEMENT_EXISTS, target="搜索输入框"),
        profile=profile,
        bundle_name=profile.bundle_name,
    )
    assert element_exists.locator is not None
    assert (element_exists.locator.kind, element_exists.locator.value) == (LocatorKind.ID, "search_input")

    property_equals = checkpoint_from_draft(
        CheckpointDraft(
            kind=CheckpointKind.PROPERTY_EQUALS,
            target="搜索输入框",
            property_name="text",
            expected="OpenHarmony",
        ),
        profile=profile,
        bundle_name=profile.bundle_name,
    )
    assert property_equals.property_name == "text"
    assert property_equals.expected == "OpenHarmony"

    screenshot = checkpoint_from_draft(
        CheckpointDraft(kind=CheckpointKind.SCREENSHOT_CAPTURED), profile=profile, bundle_name=profile.bundle_name
    )
    assert screenshot.expected == "final.jpeg"


def test_checkpoint_from_draft_falls_back_to_exact_text_locator() -> None:
    profile = make_profile()

    checkpoint = checkpoint_from_draft(
        CheckpointDraft(kind=CheckpointKind.ELEMENT_EXISTS, target="未知控件"),
        profile=profile,
        bundle_name=profile.bundle_name,
    )

    assert checkpoint.locator is not None
    assert checkpoint.locator.kind is LocatorKind.TEXT
    assert checkpoint.locator.value == "未知控件"


@pytest.mark.parametrize(
    "draft",
    [
        CheckpointDraft(kind=CheckpointKind.ELEMENT_EXISTS, target=""),
        CheckpointDraft(kind=CheckpointKind.ELEMENT_ABSENT, target=""),
        CheckpointDraft(kind=CheckpointKind.PAGE_SIGNATURE),
        CheckpointDraft(kind=CheckpointKind.PROPERTY_EQUALS, target="搜索输入框", expected="x"),
        CheckpointDraft(kind=CheckpointKind.PROPERTY_EQUALS, target="搜索输入框", property_name="not-a-prop"),
        CheckpointDraft(kind=CheckpointKind.TEXT_CONTAINS, expected=""),
        CheckpointDraft(kind=CheckpointKind.TOAST, expected=""),
    ],
)
def test_unsatisfiable_drafts_raise_chinese_value_error(draft: CheckpointDraft) -> None:
    with pytest.raises(BugReproBuildError) as excinfo:
        checkpoint_from_draft(draft, profile=None, bundle_name="")

    assert isinstance(excinfo.value, ValueError)
    assert excinfo.value.args[0]
    assert any("\u4e00" <= char <= "\u9fff" for char in str(excinfo.value)), "错误信息必须是中文"


def test_build_bug_repro_case_propagates_unsatisfiable_draft() -> None:
    """没有 Profile 时 page_signature 哨兵无法满足：构建必须失败而不是产出非法 spec。"""
    request = make_request(symptom_kind="white_screen")
    plan = mock_bug_repro_plan(request, bundle_name="", stable_locator_names=(), max_steps=20)

    with pytest.raises(BugReproBuildError, match="page_signature"):
        build_bug_repro_case(CaseBuilder(), plan, request, None)


# ---------------------------------------------------------------------------
# Provider 接入
# ---------------------------------------------------------------------------


def test_structured_output_for_bug_repro_is_prompted_not_tool_mode() -> None:
    """机制层不变量：缺陷复现的结构化输出必须是 ``PromptedOutput``（thinking 模型拒绝 tool-mode）。"""
    from pydantic_ai import PromptedOutput

    spec = OpenAICompatibleProvider._structured_output(BugReproPlan, "the defect reproduction plan")

    assert isinstance(spec, PromptedOutput)


def test_agent_provider_is_abstract() -> None:
    """基类有抽象方法，因此「基类默认实现」只能通过最小子类验证。"""
    with pytest.raises(TypeError):
        AgentProvider()  # type: ignore[abstract]


class _MinimalProvider(AgentProvider):
    """只实现三个抽象方法，用于验证 ``plan_bug_repro`` 的基类默认实现。"""

    name = "minimal"

    async def plan(self, task: str, context: PlanningContext, max_steps: int) -> PlanResult:
        return PlanResult(goal=task, steps=[], model_used=self.name)

    async def analyze(self, snapshot: ScreenSnapshot) -> VisionObservation | None:
        return None

    async def decide(
        self,
        step: PlannedStep,
        snapshot: ScreenSnapshot | None,
        feedback: str | None = None,
    ) -> ToolDecision:
        return ToolDecision(tool=step.tool)


def test_base_provider_plan_bug_repro_returns_none() -> None:
    provider = _MinimalProvider()

    result = asyncio.run(provider.plan_bug_repro(make_request(), make_context(), 5))

    assert result is None


def test_bug_repro_prompt_covers_the_plan_rules() -> None:
    """``BUG_REPRO_PROMPT`` 必须写清允许工具、禁词、期望行为语义与哨兵表。"""
    assert "追加在" not in BUG_REPRO_PROMPT  # 提示词本身不应自我描述拼接方式
    for tool in sorted(item.value for item in ALLOWED_TOOLS):
        assert tool in BUG_REPRO_PROMPT
    for forbidden in ("登录", "支付", "删除", "授权"):
        assert forbidden in BUG_REPRO_PROMPT
    for marker in ("期望行为", "symptom_checkpoint", "symptom_kind", "current_app", "page_signature"):
        assert marker in BUG_REPRO_PROMPT
    assert "1-2 个原子步骤" in BUG_REPRO_PROMPT


def _openai_settings() -> Settings:
    return Settings(
        _env_file=None,
        openai_api_key="test-key",
        agent_model="test-model",
        agent_vision_model="test-vision-model",
        agent_provider="openai",
    )


class _StubRunResult:
    """``Agent.run`` 的最小替身：只暴露被调用方读取的 ``output``。"""

    def __init__(self, output: Any) -> None:
        self.output = output


def test_openai_provider_plan_bug_repro_is_stubbed_and_aligned(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAI 路径必须走 ``_structured_output`` + ``PLANNING_PROMPT + BUG_REPRO_PROMPT``，全程离线。"""
    request = make_request()
    context = make_context()
    marker = object()
    seen: dict[str, Any] = {}

    def fake_structured_output(output_type: type[Any], description: str) -> Any:
        seen["output_type"] = output_type
        seen["description"] = description
        return marker

    monkeypatch.setattr(
        OpenAICompatibleProvider,
        "_structured_output",
        staticmethod(fake_structured_output),
    )
    monkeypatch.setattr(OpenAICompatibleProvider, "_model", lambda self, vision=False: object())

    import pydantic_ai

    class _StubAgent:
        def __init__(self, model: Any, **kwargs: Any) -> None:
            seen["model"] = model
            seen["agent_output_type"] = kwargs.get("output_type")
            seen["system_prompt"] = kwargs.get("system_prompt")

        async def run(self, prompt: str, **kwargs: Any) -> _StubRunResult:
            seen["prompt"] = prompt
            return _StubRunResult(
                BugReproPlan(
                    title_zh=request.title,
                    steps=[
                        PlannedStep(
                            step_id="step-01",
                            instruction="在搜索框输入 OpenHarmony",
                            tool=ToolName.INPUT_TEXT,
                            target="搜索输入框",
                            text="OpenHarmony",
                        ),
                        PlannedStep(
                            step_id="step-02",
                            instruction="点击搜索按钮",
                            tool=ToolName.CLICK_ELEMENT,
                            target="搜索按钮",
                        ),
                        PlannedStep(step_id="step-03", instruction="等待搜索结果", tool=ToolName.WAIT),
                        PlannedStep(step_id="step-04", instruction="结束任务", tool=ToolName.FINISH),
                    ],
                    checkpoints=[
                        CheckpointDraft(kind=CheckpointKind.TEXT_CONTAINS, expected=request.expected),
                    ],
                    symptom_checkpoint=symptom_sentinel("white_screen", request.expected),
                )
            )

    monkeypatch.setattr(pydantic_ai, "Agent", _StubAgent)
    provider = OpenAICompatibleProvider(_openai_settings())

    plan = asyncio.run(provider.plan_bug_repro(request, context, max_steps=10))

    assert seen["output_type"] is BugReproPlan
    assert seen["agent_output_type"] is marker, "结构化输出必须经 _structured_output（PromptedOutput）"
    assert PLANNING_PROMPT in seen["system_prompt"]
    assert BUG_REPRO_PROMPT in seen["system_prompt"]
    assert context.bundle_name in seen["prompt"]
    assert request.title in seen["prompt"]
    assert plan is not None
    assert plan.mock is False
    assert plan.model_used == provider.name
    # align_plan_with_task 去掉了臆造的搜索提交步骤并保证以 finish 收尾。
    assert [step.tool for step in plan.steps] == [ToolName.INPUT_TEXT, ToolName.FINISH]


def test_openai_provider_plan_bug_repro_returns_none_without_model_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleProvider(_openai_settings())
    provider.settings = _openai_settings().model_copy(update={"agent_model": ""})

    assert asyncio.run(provider.plan_bug_repro(make_request(), make_context(), 5)) is None


def test_align_plan_with_task_still_caps_steps_for_bug_repro() -> None:
    """OpenAI 路径复用 ``align_plan_with_task``：确认它在缺陷复现场景下的截断语义。"""
    request = make_request()
    steps = [
        PlannedStep(step_id="step-01", instruction="点击首页搜索入口", tool=ToolName.CLICK_ELEMENT, target="首页"),
        PlannedStep(step_id="step-02", instruction="在搜索框输入 OpenHarmony", tool=ToolName.INPUT_TEXT, text="x"),
        PlannedStep(step_id="step-03", instruction="结束任务", tool=ToolName.FINISH),
    ]

    aligned = align_plan_with_task(f"缺陷标题：{request.title}", steps, max_steps=2)

    assert len(aligned) == 2
    assert aligned[-1].tool is ToolName.FINISH
