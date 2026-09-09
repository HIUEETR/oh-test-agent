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


def _match_score(element: UIElement, target: str) -> float:
    needle = target.casefold().strip()
    if not needle:
        return 0
    values = (element.element_id, element.key, element.id, element.content, element.description)
    if any(value.casefold() == needle for value in values if value):
        return 1
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
