"""将模型给出的工具决策解析为受安全策略约束的设备操作与断言。"""

from __future__ import annotations

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
from .safety import SafetyPolicy


class ToolExecutionError(RuntimeError):
    """携带对应运行失败状态的工具执行异常。"""

    def __init__(self, message: str, state: RunState = RunState.FAILED_ACTION):
        super().__init__(message)
        self.state = state


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
            element, locator = self._resolve(snapshot, decision.target, clickable=True)
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
            start, end = self._swipe_points(snapshot, decision.direction or "up")
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
        raise ToolExecutionError(f"element not found: {target}", RunState.FAILED_ELEMENT)

    @staticmethod
    def _swipe_points(snapshot: ScreenSnapshot, direction: str) -> tuple[tuple[int, int], tuple[int, int]]:
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
        if snapshot is None:
            return AssertionResult(
                kind=decision.tool, target=decision.target or "", passed=False, message="no screenshot"
            ), None
        target = (
            (decision.text or decision.target or "")
            if decision.tool == ToolName.ASSERT_TEXT
            else (decision.target or decision.text or "")
        )
        candidates = self._assertion_candidates(target)
        found = None
        matched_candidate = None
        for candidate in candidates:
            found = find_element(
                snapshot.elements,
                candidate,
                stable_locators=self.stable_locators,
            )
            if found:
                matched_candidate = candidate
                break

        page_text = " ".join(filter(None, (snapshot.page_title, snapshot.summary))).casefold()
        summary_candidate = next(
            (candidate for candidate in candidates if candidate.casefold() in page_text),
            None,
        )
        # 页面摘要仅能证明可见性；文本断言仍要求命中实际 UI 元素。
        visible = found is not None or summary_candidate is not None
        if decision.tool == ToolName.ASSERT_NOT_VISIBLE:
            passed = not visible
        elif decision.tool == ToolName.ASSERT_TEXT:
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
        return AssertionResult(
            kind=decision.tool,
            target=target,
            passed=passed,
            message=message,
        ), found[1] if found else None

    @staticmethod
    def _assertion_candidates(target: str) -> list[str]:
        return target_variants(target)
