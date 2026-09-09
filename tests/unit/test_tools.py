from pathlib import Path

from harmony_test_agent.models import ScreenSnapshot, TargetAppProfile, ToolDecision, ToolName, UIElement
from harmony_test_agent.runtime import SafetyPolicy, ToolExecutor


def make_executor() -> ToolExecutor:
    profile = TargetAppProfile(
        target_app_id="test",
        display_name="Test",
        bundle_name="com.example.test",
        main_ability="EntryAbility",
    )
    return ToolExecutor(device=None, profile=profile, safety=SafetyPolicy())  # type: ignore[arg-type]


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
