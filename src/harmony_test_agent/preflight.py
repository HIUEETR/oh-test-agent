"""汇总运行环境、模型、Hypium 与设备能力的启动前检查。"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from .config import Settings
from .devices import DeviceError, HarmonyDeviceAdapter
from .storage import ArtifactStore


class PreflightCheck(BaseModel):
    """描述一项预检的状态、必要性、文字详情与结构化证据。"""

    name: str
    status: str
    required: bool = True
    detail: str = ""
    data: dict = Field(default_factory=dict)


class PreflightReport(BaseModel):
    """汇总预检时间、整体状态以及各项检查结果。"""

    status: str
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    checks: list[PreflightCheck]


class PreflightService:
    """执行环境能力检查并把结果保存为预检证据。"""

    REQUIRED_MODULES = ("hypium", "pydantic_ai", "fastapi", "sqlmodel", "httpx", "PIL")

    def __init__(self, settings: Settings, artifacts: ArtifactStore | None = None):
        self.settings = settings
        self.artifacts = artifacts or ArtifactStore(settings.resolved_runtime_dir)

    def run(self, include_screenshot: bool = True) -> PreflightReport:
        """执行全部预检；可选截图步骤会连接设备并产生运行产物。"""
        checks = [
            PreflightCheck(
                name="python",
                status="pass" if sys.version_info >= (3, 14) else "fail",
                detail=sys.version,
            ),
            self._dependency_check(),
            self._model_check(),
            self._hypium_check(),
        ]
        device = None
        try:
            device = HarmonyDeviceAdapter(
                self.settings.harmony_device,
                self.settings.hdc_path,
                self.settings.agent_action_timeout,
            )
            device.connect()
            health = device.health_check()
            checks.append(
                PreflightCheck(
                    name="hdc_device",
                    status="pass" if health.get("connected") else "fail",
                    detail=f"device={self.settings.harmony_device}",
                    data=health,
                )
            )
            if include_screenshot:
                run_id = f"preflight-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
                snapshot = device.screenshot(self.artifacts.run_dir(run_id) / "screens", run_id, "preflight")
                checks.append(
                    PreflightCheck(
                        name="screenshot_transfer",
                        status="pass",
                        detail=str(snapshot.image_path),
                        data={
                            "width": snapshot.width,
                            "height": snapshot.height,
                            "sha256": snapshot.image_sha256,
                            "elements": len(snapshot.elements),
                        },
                    )
                )
        except DeviceError as exc:
            checks.append(PreflightCheck(name="hdc_device", status="fail", detail=str(exc)))
        finally:
            if device:
                device.close()
        report = PreflightReport(
            status="pass" if all(item.status == "pass" for item in checks if item.required) else "fail",
            checks=checks,
        )
        output = self.artifacts.runtime_dir.parent / "preflight" / "preflight-report.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        return report

    def _dependency_check(self) -> PreflightCheck:
        found = {name: bool(importlib.util.find_spec(name)) for name in self.REQUIRED_MODULES}
        missing = [name for name, available in found.items() if not available]
        return PreflightCheck(
            name="dependencies",
            status="fail" if missing else "pass",
            detail=f"missing: {', '.join(missing)}" if missing else "all required modules are importable",
            data=found,
        )

    def _model_check(self) -> PreflightCheck:
        configured = self.settings.model_configured
        mock_allowed = self.settings.agent_provider in {"auto", "mock"}
        return PreflightCheck(
            name="model",
            status="pass" if configured or mock_allowed else "fail",
            required=True,
            detail="OpenAI-compatible model configured" if configured else "using deterministic mock provider",
            data={
                "configured": configured,
                "vision_configured": self.settings.vision_model_configured,
                "provider": self.settings.agent_provider,
                "thinking_disabled": self.settings.agent_disable_thinking,
                "base_url": self.settings.openai_base_url,
                "api_key": "configured" if self.settings.openai_api_key else "not_configured",
            },
        )

    def _hypium_check(self) -> PreflightCheck:
        version = None
        try:
            version = importlib.metadata.version("hypium")
        except importlib.metadata.PackageNotFoundError:
            return PreflightCheck(name="hypium", status="fail", detail="hypium is not installed")
        self.settings.resolved_runtime_home.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["HOME"] = str(self.settings.resolved_runtime_home)
        env["USERPROFILE"] = str(self.settings.resolved_runtime_home)
        env["PYTHONIOENCODING"] = "utf-8"
        command = [sys.executable, "-c", "import hypium; print(hypium.__version__)"]
        try:
            process = subprocess.run(
                command,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            baseline = {
                "version": version,
                "import_command": subprocess.list2cmdline(command),
                "returncode": process.returncode,
                "stdout": process.stdout,
                "stderr": process.stderr,
                "runtime_home": str(self.settings.resolved_runtime_home),
                "driver_mode": {
                    "connect": "UiDriver.connect(device_sn=..., report_path=..., log_level='info')",
                    "close": "driver.close()",
                },
                "project_mode": {
                    "status": "not_validated",
                    "note": (
                        "Do not mix project mode and Driver mode; capture a real project template before enabling it."
                    ),
                },
            }
            baseline_path = self.artifacts.runtime_dir.parent / "preflight" / "hypium-baseline.json"
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return PreflightCheck(
                name="hypium",
                status="pass" if process.returncode == 0 else "fail",
                detail=f"version={version}, returncode={process.returncode}",
                data={"version": version, "baseline_path": str(baseline_path), "stderr": process.stderr[-2000:]},
            )
        except subprocess.TimeoutExpired:
            return PreflightCheck(name="hypium", status="fail", detail="isolated Hypium import timed out")
