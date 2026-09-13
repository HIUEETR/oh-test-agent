from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image

from harmony_test_agent.agents import AgentOrchestrator, MockAgentProvider
from harmony_test_agent.config import Settings
from harmony_test_agent.devices import DeviceAdapter
from harmony_test_agent.models import (
    BoundingBox,
    CommandResult,
    RunRequest,
    RunState,
    ScreenSnapshot,
    TargetAppProfile,
    UIElement,
)
from harmony_test_agent.storage import ArtifactStore, RunRepository


class FakeDevice(DeviceAdapter):
    def __init__(self):
        self.state = "launcher"
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def health_check(self) -> dict[str, object]:
        return {"connected": self.connected, "id": "fake-device", "resolution": [800, 1200]}

    def screenshot(self, output_dir: Path, run_id: str, label: str = "screen") -> ScreenSnapshot:
        output_dir.mkdir(parents=True, exist_ok=True)
        colors = {"launcher": "black", "home": "navy", "search": "teal", "search_input": "green", "detail": "purple"}
        image_path = output_dir / f"{label}_{self.state}.png"
        Image.new("RGB", (800, 1200), colors[self.state]).save(image_path)
        elements = self._elements()
        return ScreenSnapshot(
            snapshot_id=f"{label}-{self.state}",
            run_id=run_id,
            image_path=image_path.resolve(),
            image_sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
            width=800,
            height=1200,
            page_path="pages/Index",
            elements=elements,
        )

    def _elements(self):
        if self.state == "home":
            return [
                UIElement(
                    element_id="search",
                    content="搜索",
                    key="p2_home_titlebar_search",
                    type="Button",
                    clickable=True,
                    bbox=BoundingBox(left=20, top=20, right=180, bottom=80),
                ),
                UIElement(
                    element_id="detail",
                    content="一条内容详情",
                    key="p2_home_feed_card_answer_1",
                    type="Button",
                    clickable=True,
                    bbox=BoundingBox(left=200, top=250, right=600, bottom=600),
                ),
            ]
        if self.state in {"search", "search_input"}:
            elements = [
                UIElement(
                    element_id="input",
                    content="OpenHarmony" if self.state == "search_input" else "搜索输入框",
                    key="p2_search_input",
                    type="TextInput",
                    editable=True,
                    bbox=BoundingBox(left=80, top=100, right=700, bottom=180),
                )
            ]
            return elements
        if self.state == "detail":
            return [
                UIElement(
                    element_id="content",
                    content="内容详情",
                    key="p2_answer_detail_page",
                    type="Column",
                    bbox=BoundingBox(left=10, top=80, right=790, bottom=1100),
                )
            ]
        return []

    def collect_ui_hierarchy(self) -> dict:
        return {}

    def collect_logs(self, output_path: Path) -> CommandResult:
        output_path.write_text("fake logs", encoding="utf-8")
        return self._ok("logs")

    def open_app(self, profile: TargetAppProfile, reset: bool = False) -> CommandResult:
        self.state = "home"
        return self._ok("open_app")

    def click(self, x: int, y: int) -> CommandResult:
        self.state = "search" if x < 200 else "detail"
        return self._ok("click")

    def input_text(self, text: str, x: int | None = None, y: int | None = None) -> CommandResult:
        self.state = "search_input"
        return self._ok("input_text")

    def swipe(self, start: tuple[int, int], end: tuple[int, int], duration: float = 0.5) -> CommandResult:
        return self._ok("swipe")

    def back(self) -> CommandResult:
        self.state = "home"
        return self._ok("back")

    def wait(self, seconds: float) -> CommandResult:
        return self._ok("wait")

    def close(self) -> None:
        self.connected = False

    @staticmethod
    def _ok(name: str) -> CommandResult:
        return CommandResult(command=name, args=[name], returncode=0)


async def test_mock_agent_runs_full_vertical_slice(tmp_path):
    profile_path = tmp_path / "profile.json"
    profile = TargetAppProfile(
        target_app_id="zhihu-plus",
        display_name="知乎++",
        bundle_name="com.example",
        main_ability="EntryAbility",
        device_selector={"serial": "fake-device"},
    )
    profile_path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    settings = Settings(
        agent_provider="mock",
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=profile_path,
        runtime_home=tmp_path / "home",
        unchanged_screen_limit=2,
    )
    fake = FakeDevice()
    orchestrator = AgentOrchestrator(
        settings,
        provider=MockAgentProvider(),
        repository=RunRepository(settings.resolved_database_path),
        artifacts=ArtifactStore(settings.resolved_runtime_dir),
        device_factory=lambda _: fake,
        settle_seconds=0,
    )
    trace = await orchestrator.run(
        RunRequest(
            task="打开知乎++，进入搜索，输入 OpenHarmony，返回首页，打开一条内容详情，查看内容后返回首页",
            auto_generate=True,
        )
    )
    assert trace.state == RunState.COMPLETED, trace.error
    # FakeDevice 无法通过 Profile 引导（无前台查询/探索面），按实时模式降级继续任务：
    # 全链路仍然覆盖 规划 → 决策 → 执行 → 页面图 → 断言 → 报告，但不生成 Hypium 脚本。
    assert trace.live_mode is True
    assert trace.generated is None
    assert len(trace.graph.nodes) >= 4
    assert len(trace.graph.edges) >= 3
    assert len(trace.assertions) == 3
    assert (settings.resolved_runtime_dir / trace.run_id / "reports" / "report.html").exists()
