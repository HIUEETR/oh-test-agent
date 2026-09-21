"""运行中检测的接线与立论依据（Phase 2 完成后由 Phase 0 基线转正）。

本文件保留两类长期有效的内容：

1. **立论依据**：历史守卫比较**全图 PNG 的 SHA256**，状态栏时钟跳一秒 / 光标 blink 一下
   就让 ``unchanged_count`` 归零，「点击后页面毫无结构变化」这个信号被彻底吞掉。
   这是「用结构指纹取代全图摘要做主判据」的设计依据，Phase 2 前后都必须成立。
2. **接线断言**：检测器存在、主循环接入、且在最后一道硬中止守卫**之前**执行。
"""

from __future__ import annotations

import ast
from pathlib import Path

from defect_fixtures import DefectFakeDevice, fixture_path, snapshot_from_layout_fixture

from harmony_test_agent.models import AnomalyKind, ScreenSnapshot

SRC = Path(__file__).resolve().parents[2] / "src" / "harmony_test_agent"
ORCHESTRATOR = SRC / "agents" / "orchestrator.py"


# --------------------------------------------------------------------------- 辅助


def _clock_frame(*, second: str, digest: str, snapshot_id: str) -> ScreenSnapshot:
    """同一页面的两帧：结构（page_path + 稳定文本）完全相同，只有时钟文本不同。

    这正是历史守卫失效的真实形态——``image_sha256`` 不同（时钟像素变了），
    但页面结构一个字都没动。
    """
    hierarchy = {
        "attributes": {"pagePath": "pages/Feed", "visible": "true", "bounds": "[0,0][1080,2340]"},
        "children": [
            {
                "attributes": {
                    "key": "p2_home_titlebar_search",
                    "type": "Button",
                    "text": "推荐",
                    "visible": "true",
                    "bounds": "[840,60][1260,180]",
                }
            },
            {
                "attributes": {
                    "key": "TimeView_Text_timeText",
                    "type": "Text",
                    "text": second,
                    "visible": "true",
                    "bounds": "[880,20][1000,60]",
                }
            },
        ],
    }
    from harmony_test_agent.perception.normalizer import normalize_layout, page_path

    return ScreenSnapshot(
        snapshot_id=snapshot_id,
        run_id="run-baseline",
        image_path=Path(f"{snapshot_id}.png"),
        image_sha256=digest,
        width=1080,
        height=2340,
        page_path=page_path(hierarchy),
        elements=normalize_layout(hierarchy, 1080, 2340),
    )


def _click_did_nothing_frames() -> tuple[ScreenSnapshot, ScreenSnapshot]:
    """点击后结构完全没变、只有时钟跳了一秒的两帧。"""
    before = _clock_frame(second="09:41", digest="digest-clock-0941", snapshot_id="before-0941")
    after = _clock_frame(second="09:42", digest="digest-clock-0942", snapshot_id="after-0942")
    return before, after


def orchestrator_source() -> str:
    return ORCHESTRATOR.read_text(encoding="utf-8")


def _fingerprint(snapshot: ScreenSnapshot) -> tuple[str, int, tuple[str, ...]]:
    """结构指纹：与 ``analysis/in_run.py::stability_fingerprint`` 同一条实现路径。"""
    from harmony_test_agent.analysis.in_run import snapshot_fingerprint

    return snapshot_fingerprint(snapshot)


# ----------------------------------------------- 缺口 2(a)：检测器与接线


