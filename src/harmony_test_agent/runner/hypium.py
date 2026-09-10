"""在隔离运行目录中执行已生成且具备回放资格的 Hypium 脚本。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..models import CommandResult, GeneratedArtifact, ReplayError, ReplayResult


class HypiumRunner:
    """管理回放资格、超时、生成结果解析和 Run 内相对证据路径。"""

    def __init__(self, runtime_home: Path, timeout: float = 300):
        self.runtime_home = runtime_home.resolve()
        self.runtime_home.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout

    def environment(self, report_dir: Path | None = None) -> dict[str, str]:
        """构造回放子进程环境，并把 HOME 与报告目录限制到运行产物区域。"""
        env = os.environ.copy()
        env["HOME"] = str(self.runtime_home)
        env["USERPROFILE"] = str(self.runtime_home)
        env["PYTHONIOENCODING"] = "utf-8"
        if report_dir is not None:
            env["HARMONY_AGENT_REPORT_DIR"] = str(report_dir.resolve())
        return env

    def execute(self, generated: GeneratedArtifact, attempt: int = 1) -> ReplayResult:
        """执行一次回放，并结构化保留资格、进程和 generated_result 错误。"""
        run_dir = generated.python_path.parent.parent.resolve()
        hypium_dir = (run_dir / "hypium").resolve()
        attempt_dir = (hypium_dir / f"attempt-{attempt:02d}").resolve()
        if not attempt_dir.is_relative_to(hypium_dir):
            raise ValueError("attempt directory escaped run hypium directory")
        if attempt_dir.exists():
            shutil.rmtree(attempt_dir)
        attempt_dir.mkdir(parents=True)
        command_args = [sys.executable, str(generated.python_path)]
        command_text = subprocess.list2cmdline(command_args)
        if not generated.replay_eligible:
            command = CommandResult(command=command_text, args=command_args, returncode=None)
            error = ReplayError(
                kind="ineligible",
                message="generated artifact is not eligible for replay",
                details={"purpose": generated.purpose, "incomplete_reasons": generated.incomplete_reasons},
            )
            self._write_evidence(attempt_dir, command, self.environment(attempt_dir))
            return self._result(
                attempt=attempt,
                command=command,
                run_dir=run_dir,
                attempt_dir=attempt_dir,
                status="ineligible",
                error=error,
            )

        environment = self.environment(attempt_dir)
        started = time.monotonic()
        try:
            process = subprocess.run(
                command_args,
                cwd=str(generated.python_path.parent),
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
                args=command_args,
                returncode=process.returncode,
                stdout=process.stdout,
                stderr=process.stderr,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
        except subprocess.TimeoutExpired as exc:
            command = CommandResult(
                command=command_text,
                args=command_args,
                returncode=None,
                stdout=self._timeout_text(exc.stdout),
                stderr=self._timeout_text(exc.stderr),
                timed_out=True,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
        self._write_evidence(attempt_dir, command, environment)
        result_path = attempt_dir / "generated_result.json"
        generated_result, result_error = self._read_generated_result(result_path)

        if command.timed_out:
            status = "timed_out"
            error = ReplayError(kind="timeout", message=f"Hypium replay timed out after {self.timeout} seconds")
        elif result_error is not None:
            status = "invalid_result" if result_error.kind == "invalid_result" else "failed"
            error = result_error
        elif generated_result and not generated_result["passed"]:
            status = "failed"
            error = self._script_error(generated_result)
            if command.returncode not in (None, 0):
                error.details["exit_code"] = command.returncode
        elif command.returncode != 0:
            status = "failed"
            error = ReplayError(
                kind="process_exit",
                message=f"Hypium replay exited with code {command.returncode}",
            )
        else:
            status = "passed"
            error = None

        return self._result(
            attempt=attempt,
            command=command,
            run_dir=run_dir,
            attempt_dir=attempt_dir,
            status=status,
            error=error,
            generated_result=generated_result,
            generated_result_path=result_path if result_path.exists() else None,
        )

    def execute_repeated(self, generated: GeneratedArtifact, attempts: int = 3) -> list[ReplayResult]:
        """按独立尝试目录重复执行回放，并按执行顺序返回全部结果。"""
        return [self.execute(generated, attempt=index) for index in range(1, attempts + 1)]

    @staticmethod
    def _timeout_text(value: str | bytes | None) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value or ""

    @staticmethod
    def _read_generated_result(path: Path) -> tuple[dict[str, Any] | None, ReplayError | None]:
        if not path.exists():
            return None, ReplayError(kind="missing_result", message="generated_result.json was not produced")
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return None, ReplayError(
                kind="invalid_result",
                message=f"cannot parse generated_result.json: {type(exc).__name__}: {exc}",
            )
        if not isinstance(content, dict) or not isinstance(content.get("passed"), bool):
            return None, ReplayError(
                kind="invalid_result",
                message="generated_result.json must be an object with a boolean passed field",
            )
        return content, None

    @staticmethod
    def _script_error(generated_result: dict[str, Any] | None) -> ReplayError:
        raw_error = generated_result.get("error") if generated_result else None
        if isinstance(raw_error, dict):
            message = str(raw_error.get("message") or raw_error.get("type") or "generated script reported failure")
            details = raw_error
        elif raw_error:
            message = str(raw_error)
            details = {"generated_error": raw_error}
        else:
            message = "generated script reported passed=false"
            details = {}
        return ReplayError(kind="script_error", message=message, details=details)

    @staticmethod
    def _write_evidence(attempt_dir: Path, command: CommandResult, environment: dict[str, str]) -> None:
        (attempt_dir / "stdout.log").write_text(command.stdout, encoding="utf-8")
        (attempt_dir / "stderr.log").write_text(command.stderr, encoding="utf-8")
        (attempt_dir / "command.json").write_text(command.model_dump_json(indent=2), encoding="utf-8")
        safe_environment = {
            key: environment[key]
            for key in ("HOME", "USERPROFILE", "PYTHONIOENCODING", "HARMONY_AGENT_REPORT_DIR")
            if key in environment
        }
        (attempt_dir / "environment.json").write_text(
            json.dumps(safe_environment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    @staticmethod
    def _relative(run_dir: Path, path: Path) -> str:
        return path.resolve().relative_to(run_dir).as_posix()

    def _result(
        self,
        *,
        attempt: int,
        command: CommandResult,
        run_dir: Path,
        attempt_dir: Path,
        status: str,
        error: ReplayError | None,
        generated_result: dict[str, Any] | None = None,
        generated_result_path: Path | None = None,
    ) -> ReplayResult:
        evidence = sorted(self._relative(run_dir, path) for path in attempt_dir.iterdir() if path.is_file())
        return ReplayResult(
            attempt=attempt,
            command=command,
            report_path=attempt_dir.resolve(),
            passed=status == "passed",
            status=status,
            exit_code=command.returncode,
            timed_out=command.timed_out,
            error=error,
            evidence_paths=evidence,
            generated_result_path=(
                self._relative(run_dir, generated_result_path) if generated_result_path is not None else None
            ),
            generated_result=generated_result,
        )
