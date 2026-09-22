"""改动 1 + 改动 2 的端到端回归：网易云那次「点击无反应」必须在产物里留下来。

复刻 run-20260922T141003Z-6bf8bf42 的故障模式（**轮播在动、点击没产生导航**）：

- 点击海报后页面路径 / 元素数 / 海报位置全未变，只有轮播文案换了一帧（像素与结构指纹都变）；
- 计划里的检查点是**反向断言**（``expects_defect=True``）：通过即代表观测到异常。

三件事必须同时成立（Phase 4 验收 (a)(b)）：

1. ``trace.defects`` 里有 ``noop_navigation``，发出 ``ANOMALY_DETECTED``；
2. ``GET /api/defects`` 查得到，报告缺陷章节有记录；
3. **advisory 红线**：``trace.state == completed``，断言 ``passed`` 不变。
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from harmony_test_agent.agents import AgentOrchestrator
from harmony_test_agent.agents.providers import MockAgentProvider, PlanningContext
from harmony_test_agent.analysis.defects import DefectRecorder, DefectStatus
from harmony_test_agent.analysis.in_run import InRunDetector, InRunThresholds
from harmony_test_agent.api.app import create_app
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
from harmony_test_agent.reporting import ReportBuilder
from harmony_test_agent.storage import ArtifactStore, DefectRepository, RunRepository
from harmony_test_agent.targets.catalog import InstalledApp

BUNDLE = "com.example.neteasymusic"
POSTER_KEY = "p2_daily_card"
EXPECTED = "点击第一个海报后该海报仍可见，页面未跳转，说明点击无反应"


class PosterDevice(DeviceAdapter):
    """点击海报后**页面语义不变**的假设备。

    ``click`` 只让轮播文案前进一帧：像素与结构指纹因此都变了（不会走「整页冻结」那条路），
    但 ``page_path``、元素数与被点元素 bbox 三者完全不变 —— 正是网易云那次的形态。
    """

    def __init__(self) -> None:
        self.device_id = "poster-device"
        self.connected = False
        self.click_count = 0
        self.phase = "launcher"

    # -- 连接与目录 --------------------------------------------------
    def connect(self) -> None:
        self.connected = True

    def health_check(self) -> dict[str, object]:
        return {"connected": self.connected, "id": self.device_id, "resolution": [1320, 2232]}

    def inspect_app(self, bundle_name: str) -> InstalledApp:
        return InstalledApp(
            bundle_name=BUNDLE,
            display_name="网易云音乐",
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
                    content="网易云音乐",
                    key="launcher_icon_netease",
                    type="Button",
                    clickable=True,
                    enabled=True,
                    bbox=BoundingBox(left=20, top=20, right=200, bottom=90),
                )
            ]
        return [
            UIElement(
                element_id="ui-daily-card",
                content="每日推荐卡片播放按钮",
                key=POSTER_KEY,
                type="ListItem",
                clickable=True,
                enabled=True,
                bbox=BoundingBox(left=48, top=285, right=528, bottom=915),
            ),
            UIElement(
                element_id="ui-carousel",
                content=f"轮播第 {self.click_count} 帧",
                key="home_carousel_title",
                type="Text",
                enabled=True,
                bbox=BoundingBox(left=40, top=940, right=1280, bottom=1010),
            ),
        ]

    def screenshot(self, output_dir: Path, run_id: str, label: str = "screen") -> ScreenSnapshot:
        output_dir.mkdir(parents=True, exist_ok=True)
        image_path = output_dir / f"{label}-{self.phase}-{self.click_count}.png"
        buffer = io.BytesIO()
        shade = 30 if self.phase == "launcher" else 90 + (self.click_count % 5) * 25
        Image.new("RGB", (1320, 2232), (shade, shade, shade)).save(buffer, format="PNG")
        payload = buffer.getvalue()
        image_path.write_bytes(payload)
        return ScreenSnapshot(
            snapshot_id=f"{label}-{self.phase}-{self.click_count}",
            run_id=run_id,
            image_path=image_path.resolve(),
            image_sha256=hashlib.sha256(payload).hexdigest(),
            width=1320,
            height=2232,
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

        return ForegroundApp(bundle_name=BUNDLE, ability_name="EntryAbility", window_type="main")

    # -- 交互 --------------------------------------------------------
    def open_app(self, profile, reset: bool = False) -> CommandResult:
        self.phase = "home"
        return CommandResult(command="open_app", returncode=0)

    def click(self, x: int, y: int) -> CommandResult:
        # 只让轮播前进：页面语义、元素数与被点元素位置都不变。
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


class NoopPosterProvider(MockAgentProvider):
    """计划：打开应用 → 点海报 → **反向断言**（海报仍在 ⇒ 点击无反应）→ 结束。"""

    name = "noop-poster"
    mock = True

    async def plan(self, task: str, context: PlanningContext, max_steps: int) -> PlanResult:
        del context
        steps = [
            PlannedStep(step_id="S1", instruction="打开网易云音乐", tool=ToolName.OPEN_APP),
            PlannedStep(
                step_id="S2",
                instruction="点击首页的第一个海报",
                tool=ToolName.CLICK_ELEMENT,
                target=POSTER_KEY,
                expected="应打开播放页",
            ),
            PlannedStep(
                step_id="S3",
                instruction="确认点击后是否无反应",
                tool=ToolName.ASSERT_VISIBLE,
                target=POSTER_KEY,
                expected=EXPECTED,
                expects_defect=True,
            ),
            PlannedStep(step_id="S4", instruction="结束", tool=ToolName.FINISH),
        ]
        return PlanResult(goal=task, steps=steps[:max_steps], model_used=self.name, mock=True)

    async def decide(
        self,
        step: PlannedStep,
        snapshot: ScreenSnapshot | None,
        feedback: str | None = None,
    ) -> ToolDecision:
        del snapshot, feedback
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
        # 默认检测器会额外产出一条 PAGE_UNRESPONSIVE 停滞 finding（帧完全相同的场景）；
        # 这里只隔离「点击未导航」这条判据。
        "analysis_in_run_detection": False,
    }
    payload.update(overrides)
    return Settings(**payload)


def _request(*, auto_generate: bool = False) -> RunRequest:
    return RunRequest(
        target=TargetQuery(bundle_name=BUNDLE),
        task="点击首页的第一个海报，查看是否会无反应",
        auto_generate=auto_generate,
        auto_execute=False,
    )


def _orchestrator(tmp_path: Path, *, settings: Settings, recorder=None, detector_factory=None):
    return AgentOrchestrator(
        settings,
        provider=NoopPosterProvider(),
        repository=RunRepository(settings.resolved_database_path),
        artifacts=ArtifactStore(settings.resolved_runtime_dir),
        device_factory=lambda _: PosterDevice(),
        settle_seconds=0,
        launch_settle_seconds=0,
        defect_recorder=recorder,
        in_run_detector_factory=detector_factory,
    )


class TestReverseAssertionEndToEnd:
    async def test_run_records_the_no_op_defect_and_still_completes(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        repository = DefectRepository(settings.resolved_database_path)
        orchestrator = _orchestrator(tmp_path, settings=settings, recorder=DefectRecorder(repository))

        trace = await orchestrator.run(_request())

        # (b) advisory 红线：run 仍然 completed，断言本身仍然 passed。
        assert trace.state is RunState.COMPLETED
        assert trace.agent_outcome == "completed"
        assertion = next(item for item in trace.assertions if item.target == POSTER_KEY)
        assert assertion.passed is True
        assert assertion.expects_defect is True
        # (a) 缺陷被记录下来。
        kinds = [finding.kind.value for finding in trace.defects]
        assert kinds == ["noop_navigation"]
        assert [event.type for event in trace.events].count(EventType.ANOMALY_DETECTED) == 1
        # 缺陷落库。
        assert repository.count() == 1
        record = repository.get(repository.list()[0].defect_id)
        assert record is not None
        assert record.kind.value == "noop_navigation"
        assert record.bundle_name == BUNDLE
        assert record.status is DefectStatus.SUSPECTED
        assert record.occurrences == 1
        assert EXPECTED in record.summary_zh

    async def test_defect_is_visible_through_the_http_api(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        repository = DefectRepository(settings.resolved_database_path)
        orchestrator = _orchestrator(tmp_path, settings=settings, recorder=DefectRecorder(repository))
        await orchestrator.run(_request())

        with TestClient(create_app(settings), raise_server_exceptions=False) as client:
            response = client.get("/api/defects")

        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        items = payload["defects"]
        assert len(items) == 1
        assert items[0]["kind"] == "noop_navigation"
        assert items[0]["bundle_name"] == BUNDLE

    async def test_report_defect_section_mentions_the_finding(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        orchestrator = _orchestrator(tmp_path, settings=settings, recorder=None)

        trace = await orchestrator.run(_request())
        report = ReportBuilder(orchestrator.artifacts).build(trace)
        document = report.read_text(encoding="utf-8")

        assert "疑似应用缺陷" in document
        assert "断言确认了异常现象" in document

    async def test_generated_case_carries_the_reverse_polarity(self, tmp_path: Path) -> None:
        """改动 2.6：IR 里的 ``case_spec.json`` 必须携带 ``polarity="unexpected"``。"""
        import json

        settings = _settings(tmp_path)
        orchestrator = _orchestrator(tmp_path, settings=settings, recorder=None)

        trace = await orchestrator.run(_request(auto_generate=True))
        case_spec = orchestrator.artifacts.run_dir(trace.run_id) / "generated" / "case_spec.json"

        assert case_spec.is_file(), "运行必须产出 case_spec.json"
        payload = json.loads(case_spec.read_text(encoding="utf-8"))
        polarities = [
            checkpoint["polarity"]
            for step in payload["steps"]
            for checkpoint in step.get("checkpoints", [])
            if "polarity" in checkpoint
        ]
        assert polarities and set(polarities) == {"unexpected"}


class TestDetectorAndReverseAssertionAgree:
    async def test_both_signals_merge_into_one_defect(self, tmp_path: Path) -> None:
        """改动 1 的三条件判据与改动 2 的反向断言描述同一处「点击无反应」⇒ 归并成一条缺陷。"""
        settings = _settings(tmp_path, analysis_in_run_detection=True)
        repository = DefectRepository(settings.resolved_database_path)

        def factory(device: DeviceAdapter, bundle_name: str) -> InRunDetector:
            return InRunDetector(
                device,
                bundle_name=bundle_name,
                # 廉价门全本地：帧确实变了（轮播前进一帧）⇒ 不需要任何昂贵取证。
                thresholds=InRunThresholds(escalate_count=2, screen_scan_enabled=False, probe_enabled=False),
            )

        orchestrator = _orchestrator(
            tmp_path,
            settings=settings,
            recorder=DefectRecorder(repository),
            detector_factory=factory,
        )

        trace = await orchestrator.run(_request())

        assert trace.state is RunState.COMPLETED
        kinds = sorted(finding.kind.value for finding in trace.defects)
        assert kinds == ["noop_navigation", "noop_navigation"]
        assert {finding.action_id for finding in trace.defects} == {"S2", "S3"}
        # 同一个 (bundle, kind, page_path, target) ⇒ 一条缺陷、两次观测。
        assert repository.count() == 1
        record = repository.get(repository.list()[0].defect_id)
        assert record is not None
        assert record.occurrences == 2
