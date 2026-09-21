"""Live 运行中检测的集成测试（Phase 2.2/2.3）。

钉住三件事：

1. 结构停滞在**执行过程中**被发现：``ActionResult.anomaly`` 非空、``trace.defects`` 非空、
   发出 ``ANOMALY_DETECTED`` 事件；
2. **G6 红线**：run 仍然 ``COMPLETED`` —— 分析/检测绝不翻转结论；
3. **G7**：无可疑时设备零额外调用。
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

from PIL import Image

from harmony_test_agent.agents import AgentOrchestrator
from harmony_test_agent.agents.providers import MockAgentProvider, PlanningContext
from harmony_test_agent.analysis.in_run import InRunDetector, InRunThresholds
from harmony_test_agent.config import Settings
from harmony_test_agent.devices import DeviceAdapter
from harmony_test_agent.models import (
    BoundingBox,
    CommandResult,
    EventType,
    PlannedStep,
    PlanResult,
    RunRequest,
    RunState,
    ScreenSnapshot,
    TargetQuery,
    ToolDecision,
    ToolName,
    UIElement,
)
from harmony_test_agent.storage import ArtifactStore, RunRepository
from harmony_test_agent.targets.catalog import InstalledApp

BUNDLE = "com.example.inrun"


class StuckDevice(DeviceAdapter):
    """点击后**什么都不变**的假设备：结构停滞信号的最小可复现形态。

    点击把每帧都渲染成同一个纯色图 ⇒ ``image_sha256`` 恒定；元素表恒定 ⇒ 结构指纹恒定。
    前台应用始终是目标应用，因此检测器不会走「前台丢失 + 崩溃取证」分支。
    """

    def __init__(self, *, clicks_change_screen: bool = False, size_factor: float = 1.0) -> None:
        self.device_id = "stuck-device"
        self.connected = False
        self.clicks_change_screen = clicks_change_screen
        self.size_factor = size_factor
        self.click_count = 0
        self.foreground_queries = 0
        # 启动前在桌面：``open_app`` 把页面切到应用首页，这样「打开应用」这一步
        # 本身就是一次正常的结构变化，不会被误当成停滞。
        self.phase = "launcher"

    # -- 连接与目录 --------------------------------------------------
    def connect(self) -> None:
        self.connected = True

    def health_check(self) -> dict[str, object]:
        return {"connected": self.connected, "id": self.device_id, "resolution": [800, 1200]}

    def inspect_app(self, bundle_name: str) -> InstalledApp:
        return InstalledApp(
            bundle_name=BUNDLE,
            display_name="InRun",
            abilities=("EntryAbility",),
            main_ability="EntryAbility",
            module_name="entry",
            version_name="1.0.0",
            version_code=1,
        )

    def list_installed_apps(self) -> list[InstalledApp]:
        return [self.inspect_app(BUNDLE)]

    # -- 采集 --------------------------------------------------------
    def _elements(self) -> list[UIElement]:
        if self.phase == "launcher":
            return [
                UIElement(
                    element_id="app-icon",
                    content="测试应用",
                    key="launcher_icon_inrun",
                    type="Button",
                    clickable=True,
                    enabled=True,
                    bbox=BoundingBox(left=20, top=20, right=200, bottom=90),
                )
            ]
        elements = [
            UIElement(
                element_id="noop",
                content="无响应控件",
                key="p2_noop_button",
                type="Button",
                clickable=True,
                enabled=True,
                bbox=BoundingBox(left=20, top=20, right=200, bottom=90),
            )
        ]
        if self.clicks_change_screen and self.click_count:
            elements.append(
                UIElement(
                    element_id="changed",
                    content="点开了新页面",
                    key="p2_new_page",
                    type="Column",
                    enabled=True,
                    bbox=BoundingBox(left=20, top=200, right=300, bottom=400),
                )
            )
        return elements

    def screenshot(self, output_dir: Path, run_id: str, label: str = "screen") -> ScreenSnapshot:
        output_dir.mkdir(parents=True, exist_ok=True)
        width = int(800 * self.size_factor)
        height = int(1200 * self.size_factor)
        # 文件名必须区分 phase / click_count：共用同一路径会让后一帧覆盖前一帧的文件，
        # 从而把「结构确实变了」误判成「像素相同」。
        image_path = output_dir / f"{label}-{self.phase}-{self.click_count}.png"
        buffer = io.BytesIO()
        # 颜色随页面与点击数变化：真实设备上「页面变了」必然意味着像素也变了。
        # 固定纯色会让每一帧字节完全相同，把正常变化误报成「像素相同」。
        shade = 30 if self.phase == "launcher" else 90 + (self.click_count % 3) * 40
        Image.new("RGB", (width, height), (shade, shade, shade)).save(buffer, format="PNG")
        payload = buffer.getvalue()
        image_path.write_bytes(payload)
        return ScreenSnapshot(
            snapshot_id=f"{label}-{self.phase}-{self.click_count}-{self.size_factor}",
            run_id=run_id,
            image_path=image_path.resolve(),
            image_sha256=hashlib.sha256(payload).hexdigest(),
            width=width,
            height=height,
            page_path="pages/Launcher" if self.phase == "launcher" else "pages/Index",
            elements=self._elements(),
        )

    def collect_ui_hierarchy(self) -> dict:
        return {}

    def collect_logs(self, output_path: Path) -> CommandResult:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("", encoding="utf-8")
        return CommandResult(command="hilog", returncode=0, stdout="")

    def current_foreground_app(self):
        from harmony_test_agent.targets import ForegroundApp

        self.foreground_queries += 1
        return ForegroundApp(bundle_name=BUNDLE, ability_name="EntryAbility", window_type="main")

    # -- 交互 --------------------------------------------------------
    def open_app(self, profile, reset: bool = False) -> CommandResult:
        self.phase = "home"
        return CommandResult(command="open_app", returncode=0)

    def click(self, x: int, y: int) -> CommandResult:
        self.click_count += 1
        return CommandResult(command="click", returncode=0)

    def input_text(self, text: str, x: int | None = None, y: int | None = None) -> CommandResult:
        return CommandResult(command="input_text", returncode=0)

    def swipe(self, start: tuple[int, int], end: tuple[int, int], duration: float = 0.5) -> CommandResult:
        return CommandResult(command="swipe", returncode=0)

    def back(self) -> CommandResult:
        return CommandResult(command="back", returncode=0)

    def wait(self, seconds: float) -> CommandResult:
        return CommandResult(command="wait", returncode=0)

    def close(self) -> None:
        self.connected = False


class ClickThenFinishProvider(MockAgentProvider):
    """计划：打开应用 → 点击控件 → 结束。"""

    name = "click-then-finish"
    mock = True

    async def plan(self, task: str, context: PlanningContext, max_steps: int) -> PlanResult:
        del context
        steps = [
            PlannedStep(step_id="S1", instruction="打开应用", tool=ToolName.OPEN_APP),
            PlannedStep(step_id="S2", instruction="点击控件", tool=ToolName.CLICK_ELEMENT, target="p2_noop_button"),
            PlannedStep(step_id="S3", instruction="结束", tool=ToolName.FINISH),
        ]
        return PlanResult(goal=task, steps=steps[:max_steps], model_used=self.name, mock=True)

    async def decide(
        self,
        step: PlannedStep,
        snapshot: ScreenSnapshot | None,
        feedback: str | None = None,
    ) -> ToolDecision:
        del snapshot, feedback
        if step.tool == ToolName.CLICK_ELEMENT:
            return ToolDecision(
                tool=ToolName.CLICK_ELEMENT, target=step.target, coordinate=(110, 55), reasoning="click"
            )
        return ToolDecision(tool=step.tool, target=step.target, text=step.text, reasoning="planned")


def _settings(tmp_path: Path, **overrides) -> Settings:
    payload = {
        "agent_provider": "mock",
        "runtime_dir": tmp_path / "runs",
        "database_path": tmp_path / "agent.db",
        "target_profile_path": None,
        "profiles_dir": tmp_path / "profiles",
        "runtime_home": tmp_path / "home",
        "settle_seconds": 0,
        "unchanged_screen_limit": 5,
    }
    payload.update(overrides)
    return Settings(**payload)


def _orchestrator(
    tmp_path: Path,
    device: DeviceAdapter,
    *,
    detector_factory=None,
    settings: Settings | None = None,
) -> AgentOrchestrator:
    resolved = settings or _settings(tmp_path)
    return AgentOrchestrator(
        resolved,
        provider=ClickThenFinishProvider(),
        repository=RunRepository(resolved.resolved_database_path),
        artifacts=ArtifactStore(resolved.resolved_runtime_dir),
        device_factory=lambda _: device,
        settle_seconds=0,
        launch_settle_seconds=0,
        in_run_detector_factory=detector_factory,
    )


def _request() -> RunRequest:
    return RunRequest(
        target=TargetQuery(bundle_name=BUNDLE),
        task="打开应用并点击控件，确认页面有响应",
        auto_generate=False,
        auto_execute=False,
    )


def _stall_factory(thresholds: InRunThresholds):
    def factory(device: DeviceAdapter, bundle_name: str) -> InRunDetector:
        return InRunDetector(device, bundle_name=bundle_name, thresholds=thresholds)

    return factory


class TestStructuralStallIsDetectedInRun:
    async def test_stalled_click_records_a_defect_and_emits_an_event(self, tmp_path: Path) -> None:
        device = StuckDevice()
        orchestrator = _orchestrator(
            tmp_path,
            device,
            detector_factory=_stall_factory(InRunThresholds(escalate_count=1, screen_scan_enabled=False)),
        )

        trace = await orchestrator.run(_request())

        events = [event for event in trace.events if event.type is EventType.ANOMALY_DETECTED]
        assert events, "运行中检测必须发出 ANOMALY_DETECTED"
        assert trace.defects, "finding 必须写进 trace.defects"
        assert all(finding.phase == "in_run" for finding in trace.defects)

        clicked = [action for action in trace.actions if action.step_id == "S2"]
        assert clicked and clicked[0].anomaly is not None
        assert clicked[0].anomaly.action_id == "S2"

    async def test_run_still_completes_when_a_defect_is_found(self, tmp_path: Path) -> None:
        """G6 红线：发现 critical 缺陷也不翻转 run 结论。"""
        device = StuckDevice()
        orchestrator = _orchestrator(
            tmp_path,
            device,
            detector_factory=_stall_factory(InRunThresholds(escalate_count=1, screen_scan_enabled=False)),
        )

        trace = await orchestrator.run(_request())

        assert trace.state is RunState.COMPLETED
        assert trace.agent_outcome == "completed"
        assert trace.agent_error is None

    async def test_responding_click_makes_zero_extra_device_calls(self, tmp_path: Path) -> None:
        """G7：页面确实变了 ⇒ 检测器在昂贵取证上零设备调用。

        ``size_factor`` 让 ``OPEN_APP`` 造成的帧稳定轮询改变截图尺寸（像素摘要变化），
        而 ``CLICK_ELEMENT`` 之后结构确实变了 ⇒ 廉价门直接放行。
        """
        device = StuckDevice(clicks_change_screen=True, size_factor=1.5)
        captured: list[InRunDetector] = []

        def factory(device_adapter: DeviceAdapter, bundle_name: str) -> InRunDetector:
            detector = InRunDetector(
                device_adapter,
                bundle_name=bundle_name,
                thresholds=InRunThresholds(escalate_count=1, screen_scan_enabled=False),
            )
            captured.append(detector)
            return detector

        orchestrator = _orchestrator(tmp_path, device, detector_factory=factory)

        trace = await orchestrator.run(_request())

        assert trace.defects == []
        assert not [event for event in trace.events if event.type is EventType.ANOMALY_DETECTED]
        # 检测器自己一次设备调用都没发（``foreground_queries`` 不参与断言：Profile 引导
        # 路径本来就会查前台，与本检测无关）。
        assert captured and captured[0].metrics["device_calls"] == 0
        assert captured[0].metrics["suspicious"] == 0

    async def test_default_factory_is_inert_when_injected_as_none(self, tmp_path: Path) -> None:
        """未注入工厂（单测默认）时行为与今天一致：没有检测、没有 finding、仍有事件流。"""
        device = StuckDevice()
        settings = _settings(tmp_path, analysis_in_run_detection=False)
        orchestrator = _orchestrator(tmp_path, device, settings=settings)

        trace = await orchestrator.run(_request())

        assert trace.defects == []
        assert trace.state is RunState.COMPLETED
        assert not [event for event in trace.events if event.type is EventType.ANOMALY_DETECTED]