class TestInRunDetectionSurface:
    def test_in_run_observation_model_exists(self) -> None:
        from harmony_test_agent.models import InRunObservation

        observation = InRunObservation(action_id="s1", pixel_same=False, struct_same=False)

        assert observation.finding is None
        assert observation.evidence_paths == []

    def test_in_run_detector_module_exists(self) -> None:
        from harmony_test_agent.analysis.in_run import (
            InRunDetector,
            InRunThresholds,
            probe_crash_after_foreground_loss,
        )

        assert InRunDetector is not None
        assert InRunThresholds is not None
        assert callable(probe_crash_after_foreground_loss)

    def test_orchestrator_invokes_a_detector_before_the_hard_guard(self) -> None:
        """运行中检测必须**先于**最后一道硬中止守卫执行（否则 abort 抢在检测之前）。"""
        source = orchestrator_source()

        assert "in_run_detector" in source, "AgentOrchestrator 未接入 InRunDetector"
        detect_at = source.index("_detect_in_run_anomalies")
        guard_at = source.index("unchanged_screen_limit")
        assert detect_at < guard_at, "运行中检测必须插在硬中止守卫之前"


# -------------------------------- 缺口 2(b)：结构停滞信号被时钟抖动吞掉（核心论据）


class TestStructuralStallSignal:
    def test_structurally_stalled_click_yields_an_in_run_finding(self) -> None:
        """点击后结构指纹不变 ⇒ 必须产出一条 ``phase="in_run"`` 的 finding。"""
        from harmony_test_agent.analysis.in_run import InRunDetector, InRunThresholds
        from harmony_test_agent.models import ToolName

        before, after = _click_did_nothing_frames()
        device = DefectFakeDevice(foreground="com.zhihu.hmos")
        detector = InRunDetector(
            device,
            bundle_name="com.zhihu.hmos",
            thresholds=InRunThresholds(screen_scan_enabled=False),
        )

        observation = detector.observe(
            action_id="step-1",
            before=before,
            after=after,
            tool=ToolName.CLICK_ELEMENT,
            target="ui-hot-list-item-1",
        )

        assert observation is not None
        assert observation.struct_same is True
        assert observation.finding is not None
        assert observation.finding.kind is AnomalyKind.PAGE_UNRESPONSIVE
        assert observation.finding.phase == "in_run"

    def test_the_legacy_guard_uses_a_whole_image_digest_that_clock_jitter_resets(self) -> None:
        """**历史守卫的失效证明**（设计立论依据，长期有效）。

        ``orchestrator.py`` 的守卫比较 ``before.image_sha256 == after.image_sha256``：
        时钟跳一秒就让全图摘要不同 ⇒ ``unchanged_count`` 归零 ⇒ 结构停滞信号丢失。
        """
        before, after = _click_did_nothing_frames()

        # 全图摘要不同（守卫在这里归零，看不到任何问题）
        assert before.image_sha256 != after.image_sha256
        # 但结构指纹完全相同（真正的信号）
        assert _fingerprint(before) == _fingerprint(after)

    def test_legacy_guard_source_still_compares_image_sha256(self) -> None:
        """守卫本体保持不动（设计红线：它是最后一道硬中止，不替换、只让前面多一层检测）。"""
        source = orchestrator_source()

        assert "before.image_sha256 == after.image_sha256" in source
        assert "unchanged_count >= self.settings.unchanged_screen_limit" in source

    def test_the_guard_semantics_are_now_covered_by_tests(self) -> None:
        """Phase 2 之后：运行中检测（含守卫周边语义）已有测试覆盖。"""
        tests_root = Path(__file__).resolve().parents[1]
        covered = {
            path.name
            for path in tests_root.rglob("test_*.py")
            if "in_run_detector" in path.read_text(encoding="utf-8")
            or "in_run_detector_factory" in path.read_text(encoding="utf-8")
        }

        assert covered, "运行中检测必须有测试覆盖"


# ------------------------------------------------- 缺口 2(c)：探索期崩溃不再静默


