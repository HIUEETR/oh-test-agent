"""「易变 KEY/ID」判定的唯一归属地：三个**刻意不同宽度**的分类函数。

同一个词「易变」在三个场景里的代价是不对称的，因此这里是三个函数而不是一个：

===============================  ==============================  ==============================
函数                              服务对象                        宽窄
===============================  ==============================  ==============================
``is_volatile_structural_key``    页面结构身份（``explorer``）      最宽（含 ``\\d{4,}`` 内容 ID、
                                                                  feed/card 词形）
``is_volatile_evidence_key``      任务期证据回收（``orchestrator``）  中（时间/日期**词形** + 列表实例）
``is_unreplayable_locator_key``   **脚本定位器**（``cases/builder``）  **最窄（只认形态，不认词形）**
===============================  ==============================  ==============================

**为什么不能混用（真机实测反例，务必先读完再图省事复用）**

把 ``is_volatile_evidence_key`` 直接用到脚本生成路径上，会对真实 key 给出这些答案::

    add_agenda_start_time          True   ← 误杀：脚本第 60 行正在用它，且真机回放成功
    main_page_date_info            True   ← 误杀：脚本第 39 行正在用它
    add_agenda_end_time            True   ← 误杀
    add_agenda_start_time_text     True   ← 误杀
    month_view_date                True   ← 误杀
    month_view_date_banner         True   ← 误杀
    TimeView_Text_timeText         True   ✓ 正确（状态栏时钟读数）
    1_国庆节__廿一_休                True   ✓ 正确（日期格）
    normal_agenda_list_item193     True   ✓ 正确（列表实例）

误杀来源是 :data:`_VOLATILE_TIME_KEY_PATTERN` 的
``(?i)(^|_)(time|clock|date|day|...)(_|$)`` 会命中**以 ``_time`` / ``_date`` 结尾或含
``_date_`` 的稳定骨架 key**。这个宽度对**页面身份**是合理的（过度过滤只是让身份变粗，无害，
且 ``tests/unit/test_discovery.py`` 已钉住），对**任务期证据回收**也可以接受（最坏少存一条
定位器）；但对**脚本定位器**是灾难——过度过滤会把正在工作的稳定 key 打成坐标兜底，
脚本质量断崖下降。所以定位器路径必须用最窄的 :func:`is_unreplayable_locator_key`
（只认「值本身编码了日期格 / 农历 / 节假日 / 时钟读数 / 列表实例序号」的**形态**）。

依赖方向：本模块只依赖标准库，是 ``perception`` 包里的叶子模块；
``discovery → perception`` 与 ``cases → perception`` 都无环。
``discovery/explorer.py`` 与 ``discovery/__init__.py`` 保留 re-export，公开导出面不变。
"""

from __future__ import annotations

import re

__all__ = [
    "is_unreplayable_locator_key",
    "is_volatile_evidence_key",
    "is_volatile_structural_key",
]

#: 内容型实例 ID：任何 4 位以上的数字串都可能是每次启动都变的内容序号。
_CONTENT_LIKE_KEY_ID_PATTERN = re.compile(r"\d{4,}")
#: 内容流词形（feed/card/banner/...）：这些前缀的 key 通常属于推荐流条目。
_CONTENT_STREAM_KEY_PATTERN = re.compile(r"feed|card|banner|recommend|article|answer|video", re.IGNORECASE)
#: 时间/日期/节假日型 key：日历类应用的日期格与状态栏时钟每次启动都不同，
#: 进入结构身份会让跨启动比对必然失败（实测 com.huawei.hmos.calendar）。
_VOLATILE_TIME_KEY_PATTERN = re.compile(
    r"(?i)(^|_)(time|clock|date|day|today|tomorrow|yesterday|lunar|jieqi|holiday|festival)(_|$)"
    r"|^\d{1,2}_"  # 22___十二_ / 23_秋分_秋分_十三_ 这类日期格
    r"|timeText$"
)
#: 列表项实例 key：normal_agenda_list_item193 这类带实例序号的内容项。
#: 序号是必需的：``add_custom_reminder_row`` 这类结构行的尾部词形相同，但它是稳定骨架。
_LIST_INSTANCE_KEY_PATTERN = re.compile(r"(?i)_(item|card|cell|row|entry)_?\d+$")


