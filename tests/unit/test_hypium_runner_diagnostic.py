"""HypiumRunner.execute_diagnostic：直流脚本诊断执行（不做验收资格门禁）。"""

from __future__ import annotations

import json
from pathlib import Path

from harmony_test_agent.models import GeneratedArtifact
from harmony_test_agent.runner import HypiumRunner

PASSING_SCRIPT = """\
import json
import os
from pathlib import Path

report = Path(os.environ["HARMONY_AGENT_REPORT_DIR"])
(report / "generated_result.json").write_text(json.dumps({"passed": True}), encoding="utf-8")
print("diagnostic replay done")
"""

FAILING_SCRIPT = """\
import json
import os
from pathlib import Path

report = Path(os.environ["HARMONY_AGENT_REPORT_DIR"])
(report / "generated_result.json").write_text(
    json.dumps({"passed": False, "error": {"type": "AssertionError", "message": "element missing"}}),
    encoding="utf-8",
)
"""


def write_dc_script(tmp_path: Path, name: str, body: str, run_id: str = "dc-20260101T000000Z-aaaa1111") -> Path:
    generated = tmp_path / "runs" / run_id / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    script = generated / name
    script.write_text(body, encoding="utf-8")
    script.with_suffix(".json").write_text(
        json.dumps({"purpose": "dc_recording", "replay_eligible": False}), encoding="utf-8"
    )
    return script


def make_runner(tmp_path: Path) -> HypiumRunner:
    return HypiumRunner(tmp_path / "runtime-home", timeout=120)


class TestExecuteDiagnostic:
    def test_runs_ineligible_script_and_records_evidence(self, tmp_path: Path) -> None:
        script = write_dc_script(tmp_path, "dc_test_dc_1.py", PASSING_SCRIPT)

        result = make_runner(tmp_path).execute_diagnostic(script)

        assert result.status == "passed"
        assert result.passed is True
        assert result.exit_code == 0
        assert result.error is None
        # 诊断执行同样落盘证据，且位于会话目录的 hypium/attempt-01
        assert "hypium/attempt-01/stdout.log" in result.evidence_paths
        assert "hypium/attempt-01/command.json" in result.evidence_paths
        assert "hypium/attempt-01/generated_result.json" in result.evidence_paths
        assert (script.parent.parent / "hypium" / "attempt-01" / "stdout.log").is_file()

    def test_script_failure_is_reported_with_error_details(self, tmp_path: Path) -> None:
        script = write_dc_script(tmp_path, "dc_test_dc_2.py", FAILING_SCRIPT)

        result = make_runner(tmp_path).execute_diagnostic(script)

        assert result.status == "failed"
        assert result.passed is False
        assert result.error is not None
        assert result.error.kind == "script_error"
        assert "element missing" in result.error.message

    def test_attempt_directories_are_isolated(self, tmp_path: Path) -> None:
        script = write_dc_script(tmp_path, "dc_test_dc_3.py", PASSING_SCRIPT)
        runner = make_runner(tmp_path)

        first = runner.execute_diagnostic(script, attempt=1)
        second = runner.execute_diagnostic(script, attempt=2)

        assert first.report_path != second.report_path
        assert first.report_path.name == "attempt-01"
        assert second.report_path.name == "attempt-02"

    def test_missing_script_raises(self, tmp_path: Path) -> None:
        runner = make_runner(tmp_path)

        try:
            runner.execute_diagnostic(tmp_path / "runs" / "dc-x" / "generated" / "absent.py")
        except ValueError as exc:
            assert "script not found" in str(exc)
        else:  # pragma: no cover - 明确失败更清晰
            raise AssertionError("missing script must raise ValueError")


class TestRunnableGateUnchanged:
    """回归护栏：正式 ``execute()`` 仍然拦截**不可执行**（runnable=False）的产物。"""

    def test_execute_refuses_unrunnable_artifact(self, tmp_path: Path) -> None:
        script = write_dc_script(tmp_path, "dc_test_dc_4.py", PASSING_SCRIPT)
        generated = GeneratedArtifact(
            python_path=script,
            config_path=script.with_suffix(".json"),
            metadata_path=script.with_suffix(".json"),
            purpose="diagnostic",
            replay_eligible=False,
            runnable_blockers=["app identity is a placeholder (com.example.app/EntryAbility)"],
        )

        result = make_runner(tmp_path).execute(generated)

        assert result.status == "ineligible"
        assert result.passed is False
        assert result.error is not None and result.error.kind == "ineligible"
        assert result.error.message == "generated script is not runnable"
        assert result.error.details["runnable_blockers"] == [
            "app identity is a placeholder (com.example.app/EntryAbility)"
        ]
