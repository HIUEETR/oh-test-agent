"""``cases/safety.py`` 的门禁测试（计划 C4 + 「新增测试文件与必测断言」表）。

覆盖：永久禁词/凭据词（步骤 text / target_label / key / ``bug_repro.*``）、KEY_EVENT 白名单、
iterations / duration / wait 上限、坐标缺 ``resolution_bound`` 的纵深防御、
压测循环体生命周期动作、``coordinate-risk`` 标签副作用，以及「干净 spec 返回空列表」。

测试全程离线：不触碰设备、不联网，只构造 IR 并调用纯函数。
"""

from __future__ import annotations

from typing import Any

import pytest

from harmony_test_agent.cases.safety import (
    ALLOWED_KEY_EVENTS,
    COORDINATE_RISK_TAG,
    MAX_DURATION_BUDGET_SECONDS,
    MAX_STEP_WAIT_SECONDS,
    canonical_key_event,
    validate_case_spec,
)
from harmony_test_agent.cases.spec import (
    BugReproSpec,
    CaseProvenance,
    CheckpointKind,
    CheckpointSpec,
    LocatorSpec,
    ScenarioKind,
    SetupSpec,
    StepAction,
    StressKind,
    StressSpec,
    TeardownSpec,
    TestCaseSpec,
    TestStepSpec,
)
from harmony_test_agent.config import Settings
from harmony_test_agent.models import LocatorKind
from harmony_test_agent.runtime.safety import SafetyPolicy

MAX_ITERATIONS = Settings().stress_max_iterations

# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def make_step(index: int, action: StepAction = StepAction.CLICK, **overrides: Any) -> TestStepSpec:
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


def coordinate_locator() -> LocatorSpec:
    return LocatorSpec(
        kind=LocatorKind.COORDINATE,
        value="坐标 (10, 20)",
        coordinate=(10, 20),
        resolution_bound=(1080, 2340),
        warning="坐标回退：源快照 1080x2340",
    )


def hard_checkpoint(**overrides: Any) -> CheckpointSpec:
    payload: dict[str, Any] = {"kind": CheckpointKind.ELEMENT_EXISTS, "message_zh": "检查点：首页可见"}
    payload.update(overrides)
    return CheckpointSpec(locator=text_locator("首页"), **payload)


def make_stress(**overrides: Any) -> StressSpec:
    payload: dict[str, Any] = {
        "kind": StressKind.REPEAT_CLICK,
        "iterations": 10,
        "body_steps": [make_step(1, locator=text_locator("列表"))],
        "per_iteration_checkpoints": [hard_checkpoint()],
    }
    payload.update(overrides)
    return StressSpec(**payload)


def make_spec(**overrides: Any) -> TestCaseSpec:
    payload: dict[str, Any] = {
        "case_id": "case-20250101T000000Z-abc123",
        "slug": "search-flow",
        "title_zh": "搜索流程",
        "scenario": ScenarioKind.CORE_FLOW,
        "status": "active",
        "bundle_name": "com.example.app",
        "provenance": CaseProvenance(source_kind="manual", source_id="unit-test"),
        "steps": [make_step(1, locator=text_locator("搜索框"), checkpoints=[hard_checkpoint()])],
    }
    payload.update(overrides)
    return TestCaseSpec(**payload)


def make_stress_case(**overrides: Any) -> TestCaseSpec:
    payload: dict[str, Any] = {"scenario": ScenarioKind.STRESS, "steps": [], "stress": make_stress()}
    payload.update(overrides)
    return make_spec(**payload)


def check(case: TestCaseSpec, *, max_iterations: int = MAX_ITERATIONS) -> list[str]:
    return validate_case_spec(case, max_iterations=max_iterations)


# ---------------------------------------------------------------------------
# 基线
# ---------------------------------------------------------------------------


def test_clean_core_flow_spec_returns_empty_list() -> None:
    assert check(make_spec()) == []


def test_clean_stress_spec_returns_empty_list_and_is_not_tagged() -> None:
    case = make_stress_case()
    assert check(case) == []
    assert COORDINATE_RISK_TAG not in case.tags


def test_gated_words_are_not_rechecked_at_this_layer() -> None:
    """门控词（提交/确认/登录…）录制时已由 SafetyPolicy/DcShellPolicy 查过，这里不得误伤。"""
    case = make_spec(
        steps=[
            make_step(
                1,
                action=StepAction.INPUT_TEXT,
                text="输入关键词并提交",
                locator=text_locator("登录入口"),
                checkpoints=[hard_checkpoint()],
            )
        ]
    )
    assert check(case) == []


