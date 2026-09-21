"""在**原始** ArkUI dump JSON 上检测布局异常。

关键点：``perception.normalizer.normalize_layout`` 会静默丢弃越界 bbox
（``perception/normalizer.py:80-81``），因此异常检测必须在过滤之前、直接作用于原始
层级。本模块复用 ``walk_nodes`` 遍历原始节点，并自己解析 bounds——解析正则比
normalizer 多了可选的负号（``-?\\d+``），否则 ``left < -tol`` 这类越界永远无法被发现。

规则（只看可见节点，即 ``attributes.visible != "false"``）：

- **L1 out_of_bounds**（warning）：有非空 text/key、面积 ≥ 屏幕 1%，且越界超过 ``tol``。
- **L2 text_overlap**（warning）：同级叶子节点都有非空 text、IoU > 0.5、各自面积 ≥ 屏幕 1%。
- **L3 clipped_child**（info）：子节点超出父节点 > ``tol`` 且 ``交集 / 子面积 < 0.9`` 且子节点有文本。
- **L4 zero_size_visible**（info）：可见且有 text/key，但 bounds 退化（``right <= left or bottom <= top``）。

findings 按 L1 → L2 → L3 → L4 的顺序返回，并截断到 ``max_findings``；layout 文件相对
路径由 ``ExecutionAnalyzer`` 补进 ``evidence["layout_relative"]``。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from ..models import AnomalyFinding, AnomalyKind
from ..perception.normalizer import walk_nodes

#: 与 ``normalizer.parse_bounds`` 同一形状，但允许负坐标且不做退化过滤。
BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

MIN_AREA_RATIO = 0.01
OVERLAP_IOU_THRESHOLD = 0.5
CLIP_COVERAGE_THRESHOLD = 0.9
TEXT_EXCERPT_CHARS = 80

_RULE_META = {
    "L1": ("L1_out_of_bounds", "warning", "控件越出屏幕边界（可能被系统裁剪或不可点击）"),
    "L2": ("L2_text_overlap", "warning", "同级文本控件重叠（疑似布局塌陷或浮层错误）"),
    "L3": ("L3_clipped_child", "info", "子控件被父容器裁剪（内容可能显示不全）"),
    "L4": ("L4_zero_size_visible", "info", "可见控件尺寸退化为零（有文本但不可见）"),
}


@dataclass(frozen=True)
class Rect:
    """原始 bounds 解析结果（不做退化过滤）。"""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    @property
    def degenerate(self) -> bool:
        return self.right <= self.left or self.bottom <= self.top

    @property
    def raw(self) -> str:
        return f"[{self.left},{self.top}][{self.right},{self.bottom}]"


@dataclass(frozen=True)
class _Node:
    """原始节点的最小投影，供各条规则复用。"""

    index: int
    key: str
    text: str
    type: str
    raw_bounds: str
    rect: Rect | None
    visible: bool
    child_count: int

    @property
    def named(self) -> bool:
        return bool(self.key or self.text)

    def brief(self) -> dict[str, Any]:
        """节点摘要，写进 finding evidence。"""
        return {
            "node_index": self.index,
            "key": self.key,
            "type": self.type,
            "text": self.text[:TEXT_EXCERPT_CHARS],
            "bounds": self.raw_bounds,
        }


def _parse_rect(value: Any) -> Rect | None:
    """解析原始 bounds；形状不匹配时返回 None（退化矩形仍会返回）。"""
    if not isinstance(value, str):
        return None
    match = BOUNDS_RE.search(value)
    if not match:
        return None
    left, top, right, bottom = (int(item) for item in match.groups())
    return Rect(left=left, top=top, right=right, bottom=bottom)


def _node(index: int, node: dict[str, Any]) -> _Node:
    """把原始节点转换为 :class:`_Node`。"""
    attrs = node.get("attributes") or {}
    raw_bounds = str(attrs.get("bounds") or "")
    return _Node(
        index=index,
        key=str(attrs.get("key") or ""),
        text=str(attrs.get("text") or ""),
        type=str(attrs.get("type") or ""),
        raw_bounds=raw_bounds,
        rect=_parse_rect(raw_bounds),
        visible=str(attrs.get("visible", "true")) != "false",
        child_count=len(node.get("children") or []),
    )


def _intersection_area(left: Rect, right: Rect) -> int:
    """两个矩形的交集面积。"""
    width = min(left.right, right.right) - max(left.left, right.left)
    height = min(left.bottom, right.bottom) - max(left.top, right.top)
    if width <= 0 or height <= 0:
        return 0
    return width * height


def _iou(left: Rect, right: Rect) -> float:
    """交并比。"""
    inter = _intersection_area(left, right)
    if inter <= 0:
        return 0.0
    union = left.area + right.area - inter
    return inter / union if union > 0 else 0.0


def _finding(rule: str, node: _Node, detail: str, extra: dict[str, Any]) -> AnomalyFinding:
    """按规则元数据组装一条 layout finding。"""
    label, severity, summary = _RULE_META[rule]
    evidence: dict[str, Any] = {"rule": label, "hierarchy": "raw_arkui_dump", **node.brief()}
    evidence.update(extra)
    return AnomalyFinding(
        kind=AnomalyKind.LAYOUT_ANOMALY,
        severity=severity,
        summary_zh=summary,
        detail=detail[:400],
        source="ui_dump",
        evidence=evidence,
    )


def _positions(root: dict[str, Any]) -> dict[int, int]:
    """``id(原始节点) -> walk_nodes 遍历序号``，用于 evidence 中的节点定位。"""
    return {id(node): index for index, node in enumerate(walk_nodes(root))}


def _walk_pairs(root: dict[str, Any], positions: dict[int, int]) -> Iterator[tuple[_Node, _Node]]:
    """遍历所有父子组合（父节点与子节点都转成 :class:`_Node`）。"""
    for node in walk_nodes(root):
        parent = _node(positions.get(id(node), -1), node)
        for child in node.get("children") or []:
            yield parent, _node(positions.get(id(child), -1), child)


def _detect_out_of_bounds(
    nodes: list[_Node], width: int, height: int, tol: int, min_area: float
) -> list[AnomalyFinding]:
    """L1：可见且有名有实的节点越出屏幕边界。"""
    findings: list[AnomalyFinding] = []
    for node in nodes:
        rect = node.rect
        if not node.visible or not node.named or rect is None or rect.degenerate:
            continue
        if rect.area < min_area:
            continue
        overflow = {
            "left": max(0, -tol - rect.left),
            "top": max(0, -tol - rect.top),
            "right": max(0, rect.right - width - tol),
            "bottom": max(0, rect.bottom - height - tol),
        }
        if not any(overflow.values()):
            continue
        detail = f"{node.type or 'node'} {node.raw_bounds} 越界（屏幕 {width}x{height}，tol={tol}）"
        findings.append(
            _finding(
                "L1",
                node,
                detail,
                {
                    "overflow_px": overflow,
                    "screen_size": [width, height],
                    "area_ratio": round(rect.area / (width * height), 4),
                    "tol": tol,
                },
            )
        )
    return findings


def _detect_text_overlap(
    root: dict[str, Any], positions: dict[int, int], tol: int, min_area: float
) -> list[AnomalyFinding]:
    """L2：同级叶子文本节点之间 IoU 过高。"""
    findings: list[AnomalyFinding] = []
    for node in walk_nodes(root):
        attrs = node.get("attributes") or {}
        if str(attrs.get("visible", "true")) == "false":
            continue
        leaves = [_node(positions.get(id(child), -1), child) for child in node.get("children") or []]
        leaves = [leaf for leaf in leaves if leaf.child_count == 0 and leaf.text and leaf.visible and leaf.rect]
        for position, left in enumerate(leaves):
            for right in leaves[position + 1 :]:
                left_rect = left.rect
                right_rect = right.rect
                if left_rect is None or right_rect is None:
                    continue
                if left_rect.area < min_area or right_rect.area < min_area:
                    continue
                iou = _iou(left_rect, right_rect)
                if iou <= OVERLAP_IOU_THRESHOLD:
                    continue
                detail = f"同级文本重叠 IoU={iou:.2f}：{left.brief()['text']} / {right.brief()['text']}"
                findings.append(
                    _finding(
                        "L2",
                        left,
                        detail,
                        {"iou": round(iou, 4), "other": right.brief(), "tol": tol},
                    )
                )
    return findings


def _detect_clipped_children(root: dict[str, Any], positions: dict[int, int], tol: int) -> list[AnomalyFinding]:
    """L3：子节点越过父容器且大部分面积落在父容器之外。"""
    findings: list[AnomalyFinding] = []
    for parent, child in _walk_pairs(root, positions):
        parent_rect = parent.rect
        child_rect = child.rect
        if not parent.visible or not child.visible or not child.text:
            continue
        if parent_rect is None or child_rect is None or child_rect.degenerate or parent_rect.degenerate:
            continue
        beyond = (
            child_rect.left < parent_rect.left - tol
            or child_rect.top < parent_rect.top - tol
            or child_rect.right > parent_rect.right + tol
            or child_rect.bottom > parent_rect.bottom + tol
        )
        if not beyond:
            continue
        coverage = _intersection_area(parent_rect, child_rect) / child_rect.area if child_rect.area else 0.0
        if coverage >= CLIP_COVERAGE_THRESHOLD:
            continue
        detail = f"子节点 {child.raw_bounds} 超出父容器 {parent.raw_bounds}（覆盖率 {coverage:.2f}）"
        findings.append(
            _finding(
                "L3",
                child,
                detail,
                {"parent": parent.brief(), "coverage": round(coverage, 4), "tol": tol},
            )
        )
    return findings


def _detect_zero_size(nodes: list[_Node]) -> list[AnomalyFinding]:
    """L4：可见且有 text/key，但 bounds 退化。"""
    findings: list[AnomalyFinding] = []
    for node in nodes:
        rect = node.rect
        if not node.visible or not node.named or rect is None or not rect.degenerate:
            continue
        findings.append(
            _finding(
                "L4",
                node,
                f"可见节点 {node.raw_bounds} 尺寸退化为零",
                {"degenerate": True},
            )
        )
    return findings


def detect_layout_anomalies(
    hierarchy: dict,
    width: int,
    height: int,
    *,
    tol: int = 2,
    max_findings: int = 20,
) -> list[AnomalyFinding]:
    """在原始 ArkUI dump 上跑 L1–L4 规则，返回（最多 ``max_findings`` 条）布局异常。"""
    if not isinstance(hierarchy, dict) or not hierarchy:
        return []
    if width <= 0 or height <= 0 or max_findings <= 0:
        return []
    nodes = [_node(index, node) for index, node in enumerate(walk_nodes(hierarchy))]
    positions = _positions(hierarchy)
    min_area = width * height * MIN_AREA_RATIO
    findings: list[AnomalyFinding] = []
    findings.extend(_detect_out_of_bounds(nodes, width, height, tol, min_area))
    findings.extend(_detect_text_overlap(hierarchy, positions, tol, min_area))
    findings.extend(_detect_clipped_children(hierarchy, positions, tol))
    findings.extend(_detect_zero_size(nodes))
    return findings[:max_findings]


__all__ = ["BOUNDS_RE", "detect_layout_anomalies"]
