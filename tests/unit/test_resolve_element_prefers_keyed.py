"""Phase 4：命中测试优先返回带 key/id 的元素。

回归背景（真机复盘 dc-20260922T115708Z-f2acffa4）：点击「确定」按钮中心
(948, 1596) 时，按钮 bbox [685,1536,1212,1656] 内部还有一个无 key 的 Text
标签，取最小 bbox 会选中该标签，脚本只能退化成坐标兜底；同一份 dump 里
``add_agenda_comfrim`` 本来是稳定的。因此命中优先级固定为 editable > keyed > 面积最小。

注意：这里**不**过滤「看似易变」的 key（如日期单元格 ``1_国庆节__廿一_休``）——
易变判定属于脚本生成阶段（会带告警），命中测试必须照常返回它。
"""

from __future__ import annotations

from pathlib import Path

from harmony_test_agent.dc.tools import _resolve_element
from harmony_test_agent.models import BoundingBox, ScreenSnapshot, UIElement


def _snapshot(elements: list[UIElement]) -> ScreenSnapshot:
    return ScreenSnapshot(
        snapshot_id="snap-keyed",
        run_id="dc-test",
        image_path=Path("snap.png"),
        image_sha256="sha",
        width=1080,
        height=2232,
        page_path="pages/Agenda",
        elements=elements,
    )


def _boxed(
    element_id: str,
    box: tuple[int, int, int, int],
    *,
    key: str = "",
    element_key_id: str = "",
    editable: bool = False,
    element_type: str = "",
) -> UIElement:
    """带 bbox 的元素替身（复用 test_dc_tools.py 的构造风格）。"""
    left, top, right, bottom = box
    return UIElement(
        element_id=element_id,
        type=element_type,
        key=key,
        id=element_key_id,
        editable=editable,
        clickable=True,
        bbox=BoundingBox(left=left, top=top, right=right, bottom=bottom),
    )


class TestResolveElementPrefersKeyed:
    """editable > keyed > 面积最小；无 key 命中时行为与改动前逐字一致。"""

    def test_regression_button_center_picks_parent_key(self) -> None:
        """真机回归：按钮中心命中内部无 key 的 Text 标签，仍须返回父容器 key。"""
        container = _boxed("container_1", (685, 1536, 1212, 1656), key="add_agenda_comfrim", element_type="__Common__")
        inner_text = _boxed("text_1", (800, 1560, 1000, 1620), element_type="Text")
        snapshot = _snapshot([container, inner_text])
        # 前置条件自检：内层 Text 面积更小，正是改动前会被误选的那个元素。
        assert inner_text.bbox is not None
        assert container.bbox is not None
        assert inner_text.bbox.area < container.bbox.area

        element = _resolve_element(snapshot, 948, 1596)

        assert element is not None
        assert element.key == "add_agenda_comfrim"
        assert element.element_id == "container_1"

    def test_editable_beats_larger_keyed_container(self) -> None:
        """editable 仍然最高优先：小输入框（无 key/id）胜过更大的带 key 容器。"""
        snapshot = _snapshot(
            [
                _boxed("form_container", (0, 0, 600, 600), key="form_root"),
                _boxed("plain_field", (100, 100, 200, 160), editable=True),
            ]
        )

        element = _resolve_element(snapshot, 150, 130)

        assert element is not None
        assert element.element_id == "plain_field"
        assert element.editable is True

    def test_id_also_counts_as_stable_locator(self) -> None:
        """只有 ``id``（key 为空）也算稳定定位器。"""
        snapshot = _snapshot(
            [
                _boxed("container", (0, 0, 600, 600)),
                _boxed("label", (10, 10, 60, 60)),
                _boxed("submit", (20, 20, 300, 300), element_key_id="submit_button"),
            ]
        )

        element = _resolve_element(snapshot, 30, 30)

        assert element is not None
        assert element.element_id == "submit"

    def test_keyed_pool_still_picks_smallest(self) -> None:
        """多个带 key 命中时，仍取其中面积最小者（键比较不改变内层优先）。"""
        snapshot = _snapshot(
            [
                _boxed("outer", (0, 0, 500, 500), key="outer_row"),
                _boxed("inner", (10, 10, 100, 100), key="inner_icon"),
                _boxed("unkeyed", (20, 20, 40, 40)),
            ]
        )

        element = _resolve_element(snapshot, 30, 30)

        assert element is not None
        assert element.element_id == "inner"

    def test_without_any_key_falls_back_to_smallest_bbox(self) -> None:
        """无 key/id 命中时行为不变：返回面积最小的元素（改动前的语义）。"""
        snapshot = _snapshot(
            [
                _boxed("container", (0, 0, 400, 400)),
                _boxed("inner_button", (10, 10, 60, 60)),
            ]
        )

        element = _resolve_element(snapshot, 20, 20)

        assert element is not None
        assert element.element_id == "inner_button"

    def test_volatile_looking_key_is_still_returned(self) -> None:
        """易变 key 不在命中测试里过滤（留给脚本生成阶段告警），必须照常返回。"""
        snapshot = _snapshot(
            [
                _boxed("cell_container", (0, 0, 400, 400)),
                _boxed("date_cell", (10, 10, 120, 120), key="1_国庆节__廿一_休"),
            ]
        )

        element = _resolve_element(snapshot, 30, 30)

        assert element is not None
        assert element.key == "1_国庆节__廿一_休"

    def test_point_outside_every_bbox_returns_none(self) -> None:
        snapshot = _snapshot([_boxed("container", (685, 1536, 1212, 1656), key="add_agenda_comfrim")])

        assert _resolve_element(snapshot, 10, 10) is None

    def test_none_snapshot_returns_none(self) -> None:
        assert _resolve_element(None, 948, 1596) is None
