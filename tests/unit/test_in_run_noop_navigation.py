"""改动 1 回归：``NO_OP_NAVIGATION``（点击后未导航）。

判据是 **page_path + 元素数 + 被点元素 bbox 三者全未变**，独立于结构指纹 —— 结构指纹会被
轮播 / 动画的一个文本变化推翻（run-20260922T141003Z-6bf8bf42 step-5 就是这样漏报的）。

真实帧验证（``tests/fixtures/noop/``，来自上面那次网易云运行）：

- ``step-5`` 点「每日推荐卡片播放按钮」：三条件全中 → 命中；
- ``step-2`` / ``step-3`` 点底部导航栏：元素数 61→48 / 48→61 → 不命中（真导航了，零误报）。

三条件判定是**纯本地计算**：命中路径必须零设备调用（G7）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from defect_fixtures import DefectFakeDevice, noop_case, noop_snapshots

from harmony_test_agent.analysis.in_run import (
    NOOP_NAVIGATION_SUMMARY,
    InRunDetector,
    InRunThresholds,
    structure_delta,
)
from harmony_test_agent.models import AnomalyKind, ToolName


def _detector(tmp_path: Path, device: DefectFakeDevice, **overrides) -> InRunDetector:
    thresholds = InRunThresholds(
        # 昂贵取证关掉：三条件判定不需要查前台 / 采日志，命中路径必须零设备调用。
        probe_enabled=False,
        screen_scan_enabled=False,
        noop_navigation_enabled=overrides.pop("noop_navigation_enabled", True),
    )
    return InRunDetector(device, bundle_name="com.example.neteasymusic", thresholds=thresholds, run_dir=tmp_path)


def _observe(tmp_path: Path, device: DefectFakeDevice, step_id: str, **overrides):
    before, after = noop_snapshots(step_id)
    payload = noop_case(step_id)
    return _detector(tmp_path, device, **overrides).observe(
        action_id=step_id,
        before=before,
        after=after,
        tool=ToolName.CLICK_ELEMENT,
        target=payload["target"],
        clicked_element_id=payload["target"],
    )


class TestRealTrace:
    def test_real_noop_click_is_detected(self, tmp_path: Path) -> None:
        """真机漏报的那一次：点击后路径 / 元素数 / 被点元素 bbox 全未变。"""
        device = DefectFakeDevice()
        observation = _observe(tmp_path, device, "step-5")

        assert observation is not None
        assert observation.finding is not None
        finding = observation.finding
        assert finding.kind is AnomalyKind.NO_OP_NAVIGATION
        assert finding.severity == "warning"
        assert finding.summary_zh == NOOP_NAVIGATION_SUMMARY
        assert finding.phase == "in_run"
        assert finding.page_path == "pages/Index"
        evidence = finding.evidence
        assert evidence["clicked_element"] == "ui-152b5e10bc1c"
        assert evidence["element_count"] == 61
        assert evidence["bbox"] == {"left": 48, "top": 285, "right": 528, "bottom": 915}
        assert evidence["page_path"] == "pages/Index"

    def test_real_noop_click_costs_zero_device_calls(self, tmp_path: Path) -> None:
        """G7：三条件判定纯本地，命中路径不得产生任何设备调用。"""
        device = DefectFakeDevice()
        _observe(tmp_path, device, "step-5")

        assert device.foreground_calls == 0
        assert device.collect_log_calls == 0
        assert device.hierarchy_calls == 0
        assert device.device_calls == 0

    def test_real_navigations_are_not_reported(self, tmp_path: Path) -> None:
        """零误报回归：step-2 / step-3 真的换了页面（元素数变了），不得报。"""
        for step_id in ("step-2", "step-3"):
            before, after = noop_snapshots(step_id)
            delta = structure_delta(before, after)
            assert delta.page_same is True  # page_path 确实没变
            assert delta.count_same is False  # 但元素数变了 ⇒ 判定必须失败
            assert _observe(tmp_path, DefectFakeDevice(), step_id) is None


class TestNegativeControls:
    def test_missing_clicked_element_is_not_reported(self, tmp_path: Path) -> None:
        """被点元素在 after 帧里不存在（换成了别的控件）⇒ 不是「点它没反应」。"""
        device = DefectFakeDevice()
        detector = _detector(tmp_path, device)
        before, after = noop_snapshots("step-5")

        observation = detector.observe(
            action_id="step-5",
            before=before,
            after=after,
            tool=ToolName.CLICK_ELEMENT,
            target="ui-not-in-frame",
            clicked_element_id="ui-not-in-frame",
        )

        assert observation is None

    def test_moved_bbox_is_not_reported(self, tmp_path: Path) -> None:
        """被点元素位置变了（页面重排）⇒ 点击有可见后果，不报。"""
        device = DefectFakeDevice()
        detector = _detector(tmp_path, device)
        before, after = noop_snapshots("step-5")
        moved = after.model_copy(deep=True)
        target = next(item for item in moved.elements if item.element_id == "ui-152b5e10bc1c")
        assert target.bbox is not None
        target.bbox = target.bbox.model_copy(update={"top": target.bbox.top + 40})

        observation = detector.observe(
            action_id="step-5",
            before=before,
            after=moved,
            tool=ToolName.CLICK_ELEMENT,
            target="ui-152b5e10bc1c",
            clicked_element_id="ui-152b5e10bc1c",
        )

        assert observation is None

    def test_page_path_change_is_not_reported(self, tmp_path: Path) -> None:
        """page_path 变了 ⇒ 导航确实发生了。"""
        device = DefectFakeDevice()
        detector = _detector(tmp_path, device)
        before, after = noop_snapshots("step-5")
        navigated = after.model_copy(update={"page_path": "pages/Player"})

        observation = detector.observe(
            action_id="step-5",
            before=before,
            after=navigated,
            tool=ToolName.CLICK_ELEMENT,
            target="ui-152b5e10bc1c",
            clicked_element_id="ui-152b5e10bc1c",
        )

        assert observation is None

    @pytest.mark.parametrize(
        "tool",
        [ToolName.SWIPE, ToolName.BACK, ToolName.INPUT_TEXT, ToolName.OPEN_APP],
    )
    def test_non_click_tools_are_out_of_scope(self, tmp_path: Path, tool: ToolName) -> None:
        """滑动本就不该导航，返回 / 输入文本同理：只有点击动作参与判定。"""
        device = DefectFakeDevice()
        detector = _detector(tmp_path, device)
        before, after = noop_snapshots("step-5")

        observation = detector.observe(
            action_id="step-5",
            before=before,
            after=after,
            tool=tool,
            target="ui-152b5e10bc1c",
            clicked_element_id="ui-152b5e10bc1c",
        )

        assert observation is None

    def test_empty_clicked_element_id_skips_the_check(self, tmp_path: Path) -> None:
        """没有点击目标（例如按坐标滑动）时不判定。"""
        device = DefectFakeDevice()
        detector = _detector(tmp_path, device)
        before, after = noop_snapshots("step-5")

        assert (
            detector.observe(action_id="step-5", before=before, after=after, tool=ToolName.CLICK_ELEMENT, target="")
            is None
        )

    def test_threshold_can_disable_the_check(self, tmp_path: Path) -> None:
        device = DefectFakeDevice()
        assert _observe(tmp_path, device, "step-5", noop_navigation_enabled=False) is None
