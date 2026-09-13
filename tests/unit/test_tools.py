from pathlib import Path

from harmony_test_agent.models import (
    BoundingBox,
    CommandResult,
    ScreenSnapshot,
    ToolDecision,
    ToolName,
    UIElement,
)
from harmony_test_agent.runtime import LaunchSpec, SafetyPolicy, ToolExecutor


class RecordingDevice:
    """记录点击/滑动坐标的假设备。"""

    def __init__(self) -> None:
        self.clicks: list[tuple[int, int]] = []
        self.swipes: list[tuple[tuple[int, int], tuple[int, int]]] = []

    def click(self, x: int, y: int) -> CommandResult:
        self.clicks.append((x, y))
        return CommandResult(command="click", returncode=0)

    def swipe(self, start: tuple[int, int], end: tuple[int, int], duration: float = 0.5) -> CommandResult:
        self.swipes.append((start, end))
        return CommandResult(command="swipe", returncode=0)


def make_executor(device: RecordingDevice | None = None) -> ToolExecutor:
    device = device or RecordingDevice()
    launch = LaunchSpec(bundle_name="com.example.test", main_ability="EntryAbility")
    return ToolExecutor(device=device, launch=launch, safety=SafetyPolicy())  # type: ignore[arg-type]


def make_snapshot(tmp_path: Path, *, summary: str = "", elements: list[UIElement] | None = None) -> ScreenSnapshot:
    return ScreenSnapshot(
        snapshot_id="snapshot",
        run_id="run",
        image_path=tmp_path / "screen.png",
        image_sha256="hash",
        width=100,
        height=200,
        page_title="",
        summary=summary,
        elements=elements or [],
    )


def test_assert_visible_removes_generic_content_suffix(tmp_path: Path) -> None:
    snapshot = make_snapshot(
        tmp_path,
        elements=[UIElement(element_id="home", content="首页", enabled=True)],
    )

    assertion, _ = make_executor()._assert(
        ToolDecision(tool=ToolName.ASSERT_VISIBLE, target="首页内容"),
        snapshot,
    )

    assert assertion.passed
    assert "UI element" in assertion.message


def test_assert_visible_can_use_vlm_page_summary(tmp_path: Path) -> None:
    snapshot = make_snapshot(tmp_path, summary="当前显示知乎内容详情页面，正文清晰可见。")

    assertion, locator = make_executor()._assert(
        ToolDecision(tool=ToolName.ASSERT_VISIBLE, target="详情内容"),
        snapshot,
    )

    assert assertion.passed
    assert locator is None
    assert "page summary" in assertion.message


def test_assert_text_prefers_expected_text_over_input_target(tmp_path: Path) -> None:
    snapshot = make_snapshot(
        tmp_path,
        elements=[UIElement(element_id="input", content="OpenHarmony", editable=True, enabled=True)],
    )

    assertion, _ = make_executor()._assert(
        ToolDecision(tool=ToolName.ASSERT_TEXT, target="搜索输入框", text="OpenHarmony"),
        snapshot,
    )

    assert assertion.passed
    assert assertion.target == "OpenHarmony"


def test_click_element_falls_back_to_nonclickable_wheel_item(tmp_path: Path) -> None:
    """滚轮选择器项（如时间选择器"下午"列）常不带 clickable 标志：回退按边界中心点击并留痕。"""
    snapshot = make_snapshot(
        tmp_path,
        elements=[
            UIElement(
                element_id="pm",
                content="下午",
                type="Column",
                clickable=False,
                enabled=True,
                bbox=BoundingBox(left=766, top=744, right=907, bottom=1344),
            ),
        ],
    )
    device = RecordingDevice()
    executor = make_executor(device)

    result = executor.execute(
        "s13",
        ToolDecision(tool=ToolName.CLICK_ELEMENT, target="pm"),
        snapshot,
    )

    assert result.success is True
    assert "non-clickable" in " ".join(result.warnings or [])
    assert device.clicks == [((766 + 907) // 2, (744 + 1344) // 2)]


def test_click_element_prefers_clickable_over_nonclickable_match(tmp_path: Path) -> None:
    snapshot = make_snapshot(
        tmp_path,
        elements=[
            UIElement(
                element_id="am",
                content="上午",
                type="Text",
                clickable=True,
                enabled=True,
                bbox=BoundingBox(left=766, top=852, right=907, bottom=960),
            ),
            UIElement(
                element_id="pm",
                content="下午",
                type="Column",
                clickable=False,
                enabled=True,
                bbox=BoundingBox(left=766, top=744, right=907, bottom=1344),
            ),
        ],
    )
    device = RecordingDevice()
    executor = make_executor(device)

    result = executor.execute(
        "s1",
        ToolDecision(tool=ToolName.CLICK_ELEMENT, target="am"),
        snapshot,
    )

    assert result.success is True
    assert result.warnings in (None, [])
    assert device.clicks == [((766 + 907) // 2, (852 + 960) // 2)]


def test_click_element_reports_missing_target(tmp_path: Path) -> None:
    import pytest

    from harmony_test_agent.runtime import ToolExecutionError

    snapshot = make_snapshot(tmp_path, elements=[UIElement(element_id="other", content="别的", clickable=True)])
    device = RecordingDevice()
    executor = make_executor(device)

    with pytest.raises(ToolExecutionError, match="element not found: ui-missing"):
        executor.execute(
            "s1",
            ToolDecision(tool=ToolName.CLICK_ELEMENT, target="ui-missing"),
            snapshot,
        )
    assert device.clicks == []


def test_swipe_anchors_on_target_wheel_column(tmp_path: Path) -> None:
    """带 target 的滑动以元素 bbox 中心为轴、行程为元素尺寸一半；不要求 clickable 标志。"""
    snapshot = make_snapshot(
        tmp_path,
        elements=[
            UIElement(
                element_id="hour-wheel",
                content="[n2]5",
                type="Column",
                clickable=False,
                enabled=True,
                bbox=BoundingBox(left=560, top=744, right=760, bottom=1344),
            ),
        ],
    )
    device = RecordingDevice()
    executor = make_executor(device)

    result = executor.execute(
        "s14",
        ToolDecision(tool=ToolName.SWIPE, direction="up", target="hour-wheel"),
        snapshot,
    )

    assert result.success is True
    cx, cy = (560 + 760) // 2, (744 + 1344) // 2
    dy = (1344 - 744) // 4
    assert device.swipes == [((cx, cy + dy), (cx, cy - dy))]


def test_swipe_without_target_uses_screen_center(tmp_path: Path) -> None:
    snapshot = make_snapshot(tmp_path, elements=[UIElement(element_id="text", content="无定位内容")])
    device = RecordingDevice()
    executor = make_executor(device)

    result = executor.execute(
        "s1",
        ToolDecision(tool=ToolName.SWIPE, direction="up", target="ui-not-present"),
        snapshot,
    )

    assert result.success is True
    w, h = snapshot.width, snapshot.height
    assert device.swipes == [((w // 2, int(h * 0.8)), (w // 2, int(h * 0.2)))]
    assert any("not found" in warning for warning in (result.warnings or []))