# ---------------------------------------------------------------------------
# 规则 1：永久禁词 / 凭据词
# ---------------------------------------------------------------------------


def test_forbidden_word_in_step_text_is_reported() -> None:
    case = make_spec(
        steps=[make_step(1, action=StepAction.INPUT_TEXT, text="帮我执行支付流程", checkpoints=[hard_checkpoint()])]
    )
    violations = check(case)
    assert len(violations) == 1
    assert "支付" in violations[0]
    assert "text" in violations[0]
    assert "steps[0]" in violations[0]


def test_credential_word_in_step_text_is_reported() -> None:
    case = make_spec(
        steps=[make_step(1, action=StepAction.INPUT_TEXT, text="输入密码后继续", checkpoints=[hard_checkpoint()])]
    )
    violations = check(case)
    assert len(violations) == 1
    assert "密码" in violations[0]


def test_forbidden_word_in_target_label_is_reported() -> None:
    locator = LocatorSpec(kind=LocatorKind.TEXT, value="设置项", target_label="卸载应用")
    case = make_spec(steps=[make_step(1, locator=locator, checkpoints=[hard_checkpoint()])])
    violations = check(case)
    assert len(violations) == 1
    assert "卸载" in violations[0]
    assert "locator.target_label" in violations[0]


def test_forbidden_word_in_step_key_is_reported() -> None:
    case = make_spec(steps=[make_step(1, key="delete", checkpoints=[hard_checkpoint()])])
    violations = check(case)
    assert len(violations) == 1
    assert "delete" in violations[0]
    assert "key" in violations[0]


def test_english_forbidden_word_is_case_insensitive() -> None:
    case = make_spec(
        steps=[
            make_step(
                1,
                action=StepAction.INPUT_TEXT,
                text="Tap UNINSTALL then confirm",
                checkpoints=[hard_checkpoint()],
            )
        ]
    )
    violations = check(case)
    assert len(violations) == 1
    assert "uninstall" in violations[0]


def test_forbidden_words_in_bug_repro_strings_are_reported() -> None:
    bug = BugReproSpec(
        symptom="点击删除按钮后应用闪退",
        symptom_kind="crash",
        preconditions=["已完成支付"],
        repro_steps_nl=["输入密码并回车"],
        expected="显示卸载完成提示",
        actual="实际执行了 remove 操作",
    )
    case = make_spec(scenario=ScenarioKind.BUG_REPRODUCTION, bug_repro=bug)
    violations = check(case)
    joined = "\n".join(violations)
    assert len(violations) == 5
    for field in (
        "bug_repro.symptom",
        "bug_repro.preconditions[0]",
        "bug_repro.repro_steps_nl[0]",
        "bug_repro.expected",
        "bug_repro.actual",
    ):
        assert field in joined
    for term in ("删除", "支付", "密码", "卸载", "remove"):
        assert term in joined


def test_forbidden_word_in_stress_loop_body_is_reported() -> None:
    body = [make_step(1, action=StepAction.INPUT_TEXT, text="先清除数据", locator=text_locator("搜索"))]
    case = make_stress_case(stress=make_stress(body_steps=body))
    violations = check(case)
    assert len(violations) == 1
    assert "stress.body_steps[0]" in violations[0]
    assert "清除数据" in violations[0]


def test_custom_policy_word_list_is_honoured() -> None:
    """``policy`` 参数必须真的生效（证明复用运行期词表而不是内建副本）。"""
    case = make_spec(steps=[make_step(1, text="点击支付按钮", checkpoints=[hard_checkpoint()])])
    custom = SafetyPolicy(always_blocked=("自定义禁词",))
    assert validate_case_spec(case, policy=custom, max_iterations=MAX_ITERATIONS) == []
    assert check(case) != []


# ---------------------------------------------------------------------------
# 规则 2：KEY_EVENT 白名单
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ALLOWED_KEY_EVENTS)
def test_allowed_key_events_are_clean(key: str) -> None:
    case = make_spec(steps=[make_step(1, action=StepAction.KEY_EVENT, key=key, checkpoints=[hard_checkpoint()])])
    assert check(case) == []


