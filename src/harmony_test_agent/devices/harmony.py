"""通过 HDC 连接 OpenHarmony 设备并执行受限 UI 操作。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from PIL import Image

from ..models import CommandResult, ScreenSnapshot, TargetAppProfile
from ..perception.normalizer import normalize_layout, page_path
from .base import DeviceAdapter, DeviceError


class HarmonyDeviceAdapter(DeviceAdapter):
    """封装指定设备序列号上的 HDC 命令，并将结果统一为领域模型。"""

    def __init__(self, device_id: str, hdc_path: str | None = None, timeout: float = 30):
        self.device_id = device_id
        self.hdc_path = self._find_hdc(hdc_path)
        self.timeout = timeout
        self.connected = False

    @staticmethod
    def _find_hdc(configured: str | None) -> str:
        candidates = [
            configured,
            os.environ.get("HDC_PATH"),
            shutil.which("hdc"),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return str(Path(candidate))
        raise DeviceError("hdc executable was not found; set HDC_PATH")

    def _run(self, *args: str, timeout: float | None = None, device: bool = True) -> CommandResult:
        command = [self.hdc_path]
        if device:
            command.extend(["-t", self.device_id])
        command.extend(args)
        started = time.monotonic()
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self.timeout,
                check=False,
            )
            return CommandResult(
                command=subprocess.list2cmdline(command),
                args=command,
                returncode=process.returncode,
                stdout=process.stdout,
                stderr=process.stderr,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                command=subprocess.list2cmdline(command),
                args=command,
                returncode=None,
                stdout=exc.stdout if isinstance(exc.stdout, str) else "",
                stderr=exc.stderr if isinstance(exc.stderr, str) else "",
                timed_out=True,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
        except OSError as exc:
            return CommandResult(
                command=subprocess.list2cmdline(command),
                args=command,
                returncode=None,
                stderr=str(exc),
                duration_ms=round((time.monotonic() - started) * 1000),
            )

    def connect(self) -> None:
        """建立到目标设备的 HDC 连接，连接失败时抛出设备异常。"""
        result = self._run("list", "targets", device=False)
        if not result.ok or self.device_id not in result.stdout:
            raise DeviceError(f"device {self.device_id} is not connected: {result.stderr or result.stdout}")
        self.connected = True

    def health_check(self) -> dict[str, object]:
        """读取设备列表和基础属性，返回可序列化的连接健康信息。"""
        listed = self._run("list", "targets", device=False)
        resolution = self._run("shell", "hidumper", "-s", "RenderService", "-a", "screen")
        match = re.search(r"render resolution=(\d+)x(\d+)", resolution.stdout)
        return {
            "connected": listed.ok and self.device_id in listed.stdout,
            "id": self.device_id,
            "hdc_path": self.hdc_path,
            "resolution": [int(match.group(1)), int(match.group(2))] if match else None,
            "detail": listed.stdout.strip(),
        }

    def collect_ui_hierarchy(self) -> dict:
        """导出并解析当前页面的 UI 层级数据。"""
        dump = self._run("shell", "uitest", "dumpLayout", "-a")
        output = f"{dump.stdout}\n{dump.stderr}".strip()
        match = re.search(r"DumpLayout saved to:\s*(\S+)", output)
        if not dump.ok or not match:
            raise DeviceError(f"dumpLayout failed: {output[-1000:]}")
        cat = self._run("shell", "cat", match.group(1))
        if not cat.ok or not cat.stdout.strip():
            raise DeviceError(f"failed to read layout: {cat.stderr or cat.stdout}")
        try:
            return json.loads(cat.stdout)
        except json.JSONDecodeError as exc:
            raise DeviceError(f"invalid layout JSON: {exc}") from exc

    def screenshot(self, output_dir: Path, run_id: str, label: str = "screen") -> ScreenSnapshot:
        """采集截图与 UI 层级，生成带稳定摘要的屏幕快照。"""
        output_dir.mkdir(parents=True, exist_ok=True)
        snapshot_id = f"snap-{uuid.uuid4().hex[:12]}"
        remote_path = f"/data/local/tmp/{run_id}_{snapshot_id}.jpeg"
        local_path = output_dir / f"{label}_{snapshot_id}.png"
        width, height = self._capture_png(remote_path, local_path)
        hierarchy = self.collect_ui_hierarchy()
        hierarchy_path = output_dir.parent / "layouts" / f"{label}_{snapshot_id}.json"
        hierarchy_path.parent.mkdir(parents=True, exist_ok=True)
        hierarchy_path.write_text(json.dumps(hierarchy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return ScreenSnapshot(
            snapshot_id=snapshot_id,
            run_id=run_id,
            image_path=local_path.resolve(),
            image_sha256=hashlib.sha256(local_path.read_bytes()).hexdigest(),
            width=width,
            height=height,
            page_path=page_path(hierarchy),
            hierarchy_path=hierarchy_path.resolve(),
            elements=normalize_layout(hierarchy, width, height),
        )

    def _capture_png(self, remote_path: str, local_path: Path) -> tuple[int, int]:
        errors: list[str] = []
        received_path = local_path.with_suffix(".device.jpeg")
        for attempt in range(3):
            local_path.unlink(missing_ok=True)
            received_path.unlink(missing_ok=True)
            capture = self._run("shell", "snapshot_display", "-i", "0", "-f", remote_path)
            if not capture.ok or "success:" not in capture.stdout.casefold():
                errors.append(f"capture[{attempt + 1}]: {capture.stderr or capture.stdout}")
                time.sleep(0.5)
                continue
            received = self._run("file", "recv", remote_path, str(received_path))
            if not received.ok or not received_path.exists() or received_path.stat().st_size == 0:
                errors.append(f"recv[{attempt + 1}]: {received.stderr or received.stdout}")
                time.sleep(0.5)
                continue
            try:
                with Image.open(received_path) as image:
                    image.load()
                    if image.width <= 0 or image.height <= 0:
                        raise ValueError(f"invalid image size: {image.size}")
                    size = image.size
                    image.convert("RGB").save(local_path, format="PNG")
                with Image.open(local_path) as png:
                    png.verify()
                received_path.unlink(missing_ok=True)
                self._run("shell", "rm", "-f", remote_path)
                return size
            except Exception as exc:
                errors.append(f"decode[{attempt + 1}]: {exc}")
                time.sleep(0.5)
        received_path.unlink(missing_ok=True)
        detail = "; ".join(errors)[-3000:]
        raise DeviceError(f"failed to capture a valid screenshot after 3 attempts: {detail}")

    def collect_logs(self, output_path: Path) -> CommandResult:
        """将目标设备的日志采集结果写入指定证据文件。"""
        result = self._run("shell", "hilog", "-x", timeout=10)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result.stdout[-200_000:] + result.stderr[-20_000:], encoding="utf-8")
        return result

    def open_app(self, profile: TargetAppProfile, reset: bool = False) -> CommandResult:
        """按目标应用配置启动 Ability，并在要求时先执行受支持的重置策略。"""
        if reset:
            stopped = self._run("shell", "aa", "force-stop", profile.bundle_name)
            if not stopped.ok:
                return stopped
            time.sleep(1)
        return self._run("shell", "aa", "start", "-b", profile.bundle_name, "-a", profile.main_ability)

    def click(self, x: int, y: int) -> CommandResult:
        """在设备屏幕的绝对像素坐标执行一次点击。"""
        return self._run("shell", "uitest", "uiInput", "click", str(x), str(y))

    def input_text(self, text: str, x: int | None = None, y: int | None = None) -> CommandResult:
        """可选地先聚焦坐标，再向当前输入控件写入文本。"""
        args = ["shell", "uitest", "uiInput", "inputText"]
        if x is not None and y is not None:
            args.extend([str(x), str(y)])
        args.append(text)
        return self._run(*args)

    def swipe(self, start: tuple[int, int], end: tuple[int, int], duration: float = 0.5) -> CommandResult:
        """按起止坐标和持续时间执行一次滑动。"""
        velocity = max(200, min(40_000, int(15_000 - duration * 7_000)))
        return self._run(
            "shell",
            "uitest",
            "uiInput",
            "swipe",
            str(start[0]),
            str(start[1]),
            str(end[0]),
            str(end[1]),
            str(velocity),
        )

    def back(self) -> CommandResult:
        """发送系统返回键事件。"""
        return self._run("shell", "uitest", "uiInput", "keyEvent", "Back")

    def wait(self, seconds: float) -> CommandResult:
        """等待给定秒数，并返回与其他设备动作一致的命令结果。"""
        started = time.monotonic()
        time.sleep(max(0, min(seconds, 30)))
        return CommandResult(
            command=f"wait {seconds}",
            args=["wait", str(seconds)],
            returncode=0,
            duration_ms=round((time.monotonic() - started) * 1000),
        )

    def close(self) -> None:
        """结束适配器生命周期；当前 HDC 调用不持有常驻连接。"""
        self.connected = False
