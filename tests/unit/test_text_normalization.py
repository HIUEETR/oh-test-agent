"""Phase 6 回归（R11）：断言文本必须归一化后才比较。

真实失败例：计划第 14 步 ``assert_text "下午1:00"``，屏幕实际是「9月22日 下午01:00」，
原实现只有 精确/前缀/子串/别名 四档打分，永远匹配不上，且失败信息只有
``target is not in current screen: 下午1:00``——恢复循环拿不到「屏幕实际是 下午01:00」。
"""

from __future__ import annotations

import pytest
from fakes import CALENDAR_FIXTURES, snapshot_from_layout

from harmony_test_agent.models import BoundingBox, ToolName, UIElement
from harmony_test_agent.perception.normalizer import normalize_ui_text, target_variants
from harmony_test_agent.runtime.tools import closest_screen_texts, evaluate_assertion

TIME_FIXTURE = CALENDAR_FIXTURES / "calendar-time-13-after.json"


@pytest.mark.parametrize(
    "group",
    [
        ("下午1:00", "下午01:00", "下午 1 点", "13:00", "PM 1:00", "下午1点"),
        ("上午9:30", "上午09:30", "am 9:30"),
        ("9月22日", "2026-09-22", "09/22", "9/22"),
        ("十一", "11"),
        ("十二点", "12点"),
    ],
)
def test_normalize_ui_text_collapses_equivalent_forms(group: tuple[str, ...]) -> None:
    normalized = {normalize_ui_text(value) for value in group}

    assert len(normalized) == 1, f"{group} 归一化后应完全一致，实际 {normalized}"


def test_fullwidth_and_punctuation_are_normalized() -> None:
    assert normalize_ui_text("ＡＢＣ：１２３") == "abc:123"
    assert normalize_ui_text("一条内容，详情。") == normalize_ui_text("一条内容 详情")
    assert normalize_ui_text("  多   空格\t换行  ") == "多 空格 换行"


def test_normalization_does_not_merge_unrelated_text() -> None:
    assert normalize_ui_text("下午1:00") != normalize_ui_text("下午2:00")
    assert normalize_ui_text("9月22日") != normalize_ui_text("9月23日")


def test_target_variants_include_normalized_form() -> None:
    variants = target_variants("下午1:00")

    assert "下午1:00" in variants
    assert normalize_ui_text("下午1:00") in variants


def test_real_calendar_fixture_assert_text_now_passes() -> None:
    """真实 fixture 回归：``assert_text '下午1:00'`` 必须命中屏幕上的「下午01:00」。"""
    snapshot = snapshot_from_layout(TIME_FIXTURE)

    result, _ = evaluate_assertion(snapshot, ToolName.ASSERT_TEXT, "下午1:00")

    assert result.passed is True, result.message
    assert "下午01:00" in " ".join(item.content for item in snapshot.elements)


def test_real_calendar_fixture_failure_message_lists_closest_text() -> None:
    """失败时附带最接近的屏幕文案，恢复循环才能一次改对。"""
    snapshot = snapshot_from_layout(TIME_FIXTURE)

    result, _ = evaluate_assertion(snapshot, ToolName.ASSERT_TEXT, "下午3:45")

    assert result.passed is False
    # 既有前缀必须逐字保留（有测试与前端依赖），只在后面追加候选。
    assert result.message.startswith("target is not in current screen: 下午3:45")
    assert "closest:" in result.message
    assert "01:00" in result.message


def test_closest_screen_texts_ranks_by_normalized_similarity() -> None:
    snapshot = snapshot_from_layout(TIME_FIXTURE)

    closest = closest_screen_texts(snapshot, "下午1:00", limit=5)

    assert closest
    assert any("01:00" in item for item in closest)
    assert len(closest) <= 5


def test_evaluate_assertion_still_rejects_absent_text() -> None:
    """归一化放松不得让不存在的文案通过。"""
    snapshot = snapshot_from_layout(TIME_FIXTURE)

    result, _ = evaluate_assertion(snapshot, ToolName.ASSERT_TEXT, "完全不存在的文案")

    assert result.passed is False


def test_visible_assertion_uses_normalized_page_summary() -> None:
    from harmony_test_agent.models import ScreenSnapshot

    snapshot = ScreenSnapshot(
        snapshot_id="s",
        run_id="r",
        image_path=TIME_FIXTURE,
        image_sha256="h",
        width=100,
        height=100,
        page_title="日历",
        summary="已创建日程：9月22日 下午01:00",
        elements=[],
    )

    result, _ = evaluate_assertion(snapshot, ToolName.ASSERT_VISIBLE, "下午1:00")

    assert result.passed is True


def test_find_element_scores_normalized_hit_below_exact() -> None:
    from harmony_test_agent.perception.normalizer import find_element

    exact = UIElement(element_id="e1", content="下午01:00", bbox=BoundingBox(left=0, top=0, right=10, bottom=10))
    other = UIElement(
        element_id="e2", content="9月22日 下午01:00", bbox=BoundingBox(left=0, top=0, right=10, bottom=10)
    )

    exact_hit = find_element([exact], "下午01:00")
    normalized_hit = find_element([other], "下午1:00")

    assert exact_hit is not None and normalized_hit is not None
