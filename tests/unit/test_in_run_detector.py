"""``analysis/in_run.py`` 单测：廉价门 → 昂贵取证、结构停滞升级、崩溃归属、白屏/布局顺带扫。

G2（运行中发现）与 G7（成本可控）的机器证明都在这里：

- 像素与结构都变 ⇒ 返回 ``None`` 且**设备零调用**；
- 仅像素相同、结构变了（时钟跳动）⇒ ``finding is None``，只记 metrics —— 这正是现状
  全图 SHA256 守卫的失效场景；
- 结构停滞 ⇒ 首次 warning、累计不同动作升 critical；前台丢失 ⇒ 立即昂贵取证。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from defect_fixtures import (
    BUNDLE,
    FAULTLOG_CPPCRASH_HEAD,
    DefectFakeDevice,
    faultlog_index,
    fixture_path,
    hilog_appfreeze,
    hilog_clean,
    hilog_cppcrash,
    snapshot_from_layout_fixture,
)

from harmony_test_agent.analysis.in_run import (
    InRunDetector,
    InRunThresholds,
    probe_crash_after_foreground_loss,
    snapshot_fingerprint,
    stability_fingerprint,
)
from harmony_test_agent.models import AnomalyKind, ScreenSnapshot, ToolName
from harmony_test_agent.perception.normalizer import normalize_layout, page_path


def _layout(*, clock: str = "09:41", key: str = "p2_home_titlebar_search", label: str = "推荐") -> dict:
    """一帧页面层级：``label`` 是参与结构指纹的稳定文本，``clock`` 是易变文本。"""
    return {
        "attributes": {"pagePath": "pages/Feed", "visible": "true", "bounds": "[0,0][1080,2340]"},
        "children": [
            {
                "attributes": {
                    "key": key,
                    "type": "Button",
                    "text": label,
                    "visible": "true",
                    "bounds": "[840,60][1260,180]",
                }
            },
            {
                "attributes": {
                    "key": "TimeView_Text_timeText",
                    "type": "Text",
                    "text": clock,
                    "visible": "true",
                    "bounds": "[880,20][1000,60]",
                }
            },
        ],
    }


def _snapshot(
    tmp_path: Path,
    *,
    layout: dict | None = None,
    digest: str = "digest-a",
    snapshot_id: str = "snap-a",
    image: Path | None = None,
    hierarchy: Path | None = None,
) -> ScreenSnapshot:
    """构造一帧快照。

    未显式给图时不写文件：让「纯廉价门」用例保持零本地工作量，也避免测试在不知情下
    把白屏扫描结果混进断言。
    """
    payload = layout if layout is not None else _layout()
    return ScreenSnapshot(
        snapshot_id=snapshot_id,
        run_id="run-in-run",
        image_path=image if image is not None else (tmp_path / f"{snapshot_id}.png"),
        image_sha256=digest,
        width=1080,
        height=2340,
        page_path=page_path(payload),
        hierarchy_path=hierarchy,
        elements=normalize_layout(payload, 1080, 2340),
    )


def _detector(tmp_path: Path, device: DefectFakeDevice, **overrides) -> InRunDetector:
    thresholds = InRunThresholds(
        probe_enabled=overrides.pop("probe_enabled", True),
        escalate_count=overrides.pop("escalate_count", 2),
        screen_scan_enabled=overrides.pop("screen_scan_enabled", False),
    )
    return InRunDetector(device, bundle_name=BUNDLE, thresholds=thresholds, run_dir=tmp_path)


def _textured(tmp_path: Path, name: str) -> Path:
    """写一张有内容的截图（σ 远超白屏阈值），避免顺带的白屏 finding 干扰断言。"""
    from PIL import Image

    path = tmp_path / name
    image = Image.new("L", (300, 600), 120)
    for x in range(300):
        for y in range(0, 600, 5):
            image.putpixel((x, y), (x * 7 + y * 13) % 256)
    image.save(path, format="PNG")
    return path


def _blank(tmp_path: Path, name: str = "blank.png") -> Path:
    """写一张纯白截图（会被白屏判定命中）。"""
    from PIL import Image

    path = tmp_path / name
    Image.new("L", (300, 600), 255).save(path, format="PNG")
    return path


class TestCheapGateCostsNothing:
    def test_pixel_and_structure_both_changed_makes_zero_device_calls(self, tmp_path: Path) -> None:
        """G7 的机器证明：无可疑时**一次设备调用都不发**。"""
        before = _snapshot(tmp_path, layout=_layout(clock="09:41", label="推荐"), digest="before")
        after = _snapshot(tmp_path, layout=_layout(clock="09:42", label="热榜"), digest="after")
        device = DefectFakeDevice()

        observation = _detector(tmp_path, device).observe(
            action_id="step-1", before=before, after=after, tool=ToolName.CLICK_ELEMENT, target="ui-x"
        )

        assert observation is None
        assert device.foreground_calls == 0
        assert device.collect_log_calls == 0
        assert device.hierarchy_calls == 0
        assert device.calls == []

    def test_clock_jitter_does_not_mask_a_structurally_stalled_click(self, tmp_path: Path) -> None:
        """**现状守卫的失效场景**：时钟跳一秒让全图摘要变化，结构停滞信号因此被吞掉。

        结构指纹（page + 元素数 + 稳定文本集）刻意滤掉时钟这类易变文本，所以这里仍然命中；
        这正是用结构指纹取代全图 SHA256 做主判据的理由。
        """
        before = _snapshot(tmp_path, layout=_layout(clock="09:41"), digest="digest-0941")
        after = _snapshot(tmp_path, layout=_layout(clock="09:42"), digest="digest-0942")
        device = DefectFakeDevice()

        # 全图摘要不同（历史守卫在这里把 unchanged_count 归零）
        assert before.image_sha256 != after.image_sha256
        # 结构指纹完全相同（真正的信号）
        assert snapshot_fingerprint(before) == snapshot_fingerprint(after)

        observation = _detector(tmp_path, device).observe(
            action_id="step-1", before=before, after=after, tool=ToolName.CLICK_ELEMENT, target="ui-x"
        )

        assert observation is not None
        assert observation.pixel_same is False
        assert observation.struct_same is True
        assert observation.finding is not None
        assert observation.finding.kind is AnomalyKind.PAGE_UNRESPONSIVE
        assert observation.finding.severity == "warning"
        assert observation.finding.phase == "in_run"

    def test_only_pixel_same_while_structure_moved_is_not_a_finding(self, tmp_path: Path) -> None:
        """仅像素相同、结构变了 ⇒ 只记 metrics，不产 finding（合法无视觉变化动作的护栏）。"""
        before = _snapshot(tmp_path, layout=_layout(label="推荐"), digest="same")
        after = _snapshot(tmp_path, layout=_layout(label="热榜"), digest="same")

        observation = _detector(tmp_path, DefectFakeDevice()).observe(
            action_id="step-1", before=before, after=after, tool=ToolName.CLICK_ELEMENT
        )

        assert observation is not None
        assert observation.pixel_same is True
        assert observation.struct_same is False
        assert observation.finding is None

    def test_identical_pixel_and_structure_is_the_suspicious_minimum(self, tmp_path: Path) -> None:
        before = _snapshot(tmp_path, digest="same")
        after = _snapshot(tmp_path, digest="same")
        device = DefectFakeDevice()

        observation = _detector(tmp_path, device).observe(
            action_id="step-1", before=before, after=after, tool=ToolName.CLICK_ELEMENT, target="ui-x"
        )

        assert observation is not None and observation.pixel_same and observation.struct_same
        assert observation.finding is not None

    def test_probe_disabled_avoids_every_device_call(self, tmp_path: Path) -> None:
        before = _snapshot(tmp_path, digest="same")
        after = _snapshot(tmp_path, digest="same")
        device = DefectFakeDevice()

        observation = _detector(tmp_path, device, probe_enabled=False).observe(
            action_id="step-1", before=before, after=after, tool=ToolName.CLICK_ELEMENT
        )

        # 关闭昂贵取证后仍能看到结构停滞（纯本地判定），但不再查前台 / 采日志
        assert observation is not None and observation.finding is not None
        assert device.foreground_calls == 0
        assert device.collect_log_calls == 0


class TestStructuralStallEscalation:
    def test_first_stall_is_a_warning_and_never_aborts(self, tmp_path: Path) -> None:
        detector = _detector(tmp_path, DefectFakeDevice())
        before = _snapshot(tmp_path, digest="same")
        after = _snapshot(tmp_path, digest="same")

        observation = detector.observe(
            action_id="step-1", before=before, after=after, tool=ToolName.CLICK_ELEMENT, target="ui-x"
        )

        assert observation is not None and observation.finding is not None
        assert observation.finding.severity == "warning"
        assert observation.finding.evidence["escalated"] is False

    def test_second_distinct_action_escalates_to_critical(self, tmp_path: Path) -> None:
        detector = _detector(tmp_path, DefectFakeDevice())
        before = _snapshot(tmp_path, digest="same")
        after = _snapshot(tmp_path, digest="same")

        first = detector.observe(
            action_id="step-1", before=before, after=after, tool=ToolName.CLICK_ELEMENT, target="ui-a"
        )
        second = detector.observe(
            action_id="step-2", before=before, after=after, tool=ToolName.CLICK_ELEMENT, target="ui-b"
        )

        assert first is not None and first.finding is not None and first.finding.severity == "warning"
        assert second is not None and second.finding is not None
        assert second.finding.severity == "critical"
        assert second.finding.evidence["escalated"] is True

    def test_same_action_repeated_does_not_escalate(self, tmp_path: Path) -> None:
        """升级判据是「不同动作」：同一步反复重试不该把自己升级成 critical。"""
        detector = _detector(tmp_path, DefectFakeDevice(), escalate_count=2)
        before = _snapshot(tmp_path, digest="same")
        after = _snapshot(tmp_path, digest="same")

        first = detector.observe(action_id="step-1", before=before, after=after, tool=ToolName.CLICK_ELEMENT)
        second = detector.observe(action_id="step-1", before=before, after=after, tool=ToolName.CLICK_ELEMENT)

        assert first is not None and first.finding is not None
        assert second is not None and second.finding is not None
        assert second.finding.severity == "warning"


class TestForegroundLossProbe:
    def test_foreground_lost_with_crash_hilog_reports_cppcrash(self, tmp_path: Path) -> None:
        before = _snapshot(tmp_path, digest="same")
        after = _snapshot(tmp_path, digest="same")
        device = DefectFakeDevice(
            foreground=None,
            hilog=hilog_cppcrash(),
            listing=faultlog_index(),
            head=FAULTLOG_CPPCRASH_HEAD,
        )

        observation = _detector(tmp_path, device).observe(
            action_id="step-7", before=before, after=after, tool=ToolName.CLICK_COORDINATE, target="500,900"
        )

        assert observation is not None and observation.finding is not None
        assert observation.foreground_lost is True
        assert observation.finding.severity == "critical"
        assert observation.finding.kind in {
            AnomalyKind.CPP_CRASH,
            AnomalyKind.JS_CRASH,
            AnomalyKind.APP_FREEZE,
            AnomalyKind.ANR,
        }
        assert device.collect_log_calls >= 1
        # hilog 落盘作为证据
        assert any("hilog_inrun_" in path for path in observation.evidence_paths)

    def test_foreground_lost_with_clean_logs_reports_unresponsive_critical(self, tmp_path: Path) -> None:
        before = _snapshot(tmp_path, digest="same")
        after = _snapshot(tmp_path, digest="same")
        device = DefectFakeDevice(
            foreground="com.other.app",
            hilog=hilog_clean(),
            listing="total 0\n",
            head="",
        )

        observation = _detector(tmp_path, device).observe(
            action_id="step-8", before=before, after=after, tool=ToolName.CLICK_ELEMENT, target="ui-x"
        )

        assert observation is not None and observation.finding is not None
        assert observation.foreground_lost is True
        assert observation.foreground_bundle == "com.other.app"
        assert observation.finding.kind is AnomalyKind.PAGE_UNRESPONSIVE
        assert observation.finding.severity == "critical"
        assert "前台应用丢失" in observation.finding.summary_zh

    def test_freeze_hilog_is_attributed(self, tmp_path: Path) -> None:
        before = _snapshot(tmp_path, digest="same")
        after = _snapshot(tmp_path, digest="same")
        device = DefectFakeDevice(foreground=None, hilog=hilog_appfreeze(), listing="total 0\n", head="")

        observation = _detector(tmp_path, device).observe(
            action_id="step-9", before=before, after=after, tool=ToolName.CLICK_ELEMENT
        )

        assert observation is not None and observation.finding is not None
        assert observation.finding.kind in {AnomalyKind.APP_FREEZE, AnomalyKind.ANR}

    def test_probe_crash_after_foreground_loss_returns_empty_for_clean_logs(self, tmp_path: Path) -> None:
        device = DefectFakeDevice(foreground=None, hilog=hilog_clean(), listing="total 0\n")

        findings = probe_crash_after_foreground_loss(device, BUNDLE, tmp_path / "hilog.txt")

        assert findings == []
        assert (tmp_path / "hilog.txt").is_file()

    def test_probe_degrades_when_the_device_raises(self, tmp_path: Path) -> None:
        class Broken:
            def _run(self, *args, **kwargs):
                raise RuntimeError("hdc exploded")

            def collect_logs(self, path):
                raise RuntimeError("hdc exploded")

        assert probe_crash_after_foreground_loss(Broken(), BUNDLE, tmp_path / "hilog.txt") == []


class TestLocalScans:
    def test_blank_frame_is_detected_even_when_nothing_is_suspicious(self, tmp_path: Path) -> None:
        """白屏是纯本地计算，可以无条件跑 —— 补上「运行中完全无人检白屏」的洞。

        结构也变了（label 不同）⇒ 廉价门不命中 ⇒ 本该零设备调用；白屏仍然被发现。
        """
        blank = _blank(tmp_path)
        before = _snapshot(tmp_path, layout=_layout(label="推荐"), digest="a", image=blank)
        after = _snapshot(tmp_path, layout=_layout(label="热榜"), digest="b", snapshot_id="snap-2", image=blank)
        device = DefectFakeDevice()

        observation = _detector(tmp_path, device, screen_scan_enabled=True).observe(
            action_id="step-1", before=before, after=after, tool=ToolName.CLICK_ELEMENT
        )

        assert observation is not None and observation.finding is not None
        assert observation.finding.kind is AnomalyKind.WHITE_SCREEN
        assert observation.finding.phase == "in_run"
        assert device.foreground_calls == 0  # 纯本地，没有额外设备调用
        assert device.device_calls == 0

    def test_normal_frame_produces_no_blank_finding(self, tmp_path: Path) -> None:
        normal = _textured(tmp_path, "normal.png")
        before = _snapshot(tmp_path, layout=_layout(label="推荐"), digest="a", image=normal)
        after = _snapshot(tmp_path, layout=_layout(label="热榜"), digest="b", snapshot_id="s2", image=normal)

        observation = _detector(tmp_path, DefectFakeDevice(), screen_scan_enabled=True).observe(
            action_id="step-1", before=before, after=after, tool=ToolName.CLICK_ELEMENT
        )

        assert observation is None

    def test_layout_fixture_scan_reports_out_of_bounds(self, tmp_path: Path) -> None:
        """布局规则是纯本地读文件，运行中每步都能扫。"""
        from harmony_test_agent.analysis.layout import detect_layout_anomalies

        hierarchy = json.loads(fixture_path("layout_out_of_bounds.json").read_text(encoding="utf-8"))
        findings = detect_layout_anomalies(hierarchy, 1320, 2232)

        assert findings, "越界 fixture 必须被 L1 报出"
        assert all(finding.kind is AnomalyKind.LAYOUT_ANOMALY for finding in findings)

    def test_layout_scan_produces_a_finding_from_a_dirty_hierarchy(self, tmp_path: Path) -> None:
        """布局规则是纯本地读文件，运行中每步都能扫（越界 fixture 必被报出）。

        ``observe`` 返回的是「最严重的一条」，因此这里直接对 ``_local_scan`` 断言完整集合，
        再确认 observe 路径拿到了 finding 且没有额外设备调用。
        """
        hierarchy_path = fixture_path("layout_out_of_bounds.json")
        normal = _textured(tmp_path, "normal.png")
        after = snapshot_from_layout_fixture(hierarchy_path, image_path=normal).model_copy(
            update={"hierarchy_path": hierarchy_path}
        )
        device = DefectFakeDevice()
        detector = InRunDetector(
            device,
            bundle_name=BUNDLE,
            thresholds=InRunThresholds(screen_scan_enabled=True),
            run_dir=tmp_path,
        )

        findings, evidence = detector._local_scan(after, "step-1")

        assert any(finding.kind is AnomalyKind.LAYOUT_ANOMALY for finding in findings)
        assert any(str(path).endswith("layout_out_of_bounds.json") for path in evidence)
        assert all(finding.phase == "in_run" for finding in findings)
        assert device.device_calls == 0

    def test_observe_surfaces_layout_findings_without_device_calls(self, tmp_path: Path) -> None:
        hierarchy_path = fixture_path("layout_out_of_bounds.json")
        normal = _textured(tmp_path, "normal.png")
        after = snapshot_from_layout_fixture(hierarchy_path, image_path=normal).model_copy(
            update={"hierarchy_path": hierarchy_path}
        )
        before = after.model_copy(update={"page_path": "pages/Other", "elements": []})
        device = DefectFakeDevice()

        observation = _detector(tmp_path, device, screen_scan_enabled=True).observe(
            action_id="step-1", before=before, after=after, tool=ToolName.CLICK_ELEMENT
        )

        assert observation is not None and observation.finding is not None
        assert observation.finding.phase == "in_run"
        assert device.device_calls == 0

    def test_screen_scan_disabled_skips_local_work(self, tmp_path: Path) -> None:
        blank = _blank(tmp_path)
        before = _snapshot(tmp_path, digest="a", image=blank)
        after = _snapshot(tmp_path, digest="b", snapshot_id="s2", image=blank)

        observation = _detector(tmp_path, DefectFakeDevice(), screen_scan_enabled=False).observe(
            action_id="step-1", before=before, after=after, tool=ToolName.CLICK_ELEMENT
        )

        # 结构相同 ⇒ 走可疑分支，但本地扫描关闭 ⇒ 只有结构停滞这一个 finding
        assert observation is not None and observation.finding is not None
        assert observation.finding.kind is AnomalyKind.PAGE_UNRESPONSIVE


class TestFingerprintConsistency:
    def test_matches_bounded_explorer_semantics(self) -> None:
        """结构指纹必须与 ``BoundedExplorer._stability_fingerprint`` 完全一致。"""
        from harmony_test_agent.discovery.explorer import BoundedExplorer

        layout = _layout()
        elements = normalize_layout(layout, 1080, 2340)

        assert stability_fingerprint("pages/Feed", elements) == BoundedExplorer._stability_fingerprint(
            "pages/Feed", elements
        )

    def test_snapshot_fingerprint_ignores_volatile_clock_text(self, tmp_path: Path) -> None:
        left = _snapshot(tmp_path, layout=_layout(clock="09:41"), digest="a")
        right = _snapshot(tmp_path, layout=_layout(clock="09:42"), digest="b")

        assert snapshot_fingerprint(left) == snapshot_fingerprint(right)

    def test_structural_change_moves_the_fingerprint(self, tmp_path: Path) -> None:
        left = _snapshot(tmp_path, layout=_layout(label="推荐"), digest="a")
        right = _snapshot(tmp_path, layout=_layout(label="热榜"), digest="b")

        assert snapshot_fingerprint(left) != snapshot_fingerprint(right)

    def test_volatile_clock_and_loading_text_are_filtered_like_the_explorer(self, tmp_path: Path) -> None:
        """易变文本（时钟 / 加载占位 / 纯数字）不参与指纹 —— 与探索栈逐字一致。"""
        left = _snapshot(tmp_path, layout=_layout(clock="09:41", label="推荐"), digest="a")
        right = _snapshot(tmp_path, layout=_layout(clock="23:59", label="推荐"), digest="b")

        assert snapshot_fingerprint(left) == snapshot_fingerprint(right)
        assert snapshot_fingerprint(left) == ("pages/Feed", 2, ("推荐",))


class TestDetectorNeverRaises:
    def test_internal_failure_returns_none(self, tmp_path: Path) -> None:
        """检测是 advisory：内部异常必须被吞掉并返回 None，绝不打断任务。"""
        detector = InRunDetector(
            DefectFakeDevice(),
            bundle_name=BUNDLE,
            thresholds=InRunThresholds(escalate_count="not-an-int"),  # type: ignore[arg-type]
            run_dir=tmp_path,
        )
        before = _snapshot(tmp_path, digest="same")
        after = _snapshot(tmp_path, digest="same")

        assert detector.observe(action_id="step-1", before=before, after=after, tool=ToolName.CLICK_ELEMENT) is None


@pytest.mark.parametrize("tool", [ToolName.CLICK_ELEMENT, ToolName.OPEN_APP, ToolName.SWIPE])
def test_mutating_tools_are_the_detection_surface(tool: ToolName) -> None:
    from harmony_test_agent.analysis.in_run import MUTATING_TOOLS

    assert tool in MUTATING_TOOLS
