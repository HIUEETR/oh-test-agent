"""「执行结果分析」报告章节与 HypiumRunner 分析挂钩的单元测试。

覆盖两处相位 7 增量：
- ``ReportBuilder._analysis_markup``：HTML 章节、转义、``None`` 短路、``report.json`` 载荷。
- ``HypiumRunner``：``environment(extra_env=...)`` 与 ``analysis_hook``（advisory，永不翻转 ``passed``）。
"""

from __future__ import annotations

import html
import json
import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest

from harmony_test_agent.models import (
    AnomalyFinding,
    AnomalyKind,
    ExecutionAnalysis,
    GeneratedArtifact,
    RunTrace,
)
from harmony_test_agent.reporting import ReportBuilder
from harmony_test_agent.runner import HypiumRunner
from harmony_test_agent.storage import ArtifactStore

RUN_ID = "run-analysis"
ATTEMPT_RELATIVE = "hypium/attempt-01/failure.jpeg"


def make_finding(**overrides: Any) -> AnomalyFinding:
    payload: dict[str, Any] = {
        "kind": AnomalyKind.WHITE_SCREEN,
        "severity": "warning",
        "summary_zh": "疑似白屏：mean=255.0, blank_ratio=0.99",
        "detail": "failure.jpeg 判定为浅色空白",
        "evidence": {"image_relative": ATTEMPT_RELATIVE, "mean": 255.0},
        "source": "screenshot",
    }
    payload.update(overrides)
    return AnomalyFinding(**payload)


def make_analysis(**overrides: Any) -> ExecutionAnalysis:
    payload: dict[str, Any] = {
        "subject": "hypium_replay",
        "subject_id": f"{RUN_ID}#attempt-01",
        "bundle_name": "com.example.app",
        "device_id": "device-1",
        "healthy": False,
        "findings": [make_finding()],
        "log_coverage": "partial",
    }
    payload.update(overrides)
    return ExecutionAnalysis(**payload)


def build_report(tmp_path: Path, analysis: ExecutionAnalysis | None) -> tuple[str, dict[str, Any]]:
    store = ArtifactStore(tmp_path / "runs")
    trace = RunTrace(
        run_id=RUN_ID,
        target_app_id="com.example.app",
        task="分析报告任务",
        device_id="device-1",
        analysis=analysis,
    )
    path = ReportBuilder(store).build(trace)
    document = path.read_text(encoding="utf-8")
    payload = json.loads((path.parent / "report.json").read_text(encoding="utf-8"))
    return document, payload


