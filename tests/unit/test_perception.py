from harmony_test_agent.models import (
    BoundingBox,
    ScreenSnapshot,
    UIElement,
    VisionElement,
    VisionObservation,
)
from harmony_test_agent.perception import (
    PerceptionService,
    find_element,
    normalize_layout,
    target_variants,
)


def sample_layout():
    return {
        "attributes": {"pagePath": "pages/Index", "visible": "true"},
        "children": [
            {
                "attributes": {
                    "key": "p2_home_titlebar_search",
                    "id": "p2_home_titlebar_search",
                    "type": "Button",
                    "text": "搜索",
                    "clickable": "true",
                    "visible": "true",
                    "enabled": "true",
                    "bounds": "[100,80][300,160]",
                }
            },
            {
                "attributes": {
                    "key": "StatusBarBridgeView",
                    "type": "__Common__",
                    "clickable": "true",
                    "visible": "true",
                    "bounds": "[0,0][1320,80]",
                }
            },
        ],
    }


def test_normalize_filters_system_nodes_and_finds_search():
    elements = normalize_layout(sample_layout(), 1320, 2232)
    assert len(elements) == 1
    found = find_element(elements, "搜索", clickable=True)
    assert found is not None
    assert found[0].key == "p2_home_titlebar_search"
    assert found[0].bbox.center == (200, 120)


def test_merge_adds_valid_vlm_element(tmp_path):
    image = tmp_path / "screen.png"
    image.write_bytes(b"not-used")
    snapshot = ScreenSnapshot(
        snapshot_id="s1",
        run_id="r1",
        image_path=image,
        image_sha256="abc",
        width=1000,
        height=2000,
    )
    observation = VisionObservation(
        page_title="视觉页面",
        elements=[
            VisionElement(
                content="图标",
                bbox=BoundingBox(left=100, top=100, right=200, bottom=200),
                clickable=True,
                score=0.9,
            )
        ],
    )
    merged = PerceptionService().merge(snapshot, observation)
    assert merged.page_title == "视觉页面"
    assert merged.elements[0].source == "vlm"


def test_merge_rejects_out_of_bounds_vlm_element(tmp_path):
    image = tmp_path / "screen.png"
    image.write_bytes(b"not-used")
    snapshot = ScreenSnapshot(
        snapshot_id="s1",
        run_id="r1",
        image_path=image,
        image_sha256="abc",
        width=100,
        height=100,
    )
    observation = VisionObservation(
        elements=[
            VisionElement(
                content="bad",
                bbox=BoundingBox(left=90, top=90, right=120, bottom=120),
                score=0.9,
            )
        ]
    )
    assert PerceptionService().merge(snapshot, observation).elements == []


def test_target_variants_extract_semantic_locator_names() -> None:
    variants = target_variants("搜索图标/搜索框")

    assert "搜索" in variants
    assert "输入框" in variants
    assert "首页" in target_variants("首页界面元素")
    assert "内容" in target_variants("内容列表中的一条内容")


def test_find_element_accepts_descriptive_search_target() -> None:
    element = UIElement(
        element_id="search",
        key="p2_home_titlebar_search",
        content="搜索",
        clickable=True,
        enabled=True,
    )

    found = find_element([element], "搜索图标/搜索框", clickable=True)

    assert found is not None
    assert found[0].element_id == "search"


def test_find_element_accepts_runtime_element_id() -> None:
    element = UIElement(
        element_id="ui-search",
        key="p2_home_titlebar_search",
        content="搜索",
        clickable=True,
        enabled=True,
    )

    found = find_element([element], "ui-search", clickable=True)

    assert found is not None
    assert found[0] is element
    assert found[1].value == "ui-search"
