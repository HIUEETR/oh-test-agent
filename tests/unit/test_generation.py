import json
from pathlib import Path

from harmony_test_agent.generation import HypiumGenerator
from harmony_test_agent.models import (
    ActionResult,
    BoundingBox,
    LocatorCandidate,
    LocatorKind,
    PageNode,
    RunState,
    RunTrace,
    ScreenSnapshot,
    TargetAppProfile,
    ToolName,
    UIElement,
)
from harmony_test_agent.storage import ArtifactStore


def profile() -> TargetAppProfile:
    return TargetAppProfile(
        target_app_id="zhihu-plus",
        display_name="知乎++",
        bundle_name="com.example",
        main_ability="EntryAbility",
        launch_strategy={"wait_seconds": 3},
    )


def finish_action() -> ActionResult:
    return ActionResult(step_id="finish", tool=ToolName.FINISH, success=True)


def test_generator_emits_acceptance_script_with_fixed_setup(tmp_path: Path) -> None:
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
            finish_action(),
        ],
    )

    generated = HypiumGenerator(store).generate(trace, profile())
    source = generated.python_path.read_text(encoding="utf-8")
    metadata = json.loads(generated.metadata_path.read_text(encoding="utf-8"))

    compile(source, str(generated.python_path), "exec")
    assert source.count("driver.stop_app(BUNDLE_NAME)") == 1
    assert source.count("driver.start_app(BUNDLE_NAME, MAIN_ABILITY)") == 1
    assert "driver.wait(STARTUP_WAIT_SECONDS)" in source
    assert "STARTUP_WAIT_SECONDS = 3.0" in source
    assert "BY.key('search_key')" in source
    assert generated.purpose == "acceptance"
    assert generated.replay_eligible is True
    assert generated.source_action_count == 4
    assert generated.included_action_count == 2
    assert generated.omitted_action_count == 2
    assert metadata["omitted_actions"][0]["tool"] == "open_app"
    assert "deterministic stop/start/wait" in metadata["omitted_actions"][0]["reason"]


def test_generator_converts_spatial_and_vlm_elements_to_coordinates(tmp_path: Path) -> None:
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
                locator=LocatorCandidate(kind=LocatorKind.VLM_BBOX, value="ui-back"),
            ),
            ActionResult(
                step_id="assert",
                tool=ToolName.ASSERT_VISIBLE,
                success=True,
                params={"target": "返回按钮"},
                locator=LocatorCandidate(kind=LocatorKind.TEXT, value="返回按钮"),
            ),
            finish_action(),
        ],
    )

    generated = HypiumGenerator(store).generate(trace, profile())
    source = generated.python_path.read_text(encoding="utf-8")
    metadata = json.loads(generated.metadata_path.read_text(encoding="utf-8"))

    assert "driver.touch((108, 201))  # coordinate fallback for 'ui-back'" in source
    assert metadata["counts"]["coordinate_fallbacks"] == 1
    assert any("runtime element 'ui-back' uses coordinate (108, 201)" in item for item in generated.warnings)


def test_generator_filters_desktop_app_icon_and_marks_incomplete_trace_diagnostic(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "runs")
    snapshot = ScreenSnapshot(
        snapshot_id="desktop",
        run_id="run-diagnostic",
        image_path=tmp_path / "desktop.png",
        image_sha256="abc",
        width=100,
        height=200,
        elements=[UIElement(element_id="target-icon", type="AppIcon", clickable=True)],
    )
    trace = RunTrace(
        run_id="run-diagnostic",
        target_app_id="zhihu-plus",
        task="启动后失败",
        device_id="device-1",
        snapshots=[snapshot],
        actions=[
            ActionResult(
                step_id="desktop-launch",
                tool=ToolName.CLICK_ELEMENT,
                success=True,
                params={"target": "target-icon"},
                before_snapshot_id="desktop",
            ),
            ActionResult(step_id="failed", tool=ToolName.BACK, success=False, error="device disconnected"),
        ],
        error="device disconnected",
    )

    generated = HypiumGenerator(store).generate(trace, profile())
    source = generated.python_path.read_text(encoding="utf-8")
    metadata = json.loads(generated.metadata_path.read_text(encoding="utf-8"))

    assert generated.purpose == "diagnostic"
    assert generated.replay_eligible is False
    assert "source trace contains failed actions" in generated.incomplete_reasons
    assert "target-icon" not in source
    assert metadata["omitted_action_count"] == 2
    assert any("desktop AppIcon" in item["reason"] for item in metadata["omitted_actions"])


