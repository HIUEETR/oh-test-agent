"""步骤失败自适应恢复循环的集成测试：失败反馈、纠正动作、完成判定与预算。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image

from harmony_test_agent.agents import AgentOrchestrator
from harmony_test_agent.agents.providers import MockAgentProvider, PlanningContext
from harmony_test_agent.config import Settings
from harmony_test_agent.devices import DeviceAdapter
from harmony_test_agent.models import (
    BoundingBox,
    CommandResult,
    ExplorationPolicy,
    PlannedStep,
    PlanResult,
    RunRequest,
    RunState,
    ScreenSnapshot,
    ToolDecision,
    ToolName,
    UIElement,
)
from harmony_test_agent.storage import ArtifactStore, RunRepository
from harmony_test_agent.targets.catalog import InstalledApp

BUNDLE = "com.example.recovery"


class RecoveryDevice(DeviceAdapter):
    """两态假设备：点击"准备"后出现"12:00"状态元素。"""

    def __init__(self) -> None:
        self.device_id = "recovery-device"
        self.ready = False
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def health_check(self) -> dict[str, object]:
        return {"connected": self.connected, "id": self.device_id, "resolution": [800, 1200]}

    def inspect_app(self, bundle_name: str) -> InstalledApp:
        assert bundle_name == BUNDLE
        return InstalledApp(
            bundle_name=BUNDLE,
            display_name="Recovery",
            abilities=("EntryAbility",),
            main_ability="EntryAbility",
            module_name="entry",
            version_name="1.0.0",
            version_code=1,
        )

    def list_installed_apps(self) -> list[InstalledApp]:
        return [self.inspect_app(BUNDLE)]

    def _elements(self) -> list[UIElement]:
        elements = [
            UIElement(
                element_id="toggle",
                content="准备",
                type="Button",
                clickable=True,
                enabled=True,
                bbox=BoundingBox(left=20, top=20, right=200, bottom=90),
            )
        ]
        if self.ready:
            elements.append(
                UIElement(
                    element_id="status",
                    content="12:00",
                    type="Text",
                    enabled=True,
                    bbox=BoundingBox(left=20, top=200, right=300, bottom=260),
                )
            )
        return elements

    def screenshot(self, output_dir: Path, run_id: str, label: str = "screen") -> ScreenSnapshot:
        output_dir.mkdir(parents=True, exist_ok=True)
        image_path = output_dir / f"{label}.png"
        Image.new("RGB", (800, 1200), (240, 240, 240)).save(image_path)
        return ScreenSnapshot(
            snapshot_id=f"{label}-{self.ready}",
            run_id=run_id,
            image_path=image_path.resolve(),
            image_sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
            width=800,
            height=1200,
            page_path="pages/Index",
            elements=self._elements(),
        )

    def collect_ui_hierarchy(self) -> dict:
        return {}

    def collect_logs(self, output_path: Path) -> CommandResult:
        output_path.write_text("logs", encoding="utf-8")
        return CommandResult(command="hilog", returncode=0)

    def open_app(self, profile, reset: bool = False) -> CommandResult:
        return CommandResult(command="open_app", returncode=0)

    def click(self, x: int, y: int) -> CommandResult:
        self.ready = True
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


class ScriptedRecoveryProvider(MockAgentProvider):
    """按脚本返回决策的 Provider：S2 为断言步骤，记录每次 decide 的 feedback。"""

    name = "scripted-recovery"
    mock = True

    def __init__(self, script: list[ToolDecision], recovery_limit: int) -> None:
        self.script = list(script)
        self.recovery_limit = recovery_limit
        self.feedbacks: list[str | None] = []

    async def plan(self, task: str, context: PlanningContext, max_steps: int) -> PlanResult:
        del context, max_steps
        steps = [
            PlannedStep(step_id="S1", instruction="打开应用", tool=ToolName.OPEN_APP),
            PlannedStep(step_id="S2", instruction="确认状态为12:00", tool=ToolName.ASSERT_TEXT, text="12:00"),
            PlannedStep(step_id="S3", instruction="结束", tool=ToolName.FINISH),
        ]
        return PlanResult(goal=task, steps=steps, model_used=self.name, mock=True)

    async def decide(
        self,
        step: PlannedStep,
        snapshot: ScreenSnapshot | None,
        feedback: str | None = None,
    ) -> ToolDecision:
        self.feedbacks.append(feedback)
        if step.tool == ToolName.ASSERT_TEXT:
            return self.script.pop(0)
        if step.tool == ToolName.FINISH:
            return ToolDecision(tool=ToolName.FINISH, reasoning="finish")
        return ToolDecision(tool=step.tool, target=step.target, text=step.text, reasoning="planned")


def _orchestrator(tmp_path: Path, device: RecoveryDevice, provider: ScriptedRecoveryProvider) -> AgentOrchestrator:
    settings = Settings(
        agent_provider="mock",
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "home",
        agent_step_recovery_limit=provider.recovery_limit,
        settle_seconds=0,
    )
    return AgentOrchestrator(
        settings,
        provider=provider,
        repository=RunRepository(settings.resolved_database_path),
        artifacts=ArtifactStore(settings.resolved_runtime_dir),
        device_factory=lambda _: device,
        settle_seconds=0,
        launch_settle_seconds=0,
    )


def _script(*, persist_failure: bool, recovery_limit: int) -> list[ToolDecision]:
    failing = ToolDecision(tool=ToolName.ASSERT_TEXT, text="12:00", reasoning="assert (fails)")
    if persist_failure:
        return [failing] * (recovery_limit + 1)
    return [
        failing,
        ToolDecision(tool=ToolName.CLICK_ELEMENT, target="toggle", reasoning="corrective click"),
        ToolDecision(tool=ToolName.ASSERT_TEXT, text="12:00", reasoning="re-assert (passes)"),
    ]


async def _run(tmp_path: Path, provider: ScriptedRecoveryProvider, run_id: str):
    device = RecoveryDevice()
    orchestrator = _orchestrator(tmp_path, device, provider)
    return await orchestrator.run(
        RunRequest(
            target={"bundle_name": BUNDLE},
            task="确认状态为12:00",
            auto_generate=False,
            exploration_policy=ExplorationPolicy(enabled=False),
        ),
        run_id=run_id,
    )


async def test_step_recovery_completes_after_corrective_action(tmp_path: Path) -> None:
    """断言失败 → 恢复尝试纠正动作 → 重新断言通过：步骤完成且运行继续。"""
    provider = ScriptedRecoveryProvider(_script(persist_failure=False, recovery_limit=2), recovery_limit=2)

    trace = await _run(tmp_path, provider, "run-recovery-ok")

    assert trace.state == RunState.COMPLETED, trace.error
    # 反馈序列：S1 无反馈；S2 首试无反馈；恢复尝试 1 带失败反馈；重断言带"纠正成功"反馈
    assert provider.feedbacks[0] is None
    assert provider.feedbacks[1] is None
    assert "attempt 1 failed" in provider.feedbacks[2]
    assert "corrective action succeeded" in provider.feedbacks[3]
    assert len([action for action in trace.actions if not action.success]) == 1
    final_assert = [action for action in trace.actions if action.success and action.tool == ToolName.ASSERT_TEXT]
    assert len(final_assert) == 1 and final_assert[0].assertion.passed


async def test_step_recovery_exhausted_fails_run(tmp_path: Path) -> None:
    provider = ScriptedRecoveryProvider(_script(persist_failure=True, recovery_limit=1), recovery_limit=1)

    trace = await _run(tmp_path, provider, "run-recovery-exhausted")

    assert trace.state == RunState.FAILED_ASSERTION
    assert len([action for action in trace.actions if not action.success]) == 2


async def test_step_recovery_disabled_keeps_single_attempt(tmp_path: Path) -> None:
    provider = ScriptedRecoveryProvider(_script(persist_failure=True, recovery_limit=0), recovery_limit=0)

    trace = await _run(tmp_path, provider, "run-recovery-disabled")

    assert trace.state == RunState.FAILED_ASSERTION
    # S1 与 S2 各一次 decide，S2 失败后无恢复反馈
    assert len(provider.feedbacks) == 2 and all(feedback is None for feedback in provider.feedbacks)


async def test_safety_error_is_never_recovered(tmp_path: Path) -> None:
    """安全违规不进入恢复循环：一次决策即终止。"""
    provider = ScriptedRecoveryProvider(
        [ToolDecision(tool=ToolName.CLICK_ELEMENT, target="删除用户数据", reasoning="blocked")], recovery_limit=2
    )

    trace = await _run(tmp_path, provider, "run-recovery-safety")

    assert trace.state == RunState.FAILED_ACTION
    assert any("blocked" in (action.error or "") for action in trace.actions if not action.success)
    # S1 与 S2 各一次 decide：安全违规后没有恢复反馈
    assert len(provider.feedbacks) == 2 and all(feedback is None for feedback in provider.feedbacks)
