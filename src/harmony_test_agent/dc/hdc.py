"""DC 模式专属 HDC 命令执行器。

补充 ``HarmonyDeviceAdapter`` 未覆盖的 HDC 调用（install/uninstall/clear_data/
file_send/recv/list/execute_shell/memory_dump/key_event/screenshot_jpeg）。

**不修改** ``devices/harmony.py`` 或 ``devices/base.py``；内部 ``_run`` 方法
镜像 ``HarmonyDeviceAdapter._run`` 的 subprocess 模式但独立实现，不引用私有方法。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from PIL import Image

from ..devices.base import DeviceError
from ..models import CommandResult


class DcHdcExecutor:
    """封装 DC 模式所需的额外 HDC 命令。

    所有方法返回 ``CommandResult``（与 ``HarmonyDeviceAdapter`` 一致），
    调用方负责在 ``asyncio.to_thread`` 中执行以避免阻塞事件循环。
    """

    def __init__(self, device_id: str, hdc_path: str | None = None, timeout: float = 30):
        self.device_id = device_id
        self.hdc_path = self._find_hdc(hdc_path)
        self.timeout = timeout

    # ------------------------------------------------------------------
    # HDC 路径解析（镜像 HarmonyDeviceAdapter._find_hdc 逻辑）
    # ------------------------------------------------------------------

    @staticmethod
    def _find_hdc(configured: str | None) -> str:
        candidates = [configured, os.environ.get("HDC_PATH"), shutil.which("hdc")]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return str(Path(candidate))
        raise DeviceError("hdc executable was not found; set HDC_PATH")

    # ------------------------------------------------------------------
    # 底层 subprocess 执行
    # ------------------------------------------------------------------

    def _run(self, *args: str, timeout: float | None = None, device: bool = True) -> CommandResult:
        """执行一条 HDC 命令并返回标准化结果。

        与 ``HarmonyDeviceAdapter._run`` 行为一致但独立实现，避免依赖私有方法。
        """
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

    # ------------------------------------------------------------------
    # L1 补充：key_event（泛化 HarmonyDeviceAdapter.back）
    # ------------------------------------------------------------------

    def key_event(self, name: str) -> CommandResult:
        """发送系统按键事件（Home / Back / Power 等）。"""
        return self._run("shell", "uitest", "uiInput", "keyEvent", name)

    # ------------------------------------------------------------------
    # L3：应用管理
    # ------------------------------------------------------------------

    def install_app(self, hap_path: Path) -> CommandResult:
        """安装 HAP/APP 包到设备。"""
        return self._run("install", "-r", str(hap_path), timeout=120)

    def uninstall_app(self, bundle_name: str) -> CommandResult:
        """卸载指定应用。"""
        return self._run("uninstall", bundle_name)

    def clear_app_data(self, bundle_name: str) -> CommandResult:
        """清除指定应用的数据。"""
        return self._run("shell", "bm", "clean", "-n", bundle_name, "-d")

    # ------------------------------------------------------------------
    # L4：文件操作
    # ------------------------------------------------------------------

    def file_send(self, local: Path, remote: str) -> CommandResult:
        """推送本地文件到设备。"""
        return self._run("file", "send", str(local), remote, timeout=120)

    def file_recv(self, remote: str, local: Path) -> CommandResult:
        """从设备拉取文件到本地。"""
        local.parent.mkdir(parents=True, exist_ok=True)
        return self._run("file", "recv", remote, str(local), timeout=120)

    def file_list(self, remote_dir: str) -> CommandResult:
        """列出设备目录内容。"""
        return self._run("shell", "ls", "-l", remote_dir)

    # ------------------------------------------------------------------
    # L5：受控 Shell
    # ------------------------------------------------------------------

    def execute_shell(self, argv: list[str]) -> CommandResult:
        """执行受控 shell 命令。

        ``argv`` 必须是列表形式（非字符串拼接），由 ``DcShellPolicy.validate_shell``
        在调用前完成黑名单/白名单/元字符校验。
        """
        return self._run("shell", *argv)

    # ------------------------------------------------------------------
    # L2 补充：应用目录 / memory_dump
    # ------------------------------------------------------------------

    def list_bundle_names(self, timeout: float = 20) -> CommandResult:
        """列出设备已安装的 bundle 名（仅一次 ``bm dump -a``）。

        对比 ``HarmonyDeviceAdapter.list_installed_apps``：后者对每个 bundle 追加
        一次 ``bm dump -n``，实测 66 个包约 86s，必然撞上 DC 工具 30s 超时。
        这里只做单次调用（实测 ≈0.7s），需要的元数据由 ``inspect_app`` 按需获取。
        """
        return self._run("shell", "bm", "dump", "-a", timeout=timeout)

    def memory_dump(self, bundle_name: str, output_path: Path) -> CommandResult:
        """采集指定应用的内存转储并写入本地文件。"""
        result = self._run("shell", "hidumper", "--mem", bundle_name, timeout=30)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result.stdout + result.stderr, encoding="utf-8")
        return result

    # ------------------------------------------------------------------
    # JPEG 快速路径（不转 PNG，用于 LLM 上传和 SSE 推送）
    # ------------------------------------------------------------------

    def screenshot_jpeg(self, output_dir: Path, label: str = "screen") -> tuple[Path, bytes, int, int]:
        """采集设备截图并直接返回 JPEG 字节，不经过 PNG 转换。

        Returns:
            (local_jpeg_path, jpeg_bytes, width, height)

        相比 ``HarmonyDeviceAdapter.screenshot``（5 次子进程 + PNG 转换），
        此方法仅 3 次子进程（snapshot_display → file recv → rm），
        节省 PIL PNG 编码时间和 3-8× 传输带宽。
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        snapshot_id = f"dc-{uuid.uuid4().hex[:12]}"
        remote_path = f"/data/local/tmp/{snapshot_id}.jpeg"
        local_path = output_dir / f"{label}_{snapshot_id}.jpeg"

        errors: list[str] = []
        for attempt in range(3):
            local_path.unlink(missing_ok=True)
            capture = self._run("shell", "snapshot_display", "-i", "0", "-f", remote_path)
            if not capture.ok or "success:" not in capture.stdout.casefold():
                errors.append(f"capture[{attempt + 1}]: {capture.stderr or capture.stdout}")
                time.sleep(0.5)
                continue
            received = self._run("file", "recv", remote_path, str(local_path))
            if not received.ok or not local_path.exists() or local_path.stat().st_size == 0:
                errors.append(f"recv[{attempt + 1}]: {received.stderr or received.stdout}")
                time.sleep(0.5)
                continue
            try:
                with Image.open(local_path) as image:
                    image.load()
                    if image.width <= 0 or image.height <= 0:
                        raise ValueError(f"invalid image size: {image.size}")
                    width, height = image.size
                jpeg_bytes = local_path.read_bytes()
                self._run("shell", "rm", "-f", remote_path)
                return local_path, jpeg_bytes, width, height
            except Exception as exc:
                errors.append(f"decode[{attempt + 1}]: {exc}")
                time.sleep(0.5)

        local_path.unlink(missing_ok=True)
        detail = "; ".join(errors)[-3000:]
        raise DeviceError(f"failed to capture JPEG screenshot after 3 attempts: {detail}")

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def sha256_bytes(data: bytes) -> str:
        """计算字节的 SHA-256 摘要，用于截图去重。"""
        return hashlib.sha256(data).hexdigest()
