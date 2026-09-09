from pathlib import Path

from harmony_test_agent.generation import HypiumGenerator
from harmony_test_agent.models import (
    ActionResult,
    BoundingBox,
    LocatorCandidate,
    LocatorKind,
    RunTrace,
    ScreenSnapshot,
    TargetAppProfile,
    ToolName,
    UIElement,
)
from harmony_test_agent.storage import ArtifactStore


def test_generator_emits_hypium_python_and_json(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "runs")
    trace = RunTrace(
        run_id="run-test",
        target_app_id="zhihu-plus",
        task="测试搜索",
        device_id="device-1",
        actions=[
            ActionResult(step_id="open", tool=ToolName.OPEN_APP, success=True),
            ActionResult(
                step_id="click",
                tool=ToolName.CLICK_ELEMENT,
                success=True,
                params={"target": "搜索"},
                locator=LocatorCandidate(kind=LocatorKind.KEY, value="search_key"),
            ),
            ActionResult(
                step_id="assert",
                tool=ToolName.ASSERT_VISIBLE,
                success=True,
                params={"target": "搜索"},
                locator=LocatorCandidate(kind=LocatorKind.KEY, value="search_key"),
            ),
        ],
    )
    profile = TargetAppProfile(
        target_app_id="zhihu-plus",
        display_name="知乎++",
        bundle_name="com.example",
        main_ability="EntryAbility",
    )

    generated = HypiumGenerator(store).generate(trace, profile)
    source = generated.python_path.read_text(encoding="utf-8")

    compile(source, str(generated.python_path), "exec")
    assert "UiDriver.connect" in source
    assert "BY.key('search_key')" in source
    assert generated.config_path.exists()
    assert generated.metadata_path.exists()


def test_generator_converts_runtime_spatial_element_to_coordinate(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "runs")
    snapshot = ScreenSnapshot(
        snapshot_id="before-back",
        run_id="run-spatial",
        image_path=tmp_path / "screen.png",
        image_sha256="abc",
        width=1320,
        height=2232,
        elements=[
            UIElement(
                element_id="ui-back",
                content="返回按钮",
                type="Button",
                bbox=BoundingBox(left=48, top=141, right=168, bottom=261),
                clickable=True,
            )
        ],
    )
    trace = RunTrace(
        run_id="run-spatial",
        target_app_id="zhihu-plus",
        task="返回首页",
        device_id="device-1",
        snapshots=[snapshot],
        actions=[
            ActionResult(
                step_id="back",
                tool=ToolName.CLICK_ELEMENT,
                success=True,
                params={"target": "ui-back"},
                before_snapshot_id="before-back",
                locator=LocatorCandidate(kind=LocatorKind.SPATIAL, value="ui-back"),
            )
        ],
    )
    profile = TargetAppProfile(
        target_app_id="zhihu-plus",
        display_name="知乎++",
        bundle_name="com.example",
    )

    generated = HypiumGenerator(store).generate(trace, profile)
    source = generated.python_path.read_text(encoding="utf-8")
    metadata = generated.metadata_path.read_text(encoding="utf-8")

    assert "driver.touch((108, 201))  # coordinate fallback for 'ui-back'" in source
    assert '"coordinate_fallbacks": 1' in metadata
    assert any("runtime element 'ui-back' uses coordinate (108, 201)" in item for item in generated.warnings)
