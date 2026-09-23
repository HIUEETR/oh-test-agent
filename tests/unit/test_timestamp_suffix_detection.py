"""``_timestamp_suffix`` 的形态判定（计划 Phase 2.1 / 2.3）。

只认 13 位（毫秒）与 10 位（秒），且必须落在**会话时间窗**内。
窗口约束把「时间戳实例 ID」与「19 位内容 ID」区分开：内容 ID 的前缀
（``p2_channel_content_question_``）会同时匹配多个不同内容项，
盲目 ``STARTS_WITH`` 会点到错误元素。
"""

from __future__ import annotations

import pytest

from harmony_test_agent.cases.builder import _snapshot_window, _timestamp_suffix

#: 真机会话帧时间戳范围（dc-20260922T115708Z-f2acffa4 的 layouts/*.json 文件名 epoch）。
FRAME_FIRST = 1790078264.0
FRAME_LAST = 1790078623.0
WINDOW = (FRAME_FIRST - 3600.0, FRAME_LAST + 3600.0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # 真机 key：13 位毫秒，落在会话窗口内。
        ("add_agenda_title-1790078405913", "add_agenda_title-"),
        # 下划线分隔的 13 位毫秒同样识别。
        ("add_agenda_title_1790078405913", "add_agenda_title_"),
        # 10 位秒。
        ("feed_card_1790078405", "feed_card_"),
        # 19 位内容 ID：不是时间戳形态 → 不泛化。
        ("p2_channel_content_question_2085141629112009975", None),
        # 8 位日期（feed_card_20240101）：不是 13/10 位 → 不泛化（走既有的 unvalidated 分支）。
        ("feed_card_20240101", None),
        # 13 位但落在会话窗口之外（另一个会话的时间戳）→ 不泛化。
        ("add_agenda_title-1790000005913", None),
        # 没有 ``_``/``-`` 分隔符 → 没有可用的前缀。
        ("1790078405913", None),
        # 前缀为空（分隔符在首位）→ 不作为前缀泛化。
        ("-1790078405913", None),
        ("", None),
    ],
)
def test_timestamp_suffix_shape(value: str, expected: str | None) -> None:
    assert _timestamp_suffix(value, WINDOW) == expected


def test_timestamp_suffix_requires_a_session_window() -> None:
    """一帧都没采到时不泛化：无证据不冒险，退回「保留精确值 + 响亮警告」。"""
    assert _timestamp_suffix("add_agenda_title-1790078405913", None) is None


def test_window_bounds_are_inclusive() -> None:
    window = (1790078405.0, 1790078406.0)
    assert _timestamp_suffix("x_1790078405", window) == "x_"
    assert _timestamp_suffix("x_1790078406", window) == "x_"
    assert _timestamp_suffix("x_1790078407", window) is None


def test_snapshot_window_is_derived_from_captured_at_with_margin() -> None:
    """窗口由已采集帧的 ``captured_at`` 推出，两侧各留一小时；无帧返回 ``None``。"""
    from datetime import UTC, datetime
    from pathlib import Path

    from harmony_test_agent.models import ScreenSnapshot

    def snap(epoch: float) -> ScreenSnapshot:
        return ScreenSnapshot(
            snapshot_id=f"snap-{int(epoch)}",
            run_id="run",
            captured_at=datetime.fromtimestamp(epoch, tz=UTC),
            image_path=Path("s.png"),
            image_sha256="x",
            width=100,
            height=200,
        )

    window = _snapshot_window([snap(FRAME_FIRST), snap(FRAME_LAST)])

    assert window == (FRAME_FIRST - 3600.0, FRAME_LAST + 3600.0)
    assert _timestamp_suffix("add_agenda_title-1790078405913", window) == "add_agenda_title-"
    assert _snapshot_window([]) is None
    assert _snapshot_window(None) is None
