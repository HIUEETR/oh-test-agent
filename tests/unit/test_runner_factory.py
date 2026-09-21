"""``runner/factory.py`` 单测：统一构造、hook 语义、零额外成本与降级。

G1（接线全覆盖）与 G7（成本可控）在构造层的落地：hook 只在真正启用分析时挂上，
且 identity 解析失败 / analyzer 构造失败都只降级，绝不改变回放结论。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harmony_test_agent.analysis.service import ExecutionAnalyzer
from harmony_test_agent.config import Settings
from harmony_test_agent.models import CommandResult, ExecutionAnalysis, ReplayResult
from harmony_test_agent.runner import build_execution_analyzer, make_analysis_hook, make_hypium_runner
from harmony_test_agent.runner.factory import HypiumRunner

BUNDLE = "com.zhihu.hmos"


def _settings(tmp_path: Path, **overrides) -> Settings:
    payload = {
        "runtime_dir": tmp_path / "runs",
        "database_path": tmp_path / "agent.db",
        "target_profile_path": None,
        "profiles_dir": tmp_path / "profiles",
        "runtime_home": tmp_path / "runtime-home",
        "agent_provider": "mock",
    }
    payload.update(overrides)
    return Settings(**payload)


def _attempt_dir(tmp_path: Path, run_id: str = "run-1", attempt: int = 1) -> Path:
    path = tmp_path / "runs" / run_id / "hypium" / f"attempt-{attempt:02d}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _replay(attempt_dir: Path, *, status: str = "passed") -> ReplayResult:
    return ReplayResult(
        attempt=1,
        command=CommandResult(command="python", returncode=0 if status == "passed" else 1),
        report_path=attempt_dir,
        passed=status == "passed",
        status=status,  # type: ignore[arg-type]
        evidence_paths=["hypium/attempt-01/stdout.log"],
    )


class _RecordingAnalyzer:
    """把调用参数记下来的假分析器（不碰设备）。"""

    def __init__(self, analysis: ExecutionAnalysis | None = None) -> None:
        self.calls: list[dict] = []
        self.analysis = analysis or ExecutionAnalysis(
            subject="hypium_replay", subject_id="run-1#attempt-01", bundle_name=BUNDLE, device_id="SN1"
        )

    def analyze_replay(self, replay, **kwargs) -> ExecutionAnalysis:
        self.calls.append({"kind": "replay", "replay": replay, **kwargs})
        return self.analysis

    def analyze_dc_script(self, replay, **kwargs) -> ExecutionAnalysis:
        self.calls.append({"kind": "dc_script", "replay": replay, **kwargs})
        return self.analysis.model_copy(update={"subject": "dc_script", "subject_id": kwargs.get("session_id", "")})


class TestMakeHypiumRunner:
    def test_analyzer_is_attached_by_default(self, tmp_path: Path) -> None:
        runner = make_hypium_runner(_settings(tmp_path), bundle_name=BUNDLE, device_id="SN1")

        assert isinstance(runner, HypiumRunner)
        assert runner.analysis_hook is not None

    def test_analyze_false_leaves_the_hook_off(self, tmp_path: Path) -> None:
        """用例库接入点：分析已在别处完成，避免同一 attempt 被分析两次。"""
        runner = make_hypium_runner(_settings(tmp_path), analyze=False)

        assert runner.analysis_hook is None

    def test_analysis_disabled_by_settings_leaves_the_hook_off(self, tmp_path: Path) -> None:
        runner = make_hypium_runner(_settings(tmp_path, case_analysis_enabled=False), bundle_name=BUNDLE)

        assert runner.analysis_hook is None

    def test_explicit_analyzer_is_used_instead_of_building_one(self, tmp_path: Path) -> None:
        analyzer = _RecordingAnalyzer()
        runner = make_hypium_runner(_settings(tmp_path), analyzer=analyzer, bundle_name=BUNDLE, device_id="SN1")
        attempt_dir = _attempt_dir(tmp_path)

        assert runner.analysis_hook is not None
        runner.analysis_hook(_replay(attempt_dir), attempt_dir)

        assert len(analyzer.calls) == 1
        call = analyzer.calls[0]
        assert call["run_dir"] == tmp_path / "runs" / "run-1"
        assert call["bundle_name"] == BUNDLE
        assert call["device_id"] == "SN1"


class TestAnalysisHook:
    def test_run_dir_is_derived_from_the_attempt_directory(self, tmp_path: Path) -> None:
        analyzer = _RecordingAnalyzer()
        hook = make_analysis_hook(analyzer, bundle_name=BUNDLE, device_id="SN1")
        attempt_dir = _attempt_dir(tmp_path, run_id="run-abc", attempt=3)

        assert hook is not None
        hook(_replay(attempt_dir), attempt_dir)

        assert analyzer.calls[0]["run_dir"] == tmp_path / "runs" / "run-abc"

    def test_dc_subject_routes_to_analyze_dc_script(self, tmp_path: Path) -> None:
        analyzer = _RecordingAnalyzer()
        hook = make_analysis_hook(
            analyzer, subject="dc_script", bundle_name=BUNDLE, device_id="SN1", session_id="dc-2026"
        )
        attempt_dir = _attempt_dir(tmp_path, run_id="dc-2026")

        assert hook is not None
        analysis = hook(_replay(attempt_dir), attempt_dir)

        assert analyzer.calls[0]["kind"] == "dc_script"
        assert analyzer.calls[0]["session_id"] == "dc-2026"
        assert analysis is not None and analysis.subject == "dc_script"

    def test_lazy_resolvers_supply_identity_and_failures_degrade(self, tmp_path: Path) -> None:
        analyzer = _RecordingAnalyzer()
        hook = make_analysis_hook(
            analyzer,
            bundle_resolver=lambda: (_ for _ in ()).throw(RuntimeError("identity unavailable")),
            device_resolver=lambda: "SN9",
        )
        attempt_dir = _attempt_dir(tmp_path)

        assert hook is not None
        hook(_replay(attempt_dir), attempt_dir)

        # 解析失败降级为空 bundle，但不抛异常、不影响设备侧行为
        assert analyzer.calls[0]["bundle_name"] == ""
        assert analyzer.calls[0]["device_id"] == "SN9"

    def test_none_analyzer_yields_no_hook(self) -> None:
        assert make_analysis_hook(None) is None

    def test_symptom_kind_is_forwarded(self, tmp_path: Path) -> None:
        analyzer = _RecordingAnalyzer()
        hook = make_analysis_hook(analyzer, bundle_name=BUNDLE, symptom_kind="unresponsive")
        attempt_dir = _attempt_dir(tmp_path)

        assert hook is not None
        hook(_replay(attempt_dir), attempt_dir)

        assert analyzer.calls[0]["symptom_kind"] == "unresponsive"


class TestBuildExecutionAnalyzer:
    def test_returns_analyzer_with_the_configured_log_policy(self, tmp_path: Path) -> None:
        analyzer = build_execution_analyzer(_settings(tmp_path, analysis_collect_logs_on_success=True))

        assert isinstance(analyzer, ExecutionAnalyzer)
        assert analyzer.collect_logs_on_success is True

    def test_clean_pass_keeps_logs_off_by_default(self, tmp_path: Path) -> None:
        analyzer = build_execution_analyzer(_settings(tmp_path))

        assert isinstance(analyzer, ExecutionAnalyzer)
        assert analyzer.collect_logs_on_success is False
        assert analyzer._needs_logs(passed=True) is False
        assert analyzer._needs_logs(passed=False) is True

    def test_disabled_setting_returns_none(self, tmp_path: Path) -> None:
        assert build_execution_analyzer(_settings(tmp_path, case_analysis_enabled=False)) is None

    def test_missing_log_policy_setting_defaults_to_off(self) -> None:
        """settings 缺 analysis_collect_logs_on_success 时只降级为默认关，不抛异常。"""

        class Minimal:
            case_analysis_enabled = True

        analyzer = build_execution_analyzer(Minimal())

        assert isinstance(analyzer, ExecutionAnalyzer)
        assert analyzer.collect_logs_on_success is False


class TestHookNeverFlipsReplayVerdict:
    def test_raising_hook_is_swallowed_by_the_runner(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """红线：分析是 advisory —— hook 抛异常时回放结论原样返回。"""
        import logging

        script = tmp_path / "diagnostic.py"
        script.write_text(
            "import json, os\n"
            "from pathlib import Path\n"
            "report = Path(os.environ['HARMONY_AGENT_REPORT_DIR'])\n"
            "(report / 'generated_result.json').write_text(json.dumps({'passed': True}), encoding='utf-8')\n",
            encoding="utf-8",
        )
        runner = make_hypium_runner(
            _settings(tmp_path),
            analyzer=_RecordingAnalyzer(),
            bundle_name=BUNDLE,
            device_id="SN1",
        )
        assert runner.analysis_hook is not None

        def boom(replay, attempt_dir):
            raise RuntimeError("analyzer exploded")

        runner.analysis_hook = boom
        with caplog.at_level(logging.WARNING):
            result = runner.execute_diagnostic(script)

        assert result.analysis is None
        assert result.status == "passed"
        assert result.passed is True
        assert "analysis hook failed" in caplog.text