def is_volatile_structural_key(value: str) -> bool:
    """该 KEY/ID 是否因时间/内容实例特征而不应进入结构身份或长期证据库。

    与 ``BoundedExplorer._identity_key_set`` 共用同一组 pattern：页面身份判定与
    任务期证据回收（orchestrator）必须对「什么算易变」给出一致答案（计划 2.2/3.3）。
    """
    if not value:
        return True
    return bool(
        _CONTENT_LIKE_KEY_ID_PATTERN.search(value)
        or _CONTENT_STREAM_KEY_PATTERN.search(value)
        or _VOLATILE_TIME_KEY_PATTERN.search(value)
        or _LIST_INSTANCE_KEY_PATTERN.search(value)
    )


def is_volatile_evidence_key(value: str) -> bool:
    """任务期证据回收专用的易变判定（计划 3.3）：只含时间/日期型与列表实例型 key。

    比 :func:`is_volatile_structural_key` 更窄：带长数字实例 ID 的内容 key
    （``add_agenda_title-1789951623657``）在证据回收里会被折叠成 ``add_agenda_title-#``
    继续复用，而时钟、日期格与列表实例序号每次启动都会变，必须丢弃。

    .. warning::
       **不要**在脚本生成路径上复用它：它会把 ``add_agenda_start_time`` /
       ``main_page_date_info`` / ``month_view_date`` 这类**正在工作的稳定骨架 key** 判为易变
       （实测，见模块 docstring）。脚本定位器请用 :func:`is_unreplayable_locator_key`。
    """
    if not value:
        return True
    return bool(_VOLATILE_TIME_KEY_PATTERN.search(value) or _LIST_INSTANCE_KEY_PATTERN.search(value))


# ---------------------------------------------------------------------------
# 脚本定位器专用（最窄）：只认形态，不认词形
# ---------------------------------------------------------------------------

#: 日历日期格 key：``1_国庆节__廿一_休`` / ``22___十二_`` / ``30_中国烈士纪念日__二十_``。
#: 首位数字就是「几号」，只在当天存在。
_DATE_CELL_KEY = re.compile(r"^\d{1,2}_")
#: 状态栏/表盘时钟读数 key：``TimeView_Text_timeText``。
_CLOCK_READOUT_KEY = re.compile(r"(?i)(timeText|clockText|_(clock|timer)_text)$")
#: 列表实例序号 key：``normal_agenda_list_item193``。与 explorer 同规则。
_LIST_INSTANCE_LOCATOR_KEY = re.compile(r"(?i)_(item|card|cell|row|entry)_?\d+$")
#: 农历/节气/节假日词形：日期格 key 把「几号 + 节日 + 农历 + 休/班」编码在一起。
_LUNAR_FESTIVAL_KEY = re.compile(
    r"(国庆节|中秋节|元旦|春节|清明|劳动节|端午|重阳|除夕|妇女节|儿童节|建党|建军|"
    r"春分|秋分|夏至|冬至|立春|立夏|立秋|立冬|谷雨|白露|寒露|霜降|小暑|大暑|处暑|惊蛰|"
    r"廿[一二三四五六七八九十]?|初[一二三四五六七八九十]|_[休班]$)"
)


def is_unreplayable_locator_key(value: str) -> bool:
    """该 KEY/ID 是否**无法用于回放**（换一天 / 换一次运行必然失效）。

    比 :func:`is_volatile_evidence_key` 窄得多：后者为页面身份与证据回收服务，会把
    ``add_agenda_start_time`` / ``main_page_date_info`` / ``month_view_date`` 这类
    稳定骨架 key 一并判为易变（实测），用于定位器会误杀正在工作的选择器。
    本函数只认「值本身编码了日期/农历/节假日/时钟读数/列表实例序号」的**形态**。

    注意与时间戳实例 ID 的分工：``add_agenda_title-1790078405913`` 这类
    ``<前缀>-<epoch_ms>`` 会被 ``_DYNAMIC_LOCATOR`` 折叠成前缀继续复用，
    **不属于**本函数的拒绝范围（它编码的是实例时间戳，不是可读日期），
    调用方必须先用 ``_DYNAMIC_LOCATOR.fullmatch(...) is None`` 把它排除掉。
    """
    if not value:
        return True
    return bool(
        _DATE_CELL_KEY.search(value)
        or _CLOCK_READOUT_KEY.search(value)
        or _LIST_INSTANCE_LOCATOR_KEY.search(value)
        or _LUNAR_FESTIVAL_KEY.search(value)
    )
