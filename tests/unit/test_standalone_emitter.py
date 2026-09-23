"""``StandaloneEmitter`` 的 A5 字面量清单与产物契约测试。

覆盖计划 A5/Phase 1 字面量对齐清单（``driver.stop_app(BUNDLE_NAME)`` 恰好一次、
``BY.key(...)``、``driver.touch((108, 201))  # coordinate fallback for 'ui-back'``、
``driver.swipe('UP')``、``driver.go_back()``、``driver.wait(1.0)``、``# skipped: ...``、
``pass  # no replayable operations``、``# start_app: ... (handled by script setup)`` …），
外加 ``compile()`` 可执行性、``ASSERT_TEXT`` 两形态、``--params``/env 双通道参数注入、
``generated_result.json`` 契约键、soft checkpoint 的 try/except、toast 监听、压测循环
（deadline / ``_sample_pss`` / 阈值 raise）与 ``StandaloneArtifact.config`` 形状。

测试全程离线：手写 IR（必要处用 ``CaseBuilder`` 复现 Live 的 ASSERT_TEXT 映射）后只
渲染文本，不连接设备、不执行生成的脚本。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from harmony_test_agent.cases.builder import CaseBuilder
from harmony_test_agent.cases.spec import (
    NO_REPLAYABLE_COMMENT,
    CaseProvenance,
    CheckpointKind,
    CheckpointSpec,
    LocatorSpec,
    ScenarioKind,
    SetupSpec,
    StepAction,
    StressSpec,
    TeardownSpec,
)
from harmony_test_agent.cases.spec import TestCaseSpec as CaseSpec
from harmony_test_agent.cases.spec import TestParamSpec as ParamSpec
from harmony_test_agent.cases.spec import TestStepSpec as StepSpec
from harmony_test_agent.generation.standalone import StandaloneArtifact, StandaloneEmitter
from harmony_test_agent.models import (
    ActionResult,
    LocatorCandidate,
    LocatorKind,
    RunState,
    RunTrace,
    ScreenSnapshot,
    StressKind,
    TargetAppProfile,
    ToolName,
    UIElement,
)

RUN_ID = "run-20240101-000000"
DEVICE_ID = "SN-IR-01"
CASE_ID = "case-20240101T000000Z-abcdef"

SpecFactory = Callable[[], CaseSpec]


# ---------------------------------------------------------------------------
# fixture 构造
# ---------------------------------------------------------------------------


def provenance(**overrides: Any) -> CaseProvenance:
    data: dict[str, Any] = {"source_kind": "live_run", "source_id": RUN_ID, "profile_target_app_id": "demo-app"}
    data.update(overrides)
    return CaseProvenance(**data)


def key(value: str, label: str = "") -> LocatorSpec:
    return LocatorSpec(kind=LocatorKind.KEY, value=value, target_label=label)


def identifier(value: str, label: str = "") -> LocatorSpec:
    return LocatorSpec(kind=LocatorKind.ID, value=value, target_label=label)


def coordinate(point: tuple[int, int], label: str = "") -> LocatorSpec:
    """已解析为坐标的定位器（IR 不变式要求带解析边界与警告）。"""
    return LocatorSpec(
        kind=LocatorKind.COORDINATE,
        coordinate=point,
        resolution_bound=(1080, 2340),
        target_label=label,
        warning="spatial locator resolved to a coordinate",
    )


def make_spec(
    *,
    steps: list[StepSpec] | None = None,
    setup: SetupSpec | None = None,
    teardown: TeardownSpec | None = None,
    params: list[ParamSpec] | None = None,
    scenario: ScenarioKind = ScenarioKind.CORE_FLOW,
    stress: StressSpec | None = None,
    status: str = "draft",
) -> CaseSpec:
    """构造最小可用 IR；默认 ``draft`` 以避开「非 draft 必须有硬检查点」的不变式。"""
    return CaseSpec(
        case_id=CASE_ID,
        slug="standalone-emitter-contract",
        title_zh="独立脚本发射契约",
        scenario=scenario,
        status=status,  # type: ignore[arg-type]
        bundle_name="com.demo.app",
        main_ability="MainAbility",
        device_sn=DEVICE_ID,
        timeout_seconds=300,
        params=params or [],
        setup=setup or SetupSpec(stop_app_first=True, start_app=True, startup_wait_seconds=3.0),
        steps=steps or [],
        teardown=teardown or TeardownSpec(capture_final_screenshot=True, stop_app=False),
        stress=stress,
        provenance=provenance(),
    )


def render(spec: CaseSpec) -> StandaloneArtifact:
    return StandaloneEmitter().render(spec, run_id=RUN_ID, device_id=DEVICE_ID)


def source_of(spec: CaseSpec) -> str:
    return render(spec).python_text


def literals_spec() -> CaseSpec:
    """一次性覆盖 A5 全量字面量的正常用例。"""
    return make_spec(
        steps=[
            StepSpec(
                step_id="s1",
                index=1,
                action=StepAction.CLICK,
                title_zh="点击搜索入口",
                locator=key("search_key", "搜索"),
            ),
            StepSpec(
                step_id="s2",
                index=2,
                action=StepAction.CLICK,
                title_zh="点击返回",
                locator=coordinate((108, 201), "ui-back"),
            ),
            StepSpec(step_id="s3", index=3, action=StepAction.SWIPE, title_zh="向上滑动", direction="up"),
            StepSpec(step_id="s4", index=4, action=StepAction.BACK, title_zh="返回首页"),
            StepSpec(step_id="s5", index=5, action=StepAction.WAIT, title_zh="等待一秒", wait_seconds=1.0),
            StepSpec(
                step_id="s6",
                index=6,
                action=StepAction.CHECK,
                title_zh="校验首页",
                locator=key("stable_title", "首页"),
                checkpoints=[
                    CheckpointSpec(
                        kind=CheckpointKind.ELEMENT_EXISTS,
                        message_zh="首页应可见",
                        locator=key("stable_title", "首页"),
                    )
                ],
            ),
            StepSpec(
                step_id="s7",
                index=7,
                action=StepAction.NOOP_COMMENT,
                title_zh="跳过观测工具",
                comment="skipped: collect_logs",
            ),
            StepSpec(
                step_id="s8",
                index=8,
                action=StepAction.NOOP_COMMENT,
                title_zh="无可回放操作",
                comment=NO_REPLAYABLE_COMMENT,
            ),
            StepSpec(
                step_id="s9",
                index=9,
                action=StepAction.NOOP_COMMENT,
                title_zh="启动应用",
                comment="start_app: com.demo.app (handled by script setup)",
            ),
        ],
    )


def empty_body_spec() -> CaseSpec:
    """空脚本体：应发射裸 ``pass``。"""
    return make_spec(
        steps=[StepSpec(step_id="empty", index=1, action=StepAction.NOOP_COMMENT, title_zh="空")],
        teardown=TeardownSpec(capture_final_screenshot=False, stop_app=False),
    )


def toast_spec() -> CaseSpec:
    return make_spec(
        steps=[
            StepSpec(
                step_id="s1",
                index=1,
                action=StepAction.CLICK,
                title_zh="点击保存",
                locator=key("save_button", "保存"),
                checkpoints=[
                    CheckpointSpec(
                        kind=CheckpointKind.TOAST,
                        message_zh="应弹出保存成功提示",
                        expected="保存成功",
                        wait_seconds=3,
                    )
                ],
            )
        ],
        setup=SetupSpec(stop_app_first=True, start_app=True, startup_wait_seconds=2.0, listen_toast=True),
    )


def soft_checkpoint_spec() -> CaseSpec:
    return make_spec(
        steps=[
            StepSpec(
                step_id="s1",
                index=1,
                action=StepAction.CLICK,
                title_zh="点击广告位",
                locator=key("banner_slot", "广告位"),
                checkpoints=[
                    CheckpointSpec(
                        kind=CheckpointKind.ELEMENT_EXISTS,
                        message_zh="广告位应可见",
                        locator=key("banner_slot", "广告位"),
                        soft=True,
                    )
                ],
            )
        ],
    )


def params_spec() -> CaseSpec:
    return make_spec(
        params=[ParamSpec(name="search_text", default="OpenHarmony", description_zh="搜索关键词")],
        steps=[
            StepSpec(
                step_id="s1",
                index=1,
                action=StepAction.INPUT_TEXT,
                title_zh="输入搜索关键词",
                locator=key("p2_search_input", "搜索框"),
                param_ref="search_text",
                text="OpenHarmony",
            )
        ],
    )


def counted_stress_spec() -> CaseSpec:
    """纯计数压测：无时长预算、无内存采样。"""
    return make_spec(
        scenario=ScenarioKind.STRESS,
        stress=StressSpec(
            kind=StressKind.CONTINUOUS_SWIPE,
            iterations=20,
            fail_break=True,
            inter_iteration_wait_seconds=0.5,
            body_steps=[
                StepSpec(step_id="k1", index=1, action=StepAction.SWIPE, title_zh="向上滑动", direction="up"),
                StepSpec(step_id="k2", index=2, action=StepAction.SWIPE, title_zh="向下滑动", direction="down"),
            ],
            per_iteration_checkpoints=[
                CheckpointSpec(
                    kind=CheckpointKind.ELEMENT_EXISTS,
                    message_zh="首页应可见",
                    locator=key("stable_title", "首页"),
                )
            ],
        ),
    )


def soak_stress_spec() -> CaseSpec:
    """soak 压测：时长预算 + 内存采样 + 内存增长阈值。"""
    return make_spec(
        scenario=ScenarioKind.STRESS,
        stress=StressSpec(
            kind=StressKind.SOAK,
            iterations=200,
            duration_budget_seconds=600,
            fail_break=True,
            sample_memory_every=5,
            memory_growth_threshold_kb=102400,
            step_log_interval=10,
            inter_iteration_wait_seconds=1.0,
            body_steps=[StepSpec(step_id="q1", index=1, action=StepAction.BACK, title_zh="返回首页")],
            per_iteration_checkpoints=[
                CheckpointSpec(
                    kind=CheckpointKind.ELEMENT_EXISTS,
                    message_zh="首页应可见",
                    locator=key("stable_title", "首页"),
                )
            ],
        ),
    )


def assert_text_spec(locator: LocatorCandidate | None, params: dict[str, Any]) -> CaseSpec:
    """经 ``CaseBuilder`` 复现 Live 的 ASSERT_TEXT 映射，再交给 emitter 渲染。"""
    trace = RunTrace(
        run_id="run-assert-text",
        target_app_id="demo-app",
        task="断言文本",
        device_id=DEVICE_ID,
        state=RunState.COMPLETED,
        agent_outcome="completed",
        actions=[
            ActionResult(
                step_id="click",
                tool=ToolName.CLICK_ELEMENT,
                success=True,
                params={"target": "搜索框"},
                locator=LocatorCandidate(kind=LocatorKind.KEY, value="search_input"),
            ),
            ActionResult(
                step_id="assert-text", tool=ToolName.ASSERT_TEXT, success=True, params=params, locator=locator
            ),
            ActionResult(step_id="finish", tool=ToolName.FINISH, success=True),
        ],
    )
    return CaseBuilder().from_trace(trace, demo_profile()).spec


def fallback_spec() -> CaseSpec:
    """终态完成但无显式断言：``CaseBuilder`` 注入兜底断言后交给 emitter 渲染。"""
    trace = RunTrace(
        run_id="run-fallback",
        target_app_id="demo-app",
        task="没有显式断言",
        device_id=DEVICE_ID,
        state=RunState.COMPLETED,
        agent_outcome="completed",
        snapshots=[
            ScreenSnapshot(
                snapshot_id="stable",
                run_id="run-fallback",
                image_path=Path("screens/stable.png"),
                image_sha256="abc",
                width=100,
                height=200,
                elements=[UIElement(element_id="stable-title", key="stable_title", content="首页")],
            )
        ],
        actions=[ActionResult(step_id="finish", tool=ToolName.FINISH, success=True)],
    )
    return CaseBuilder().from_trace(trace, demo_profile()).spec


def demo_profile() -> TargetAppProfile:
    return TargetAppProfile(
        target_app_id="demo-app",
        display_name="Demo",
        bundle_name="com.demo.app",
        main_ability="MainAbility",
    )


# ---------------------------------------------------------------------------
# A5：字面量清单
# ---------------------------------------------------------------------------


def test_setup_emits_deterministic_stop_start_wait_exactly_once() -> None:
    source = source_of(literals_spec())

    assert source.count("driver.stop_app(BUNDLE_NAME)") == 1
    assert source.count("driver.start_app(BUNDLE_NAME, MAIN_ABILITY)") == 1
    assert "driver.wait(STARTUP_WAIT_SECONDS)" in source


def test_teardown_stop_app_adds_a_second_stop_call() -> None:
    """``teardown.stop_app=True`` 时才出现第二次 ``stop_app``（setup 仍是恰好一次）。"""
    spec = literals_spec()
    spec.teardown.stop_app = True

    source = source_of(spec)

    assert source.count("driver.stop_app(BUNDLE_NAME)") == 2


def test_startup_wait_constant_follows_setup_spec() -> None:
    spec = make_spec(
        steps=[StepSpec(step_id="s1", index=1, action=StepAction.BACK, title_zh="返回")],
        setup=SetupSpec(stop_app_first=True, start_app=True, startup_wait_seconds=3.0),
    )

    source = source_of(spec)

    assert "STARTUP_WAIT_SECONDS = 3.0" in source


def test_selector_coordinate_and_action_literals_are_exact() -> None:
    source = source_of(literals_spec())

    assert "BY.key('search_key')" in source
    assert "driver.touch((108, 201))  # coordinate fallback for 'ui-back'" in source
    assert "driver.swipe('UP')" in source
    assert "driver.swipe('up')" not in source
    assert "driver.go_back()" in source
    assert "driver.wait(1.0)" in source


def test_noop_comment_literals_are_exact() -> None:
    source = source_of(literals_spec())

    assert "# skipped: collect_logs" in source
    assert "pass  # no replayable operations" in source
    assert "# start_app: com.demo.app (handled by script setup)" in source


def test_empty_body_renders_a_bare_pass() -> None:
    source = source_of(empty_body_spec())

    assert "\n        pass\n" in source


def test_empty_body_list_renders_a_bare_pass() -> None:
    """``render_document`` 收到空脚本体时也发射裸 ``pass``（历史模板行为）。"""
    text = StandaloneEmitter().render_document(literals_spec(), [], run_id=RUN_ID, device_id=DEVICE_ID)

    assert "\n        pass\n" in text


def test_fallback_assertion_literal_matches_history() -> None:
    """终态无显式断言的轨迹：兜底断言渲染为 ``BY.key('stable_title')`` 存在性检查。"""
    source = source_of(fallback_spec())

    assert "driver.check_component_exist(BY.key('stable_title'), expect_exist=True)" in source


# ---------------------------------------------------------------------------
# compile()：生成产物必须是可编译的 Python
# ---------------------------------------------------------------------------

COMPILED_SPECS = [
    pytest.param(literals_spec, id="normal"),
    pytest.param(toast_spec, id="toast"),
    pytest.param(soft_checkpoint_spec, id="soft-checkpoint"),
    pytest.param(params_spec, id="params-and-param-ref"),
    pytest.param(counted_stress_spec, id="counted-stress"),
    pytest.param(soak_stress_spec, id="soak-stress"),
]


@pytest.mark.parametrize("factory", COMPILED_SPECS)
def test_generated_source_compiles(factory: SpecFactory) -> None:
    spec = factory()
    source = source_of(spec)

    compile(source, f"<{spec.slug}>", "exec")

    assert "def main() -> int:" in source
    assert "raise SystemExit(main())" in source


# ---------------------------------------------------------------------------
# ASSERT_TEXT 修复的两个形态
# ---------------------------------------------------------------------------


def test_assert_text_with_key_locator_renders_exact_component_check() -> None:
    spec = assert_text_spec(
        LocatorCandidate(kind=LocatorKind.KEY, value="search_input"),
        {"target": "搜索框", "text": "OpenHarmony"},
    )

    source = source_of(spec)

    assert "driver.check_component(BY.key('search_input'), expected_equal=True, text='OpenHarmony')" in source
    assert "MatchPattern.CONTAINS" not in source


def test_assert_text_with_id_locator_renders_exact_component_check() -> None:
    spec = assert_text_spec(
        LocatorCandidate(kind=LocatorKind.ID, value="p2_search_input"),
        {"text": "OpenHarmony"},
    )

    source = source_of(spec)

    assert "driver.check_component(BY.id('p2_search_input'), expected_equal=True, text='OpenHarmony')" in source


def test_assert_text_without_locator_renders_fuzzy_contains_check() -> None:
    spec = assert_text_spec(None, {"target": "OpenHarmony"})

    source = source_of(spec)

    assert "driver.check_component_exist(BY.text('OpenHarmony', MatchPattern.CONTAINS), expect_exist=True)" in source
    assert "expected_equal=True" not in source


# ---------------------------------------------------------------------------
# 参数注入：CLI + env 双通道
# ---------------------------------------------------------------------------


def test_parameter_injection_has_both_channels() -> None:
    source = source_of(params_spec())

    assert "--params" in source
    assert "HARMONY_AGENT_CASE_PARAMS" in source
    assert 'parser.add_argument("--params", default=os.environ.get("HARMONY_AGENT_CASE_PARAMS", "{}"))' in source


def test_param_ref_step_renders_param_call_with_default() -> None:
    source = source_of(params_spec())

    assert "driver.input_text(BY.key('p2_search_input'), param('search_text', 'OpenHarmony'))" in source
    assert 'DEFAULT_PARAMS = {"search_text": "OpenHarmony"}' in source


def test_script_without_params_does_not_import_argparse() -> None:
    source = source_of(literals_spec())

    assert "import argparse" not in source
    assert "HARMONY_AGENT_CASE_PARAMS" not in source


# ---------------------------------------------------------------------------
# generated_result.json 契约
# ---------------------------------------------------------------------------


def test_generated_result_contract_keys_are_present() -> None:
    source = source_of(literals_spec())

    for contract_key in ("run_id", "passed", "error", "case_id", "soft_failures"):
        assert f'"{contract_key}"' in source
    assert f'"run_id": {RUN_ID!r}' in source
    assert '"case_id": CASE_ID' in source
    assert 'RESULT_PATH = REPORT_DIR / "generated_result.json"' in source


# ---------------------------------------------------------------------------
# soft checkpoint / toast / 压测循环
# ---------------------------------------------------------------------------


def test_soft_checkpoint_wraps_in_try_except_and_records_soft_failures() -> None:
    source = source_of(soft_checkpoint_spec())

    assert "        try:" in source
    assert "except Exception as _exc:" in source
    assert "result['soft_failures'].append({'message': '广告位应可见', 'error': str(_exc)})" in source
    assert "driver.check_component_exist(BY.key('banner_slot'), expect_exist=True)" in source


def test_ungrounded_assertion_renders_as_a_soft_key_checkpoint_end_to_end() -> None:
    """真机复盘 run-20260923T065210Z-23434a78：生成脚本里不得再有恒假的 ``BY.text(标识符)``。

    这条走完整链路（``CaseBuilder.from_trace`` → ``StandaloneEmitter``），确认降级后的
    soft 检查点确实渲染成 ``try/except`` + ``soft_failures``，脚本因此不会再 3/3 全红。
    """
    target = "p2_channel_content_question_2085141629112009975"
    trace = RunTrace(
        run_id=RUN_ID,
        target_app_id="zhihu-plus",
        task="断言证据门禁",
        device_id=DEVICE_ID,
        state=RunState.COMPLETED,
        agent_outcome="completed",
        actions=[
            ActionResult(
                step_id="click",
                tool=ToolName.CLICK_ELEMENT,
                success=True,
                params={"target": "p2_home_titlebar_search"},
                locator=LocatorCandidate(kind=LocatorKind.KEY, value="p2_home_titlebar_search"),
            ),
            ActionResult(
                step_id="assert",
                tool=ToolName.ASSERT_VISIBLE,
                success=True,
                params={"target": target},
                locator=None,
            ),
            ActionResult(step_id="finish", tool=ToolName.FINISH, success=True),
        ],
    )
    built = CaseBuilder().from_trace(trace, demo_profile())

    source = source_of(built.spec)

    # 有证据的点击逐字不变。
    assert "driver.touch(BY.key('p2_home_titlebar_search'))" in source
    assert f"BY.text({target!r})" not in source
    assert f"driver.check_component_exist(BY.key({target!r}), expect_exist=True)" in source
    assert "except Exception as _exc:" in source
    assert "result['soft_failures'].append(" in source
    # 生成器明知选择器命不中 ⇒ 不能再对外承诺 high / promotion_eligible。
    assert built.confidence == "low"
    assert built.promotion_eligible is False
    assert built.replay_eligible is True


def test_toast_checkpoint_starts_toast_listener_in_setup() -> None:
    source = source_of(toast_spec())

    assert "driver.start_listen_toast()" in source
    assert "driver.check_toast('保存成功', fuzzy='equal', timeout=3)" in source
    # 监听必须在确定性初始化之后、业务步骤之前。
    assert source.index("driver.wait(STARTUP_WAIT_SECONDS)") < source.index("driver.start_listen_toast()")
    assert source.index("driver.start_listen_toast()") < source.index("# 步骤 1")


def test_counted_stress_loop_has_deadline_sampling_and_stats() -> None:
    source = source_of(counted_stress_spec())

    assert "ITERATIONS = 20" in source
    assert "deadline = time.monotonic() + DEADLINE_SECONDS if DEADLINE_SECONDS else None" in source
    assert "_sample_pss(driver, iteration)" in source
    assert "def _sample_pss(driver, iteration: int):" in source
    assert "raise RuntimeError(" in source
    assert 'result["stress"] = stress_stats' in source
    assert source.count("driver.swipe('UP')") == 1
    assert source.count("driver.swipe('DOWN')") == 1


def test_soak_stress_loop_has_budget_sampling_and_threshold() -> None:
    source = source_of(soak_stress_spec())

    assert "DEADLINE_SECONDS = 600.0" in source
    assert "MEMORY_SAMPLE_EVERY = 5" in source
    assert "MEMORY_GROWTH_THRESHOLD_KB = 102400" in source
    assert "STEP_LOG_INTERVAL = 10" in source
    assert "INTER_ITERATION_WAIT = 1.0" in source
    assert "if _last - _first > MEMORY_GROWTH_THRESHOLD_KB:" in source
    assert 'stress_stats["memory_samples"].append({"iteration": iteration, "pss_kb": pss})' in source
    assert 'result["stress"] = stress_stats' in source
    assert "driver.go_back()" in source


def test_stress_body_overrides_plain_steps() -> None:
    """压测用例的脚本体只来自 ``stress.body_steps``，不渲染 ``spec.steps``。"""
    spec = make_spec(
        scenario=ScenarioKind.STRESS,
        steps=[StepSpec(step_id="plain", index=1, action=StepAction.BACK, title_zh="普通步骤不应出现")],
        stress=StressSpec(
            kind=StressKind.CONTINUOUS_SWIPE,
            iterations=3,
            body_steps=[StepSpec(step_id="k1", index=1, action=StepAction.SWIPE, title_zh="向上滑动", direction="up")],
        ),
    )

    source = source_of(spec)

    assert "# 步骤 1：普通步骤不应出现" not in source
    assert "# 步骤 1：向上滑动" in source


# ---------------------------------------------------------------------------
# StandaloneArtifact.config 契约
# ---------------------------------------------------------------------------


def test_config_carries_ir_keys_without_ir_case_id() -> None:
    spec = literals_spec()

    artifact = render(spec)
    config = artifact.config

    assert "ir_case_id" not in config
    assert config["case_id"] == spec.case_id
    assert config["case_slug"] == spec.slug
    assert config["scenario"] == str(spec.scenario)
    assert config["bundle_name"] == spec.bundle_name
    assert config["main_ability"] == spec.main_ability
    assert config["device_id"] == DEVICE_ID
    assert config["startup_wait_seconds"] == spec.setup.startup_wait_seconds
    assert config["generated_from_run_id"] == RUN_ID
    assert config["source_kind"] == spec.provenance.source_kind
    assert config["source_id"] == spec.provenance.source_id
    assert config["step_count"] == len(spec.steps)
    assert config["hard_checkpoint_count"] == 1


def test_case_id_in_source_matches_spec() -> None:
    spec = literals_spec()

    source = source_of(spec)

    assert f"CASE_ID = {spec.case_id!r}" in source
    assert spec.case_id == CASE_ID
