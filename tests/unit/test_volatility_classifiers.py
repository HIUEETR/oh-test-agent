"""三个「易变 KEY/ID」分类函数的**实测清单**参数化测试（计划 Phase 3.6）。

这张表存在的理由是那 6 条「防误杀」：``is_volatile_evidence_key`` 会把
``add_agenda_start_time`` / ``main_page_date_info`` / ``add_agenda_end_time`` /
``add_agenda_start_time_text`` / ``month_view_date`` / ``month_view_date_banner``
判为易变（它们服务页面身份，这个宽度是对的），而其中前两个正是真机失败脚本里
**正常工作**的稳定骨架 key。脚本定位器路径必须用最窄的
``is_unreplayable_locator_key``：**任何一条防误杀变红都意味着 Phase 3 让脚本更差，
必须停下来收紧 pattern，不得继续。**

所有 key 都取自真机 dump（``artifacts/runs/dc-20260922T115708Z-f2acffa4`` 与
``run-20260921T053514Z-8418044b`` 的复盘记录），不是编造的字符串。
"""

from __future__ import annotations

import pytest

from harmony_test_agent.perception.volatility import (
    is_unreplayable_locator_key,
    is_volatile_evidence_key,
    is_volatile_structural_key,
)

#: ``(key, 定位器判定, 证据回收判定, 页面身份判定)``。
#: 定位器列是本次修复的**契约**；另外两列记录既有宽度，防止有人顺手「统一」它们。
TABLE: tuple[tuple[str, bool, bool, bool], ...] = (
    # --- 必须被定位器拒绝：值本身编码了日期 / 农历 / 节假日 / 时钟读数 / 实例序号 ---
    ("1_国庆节__廿一_休", True, True, True),
    ("22___十二_", True, True, True),
    ("23_秋分_秋分_十三_", True, True, True),
    ("30_中国烈士纪念日__二十_", True, True, True),
    ("27___十七_休", True, True, True),
    ("TimeView_Text_timeText", True, True, True),
    ("normal_agenda_list_item193", True, True, True),
    # --- 必须**不**被定位器拒绝（防误杀）。证据回收列是 True：那是已知且可接受的宽度。 ---
    ("add_agenda_start_time", False, True, True),
    ("add_agenda_end_time", False, True, True),
    ("add_agenda_start_time_text", False, True, True),
    ("main_page_date_info", False, True, True),
    ("month_view_date", False, True, True),
    ("month_view_date_banner", False, True, True),
    # --- 时间戳实例 ID：走前缀泛化，不在这里拒绝 ---
    # 页面身份列是 True：它带 4 位以上数字，属于内容实例 ID（最宽的判定），这也是为什么
    # 三个判定不能互相替代——同一个 key 在三个场景里的正确答案本来就不同。
    ("add_agenda_title-1790078405913", False, False, True),
    # --- 普通稳定 key：三个判定都必须放行 ---
    ("add_agenda_comfrim", False, False, False),
    ("phone_add_agenda", False, False, False),
    ("more_menu", False, False, False),
    ("slide-button", False, False, False),
    ("tabs_month", False, False, False),
    ("agenda_list", False, False, False),
    ("add_agenda_location", False, False, False),
    ("add_agenda_cancel", False, False, False),
    ("add_custom_reminder_row", False, False, False),
    ("p2_home_titlebar_search", False, False, False),
    ("p2_search_input", False, False, False),
)


@pytest.mark.parametrize(("key", "locator", "evidence", "structural"), TABLE, ids=[item[0] for item in TABLE])
def test_classifier_table(key: str, locator: bool, evidence: bool, structural: bool) -> None:
    assert is_unreplayable_locator_key(key) is locator, f"定位器判定漂移：{key!r}"
    assert is_volatile_evidence_key(key) is evidence, f"证据回收判定漂移：{key!r}"
    assert is_volatile_structural_key(key) is structural, f"页面身份判定漂移：{key!r}"


@pytest.mark.parametrize(
    "key",
    [
        "add_agenda_start_time",
        "add_agenda_end_time",
        "add_agenda_start_time_text",
        "main_page_date_info",
        "month_view_date",
        "month_view_date_banner",
    ],
)
def test_locator_classifier_never_rejects_stable_skeleton_keys(key: str) -> None:
    """6 条防误杀的显式断言：这 6 个 key 在真机脚本里正在工作，绝不能退化成坐标。"""
    assert is_unreplayable_locator_key(key) is False
    # 证据回收把它们判为易变是已知且可接受的——这条断言记录「两个场景代价不对称」的事实。
    assert is_volatile_evidence_key(key) is True


@pytest.mark.parametrize("key", ["1_国庆节__廿一_休", "22___十二_", "TimeView_Text_timeText"])
def test_locator_classifier_rejects_keys_that_encode_the_day(key: str) -> None:
    assert is_unreplayable_locator_key(key) is True


def test_empty_key_is_rejected_by_every_classifier() -> None:
    """空 key 无从回放，也与「无定位器」等价。"""
    assert is_unreplayable_locator_key("") is True
    assert is_volatile_evidence_key("") is True
    assert is_volatile_structural_key("") is True
