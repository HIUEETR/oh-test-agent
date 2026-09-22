"""``CaseBuilder._prefix_unique_in_snapshots`` 的离线前缀唯一性验证（计划 Phase 2.2）。

零设备零磁盘成本：``BY.key(prefix, STARTS_WITH)`` 在同帧匹配到多个控件时 hypium 取第一个，
可能操作到错误元素——所以「不唯一就不泛化」是硬前置，而不是优化。
"""

from __future__ import annotations

from pathlib import Path

from harmony_test_agent.cases.builder import CaseBuilder
from harmony_test_agent.models import BoundingBox, LocatorKind, ScreenSnapshot, UIElement

PREFIX = "add_agenda_title-"


def element(key: str, *, top: int = 0) -> UIElement:
    return UIElement(
        element_id=key or f"ui-{top}",
        key=key,
        content=key,
        bbox=BoundingBox(left=0, top=top, right=100, bottom=top + 40),
    )


def snapshot(name: str, keys: list[str]) -> ScreenSnapshot:
    return ScreenSnapshot(
        snapshot_id=name,
        run_id="run",
        image_path=Path(f"{name}.png"),
        image_sha256="x",
        width=1320,
        height=2232,
        elements=[element(key, top=index * 50) for index, key in enumerate(keys)],
    )


def test_single_frame_single_hit_is_unique() -> None:
    frames = [snapshot("f1", [f"{PREFIX}1790078405913"])]

    assert CaseBuilder()._prefix_unique_in_snapshots(PREFIX, LocatorKind.KEY, frames) == (True, 1, 1)


def test_two_frames_with_one_hit_each_is_still_unique() -> None:
    frames = [
        snapshot("f1", [f"{PREFIX}1790078405913"]),
        snapshot("f2", [f"{PREFIX}1790078405999"]),
    ]

    assert CaseBuilder()._prefix_unique_in_snapshots(PREFIX, LocatorKind.KEY, frames) == (True, 2, 1)


def test_two_hits_in_one_frame_is_not_unique() -> None:
    frames = [
        snapshot("f1", [f"{PREFIX}1790078405913"]),
        snapshot("f2", [f"{PREFIX}1790078405913", f"{PREFIX}1790078405999"]),
    ]

    assert CaseBuilder()._prefix_unique_in_snapshots(PREFIX, LocatorKind.KEY, frames) == (False, 2, 2)


def test_prefix_absent_from_every_frame_is_not_unique() -> None:
    frames = [snapshot("f1", ["add_agenda_comfrim", "main_page_date_info"])]

    assert CaseBuilder()._prefix_unique_in_snapshots(PREFIX, LocatorKind.KEY, frames) == (False, 0, 0)


def test_missing_snapshots_are_not_unique() -> None:
    builder = CaseBuilder()

    assert builder._prefix_unique_in_snapshots(PREFIX, LocatorKind.KEY, None) == (False, 0, 0)
    assert builder._prefix_unique_in_snapshots(PREFIX, LocatorKind.KEY, []) == (False, 0, 0)
    assert builder._prefix_unique_in_snapshots("", LocatorKind.KEY, [snapshot("f1", [])]) == (False, 0, 0)


def test_id_kind_reads_the_id_attribute() -> None:
    """``kind=ID`` 只看 ``id``：key 命中不算数，反之亦然。"""
    frame = ScreenSnapshot(
        snapshot_id="f1",
        run_id="run",
        image_path=Path("f1.png"),
        image_sha256="x",
        width=1320,
        height=2232,
        elements=[UIElement(element_id="ui-1", id=f"{PREFIX}1790078405913", key="other_key")],
    )

    assert CaseBuilder()._prefix_unique_in_snapshots(PREFIX, LocatorKind.KEY, [frame]) == (False, 0, 0)
    assert CaseBuilder()._prefix_unique_in_snapshots(PREFIX, LocatorKind.ID, [frame]) == (True, 1, 1)
