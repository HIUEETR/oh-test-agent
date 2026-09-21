"""将 HDC UI 层级转换为统一元素模型，并提供语义元素匹配。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import Any

from ..models import BoundingBox, LocatorCandidate, LocatorKind, StableLocator, UIElement

SYSTEM_NODE_PREFIXES = (
    "StatusBar",
    "Battery",
    "Signal",
    "Clock",
    "LiveMeta",
    "[Live]",
    "Keyboard",
    "keyboard",
    "inputMethod",
    "sbg_",
    "session",
)


def walk_nodes(root: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """按原始子节点顺序深度遍历 UI 层级。"""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.get("children") or []))


def parse_bounds(value: str | None) -> BoundingBox | None:
    """解析 HDC bounds 字符串，并过滤无效或退化的矩形。"""
    if not value:
        return None
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", value.strip())
    if not match:
        return None
    left, top, right, bottom = (int(item) for item in match.groups())
    if right <= left or bottom <= top:
        return None
    return BoundingBox(left=left, top=top, right=right, bottom=bottom)


def page_path(layout: dict[str, Any]) -> str:
    """从层级中提取首个可用页面路径。"""
    for node in walk_nodes(layout):
        value = (node.get("attributes") or {}).get("pagePath")
        if value:
            return str(value)
    return "unknown"


def normalize_layout(layout: dict[str, Any], width: int, height: int) -> list[UIElement]:
    """把可见且可识别的层级节点转换为去重后的统一 UI 元素。"""
    elements: list[UIElement] = []
    seen: set[str] = set()
    for index, node in enumerate(walk_nodes(layout)):
        attrs = node.get("attributes") or {}
        if attrs.get("visible", "true") != "true" or attrs.get("enabled", "true") == "false":
            continue
        key = str(attrs.get("key") or "")
        item_id = str(attrs.get("id") or "")
        text = str(attrs.get("text") or attrs.get("originalText") or "")
        description = str(attrs.get("description") or "")
        type_name = str(attrs.get("type") or "")
        identity = key or item_id
        if identity.startswith(SYSTEM_NODE_PREFIXES):
            continue
        clickable = attrs.get("clickable") == "true"
        scrollable = attrs.get("scrollable") == "true"
        editable = type_name in {"TextInput", "Search", "Input", "TextArea"} or "input" in type_name.lower()
        if not any((key, item_id, text, description, clickable, scrollable, editable)):
            continue
        bbox = parse_bounds(attrs.get("bounds"))
        if bbox and not bbox.within(width, height):
            bbox = None
        raw_identity = "|".join((key, item_id, text, type_name, str(index)))
        element_id = "ui-" + hashlib.sha1(raw_identity.encode("utf-8")).hexdigest()[:12]
        if element_id in seen:
            continue
        seen.add(element_id)
        candidates: list[LocatorCandidate] = []
        if key:
            candidates.append(LocatorCandidate(kind=LocatorKind.KEY, value=key, score=1))
        if item_id and item_id != key:
            candidates.append(LocatorCandidate(kind=LocatorKind.ID, value=item_id, score=0.98))
        if text:
            candidates.append(LocatorCandidate(kind=LocatorKind.TEXT, value=text, score=0.9))
        if type_name and text:
            candidates.append(LocatorCandidate(kind=LocatorKind.TYPE_TEXT, value=f"{type_name}|{text}", score=0.85))
        elements.append(
            UIElement(
                element_id=element_id,
                content=text or description or key or item_id,
                type=type_name,
                bbox=bbox,
                key=key,
                id=item_id,
                description=description,
                clickable=clickable,
                editable=editable,
                scrollable=scrollable,
                selected=attrs.get("selected") == "true",
                source="hdc_uitest_dumpLayout",
                locator_candidates=candidates,
                metadata={"page_path": attrs.get("pagePath", ""), "hierarchy": attrs.get("hierarchy", "")},
            )
        )
    return elements


def _to_halfwidth(value: str) -> str:
    """全角 ASCII 与常见中文标点折叠为半角/空格（``：``→``:``、``１``→``1``）。"""
    translated = value.translate({code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)})
    translated = translated.replace("\u3000", " ")
    return _CJK_PUNCTUATION_PATTERN.sub(" ", translated)


_CJK_PUNCTUATION_PATTERN = re.compile(r"[，。、；！？（）【】「」『』《》〈〉·•…—～]")
#: 时间/日期归一之后再折叠的标点（``:`` 与 ``-`` 是归一化形式的一部分，必须保留）。
_PUNCTUATION_TO_SPACE_PATTERN = re.compile(r"[,;!?()\[\]{}\"'`~@#$%^&*_+=|\\<>/]")
_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_NUMBER_PATTERN = re.compile(r"[零〇一二两三四五六七八九十]{1,3}")
_TIME_PATTERN = re.compile(
    r"(?P<meridiem>上午|下午|凌晨|中午|晚上|早上|am|pm)?\s*"
    r"(?P<hour>\d{1,2})\s*(?P<sep>[:时点])\s*(?P<minute>\d{1,2})?\s*分?",
    re.IGNORECASE,
)
_FULL_DATE_PATTERN = re.compile(r"(?P<year>\d{4})\s*[-/年]\s*(?P<month>\d{1,2})\s*[-/月]\s*(?P<day>\d{1,2})\s*日?")
_MONTH_DAY_PATTERN = re.compile(r"(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日")
_SLASH_DATE_PATTERN = re.compile(r"(?<!\d)(?P<month>\d{1,2})\s*/\s*(?P<day>\d{1,2})(?!\d)")

_PM_MARKERS = ("下午", "晚上", "中午", "pm")
_AM_MARKERS = ("上午", "早上", "凌晨", "am")


def _cn_number(token: str) -> int | None:
    """把 1-3 个中文数字字符转换为整数（``十``/``十一``/``二十三``）。"""
    if not token:
        return None
    if "十" not in token:
        digits = [_CN_DIGITS.get(char) for char in token]
        if any(item is None for item in digits):
            return None
        return int("".join(str(item) for item in digits))
    head, _, tail = token.partition("十")
    left = _CN_DIGITS.get(head, 1) if head else 1
    if left is None:
        return None
    if not tail:
        return left * 10
    right = _CN_DIGITS.get(tail)
    return left * 10 + right if right is not None else None


def _normalize_chinese_numbers(value: str) -> str:
    """中文数字 → 阿拉伯数字（至少覆盖 1-12 与「十」组合）。"""

    def replace(match: re.Match[str]) -> str:
        number = _cn_number(match.group(0))
        return str(number) if number is not None else match.group(0)

    return _CN_NUMBER_PATTERN.sub(replace, value)


def _normalize_time(value: str) -> str:
    """时间归一：``下午1:00`` / ``下午01:00`` / ``下午 1 点`` / ``13:00`` → ``pm 1:00``。"""

    def replace(match: re.Match[str]) -> str:
        marker = (match.group("meridiem") or "").casefold()
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or 0)
        if marker in _PM_MARKERS:
            meridiem = "pm"
        elif marker in _AM_MARKERS:
            meridiem = "am"
        elif hour >= 12:
            meridiem = "pm"
        else:
            meridiem = ""
        if meridiem == "pm" and hour > 12:
            hour -= 12
        if meridiem == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            return match.group(0)
        prefix = f"{meridiem} " if meridiem else ""
        return f"{prefix}{hour}:{minute:02d}"

    return _TIME_PATTERN.sub(replace, value)


def _normalize_date(value: str) -> str:
    """日期归一：``9月22日`` / ``2026-09-22`` / ``09/22`` → 同一形式 ``09-22``。"""

    def full(match: re.Match[str]) -> str:
        return f"{int(match.group('month')):02d}-{int(match.group('day')):02d}"

    value = _FULL_DATE_PATTERN.sub(full, value)
    value = _MONTH_DAY_PATTERN.sub(full, value)
    return _SLASH_DATE_PATTERN.sub(full, value)


def normalize_ui_text(value: str) -> str:
    """UI 文本归一化：全半角、空白与标点、时间补零、中文数字与日期形式统一。

    纯函数：``evaluate_assertion``（Live 与 DC 的唯一断言入口）与元素匹配共用它，
    一处修两边受益。用途是**比较**，不做展示，因此统一 casefold 为小写。
    """
    text = _to_halfwidth(value or "")
    text = _normalize_chinese_numbers(text)
    text = _normalize_time(text)
    text = _normalize_date(text)
    text = _PUNCTUATION_TO_SPACE_PATTERN.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def _match_score(element: UIElement, target: str) -> float:
    needle = target.casefold().strip()
    if not needle:
        return 0
    values = (element.element_id, element.key, element.id, element.content, element.description)
    if any(value.casefold() == needle for value in values if value):
        return 1
    # 归一化命中（计划 6.1）：低于精确 1.0、高于前缀 0.92，避免盖过真正的精确匹配。
    normalized_needle = normalize_ui_text(target)
    if normalized_needle:
        normalized_values = [normalize_ui_text(value) for value in values if value]
        if any(value == normalized_needle for value in normalized_values):
            return 0.95
        if any(value.startswith(normalized_needle) for value in normalized_values):
            return 0.9
        if any(normalized_needle in value for value in normalized_values):
            return 0.8
    if any(value.casefold().startswith(needle) for value in values if value):
        return 0.92
    if any(needle in value.casefold() for value in values if value):
        return 0.82
    aliases = {
        "搜索": ("search", "p2_home_titlebar_search"),
        "输入框": ("input", "textinput", "p2_search_input"),
        "详情": ("feed_card", "detail", "answer"),
        "内容": ("feed_card", "detail", "answer"),
    }
    joined = " ".join(values).casefold()
    return 0.75 if any(alias in joined for alias in aliases.get(needle, ())) else 0


def target_variants(target: str) -> list[str]:
    """展开目标描述中的同义短语和通用 UI 后缀，供语义匹配使用。"""
    normalized = target.strip()
    if not normalized:
        return []

    variants = [normalized]
    variants.extend(part.strip() for part in re.split(r"[/|、]|或", normalized) if part.strip())

    semantic_phrases = ("搜索结果", "搜索", "输入框", "首页", "内容详情", "详情", "内容")
    if "搜索框" in normalized or "输入" in normalized:
        variants.append("输入框")
    variants.extend(phrase for phrase in semantic_phrases if phrase in normalized)

    generic_terms = (
        "界面",
        "元素",
        "页面",
        "区域",
        "列表项",
        "列表",
        "条目",
        "卡片",
        "图标",
        "按钮",
        "中的",
        "一个",
        "一条",
        "第一条",
    )
    queue = list(variants)
    while queue:
        value = queue.pop(0)
        for term in generic_terms:
            simplified = value.replace(term, "").strip()
            if simplified and simplified != value and simplified not in variants:
                variants.append(simplified)
                queue.append(simplified)
    # 归一化变体（计划 6.1）：``下午1:00`` 与屏幕上的 ``下午01:00`` 归一后是同一形式。
    normalized = normalize_ui_text(normalized)
    if normalized:
        variants.append(normalized)
    return list(dict.fromkeys(variants))


def find_element(
    elements: list[UIElement],
    target: str,
    *,
    stable_locators: list[StableLocator] | None = None,
    clickable: bool | None = None,
    editable: bool | None = None,
) -> tuple[UIElement, LocatorCandidate] | None:
    """按可点击、可编辑约束和定位器得分选择最匹配的元素。"""
    locator_values = target_variants(target)
    for locator in stable_locators or []:
        locator_name = locator.name.casefold()
        if any(value.casefold() == locator_name or value.casefold() in locator_name for value in locator_values):
            locator_values.extend(value for value in (locator.key, locator.id, locator.text) if value)
    ranked: list[tuple[float, UIElement]] = []
    for element in elements:
        if clickable is True and not element.clickable:
            continue
        if editable is True and not element.editable:
            continue
        score = max((_match_score(element, value) for value in locator_values), default=0)
        if score:
            ranked.append((score, element))
    if not ranked:
        return None
    score, element = max(ranked, key=lambda item: (item[0], bool(item[1].key), bool(item[1].bbox)))
    candidate = next(iter(element.locator_candidates), None)
    return element, candidate or LocatorCandidate(kind=LocatorKind.SPATIAL, value=target, score=score)
