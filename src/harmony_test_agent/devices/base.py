from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..models import CommandResult, ScreenSnapshot, TargetAppProfile


class DeviceError(RuntimeError):
    pass


class DeviceAdapter(ABC):
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def health_check(self) -> dict[str, object]: ...

    @abstractmethod
    def screenshot(self, output_dir: Path, run_id: str, label: str = "screen") -> ScreenSnapshot: ...

    @abstractmethod
    def collect_ui_hierarchy(self) -> dict: ...

    @abstractmethod
    def collect_logs(self, output_path: Path) -> CommandResult: ...

    @abstractmethod
    def open_app(self, profile: TargetAppProfile, reset: bool = False) -> CommandResult: ...

    @abstractmethod
    def click(self, x: int, y: int) -> CommandResult: ...

    @abstractmethod
    def input_text(self, text: str, x: int | None = None, y: int | None = None) -> CommandResult: ...

    @abstractmethod
    def swipe(self, start: tuple[int, int], end: tuple[int, int], duration: float = 0.5) -> CommandResult: ...

    @abstractmethod
    def back(self) -> CommandResult: ...

    @abstractmethod
    def wait(self, seconds: float) -> CommandResult: ...

    @abstractmethod
    def close(self) -> None: ...
