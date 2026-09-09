from pathlib import Path

from PIL import Image

from harmony_test_agent.graph import PageGraphBuilder
from harmony_test_agent.models import ScreenSnapshot, ToolName, UIElement


def make_snapshot(tmp_path: Path, snapshot_id: str, color: str, key: str) -> ScreenSnapshot:
    image = tmp_path / f"{snapshot_id}.png"
    Image.new("RGB", (200, 400), color).save(image)
    return ScreenSnapshot(
        snapshot_id=snapshot_id,
        run_id="run-test",
        image_path=image,
        image_sha256=color,
        width=200,
        height=400,
        page_path="pages/Index",
        elements=[UIElement(element_id=key, key=key, content=key, enabled=True)],
    )


def test_page_graph_deduplicates_and_connects(tmp_path: Path) -> None:
    builder = PageGraphBuilder()
    first, created = builder.add_snapshot(make_snapshot(tmp_path, "one", "red", "home"))
    duplicate, duplicate_created = builder.add_snapshot(make_snapshot(tmp_path, "one-b", "red", "home"))
    second, second_created = builder.add_snapshot(make_snapshot(tmp_path, "two", "blue", "detail"))
    edge = builder.add_edge(first, second, ToolName.CLICK_ELEMENT, "详情")

    assert created and not duplicate_created and second_created
    assert duplicate.node_id == first.node_id
    assert edge is not None
    assert len(builder.graph.nodes) == 2
