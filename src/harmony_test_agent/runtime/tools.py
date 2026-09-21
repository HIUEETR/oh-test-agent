"""将模型给出的工具决策解析为受安全策略约束的设备操作与断言。"""

from __future__ import annotations

import difflib
import time
from dataclasses import dataclass, field

from ..devices import DeviceAdapter
from ..models import (
    ActionResult,
    AssertionResult,
    LocatorCandidate,
    LocatorKind,
    RunState,
    ScreenSnapshot,
    StableLocator,
    TargetAppProfile,
    ToolDecision,
    ToolName,
    utc_now,
)
from ..perception import find_element, target_variants
from ..perception.normalizer import normalize_ui_text
from .safety import SafetyPolicy


class ToolExecutionError(RuntimeError):
    """携带对应运行失败状态的工具执行异常。"""

    def __init__(self, message: str, state: RunState = RunState.FAILED_ACTION):
        super().__init__(message)
        self.state = state


#: 滚轮候选「行」的最大高度（px）：更高的元素是容器而不是一行。
_ROW_LIKE_MAX_HEIGHT = 240
#: 判定两个间距属于同一「格距」的容差（px）。
_ROW_PITCH_TOLERANCE = 4
#: 滑动坐标与屏幕边缘保留的最小边距（px）：真机 `uiInput swipe` 对 y=0 直接拒绝。
_SWIPE_EDGE_INSET = 2


@dataclass(slots=True)
class LaunchSpec:
    """最小启动规格：verified Profile 或实时模式解析结果都能产出它。"""

    bundle_name: str
    main_ability: str
    launch_strategy: dict[str, str] | None = None

    @classmethod
    def from_profile(cls, profile: TargetAppProfile) -> LaunchSpec:
        return cls(
            bundle_name=profile.bundle_name,
            main_ability=profile.main_ability,
            launch_strategy=dict(profile.launch_strategy) if profile.launch_strategy else None,
        )

    @classmethod
    def from_kwargs(cls, bundle_name: str, main_ability: str, module_name: str | None = None) -> LaunchSpec:
        return cls(
            bundle_name=bundle_name,
            main_ability=main_ability,
            launch_strategy={"module_name": module_name} if module_name else None,
        )