class TestExplorationCrashSilence:
    def test_explorer_has_a_crash_probe_hook(self) -> None:
        source = (SRC / "discovery" / "explorer.py").read_text(encoding="utf-8")

        assert "probe_crash_after_foreground_loss" in source
        assert "app crash suspected" in source

    def test_discovery_result_carries_anomalies(self) -> None:
        """``DiscoveryResult.anomalies`` 必须被声明（annotations 含 ``list[...]`` 下标形式）。"""
        source = (SRC / "discovery" / "explorer.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        declared = {
            node.target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        }
        assert "anomalies" in declared, "DiscoveryResult 未声明 anomalies 字段"

        # 字段类型必须是 list[AnomalyFinding]，而不是 list[dict] 之类的弱类型
        annotations = {
            node.target.id: ast.unparse(node.annotation)
            for node in ast.walk(tree)
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        }
        assert "AnomalyFinding" in annotations["anomalies"]


# --------------------------------------------------- fixture 自检（Phase 0 交付物）


class TestDefectFixtures:
    def test_blank_and_normal_frames_are_distinguishable(self) -> None:
        from harmony_test_agent.analysis.screen import detect_blank_screen, screen_metrics

        white = fixture_path("blank_white.png")
        dark = fixture_path("blank_dark.png")

        white_metrics = screen_metrics(white)
        dark_metrics = screen_metrics(dark)

        assert white_metrics["mean"] >= 200 and white_metrics["stddev"] < 8.0
        assert dark_metrics["mean"] <= 40 and dark_metrics["stddev"] < 8.0
        assert detect_blank_screen(white) is not None
        assert detect_blank_screen(dark) is not None

    def test_layout_fixtures_are_parsed_into_elements(self) -> None:
        out_of_bounds = snapshot_from_layout_fixture(fixture_path("layout_out_of_bounds.json"))
        overlap = snapshot_from_layout_fixture(fixture_path("layout_overlap.json"))

        assert out_of_bounds.width == 1320 and out_of_bounds.height == 2232
        assert out_of_bounds.page_path == "pages/Index"
        assert len(out_of_bounds.elements) >= 3
        assert overlap.page_path == "pages/Feed"
        assert len(overlap.elements) >= 2

    def test_crash_and_freeze_hilog_fixtures_match_crash_patterns(self) -> None:
        from defect_fixtures import BUNDLE, hilog_appfreeze, hilog_clean, hilog_cppcrash

        from harmony_test_agent.analysis.hilog import parse_hilog

        crash_kinds = {
            item.kind for item in parse_hilog(hilog_cppcrash(), bundle_name=BUNDLE) if item.severity == "critical"
        }
        freeze_kinds = {
            item.kind for item in parse_hilog(hilog_appfreeze(), bundle_name=BUNDLE) if item.severity == "critical"
        }

        assert AnomalyKind.CPP_CRASH in crash_kinds
        assert {AnomalyKind.APP_FREEZE, AnomalyKind.ANR} <= freeze_kinds
        assert parse_hilog(hilog_clean(), bundle_name=BUNDLE) == []

    def test_hilog_fixtures_contain_only_log_lines(self) -> None:
        """hilog fixture 必须只含日志行。

        样本出处写在 ``tests/fixtures/defects/SOURCES.md``：如果塞进 ``.txt`` 里当注释，
        崩溃模式正则会命中说明文字里的 ``appfreeze`` / ``THREAD_BLOCK`` / ``LIFECYCLE_TIMEOUT``，
        把「来源说明」变成假 finding（真机验收时踩到过）。
        """
        from defect_fixtures import FIXTURES

        for name in ("hilog_cppcrash.txt", "hilog_appfreeze.txt", "hilog_clean.txt"):
            lines = [line for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines() if line.strip()]
            offenders = [line for line in lines if not line.startswith("0") and not line.startswith("# pidof")]
            assert offenders == [], f"{name} 含非日志行：{offenders[:3]}"
        assert (FIXTURES / "SOURCES.md").is_file(), "样本来源说明必须存在"

    def test_faultlog_index_fixture_lists_the_target_bundle(self) -> None:
        from defect_fixtures import faultlog_index

        assert "cppcrash-com.zhihu.hmos-20260210-101530" in faultlog_index()
