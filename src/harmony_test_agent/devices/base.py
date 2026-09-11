"""定义设备操作的抽象协议及统一设备异常。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import CommandResult, ScreenSnapshot, TargetAppProfile

if TYPE_CHECKING:
    from ..targets.catalog import ForegroundApp, InstalledApp


class DeviceError(RuntimeError):
    """表示设备连接、命令执行或产物采集失败。"""

    pass


class DeviceAdapter(ABC):
    """编排器依赖的设备能力边界，具体传输协议由实现类负责。"""

    @abstractmethod
    def connect(self) -> None:
        """建立设备连接，无法使用目标设备时抛出 ``DeviceError``。"""
        ...

    @abstractmethod
    def health_check(self) -> dict[str, object]:
        """返回可序列化的设备连接与运行状态。"""
        ...

    @abstractmethod
    def screenshot(self, output_dir: Path, run_id: str, label: str = "screen") -> ScreenSnapshot:
        """采集截图及其 UI 层级，并保存到指定运行目录。"""
        ...

    @abstractmethod
    def collect_ui_hierarchy(self) -> dict:
        """返回设备当前页面的原始 UI 层级。"""
        ...

    @abstractmethod
    def collect_logs(self, output_path: Path) -> CommandResult:
        """采集设备日志并写入指定路径。"""
        ...

    @abstractmethod
    def open_app(self, profile: TargetAppProfile, reset: bool = False) -> CommandResult:
        """按应用档案启动目标 Ability，可选执行档案允许的重置流程。"""
        ...

    def list_installed_apps(self) -> list[InstalledApp]:
        """列出设备中可解析的应用；旧适配器默认返回空目录。"""
        return []

    def inspect_app(self, bundle_name: str) -> InstalledApp:
        """读取一个应用的确定性元数据；不支持目录能力的适配器明确失败。"""
        raise DeviceError(f"device adapter cannot inspect application: {bundle_name}")

    def find_installed_apps(self, label: str) -> list[InstalledApp]:
        """按应用显示名筛选目录，默认基于完整目录进行规范化匹配。"""
        needle = " ".join(label.split()).casefold()
        return [app for app in self.list_installed_apps() if " ".join(app.display_name.split()).casefold() == needle]

    def current_foreground_app(self) -> ForegroundApp | None:
        """返回当前前台应用；旧适配器可返回未知。"""
        return None

    def start_app(
        self,
        bundle_name: str,
        ability_name: str,
        module_name: str | None = None,
    ) -> CommandResult:
        """通过已解析的应用身份启动 Ability。"""
        raise DeviceError(f"device adapter cannot start application: {bundle_name}/{ability_name}")

    def stop_app(self, bundle_name: str) -> CommandResult:
        """强制停止目标应用，但不清除应用数据。"""
        raise DeviceError(f"device adapter cannot stop application: {bundle_name}")
    @abstractmethod
    def click(self, x: int, y: int) -> CommandResult:
        """点击屏幕绝对像素坐标。"""
        ...

    @abstractmethod
    def input_text(self, text: str, x: int | None = None, y: int | None = None) -> CommandResult:
        """向当前输入控件写入文本，可选地先点击指定坐标。"""
        ...

    @abstractmethod
    def swipe(self, start: tuple[int, int], end: tuple[int, int], duration: float = 0.5) -> CommandResult:
        """按给定起点、终点和持续时间滑动。"""
        ...

    @abstractmethod
    def back(self) -> CommandResult:
        """触发一次系统返回操作。"""
        ...

    @abstractmethod
    def wait(self, seconds: float) -> CommandResult:
        """等待指定秒数，并返回统一动作结果。"""
        ...

    @abstractmethod
    def close(self) -> None:
        """释放适配器持有的设备资源。"""
        ...
