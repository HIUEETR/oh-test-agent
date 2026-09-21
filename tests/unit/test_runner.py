import json
import subprocess
from pathlib import Path

from harmony_test_agent.models import GeneratedArtifact
from harmony_test_agent.runner import HypiumRunner


def artifact(tmp_path: Path, *, eligible: bool = True) -> GeneratedArtifact:
    generated_dir = tmp_path / "runs" / "run-test" / "generated"
    generated_dir.mkdir(parents=True)
    python_path = generated_dir / "test_run.py"
    python_path.write_text("raise SystemExit(0)\n", encoding="utf-8")
    config_path = generated_dir / "test_run.json"
    config_path.write_text("{}", encoding="utf-8")
    metadata_path = generated_dir / "generation_metadata.json"
    metadata_path.write_text("{}", encoding="utf-8")
    # 不可执行只能由 G3 的两条物理必要条件造成（例如没有可回放动作），
    # 质量顾虑（confidence_factors）不再让 execute() 拒绝脚本。
    return GeneratedArtifact(
        python_path=python_path,
        config_path=config_path,
        metadata_path=metadata_path,
        purpose="acceptance" if eligible else "diagnostic",
        replay_eligible=eligible,
        confidence="high" if eligible else "low",
        runnable_blockers=[] if eligible else ["script has no replayable action"],
        incomplete_reasons=[] if eligible else ["source trace is incomplete"],
    )


def test_runner_rejects_unrunnable_artifact_without_starting_process(tmp_path: Path, monkeypatch) -> None:
    generated = artifact(tmp_path, eligible=False)

    def unexpected_run(*args, **kwargs):
        raise AssertionError("subprocess must not be called")

    monkeypatch.setattr(subprocess, "run", unexpected_run)
    result = HypiumRunner(tmp_path / "home").execute(generated)

    assert result.status == "ineligible"
    assert result.passed is False
    assert result.error and result.error.kind == "ineligible"
    assert result.error.message == "generated script is not runnable"
    assert result.error.details["runnable_blockers"] == ["script has no replayable action"]
    assert result.exit_code is None
    assert all(not Path(path).is_absolute() for path in result.evidence_paths)
    assert "hypium/attempt-01/command.json" in result.evidence_paths


def test_runner_parses_generated_result_error_and_relative_evidence(tmp_path: Path, monkeypatch) -> None:
    generated = artifact(tmp_path)

    def fake_run(command, **kwargs):
        report_dir = Path(kwargs["env"]["HARMONY_AGENT_REPORT_DIR"])
        (report_dir / "generated_result.json").write_text(
            json.dumps({"passed": False, "error": {"type": "AssertionError", "message": "未找到搜索"}}),
            encoding="utf-8",
        )
        (report_dir / "failure.jpeg").write_bytes(b"jpeg")
        return subprocess.CompletedProcess(command, 1, stdout="out", stderr="err")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = HypiumRunner(tmp_path / "home").execute(generated)

    assert result.status == "failed"
    assert result.exit_code == 1
    assert result.error and result.error.kind == "script_error"
    assert result.error.message == "未找到搜索"
    assert result.error.details["exit_code"] == 1
    assert result.generated_result_path == "hypium/attempt-01/generated_result.json"
    assert "hypium/attempt-01/failure.jpeg" in result.evidence_paths


def test_runner_reports_invalid_result_and_timeout(tmp_path: Path, monkeypatch) -> None:
    generated = artifact(tmp_path)

    def invalid_run(command, **kwargs):
        report_dir = Path(kwargs["env"]["HARMONY_AGENT_REPORT_DIR"])
        (report_dir / "generated_result.json").write_text("{not-json", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", invalid_run)
    invalid = HypiumRunner(tmp_path / "home").execute(generated)
    assert invalid.status == "invalid_result"
    assert invalid.error and invalid.error.kind == "invalid_result"

    def timeout_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 1, output=b"partial", stderr=b"timeout")

    monkeypatch.setattr(subprocess, "run", timeout_run)
    timed_out = HypiumRunner(tmp_path / "home", timeout=1).execute(generated, attempt=2)
    assert timed_out.status == "timed_out"
    assert timed_out.timed_out is True
    assert timed_out.exit_code is None
    assert timed_out.error and timed_out.error.kind == "timeout"
    assert timed_out.command.stdout == "partial"
