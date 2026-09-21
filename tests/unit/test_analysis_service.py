"""``analysis/service.py`` 单测：失败 / 干净回放、白屏 + 崩溃、无响应、压测、symptom 映射与异常吞噬。"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from harmony_test_agent.analysis.service import (
    DEFAULT_MEMORY_GROWTH_THRESHOLD_KB,
    ExecutionAnalyzer,
)
from harmony_test_agent.models import (
    AnomalyFinding,
    AnomalyKind,
    CommandResult,
    ExecutionAnalysis,
    ReplayResult,
    RunTrace,
    ScenarioKind,
    ScreenSnapshot,
    TargetAppProfile,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "analysis"
BUNDLE = "com.zhihu.hmos"
CRASH_HILOG = "02-10 10:00:01.200  4321  4321 E C01310/com.zhihu.hmos/Crash: cppcrash happened\n"

CLEAN_LAYOUT: dict = {
    "attributes": {"pagePath": "pages/Index", "visible": "true", "bounds": "[0,0][1320,2232]"},
    "children": [
        {
            "attributes": {
                "key": "p2_home_titlebar_search",
                "type": "Button",
                "text": "搜索",
                "visible": "true",
                "bounds": "[100,80][300,160]",
            }
        }
    ],
}


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _png(path: Path, value: int = 255, size: tuple[int, int] = (300, 300)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("L", size, value).save(path, format="PNG")
    return path


class FakeDevice:
    """最小设备替身：``_run`` / ``collect_logs`` / ``collect_ui_hierarchy`` 全部受控。"""

    def __init__(
        self,
        *,
        hilog: str = "",
        listing: str = "",
        head: str = "",
        hierarchy: dict | None = None,
        pidof: str = "",
    ) -> None:
        self.hilog = hilog
        self.listing = listing
        self.head = head
        self.hierarchy = hierarchy
        self.pidof = pidof
        self.calls: list[tuple[str, ...]] = []
        self.log_calls = 0
        self.hierarchy_calls = 0

    def _run(self, *args: str, timeout: float | None = None, device: bool = True) -> CommandResult:
        self.calls.append(tuple(args))
        stdout = ""
        if args[:2] == ("shell", "ls"):
            stdout = self.listing
        elif args[:2] == ("shell", "head"):
            stdout = self.head
        elif args[:2] == ("shell", "pidof"):
            stdout = self.pidof
        return CommandResult(command="hdc " + " ".join(args), args=list(args), returncode=0, stdout=stdout)

    def collect_logs(self, output_path: Path) -> CommandResult:
        self.log_calls += 1
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(self.hilog, encoding="utf-8")
        return CommandResult(
            command="hdc shell hilog -x",
            args=["shell", "hilog", "-x"],
            returncode=0,
            stdout=self.hilog,
        )

    def collect_ui_hierarchy(self) -> dict:
        self.hierarchy_calls += 1
        if self.hierarchy is None:
            raise RuntimeError("no hierarchy available")
        return self.hierarchy


def _replay(
    tmp_path: Path,
    *,
    attempt: int = 1,
    status: str = "failed",
    stdout: str = "",
    stderr: str = "",
    generated_result: dict | None = None,
    images: tuple[tuple[str, int], ...] = (),
    layouts: tuple[dict, ...] = (),
) -> tuple[Path, Path, ReplayResult]:
    """构造一次 attempt 的 run 目录、attempt 目录与 ReplayResult。"""
    run_dir = tmp_path / "run-1"
    attempt_dir = run_dir / "hypium" / f"attempt-{attempt:02d}"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    for name, value in images:
        _png(attempt_dir / name, value)
    if layouts:
        layout_dir = run_dir / "layouts"
        layout_dir.mkdir(parents=True, exist_ok=True)
        base = 1_700_000_000
        for index, dump in enumerate(layouts):
            path = layout_dir / f"{index + 1:03d}.json"
            path.write_text(json.dumps(dump, ensure_ascii=False), encoding="utf-8")
            stamp = base + index
            os.utime(path, (stamp, stamp))
    passed = status == "passed"
    command = CommandResult(
        command="python standalone/test_case.py",
        args=[],
        returncode=0 if passed else 1,
        stdout=stdout,
        stderr=stderr,
        timed_out=status == "timed_out",
    )
    replay = ReplayResult(
        attempt=attempt,
        command=command,
        report_path=attempt_dir,
        passed=passed,
        status=status,
        generated_result=generated_result,
    )
    return run_dir, attempt_dir, replay


def _trace(
    *,
    snapshots: tuple[ScreenSnapshot, ...] = (),
    replays: tuple[ReplayResult, ...] = (),
    scenario: ScenarioKind | None = None,
    device_id: str = "SN1",
) -> RunTrace:
    return RunTrace(
        run_id="run-live",
        target_app_id="zhihu",
        task="t",
        device_id=device_id,
        scenario=scenario,
        profile_snapshot=TargetAppProfile(
            target_app_id="zhihu",
            display_name="知乎++",
            bundle_name=BUNDLE,
        ),
        snapshots=list(snapshots),
        replays=list(replays),
        started_at=datetime(2026, 2, 10, 10, 0, 0, tzinfo=UTC),
    )


def _kinds(analysis: ExecutionAnalysis) -> list[str]:
    return sorted(finding.kind.value for finding in analysis.findings)


def test_failed_replay_with_white_frame_and_crash_logs_two_findings(tmp_path):
    device = FakeDevice(hilog=CRASH_HILOG, hierarchy=CLEAN_LAYOUT, pidof="4321")
    run_dir, attempt_dir, replay = _replay(tmp_path, stdout="cppcrash happened", images=(("failure.jpeg", 255),))
    analyzer = ExecutionAnalyzer(device_factory=lambda device_id: device)

    analysis = analyzer.analyze_replay(
        replay, run_dir=run_dir, bundle_name=BUNDLE, device_id="SN1", symptom_kind="crash"
    )

    assert _kinds(analysis) == ["cppcrash", "white_screen"]
    assert analysis.healthy is False
    assert analysis.subject == "hypium_replay"
    assert analysis.subject_id == "run-1#attempt-01"
    assert analysis.bundle_name == BUNDLE
    assert analysis.device_id == "SN1"
    assert analysis.symptom_reproduced is True
    assert analysis.log_coverage == "full"
    assert analysis.metrics["log"]["notes"]["faultlog_findings"] == 0
    assert analysis.metrics["log"]["notes"]["pidof"] == ["4321"]
    assert analysis.metrics["log"]["notes"]["hilog_findings"] == 1

    white = next(finding for finding in analysis.findings if finding.kind is AnomalyKind.WHITE_SCREEN)
    assert white.severity == "critical"  # 有崩溃佐证 → warning 提升为 critical
    assert white.source == "screenshot"
    assert white.evidence["blank_kind"] == "light"
    assert white.evidence["frame_group"] == "attempt_images"
    assert white.evidence["image_relative"] == "hypium/attempt-01/failure.jpeg"

    crash = next(finding for finding in analysis.findings if finding.kind is AnomalyKind.CPP_CRASH)
    assert crash.severity == "critical"
    assert crash.source == "hilog"

    assert (attempt_dir / "hilog_after.txt").is_file()
    assert (attempt_dir / "layout_after.json").is_file()
    assert device.log_calls == 1
    assert device.hierarchy_calls == 1

    payload = json.loads((attempt_dir / "analysis.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["healthy"] is False
    assert payload["subject"] == "hypium_replay"
    assert [finding["kind"] for finding in payload["findings"]] == [finding.kind.value for finding in analysis.findings]


def test_failed_replay_reports_faultlog_hits_and_persists_evidence(tmp_path):
    device = FakeDevice(
        listing=_fixture("faultlog_listing.txt"),
        head=_fixture("faultlog_cppcrash_head.txt"),
        hierarchy=CLEAN_LAYOUT,
    )
    run_dir, attempt_dir, replay = _replay(tmp_path, status="timed_out")
    analyzer = ExecutionAnalyzer(device_factory=lambda device_id: device)

    analysis = analyzer.analyze_replay(
        replay,
        run_dir=run_dir,
        bundle_name=BUNDLE,
        device_id="SN1",
        trace=_trace(),
        symptom_kind="crash",
    )

    assert _kinds(analysis) == ["appfreeze", "cppcrash", "cppcrash"]
    assert analysis.healthy is False
    assert analysis.symptom_reproduced is True
    assert analysis.log_coverage == "full"
    assert analysis.metrics["log"]["notes"]["faultlog_findings"] == 3
    assert (attempt_dir / "faultlog_cppcrash-com.zhihu.hmos-20260210-102000.txt").is_file()
    faultlog = analysis.findings[0]
    assert faultlog.source == "faultlog"
    assert faultlog.evidence["reason"].startswith("Signal:SIGSEGV")
    assert faultlog.evidence["faultlog_relative"] == (
        "hypium/attempt-01/faultlog_cppcrash-com.zhihu.hmos-20260210-102000.txt"
    )


def test_clean_pass_skips_hilog_but_still_scans_faultlog(tmp_path):
    device = FakeDevice(listing="")
    run_dir, attempt_dir, replay = _replay(tmp_path, status="passed")
    analyzer = ExecutionAnalyzer(device_factory=lambda device_id: device)

    analysis = analyzer.analyze_replay(replay, run_dir=run_dir, bundle_name=BUNDLE, device_id="SN1")

    assert analysis.findings == []
    assert analysis.healthy is True
    assert analysis.symptom_reproduced is None
    assert analysis.log_coverage == "full"
    assert analysis.metrics["log"]["notes"]["hilog_skipped"] == "clean_pass"
    assert device.log_calls == 0
    assert device.hierarchy_calls == 0
    assert not (attempt_dir / "hilog_after.txt").exists()
    assert (attempt_dir / "analysis.json").is_file()


def test_clean_pass_without_device_reports_unavailable_coverage(tmp_path):
    run_dir, _, replay = _replay(tmp_path, status="passed")

    analysis = ExecutionAnalyzer().analyze_replay(replay, run_dir=run_dir, bundle_name=BUNDLE, device_id="")

    assert analysis.findings == []
    assert analysis.healthy is True
    assert analysis.log_coverage == "unavailable"
    assert analysis.metrics["log"]["degradations"] == []
    assert analysis.metrics["log"]["notes"]["device"] == "unavailable"


def test_failed_replay_without_device_degrades_but_stays_valid(tmp_path):
    run_dir, attempt_dir, replay = _replay(tmp_path, status="failed", stdout="WaitForIdle timeout")

    analysis = ExecutionAnalyzer().analyze_replay(replay, run_dir=run_dir, bundle_name=BUNDLE, device_id="SN1")

    codes = [item["code"] for item in analysis.metrics["log"]["degradations"]]
    assert codes == ["device_unavailable"]
    assert analysis.log_coverage == "unavailable"
    assert analysis.healthy is True
    assert replay.passed is False
    assert replay.status == "failed"
    assert (attempt_dir / "analysis.json").is_file()


def test_timeout_marker_plus_stale_layouts_is_page_unresponsive(tmp_path):
    run_dir, _, replay = _replay(
        tmp_path,
        status="timed_out",
        stdout="WaitForIdle timeout after 10s",
        layouts=(CLEAN_LAYOUT, CLEAN_LAYOUT),
    )

    analysis = ExecutionAnalyzer().analyze_replay(replay, run_dir=run_dir, bundle_name=BUNDLE, device_id="")

    unresponsive = next(finding for finding in analysis.findings if finding.kind is AnomalyKind.PAGE_UNRESPONSIVE)
    assert unresponsive.severity == "critical"
    assert unresponsive.evidence["timeout_marker"] is True
    assert unresponsive.evidence["layout_stale"] is True
    assert analysis.healthy is False
    assert analysis.metrics["layout"]["stale"] is True


def test_timeout_marker_without_stall_is_not_unresponsive(tmp_path):
    run_dir, _, replay = _replay(tmp_path, status="timed_out", stdout="UiTimedOutError")
    analysis = ExecutionAnalyzer().analyze_replay(replay, run_dir=run_dir, bundle_name=BUNDLE, device_id="")
    assert analysis.findings == []
    assert analysis.healthy is True


def test_stress_stall_without_timeout_marker_is_warning(tmp_path):
    run_dir, _, replay = _replay(tmp_path, status="passed", layouts=(CLEAN_LAYOUT, CLEAN_LAYOUT))
    trace = _trace(replays=(replay,), scenario=ScenarioKind.STRESS)

    analysis = ExecutionAnalyzer().analyze_replay(
        replay, run_dir=run_dir, bundle_name=BUNDLE, device_id="", trace=trace
    )

    unresponsive = next(finding for finding in analysis.findings if finding.kind is AnomalyKind.PAGE_UNRESPONSIVE)
    assert unresponsive.severity == "warning"
    assert unresponsive.evidence["timeout_marker"] is False
    assert unresponsive.evidence["stress_context"] is True


def test_layout_anomalies_come_from_run_layouts(tmp_path):
    raw = json.loads((FIXTURES / "layout_raw.json").read_text(encoding="utf-8"))
    run_dir, _, replay = _replay(tmp_path, status="passed", layouts=(raw,))

    analysis = ExecutionAnalyzer().analyze_replay(replay, run_dir=run_dir, bundle_name=BUNDLE, device_id="")

    layout_findings = [finding for finding in analysis.findings if finding.kind is AnomalyKind.LAYOUT_ANOMALY]
    assert len(layout_findings) == 4
    assert {finding.evidence["rule"] for finding in layout_findings} == {
        "L1_out_of_bounds",
        "L2_text_overlap",
        "L3_clipped_child",
        "L4_zero_size_visible",
    }
    assert all(finding.evidence["layout_relative"] == "layouts/001.json" for finding in layout_findings)
    assert analysis.metrics["layout"]["file"] == "layouts/001.json"
    assert analysis.healthy is False


def test_stress_memory_growth_above_threshold_is_reported(tmp_path):
    growth = DEFAULT_MEMORY_GROWTH_THRESHOLD_KB + 1024
    generated = {
        "passed": True,
        "stress": {
            "iterations_completed": 10,
            "memory_samples": [
                {"iteration": 1, "pss_kb": 100_000},
                {"iteration": 10, "pss_kb": 100_000 + growth},
            ],
        },
    }
    run_dir, _, replay = _replay(tmp_path, status="passed", generated_result=generated)

    analysis = ExecutionAnalyzer().analyze_replay(replay, run_dir=run_dir, bundle_name=BUNDLE, device_id="")

    memory = next(finding for finding in analysis.findings if finding.kind is AnomalyKind.MEMORY_GROWTH)
    assert memory.severity == "warning"
    assert memory.source == "stress_stats"
    assert memory.evidence["growth_kb"] == growth
    assert analysis.metrics["stress"]["iterations_completed"] == 10
    assert analysis.healthy is False


def test_stress_memory_within_threshold_is_ignored(tmp_path):
    generated = {
        "passed": True,
        "stress": {
            "iterations_completed": 5,
            "memory_samples": [
                {"iteration": 1, "pss_kb": 100_000},
                {"iteration": 5, "pss_kb": 100_500},
            ],
        },
    }
    run_dir, _, replay = _replay(tmp_path, status="passed", generated_result=generated)

    analysis = ExecutionAnalyzer().analyze_replay(replay, run_dir=run_dir, bundle_name=BUNDLE, device_id="")

    assert analysis.findings == []
    assert analysis.healthy is True
    assert analysis.metrics["stress"]["growth_kb"] == 500


@pytest.mark.parametrize(
    ("symptom_kind", "expected"),
    [
        ("crash", True),
        ("white_screen", True),
        ("freeze", False),
        ("unresponsive", False),
        ("layout", False),
        ("functional", True),
        ("other", None),
    ],
)
def test_symptom_kind_mapping(tmp_path, symptom_kind, expected):
    device = FakeDevice(hilog=CRASH_HILOG, hierarchy=CLEAN_LAYOUT)
    run_dir, _, replay = _replay(tmp_path, status="failed", images=(("failure.jpeg", 255),))
    analyzer = ExecutionAnalyzer(device_factory=lambda device_id: device)

    analysis = analyzer.analyze_replay(
        replay, run_dir=run_dir, bundle_name=BUNDLE, device_id="SN1", symptom_kind=symptom_kind
    )

    assert analysis.symptom_reproduced is expected


def test_functional_symptom_ignores_soft_checkpoint_failures(tmp_path):
    soft_only = {
        "passed": False,
        "soft_failures": [{"message": "soft checkpoint failed"}],
        "checkpoints": [{"name": "soft-1", "soft": True, "passed": False}],
    }
    run_dir, _, replay = _replay(tmp_path, status="failed", generated_result=soft_only)
    analyzer = ExecutionAnalyzer(device_factory=lambda device_id: FakeDevice(hierarchy=CLEAN_LAYOUT))

    analysis = analyzer.analyze_replay(
        replay, run_dir=run_dir, bundle_name=BUNDLE, device_id="SN1", symptom_kind="functional"
    )

    assert analysis.metrics["hard_checkpoint_failure"] is False
    assert analysis.symptom_reproduced is False

    hard = {"passed": False, "checkpoints": [{"name": "hard-1", "passed": False}]}
    run_dir2, _, replay2 = _replay(tmp_path / "second", status="failed", generated_result=hard)
    analysis2 = analyzer.analyze_replay(
        replay2, run_dir=run_dir2, bundle_name=BUNDLE, device_id="SN1", symptom_kind="functional"
    )

    assert analysis2.metrics["hard_checkpoint_failure"] is True
    assert analysis2.symptom_reproduced is True


def test_step_failure_is_swallowed_and_marked_as_degradation(tmp_path, monkeypatch):
    device = FakeDevice(hilog=CRASH_HILOG, hierarchy=CLEAN_LAYOUT)
    run_dir, _, replay = _replay(tmp_path, status="failed", images=(("failure.jpeg", 255),))
    analyzer = ExecutionAnalyzer(device_factory=lambda device_id: device)

    def _boom(*args, **kwargs):
        raise RuntimeError("blank scanner exploded")

    monkeypatch.setattr("harmony_test_agent.analysis.service.detect_blank_screen", _boom)

    analysis = analyzer.analyze_replay(replay, run_dir=run_dir, bundle_name=BUNDLE, device_id="SN1")

    assert _kinds(analysis) == ["cppcrash"]
    codes = [item["code"] for item in analysis.metrics["log"]["degradations"]]
    assert "blank_screen_failed" in codes
    assert replay.passed is False
    assert replay.status == "failed"
    assert replay.analysis is None


def test_unexpected_pipeline_failure_returns_fallback_analysis(tmp_path, monkeypatch):
    run_dir, attempt_dir, replay = _replay(tmp_path, status="failed", images=(("failure.jpeg", 255),))

    def _boom(self, analysis_input, *, fetch_layout):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(ExecutionAnalyzer, "_analyze", _boom)

    analysis = ExecutionAnalyzer().analyze_replay(
        replay, run_dir=run_dir, bundle_name=BUNDLE, device_id="SN1", symptom_kind="crash"
    )

    assert analysis.healthy is True
    assert analysis.findings == []
    assert analysis.symptom_reproduced is None
    assert analysis.log_coverage == "unavailable"
    assert analysis.metrics["log"]["degradations"][0]["code"] == "analysis_failed"
    assert replay.passed is False
    assert replay.status == "failed"
    assert (attempt_dir / "analysis.json").is_file()


def test_analysis_can_be_attached_to_replay_result(tmp_path):
    run_dir, _, replay = _replay(tmp_path, status="passed")

    analysis = ExecutionAnalyzer().analyze_replay(replay, run_dir=run_dir, bundle_name=BUNDLE, device_id="")
    replay.analysis = analysis  # runner/hypium.py 的 analysis_hook 挂载点（该文件不属于本阶段改动范围）

    assert replay.analysis is analysis
    assert json.loads(replay.model_dump_json())["analysis"]["subject"] == "hypium_replay"


def test_analyze_run_merges_attempt_findings_and_writes_analysis(tmp_path):
    run_dir = tmp_path / "run-live"
    (run_dir / "layouts").mkdir(parents=True)
    blank = _png(run_dir / "screens" / "final.png", 255)
    snapshot = ScreenSnapshot(
        snapshot_id="s1",
        run_id="run-live",
        image_path=blank,
        image_sha256="a" * 64,
        width=1320,
        height=2232,
    )
    attempt_dir = run_dir / "hypium" / "attempt-01"
    attempt_dir.mkdir(parents=True)
    replay = ReplayResult(
        attempt=1,
        command=CommandResult(command="python test_case.py", args=[], returncode=1),
        report_path=attempt_dir,
        passed=False,
        status="failed",
    )
    replay.analysis = ExecutionAnalysis(
        subject="hypium_replay",
        subject_id="run-live#attempt-01",
        findings=[
            AnomalyFinding(
                kind=AnomalyKind.APP_FREEZE,
                severity="critical",
                summary_zh="应用冻屏",
                source="hilog",
            )
        ],
    )
    trace = _trace(snapshots=(snapshot,), replays=(replay,))

    analysis = ExecutionAnalyzer().analyze_run(trace, run_dir)

    assert analysis.subject == "live_run"
    assert analysis.subject_id == "run-live"
    assert analysis.bundle_name == BUNDLE
    assert analysis.device_id == "SN1"
    assert _kinds(analysis) == ["appfreeze", "white_screen"]
    assert analysis.healthy is False
    assert analysis.log_coverage == "unavailable"
    assert analysis.metrics["log"]["need_logs"] is True
    white = next(finding for finding in analysis.findings if finding.kind is AnomalyKind.WHITE_SCREEN)
    assert white.evidence["image_relative"] == "screens/final.png"
    assert (run_dir / "analysis.json").is_file()


def test_analyze_run_bug_reproduction_uses_functional_symptom(tmp_path):
    run_dir = tmp_path / "run-live"
    (run_dir / "layouts").mkdir(parents=True)
    attempt_dir = run_dir / "hypium" / "attempt-01"
    attempt_dir.mkdir(parents=True)
    replay = ReplayResult(
        attempt=1,
        command=CommandResult(command="python test_case.py", args=[], returncode=1),
        report_path=attempt_dir,
        passed=False,
        status="failed",
    )
    trace = _trace(replays=(replay,), scenario=ScenarioKind.BUG_REPRODUCTION)

    analysis = ExecutionAnalyzer().analyze_run(trace, run_dir)

    assert analysis.symptom_reproduced is True
    assert analysis.metrics["hard_checkpoint_failure"] is True


def test_analyze_xdevice_detects_white_screen_from_evidence(tmp_path):
    project_root = tmp_path / "project"
    report_dir = project_root / "reports"
    report_dir.mkdir(parents=True)
    _png(report_dir / "failure.jpeg", 255)
    result = SimpleNamespace(
        passed=False,
        status="failed",
        command=CommandResult(command="xdevice run", args=[], returncode=1),
        evidence_paths=["reports/failure.jpeg"],
        report_dir=report_dir,
    )

    analysis = ExecutionAnalyzer().analyze_xdevice(
        result, project_root=project_root, bundle_name=BUNDLE, device_id="SN1", symptom_kind="white_screen"
    )

    assert _kinds(analysis) == ["white_screen"]
    assert analysis.subject == "xdevice_run"
    assert analysis.subject_id == "reports"
    assert analysis.symptom_reproduced is True
    assert analysis.metrics["hard_checkpoint_failure"] is True
    assert analysis.log_coverage == "unavailable"
    assert (report_dir / "analysis.json").is_file()


def test_analyze_xdevice_timeout_with_stale_layouts(tmp_path):
    project_root = tmp_path / "project"
    report_dir = project_root / "reports"
    layout_dir = report_dir / "layouts"
    layout_dir.mkdir(parents=True)
    for index in range(2):
        path = layout_dir / f"{index + 1:03d}.json"
        path.write_text(json.dumps(CLEAN_LAYOUT, ensure_ascii=False), encoding="utf-8")
        stamp = 1_700_000_000 + index
        os.utime(path, (stamp, stamp))
    result = SimpleNamespace(
        passed=False,
        status="timed_out",
        command=CommandResult(
            command="xdevice run", args=[], returncode=None, stdout="UiTimedOutError", timed_out=True
        ),
        evidence_paths=["reports/layouts/001.json", "reports/layouts/002.json"],
        report_dir=report_dir,
    )

    analysis = ExecutionAnalyzer().analyze_xdevice(
        result, project_root=project_root, bundle_name=BUNDLE, device_id="", symptom_kind="unresponsive"
    )

    unresponsive = next(finding for finding in analysis.findings if finding.kind is AnomalyKind.PAGE_UNRESPONSIVE)
    assert unresponsive.severity == "critical"
    assert unresponsive.evidence["layout_stale"] is True
    assert analysis.symptom_reproduced is True
    assert analysis.metrics["layout"]["file"] == "layouts/002.json"


def test_analyze_xdevice_passed_run_is_healthy(tmp_path):
    project_root = tmp_path / "project"
    report_dir = project_root / "reports"
    report_dir.mkdir(parents=True)
    result = SimpleNamespace(
        passed=True,
        status="passed",
        command=CommandResult(command="xdevice run", args=[], returncode=0),
        evidence_paths=[],
        report_dir=report_dir,
    )

    analysis = ExecutionAnalyzer().analyze_xdevice(
        result, project_root=project_root, bundle_name=BUNDLE, device_id="SN1"
    )

    assert analysis.findings == []
    assert analysis.healthy is True
    assert analysis.symptom_reproduced is None
