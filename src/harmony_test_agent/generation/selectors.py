"""``LocatorSpec`` → hypium 选择器源码文本。

两个 emitter（独立 ``UiDriver`` 脚本与官方 devicetest TestCase）共用本模块，
保证同一份 IR 在两个引擎里渲染出**同一个选择器**。

必须逐字节复现 ``generation/hypium.py`` 历史输出（既有测试做精确子串断言）：

======================  ======================================================
LocatorSpec             渲染
======================  ======================================================
KEY/ID + EQUALS         ``BY.key('v')`` / ``BY.id('v')``
KEY/ID + STARTS_WITH    ``BY.key('prefix', MatchPattern.STARTS_WITH)``
TEXT + EQUALS           ``BY.text('v')``
TEXT + CONTAINS         ``BY.text('v', MatchPattern.CONTAINS)``
TYPE_TEXT               ``BY.type('T').text('x')``
COORDINATE              ``(x, y)``（调用方按需追加 ``# coordinate fallback`` 注释）
======================  ======================================================
"""

from __future__ import annotations

from ..cases.spec import LocatorSpec, MatchMode
from ..models import LocatorKind

#: IR ``MatchMode`` → hypium ``MatchPattern`` 成员名；``EQUALS`` 是默认值，渲染时可省略。
_MATCH_PATTERN_MEMBERS: dict[MatchMode, str] = {
    MatchMode.EQUALS: "EQUALS",
    MatchMode.CONTAINS: "CONTAINS",
    MatchMode.STARTS_WITH: "STARTS_WITH",
    MatchMode.ENDS_WITH: "ENDS_WITH",
    MatchMode.REGEXP: "REGEXP",
}

#: 只有 ``key`` / ``id`` 支持前缀泛化（``MatchPattern.STARTS_WITH``）。
_PREFIX_GENERALIZABLE = (LocatorKind.KEY, LocatorKind.ID)


def render_match_pattern(match: MatchMode) -> str:
    """把 ``MatchMode`` 渲染为 ``MatchPattern.<MEMBER>`` 源码文本。"""
    return f"MatchPattern.{_MATCH_PATTERN_MEMBERS[match]}"


def render_coordinate(point: tuple[int, int]) -> str:
    """把屏幕坐标渲染为 hypium 接受的点元组字面量。"""
    return f"({int(point[0])}, {int(point[1])})"


def render_selector(locator: LocatorSpec) -> str:
    """把 IR 定位器渲染为 hypium 选择器表达式源码文本。"""
    kind = locator.kind
    if kind in (LocatorKind.KEY, LocatorKind.ID):
        method = "key" if kind == LocatorKind.KEY else "id"
        if locator.match == MatchMode.EQUALS:
            return f"BY.{method}({locator.value!r})"
        if locator.match in {MatchMode.STARTS_WITH, MatchMode.CONTAINS}:
            return f"BY.{method}({locator.value!r}, {render_match_pattern(locator.match)})"
        raise ValueError(f"locator match {locator.match} is not supported for {kind} selectors")
    if kind == LocatorKind.TEXT:
        if locator.match == MatchMode.EQUALS:
            return f"BY.text({locator.value!r})"
        return f"BY.text({locator.value!r}, {render_match_pattern(locator.match)})"
    if kind == LocatorKind.TYPE_TEXT:
        type_name, _, text = locator.value.partition("|")
        return f"BY.type({type_name!r}).text({text!r})"
    if kind == LocatorKind.COORDINATE:
        if locator.coordinate is None:
            raise ValueError("coordinate locator without a coordinate cannot be rendered")
        return render_coordinate(locator.coordinate)
    raise ValueError(f"locator kind {kind} cannot be rendered as a hypium selector")


def is_prefix_generalizable(locator: LocatorSpec) -> bool:
    """该定位器是否走了动态 key 前缀泛化（StableLocator 证据已生效）。"""
    return locator.kind in _PREFIX_GENERALIZABLE and locator.match == MatchMode.STARTS_WITH


__all__ = [
    "is_prefix_generalizable",
    "render_coordinate",
    "render_match_pattern",
    "render_selector",
]