@dataclass(slots=True)
class ToolExecutor:
    """解析元素、调用设备适配器并生成统一的动作结果。"""

    device: DeviceAdapter
    launch: LaunchSpec
    safety: SafetyPolicy
    stable_locators: list[StableLocator] = field(default_factory=list)

    def execute(self, step_id: str, decision: ToolDecision, snapshot: ScreenSnapshot | None) -> ActionResult:
        """执行一个已规划工具决策，并返回标准化动作结果。"""
        self.safety.validate_decision(decision, snapshot)
        started_at = utc_now()
        started = time.monotonic()
        params = decision.model_dump(exclude_none=True, mode="json")
        command = None
        assertion = None
        locator = None
        warnings: list[str] = []

        if decision.tool == ToolName.OPEN_APP:
            command = self.device.open_app(self.launch, reset=True)
        elif decision.tool == ToolName.INSPECT_SCREEN:
            pass
        elif decision.tool == ToolName.CLICK_ELEMENT:
            element, locator = self._resolve(snapshot, decision.target, clickable=True, warnings=warnings)
            if not element.bbox:
                raise ToolExecutionError("resolved element has no usable bounds", RunState.FAILED_ELEMENT)
            command = self.device.click(*element.bbox.center)
        elif decision.tool == ToolName.CLICK_COORDINATE:
            assert decision.coordinate is not None
            command = self.device.click(*decision.coordinate)
            locator = LocatorCandidate(kind=LocatorKind.COORDINATE, value=str(decision.coordinate), score=0.5)
            warnings.append("coordinate fallback may fail when resolution or layout changes")
        elif decision.tool == ToolName.INPUT_TEXT:
            if not decision.text:
                raise ToolExecutionError("input_text requires text")
            element, locator = self._resolve(snapshot, decision.target or "输入框", editable=True)
            if not element.bbox:
                raise ToolExecutionError("editable element has no usable bounds", RunState.FAILED_ELEMENT)
            command = self.device.input_text(decision.text, *element.bbox.center)
        elif decision.tool == ToolName.SWIPE:
            if snapshot is None:
                raise ToolExecutionError("swipe requires a current screenshot", RunState.FAILED_ELEMENT)
            anchor = self._swipe_anchor(snapshot, decision.target)
            if anchor is None and decision.target:
                warnings.append(f"swipe target {decision.target!r} not found; swiping at screen center")
            start, end = self._swipe_points(snapshot, decision, anchor, warnings=warnings)
            command = self.device.swipe(start, end)
        elif decision.tool == ToolName.BACK:
            command = self.device.back()
        elif decision.tool == ToolName.WAIT:
            command = self.device.wait(decision.wait_seconds or 1)
        elif decision.tool in {ToolName.ASSERT_VISIBLE, ToolName.ASSERT_NOT_VISIBLE, ToolName.ASSERT_TEXT}:
            assertion, locator = self._assert(decision, snapshot)
        elif decision.tool == ToolName.FINISH:
            pass
        else:
            raise ToolExecutionError(f"unsupported tool: {decision.tool}")

        if command is not None and not command.ok:
            raise ToolExecutionError(command.stderr or command.stdout or f"{decision.tool} failed")
        if assertion is not None and not assertion.passed:
            raise ToolExecutionError(assertion.message, RunState.FAILED_ASSERTION)
        return ActionResult(
            step_id=step_id,
            tool=decision.tool,
            params=params,
            success=True,
            started_at=started_at,
            ended_at=utc_now(),
            duration_ms=round((time.monotonic() - started) * 1000),
            command=command,
            assertion=assertion,
            locator=locator,
            warnings=warnings,
        )

    def _resolve(
        self,
        snapshot: ScreenSnapshot | None,
        target: str | None,
        *,
        clickable: bool | None = None,
        editable: bool | None = None,
        warnings: list[str] | None = None,
    ):
        if snapshot is None:
            raise ToolExecutionError("element tool requires a current screenshot", RunState.FAILED_ELEMENT)
        result = find_element(
            snapshot.elements,
            target or "",
            stable_locators=self.stable_locators,
            clickable=clickable,
            editable=editable,
        )
        if result:
            return result
        if editable:
            # 输入框文案常为空；精确目标失败时仅回退到具备边界的可编辑元素。
            fallback = next((item for item in snapshot.elements if item.editable and item.bbox), None)
            if fallback:
                locator = next(iter(fallback.locator_candidates), None)
                return fallback, locator or LocatorCandidate(kind=LocatorKind.SPATIAL, value="editable", score=0.6)
        if clickable:
            # 滚轮选择器/自绘控件的滚轮项常不带 clickable 标志（如系统时间选择器的"上午/下午"列），
            # 但坐标点击仍然有效：放宽为任意带边界元素并按中心点击。严格匹配优先，避免放宽后误点相近元素。
            relaxed = find_element(snapshot.elements, target or "", stable_locators=self.stable_locators)
            if relaxed is not None:
                if not relaxed[0].bbox:
                    raise ToolExecutionError(
                        f"element {target!r} exists but has no clickable bounds", RunState.FAILED_ELEMENT
                    )
                if warnings is not None:
                    warnings.append(f"target {target!r} matched a non-clickable element; clicking its bounds center")
                return relaxed
        raise ToolExecutionError(f"element not found: {target}", RunState.FAILED_ELEMENT)

    def _swipe_anchor(self, snapshot: ScreenSnapshot, target: str | None) -> tuple[int, int, int, int] | None:
        """解析滑动锚定元素：滚轮列等目标不要求 clickable 标志，但必须有边界。"""
        if not target:
            return None
        result = find_element(snapshot.elements, target, stable_locators=self.stable_locators)
        if result is None or not result[0].bbox:
            return None
        bbox = result[0].bbox
        return bbox.left, bbox.top, bbox.right, bbox.bottom

    @staticmethod
    def _swipe_points(
        snapshot: ScreenSnapshot,
        decision: ToolDecision,
        anchor: tuple[int, int, int, int] | None = None,
        *,
        warnings: list[str] | None = None,
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        """按优先级解析滑动起止点（计划 5.5/R10）。

        1. 显式 ``start``/``end`` 坐标：模型自己算出「每格多少像素、要移动几格」时最精确；
        2. ``steps`` × 滚轮格距：格距由同一列相邻可见项的 y 差推导，推不出时回退并告警；
        3. ``direction`` + 锚点元素 1/4 行程（历史行为）；
        4. 全屏 30%（历史行为）。
        """
        direction = decision.direction or "up"
        if decision.start is not None and decision.end is not None:
            return decision.start, decision.end
        if anchor is not None and (decision.steps or decision.distance):
            left, top, right, bottom = anchor
            cx, cy = (left + right) // 2, (top + bottom) // 2
            travel = ToolExecutor._swipe_travel(snapshot, anchor, decision, warnings=warnings)
            if travel is not None:
                half = travel // 2
                # 以锚点为轴的对称滑动必须两侧都落在屏幕内：真机实测 `uiInput swipe` 对
                # y=0 直接拒绝（打印 usage 且仍以 0 退出），所以既留边距也按可用空间收缩行程。
                vertical = direction in {"up", "down"}
                pivot = cy if vertical else cx
                span = snapshot.height if vertical else snapshot.width
                available = min(pivot - _SWIPE_EDGE_INSET, span - _SWIPE_EDGE_INSET - 1 - pivot)
                if half > available:
                    if warnings is not None:
                        warnings.append(
                            f"swipe travel {travel}px does not fit around the anchor; "
                            f"shortened to {max(available * 2, 2)}px to stay on screen"
                        )
                    half = available
                half = max(half, 1)
                mapping = {
                    "up": ((cx, cy + half), (cx, cy - half)),
                    "down": ((cx, cy - half), (cx, cy + half)),
                    "left": ((cx + half, cy), (cx - half, cy)),
                    "right": ((cx - half, cy), (cx + half, cy)),
                }
                start, end = mapping[direction]
                clamp = lambda point: (  # noqa: E731 - 就地裁剪，避免坐标越出屏幕
                    min(
                        max(point[0], _SWIPE_EDGE_INSET),
                        max(snapshot.width - _SWIPE_EDGE_INSET - 1, _SWIPE_EDGE_INSET),
                    ),
                    min(
                        max(point[1], _SWIPE_EDGE_INSET),
                        max(snapshot.height - _SWIPE_EDGE_INSET - 1, _SWIPE_EDGE_INSET),
                    ),
                )
                return clamp(start), clamp(end)
        return ToolExecutor._default_swipe_points(snapshot, direction, anchor)

    @staticmethod
    def _swipe_travel(
        snapshot: ScreenSnapshot,
        anchor: tuple[int, int, int, int],
        decision: ToolDecision,
        *,
        warnings: list[str] | None = None,
    ) -> int | None:
        """推导本次滑动行程：显式 distance 优先，其次格数 × 格距。

        返回 ``None`` 表示「格距推不出来」，调用方回退到历史的方向 + 锚点 1/4 行程规则
        （实测：模型有时会把整页容器当作滚轮锚点，此时任何「猜出来」的小格距都会退化成
        几乎没有位移的空滑动，例如 36px；回退到 1/4 锚点至少与改动前的行为一致）。
        """
        if decision.distance:
            return max(int(decision.distance), 1)
        steps = max(int(decision.steps or 0), 1)
        anchor_height = max(anchor[3] - anchor[1], 1)
        pitch = ToolExecutor._row_pitch(snapshot, anchor)
        if pitch is None:
            if warnings is not None:
                warnings.append(
                    f"cannot derive wheel row pitch for {decision.target!r}; "
                    "falling back to a directional swipe with anchor quarter travel"
                )
            # 历史回退规则（锚点 1/4 行程）× 请求格数：格距不可信时至少保持与改动前一致的
            # 单格位移，并让「移动 N 格」在尺度上单调。
            return max(anchor_height // 4, 1) * steps
        # 不按锚点高度裁剪：滚轮单行元素的 bbox 往往只有一格高，裁剪会把「移动 4 格」压成 1 格。
        return min(pitch * steps, max(snapshot.height, pitch))

    @staticmethod
    def _row_pitch(snapshot: ScreenSnapshot, anchor: tuple[int, int, int, int]) -> int | None:
        """同一滚轮列内相邻可见行的 y 差（格距）；推不出可靠格距时返回 ``None``。

        实测参考：日历时间选择器每格 108px（960−852），据此可算出 9→1 要往下 4 格而不是往上 8 格。

        可靠性约束（避免把整页容器当成滚轮列时凭空造出小格距）：候选行必须与锚点同列
        （水平重叠 ≥ 较窄一方宽度的 60%）、不是整屏宽容器、高度像一行（8-240px）；
        同一 y 只保留**面积最大**的元素（外层行而非其内部文本），再按高度分桶取最大的一桶，
        桶内至少 3 行且间距重复出现（取众数而非中位数）。
        """
        left, top, right, bottom = anchor
        width = max(right - left, 1)
        height = max(bottom - top, 1)
        by_center: dict[int, tuple[int, int]] = {}
        for item in snapshot.elements:
            bbox = item.bbox
            if bbox is None or not (item.key or item.id or item.content):
                continue
            if bbox.width >= snapshot.width * 0.9 or not 8 <= bbox.height <= _ROW_LIKE_MAX_HEIGHT:
                continue
            overlap = min(bbox.right, right) - max(bbox.left, left)
            if overlap < min(width, bbox.width) * 0.6:
                continue
            center = (bbox.top + bbox.bottom) // 2
            if not (top - height <= center <= bottom + height):
                continue
            # 同一 y 只保留面积最大的元素：滚轮的一行里往往还嵌着若干文本子节点。
            best = by_center.get(center)
            if best is None or bbox.area > best[1]:
                by_center[center] = (bbox.height, bbox.area)
        if len(by_center) < 3:
            return None
        buckets: dict[int, list[int]] = {}
        for center, (row_height, _) in by_center.items():
            key = round(row_height / max(_ROW_PITCH_TOLERANCE, 1))
            buckets.setdefault(key, []).append(center)
        centers = sorted(max(buckets.values(), key=len))
        if len(centers) < 3:
            return None
        gaps = [later - earlier for earlier, later in zip(centers, centers[1:], strict=False) if later - earlier >= 24]
        if not gaps:
            return None
        best_gap, best_count = gaps[0], 0
        for gap in gaps:
            count = sum(1 for other in gaps if abs(other - gap) <= _ROW_PITCH_TOLERANCE)
            if count > best_count:
                best_gap, best_count = gap, count
        return best_gap

    @staticmethod
    def _default_swipe_points(
        snapshot: ScreenSnapshot,
        direction: str,
        anchor: tuple[int, int, int, int] | None = None,
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        if anchor is not None:
            # 锚定元素（滚轮列等）：以元素中心为轴，行程取元素尺寸的一半，方向语义与全屏滑动一致。
            left, top, right, bottom = anchor
            cx, cy = (left + right) // 2, (top + bottom) // 2
            dx, dy = (right - left) // 4, (bottom - top) // 4
            mapping = {
                "up": ((cx, cy + dy), (cx, cy - dy)),
                "down": ((cx, cy - dy), (cx, cy + dy)),
                "left": ((cx + dx, cy), (cx - dx, cy)),
                "right": ((cx - dx, cy), (cx + dx, cy)),
            }
            return mapping[direction]
        x, y = snapshot.width // 2, snapshot.height // 2
        dx, dy = int(snapshot.width * 0.3), int(snapshot.height * 0.3)
        mapping = {
            "up": ((x, y + dy), (x, y - dy)),
            "down": ((x, y - dy), (x, y + dy)),
            "left": ((x + dx, y), (x - dx, y)),
            "right": ((x - dx, y), (x + dx, y)),
        }
        return mapping[direction]

    def _assert(
        self,
        decision: ToolDecision,
        snapshot: ScreenSnapshot | None,
    ) -> tuple[AssertionResult, LocatorCandidate | None]:
        # ASSERT_TEXT 以 text 优先（期望文案），其余断言以 target 优先（语义目标）。
        # 该优先级必须与重构前逐字一致，因此在此计算而不是下沉到公共函数。
        target = (
            (decision.text or decision.target or "")
            if decision.tool == ToolName.ASSERT_TEXT
            else (decision.target or decision.text or "")
        )
        return evaluate_assertion(snapshot, decision.tool, target, self.stable_locators)

    @staticmethod
    def _assertion_candidates(target: str) -> list[str]:
        return target_variants(target)


def evaluate_assertion(
    snapshot: ScreenSnapshot | None,
    kind: ToolName,
    target: str,
    stable_locators: list[StableLocator] | None = None,
) -> tuple[AssertionResult, LocatorCandidate | None]:
    """纯函数断言评估，供 Live Mode ``ToolExecutor._assert`` 与 DC 断言工具共用。

    语义与重构前的 ``ToolExecutor._assert`` 完全一致：

    - ``target_variants`` 模糊匹配（返回首个命中变体）；
    - 页面摘要兜底仅证明「可见性」，供 ASSERT_VISIBLE / ASSERT_NOT_VISIBLE 使用；
    - ASSERT_TEXT 是严格断言，必须命中真实 UI 元素（页面摘要不算通过）；
    - 通用「内容/content」在仍有元素时视为通过。

    Args:
        snapshot: 目标帧；``None`` 表示尚未采集截图，直接判为失败。
        kind: ASSERT_VISIBLE / ASSERT_NOT_VISIBLE / ASSERT_TEXT。
        target: 已按工具语义选定优先级的断言目标。
        stable_locators: 参与 ``find_element`` 的稳定定位器证据。

    Returns:
        ``(AssertionResult, 命中元素的 LocatorCandidate 或 None)``。
    """
    if snapshot is None:
        return AssertionResult(kind=kind, target=target, passed=False, message="no screenshot"), None
    candidates = target_variants(target)
    found = None
    matched_candidate = None
    for candidate in candidates:
        found = find_element(
            snapshot.elements,
            candidate,
            stable_locators=stable_locators or [],
        )
        if found:
            matched_candidate = candidate
            break

    page_text = normalize_ui_text(" ".join(filter(None, (snapshot.page_title, snapshot.summary))))
    summary_candidate = next(
        (
            candidate
            for candidate in candidates
            if normalize_ui_text(candidate) and normalize_ui_text(candidate) in page_text
        ),
        None,
    )
    # 页面摘要仅能证明可见性；文本断言仍要求命中实际 UI 元素。
    visible = found is not None or summary_candidate is not None
    if kind == ToolName.ASSERT_NOT_VISIBLE:
        passed = not visible
    elif kind == ToolName.ASSERT_TEXT:
        passed = found is not None
    else:
        passed = visible or (target.casefold() in {"内容", "content"} and bool(snapshot.elements))

    if passed and found:
        message = f"assertion passed using UI element: {matched_candidate}"
    elif passed and summary_candidate:
        message = f"assertion passed using page summary: {summary_candidate}"
    elif passed:
        message = "assertion passed using visible screen content"
    else:
        message = f"target is not in current screen: {target.casefold()}"
        # 计划 6.2：把「屏幕实际是什么」回给恢复循环，模型才能一次改对。
        closest = closest_screen_texts(snapshot, target)
        if closest:
            message += f" (closest: {', '.join(closest)})"
    return AssertionResult(
        kind=kind,
        target=target,
        passed=passed,
        message=message,
    ), found[1] if found else None


def closest_screen_texts(snapshot: ScreenSnapshot, target: str, *, limit: int = 5) -> list[str]:
    """返回屏幕上与 ``target`` 最接近的若干原始文案（归一化后比较，展示原文）。

    例：目标 ``下午1:00``、屏幕 ``9月22日 下午01:00`` 时返回后者，恢复反馈因此能直接指出格式差异。
    """
    needle = normalize_ui_text(target)
    if not needle:
        return []
    candidates: list[tuple[float, str]] = []
    for element in snapshot.elements:
        for value in (element.content, element.description):
            if not value:
                continue
            normalized = normalize_ui_text(value)
            if not normalized:
                continue
            ratio = difflib.SequenceMatcher(None, needle, normalized).ratio()
            if needle in normalized or normalized in needle:
                ratio = max(ratio, 0.9)
            candidates.append((ratio, value))
    candidates.sort(key=lambda item: (-item[0], len(item[1])))
    selected: list[str] = []
    for _, value in candidates:
        if value in selected:
            continue
        selected.append(value)
        if len(selected) >= limit:
            break
    return selected
