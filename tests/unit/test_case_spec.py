"""``cases/spec.py`` 的 IR 不变式测试（计划 A1 + 「新增测试文件与必测断言」表）。

覆盖七条不变式：STRESS ⟺ ``stress``、BUG_REPRODUCTION ⟹ ``bug_repro``、索引严格 1..N、
悬空 ``param_ref``、非 draft 至少一个硬 checkpoint、TOAST ⟹ ``listen_toast``、
``CURRENT_APP`` / ``PAGE_SIGNATURE`` / 坐标 locator 的非空约束；外加 ``extra="forbid"``、
JSON 往返稳定与枚举的 ``StrEnum`` 语义。
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

import pytest
from pydantic import ValidationError

from harmony_test_agent.cases.spec import (
    BugReproSpec,
    CaseProvenance,
    CheckpointKind,
    CheckpointSpec,
    DataSetSpec,
    LocatorSpec,
    MatchMode,
    ScenarioKind,
    SetupSpec,
    StepAction,
    StressKind,
    StressSpec,
    TeardownSpec,
    TestCaseSpec,
    TestParamSpec,
    TestStepSpec,
)
from harmony_test_agent.models import LocatorKind

# ---------------------------------------------------------------------------
# 工厂：让每个用例只写它真正关心的字段
# ---------------------------------------------------------------------------


def make_step(index: int, action: StepAction = StepAction.CLICK, **overrides: Any) -> TestStepSpec:
    """构造一个最小可用的步骤；``index`` 由调用方显式给出以便测索引不变式。"""
    payload: dict[str, Any] = {
        "step_id": f"step-{index}",
        "index": index,
        "action": action,
        "title_zh": f"步骤 {index}",
    }
    payload.update(overrides)
    return TestStepSpec(**payload)


def text_locator(label: str = "搜索") -> LocatorSpec:
    return LocatorSpec(kind=LocatorKind.TEXT, value=label, target_label=label)


def coordinate_locator(
    *,
    bound: tuple[int, int] | None = (1080, 2340),
    warning: str | None = "坐标回退：源快照 1080x2340",
) -> LocatorSpec:
    return LocatorSpec(
        kind=LocatorKind.COORDINATE,
        value="坐标 (10, 20)",
        coordinate=(10, 20),
        resolution_bound=bound,
        warning=warning,
    )


def make_checkpoint(kind: CheckpointKind = CheckpointKind.ELEMENT_EXISTS, **overrides: Any) -> CheckpointSpec:
    payload: dict[str, Any] = {"kind": kind, "message_zh": f"检查点：{kind.value}"}
    payload.update(overrides)
    return CheckpointSpec(**payload)


def hard_checkpoint(**overrides: Any) -> CheckpointSpec:
    """默认硬检查点：元素存在断言（不触发任何额外不变式）。"""
    payload: dict[str, Any] = {"locator": text_locator("首页"), "soft": False}
    payload.update(overrides)
    return make_checkpoint(**payload)


def make_stress(**overrides: Any) -> StressSpec:
    payload: dict[str, Any] = {
        "kind": StressKind.REPEAT_CLICK,
        "iterations": 10,
        "body_steps": [make_step(1)],
        "per_iteration_checkpoints": [hard_checkpoint()],
    }
    payload.update(overrides)
    return StressSpec(**payload)


def make_spec(**overrides: Any) -> TestCaseSpec:
    """默认是一个合法的 active 核心流程用例（带一个硬检查点）。"""
    payload: dict[str, Any] = {
        "case_id": "case-20250101T000000Z-abc123",
        "slug": "search-flow",
        "title_zh": "搜索流程",
        "scenario": ScenarioKind.CORE_FLOW,
        "status": "active",
        "bundle_name": "com.example.app",
        "provenance": CaseProvenance(source_kind="manual", source_id="unit-test"),
        "steps": [make_step(1, checkpoints=[hard_checkpoint()])],
    }
    payload.update(overrides)
    return TestCaseSpec(**payload)


def make_stress_case(**overrides: Any) -> TestCaseSpec:
    payload: dict[str, Any] = {
        "scenario": ScenarioKind.STRESS,
        "steps": [],
        "stress": make_stress(),
    }
    payload.update(overrides)
    return make_spec(**payload)


def make_bug_repro_spec() -> BugReproSpec:
    return BugReproSpec(
        symptom="进入搜索页后整屏空白",
        symptom_kind="white_screen",
        preconditions=["已登录"],
        repro_steps_nl=["点击首页搜索入口", "等待 3 秒"],
        expected="显示搜索输入框与热词列表",
        actual="白屏，无任何控件",
    )


# ---------------------------------------------------------------------------
# 不变式 1：STRESS ⟺ stress 存在
# ---------------------------------------------------------------------------


def test_stress_scenario_without_stress_spec_is_rejected() -> None:
    with pytest.raises(ValidationError, match="stress scenario requires a stress spec"):
        make_spec(scenario=ScenarioKind.STRESS, steps=[])


def test_stress_spec_on_non_stress_scenario_is_rejected() -> None:
    with pytest.raises(ValidationError, match="stress scenario requires a stress spec"):
        make_spec(scenario=ScenarioKind.SMOKE, stress=make_stress())


def test_stress_case_with_stress_spec_is_accepted() -> None:
    case = make_stress_case()
    assert case.scenario == ScenarioKind.STRESS
    assert case.stress is not None


# ---------------------------------------------------------------------------
# 不变式 2：BUG_REPRODUCTION ⟹ bug_repro 存在
# ---------------------------------------------------------------------------


def test_bug_reproduction_without_bug_repro_is_rejected() -> None:
    with pytest.raises(ValidationError, match="bug_reproduction scenario requires a bug_repro spec"):
        make_spec(scenario=ScenarioKind.BUG_REPRODUCTION, status="draft")


def test_bug_reproduction_with_bug_repro_is_accepted() -> None:
    case = make_spec(scenario=ScenarioKind.BUG_REPRODUCTION, bug_repro=make_bug_repro_spec())
    assert case.bug_repro is not None
    assert case.bug_repro.symptom_kind == "white_screen"


# ---------------------------------------------------------------------------
# 不变式 3：非压测至少一步；index 严格 1..N
# ---------------------------------------------------------------------------


def test_non_stress_case_requires_at_least_one_step() -> None:
    with pytest.raises(ValidationError, match="non-stress case requires at least one step"):
        make_spec(steps=[])


def test_stress_case_may_have_empty_top_level_steps() -> None:
    make_stress_case(steps=[])


def test_step_index_must_be_one_based_contiguous() -> None:
    with pytest.raises(ValidationError, match=r"steps\[0\].index must be 1, got 2"):
        make_spec(steps=[make_step(2)])


def test_step_index_gap_is_rejected() -> None:
    steps = [make_step(1), make_step(3)]
    with pytest.raises(ValidationError, match=r"steps\[1\].index must be 2, got 3"):
        make_spec(steps=steps)


def test_step_index_one_based_is_accepted() -> None:
    steps = [make_step(1, checkpoints=[hard_checkpoint()]), make_step(2), make_step(3)]
    case = make_spec(steps=steps)
    assert [step.index for step in case.steps] == [1, 2, 3]


def test_stress_body_index_is_validated() -> None:
    with pytest.raises(ValidationError, match=r"stress\.body_steps\[0\].index must be 1, got 2"):
        make_stress_case(stress=make_stress(body_steps=[make_step(2)]))


def test_setup_pre_steps_index_is_validated() -> None:
    with pytest.raises(ValidationError, match=r"setup\.pre_steps\[0\].index must be 1, got 3"):
        make_spec(setup=SetupSpec(pre_steps=[make_step(3)]))


def test_teardown_post_steps_index_is_validated() -> None:
    with pytest.raises(ValidationError, match=r"teardown\.post_steps\[0\].index must be 1, got 2"):
        make_spec(teardown=TeardownSpec(post_steps=[make_step(2)]))


def test_renumber_repairs_indices() -> None:
    """``renumber()`` 是 builder 修索引的正式入口，修完必须能被重新校验通过。"""
    case = make_spec(steps=[make_step(1, checkpoints=[hard_checkpoint()]), make_step(2)])
    case.steps[1].index = 7
    case.renumber()
    assert [step.index for step in case.steps] == [1, 2]
    TestCaseSpec.model_validate(case.model_dump())


# ---------------------------------------------------------------------------
# 不变式 4：param_ref 必须能在 params 里找到
# ---------------------------------------------------------------------------


def test_dangling_param_ref_is_rejected() -> None:
    with pytest.raises(ValidationError, match=r"references unknown param 'keyword'"):
        make_spec(steps=[make_step(1, action=StepAction.INPUT_TEXT, text="x", param_ref="keyword")])


def test_dangling_param_ref_in_stress_body_is_rejected() -> None:
    stress = make_stress(body_steps=[make_step(1, param_ref="keyword")])
    with pytest.raises(ValidationError, match=r"references unknown param 'keyword'"):
        make_stress_case(stress=stress)


def test_declared_param_ref_is_accepted() -> None:
    case = make_spec(
        params=[TestParamSpec(name="keyword", description_zh="搜索词")],
        steps=[
            make_step(
                1,
                action=StepAction.INPUT_TEXT,
                text=None,
                param_ref="keyword",
                checkpoints=[hard_checkpoint()],
            )
        ],
    )
    assert case.params[0].name == "keyword"


# ---------------------------------------------------------------------------
# 不变式 5：非 draft 至少一个硬检查点
# ---------------------------------------------------------------------------


def test_non_draft_case_without_hard_checkpoint_is_rejected() -> None:
    soft_only = make_checkpoint(soft=True, locator=text_locator("首页"))
    with pytest.raises(ValidationError, match="a non-draft case requires at least one non-soft checkpoint"):
        make_spec(steps=[make_step(1, checkpoints=[soft_only])])


def test_draft_case_without_any_checkpoint_is_accepted() -> None:
    case = make_spec(status="draft", steps=[make_step(1)])
    assert case.status == "draft"


def test_stress_hard_checkpoint_may_live_in_per_iteration_checkpoints() -> None:
    case = make_stress_case(stress=make_stress(per_iteration_checkpoints=[hard_checkpoint()]), status="active")
    assert case.stress is not None
    assert len([cp for cp in case.stress.per_iteration_checkpoints if not cp.soft]) == 1


# ---------------------------------------------------------------------------
# 不变式 6：TOAST 检查点 ⟹ setup.listen_toast
# ---------------------------------------------------------------------------


def test_toast_checkpoint_requires_listen_toast() -> None:
    toast = make_checkpoint(CheckpointKind.TOAST, expected="已收藏")
    with pytest.raises(ValidationError, match="toast checkpoint requires setup.listen_toast"):
        make_spec(steps=[make_step(1, checkpoints=[toast, hard_checkpoint()])])


def test_toast_checkpoint_with_listen_toast_is_accepted() -> None:
    toast = make_checkpoint(CheckpointKind.TOAST, expected="已收藏")
    case = make_spec(
        setup=SetupSpec(listen_toast=True),
        steps=[make_step(1, checkpoints=[toast, hard_checkpoint()])],
    )
    assert case.setup.listen_toast is True


# ---------------------------------------------------------------------------
# 不变式 7：CURRENT_APP.expected / PAGE_SIGNATURE.anchors / 坐标 locator
# ---------------------------------------------------------------------------


def test_current_app_checkpoint_requires_expected() -> None:
    with pytest.raises(ValidationError, match="current_app checkpoint requires a non-empty expected bundle name"):
        make_checkpoint(CheckpointKind.CURRENT_APP, expected="")


def test_current_app_checkpoint_with_expected_is_accepted() -> None:
    checkpoint = make_checkpoint(CheckpointKind.CURRENT_APP, expected="com.example.app")
    assert checkpoint.expected == "com.example.app"


def test_page_signature_checkpoint_requires_anchor() -> None:
    with pytest.raises(ValidationError, match="page_signature checkpoint requires at least one anchor"):
        make_checkpoint(CheckpointKind.PAGE_SIGNATURE, anchors=[])


def test_page_signature_checkpoint_with_anchor_is_accepted() -> None:
    checkpoint = make_checkpoint(CheckpointKind.PAGE_SIGNATURE, anchors=[text_locator("首页")])
    assert len(checkpoint.anchors) == 1


def test_property_equals_checkpoint_requires_property_and_locator() -> None:
    with pytest.raises(ValidationError, match="property_equals checkpoint requires property_name"):
        make_checkpoint(CheckpointKind.PROPERTY_EQUALS, locator=text_locator("开关"))
    with pytest.raises(ValidationError, match="property_equals checkpoint requires a locator"):
        make_checkpoint(CheckpointKind.PROPERTY_EQUALS, property_name="checked")


def test_coordinate_locator_requires_resolution_bound() -> None:
    with pytest.raises(ValidationError, match="coordinate locator requires a resolution bound and warning"):
        coordinate_locator(bound=None)


def test_coordinate_locator_requires_warning() -> None:
    with pytest.raises(ValidationError, match="coordinate locator requires a resolution bound and warning"):
        coordinate_locator(warning="")


def test_coordinate_locator_requires_coordinate() -> None:
    with pytest.raises(ValidationError, match="coordinate locator requires a coordinate"):
        LocatorSpec(kind=LocatorKind.COORDINATE, resolution_bound=(1080, 2340), warning="回退")


def test_coordinate_locator_with_bound_and_warning_is_accepted() -> None:
    locator = coordinate_locator()
    assert locator.resolution_bound == (1080, 2340)
    assert locator.warning


def test_unresolved_locator_kind_is_rejected() -> None:
    """SPATIAL / VLM_BBOX 必须在 build 期解析为 coordinate 后才能进 IR。"""
    with pytest.raises(ValidationError, match="must be resolved to coordinate"):
        LocatorSpec(kind=LocatorKind.SPATIAL, value="右上角按钮")


def test_post_construction_checkpoint_edit_is_still_rejected() -> None:
    """构造后原地改写检查点（绕过模型校验器）在进入用例时仍会被拦下。"""
    checkpoint = make_checkpoint(CheckpointKind.CURRENT_APP, expected="com.example.app")
    checkpoint.expected = ""
    with pytest.raises(ValidationError, match="current_app checkpoint requires a non-empty expected bundle name"):
        make_spec(steps=[make_step(1, checkpoints=[checkpoint])])


def test_checkpoint_invariant_is_rechecked_on_reload() -> None:
    """用例库读盘（model_validate）路径必须重跑不变式，不能信任已落盘的脏 IR。"""
    case = make_spec(
        steps=[make_step(1, checkpoints=[make_checkpoint(CheckpointKind.CURRENT_APP, expected="com.example.app")])]
    )
    case.steps[0].checkpoints[0].expected = ""
    with pytest.raises(ValidationError, match="current_app checkpoint requires a non-empty expected bundle name"):
        TestCaseSpec.model_validate(case.model_dump())


def test_post_construction_coordinate_edit_is_still_rejected() -> None:
    """坐标 locator 的解析边界是硬约束：构造后被清空，进入用例时同样被拒。"""
    locator = coordinate_locator()
    locator.resolution_bound = None
    with pytest.raises(ValidationError, match="coordinate locator requires a resolution bound and warning"):
        make_spec(steps=[make_step(1, locator=locator, checkpoints=[hard_checkpoint()])])


# ---------------------------------------------------------------------------
# extra="forbid" 与 JSON 往返
# ---------------------------------------------------------------------------


def test_unknown_case_field_is_rejected() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        make_spec(unknown_field=1)


def test_unknown_step_field_is_rejected() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        make_step(1, unknown_field=1)


def test_unknown_locator_field_is_rejected() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        LocatorSpec(kind=LocatorKind.TEXT, value="搜索", unknown_field=1)


def test_unknown_checkpoint_field_is_rejected() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        make_checkpoint(unknown_field=1)


def test_json_round_trip_is_stable_for_core_flow_case() -> None:
    toast = make_checkpoint(CheckpointKind.TOAST, expected="已收藏", soft=True)
    case = make_spec(
        status="active",
        tags=["smoke", "profile:verified"],
        device_sn="ABC123",
        timeout_seconds=600,
        params=[TestParamSpec(name="keyword", type="string", default="华为")],
        datasets=[DataSetSpec(name="默认", values={"keyword": "华为"})],
        setup=SetupSpec(listen_toast=True, pre_steps=[make_step(1, action=StepAction.WAIT, wait_seconds=1.0)]),
        steps=[
            make_step(
                1,
                action=StepAction.INPUT_TEXT,
                text="华为",
                param_ref="keyword",
                locator=text_locator("搜索框"),
                checkpoints=[toast, hard_checkpoint()],
            ),
            make_step(2, action=StepAction.CLICK, locator=coordinate_locator()),
        ],
        teardown=TeardownSpec(stop_app=True, capture_final_screenshot=True),
        bug_repro=make_bug_repro_spec(),
    )

    dumped = case.model_dump(mode="json")
    assert json.loads(json.dumps(dumped, ensure_ascii=False)) == dumped
    restored = TestCaseSpec.model_validate(dumped)
    assert restored == case
    assert restored.model_dump(mode="json") == dumped


def test_json_round_trip_is_stable_for_stress_case() -> None:
    case = make_stress_case(
        tags=["stress"],
        stress=make_stress(
            kind=StressKind.SOAK,
            iterations=50,
            duration_budget_seconds=600,
            sample_memory_every=10,
            inter_iteration_wait_seconds=1.0,
            body_steps=[make_step(1, action=StepAction.SWIPE, direction="up")],
        ),
    )
    dumped = case.model_dump(mode="json")
    restored = TestCaseSpec.model_validate(json.loads(json.dumps(dumped, ensure_ascii=False)))
    assert restored == case
    assert restored.stress is not None
    assert restored.stress.duration_budget_seconds == 600
    assert restored.stress.body_steps[0].direction == "up"


# ---------------------------------------------------------------------------
# 枚举语义
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("enum_cls", "member_values"),
    [
        (StepAction, ["click", "key_event", "noop_comment"]),
        (MatchMode, ["equals", "contains", "starts_with", "ends_with", "regexp"]),
        (CheckpointKind, ["element_exists", "toast", "page_signature"]),
        (ScenarioKind, ["core_flow", "bug_reproduction", "exploratory", "stress", "smoke"]),
        (StressKind, ["repeat_click", "continuous_swipe", "page_enter_exit", "soak"]),
    ],
)
def test_ir_enums_are_str_enums(enum_cls: type[StrEnum], member_values: list[str]) -> None:
    assert issubclass(enum_cls, StrEnum)
    for value in member_values:
        member = enum_cls(value)
        assert isinstance(member, str)
        assert member.value == value
        assert member == value
        assert json.loads(json.dumps(member)) == value


def test_scenario_accepts_plain_string() -> None:
    case = make_spec(scenario="smoke")
    assert case.scenario is ScenarioKind.SMOKE
    assert case.model_dump(mode="json")["scenario"] == "smoke"