class TestAnalysisSection:
    def test_renders_heading_findings_and_artifact_link(self, tmp_path: Path) -> None:
        document, _ = build_report(tmp_path, make_analysis())

        assert "<h2>执行结果分析</h2>" in document
        assert "white_screen" in document
        assert "疑似白屏" in document
        assert "failure.jpeg 判定为浅色空白" in document
        assert f"/api/runs/{RUN_ID}/artifacts/{ATTEMPT_RELATIVE}" in document
        assert document.index("回放结果") < document.index("执行结果分析") < document.index("失败摘要")

    def test_none_analysis_omits_the_section(self, tmp_path: Path) -> None:
        document, payload = build_report(tmp_path, None)

        assert "执行结果分析" not in document
        assert payload["analysis"] is None

    def test_interpolated_text_is_html_escaped(self, tmp_path: Path) -> None:
        finding = make_finding(
            summary_zh="<script>alert('白屏')</script>",
            detail="<img src=x onerror=alert(1)>",
            evidence={"matched_line": "<b>crash</b>"},
        )
        document, _ = build_report(tmp_path, make_analysis(findings=[finding]))

        assert "<script>" not in document
        assert "<img src=x" not in document
        assert "&lt;script&gt;" in document
        assert "&lt;img src=x" in document
        assert "&lt;b&gt;crash&lt;/b&gt;" in document

    def test_unhealthy_banner_counts_findings(self, tmp_path: Path) -> None:
        document, _ = build_report(
            tmp_path,
            make_analysis(
                healthy=False,
                findings=[
                    make_finding(),
                    make_finding(kind=AnomalyKind.CPP_CRASH, severity="critical", source="hilog"),
                ],
            ),
        )

        assert "发现 2 项异常" in document
        assert "健康" not in document

    def test_healthy_banner_renders_health_marker(self, tmp_path: Path) -> None:
        document, _ = build_report(tmp_path, make_analysis(healthy=True, findings=[]))

        assert ">健康<" in document
        assert "项异常" not in document

    def test_symptom_reproduced_row_only_when_set(self, tmp_path: Path) -> None:
        reproduced, _ = build_report(tmp_path, make_analysis(symptom_reproduced=True))
        not_reproduced, _ = build_report(tmp_path, make_analysis(symptom_reproduced=False))
        absent, _ = build_report(tmp_path, make_analysis(symptom_reproduced=None))

        assert "症状复现" in reproduced and "已复现" in reproduced
        assert "症状复现" in not_reproduced and "未复现" in not_reproduced
        assert "症状复现" not in absent

    def test_info_finding_uses_ok_class_and_critical_uses_bad(self, tmp_path: Path) -> None:
        document, _ = build_report(
            tmp_path,
            make_analysis(
                findings=[
                    make_finding(severity="info", source="ui_dump"),
                    make_finding(kind=AnomalyKind.APP_FREEZE, severity="critical", source="faultlog"),
                ]
            ),
        )

        assert '<span class="ok">info</span>' in document
        assert '<span class="bad">critical</span>' in document

    def test_report_json_carries_analysis_payload(self, tmp_path: Path) -> None:
        _, payload = build_report(tmp_path, make_analysis())

        analysis = payload["analysis"]
        assert analysis["schema_version"] == 1
        assert analysis["subject"] == "hypium_replay"
        assert analysis["subject_id"] == f"{RUN_ID}#attempt-01"
        assert analysis["healthy"] is False
        assert analysis["log_coverage"] == "partial"
        assert analysis["findings"][0]["kind"] == "white_screen"
        assert analysis["findings"][0]["evidence"]["image_relative"] == ATTEMPT_RELATIVE

    def test_locator_stale_row_renders_selector_and_line(self, tmp_path: Path) -> None:
        """``LOCATOR_STALE`` 的证据渲染成可读的选择器 / 出错行，而不是 300 字符 JSON 摘录。"""
        selector = "BY.key('add_agenda_title-1790078405913')"
        source_line = f"driver.input_text({selector}, '生日')"
        finding = make_finding(
            kind=AnomalyKind.LOCATOR_STALE,
            severity="critical",
            summary_zh=f"脚本定位器在设备上已失效：{selector}",
            detail=f"第 57 行：{source_line}",
            evidence={
                "selector": selector,
                "script_line": 57,
                "source_line": source_line,
                "matched_in": "generated_result",
                "exception": "HypiumComponentNotFoundError",
            },
            source="stdout",
            phase="replay",
        )

        document, _ = build_report(tmp_path, make_analysis(findings=[finding]))

        assert "失效选择器：" in document
        assert f"<code>{html.escape(selector)}</code>" in document
        assert "出错位置：第 57 行" in document
        assert f"<small><code>{html.escape(source_line)}</code></small>" in document
        # 不再是原始 JSON 摘录。
        assert '{"selector"' not in document

    def test_locator_stale_row_escapes_interpolated_values(self, tmp_path: Path) -> None:
        finding = make_finding(
            kind=AnomalyKind.LOCATOR_STALE,
            severity="critical",
            summary_zh="脚本定位器在设备上已失效",
            evidence={
                "selector": "<img src=x onerror=alert(1)>",
                "script_line": 12,
                "source_line": "<script>alert('x')</script>",
            },
            source="stdout",
            phase="replay",
        )

        document, _ = build_report(tmp_path, make_analysis(findings=[finding]))

        assert "<img src=x" not in document
        assert "<script>" not in document
        assert "&lt;img src=x onerror=alert(1)&gt;" in document
        assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in document


PASSING_SCRIPT = """\
import json
import os
from pathlib import Path

report = Path(os.environ["HARMONY_AGENT_REPORT_DIR"])
(report / "generated_result.json").write_text(json.dumps({"passed": True}), encoding="utf-8")
"""


def make_artifact(tmp_path: Path) -> GeneratedArtifact:
    generated_dir = tmp_path / "runs" / RUN_ID / "generated"
    generated_dir.mkdir(parents=True)
    python_path = generated_dir / "test_run.py"
    python_path.write_text(PASSING_SCRIPT, encoding="utf-8")
    config_path = generated_dir / "test_run.json"
    config_path.write_text("{}", encoding="utf-8")
    metadata_path = generated_dir / "generation_metadata.json"
    metadata_path.write_text("{}", encoding="utf-8")
    return GeneratedArtifact(
        python_path=python_path,
        config_path=config_path,
        metadata_path=metadata_path,
        purpose="acceptance",
        replay_eligible=True,
    )


def patch_subprocess(monkeypatch: pytest.MonkeyPatch, *, passed: bool, returncode: int) -> None:
    def fake_run(command, **kwargs):
        report_dir = Path(kwargs["env"]["HARMONY_AGENT_REPORT_DIR"])
        payload = {"passed": passed}
        if not passed:
            payload["error"] = {"type": "AssertionError", "message": "元素缺失"}
        (report_dir / "generated_result.json").write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, returncode, stdout="out", stderr="err")

    monkeypatch.setattr(subprocess, "run", fake_run)


