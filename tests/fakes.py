"""共享测试替身：确定性假设备与可编程 provider。

放在 ``tests/`` 根目录（而非某个子目录）以便 unit / integration 共用：
``tests/conftest.py`` 已把该目录加入 ``sys.path``。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from PIL import Image

from harmony_test_agent.agents.providers import MockAgentProvider
from harmony_test_agent.devices import DeviceAdapter
from harmony_test_agent.models import (
    BoundingBox,
    CommandResult,
    PlannedStep,
    PlanResult,
    ScreenSnapshot,
    TargetAppProfile,
    ToolDecision,
    UIElement,
)
from harmony_test_agent.perception.normalizer import normalize_layout, page_path
from harmony_test_agent.targets import ForegroundApp, InstalledApp

BUNDLE_NAME = "com.example.calendar"
MAIN_ABILITY = "EntryAbility"
CALENDAR_BUNDLE = "com.huawei.hmos.calendar"
CALENDAR_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "calendar"

_COLORS = {
    "launcher": "black",
    "home": "navy",
    "month": "teal",
    "editor": "green",
    "picker": "orange",
    "detail": "purple",
}


def snapshot_from_layout(path: Path, *, run_id: str = "fixture") -> ScreenSnapshot:
    """把设备 ``dumpLayout`` 原始 JSON 还原为 ``ScreenSnapshot``（真实 fixture 回归用）。

    宽高从根节点 bounds 推导；页面路径走与设备适配器完全相同的 ``page_path``。
    """
    layout = json.loads(path.read_text(encoding="utf-8"))
    attrs = layout.get("attributes") or {}
    match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", str(attrs.get("bounds", "")))
    width = int(match.group(3)) - int(match.group(1)) if match else 1222
    height = int(match.group(4)) - int(match.group(2)) if match else 2670
    return ScreenSnapshot(
        snapshot_id=path.stem,
        run_id=run_id,
        image_path=path.with_suffix(".png"),
        image_sha256=f"fixture-{path.stem}",
        width=width,
        height=height,
        page_path=page_path(layout),
        elements=normalize_layout(layout, width, height),
    )


def calendar_foreground() -> ForegroundApp:
    return ForegroundApp(bundle_name=CALENDAR_BUNDLE, ability_name="EntryAbility", window_type="main")


def element(
    element_id: str,
    *,
    content: str = "",
    key: str = "",
    type_name: str = "Button",
    bbox: tuple[int, int, int, int] | None = None,
    clickable: bool = False,
    editable: bool = False,
    scrollable: bool = False,
    selected: bool = False,
) -> UIElement:
    """构造一个最小 UI 元素，避免每个测试重复手写 BoundingBox。"""
    box = BoundingBox(left=bbox[0], top=bbox[1], right=bbox[2], bottom=bbox[3]) if bbox else None
    return UIElement(
        element_id=element_id,
        content=content or key or element_id,
        key=key,
        type=type_name,
        bbox=box,
        clickable=clickable,
        editable=editable,
        scrollable=scrollable,
        selected=selected,
    )


class FakeLiveDevice(DeviceAdapter):
    """确定性假设备：launcher → home → month/editor → home，覆盖 Live 任务期全链路。

    与 ``test_orchestrator_mock.FakeDevice`` 同族，额外提供前台查询与 stop/start，
    使 Profile 引导、验证与实时降级三条路径都能在同一个替身上跑通。
    """

    device_id = "fake-device"
    display_name = "日历"

    def __init__(self, bundle_name: str = BUNDLE_NAME, main_ability: str = MAIN_ABILITY) -> None:
        self.bundle_name = bundle_name
        self.main_ability = main_ability
        self.state = "launcher"
        self.connected = False
        self.clicks: list[tuple[int, int]] = []
        self.swipes: list[tuple[tuple[int, int], tuple[int, int]]] = []
        self.inputs: list[str] = []
        self.foreground: ForegroundApp | None = None

    # -- 连接与采集 --------------------------------------------------
    def connect(self) -> None:
        self.connected = True

    def health_check(self) -> dict[str, object]:
        return {"connected": self.connected, "id": self.device_id, "resolution": [800, 1200]}

    def screenshot(self, output_dir: Path, run_id: str, label: str = "screen") -> ScreenSnapshot:
        output_dir.mkdir(parents=True, exist_ok=True)
        image_path = output_dir / f"{label}_{self.state}.png"
        Image.new("RGB", (800, 1200), _COLORS.get(self.state, "gray")).save(image_path)
        return ScreenSnapshot(
            snapshot_id=f"{label}-{self.state}",
            run_id=run_id,
            image_path=image_path.resolve(),
            image_sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
            width=800,
            height=1200,
            page_title=f"日历 {self.state}",
            page_path=self.page_path,
            elements=self.elements(),
        )

    def collect_ui_hierarchy(self) -> dict:
        return {}

    # -- 应用目录（供 TargetResolver 解析） --------------------------
    def list_installed_apps(self) -> list[InstalledApp]:
        return [
            InstalledApp(
                bundle_name=self.bundle_name,
                display_name=self.display_name,
                abilities=(self.main_ability,),
                main_ability=self.main_ability,
                version_name="1.0.0",
                version_code=100,
            )
        ]

    def inspect_app(self, bundle_name: str) -> InstalledApp:
        from harmony_test_agent.devices import DeviceError

        for app in self.list_installed_apps():
            if app.bundle_name == bundle_name:
                return app
        raise DeviceError(f"device adapter cannot inspect application: {bundle_name}")

    def collect_logs(self, output_path: Path) -> CommandResult:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("fake logs", encoding="utf-8")
        return self._ok("logs")

    # -- 应用生命周期 ------------------------------------------------
    def open_app(self, profile: TargetAppProfile | Any, reset: bool = False) -> CommandResult:
        self._enter("home")
        return self._ok("open_app")

    def start_app(self, bundle_name: str, ability_name: str, module_name: str | None = None) -> CommandResult:
        if bundle_name != self.bundle_name:
            return CommandResult(command="start_app", args=[bundle_name], returncode=1, stderr="bundle mismatch")
        self._enter("home")
        return self._ok("start_app")

    def stop_app(self, bundle_name: str) -> CommandResult:
        self.state = "launcher"
        self.foreground = None
        return self._ok("stop_app")

    def current_foreground_app(self) -> ForegroundApp | None:
        return self.foreground

    # -- 交互 --------------------------------------------------------
    def click(self, x: int, y: int) -> CommandResult:
        self.clicks.append((x, y))
        if self.state == "launcher":
            return self._ok("click")
        self._enter("month" if x < 400 else "editor")
        return self._ok("click")

    def input_text(self, text: str, x: int | None = None, y: int | None = None) -> CommandResult:
        self.inputs.append(text)
        self._enter("editor")
        return self._ok("input_text")

    def swipe(self, start: tuple[int, int], end: tuple[int, int], duration: float = 0.5) -> CommandResult:
        self.swipes.append((start, end))
        return self._ok("swipe")

    def back(self) -> CommandResult:
        self._enter("launcher" if self.state == "home" else "home")
        return self._ok("back")

    def wait(self, seconds: float) -> CommandResult:
        return self._ok("wait")

    def close(self) -> None:
        self.connected = False

    # -- 页面内容 ----------------------------------------------------
    @property
    def page_path(self) -> str:
        if self.state == "launcher":
            return "pages/Launcher"
        if self.state == "detail":
            return "pages/DetailPage"
        return "pages/EntryPage"

    def elements(self) -> list[UIElement]:
        if self.state == "launcher":
            return []
        if self.state == "home":
            return [
                element("tabs", key="tabs_month", content="月", bbox=(300, 1100, 500, 1180), clickable=True),
                element("add", key="phone_add_agenda", content="新建日程", bbox=(600, 1100, 780, 1180), clickable=True),
                element(
                    "clock",
                    key="TimeView_Text_timeText",
                    content="09:41",
                    type_name="Text",
                    bbox=(600, 20, 780, 60),
                ),
                element(
                    "agenda",
                    key="normal_agenda_list_item193",
                    content="团队周会",
                    bbox=(40, 300, 760, 380),
                    clickable=True,
                ),
            ]
        if self.state == "month":
            return [
                element(
                    "tabs",
                    key="tabs_month",
                    content="月",
                    bbox=(300, 1100, 500, 1180),
                    clickable=True,
                    selected=True,
                ),
                element("day22", key="22___十二_", content="22", bbox=(600, 300, 700, 380), clickable=True),
                element("grid", key="month_view", content="月视图", bbox=(20, 200, 780, 1000), scrollable=True),
                element("add", key="phone_add_agenda", content="新建日程", bbox=(600, 1100, 780, 1180), clickable=True),
            ]
        if self.state == "editor":
            return [
                element(
                    "title",
                    key="add_agenda_title-1789951623657",
                    content="",
                    type_name="TextInput",
                    bbox=(40, 300, 760, 380),
                    editable=True,
                ),
                element(
                    "remind",
                    key="add_agenda_add_remind",
                    content="添加提醒",
                    bbox=(40, 500, 760, 580),
                    clickable=True,
                ),
                element("save", key="add_agenda_save", content="保存", bbox=(600, 1100, 780, 1180), clickable=True),
            ]
        if self.state == "picker":
            return [
                element("hour1", key="picker_hour_1", content="1", bbox=(300, 700, 400, 800), clickable=True),
                element("hour2", key="picker_hour_2", content="2", bbox=(300, 810, 400, 910), clickable=True),
                element("minute0", key="picker_minute_0", content="00", bbox=(500, 700, 600, 800), clickable=True),
                element("minute1", key="picker_minute_1", content="01", bbox=(500, 810, 600, 910), clickable=True),
                element("ampm", key="picker_ampm", content="下午", bbox=(700, 700, 780, 910), clickable=True),
            ]
        if self.state == "detail":
            return [
                element(
                    "content",
                    key="add_agenda_detail_page",
                    content="9月22日 下午01:00",
                    type_name="Column",
                    bbox=(10, 80, 790, 1100),
                )
            ]
        return []

    def _enter(self, state: str) -> None:
        self.state = state
        self.foreground = (
            None
            if state == "launcher"
            else ForegroundApp(bundle_name=self.bundle_name, ability_name=self.main_ability, window_type="main")
        )

    @staticmethod
    def _ok(name: str) -> CommandResult:
        return CommandResult(command=name, args=[name], returncode=0)


class FixedPlanProvider(MockAgentProvider):
    """按固定步骤计划执行的确定性 provider：decide 直接返回计划步骤对应的工具。"""

    name = "mock-fixed"
    mock = True

    def __init__(self, steps: list[PlannedStep]) -> None:
        self.steps = steps

    async def plan(self, task: str, context: Any, max_steps: int) -> PlanResult:
        return PlanResult(goal=task, steps=list(self.steps[:max_steps]), model_used=self.name, mock=True)

    async def decide(
        self,
        step: PlannedStep,
        snapshot: ScreenSnapshot | None,
        feedback: str | None = None,
    ) -> ToolDecision:
        return ToolDecision(
            tool=step.tool,
            target=step.target,
            text=step.text,
            coordinate=step.coordinate,
            direction=step.direction,
            wait_seconds=step.wait_seconds,
            reasoning="fixed plan decision",
        )
