"""``XDeviceRunner`` 的 argv/env 契约、结果判定与相对证据路径。

全部用例 monkeypatch ``subprocess.run``，**不接触真实设备**（真实 ``xdevice`` 执行
属于 Phase 0/8 的真机门禁，不在单测范围内）。
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from harmony_test_agent.generation.xdevice_case import (
    XDEVICE_CASE_CLASS,
    XDEVICE_CASE_FILE,
    XDEVICE_MAIN_METHOD,
    XDEVICE_STRESS_METHOD,
    XDEVICE_SUITE_NAME,
    XDeviceArtifact,
)
from harmony_test_agent.runner import xdevice as xdevice_module
from harmony_test_agent.runner.xdevice import (
    XDeviceProject,
    XDeviceRunner,
    XDeviceRunResult,
    build_command_args,
    build_project,
)

REPORT_STAMP = "20240101-000000"


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


def project(tmp_path: Path, *, method: str = XDEVICE_MAIN_METHOD, device_sn: str = "SN-TEST-01") -> XDeviceProject:
    """构造一个已铺好目录的 xdevice 工程（不落任何真实用例文件）。"""
    root = tmp_path / "xdevice"
    (root / "testcases" / XDEVICE_SUITE_NAME).mkdir(parents=True)
    (root / "reports").mkdir()
    (root / "logs").mkdir()
    return XDeviceProject(
        root=root,
        suite=XDEVICE_SUITE_NAME,
        case_file=XDEVICE_CASE_FILE,
        class_name=XDEVICE_CASE_CLASS,
        method=method,
        device_sn=device_sn,
        timeout_seconds=600,
    )


class FakeRun:
    """记录 argv/env 的 ``subprocess.run`` 替身；可选写官方报告目录或抛异常。"""

    def __init__(
        self,
        *,
        stdout: str = "Result: PASS",
        stderr: str = "",
        returncode: int = 0,
        write_summary: bool = True,
        stamp: str = REPORT_STAMP,
        exception: BaseException | None = None,
    ):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.write_summary = write_summary
        self.stamp = stamp
        self.exception = exception
        self.args: list[str] = []
        self.kwargs: dict = {}
        self.calls = 0

    def summary_path(self) -> Path:
        return Path(self.kwargs["cwd"]) / "reports" / self.stamp / "report" / "summary_report.html"

    def __call__(self, args, **kwargs):
        self.calls += 1
        self.args = list(args)
        self.kwargs = kwargs
        if self.write_summary:
            report = self.summary_path()
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("<html>summary</html>", encoding="utf-8")
        if self.exception is not None:
            raise self.exception
        return subprocess.CompletedProcess(args, self.returncode, stdout=self.stdout, stderr=self.stderr)


def runner(tmp_path: Path, *, timeout: float = 900) -> XDeviceRunner:
    return XDeviceRunner(tmp_path / "home", timeout=timeout)


# ---------------------------------------------------------------------------
# argv / env / cwd
# ---------------------------------------------------------------------------


def test_execute_builds_the_documented_argv_and_environment(tmp_path: Path, monkeypatch) -> None:
    proj = project(tmp_path)
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)

    result = runner(tmp_path, timeout=123).execute(
        proj,
        attempt=2,
        params={"search_text": "OpenHarmony"},
        extra_env={"HARMONY_AGENT_EXTRA": "1"},
    )

    root = proj.root.resolve()
    assert fake.args == [
        sys.executable,
        "-m",
        "xdevice",
        "run",
        "harmonyAgent-HarmonyAgentCases-2",
        "-tc",
        "HarmonyAgentCase",
        "-sn",
        "SN-TEST-01",
        "-rp",
        str(root / "reports"),
        "-tcpath",
        str(root / "testcases"),
        "-le",
        str(root / "logs"),
    ]
    assert fake.kwargs["cwd"] == str(root)
    assert fake.kwargs["timeout"] == 123
    assert fake.kwargs["capture_output"] is True

    environment = fake.kwargs["env"]
    assert environment["HARMONY_AGENT_DEVICE_SN"] == "SN-TEST-01"
    assert json.loads(environment["HARMONY_AGENT_CASE_PARAMS"]) == {"search_text": "OpenHarmony"}
    assert environment["HARMONY_AGENT_EXTRA"] == "1"
    # HypiumRunner.environment 的隔离语义必须保留。
    assert environment["HOME"] == str((tmp_path / "home").resolve())
    assert environment["PYTHONIOENCODING"] == "utf-8"
    assert environment["HARMONY_AGENT_REPORT_DIR"] == str(root / "attempt-02")

    assert result.status == "passed"
    assert result.passed is True


def test_execute_defaults_params_to_empty_json(tmp_path: Path, monkeypatch) -> None:
    proj = project(tmp_path)
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    runner(tmp_path).execute(proj)
    assert fake.kwargs["env"]["HARMONY_AGENT_CASE_PARAMS"] == "{}"


def test_stress_method_is_forwarded_into_the_testcase_selector(tmp_path: Path) -> None:
    """``-tc`` 只承载用例名（模块名）。

    Phase 0 真机 spike（2026-09-21，xdevice 6.0.7.210）实测：``-tc`` 不接受
    ``suite/file/class/method`` 复合串，只接受用例名（不带扩展名/目录）；
    方法选择由任务描述里的 ``tests[].name`` 决定，因此压测方法不再体现在 argv 里。
    """
    proj = project(tmp_path, method=XDEVICE_STRESS_METHOD)
    args = build_command_args(proj, 3)
    assert args[4] == "harmonyAgent-HarmonyAgentCases-3"
    assert args[5] == "-tc"
    assert args[6] == Path(XDEVICE_CASE_FILE).stem
    # 压测用例由 testfile.json 的 tests[].name 选中，argv 里不得出现复合 selector。
    assert "/" not in args[6]
    assert XDEVICE_STRESS_METHOD not in args[6]


def test_execute_rejects_invalid_attempt(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        runner(tmp_path).execute(project(tmp_path), attempt=0)


# ---------------------------------------------------------------------------
# 结果判定
# ---------------------------------------------------------------------------


def test_pass_marker_with_report_is_passed(tmp_path: Path, monkeypatch) -> None:
    proj = project(tmp_path)
    fake = FakeRun(stdout="[Test Step] x\nResult: PASS\n")
    monkeypatch.setattr(subprocess, "run", fake)

    result = runner(tmp_path).execute(proj)

    assert result.status == "passed"
    assert result.passed is True
    assert result.error is None
    assert result.attempt == 1
    assert result.summary_report_path == f"reports/{REPORT_STAMP}/report/summary_report.html"
    assert result.report_dir == proj.root.resolve() / "reports" / REPORT_STAMP / "report"
    assert result.analysis is None


def test_fail_marker_is_failed_with_replay_error(tmp_path: Path, monkeypatch) -> None:
    proj = project(tmp_path)
    fake = FakeRun(stdout="Result: PASS\nResult: FAIL\n", returncode=1)
    monkeypatch.setattr(subprocess, "run", fake)

    result = runner(tmp_path).execute(proj)

    assert result.status == "failed"
    assert result.passed is False
    assert result.error is not None
    assert result.error.kind == "script_error"
    assert result.error.details["returncode"] == 1


def test_error_marker_is_failed(tmp_path: Path, monkeypatch) -> None:
    proj = project(tmp_path)
    monkeypatch.setattr(subprocess, "run", FakeRun(stdout="Result: ERROR\n", returncode=0))
    result = runner(tmp_path).execute(proj)
    assert result.status == "failed"
    assert result.passed is False
    assert result.error is not None


def test_timeout_is_timed_out(tmp_path: Path, monkeypatch) -> None:
    proj = project(tmp_path)
    fake = FakeRun(exception=subprocess.TimeoutExpired("xdevice", 5), write_summary=False)
    monkeypatch.setattr(subprocess, "run", fake)

    result = runner(tmp_path, timeout=5).execute(proj)

    assert result.status == "timed_out"
    assert result.passed is False
    assert result.command.timed_out is True
    assert result.command.returncode is None
    assert result.error is not None
    assert result.error.kind == "timeout"
    assert result.summary_report_path is None
    assert "attempt-01/stderr.log" in result.evidence_paths


def test_missing_report_is_invalid_result(tmp_path: Path, monkeypatch) -> None:
    proj = project(tmp_path)
    monkeypatch.setattr(subprocess, "run", FakeRun(stdout="Result: PASS", write_summary=False))

    result = runner(tmp_path).execute(proj)

    assert result.status == "invalid_result"
    assert result.passed is False
    assert result.error is not None
    assert result.error.kind == "missing_result"
    assert result.report_dir is None


def test_unparseable_stdout_is_invalid_result(tmp_path: Path, monkeypatch) -> None:
    proj = project(tmp_path)
    monkeypatch.setattr(subprocess, "run", FakeRun(stdout="all good, trust me", returncode=0))

    result = runner(tmp_path).execute(proj)

    assert result.status == "invalid_result"
    assert result.passed is False
    assert result.error is not None
    assert result.error.kind == "invalid_result"


def test_pass_marker_with_nonzero_exit_is_never_reported_as_passed(tmp_path: Path, monkeypatch) -> None:
    proj = project(tmp_path)
    monkeypatch.setattr(subprocess, "run", FakeRun(stdout="Result: PASS", returncode=1))

    result = runner(tmp_path).execute(proj)

    assert result.passed is False
    assert result.status == "failed"
    assert result.error is not None


def test_process_start_failure_is_error(tmp_path: Path, monkeypatch) -> None:
    proj = project(tmp_path)

    def exploding_run(*args, **kwargs):
        raise OSError("xdevice is not runnable")

    monkeypatch.setattr(subprocess, "run", exploding_run)
    result = runner(tmp_path).execute(proj)

    assert result.status == "error"
    assert result.passed is False
    assert result.error is not None
    assert result.error.kind == "process_exit"
    assert "attempt-01/stderr.log" in result.evidence_paths


def test_newest_summary_report_wins(tmp_path: Path, monkeypatch) -> None:
    proj = project(tmp_path)
    older = proj.root / "reports" / "20240101-000000" / "report" / "summary_report.html"
    newer = proj.root / "reports" / "20240102-000000" / "report" / "summary_report.html"
    for path, stamp in ((older, 1_700_000_000), (newer, 1_700_000_500)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<html>summary</html>", encoding="utf-8")
        os.utime(path, (stamp, stamp))

    monkeypatch.setattr(subprocess, "run", FakeRun(stdout="Result: PASS", write_summary=False))
    result = runner(tmp_path).execute(proj)

    assert result.status == "passed"
    assert result.summary_report_path == "reports/20240102-000000/report/summary_report.html"


# ---------------------------------------------------------------------------
# 证据落盘
# ---------------------------------------------------------------------------


def test_evidence_paths_are_project_root_relative_posix(tmp_path: Path, monkeypatch) -> None:
    proj = project(tmp_path)
    monkeypatch.setattr(subprocess, "run", FakeRun(stdout="Result: PASS"))

    result = runner(tmp_path).execute(proj, attempt=1)

    assert result.evidence_paths == [
        "attempt-01/command.json",
        "attempt-01/environment.json",
        "attempt-01/stderr.log",
        "attempt-01/stdout.log",
    ]
    assert all(not Path(path).is_absolute() for path in result.evidence_paths)
    assert all("\\" not in path for path in result.evidence_paths)

    attempt_dir = proj.root / "attempt-01"
    assert json.loads((attempt_dir / "command.json").read_text(encoding="utf-8"))["returncode"] == 0
    environment = json.loads((attempt_dir / "environment.json").read_text(encoding="utf-8"))
    assert environment["HARMONY_AGENT_DEVICE_SN"] == "SN-TEST-01"
    assert "PATH" not in environment
    assert (attempt_dir / "stdout.log").read_text(encoding="utf-8") == "Result: PASS"


def test_evidence_directory_is_recreated_per_attempt(tmp_path: Path, monkeypatch) -> None:
    proj = project(tmp_path)
    stale = proj.root / "attempt-01" / "stale.log"
    stale.parent.mkdir(parents=True)
    stale.write_text("left over", encoding="utf-8")

    monkeypatch.setattr(subprocess, "run", FakeRun(stdout="Result: PASS"))
    result = runner(tmp_path).execute(proj, attempt=1)

    assert not stale.exists()
    assert "attempt-01/stale.log" not in result.evidence_paths


# ---------------------------------------------------------------------------
# 工程描述与模块边界
# ---------------------------------------------------------------------------


def test_build_project_maps_artifact_fields(tmp_path: Path) -> None:
    root = tmp_path / "xdevice"
    case_file = root / "testcases" / XDEVICE_SUITE_NAME / XDEVICE_CASE_FILE
    artifact = XDeviceArtifact(
        root=root,
        suite=XDEVICE_SUITE_NAME,
        case_file=case_file,
        testfile_path=case_file.parent / "testfile.json",
        testlist_path=root / "testlist.txt",
        reports_dir=root / "reports",
        logs_dir=root / "logs",
        python_text="",
        testfile={},
        class_name=XDEVICE_CASE_CLASS,
        method=XDEVICE_STRESS_METHOD,
        timeout_seconds=900,
    )

    proj = build_project(artifact, "SN-42")

    assert proj.root == root
    assert proj.suite == XDEVICE_SUITE_NAME
    assert proj.case_file == XDEVICE_CASE_FILE
    assert proj.class_name == XDEVICE_CASE_CLASS
    assert proj.method == XDEVICE_STRESS_METHOD
    assert proj.device_sn == "SN-42"
    assert proj.timeout_seconds == 900


def test_run_result_model_exposes_the_frozen_fields() -> None:
    assert set(XDeviceRunResult.model_fields) == {
        "attempt",
        "command",
        "status",
        "passed",
        "report_dir",
        "summary_report_path",
        "evidence_paths",
        "error",
        "analysis",
    }


def test_runner_module_never_imports_the_analysis_package() -> None:
    source = Path(xdevice_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert "harmony_test_agent.analysis" not in source
    assert all(not module.endswith("analysis") for module in modules)