def test_key_event_comparison_is_case_insensitive() -> None:
    case = make_spec(steps=[make_step(1, action=StepAction.KEY_EVENT, key="back", checkpoints=[hard_checkpoint()])])
    assert check(case) == []


@pytest.mark.parametrize("key", ["Power", "power", "Backspace", ""])
def test_key_event_outside_whitelist_is_reported(key: str) -> None:
    case = make_spec(steps=[make_step(1, action=StepAction.KEY_EVENT, key=key, checkpoints=[hard_checkpoint()])])
    violations = check(case)
    assert len(violations) == 1
    assert "KEY_EVENT" in violations[0]


def test_key_event_with_missing_key_field_is_reported() -> None:
    case = make_spec(steps=[make_step(1, action=StepAction.KEY_EVENT, checkpoints=[hard_checkpoint()])])
    violations = check(case)
    assert len(violations) == 1
    assert "KEY_EVENT" in violations[0]


def test_key_event_with_forbidden_word_reports_both_rules() -> None:
    case = make_spec(steps=[make_step(1, action=StepAction.KEY_EVENT, key="Delete", checkpoints=[hard_checkpoint()])])
    violations = check(case)
    assert len(violations) == 2
    assert any("delete" in item for item in violations)
    assert any("KEY_EVENT" in item for item in violations)


# ---------------------------------------------------------------------------
# 规则 2 补充：canonical_key_event 与三张按键表的一致性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Enter", "Enter"),
        ("enter", "Enter"),
        ("ENTER", "Enter"),
        (" Enter ", "Enter"),
        ("Home", "Home"),
        ("home", "Home"),
        ("Volume Up", "VolumeUp"),
        ("volume-up", "VolumeUp"),
        ("volumeUp", "VolumeUp"),
        ("volume_up", "VolumeUp"),
        ("VolumeUp", "VolumeUp"),
        ("VolumeDown", "VolumeDown"),
        ("volume_down", "VolumeDown"),
        ("volume-down", "VolumeDown"),
    ],
)
def test_canonical_key_event_normalizes_whitelisted_keys(raw: str, expected: str) -> None:
    assert canonical_key_event(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["Back", "back", "BACK", "backspace", "Power", "power", "Menu", "KEYCODE_ENTER", "return", ""],
)
def test_canonical_key_event_rejects_back_and_non_whitelisted_keys(raw: str) -> None:
    # Back 由调用方映射成 StepAction.BACK（渲染 driver.go_back()），有意不在此表内。
    assert canonical_key_event(raw) is None


@pytest.mark.parametrize(
    "raw",
    ["home", "enter", "Volume Up", "volume-up", "volumeUp", "volume_down", "Home", "ENTER"],
)
def test_canonical_key_event_output_passes_validate_case_spec(raw: str) -> None:
    """canonical 的输出必须能通过 KEY_EVENT 白名单校验（不产生违规）。"""
    key = canonical_key_event(raw)
    assert key is not None
    case = make_spec(steps=[make_step(1, action=StepAction.KEY_EVENT, key=key, checkpoints=[hard_checkpoint()])])
    assert check(case) == []


def test_key_event_tables_agree() -> None:
    """三张按键表（safety / standalone / xdevice）对 canonical 输出必须一致命中。"""
    from harmony_test_agent.generation.standalone import KEYCODE_BY_KEY
    from harmony_test_agent.generation.xdevice_case import _KEY_CODES

    canonical = {canonical_key_event(k) for k in ("home", "enter", "Volume Up", "volume-up", "volumeUp", "volume_down")}
    assert None not in canonical
    for key in canonical:
        assert key.casefold() in {item.casefold() for item in ALLOWED_KEY_EVENTS}
        assert key.lower().replace(" ", "").replace("-", "_") in KEYCODE_BY_KEY
        assert key.replace("-", "_").lower() in _KEY_CODES


# ---------------------------------------------------------------------------
# 规则 3：压测边界
# ---------------------------------------------------------------------------


def test_iterations_over_cap_is_reported() -> None:
    case = make_stress_case(stress=make_stress(iterations=50))
    violations = check(case, max_iterations=20)
    assert len(violations) == 1
    assert "iterations=50" in violations[0]
    assert "20" in violations[0]


def test_iterations_at_cap_is_clean() -> None:
    case = make_stress_case(stress=make_stress(iterations=20))
    assert check(case, max_iterations=20) == []


