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


# ---------------------------------------------------------------------------
# 多窗口归属：鸿蒙把每个窗口输出成一棵带 bundleName 的 root 树，窗口内的控件节点自己
# **不带** bundleName，归属必须靠继承。真机复盘 dc-20260923T180535Z-1bed642e：同一帧里
# 并列着被测应用、桌面与输入法三棵树，index_keyMenu_container 挂在输入法那棵下面。
# ---------------------------------------------------------------------------


def multi_window_layout():
    """照真机形态构造：应用窗口 + 桌面状态栏窗口 + 输入法窗口。"""
    return {
        "attributes": {"bounds": "[0,0][1320,2232]"},
        "children": [
            {
                "attributes": {
                    "type": "root",
                    "bundleName": "com.github.zhuoyi233.zhplus",
                    "pagePath": "pages/Index",
                    "bounds": "[0,117][1320,1290]",
                },
                "children": [
                    {
                        "attributes": {
                            "key": "p2_search_input",
                            "id": "p2_search_input",
                            "type": "TextInput",
                            "text": "zcode",
                            "bounds": "[294,165][1038,237]",
                        },
                        "children": [
                            {
                                "attributes": {
                                    "key": "p2_search_input_inner",
                                    "type": "Row",
                                    "bounds": "[300,170][1000,230]",
                                }
                            }
                        ],
                    }
                ],
            },
            {
                "attributes": {
                    "type": "WindowScene",
                    "key": "session27",
                    "id": "session27",
                    "bundleName": "com.ohos.sceneboard",
                    "bounds": "[0,0][1320,117]",
                }
            },
            {
                "attributes": {
                    "type": "root",
                    "bundleName": "com.huawei.hmos.inputmethod",
                    "bounds": "[0,117][1320,2232]",
                },
                "children": [
                    {
                        "attributes": {
                            "key": "index_keyMenu_container",
                            "id": "index_keyMenu_container",
                            "type": "Stack",
                            "clickable": "false",
                            "bounds": "[0,1443][1320,2097]",
                        }
                    }
                ],
            },
        ],
    }


def test_owner_bundle_is_inherited_from_the_window_root():
    elements = normalize_layout(multi_window_layout(), 1320, 2232)
    owners = {element.key: element.metadata.get("bundle_name") for element in elements}

    assert owners["p2_search_input"] == "com.github.zhuoyi233.zhplus"
    # 嵌套两层的节点同样继承到窗口归属。
    assert owners["p2_search_input_inner"] == "com.github.zhuoyi233.zhplus"
    assert owners["index_keyMenu_container"] == "com.huawei.hmos.inputmethod"


def test_foreign_window_elements_are_still_perceived():
    """归属信息只是**标注**：输入法元素仍要进元素列表，模型需要看得见软键盘。"""
    elements = normalize_layout(multi_window_layout(), 1320, 2232)

    assert "index_keyMenu_container" in {element.key for element in elements}


def test_walk_order_is_identical_to_walk_nodes():
    """遍历顺序必须与 ``walk_nodes`` 完全一致。

    ``normalize_layout`` 用 ``enumerate`` 的下标参与 ``element_id`` 哈希；顺序一变所有
    element_id 都会变，历史帧与录制里的 ``resolved_element`` 就对不上了。
    """
    from harmony_test_agent.perception.normalizer import walk_nodes, walk_nodes_with_bundle

    layout = multi_window_layout()

    assert [id(node) for node in walk_nodes(layout)] == [id(node) for node, _ in walk_nodes_with_bundle(layout)]


def test_element_ids_are_stable_with_bundle_metadata():
    """加入归属信息不得改变 element_id（历史帧与录制里的 ``resolved_element`` 靠它对账）。"""
    layout = multi_window_layout()
    first = {element.key: element.element_id for element in normalize_layout(layout, 1320, 2232)}
    second = {element.key: element.element_id for element in normalize_layout(layout, 1320, 2232)}

    assert first == second
    # element_id 是 (key, id, text, type, index) 的内容哈希：归属信息不参与，
    # 因此带 metadata 与否都必须得到同一个 id。
    assert first["index_keyMenu_container"].startswith("ui-")
    assert first["index_keyMenu_container"] == second["index_keyMenu_container"]


def test_layout_without_bundle_name_yields_empty_owner():
    """没有归属信息时写空串：消费方按「判不了就不判」处理，不得当成外来窗口。"""
    elements = normalize_layout(sample_layout(), 1320, 2232)

    assert elements[0].metadata.get("bundle_name") == ""
