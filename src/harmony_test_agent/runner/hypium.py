"""在隔离运行目录中执行已生成的 Hypium Driver 回放脚本。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ..models import CommandResult, GeneratedArtifact, ReplayResult


class HypiumRunner:
    """管理回放环境、超时、标准输出和每次尝试的证据目录。"""

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
        """执行一次回放；退出码和生成结果均成功时才判定通过。"""
        run_dir = generated.python_path.parent.parent
        attempt_dir = run_dir / "hypium" / f"attempt-{attempt:02d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        for stale_name in ("generated_result.json", "final.jpeg", "failure.jpeg"):
            (attempt_dir / stale_name).unlink(missing_ok=True)
        command = [sys.executable, str(generated.python_path)]
        environment = self.environment(attempt_dir)
        started = time.monotonic()
        try:
            process = subprocess.run(
                command,
                cwd=str(generated.python_path.parent),
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )
            result = CommandResult(
                command=subprocess.list2cmdline(command),
                args=command,
                returncode=process.returncode,
                stdout=process.stdout,
                stderr=process.stderr,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
        except subprocess.TimeoutExpired as exc:
            result = CommandResult(
                command=subprocess.list2cmdline(command),
                args=command,
                returncode=None,
                stdout=exc.stdout if isinstance(exc.stdout, str) else "",
                stderr=exc.stderr if isinstance(exc.stderr, str) else "",
                timed_out=True,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
        (attempt_dir / "stdout.log").write_text(result.stdout, encoding="utf-8")
        (attempt_dir / "stderr.log").write_text(result.stderr, encoding="utf-8")
        (attempt_dir / "command.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
        safe_environment = {
            key: environment[key] for key in ("HOME", "USERPROFILE", "PYTHONIOENCODING", "HARMONY_AGENT_REPORT_DIR")
        }
        (attempt_dir / "environment.json").write_text(
            json.dumps(safe_environment, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        result_path = attempt_dir / "generated_result.json"
        semantic_pass = False
        if result_path.exists():
            try:
                semantic_pass = bool(json.loads(result_path.read_text(encoding="utf-8")).get("passed"))
            except json.JSONDecodeError, OSError:
                semantic_pass = False
        return ReplayResult(
            attempt=attempt,
            command=result,
            report_path=attempt_dir.resolve(),
            passed=result.ok and semantic_pass,
        )

    def execute_repeated(self, generated: GeneratedArtifact, attempts: int = 3) -> list[ReplayResult]:
        """按独立尝试目录重复执行回放，并按执行顺序返回全部结果。"""
        return [self.execute(generated, attempt=index) for index in range(1, attempts + 1)]