class TestRunnerAnalysisHook:
    def test_without_hook_analysis_is_none_and_behaviour_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_subprocess(monkeypatch, passed=True, returncode=0)
        generated = make_artifact(tmp_path)

        result = HypiumRunner(tmp_path / "home").execute(generated)

        assert result.analysis is None
        assert result.status == "passed"
        assert result.passed is True
        assert result.exit_code == 0
        assert result.error is None
        assert result.report_path == (tmp_path / "runs" / RUN_ID / "hypium" / "attempt-01").resolve()
        assert "hypium/attempt-01/stdout.log" in result.evidence_paths
        assert result.generated_result_path == "hypium/attempt-01/generated_result.json"
        dumped = json.loads((result.report_path / "environment.json").read_text(encoding="utf-8"))
        assert dumped["HARMONY_AGENT_REPORT_DIR"] == str(result.report_path)

    def test_hook_receives_attempt_dir_and_analysis_is_attached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_subprocess(monkeypatch, passed=True, returncode=0)
        generated = make_artifact(tmp_path)
        analysis = make_analysis()
        seen: list[tuple[str, Path]] = []

        def hook(replay, attempt_dir: Path) -> ExecutionAnalysis:
            seen.append((replay.status, attempt_dir))
            return analysis

        result = HypiumRunner(tmp_path / "home", analysis_hook=hook).execute(generated)

        assert seen == [("passed", (tmp_path / "runs" / RUN_ID / "hypium" / "attempt-01").resolve())]
        assert result.analysis is analysis
        assert result.passed is True
        assert result.status == "passed"

    def test_hook_returning_none_leaves_analysis_unset(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_subprocess(monkeypatch, passed=True, returncode=0)
        generated = make_artifact(tmp_path)

        result = HypiumRunner(tmp_path / "home", analysis_hook=lambda replay, attempt_dir: None).execute(generated)

        assert result.analysis is None
        assert result.passed is True

    def test_unhealthy_analysis_never_flips_passed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_subprocess(monkeypatch, passed=True, returncode=0)
        generated = make_artifact(tmp_path)
        analysis = make_analysis(healthy=False)

        result = HypiumRunner(tmp_path / "home", analysis_hook=lambda replay, attempt_dir: analysis).execute(generated)

        assert result.analysis is analysis
        assert result.passed is True
        assert result.status == "passed"
        assert result.error is None

    def test_failing_replay_keeps_status_when_hook_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        patch_subprocess(monkeypatch, passed=False, returncode=1)
        generated = make_artifact(tmp_path)

        def hook(replay, attempt_dir: Path) -> ExecutionAnalysis:
            raise RuntimeError("分析器崩了")

        with caplog.at_level(logging.WARNING):
            result = HypiumRunner(tmp_path / "home", analysis_hook=hook).execute(generated)

        assert result.status == "failed"
        assert result.passed is False
        assert result.error is not None and result.error.kind == "script_error"
        assert result.error.message == "元素缺失"
        assert result.analysis is None
        assert "analysis hook failed" in caplog.text

    def test_passing_replay_survives_raising_hook(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        patch_subprocess(monkeypatch, passed=True, returncode=0)
        generated = make_artifact(tmp_path)

        def hook(replay, attempt_dir: Path) -> ExecutionAnalysis:
            raise ValueError("boom")

        with caplog.at_level(logging.WARNING):
            result = HypiumRunner(tmp_path / "home", analysis_hook=hook).execute(generated)

        assert result.status == "passed"
        assert result.passed is True
        assert result.error is None
        assert result.analysis is None
        assert "analysis hook failed" in caplog.text


class TestEnvironment:
    def test_extra_env_merges_on_top_of_controlled_variables(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HARMONY_AGENT_REPORT_DIR", raising=False)
        home = tmp_path / "home"
        report_dir = tmp_path / "report"

        env = HypiumRunner(home).environment(report_dir, extra_env={"X": "1"})

        assert env["X"] == "1"
        assert env["HOME"] == str(home.resolve())
        assert env["USERPROFILE"] == str(home.resolve())
        assert env["PYTHONIOENCODING"] == "utf-8"
        assert env["HARMONY_AGENT_REPORT_DIR"] == str(report_dir.resolve())

    def test_single_positional_argument_still_works(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HARMONY_AGENT_REPORT_DIR", raising=False)
        report_dir = tmp_path / "report"

        env = HypiumRunner(tmp_path / "home").environment(report_dir)

        assert env["HARMONY_AGENT_REPORT_DIR"] == str(report_dir.resolve())
        assert "HARMONY_AGENT_TEST_EXTRA" not in env

    def test_no_argument_omits_report_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HARMONY_AGENT_REPORT_DIR", raising=False)

        env = HypiumRunner(tmp_path / "home").environment()

        assert "HARMONY_AGENT_REPORT_DIR" not in env
        assert env["HOME"] == str((tmp_path / "home").resolve())
