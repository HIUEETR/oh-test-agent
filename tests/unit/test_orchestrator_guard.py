import time
from pathlib import Path

from harmony_test_agent.agents.orchestrator import AgentOrchestrator
from harmony_test_agent.discovery import ExplorationPolicy
from harmony_test_agent.models import (
    CommandResult,
    PlannedStep,
    RunTrace,
    ScreenSnapshot,
    ToolDecision,
    ToolName,
)
from harmony_test_agent.perception.normalizer import normalize_layout, page_path


def test_premature_finish_is_replaced_with_planned_tool() -> None:
    step = PlannedStep(step_id="open", instruction="打开应用", tool=ToolName.OPEN_APP)
    decision = ToolDecision(tool=ToolName.FINISH, text="应用已经打开")

    constrained = AgentOrchestrator._constrain_finish_decision(step, decision)

    assert constrained.tool == ToolName.OPEN_APP
    assert "finish is only valid" in constrained.reasoning


def test_non_finish_adaptation_is_preserved() -> None:
    step = PlannedStep(step_id="back", instruction="返回首页", tool=ToolName.BACK)
    decision = ToolDecision(tool=ToolName.INSPECT_SCREEN)

    assert AgentOrchestrator._constrain_finish_decision(step, decision) is decision


def test_planned_finish_cannot_be_replaced_by_another_tool() -> None:
    step = PlannedStep(step_id="finish", instruction="结束", tool=ToolName.FINISH)
    decision = ToolDecision(tool=ToolName.INSPECT_SCREEN)

    constrained = AgentOrchestrator._constrain_finish_decision(step, decision)

    assert constrained.tool == ToolName.FINISH


def _hierarchy(key: str, text: str) -> dict:
    """单按钮层级；key/text 变化即代表层级（内容）发生了变化。"""
    return {
        "attributes": {"type": "List", "scrollable": "true", "visible": "true", "bounds": "[0,50][1080,1900]"},
        "children": [
            {
                "attributes": {
                    "key": key,
                    "text": text,
                    "type": "Button",
                    "clickable": "true",
                    "visible": "true",
                    "enabled": "true",
                    "bounds": "[20,10][180,30]",
                },
                "children": [],
            }
        ],
    }


class _HierarchyDevice:
    """截图与层级可编排的假设备：用于稳定帧采集的确定性验证。"""

    def __init__(self, hierarchies: list[dict]) -> None:
        self.hierarchies = hierarchies
        self.polls = 0
        self.labels: list[str] = []

    def screenshot(self, output_dir: Path, run_id: str, label: str = "screen") -> ScreenSnapshot:
        del output_dir
        self.labels.append(label)
        hierarchy = self.hierarchies[min(self.polls, len(self.hierarchies) - 1)]
        return ScreenSnapshot(
            snapshot_id=f"shot-{len(self.labels)}",
            run_id=run_id,
            image_path=Path(f"{label}.png"),
            image_sha256=f"hash-{len(self.labels)}",
            width=1080,
            height=1920,
            page_path=page_path(hierarchy),
            elements=normalize_layout(hierarchy, 1080, 1920),
        )

    def collect_ui_hierarchy(self) -> dict:
        self.polls += 1
        return self.hierarchies[min(self.polls, len(self.hierarchies) - 1)]

    def wait(self, seconds: float) -> CommandResult:
        del seconds
        return CommandResult(command="wait", returncode=0)


def _stable_frame_trace() -> RunTrace:
    return RunTrace(
        run_id="run-stable-frame",
        target_app_id="t",
        task="",
        device_id="device-1",
        mode="exploration",
        model_used="m",
        model_mock=True,
        exploration_policy=ExplorationPolicy(settle_timeout_seconds=1),
    )


def test_capture_stable_frame_returns_first_frame_when_hierarchy_is_stable(tmp_path: Path) -> None:
    hierarchy = _hierarchy("btn_home", "首页")
    device = _HierarchyDevice([hierarchy, hierarchy])

    snapshot = AgentOrchestrator._capture_stable_frame(device, tmp_path, _stable_frame_trace(), "step_01_after")

    assert snapshot.snapshot_id == "shot-1"
    assert device.labels == ["step_01_after"]


def test_capture_stable_frame_recaptures_when_hierarchy_keeps_changing(tmp_path: Path) -> None:
    # 层级与首帧持续不同（不回到初始状态）：预算耗尽后补截 -settled 帧
    device = _HierarchyDevice(
        [_hierarchy("btn_home", "首页"), _hierarchy("btn_other", "加载中"), _hierarchy("btn_other", "加载中")]
    )
    started = time.monotonic()

    snapshot = AgentOrchestrator._capture_stable_frame(device, tmp_path, _stable_frame_trace(), "step_02_after")

    assert device.labels == ["step_02_after", "step_02_after-settled"]
    assert snapshot.snapshot_id == "shot-2"
    assert time.monotonic() - started >= 0.5