def test_iterations_over_settings_default_cap_is_reported() -> None:
    """``Settings.stress_max_iterations``（默认 2000）必须能直接当上限用。"""
    case = make_stress_case(stress=make_stress(iterations=MAX_ITERATIONS + 1))
    violations = check(case)
    assert len(violations) == 1
    assert f"超过上限 {MAX_ITERATIONS}" in violations[0]


def test_duration_budget_over_cap_is_reported() -> None:
    case = make_stress_case(stress=make_stress(duration_budget_seconds=7200))
    assert case.stress is not None
    # StressSpec 自身 le=7200，这里模拟构造后被就地改写/上限未来放宽后的兜底。
    case.stress.duration_budget_seconds = 7201
    violations = check(case)
    assert len(violations) == 1
    assert "7201" in violations[0]
    assert str(MAX_DURATION_BUDGET_SECONDS) in violations[0]


def test_duration_budget_at_cap_is_clean() -> None:
    case = make_stress_case(stress=make_stress(duration_budget_seconds=7200))
    assert check(case) == []


def test_step_wait_over_limit_is_reported() -> None:
    case = make_spec(
        steps=[
            make_step(
                1,
                action=StepAction.WAIT,
                wait_seconds=MAX_STEP_WAIT_SECONDS + 1,
                checkpoints=[hard_checkpoint()],
            )
        ]
    )
    violations = check(case)
    assert len(violations) == 1
    assert "wait_seconds=31" in violations[0]
    assert "30" in violations[0]


def test_step_wait_at_limit_is_clean() -> None:
    case = make_spec(
        steps=[
            make_step(1, action=StepAction.WAIT, wait_seconds=MAX_STEP_WAIT_SECONDS, checkpoints=[hard_checkpoint()])
        ]
    )
    assert check(case) == []


def test_stress_body_wait_over_limit_is_reported() -> None:
    body = [make_step(1, action=StepAction.WAIT, wait_seconds=45.0)]
    case = make_stress_case(stress=make_stress(body_steps=body))
    violations = check(case)
    assert len(violations) == 1
    assert "stress.body_steps[0]" in violations[0]


def test_coordinate_step_without_resolution_bound_is_reported() -> None:
    """纵深防御：用例构造完成后再就地清空 resolution_bound（IR 校验器不再重跑）也要拦住。"""
    case = make_spec(steps=[make_step(1, locator=coordinate_locator(), checkpoints=[hard_checkpoint()])])
    assert case.steps[0].locator is not None
    case.steps[0].locator.resolution_bound = None
    violations = check(case)
    assert len(violations) == 1
    assert "resolution_bound" in violations[0]


def test_coordinate_step_without_warning_is_reported() -> None:
    case = make_spec(steps=[make_step(1, locator=coordinate_locator(), checkpoints=[hard_checkpoint()])])
    assert case.steps[0].locator is not None
    case.steps[0].locator.warning = ""
    violations = check(case)
    assert len(violations) == 1
    assert "warning" in violations[0]


def test_coordinate_step_with_bound_and_warning_is_clean() -> None:
    case = make_spec(steps=[make_step(1, locator=coordinate_locator(), checkpoints=[hard_checkpoint()])])
    assert check(case) == []


# ---------------------------------------------------------------------------
# 规则 4：压测循环体禁止应用生命周期动作
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", [StepAction.START_APP, StepAction.STOP_APP])
def test_app_lifecycle_inside_stress_loop_body_is_rejected(action: StepAction) -> None:
    case = make_stress_case(stress=make_stress(body_steps=[make_step(1, action=action)]))
    violations = check(case)
    assert len(violations) == 1
    assert action.value in violations[0]
    assert "stress.body_steps[0]" in violations[0]


def test_noop_comment_and_screenshot_are_allowed_in_loop_body() -> None:
    body = [
        make_step(1, action=StepAction.NOOP_COMMENT, comment="本工具不支持，跳过"),
        make_step(2, action=StepAction.SCREENSHOT),
    ]
    case = make_stress_case(stress=make_stress(body_steps=body))
    assert check(case) == []


def test_app_lifecycle_outside_loop_body_is_not_flagged() -> None:
    """setup / teardown 里的 START_APP / STOP_APP 正是生命周期该待的地方。"""
    case = make_stress_case(
        setup=SetupSpec(pre_steps=[make_step(1, action=StepAction.START_APP)]),
        teardown=TeardownSpec(post_steps=[make_step(1, action=StepAction.STOP_APP)]),
    )
    assert check(case) == []


