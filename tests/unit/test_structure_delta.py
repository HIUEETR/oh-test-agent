"""改动 4 回归：结构指纹的**分量**比较（``StructureDelta``）。

全等判定 ``struct_same`` 会被轮播的一个文本变化推翻（真机复盘 run-20260922T141003Z-6bf8bf42
step-5：稳定文本 31→32 就让三元组不等），而 ``page_path`` 与元素数不变本身就是「未导航」的
强信号。``structure_delta`` 把指纹拆成分量，供「弱变化」判据使用 —— 刻意不改
``stability_fingerprint`` 本身（它被探索栈的页面身份判定与事后分析共用）。

**与计划的实测偏差**：计划按 step-5 的文本漂移 ``≈0.03`` 设定默认阈值 0.10；实测对称差 7 /
并集 35 = **0.20**（轮播滚过 4 张海报的标题）。因此默认阈值下改动 4 的弱判据在 step-5 上
**不**触发（该步由改动 1 的三条件强判据命中），本条用显式阈值验证弱判据本身可用。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from defect_fixtures import DefectFakeDevice, noop_snapshots

from harmony_test_agent.analysis.in_run import (
    InRunDetector,
    InRunThresholds,
    snapshot_fingerprint,
    structure_delta,
)
from harmony_test_agent.models import AnomalyKind, ScreenSnapshot, ToolName
from harmony_test_agent.perception.normalizer import normalize_layout, page_path

#: 30 个互不相同的稳定文本：改 1 条时对称差 2 / 并集 30 ≈ 0.067，稳落在 0.10 阈值内。
_WORDS = (
    "alpha",
    "bravo",
    "charlie",
    "delta",
    "echo",
    "foxtrot",
    "golf",
    "hotel",
    "india",
    "juliett",
    "kilo",
    "lima",
    "mike",
    "november",
    "oscar",
    "papa",
    "quebec",
    "romeo",
    "sierra",
    "tango",
    "uniform",
    "victor",
    "whiskey",
    "xray",
    "yankee",
    "zulu",
    "anchor",
    "banner",
    "canyon",
    "dune",
)


def _layout(page: str, texts: list[str]) -> dict:
    children = [
        {
            "attributes": {
                "key": f"row_{index}",
                "type": "Text",
                "text": text,
                "visible": "true",
                "bounds": f"[0,{100 + index * 60}][1080,{140 + index * 60}]",
            }
        }
        for index, text in enumerate(texts)
    ]
    return {
        "attributes": {"pagePath": page, "visible": "true", "bounds": "[0,0][1080,2340]"},
        "children": children,
    }


def _frame(
    tmp_path: Path,
    *,
    texts: list[str],
    page: str = "pages/Feed",
    digest: str = "digest",
    name: str = "snap.png",
) -> ScreenSnapshot:
    layout = _layout(page, texts)
    return ScreenSnapshot(
        snapshot_id=f"snap-{digest}-{len(texts)}",
        run_id="run-structure-delta",
        image_path=tmp_path / name,
        image_sha256=digest,
        width=1080,
        height=2340,
        page_path=page_path(layout),
        elements=normalize_layout(layout, 1080, 2340),
    )


def _detector(tmp_path: Path, device: DefectFakeDevice, **overrides) -> InRunDetector:
    thresholds = InRunThresholds(
        probe_enabled=False,
        screen_scan_enabled=False,
        escalate_count=overrides.pop("escalate_count", 2),
        noop_navigation_enabled=overrides.pop("noop_navigation_enabled", False),
        noop_text_drift_ratio=overrides.pop("noop_text_drift_ratio", 0.10),
    )
    return InRunDetector(device, bundle_name="com.example.neteasymusic", thresholds=thresholds, run_dir=tmp_path)


class TestRealTrace:
    def test_carousel_drift_splits_the_fingerprint(self) -> None:
        """step-5：page_path 与元素数都没变，只有轮播文本漂移 ⇒ 全等判定失效、分量判定有效。"""
        before, after = noop_snapshots("step-5")
        delta = structure_delta(before, after)

        assert delta.page_same is True
        assert delta.count_same is True
        assert delta.struct_same is False
        # 实测：31 -> 32 个稳定文本，对称差 7 / 并集 35 = 0.20（不是计划假设的 0.03）。
        assert delta.text_drift_ratio == pytest.approx(0.2)
        # 全等判定的失效是**分量**造成的，不是页面身份或元素数变了。
        assert snapshot_fingerprint(before)[:2] == snapshot_fingerprint(after)[:2]
        assert snapshot_fingerprint(before) != snapshot_fingerprint(after)

    def test_real_navigation_moves_the_element_count(self) -> None:
        """真导航（step-2）：``count_same`` 直接为假 —— 这正是零误报的来源。"""
        before, after = noop_snapshots("step-2")
        delta = structure_delta(before, after)

        assert delta.page_same is True
        assert delta.count_same is False
        assert delta.struct_same is False
        assert delta.text_drift_ratio > 0.5

    def test_frozen_page_keeps_the_full_equality(self) -> None:
        """整页冻结（日历那次 ``defect-2b5ccf0ab000`` 的故障模式）⇒ ``struct_same`` 仍为真。

        回归保护：改动 4 只是**旁路**新增分量，既有停滞检测的判据一个字都没改。
        """
        before, after = noop_snapshots("step-5")
        frozen = before.model_copy(update={"snapshot_id": "snap-frozen"}, deep=True)
        frozen.image_sha256 = before.image_sha256

        delta = structure_delta(before, frozen)

        assert delta.struct_same is True
        assert delta.text_drift_ratio == 0.0
        assert delta.page_same is True and delta.count_same is True


class TestWeakChangeEscalation:
    def test_single_weak_change_is_only_a_signal(self, tmp_path: Path) -> None:
        """一次弱变化不产 finding（单次无视觉变化是信号不是判决）。"""
        device = DefectFakeDevice()
        detector = _detector(tmp_path, device)
        before = _frame(tmp_path, texts=list(_WORDS), digest="a", name="a.png")
        after = _frame(tmp_path, texts=[*_WORDS[:-1], "zephyr"], digest="b", name="b.png")
        assert structure_delta(before, after).text_drift_ratio <= 0.10

        observation = detector.observe(action_id="step-1", before=before, after=after, tool=ToolName.SWIPE, target="")

        assert observation is None
        assert detector.state.weak_change_actions == {"step-1"}

    def test_distinct_actions_escalate_to_a_warning(self, tmp_path: Path) -> None:
        """累计 2 个**不同动作**命中弱判据 ⇒ ``PAGE_UNRESPONSIVE`` warning（纯本地）。"""
        device = DefectFakeDevice()
        detector = _detector(tmp_path, device)
        before = _frame(tmp_path, texts=list(_WORDS), digest="a", name="a.png")
        after = _frame(tmp_path, texts=[*_WORDS[:-1], "zephyr"], digest="b", name="b.png")

        first = detector.observe(action_id="step-1", before=before, after=after, tool=ToolName.SWIPE)
        second = detector.observe(action_id="step-2", before=before, after=after, tool=ToolName.WAIT)

        assert first is None
        assert second is not None and second.finding is not None
        assert second.finding.kind is AnomalyKind.PAGE_UNRESPONSIVE
        assert second.finding.severity == "warning"
        assert second.finding.evidence["weak_change_actions_count"] == 2
        # 30 个文本元素 + 页面根节点：改 1 条 ⇒ 对称差 2 / 并集 31 ≈ 0.065。
        assert 0.0 < second.finding.evidence["text_drift_ratio"] <= 0.10
        assert device.device_calls == 0

    def test_same_action_repeated_does_not_escalate(self, tmp_path: Path) -> None:
        device = DefectFakeDevice()
        detector = _detector(tmp_path, device)
        before = _frame(tmp_path, texts=list(_WORDS), digest="a", name="a.png")
        after = _frame(tmp_path, texts=[*_WORDS[:-1], "zephyr"], digest="b", name="b.png")

        for _ in range(3):
            assert detector.observe(action_id="step-1", before=before, after=after, tool=ToolName.SWIPE) is None

    def test_drift_above_the_threshold_is_not_a_weak_change(self, tmp_path: Path) -> None:
        """真机 step-5 的漂移（0.20）超过默认阈值 0.10 ⇒ 弱判据不触发。

        这不是 bug：该步由改动 1 的三条件强判据命中；显式放宽阈值后弱判据才接管
        （用于 swipe / wait 这类没有明确点击目标的序列）。
        """
        before, after = noop_snapshots("step-5")

        strict = _detector(tmp_path, DefectFakeDevice(), escalate_count=1)
        assert strict.observe(action_id="step-5", before=before, after=after, tool=ToolName.SWIPE) is None

        relaxed = _detector(tmp_path, DefectFakeDevice(), escalate_count=1, noop_text_drift_ratio=0.25)
        observation = relaxed.observe(action_id="step-5", before=before, after=after, tool=ToolName.SWIPE)

        assert observation is not None and observation.finding is not None
        assert observation.finding.kind is AnomalyKind.PAGE_UNRESPONSIVE
        assert observation.finding.evidence["weak_change_actions_count"] == 1