def test_page_node_keeps_absolute_image_and_derives_relative_artifact_path(tmp_path: Path) -> None:
    absolute = tmp_path / "run-123" / "screens" / "a.png"
    node = PageNode(
        node_id="page-1",
        signature="sig",
        page_path="pages/Home",
        title="首页",
        snapshot_id="snap-1",
        image_path=absolute,
        discovered_order=1,
        element_count=0,
    )
    assert node.image_path == absolute
    assert node.artifact_path == Path("screens/a.png")
    restored = PageNode.model_validate(node.model_dump(exclude={"artifact_path"}))
    assert restored.artifact_path == Path("screens/a.png")


def test_page_node_falls_back_to_screen_filename_for_external_legacy_path(tmp_path: Path) -> None:
    node = PageNode(
        node_id="page-external",
        signature="sig",
        page_path="pages/Home",
        title="首页",
        snapshot_id="snap-external",
        image_path=tmp_path / "legacy.png",
        discovered_order=1,
        element_count=0,
    )
    assert node.image_path == tmp_path / "legacy.png"
    assert node.artifact_path == Path("screens/legacy.png")


def test_generator_keeps_finished_trace_without_explicit_assertion_diagnostic(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "runs")
    snapshot = ScreenSnapshot(
        snapshot_id="stable",
        run_id="run-no-assertion",
        image_path=tmp_path / "stable.png",
        image_sha256="abc",
        width=100,
        height=200,
        elements=[UIElement(element_id="stable-title", key="stable_title", content="首页")],
    )
    trace = RunTrace(
        run_id="run-no-assertion",
        target_app_id="zhihu-plus",
        task="没有显式断言",
        device_id="device-1",
        state=RunState.COMPLETED,
        agent_outcome="completed",
        snapshots=[snapshot],
        actions=[finish_action()],
    )

    generated = HypiumGenerator(store).generate(trace, profile())
    source = generated.python_path.read_text(encoding="utf-8")

    assert generated.purpose == "diagnostic"
    assert generated.replay_eligible is False
    assert "source trace has no successful explicit assertion" in generated.incomplete_reasons
    assert "driver.check_component_exist(BY.key('stable_title'), expect_exist=True)" in source


def test_legacy_failed_script_trace_preserves_completed_agent_outcome(tmp_path: Path) -> None:
    trace = RunTrace(
        run_id="run-legacy-replay-failure",
        target_app_id="zhihu-plus",
        task="Agent completed but replay failed",
        device_id="device-1",
        state=RunState.FAILED_SCRIPT,
        error="one or more Hypium replay attempts failed",
        actions=[
            ActionResult(
                step_id="assert",
                tool=ToolName.ASSERT_VISIBLE,
                success=True,
                params={"target": "搜索"},
                locator=LocatorCandidate(kind=LocatorKind.TEXT, value="搜索"),
            ),
            finish_action(),
        ],
    )

    generated = HypiumGenerator(ArtifactStore(tmp_path / "runs")).generate(trace, profile())

    assert trace.agent_outcome == "completed"
    assert trace.agent_error is None
    assert generated.replay_eligible is True


def test_generator_filters_desktop_icon_marker_in_key(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "runs")
    snapshot = ScreenSnapshot(
        snapshot_id="desktop-key",
        run_id="run-desktop-key",
        image_path=tmp_path / "desktop.png",
        image_sha256="abc",
        width=100,
        height=200,
        elements=[
            UIElement(
                element_id="launcher-item",
                type="RelativeContainer",
                key="Container_AppIcon_Image_com.exampleEntryAbilityentry0_undefined_0",
                clickable=True,
            )
        ],
    )
    trace = RunTrace(
        run_id="run-desktop-key",
        target_app_id="zhihu-plus",
        task="desktop icon",
        device_id="device-1",
        state=RunState.COMPLETED,
        agent_outcome="completed",
        snapshots=[snapshot],
        actions=[
            ActionResult(
                step_id="launch",
                tool=ToolName.CLICK_ELEMENT,
                success=True,
                params={"target": "launcher-item"},
                before_snapshot_id="desktop-key",
                locator=LocatorCandidate(
                    kind=LocatorKind.KEY,
                    value="Container_AppIcon_Image_com.exampleEntryAbilityentry0_undefined_0",
                ),
            ),
            ActionResult(
                step_id="assert",
                tool=ToolName.ASSERT_VISIBLE,
                success=True,
                params={"target": "搜索"},
                locator=LocatorCandidate(kind=LocatorKind.TEXT, value="搜索"),
            ),
            finish_action(),
        ],
    )

    generated = HypiumGenerator(store).generate(trace, profile())
    source = generated.python_path.read_text(encoding="utf-8")

    assert generated.replay_eligible is True
    assert "Container_AppIcon_Image" not in source