# ---------------------------------------------------------------------------
# 规则 5：coordinate-risk 标签（唯一的有意副作用）
# ---------------------------------------------------------------------------


def test_purely_coordinate_stress_body_gets_coordinate_risk_tag() -> None:
    body = [make_step(1, locator=coordinate_locator()), make_step(2, locator=coordinate_locator())]
    case = make_stress_case(stress=make_stress(body_steps=body))
    assert check(case) == []
    assert case.tags == [COORDINATE_RISK_TAG]


def test_bare_coordinate_step_counts_as_coordinate_driven() -> None:
    body = [make_step(1, action=StepAction.CLICK, coordinate=(10, 20))]
    case = make_stress_case(stress=make_stress(body_steps=body))
    assert check(case) == []
    assert COORDINATE_RISK_TAG in case.tags


def test_drag_to_step_counts_as_coordinate_driven() -> None:
    body = [make_step(1, action=StepAction.DRAG, direction="up", drag_to=(500, 900))]
    case = make_stress_case(stress=make_stress(body_steps=body))
    assert check(case) == []
    assert COORDINATE_RISK_TAG in case.tags


def test_direction_only_swipe_body_is_not_tagged() -> None:
    """默认的 UP/DOWN 交替滑动只给方向、不给坐标，不得误判为纯坐标循环体。"""
    body = [
        make_step(1, action=StepAction.SWIPE, direction="up"),
        make_step(2, action=StepAction.SWIPE, direction="down"),
    ]
    case = make_stress_case(stress=make_stress(kind=StressKind.CONTINUOUS_SWIPE, body_steps=body))
    assert check(case) == []
    assert COORDINATE_RISK_TAG not in case.tags


def test_coordinate_risk_tag_is_idempotent() -> None:
    body = [make_step(1, locator=coordinate_locator())]
    case = make_stress_case(stress=make_stress(body_steps=body))
    check(case)
    check(case)
    assert case.tags.count(COORDINATE_RISK_TAG) == 1


def test_preexisting_coordinate_risk_tag_is_not_duplicated() -> None:
    body = [make_step(1, locator=coordinate_locator())]
    case = make_stress_case(tags=[COORDINATE_RISK_TAG, "stress"], stress=make_stress(body_steps=body))
    assert check(case) == []
    assert case.tags == [COORDINATE_RISK_TAG, "stress"]


def test_mixed_loop_body_is_not_tagged() -> None:
    body = [make_step(1, locator=coordinate_locator()), make_step(2, locator=text_locator("列表"))]
    case = make_stress_case(stress=make_stress(body_steps=body))
    assert check(case) == []
    assert COORDINATE_RISK_TAG not in case.tags


def test_empty_loop_body_is_not_tagged() -> None:
    case = make_stress_case(stress=make_stress(body_steps=[]))
    assert check(case) == []
    assert COORDINATE_RISK_TAG not in case.tags


def test_non_stress_coordinate_steps_are_not_tagged() -> None:
    case = make_spec(steps=[make_step(1, locator=coordinate_locator(), checkpoints=[hard_checkpoint()])])
    assert check(case) == []
    assert COORDINATE_RISK_TAG not in case.tags


def test_validation_only_mutates_tags_for_coordinate_risk() -> None:
    """除文档化的标签副作用外，校验不得改动 spec 的任何字段。"""
    case = make_stress_case(stress=make_stress(body_steps=[make_step(1, locator=coordinate_locator())]))
    untouched = make_stress_case(stress=make_stress(body_steps=[make_step(1, locator=coordinate_locator())]))
    before = case.model_dump(mode="json")
    assert before["tags"] == []

    check(case)

    after = case.model_dump(mode="json")
    assert after["tags"] == [COORDINATE_RISK_TAG]
    del before["tags"]
    del after["tags"]
    assert after == before
    assert untouched.tags == []


def test_violations_are_chinese_messages() -> None:
    case = make_spec(steps=[make_step(1, text="点击删除按钮", checkpoints=[hard_checkpoint()])])
    violations = check(case)
    assert violations
    assert all(any("\u4e00" <= char <= "\u9fff" for char in item) for item in violations)
