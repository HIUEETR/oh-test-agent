"""官方 xdevice + devicetest 引擎执行器。

与 ``runner/hypium.py``（独立 ``UiDriver`` 脚本）并列的第二条回放链路：把
``generation/xdevice_case.py`` 产出的工程目录交给 ``python -m xdevice run`` 执行，
再把子进程输出与官方报告目录归一化为 ``XDeviceRunResult``。

判定原则（**绝不误报 PASS**）：

* ``timed_out`` → ``timed_out``；
* 否则扫 stdout 里的 ``Result: PASS|FAIL|ERROR`` 标记，并定位最新
  ``reports/*/report/summary_report.html``；
* ``passed`` ⟺ 退出码 0 **且** 至少一个 PASS **且** 没有 FAIL/ERROR；
* 报告目录缺失 → ``ReplayError(kind="missing_result")`` + ``status="invalid_result"``；
* 其余解析不出一律 ``invalid_result``。

证据落盘镜像 ``HypiumRunner``：写进 ``<project.root>/attempt-NN/``，并以
**project root 相对 posix 路径**返回。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..generation.xdevice_case import XDeviceArtifact
from ..models import CommandResult, ExecutionAnalysis, ReplayError
from .hypium import HypiumRunner

# ---------------------------------------------------------------------------
# xdevice CLI 契约（命令行参数在 ``xdevice/_core/command/console.py`` 里注册）
# ---------------------------------------------------------------------------

XDEVICE_MODULE = "xdevice"
XDEVICE_RUN_ACTION = "run"
XDEVICE_TESTCASE_FLAG = "-tc"
XDEVICE_DEVICE_FLAG = "-sn"
XDEVICE_REPORT_FLAG = "-rp"
XDEVICE_TESTCASE_PATH_FLAG = "-tcpath"
XDEVICE_LOG_FLAG = "-le"

#: task 位置参数前缀：``harmonyAgent-<case>-<attempt>``。
XDEVICE_TASK_PREFIX = "harmonyAgent"

#: 官方报告里的一行结果标记（历史假设；xdevice 6.0.7 实测并不打印，保留作兼容）。
RESULT_MARKER_PATTERN = re.compile(r"Result:\s*(PASS|FAIL|ERROR)", re.I)
#: 真实的结果汇总行：``[Test Summary: ... total: 1, passed: 0, failed: 1, ...]``。
SUMMARY_REPORT_PATTERN = re.compile(
    r"Test Summary:.*?total:\s*(\d+),\s*passed:\s*(\d+),\s*failed:\s*(\d+)",
    re.I,
)
#: ``-rp`` 下的报告目录树：``<ts>/report/summary_report.html``。
SUMMARY_REPORT_NAME = "summary_report.html"
REPORT_SUBDIR = "report"
REPORTS_DIR_NAME = "reports"
#: ``run <task>`` 强制加载的任务配置目录：``<root>/config/<task>.json``（Phase 0 实测）。
TASK_CONFIG_DIR_NAME = "config"
EVIDENCE_DIR_TEMPLATE = "attempt-{attempt:02d}"
#: 证据目录里必须保留的环境变量（其余变量可能含凭据，不落盘）。
EVIDENCE_ENV_KEYS = (
    "HOME",
    "USERPROFILE",
    "PYTHONIOENCODING",
    "HARMONY_AGENT_REPORT_DIR",
    "HARMONY_AGENT_DEVICE_SN",
    "HARMONY_AGENT_CASE_PARAMS",
)

XDeviceStatus = Literal["passed", "failed", "timed_out", "error", "invalid_result"]


@dataclass
class XDeviceProject:
    """一次 xdevice 执行所需的最小工程描述。"""

    root: Path
    suite: str
    case_file: str
    class_name: str
    method: str
    device_sn: str
    timeout_seconds: int


class XDeviceRunResult(BaseModel):
    """一次 xdevice 执行的状态、官方报告位置与工程内相对证据路径。"""

    attempt: int
    command: CommandResult
    status: XDeviceStatus
    passed: bool
    report_dir: Path | None = None
    summary_report_path: str | None = None
    evidence_paths: list[str] = Field(default_factory=list)
    error: ReplayError | None = None
    analysis: ExecutionAnalysis | None = None


def build_project(artifact: XDeviceArtifact, device_sn: str) -> XDeviceProject:
    """把 emitter 产物转换为 runner 的工程描述。"""
    return XDeviceProject(
        root=Path(artifact.root),
        suite=artifact.suite,
        case_file=Path(artifact.case_file).name,
        class_name=artifact.class_name,
        method=artifact.method,
        device_sn=device_sn,
        timeout_seconds=int(artifact.timeout_seconds),
    )


def build_task_name(project: XDeviceProject, attempt: int) -> str:
    """构造 xdevice 任务名。

    ``XDeviceProject``（已冻结）不携带 ``case_id``，因此用 suite 作为任务身份，
    保证同一工程重复执行时任务名稳定且互不覆盖。
    """
    return f"{XDEVICE_TASK_PREFIX}-{project.suite}-{attempt}"


def build_command_args(project: XDeviceProject, attempt: int) -> list[str]:
    """构造 ``python -m xdevice run ...`` 的 argv（cwd 由调用方设为 ``project.root``）。

    Phase 0 真机 spike（2026-09-21，xdevice 6.0.7.210）实测结论：

    * ``run <task>`` 会强制加载 ``<cwd>/config/<task>.json``（缺失即
      ``Environment-0101015``）；但该任务描述**只用来加载 kits**
      （``Task._load_task`` 仅做 ``get_kit_instances``），并不声明用例清单。
      用例清单仍然来自 ``-tc`` / ``-tf`` / ``-l``。
    * ``-tc`` 接受的是**用例名**（模块名，不带扩展名、不带目录），
      不接受计划假设的 ``suite/file/class/method`` 复合串，也不接受路径：
      传复合串会在用例发现阶段抛 ``FileNotFoundError``；传路径则会被
      ``DeviceTestDriver._get_test_list`` 判为「Test is ignored」，
      最终以 ``Script-0203012 No test list found`` 结束。
      用例文件由 ``testfile.json`` 的 ``driver.py_file``（相对 ``-tcpath``）解析。
    """
    root = Path(project.root)
    return [
        sys.executable,
        "-m",
        XDEVICE_MODULE,
        XDEVICE_RUN_ACTION,
        build_task_name(project, attempt),
        XDEVICE_TESTCASE_FLAG,
        Path(project.case_file).stem,
        XDEVICE_DEVICE_FLAG,
        project.device_sn,
        XDEVICE_REPORT_FLAG,
        str(root / REPORTS_DIR_NAME),
        XDEVICE_TESTCASE_PATH_FLAG,
        str(root / "testcases"),
        XDEVICE_LOG_FLAG,
        str(root / "logs"),
    ]


class XDeviceRunner:
    """执行官方 xdevice 工程，并把结果归一化为 ``XDeviceRunResult``。"""

    def __init__(self, runtime_home: Path, timeout: float = 900):
        self.runtime_home = Path(runtime_home).resolve()
        self.runtime_home.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        # 复用 HypiumRunner 的环境构造（HOME / PYTHONIOENCODING / 报告目录隔离）。
        self._environment_source = HypiumRunner(self.runtime_home, timeout=timeout)

    def execute(
        self,
        project: XDeviceProject,
        attempt: int = 1,
        *,
        params: dict | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> XDeviceRunResult:
        """执行一次 xdevice 回放；任何解析不出的结果都不会被判为 PASS。"""
        if attempt < 1:
            raise ValueError("attempt must be >= 1")
        root = Path(project.root).resolve()
        attempt_dir = (root / EVIDENCE_DIR_TEMPLATE.format(attempt=attempt)).resolve()
        if not attempt_dir.is_relative_to(root):
            raise ValueError("attempt directory escaped the xdevice project root")
        if attempt_dir.exists():
            shutil.rmtree(attempt_dir)
        attempt_dir.mkdir(parents=True)

        args = build_command_args(project, attempt)
        command_text = subprocess.list2cmdline(args)
        # Phase 0 真机 spike 实测：`run <task>` 会强制加载 `<cwd>/config/<task>.json`，
        # 缺失时直接以 Environment-0101015 失败（即使同时给了 -tc/-tf）。
        self._materialize_task_config(root, project, attempt)
        environment = self._environment(attempt_dir, project, params=params, extra_env=extra_env)
        started = time.monotonic()
        try:
            process = subprocess.run(
                args,
                cwd=str(root),
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )
            command = CommandResult(
                command=command_text,
                args=args,
                returncode=process.returncode,
                stdout=process.stdout or "",
                stderr=process.stderr or "",
                duration_ms=round((time.monotonic() - started) * 1000),
            )
        except subprocess.TimeoutExpired as exc:
            command = CommandResult(
                command=command_text,
                args=args,
                returncode=None,
                stdout=self._timeout_text(exc.stdout),
                stderr=self._timeout_text(exc.stderr),
                timed_out=True,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            self._write_evidence(attempt_dir, command, environment)
            return self._result(
                attempt=attempt,
                command=command,
                root=root,
                attempt_dir=attempt_dir,
                status="timed_out",
                passed=False,
                error=ReplayError(
                    kind="timeout",
                    message=f"xdevice run timed out after {self.timeout} seconds",
                ),
            )
        except OSError as exc:
            command = CommandResult(
                command=command_text,
                args=args,
                returncode=None,
                stderr=f"{type(exc).__name__}: {exc}",
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            self._write_evidence(attempt_dir, command, environment)
            return self._result(
                attempt=attempt,
                command=command,
                root=root,
                attempt_dir=attempt_dir,
                status="error",
                passed=False,
                error=ReplayError(kind="process_exit", message=f"cannot start xdevice: {exc}"),
            )

        self._write_evidence(attempt_dir, command, environment)
        report_dir, summary_report = self._locate_report(root)
        status, passed, error = self._judge(command, report_dir, summary_report)
        return self._result(
            attempt=attempt,
            command=command,
            root=root,
            attempt_dir=attempt_dir,
            status=status,
            passed=passed,
            error=error,
            report_dir=report_dir,
            summary_report=summary_report,
        )

    # -- 环境与证据 --------------------------------------------------------

    def _environment(
        self,
        attempt_dir: Path,
        project: XDeviceProject,
        *,
        params: dict | None,
        extra_env: dict[str, str] | None,
    ) -> dict[str, str]:
        """``HypiumRunner.environment`` 之上叠加设备号与用例参数（双通道注入）。"""
        environment = self._environment_source.environment(attempt_dir)
        for key, value in (extra_env or {}).items():
            environment[str(key)] = str(value)
        environment["HARMONY_AGENT_DEVICE_SN"] = project.device_sn
        environment["HARMONY_AGENT_CASE_PARAMS"] = json.dumps(params or {}, ensure_ascii=False)
        return environment

    @staticmethod
    def _timeout_text(value: str | bytes | None) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value or ""

    @staticmethod
    def _write_evidence(attempt_dir: Path, command: CommandResult, environment: dict[str, str]) -> None:
        """把命令、输出与脱敏环境写进 attempt 目录，保证失败也可审计。"""
        (attempt_dir / "stdout.log").write_text(command.stdout, encoding="utf-8")
        (attempt_dir / "stderr.log").write_text(command.stderr, encoding="utf-8")
        (attempt_dir / "command.json").write_text(command.model_dump_json(indent=2), encoding="utf-8")
        safe_environment = {key: environment[key] for key in EVIDENCE_ENV_KEYS if key in environment}
        (attempt_dir / "environment.json").write_text(
            json.dumps(safe_environment, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    # -- 结果判定 ----------------------------------------------------------

    @staticmethod
    def _materialize_task_config(root: Path, project: XDeviceProject, attempt: int) -> Path | None:
        """把工程的 ``testfile.json`` 复制成 ``<root>/config/<task>.json``。

        xdevice 的 ``run <task>`` 语义是「加载 ``config/<task>.json`` 作为任务描述」，
        因此任务名与配置文件名必须一致。工程里没有 ``config/`` 时自动创建；
        ``testfile.json`` 不存在则什么都不做（由 xdevice 自己报缺配置）。
        """
        testfile = root / "testcases" / project.suite / "testfile.json"
        if not testfile.is_file():
            return None
        config_dir = root / TASK_CONFIG_DIR_NAME
        config_dir.mkdir(parents=True, exist_ok=True)
        target = config_dir / f"{build_task_name(project, attempt)}.json"
        shutil.copyfile(testfile, target)
        return target

    @staticmethod
    def _locate_report(root: Path) -> tuple[Path | None, Path | None]:
        """定位官方 ``summary_report.html``。

        Phase 0 真机 spike（2026-09-21，xdevice 6.0.7.210）实测：报告**直接**写在
        ``-rp`` 目录下（日志原文 ``Generate test report: file:///<rp>/summary_report.html``），
        并不存在计划假设的 ``<ts>/report/`` 中间层。这里两种形态都接受，
        新形态优先，缺失才算 ``invalid_result``。
        """
        reports_root = root / REPORTS_DIR_NAME
        if not reports_root.is_dir():
            return None, None
        direct = reports_root / SUMMARY_REPORT_NAME
        if direct.is_file():
            return reports_root.resolve(), direct.resolve()
        candidates = [path for path in reports_root.glob(f"*/{REPORT_SUBDIR}/{SUMMARY_REPORT_NAME}") if path.is_file()]
        if not candidates:
            return None, None
        newest = max(candidates, key=lambda path: path.stat().st_mtime)
        return newest.parent.resolve(), newest.resolve()

    @staticmethod
    def _judge(
        command: CommandResult,
        report_dir: Path | None,
        summary_report: Path | None,
    ) -> tuple[XDeviceStatus, bool, ReplayError | None]:
        """把子进程输出与报告存在性翻译为状态；宁可 ``invalid_result`` 也不误报 PASS。

        Phase 0 真机 spike（2026-09-21）实测：xdevice 6.0.7.210 **不打印**
        ``Result: PASS|FAIL|ERROR``；真实结果出现在
        ``[Test Summary: ... total: 1, passed: 0, failed: 1, ...]`` 这一行。
        因此这里以该汇总行为主判据，``Result:`` 标记仅作兼容保留。
        """
        if command.timed_out:
            return (
                "timed_out",
                False,
                ReplayError(kind="timeout", message="xdevice run timed out"),
            )
        if report_dir is None or summary_report is None:
            return (
                "invalid_result",
                False,
                ReplayError(
                    kind="missing_result",
                    message=f"xdevice produced no {REPORTS_DIR_NAME}/{SUMMARY_REPORT_NAME}",
                    details={"returncode": command.returncode},
                ),
            )
        stdout = command.stdout or ""
        summary_match = SUMMARY_REPORT_PATTERN.search(stdout)
        if summary_match is not None:
            total, passed_count, failed_count = (int(value) for value in summary_match.groups())
            passed = command.returncode == 0 and total > 0 and failed_count == 0 and passed_count > 0
            if passed:
                return "passed", True, None
            error = ReplayError(
                kind="script_error",
                message=f"xdevice reported total={total} passed={passed_count} failed={failed_count}",
                details={
                    "returncode": command.returncode,
                    "total": total,
                    "passed": passed_count,
                    "failed": failed_count,
                },
            )
            return "failed", False, error
        markers = [marker.upper() for marker in RESULT_MARKER_PATTERN.findall(stdout)]
        if not markers:
            return (
                "invalid_result",
                False,
                ReplayError(
                    kind="invalid_result",
                    message="xdevice stdout has no 'Test Summary: ... passed: N, failed: M' line",
                    details={"returncode": command.returncode},
                ),
            )
        failures = sorted({marker for marker in markers if marker in {"FAIL", "ERROR"}})
        passed = command.returncode == 0 and "PASS" in markers and not failures
        if passed:
            return "passed", True, None
        error = ReplayError(
            kind="script_error",
            message=f"xdevice result markers {sorted(set(markers))} with exit code {command.returncode}",
            details={"returncode": command.returncode, "markers": sorted(set(markers))},
        )
        return "failed", False, error

    # -- 结果组装 ----------------------------------------------------------

    def _result(
        self,
        *,
        attempt: int,
        command: CommandResult,
        root: Path,
        attempt_dir: Path,
        status: XDeviceStatus,
        passed: bool,
        error: ReplayError | None,
        report_dir: Path | None = None,
        summary_report: Path | None = None,
    ) -> XDeviceRunResult:
        evidence = sorted(self._relative(root, path) for path in attempt_dir.iterdir() if path.is_file())
        return XDeviceRunResult(
            attempt=attempt,
            command=command,
            status=status,
            passed=passed,
            report_dir=report_dir,
            summary_report_path=self._relative(root, summary_report) if summary_report is not None else None,
            evidence_paths=evidence,
            error=error,
        )

    @staticmethod
    def _relative(root: Path, path: Path) -> str:
        """project root 相对 posix 路径（镜像 ``HypiumRunner`` 的证据路径约定）。"""
        return Path(path).resolve().relative_to(root).as_posix()


__all__ = [
    "RESULT_MARKER_PATTERN",
    "XDeviceProject",
    "XDeviceRunResult",
    "XDeviceRunner",
    "XDeviceStatus",
    "build_command_args",
    "build_project",
    "build_task_name",
]
