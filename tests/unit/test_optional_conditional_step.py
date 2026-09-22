"""「状态条件存在」控件的条件步骤降级（真机复盘 dc-20260922T171655Z-6fff3547）。

录制时搜索框里还留着上一轮的查询词，因此 ``p2_search_clear`` 存在并被点了一次；生成脚本的
setup 却是 ``stop_app`` + ``start_app`` 冷启动 ⇒ 输入框为空 ⇒ 该按钮不渲染 ⇒
回放第一步 ``Can't find component with [BY.key('p2_search_clear')]``。

修复后的语义：这类控件按**条件步骤**渲染 —— 先 ``find_component`` 探测，命中才点，未命中
记入 ``generated_result.json`` 的 ``skipped_conditional_steps``，不判失败。
普通控件必须保持硬失败（否则真失败会被静默吞掉）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harmony_test_agent.cases.builder import is_optional_control
from harmony_test_agent.cases.spec import CheckpointKind, CheckpointSpec, LocatorKind, MatchMode, StepAction
from harmony_test_agent.dc.generator import DcHypiumGenerator
from harmony_test_agent.dc.models import DcToolInvocation, DcToolName, DcToolTier, utc_now
from harmony_test_agent.generation.standalone import OPTIONAL_TARGET_VAR, StandaloneEmitter
from harmony_test_agent.models import BoundingBox, UIElement
from harmony_test_agent.storage.artifacts import ArtifactStore

OPTIONAL_KEYS = [
    "p2_search_clear",
    "p2_home_update_card_dismiss",
    "home_back_to_top",
    "guide_skip",
    "清空",
    "不再提示",
]
REQUIRED_KEYS = [
    "p2_search_input",
    "p2_home_titlebar_search",
    "p2_search_hot_0",
    "add_agenda_cancel",  # 对话框取消：弹窗在就必然可见，不做条件降级
    "dialog_close",
    "closed",
]


class TestOptionalControlClassifier:
    @pytest.mark.parametrize("value", OPTIONAL_KEYS)
    def test_state_conditional_controls_are_recognised(self, value: str) -> None:
        assert is_optional_control(value) is True

    @pytest.mark.parametrize("value", REQUIRED_KEYS)
    def test_ordinary_controls_are_never_downgraded(self, value: str) -> None:
        assert is_optional_control(value) is False

    def test_empty_values_are_not_controls(self) -> None:
        assert is_optional_control("", None, "   ") is False  # type: ignore[arg-type]


def _click(key: str, *, invocation_id: str = "inv-clear") -> DcToolInvocation:
    element = UIElement(
        element_id=key,
        key=key,
        id=key,
        content=key,
        type="Button",
        clickable=True,
        enabled=True,
        bbox=BoundingBox(left=1050, top=159, right=1134, bottom=243),
    )
    return DcToolInvocation(
        invocation_id=invocation_id,
        turn_id="turn-1",
        tool=DcToolName.CLICK,
        tier=DcToolTier.L1,
        args={"x": 1092, "y": 201},
        success=True,
        started_at=utc_now(),
        ended_at=utc_now(),
        duration_ms=100,
        resolved_element=element,
    )


def _generate(tmp_path: Path, key: str):
    generator = DcHypiumGenerator(ArtifactStore(tmp_path / "runs"), min_observed_rounds=1)
    return generator.generate(
        session_id="dc-20260922T171655Z-6fff3547",
        device_id="127.0.0.1:5555",
        invocations=[_click(key)],
        bundle_name="com.github.zhuoyi233.zhplus",
        main_ability="EntryAbility",
    )


class TestBuilderMarksConditionalSteps:
    def test_clear_button_becomes_an_optional_step(self, tmp_path: Path) -> None:
        result = _generate(tmp_path, "p2_search_clear")

        assert "driver.touch(BY.key('p2_search_clear'))" not in result.python_text
        assert f"{OPTIONAL_TARGET_VAR} = driver.find_component(BY.key('p2_search_clear'))" in result.python_text
        assert f"if {OPTIONAL_TARGET_VAR} is not None:" in result.python_text
        assert "skipped_conditional_steps" in result.python_text
        assert any("optional step" in warning for warning in result.warnings)

    def test_ordinary_control_keeps_the_hard_click(self, tmp_path: Path) -> None:
        result = _generate(tmp_path, "p2_search_hot_0")

        assert "driver.touch(BY.key('p2_search_hot_0'))" in result.python_text
        assert "find_component" not in result.python_text
        assert not any("optional step" in warning for warning in result.warnings)

    def test_case_ir_carries_the_flag(self, tmp_path: Path) -> None:
        """``case_spec.json`` 里必须能查到 ``optional: true``（可审计）。"""
        import json

        result = _generate(tmp_path, "p2_search_clear")

        spec = json.loads(Path(result.case_spec_path).read_text(encoding="utf-8"))
        optional = [step["step_id"] for step in spec["steps"] if step.get("optional")]
        assert optional == ["inv-clear"]


class TestOptionalRendering:
    def _spec(self, *, optional: bool):
        from harmony_test_agent.cases.spec import (
            CaseProvenance,
            LocatorSpec,
            ScenarioKind,
            TestCaseSpec,
            TestStepSpec,
        )

        return TestCaseSpec(
            case_id="case-20260101T000000Z-aaaaaa",
            slug="case-1",
            title_zh="条件步骤",
            scenario=ScenarioKind.CORE_FLOW,
            bundle_name="com.example.app",
            main_ability="EntryAbility",
            steps=[
                TestStepSpec(
                    step_id="step-1",
                    index=1,
                    action=StepAction.CLICK,
                    title_zh="清空搜索框",
                    text="p2_search_clear",
                    optional=optional,
                    locator=LocatorSpec(
                        kind=LocatorKind.KEY,
                        value="p2_search_clear",
                        match=MatchMode.EQUALS,
                        target_label="p2_search_clear",
                    ),
                    checkpoints=[
                        CheckpointSpec(
                            kind=CheckpointKind.ELEMENT_EXISTS,
                            message_zh="结果列表应可见",
                            locator=LocatorSpec(
                                kind=LocatorKind.KEY,
                                value="p2_search_result_list",
                                match=MatchMode.EQUALS,
                                target_label="结果列表",
                            ),
                        )
                    ],
                )
            ],
            provenance=CaseProvenance(source_kind="dc_session", source_id="dc-1"),
        )

    def test_optional_step_is_probed_before_clicking(self) -> None:
        rendered = StandaloneEmitter().render(self._spec(optional=True), run_id="dc-1", device_id="SN1")

        assert f"{OPTIONAL_TARGET_VAR} = driver.find_component(BY.key('p2_search_clear'))" in rendered.python_text
        assert f"            driver.touch({OPTIONAL_TARGET_VAR})" in rendered.python_text
        assert "result.setdefault('skipped_conditional_steps', []).append('p2_search_clear')" in rendered.python_text

    def test_required_step_is_unchanged(self) -> None:
        rendered = StandaloneEmitter().render(self._spec(optional=False), run_id="dc-1", device_id="SN1")

        assert "driver.touch(BY.key('p2_search_clear'))" in rendered.python_text
        assert "find_component" not in rendered.python_text

    def test_checkpoints_after_an_optional_step_stay_hard(self) -> None:
        """条件只覆盖动作本身：挂在它后面的断言必须照常硬判。"""
        rendered = StandaloneEmitter().render(self._spec(optional=True), run_id="dc-1", device_id="SN1")

        checkpoint_line = "driver.check_component_exist(BY.key('p2_search_result_list'), expect_exist=True)"
        assert checkpoint_line in rendered.python_text
        # 断言行在 if 之外（缩进 8 空格），不会被条件吞掉
        assert f"        {checkpoint_line}" in rendered.python_text

    def test_xdevice_emitter_renders_the_same_guard(self, tmp_path: Path) -> None:
        """官方 devicetest 工程共用同一语义：命中才点，未命中不算失败。"""
        from harmony_test_agent.generation.xdevice_case import XDeviceEmitter

        artifact = XDeviceEmitter().render(self._spec(optional=True), tmp_path / "xdevice")
        source = artifact.python_text

        assert "_conditional_target = driver.find_component(BY.key('p2_search_clear'))" in source
        assert "if _conditional_target is not None:" in source
        assert "driver.touch(BY.key('p2_search_clear'))" not in source
        compile(source, str(artifact.case_file), "exec")  # 生成物必须是合法 Python
