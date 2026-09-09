from __future__ import annotations

import time
from dataclasses import dataclass

from ..devices import DeviceAdapter
from ..models import (
    ActionResult,
    AssertionResult,
    LocatorCandidate,
    LocatorKind,
    RunState,
    ScreenSnapshot,
    TargetAppProfile,
    ToolDecision,
    ToolName,
    utc_now,
)
from ..perception import find_element
from .safety import SafetyPolicy


class ToolExecutionError(RuntimeError):
    def __init__(self, message: str, state: RunState = RunState.FAILED_ACTION):
        super().__init__(message)
        self.state = state


@dataclass(slots=True)
class ToolExecutor:
    device: DeviceAdapter
    profile: TargetAppProfile
    safety: SafetyPolicy

    def execute(self, step_id: str, decision: ToolDecision, snapshot: ScreenSnapshot | None) -> ActionResult:
        self.safety.validate_decision(decision, snapshot)
        started_at = utc_now()
        started = time.monotonic()
        params = decision.model_dump(exclude_none=True, mode="json")
        command = None
        assertion = None
        locator = None
        warnings: list[str] = []

        if decision.tool == ToolName.OPEN_APP:
            command = self.device.open_app(self.profile, reset=True)
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
            stable_locators=self.profile.stable_locator_inventory,
            clickable=clickable,
            editable=editable,
        )
        if result:
            return result
        if editable:
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
        target = decision.target or decision.text or ""
        found = find_element(snapshot.elements, target, stable_locators=self.profile.stable_locator_inventory)
        visible = found is not None
        if decision.tool == ToolName.ASSERT_NOT_VISIBLE:
            passed = not visible
        elif decision.tool == ToolName.ASSERT_TEXT:
            passed = visible
        else:
            passed = visible or (target.casefold() in {"内容", "content"} and bool(snapshot.elements))
        return AssertionResult(
            kind=decision.tool,
            target=target,
            passed=passed,
            message="assertion passed" if passed else f"target is not in current screen: {target.casefold()}",
        ), found[1] if found else None
