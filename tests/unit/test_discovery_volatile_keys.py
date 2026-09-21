"""Phase 2 验收基石：真实日历 dump 证明页面身份跨时刻稳定（计划 R7 / R8）。

fixture 取自 ``artifacts/runs/run-20260921T005442Z-1f7bb32e/layouts/``：

- ``calendar-home-t0.json`` ← ``discovery-000_snap-be64eb3999be.json``
- ``calendar-home-t1.json`` ← ``discovery-003_snap-b2abef792123.json``

两份 dump 是同一状态在不同时刻的采集：元素数相同（57）、结构骨架完全相同，只有
日期格/农历/时钟（``TimeView_Text_timeText``）与 agenda 实例 ID 变化。

修复前 ``_structural_features`` 只折叠 ``\\d{4,}``、且 KEY/ID 从不做时间型过滤，两份的身份
**不相等**；``test_legacy_folding_rules_would_be_unstable`` 用同一套旧规则复算并断言这一点，
保证这份 fixture 真的覆盖了该 bug，而不是碰巧通过。
"""

from __future__ import annotations

import hashlib
import json
import re

from fakes import CALENDAR_FIXTURES, calendar_foreground, snapshot_from_layout

from harmony_test_agent.discovery import BoundedExplorer
from harmony_test_agent.models import ScreenSnapshot

T0 = CALENDAR_FIXTURES / "calendar-home-t0.json"
T1 = CALENDAR_FIXTURES / "calendar-home-t1.json"

#: 修复前的折叠/过滤规则（仅用于证明 fixture 的可复现性）。
_LEGACY_IDENTITY_KEY_ID_PATTERN = re.compile(r"\d+")
_LEGACY_CONTENT_LIKE_KEY_ID_PATTERN = re.compile(r"\d{4,}")
_LEGACY_CONTENT_STREAM_KEY_PATTERN = re.compile(r"feed|card|banner|recommend|article|answer|video", re.IGNORECASE)


def legacy_structural_identity(snapshot: ScreenSnapshot) -> str:
    """修复前 ``_structural_identity`` 的等价实现。"""
    foreground = calendar_foreground()
    keys = {
        _LEGACY_IDENTITY_KEY_ID_PATTERN.sub("#", item.key or item.id)
        for item in snapshot.elements
        if (item.key or item.id)
        and not _LEGACY_CONTENT_LIKE_KEY_ID_PATTERN.search(item.key or item.id)
        and not _LEGACY_CONTENT_STREAM_KEY_PATTERN.search(item.key or item.id)
    }
    _, interactive = BoundedExplorer._structural_features(snapshot)
    raw = json.dumps(
        [
            snapshot.page_path,
            foreground.bundle_name,
            foreground.window_type,
            sorted(keys),
            sorted(interactive),
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_legacy_folding_rules_would_be_unstable() -> None:
    """可复现性证明：旧规则下这两份 dump 的身份不相等（这就是 R7 的 bug）。"""
    first = snapshot_from_layout(T0)
    second = snapshot_from_layout(T1)

    assert len(first.elements) == len(second.elements)
    assert legacy_structural_identity(first) != legacy_structural_identity(second)


def test_structural_identity_is_stable_across_calendar_moments() -> None:
    foreground = calendar_foreground()
    first = snapshot_from_layout(T0)
    second = snapshot_from_layout(T1)

    assert BoundedExplorer._structural_identity(first, foreground) == BoundedExplorer._structural_identity(
        second, foreground
    )
    # 整树签名仍会抖动：这正是身份必须与整树签名分开的原因。
    assert BoundedExplorer._snapshot_signature(first, foreground) != BoundedExplorer._snapshot_signature(
        second, foreground
    )


def test_volatile_keys_are_excluded_from_identity() -> None:
    snapshot = snapshot_from_layout(T0)
    keys, volatile = BoundedExplorer._identity_key_set(snapshot.elements)

    assert "TimeView_Text_timeText" in volatile  # 状态栏/头部实时时钟
    assert "normal_agenda_list_item193" in volatile  # 列表项实例 ID
    assert "main_page_date_info" in volatile
    assert "month_view_date" in volatile
    assert "month_view_date_banner" in volatile
    assert "tabs_day" in volatile
    # 日期格（``22___十二_`` / ``23_秋分_秋分_十三_`` 这类）随农历与节假日变化。
    assert sum(1 for item in volatile if re.match(r"^\d{1,2}_", item)) >= 5

    assert not set(volatile) & keys
    for probe in ("TimeView_Text_timeText", "normal_agenda_list_item193", "main_page_date_info", "tabs_day"):
        assert probe not in keys


def test_structural_keys_survive_filtering() -> None:
    """不能过滤过度：真正的结构骨架 key 必须留在身份里。"""
    first = snapshot_from_layout(T0)
    second = snapshot_from_layout(T1)
    first_keys, _ = BoundedExplorer._identity_key_set(first.elements)
    second_keys, _ = BoundedExplorer._identity_key_set(second.elements)

    for probe in (
        "tabs_month",
        "tabs_week",
        "tabs_year",
        "phone_add_agenda",
        "month_view",
        "agenda_list",
        "calendar_hds_sidebar",
        "month_view_swiper",
    ):
        assert probe in first_keys, probe
        assert probe in second_keys, probe

    assert first_keys == second_keys
    assert len(first_keys) >= 25


def test_discovery_page_records_volatile_keys_and_state_kind() -> None:
    snapshot = snapshot_from_layout(T0)
    foreground = calendar_foreground()
    page = BoundedExplorer._page(snapshot, foreground, 1)

    assert page.structural_identity == BoundedExplorer._structural_identity(snapshot, foreground)
    assert page.volatile_keys
    assert "TimeView_Text_timeText" in page.volatile_keys
    assert page.state_kind == "page"
    keys, _ = BoundedExplorer._identity_key_set(snapshot.elements)
    assert page.identity_keys == sorted(keys)
    assert not set(page.volatile_keys) & set(page.identity_keys)

    restored = type(page).model_validate(page.model_dump(mode="json"))
    assert restored.volatile_keys == page.volatile_keys
    assert restored.state_kind == page.state_kind
