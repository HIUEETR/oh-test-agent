from pathlib import Path

from harmony_test_agent.generation import HypiumGenerator
from harmony_test_agent.models import (
    ActionResult,
    LocatorCandidate,
    LocatorKind,
    RunTrace,
    TargetAppProfile,
    ToolName,
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
