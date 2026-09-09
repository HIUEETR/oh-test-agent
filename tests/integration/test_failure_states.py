from pathlib import Path

from PIL import Image

from harmony_test_agent.agents import AgentOrchestrator, MockAgentProvider
from harmony_test_agent.config import Settings
from harmony_test_agent.devices import DeviceAdapter
from harmony_test_agent.models import (
    CommandResult,
    RunRequest,
    RunState,
    ScreenSnapshot,
    TargetAppProfile,
)
from harmony_test_agent.storage import ArtifactStore, RunRepository


class MinimalDevice(DeviceAdapter):
    def __init__(self, connected: bool = True):
        self.connected = connected

    def connect(self) -> None:
        pass

    def health_check(self) -> dict[str, object]:
        return {"connected": self.connected, "id": "failure-device", "resolution": [100, 200]}

    def screenshot(self, output_dir: Path, run_id: str, label: str = "screen") -> ScreenSnapshot:
        output_dir.mkdir(parents=True, exist_ok=True)
        image_path = output_dir / f"{label}.png"
        Image.new("RGB", (100, 200), "black").save(image_path)
        return ScreenSnapshot(
            snapshot_id=label,
            run_id=run_id,
            image_path=image_path,
            image_sha256="black",
            width=100,
            height=200,
        )

    def collect_ui_hierarchy(self) -> dict:
        return {}

    def collect_logs(self, output_path: Path) -> CommandResult:
        return self._ok("logs")

    def open_app(self, profile: TargetAppProfile, reset: bool = False) -> CommandResult:
        return self._ok("open_app")

    def click(self, x: int, y: int) -> CommandResult:
        return self._ok("click")

    def input_text(self, text: str, x: int | None = None, y: int | None = None) -> CommandResult:
        return self._ok("input_text")

    def swipe(self, start: tuple[int, int], end: tuple[int, int], duration: float = 0.5) -> CommandResult:
        return self._ok("swipe")

    def back(self) -> CommandResult:
        return self._ok("back")

    def wait(self, seconds: float) -> CommandResult:
        return self._ok("wait")

    def close(self) -> None:
        pass

    @staticmethod
    def _ok(name: str) -> CommandResult:
        return CommandResult(command=name, args=[name], returncode=0)


class FailingVisionProvider(MockAgentProvider):
    name = "failing-vision"
    mock = False

    async def analyze(self, snapshot: ScreenSnapshot):
        raise TimeoutError("vision timeout")


def make_orchestrator(tmp_path: Path, device: MinimalDevice, provider: MockAgentProvider):
    profile_path = tmp_path / "profile.json"
    profile = TargetAppProfile(
        target_app_id="zhihu-plus",
        display_name="知乎++",
        bundle_name="com.example",
    )
    profile_path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    settings = Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=profile_path,
        runtime_home=tmp_path / "home",
        agent_action_timeout=1,
    )
    return AgentOrchestrator(
        settings,
        provider=provider,
        repository=RunRepository(settings.resolved_database_path),
        artifacts=ArtifactStore(settings.resolved_runtime_dir),
        device_factory=lambda _: device,
        settle_seconds=0,
    )


async def test_model_failure_stops_before_device_action(tmp_path):
    orchestrator = make_orchestrator(tmp_path, MinimalDevice(), FailingVisionProvider())
    trace = await orchestrator.run(RunRequest(task="检查页面", auto_generate=False))
    assert trace.state == RunState.FAILED_MODEL
    assert trace.actions == []
    assert "vision analysis failed" in (trace.error or "")


async def test_disconnected_device_stops_before_planning(tmp_path):
    orchestrator = make_orchestrator(tmp_path, MinimalDevice(connected=False), MockAgentProvider())
    trace = await orchestrator.run(RunRequest(task="检查页面", auto_generate=False))
    assert trace.state == RunState.FAILED_DEVICE
    assert trace.plan == []
    assert trace.actions == []
