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
from collections.abc import Callable
from pathlib import Path

from PIL import Image

from ..devices.base import DeviceError, reject_ui_input_usage, ui_input_key_argument
from ..models import CommandResult

# Windows 下用独立进程组启动子进程，便于超时时按进程树终止
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


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
        与旧实现的关键差别：使用 ``Popen`` + ``communicate(timeout)``，超时后
        **主动终止进程（Windows 下含子进程树）**，避免「等待被取消但底层 HDC
        仍在跑」的静默副作用。
        """
        command = [self.hdc_path]
        if device:
            command.extend(["-t", self.device_id])
        command.extend(args)
        started = time.monotonic()
        effective_timeout = timeout or self.timeout
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
        except OSError as exc:
            return CommandResult(
                command=subprocess.list2cmdline(command),
                args=command,
                returncode=None,
                stderr=str(exc),
                duration_ms=round((time.monotonic() - started) * 1000),
            )

        try:
            stdout, stderr = process.communicate(timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            terminated = self._terminate(process)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except Exception:  # noqa: BLE001 - 收尾读取失败不影响超时结论
                stdout, stderr = "", ""
            note = "" if terminated else "; process tree may still be running"
            return CommandResult(
                command=subprocess.list2cmdline(command),
                args=command,
                returncode=None,
                stdout=stdout or "",
                stderr=f"{stderr or ''}timeout after {effective_timeout}s{note}".strip(),
                timed_out=True,
                duration_ms=round((time.monotonic() - started) * 1000),
            )

        return CommandResult(
            command=subprocess.list2cmdline(command),
            args=command,
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=round((time.monotonic() - started) * 1000),
        )

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> bool:
        """终止进程（Windows 下连同子进程树）；返回是否成功确认退出。"""
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
            except OSError, subprocess.SubprocessError:
                pass
        else:  # pragma: no cover - 非 Windows 分支仅作兼容
            import signal

            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except OSError, ProcessLookupError:
                pass
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
            return process.poll() is not None
        except subprocess.TimeoutExpired:
            return False

    # ------------------------------------------------------------------
    # L1 补充：key_event（泛化 HarmonyDeviceAdapter.back）
    # ------------------------------------------------------------------

    def key_event(self, name: str) -> CommandResult:
        """发送系统按键事件（Back / Home / Power 走名字，其余走数字 keyID）。

        ``uitest uiInput keyEvent`` 只接受 ``Back``/``Home``/``Power`` 三个名字，其余按键必须
        传数字 keyID；参数非法时它打印 usage **但退出码仍是 0**，只靠返回码判定就会把
        「按了 Enter」录制成成功（真机复盘 dc-20260923T180535Z-1bed642e，详见
        ``devices/base.py::ui_input_key_argument``）。因此这里同时做名字翻译与拒绝识别。
        """
        return reject_ui_input_usage(self._run("shell", "uitest", "uiInput", "keyEvent", ui_input_key_argument(name)))

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

    def screenshot_jpeg(
        self,
        output_dir: Path,
        label: str = "screen",
        on_phase: Callable[[str], None] | None = None,
    ) -> tuple[Path, bytes, int, int]:
        """采集设备截图并直接返回 JPEG 字节，不经过 PNG 转换。

        Returns:
            (local_jpeg_path, jpeg_bytes, width, height)

        相比 ``HarmonyDeviceAdapter.screenshot``（5 次子进程 + PNG 转换），
        此方法仅 3 次子进程（snapshot_display → file recv → rm），
        节省 PIL PNG 编码时间和 3-8× 传输带宽。

        ``on_phase`` 会在每个子步骤前回调（``snapshot_display`` / ``file_recv`` /
        ``decode`` / ``remote_cleanup`` / ``retry_wait``），使 8-16 秒的慢截图
        能定位到具体步骤，而不是一段静默。
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        snapshot_id = f"dc-{uuid.uuid4().hex[:12]}"
        remote_path = f"/data/local/tmp/{snapshot_id}.jpeg"
        local_path = output_dir / f"{label}_{snapshot_id}.jpeg"

        def phase(name: str) -> None:
            if on_phase is not None:
                on_phase(name)

        errors: list[str] = []
        for attempt in range(3):
            local_path.unlink(missing_ok=True)
            phase("snapshot_display")
            capture = self._run("shell", "snapshot_display", "-i", "0", "-f", remote_path)
            if not capture.ok or "success:" not in capture.stdout.casefold():
                errors.append(f"capture[{attempt + 1}]: {capture.stderr or capture.stdout}")
                phase("retry_wait")
                time.sleep(0.5)
                continue
            phase("file_recv")
            received = self._run("file", "recv", remote_path, str(local_path))
            if not received.ok or not local_path.exists() or local_path.stat().st_size == 0:
                errors.append(f"recv[{attempt + 1}]: {received.stderr or received.stdout}")
                phase("retry_wait")
                time.sleep(0.5)
                continue
            try:
                phase("decode")
                with Image.open(local_path) as image:
                    image.load()
                    if image.width <= 0 or image.height <= 0:
                        raise ValueError(f"invalid image size: {image.size}")
                    width, height = image.size
                jpeg_bytes = local_path.read_bytes()
                phase("remote_cleanup")
                self._run("shell", "rm", "-f", remote_path)
                return local_path, jpeg_bytes, width, height
            except Exception as exc:
                errors.append(f"decode[{attempt + 1}]: {exc}")
                phase("retry_wait")
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
