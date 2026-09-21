"""``cases/stress.py`` 的单元测试（改进计划 C2 / C3）。

覆盖：``StressRequest`` 的字段/默认值/边界与载荷自洽、``steps_from_exploration_actions``
的逐 kind 翻译（并与 orchestrator 合成 + ``CaseBuilder.from_trace`` 的产物对照）、四种
``StressKind`` 的循环体与检查点、timeout 公式边界、``needs_explicit_loop`` 与发射引擎选择矩阵、
以及产出 spec 的 IR 不变式。全部为纯静态构建，不接触真机。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from harmony_test_agent.cases.builder import CaseBuilder
from harmony_test_agent.cases.spec import (
    CaseProvenance,
    CheckpointKind,
    CheckpointSpec,
    LocatorSpec,
    StepAction,
    TestCaseSpec,
    TestStepSpec,
)
from harmony_test_agent.cases.stress import (
    DEFAULT_FIXED_INPUT_TEXT,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    STRESS_COUNT_KEYS,
    StressRequest,
    build_stress_case,
    steps_from_exploration_actions,
)
from harmony_test_agent.discovery.explorer import ExplorationAction
from harmony_test_agent.generation.standalone import StandaloneEmitter
from harmony_test_agent.generation.xdevice_case import (
    XDEVICE_MAIN_METHOD,
    XDEVICE_STRESS_METHOD,
    XDeviceEmitter,
    resolve_method,
    uses_explicit_loop,
)
from harmony_test_agent.models import (
    ActionResult,
    ConfidenceLevel,
    LocatorCandidate,
    LocatorKind,
    RunTrace,
    ScenarioKind,
    StableLocator,
    StressKind,
    TargetAppProfile,
    ToolName,
)

FIXED_INPUT_TEXT = "OpenHarmony"
ENTRY_PAGE = "page-entry"
DETAIL_PAGE = "page-detail"
PLACEHOLDER_BUNDLE = "com.example.app"

_CJK = re.compile(r"[\u4e00-\u9fff]")
_CASE_ID_PATTERN = re.compile(r"^case-[A-Za-z0-9T.Z-]+-[a-f0-9]{6}$")
_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

STRESS_REQUEST_FIELDS = (
    "target",
    "kind",
    "iterations",
    "duration_budget_seconds",
    "body",
    "case_id",
    "steps",
    "click_target",
    "swipe_pair",
    "fail_break",
    "fail_times",
    "sample_memory_every",
    "memory_growth_threshold_kb",
    "inter_iteration_wait_seconds",
    "device_id",
    "auto_execute",
    "title_zh",
)

KIND_KWARGS: dict[StressKind, dict[str, object]] = {
    StressKind.REPEAT_CLICK: {"click_target": "发布按钮"},
    StressKind.CONTINUOUS_SWIPE: {},
    StressKind.PAGE_ENTER_EXIT: {},
    StressKind.SOAK: {"duration_budget_seconds": 600},
}


# ---------------------------------------------------------------------------
# fixture 工具
# ---------------------------------------------------------------------------


def _stable_locator(name: str, **kwargs: object) -> StableLocator:
    kwargs.setdefault("page_signature", ENTRY_PAGE)
    kwargs.setdefault("observed_rounds", 4)
    kwargs.setdefault("unique_match_rounds", 3)
    kwargs.setdefault("confidence", ConfidenceLevel.HIGH)
    return StableLocator(name=name, **kwargs)


def _inventory() -> list[StableLocator]:
    """入口页两个稳定定位器 + 一个 ``unique_match_rounds`` 更高的详情页定位器（锚点过滤用）。"""
    return [
        _stable_locator("首页信息流", key="feed_list", text="首页", observed_rounds=8, unique_match_rounds=7),
        _stable_locator("发布按钮", key="publish_button", text="发布", observed_rounds=5, unique_match_rounds=4),
        _stable_locator(
            "详情标题", key="detail_title", page_signature=DETAIL_PAGE, observed_rounds=9, unique_match_rounds=9
        ),
    ]


def _core_flow_actions() -> list[dict[str, object]]:
    """与 orchestrator 落盘格式一致的 ``profile.core_flows[0]["steps"]``（JSON mode）。"""
    return [
        ExplorationAction(
            action_id="a-click",
            kind="click",
            element_id="el-1",
            locator_kind="key",
            locator_value="publish_button",
            target_text="发布",
            coordinate=(100, 200),
        ).model_dump(mode="json"),
        ExplorationAction(
            action_id="a-input",
            kind="input",
            element_id="el-2",
            locator_kind="text",
            locator_value="说点什么",
            target_text="说点什么",
            coordinate=(200, 320),
        ).model_dump(mode="json"),
        ExplorationAction(
            action_id="a-swipe",
            kind="swipe",
            element_id="el-3",
            locator_kind="coordinate",
            locator_value="(300, 600)",
            coordinate=(300, 600),
            direction="up",
        ).model_dump(mode="json"),
        ExplorationAction(action_id="a-back", kind="back").model_dump(mode="json"),
    ]


def _profile(*, locators: list[StableLocator] | None = None, steps: list[dict[str, object]] | None = None):
    """一个非 verified（draft）的 Profile：不触发 verified 的额外门禁，构建只记警告。"""
    return TargetAppProfile(
        target_app_id="zhihu-plus",
        display_name="知乎++",
        bundle_name="com.zhihu.ohos",
        main_ability="EntryAbility",
        launch_strategy={"kind": "hdc_aa_start", "wait_seconds": 2},
        test_data_strategy={"fixed_input_text": FIXED_INPUT_TEXT},
        stable_locator_inventory=_inventory() if locators is None else locators,
        core_flows=[
            {
                "pages": [ENTRY_PAGE],
                "page_ids": ["entry"],
                "steps": _core_flow_actions() if steps is None else steps,
            }
        ],
    )


def _base_case() -> TestCaseSpec:
    """``body=existing_case`` 用的源用例。"""
    return TestCaseSpec(
        case_id="case-20260101T000000Z-abcdef",
        slug="base-case",
        title_zh="源用例",
        scenario=ScenarioKind.CORE_FLOW,
        bundle_name="com.zhihu.ohos",
        steps=[
            TestStepSpec(
                step_id="s1",
                index=1,
                action=StepAction.CLICK,
                title_zh="点击「首页」",
                locator=LocatorSpec(kind=LocatorKind.KEY, value="feed_list", target_label="首页"),
            ),
            TestStepSpec(
                step_id="s2",
                index=2,
                action=StepAction.BACK,
                title_zh="按下返回键",
                checkpoints=[
                    CheckpointSpec(
                        kind=CheckpointKind.CURRENT_APP,
                        message_zh="检查点：前台应用仍为「com.zhihu.ohos」",
                        expected="com.zhihu.ohos",
                    )
                ],
            ),
        ],
        provenance=CaseProvenance(source_kind="manual", source_id="manual-1"),
    )


def _build(kind: StressKind, **kwargs: object):
    profile = kwargs.pop("profile", _profile())
    builder = kwargs.pop("builder", CaseBuilder())
    base_spec = kwargs.pop("base_spec", None)
    return build_stress_case(builder, StressRequest(kind=kind, **kwargs), profile, base_spec)  # type: ignore[arg-type]


def _projection(step: TestStepSpec) -> tuple[object, ...]:
    """步骤的可比较投影：不含 ``step_id``（压测侧不带 orchestrator 的页序号）与 Profile 证据。"""
    locator = step.locator
    return (
        step.action,
        locator.kind if locator is not None else None,
        locator.value if locator is not None else "",
        locator.target_label if locator is not None else "",
        locator.match if locator is not None else None,
        locator.coordinate if locator is not None else None,
        locator.resolution_bound if locator is not None else None,
        step.text,
        step.direction,
        step.coordinate,
        step.index,
    )


def _orchestrator_actions(actions: list[dict[str, object]], fixed_input_text: str) -> list[ActionResult]:
    """对照实现：复刻 ``orchestrator.py::_profile_validation_trace`` 的 pending 动作合成（766-807 行）。"""
    produced: list[ActionResult] = []
    for position, action in enumerate(actions):
        kind = str(action["kind"])
        tool = {
            "click": ToolName.CLICK_ELEMENT,
            "input": ToolName.CLICK_COORDINATE,
            "swipe": ToolName.SWIPE,
            "back": ToolName.BACK,
        }[kind]
        params: dict[str, object] = {"target": action.get("element_id") or action.get("target_text")}
        if action.get("coordinate"):
            params["coordinate"] = action["coordinate"]
        if kind == "input":
            params["text"] = fixed_input_text
        if kind == "swipe":
            params["direction"] = action.get("direction") or "up"
        locator = (
            LocatorCandidate(kind=LocatorKind(str(action["locator_kind"])), value=str(action["locator_value"]))
            if action.get("locator_kind") != "coordinate" and action.get("locator_value")
            else None
        )
        if kind == "input" and action.get("coordinate"):
            produced.append(
                ActionResult(
                    step_id=f"orchestrator-focus-{position}",
                    tool=ToolName.CLICK_COORDINATE,
                    params={"coordinate": action["coordinate"]},
                    success=True,
                )
            )
            tool = ToolName.INPUT_TEXT
        produced.append(
            ActionResult(
                step_id=f"orchestrator-action-{position}",
                tool=tool,
                params=params,
                success=True,
                locator=locator,
            )
        )
    return produced


# ---------------------------------------------------------------------------
# C2：请求契约
# ---------------------------------------------------------------------------


def test_stress_request_field_list_matches_the_plan() -> None:
    assert tuple(StressRequest.model_fields) == STRESS_REQUEST_FIELDS
    request = StressRequest()
    assert request.target is None
    assert request.kind is StressKind.REPEAT_CLICK
    assert request.iterations == 20
    assert request.duration_budget_seconds is None
    assert request.body == "profile_core_flow"
    assert request.case_id is None
    assert request.steps == []
    assert request.click_target is None
    assert request.swipe_pair is True
    assert request.fail_break is True
    assert request.fail_times == 0
    assert request.sample_memory_every == 0
    assert request.memory_growth_threshold_kb is None
    assert request.inter_iteration_wait_seconds == 0.5
    assert request.device_id is None
    assert request.auto_execute is False
    assert request.title_zh is None
    assert DEFAULT_FIXED_INPUT_TEXT == "OpenHarmony"


@pytest.mark.parametrize(
    "payload",
    [
        {"iterations": 0},
        {"iterations": 2001},
        {"duration_budget_seconds": 9},
        {"duration_budget_seconds": 7201},
        {"fail_times": -1},
        {"sample_memory_every": -1},
        {"sample_memory_every": 501},
        {"inter_iteration_wait_seconds": -0.1},
        {"inter_iteration_wait_seconds": 10.1},
        {"kind": "not-a-kind"},
        {"body": "not-a-body"},
    ],
)
def test_stress_request_bounds_are_enforced(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        StressRequest(**payload)  # type: ignore[arg-type]


def test_inline_steps_requires_a_non_empty_step_list() -> None:
    with pytest.raises(ValidationError):
        StressRequest(body="inline_steps", steps=[])
    step = TestStepSpec(step_id="s1", index=1, action=StepAction.BACK, title_zh="按下返回键")
    assert StressRequest(body="inline_steps", steps=[step]).steps == [step]


def test_existing_case_requires_case_id() -> None:
    with pytest.raises(ValidationError):
        StressRequest(body="existing_case")
    with pytest.raises(ValidationError):
        StressRequest(body="existing_case", case_id="   ")
    assert StressRequest(body="existing_case", case_id="case-1").case_id == "case-1"


# ---------------------------------------------------------------------------
# ExplorationAction dict → IR 步骤
# ---------------------------------------------------------------------------


def test_steps_from_exploration_actions_translates_each_kind() -> None:
    steps = steps_from_exploration_actions(_core_flow_actions(), FIXED_INPUT_TEXT)

    assert [step.action for step in steps] == [
        StepAction.CLICK,
        StepAction.CLICK,
        StepAction.INPUT_TEXT,
        StepAction.SWIPE,
        StepAction.BACK,
    ]
    assert [step.index for step in steps] == [1, 2, 3, 4, 5]

    click, focus, text_input, swipe, back = steps
    assert (click.locator.kind, click.locator.value, click.locator.target_label) == (
        LocatorKind.KEY,
        "publish_button",
        "el-1",
    )
    assert focus.coordinate == (200, 320)
    assert focus.locator.kind is LocatorKind.COORDINATE
    assert focus.locator.resolution_bound == (0, 0)
    assert focus.locator.warning
    assert (text_input.locator.kind, text_input.locator.value, text_input.text) == (
        LocatorKind.TEXT,
        "说点什么",
        FIXED_INPUT_TEXT,
    )
    assert swipe.direction == "up"
    assert back.action is StepAction.BACK


def test_steps_from_exploration_actions_indices_are_strictly_increasing() -> None:
    steps = steps_from_exploration_actions(_core_flow_actions(), FIXED_INPUT_TEXT)
    assert [step.index for step in steps] == list(range(1, len(steps) + 1))


def test_steps_from_exploration_actions_falls_back_to_text_locator() -> None:
    """``locator_kind=coordinate`` 的 click 与 ``from_trace`` 一样退化为 BY.text(element_id)。"""
    action = ExplorationAction(
        action_id="coordinate-click",
        kind="click",
        element_id="el-9",
        locator_kind="coordinate",
        locator_value="(10, 20)",
        coordinate=(10, 20),
    ).model_dump(mode="json")
    steps = steps_from_exploration_actions([action], FIXED_INPUT_TEXT)
    assert len(steps) == 1
    assert steps[0].locator is not None
    assert (steps[0].locator.kind, steps[0].locator.value) == (LocatorKind.TEXT, "el-9")
    assert steps[0].coordinate is None


def test_steps_from_exploration_actions_defaults_swipe_direction_and_type_text() -> None:
    type_text = ExplorationAction(
        action_id="tt",
        kind="click",
        element_id="el-tt",
        locator_kind="type_text",
        locator_value="Button|发布",
    ).model_dump(mode="json")
    bad_swipe = {"action_id": "sw", "kind": "swipe", "direction": "sideways"}
    steps = steps_from_exploration_actions([type_text, bad_swipe], FIXED_INPUT_TEXT)
    assert steps[0].locator is not None
    assert (steps[0].locator.kind, steps[0].locator.value, steps[0].locator.target_label) == (
        LocatorKind.TYPE_TEXT,
        "Button|发布",
        "el-tt",
    )
    assert steps[1].action is StepAction.SWIPE
    assert steps[1].direction == "up"


def test_steps_from_exploration_actions_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        steps_from_exploration_actions([{"action_id": "x", "kind": "long_press"}], FIXED_INPUT_TEXT)


def test_translation_matches_orchestrator_action_synthesis() -> None:
    """与 ``_profile_validation_trace`` 合成 ``ActionResult`` 后经 ``from_trace`` 的产物逐一对照。"""
    actions = _core_flow_actions()
    profile = _profile()
    trace = RunTrace(
        run_id="run-profile-validation",
        target_app_id="zhihu-plus",
        task="Profile 准入回放",
        device_id="device-1",
        actions=_orchestrator_actions(actions, FIXED_INPUT_TEXT),
    )
    reference = CaseBuilder(min_observed_rounds=1).from_trace(trace, profile)
    assert [step.action for step in reference.spec.steps] == [
        StepAction.CLICK,
        StepAction.CLICK,
        StepAction.INPUT_TEXT,
        StepAction.SWIPE,
        StepAction.BACK,
    ]
    translated = steps_from_exploration_actions(actions, FIXED_INPUT_TEXT)
    assert [_projection(step) for step in reference.spec.steps] == [_projection(step) for step in translated]


# ---------------------------------------------------------------------------
# C3：四种 kind 的循环体与检查点
# ---------------------------------------------------------------------------


def test_repeat_click_body_and_per_iteration_checkpoints() -> None:
    result = _build(StressKind.REPEAT_CLICK, click_target="发布按钮")
    stress = result.spec.stress
    assert stress is not None
    assert [step.action for step in stress.body_steps] == [StepAction.CLICK]
    locator = stress.body_steps[0].locator
    assert locator is not None
    assert (locator.kind, locator.value) == (LocatorKind.KEY, "publish_button")
    assert locator.evidence is not None
    assert (locator.evidence.unique_match_rounds, locator.evidence.source) == (4, "profile_stable_locator")

    checkpoints = stress.per_iteration_checkpoints
    assert [checkpoint.kind for checkpoint in checkpoints] == [
        CheckpointKind.ELEMENT_EXISTS,
        CheckpointKind.CURRENT_APP,
    ]
    assert checkpoints[0].locator is not None
    assert checkpoints[0].locator.value == "feed_list"
    assert checkpoints[1].expected == "com.zhihu.ohos"
    assert all(not checkpoint.soft for checkpoint in checkpoints)


def test_repeat_click_target_resolution_uses_exact_then_contains() -> None:
    exact = _build(StressKind.REPEAT_CLICK, click_target="发布")
    contains = _build(StressKind.REPEAT_CLICK, click_target="信息流")
    missing = _build(StressKind.REPEAT_CLICK, click_target="完全不存在")

    exact_locator = exact.spec.stress.body_steps[0].locator
    contains_locator = contains.spec.stress.body_steps[0].locator
    missing_locator = missing.spec.stress.body_steps[0].locator
    assert (exact_locator.kind, exact_locator.value) == (LocatorKind.KEY, "publish_button")
    assert (contains_locator.kind, contains_locator.value) == (LocatorKind.KEY, "feed_list")
    assert (missing_locator.kind, missing_locator.value) == (LocatorKind.TEXT, "完全不存在")
    assert missing_locator.warning
    assert any("退化为文本精确匹配" in warning for warning in missing.warnings)


def test_repeat_click_without_target_raises() -> None:
    with pytest.raises(ValueError):
        _build(StressKind.REPEAT_CLICK, profile=_profile(steps=[]))


def test_continuous_swipe_emits_an_up_down_pair() -> None:
    result = _build(StressKind.CONTINUOUS_SWIPE)
    stress = result.spec.stress
    assert [(step.action, step.direction) for step in stress.body_steps] == [
        (StepAction.SWIPE, "up"),
        (StepAction.SWIPE, "down"),
    ]
    assert [checkpoint.kind for checkpoint in stress.per_iteration_checkpoints] == [
        CheckpointKind.ELEMENT_EXISTS,
        CheckpointKind.CURRENT_APP,
    ]


def test_continuous_swipe_without_pair_keeps_one_direction() -> None:
    result = _build(StressKind.CONTINUOUS_SWIPE, swipe_pair=False)
    stress = result.spec.stress
    assert [(step.action, step.direction) for step in stress.body_steps] == [(StepAction.SWIPE, "up")]
    assert any("swipe_pair=False" in warning for warning in result.warnings)


def test_page_enter_exit_is_core_flow_plus_back_steps() -> None:
    result = _build(StressKind.PAGE_ENTER_EXIT)
    stress = result.spec.stress
    enter = steps_from_exploration_actions(_core_flow_actions(), FIXED_INPUT_TEXT)
    clicks = sum(1 for step in enter if step.action is StepAction.CLICK)

    assert clicks == 2
    assert [step.action for step in stress.body_steps] == [
        StepAction.CLICK,
        StepAction.CLICK,
        StepAction.INPUT_TEXT,
        StepAction.SWIPE,
        StepAction.BACK,
        StepAction.BACK,
        StepAction.BACK,
    ]
    assert len(stress.body_steps) == len(enter) + clicks
    assert [step.index for step in stress.body_steps] == list(range(1, len(stress.body_steps) + 1))
    assert stress.body_steps[0].locator.evidence is not None

    assert len(stress.per_iteration_checkpoints) == 1
    checkpoint = stress.per_iteration_checkpoints[0]
    assert checkpoint.kind is CheckpointKind.PAGE_SIGNATURE
    assert checkpoint.page_path == "entry"
    assert [anchor.value for anchor in checkpoint.anchors] == ["feed_list", "publish_button"]
    assert all(anchor.warning is None for anchor in checkpoint.anchors)


def test_page_enter_exit_anchors_ignore_other_pages() -> None:
    """详情页定位器的 ``unique_match_rounds`` 更高，但入口页锚点必须来自入口页。"""
    result = _build(StressKind.PAGE_ENTER_EXIT)
    values = [anchor.value for anchor in result.spec.stress.per_iteration_checkpoints[0].anchors]
    assert values == ["feed_list", "publish_button"]
    assert "detail_title" not in values


def test_page_enter_exit_without_anchors_is_a_draft() -> None:
    result = _build(StressKind.PAGE_ENTER_EXIT, profile=_profile(locators=[]))
    assert result.spec.stress.per_iteration_checkpoints == []
    assert result.spec.status == "draft"
    assert result.replay_eligible is False
    assert result.purpose == "diagnostic"
    assert any("stable_locator_inventory" in warning for warning in result.warnings)


def test_page_enter_exit_requires_a_core_flow() -> None:
    with pytest.raises(ValueError):
        _build(StressKind.PAGE_ENTER_EXIT, profile=_profile(steps=[]))


def test_soak_without_duration_budget_raises() -> None:
    with pytest.raises(ValueError, match="duration_budget_seconds"):
        _build(StressKind.SOAK)


def test_soak_body_from_existing_case() -> None:
    base = _base_case()
    result = _build(
        StressKind.SOAK,
        body="existing_case",
        case_id=base.case_id,
        base_spec=base,
        duration_budget_seconds=600,
    )
    body = result.spec.stress.body_steps
    assert [step.action for step in body] == [StepAction.CLICK, StepAction.BACK]
    assert body[0] is not base.steps[0]
    assert body[0].locator is not None and body[0].locator.value == "feed_list"
    assert result.spec.provenance.source_id == base.case_id
    assert result.counts["source_actions"] == 2
    assert result.counts["source_assertions"] == 1
    assert result.spec.timeout_seconds == 760


def test_soak_body_from_existing_case_requires_base_spec() -> None:
    with pytest.raises(ValueError):
        _build(StressKind.SOAK, body="existing_case", case_id="case-1", duration_budget_seconds=60)


def test_soak_body_from_inline_steps() -> None:
    step = TestStepSpec(step_id="inline-1", index=1, action=StepAction.CLICK, title_zh="", text="发布")
    result = _build(
        StressKind.SOAK,
        body="inline_steps",
        steps=[step],
        duration_budget_seconds=600,
    )
    body = result.spec.stress.body_steps
    assert len(body) == 1
    assert body[0] is not step
    assert body[0].step_id == "inline-1"
    assert body[0].title_zh == "点击页面"
    assert result.counts["source_actions"] == 1


def test_soak_body_from_profile_core_flow() -> None:
    result = _build(StressKind.SOAK, duration_budget_seconds=60)
    body = result.spec.stress.body_steps
    assert [step.action for step in body] == [
        StepAction.CLICK,
        StepAction.CLICK,
        StepAction.INPUT_TEXT,
        StepAction.SWIPE,
        StepAction.BACK,
    ]
    assert result.counts["source_actions"] == 4
    assert [checkpoint.kind for checkpoint in result.spec.stress.per_iteration_checkpoints] == [
        CheckpointKind.ELEMENT_EXISTS,
        CheckpointKind.CURRENT_APP,
    ]


def test_soak_without_any_body_source_raises() -> None:
    with pytest.raises(ValueError):
        _build(StressKind.SOAK, duration_budget_seconds=60, profile=_profile(steps=[]))


# ---------------------------------------------------------------------------
# timeout 公式
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("iterations", "duration", "expected"),
    [
        (1, None, MIN_TIMEOUT_SECONDS),
        (47, None, MIN_TIMEOUT_SECONDS),
        (48, None, 300),
        (49, None, 305),
        (100, None, 560),
        (1428, None, MAX_TIMEOUT_SECONDS),
        (1429, None, MAX_TIMEOUT_SECONDS),
        (2000, None, MAX_TIMEOUT_SECONDS),
    ],
)
def test_timeout_formula_boundaries(iterations: int, duration: int | None, expected: int) -> None:
    result = _build(StressKind.CONTINUOUS_SWIPE, iterations=iterations, duration_budget_seconds=duration)
    assert result.spec.timeout_seconds == expected


@pytest.mark.parametrize(("duration", "expected"), [(600, 710), (7200, MAX_TIMEOUT_SECONDS)])
def test_timeout_formula_includes_the_soak_budget(duration: int, expected: int) -> None:
    result = _build(StressKind.SOAK, iterations=10, duration_budget_seconds=duration)
    assert result.spec.timeout_seconds == expected


def test_timeout_floor_and_ceiling_constants() -> None:
    assert (MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS) == (300, 7200)


# ---------------------------------------------------------------------------
# needs_explicit_loop / 发射引擎选择矩阵
# ---------------------------------------------------------------------------


def test_engine_method_names() -> None:
    assert (XDEVICE_STRESS_METHOD, XDEVICE_MAIN_METHOD) == ("test_stress_iteration", "test_main")


@pytest.mark.parametrize(
    ("kind", "sample_every", "explicit"),
    [
        (StressKind.REPEAT_CLICK, 0, False),
        (StressKind.REPEAT_CLICK, 10, True),
        (StressKind.CONTINUOUS_SWIPE, 0, False),
        (StressKind.CONTINUOUS_SWIPE, 25, True),
        (StressKind.PAGE_ENTER_EXIT, 0, False),
        (StressKind.PAGE_ENTER_EXIT, 5, True),
        (StressKind.SOAK, 0, True),
        (StressKind.SOAK, 10, True),
    ],
)
def test_engine_selection_matrix(kind: StressKind, sample_every: int, explicit: bool) -> None:
    result = _build(kind, sample_memory_every=sample_every, **KIND_KWARGS[kind])
    stress = result.spec.stress
    assert stress.needs_explicit_loop is explicit
    assert uses_explicit_loop(stress) is explicit
    assert resolve_method(result.spec) == (XDEVICE_MAIN_METHOD if explicit else XDEVICE_STRESS_METHOD)


def test_counting_kind_carries_no_deadline() -> None:
    stress = _build(StressKind.REPEAT_CLICK, click_target="发布按钮").spec.stress
    assert stress.duration_budget_seconds is None
    assert stress.needs_explicit_loop is False
    assert resolve_method(_build(StressKind.REPEAT_CLICK, click_target="发布按钮").spec) == XDEVICE_STRESS_METHOD


# ---------------------------------------------------------------------------
# 产出 spec 的 IR 契约
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(StressKind))
def test_produced_spec_satisfies_the_ir_invariants(kind: StressKind) -> None:
    result = _build(kind, **KIND_KWARGS[kind])
    spec = result.spec

    assert spec.scenario is ScenarioKind.STRESS
    assert spec.stress is not None
    assert spec.stress.kind is kind
    assert spec.stress.iterations == 20
    assert spec.status == "active"
    assert spec.steps == []
    assert _CASE_ID_PATTERN.fullmatch(spec.case_id)
    assert _SLUG_PATTERN.fullmatch(spec.slug)
    assert MIN_TIMEOUT_SECONDS <= spec.timeout_seconds <= MAX_TIMEOUT_SECONDS

    assert spec.stress.body_steps
    assert [step.index for step in spec.stress.body_steps] == list(range(1, len(spec.stress.body_steps) + 1))
    for step in spec.stress.body_steps:
        assert step.title_zh and _CJK.search(step.title_zh)
        for checkpoint in step.checkpoints:
            assert checkpoint.message_zh and _CJK.search(checkpoint.message_zh)
    for checkpoint in spec.stress.per_iteration_checkpoints:
        assert checkpoint.message_zh and _CJK.search(checkpoint.message_zh)
        assert not checkpoint.soft

    assert set(result.counts) == set(STRESS_COUNT_KEYS)
    assert result.counts["generated_actions"] == len(spec.stress.body_steps)
    assert result.counts["omitted_actions"] == 0
    assert result.counts["failed_actions"] == 0
    assert result.replay_eligible is True
    assert result.purpose == "acceptance"
    assert result.explicit_assertions >= 1
    assert result.incomplete_reasons == []


def test_counts_keys_match_the_live_builder() -> None:
    trace = RunTrace(
        run_id="run-live",
        target_app_id="zhihu-plus",
        task="计数键对照",
        device_id="device-1",
        actions=[
            ActionResult(
                step_id="click",
                tool=ToolName.CLICK_ELEMENT,
                success=True,
                params={"target": "发布"},
                locator=LocatorCandidate(kind=LocatorKind.KEY, value="publish_button"),
            )
        ],
    )
    live_counts = CaseBuilder().from_trace(trace, _profile()).counts
    stress_counts = _build(StressKind.REPEAT_CLICK, click_target="发布按钮").counts
    assert set(live_counts) == set(STRESS_COUNT_KEYS)
    assert set(stress_counts) == set(STRESS_COUNT_KEYS)


def test_request_metadata_is_carried_into_the_spec() -> None:
    result = _build(
        StressKind.CONTINUOUS_SWIPE,
        title_zh="滑动压测专用",
        device_id="device-42",
        iterations=50,
        fail_break=False,
        fail_times=2,
        inter_iteration_wait_seconds=1.5,
        memory_growth_threshold_kb=2048,
    )
    spec = result.spec
    assert spec.title_zh == "滑动压测专用"
    assert spec.device_sn == "device-42"
    assert spec.stress.iterations == 50
    assert spec.stress.fail_break is False
    assert spec.stress.fail_times == 2
    assert spec.stress.continues_fail is False
    assert spec.stress.inter_iteration_wait_seconds == 1.5
    assert spec.stress.memory_growth_threshold_kb == 2048
    assert spec.timeout_seconds == 310
    assert spec.setup.stop_app_first is True
    assert spec.setup.start_app is True
    assert spec.setup.startup_wait_seconds == 2.0
    assert spec.teardown.capture_final_screenshot is True


def test_missing_profile_falls_back_to_placeholder_identity() -> None:
    result = _build(StressKind.CONTINUOUS_SWIPE, profile=None)
    assert result.spec.bundle_name == PLACEHOLDER_BUNDLE
    assert result.spec.status == "active"
    assert result.replay_eligible is False
    assert result.purpose == "diagnostic"
    assert result.incomplete_reasons
    assert any("占位身份" in warning for warning in result.warnings)


def test_non_verified_profile_is_flagged_in_warnings() -> None:
    result = _build(StressKind.REPEAT_CLICK, click_target="发布按钮")
    assert any("非 verified" in warning for warning in result.warnings)


# ---------------------------------------------------------------------------
# 发射端到端（IR → 两种引擎的源码）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(StressKind))
def test_produced_spec_renders_through_both_engines(kind: StressKind, tmp_path: Path) -> None:
    """四种 kind 的产物都必须能被独立脚本与 devicetest 两个 emitter 直接渲染。"""
    spec = _build(kind, **KIND_KWARGS[kind]).spec
    standalone = StandaloneEmitter().render(spec, run_id="stress-render", device_id="device-1")
    # 独立脚本始终显式循环（deadline + 每轮内存采样）。
    assert "stress_stats" in standalone.python_text
    assert "for iteration in range(1, ITERATIONS + 1):" in standalone.python_text
    assert "deadline = time.monotonic() + DEADLINE_SECONDS" in standalone.python_text

    artifact = XDeviceEmitter().render(spec, tmp_path / kind.value)
    assert artifact.method == resolve_method(spec)
    assert artifact.testfile["tests"][0]["name"] == artifact.method
    assert artifact.timeout_seconds == spec.timeout_seconds
    if uses_explicit_loop(spec.stress):
        assert "for _iteration in range(1, ITERATIONS + 1):" in artifact.python_text
        assert "@loop(times=ITERATIONS" not in artifact.python_text
    else:
        assert "@loop(times=ITERATIONS" in artifact.python_text
