"""Phase 5.5 回归（R10）：滚轮选择器必须能表达精确滑动，而不是盲点坐标点击。

本次日历运行第 13 步只能退化成 ``click_coordinate (410,1410)`` 盲点滚轮项；DC 侧模型自己
算出「每格 108px，9→1 往下 4 格比往上 8 格近」，一次到位。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes import CALENDAR_FIXTURES, element, snapshot_from_layout

from harmony_test_agent.models import (
    CommandResult,
    RunState,
    ScreenSnapshot,
    TargetAppProfile,
    ToolDecision,
    ToolName,
)
from harmony_test_agent.runtime import LaunchSpec, SafetyError, SafetyPolicy, ToolExecutionError, ToolExecutor


class RecordingDevice:
    """记录每次滑动起止点的设备替身。"""

    def __init__(self) -> None:
        self.swipes: list[tuple[tuple[int, int], tuple[int, int]]] = []

    def open_app(self, profile, reset: bool = False) -> CommandResult:  # pragma: no cover - 未使用
        return CommandResult(command="open", returncode=0)

    def click(self, x: int, y: int) -> CommandResult:
        return CommandResult(command="click", returncode=0)

    def input_text(self, text: str, x: int | None = None, y: int | None = None) -> CommandResult:
        return CommandResult(command="input", returncode=0)

    def swipe(self, start: tuple[int, int], end: tuple[int, int], duration: float = 0.5) -> CommandResult:
        self.swipes.append((start, end))
        return CommandResult(command="swipe", returncode=0)

    def back(self) -> CommandResult:
        return CommandResult(command="back", returncode=0)

    def wait(self, seconds: float) -> CommandResult:
        return CommandResult(command="wait", returncode=0)


def _picker_snapshot() -> ScreenSnapshot:
    """时间选择器：小时列每格 110px（参考实测的 108px 格距）。"""
    return ScreenSnapshot(
        snapshot_id="snap-picker",
        run_id="run-picker",
        image_path=Path("picker.png"),
        image_sha256="hash",
        width=1222,
        height=2670,
        page_path="pages/EntryPage",
        elements=[
            element("hour-9", key="picker_hour_9", content="9", bbox=(300, 850, 500, 960), clickable=True),
            element("hour-10", key="picker_hour_10", content="10", bbox=(300, 960, 500, 1070), clickable=True),
            element("hour-11", key="picker_hour_11", content="11", bbox=(300, 1070, 500, 1180), clickable=True),
            element("hour-12", key="picker_hour_12", content="12", bbox=(300, 1180, 500, 1290), clickable=True),
        ],
    )


def _executor(device: RecordingDevice) -> ToolExecutor:
    return ToolExecutor(
        device=device,  # type: ignore[arg-type]
        launch=LaunchSpec.from_kwargs("com.example", "EntryAbility"),
        safety=SafetyPolicy(),
    )


def _swipe(device: RecordingDevice, **kwargs: object) -> None:
    decision = ToolDecision(tool=ToolName.SWIPE, **kwargs)  # type: ignore[arg-type]
    _executor(device).execute("step-swipe", decision, _picker_snapshot())


def test_explicit_start_end_wins() -> None:
    device = RecordingDevice()

    _swipe(device, start=(977, 1150), end=(977, 718), steps=4, direction="up")

    assert device.swipes == [((977, 1150), (977, 718))]


def test_steps_times_row_pitch_is_used_when_start_end_absent() -> None:
    device = RecordingDevice()

    _swipe(device, target="picker_hour_11", direction="down", steps=2)

    (start, end) = device.swipes[0]
    # 格距 110 × 2 格 = 220：锚点中心 y=1125，向下即从 1015 滑到 1235。
    assert end[1] - start[1] == 220
    assert start[0] == end[0] == 400
    assert start[1] < end[1]


def test_direction_semantics_are_preserved_for_up() -> None:
    device = RecordingDevice()

    _swipe(device, target="picker_hour_11", direction="up", steps=1)

    (start, end) = device.swipes[0]
    assert start[1] > end[1]
    assert start[1] - end[1] == 110


def test_explicit_distance_is_used_verbatim() -> None:
    device = RecordingDevice()

    _swipe(device, target="picker_hour_11", direction="up", distance=400)

    (start, end) = device.swipes[0]
    assert start[1] - end[1] == 400


def test_missing_pitch_falls_back_with_warning() -> None:
    device = RecordingDevice()
    snapshot = _picker_snapshot().model_copy(
        update={"elements": [element("only", key="only_row", content="x", bbox=(300, 850, 500, 960))]},
        deep=True,
    )
    decision = ToolDecision(tool=ToolName.SWIPE, target="only_row", direction="up", steps=3)

    result = _executor(device).execute("step-swipe", decision, snapshot)

    assert any("cannot derive wheel row pitch" in item for item in result.warnings)
    assert device.swipes  # 仍然发出一次滑动，而不是失败
    # 回退到历史「锚点 1/4 行程 × 格数」，而不是凭空的半锚点高
    # （实测中模型把整页容器当锚点时，半锚点高推出了 36px 的空滑动）。
    (start, end) = device.swipes[0]
    expected_travel = 2 * (((110 // 4) * 3) // 2)  # 行程按 half = travel // 2 对称落地
    assert start[1] - end[1] == expected_travel


def test_wheel_column_pitch_is_derived_from_homogeneous_rows() -> None:
    """真实滚轮列：同高同宽的行以固定间距排列 → 格距 = 行距。"""
    rows = [
        element(
            f"row-{index}",
            key=f"picker_row_{index}",
            content=str(index),
            bbox=(500, 800 + index * 108, 700, 908 + index * 108),
            clickable=True,
        )
        for index in range(5)
    ]
    # 每行内部还有同心的文本子节点（真实 dump 常见），不能被当成额外的一行。
    inner = [
        element(
            f"text-{index}",
            content=str(index),
            type_name="Text",
            bbox=(520, 830 + index * 108, 680, 880 + index * 108),
        )
        for index in range(5)
    ]
    snapshot = ScreenSnapshot(
        snapshot_id="snap-wheel",
        run_id="run-wheel",
        image_path=Path("wheel.png"),
        image_sha256="hash",
        width=1222,
        height=2670,
        page_path="pages/EntryPage",
        elements=rows + inner,
    )
    anchor = (500, 800, 700, 908 + 4 * 108)

    assert ToolExecutor._row_pitch(snapshot, anchor) == 108
    decision = ToolDecision(tool=ToolName.SWIPE, target="picker_row_0", direction="down", steps=2)
    start, end = ToolExecutor._swipe_points(snapshot, decision, anchor)
    assert end[1] - start[1] == 216


def test_container_anchor_never_yields_a_degenerate_pitch() -> None:
    """真机回归：模型把整页容器（create_agenda）当滚轮锚点时，不能得出 36px 的空滑动。"""
    snapshot = snapshot_from_layout(CALENDAR_FIXTURES / "calendar-editor-picker.json")
    container = next(item for item in snapshot.elements if item.key == "create_agenda")
    assert container.bbox is not None
    anchor = (container.bbox.left, container.bbox.top, container.bbox.right, container.bbox.bottom)

    pitch = ToolExecutor._row_pitch(snapshot, anchor)
    decision = ToolDecision(tool=ToolName.SWIPE, target="create_agenda", direction="up", steps=1)
    start, end = ToolExecutor._swipe_points(snapshot, decision, anchor)

    travel = start[1] - end[1]
    assert travel >= 90, f"单格行程过小（{travel}px）会退化成空滑动"
    assert pitch is None or pitch >= 90
    # 行程随格数单调增长。
    more = ToolDecision(tool=ToolName.SWIPE, target="create_agenda", direction="up", steps=4)
    start4, end4 = ToolExecutor._swipe_points(snapshot, more, anchor)
    assert (start4[1] - end4[1]) > travel


def test_plain_direction_swipe_keeps_legacy_travel() -> None:
    """未提供 steps/distance 时保持历史行为（锚点 1/4 行程、无锚点全屏 30%）。"""
    device = RecordingDevice()

    _swipe(device, target="picker_hour_11", direction="up")

    (start, end) = device.swipes[0]
    assert start[1] - end[1] == 54  # 2 × ((bottom - top) // 4) = 2 × 27

    device = RecordingDevice()
    _swipe(device, direction="up")
    (start, end) = device.swipes[0]
    assert start == (611, 2136) and end == (611, 534)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ((0, 0), (5000, 5000)),
        ((-1, 10), (10, 10)),
        ((10, 10), (10, 9000)),
    ],
)
def test_out_of_bounds_start_end_is_rejected(start: tuple[int, int], end: tuple[int, int]) -> None:
    device = RecordingDevice()

    with pytest.raises(SafetyError):
        _swipe(device, start=start, end=end)

    assert device.swipes == []


def test_swipe_without_snapshot_still_fails_cleanly() -> None:
    device = RecordingDevice()
    decision = ToolDecision(tool=ToolName.SWIPE, direction="up")

    with pytest.raises(ToolExecutionError) as excinfo:
        _executor(device).execute("step-swipe", decision, None)

    assert excinfo.value.state == RunState.FAILED_ELEMENT


def test_generator_renders_slide_for_trace_swipe(tmp_path: Path) -> None:
    """生成器侧：带显式起止坐标的 swipe 必须渲染 driver.slide，丢精度只写方向是不可接受的。"""
    from harmony_test_agent.generation import HypiumGenerator
    from harmony_test_agent.models import ActionResult, AssertionResult, LocatorCandidate, LocatorKind, RunTrace
    from harmony_test_agent.storage import ArtifactStore

    trace = RunTrace(
        run_id="run-slide",
        target_app_id="com-example-calendar",
        task="设置下午1点",
        device_id="dev-1",
        actions=[
            ActionResult(step_id="open", tool=ToolName.OPEN_APP, success=True),
            ActionResult(
                step_id="swipe-precise",
                tool=ToolName.SWIPE,
                success=True,
                params={"direction": "up", "start": [977, 1150], "end": [977, 718], "target": "picker_hour_11"},
            ),
            ActionResult(
                step_id="swipe-direction",
                tool=ToolName.SWIPE,
                success=True,
                params={"direction": "up"},
            ),
            ActionResult(
                step_id="assert",
                tool=ToolName.ASSERT_VISIBLE,
                success=True,
                params={"target": "下午01:00"},
                locator=LocatorCandidate(kind=LocatorKind.KEY, value="add_agenda_detail"),
                assertion=AssertionResult(kind=ToolName.ASSERT_VISIBLE, target="下午01:00", passed=True, message="ok"),
            ),
            ActionResult(step_id="finish", tool=ToolName.FINISH, success=True),
        ],
    )
    profile = TargetAppProfile(
        target_app_id="com-example-calendar",
        display_name="日历",
        bundle_name="com.example.calendar",
        main_ability="EntryAbility",
    )

    generated = HypiumGenerator(ArtifactStore(tmp_path / "runs")).generate(trace, profile)
    source = generated.python_path.read_text(encoding="utf-8")

    assert "driver.slide((977, 1150), (977, 718))" in source
    assert "driver.swipe('UP')" in source
    compile(source, str(generated.python_path), "exec")
