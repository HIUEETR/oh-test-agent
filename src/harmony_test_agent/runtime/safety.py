from __future__ import annotations

from dataclasses import dataclass

from ..models import ScreenSnapshot, ToolDecision, ToolName


class SafetyError(RuntimeError):
    pass


@dataclass(slots=True)
class SafetyPolicy:
    blocked_terms: tuple[str, ...] = (
        "支付",
        "付款",
        "购买",
        "删除",
        "卸载",
        "授权",
        "允许权限",
        "验证码",
        "登录",
        "注册",
        "payment",
        "purchase",
        "delete",
        "uninstall",
        "grant permission",
        "captcha",
        "login",
        "register",
    )
    min_target_area: int = 16

    def validate_task(self, task: str) -> None:
        lowered = task.casefold()
        match = next((term for term in self.blocked_terms if term.casefold() in lowered), None)
        if match:
            raise SafetyError(f"task contains blocked operation: {match}")

    def validate_decision(self, decision: ToolDecision, snapshot: ScreenSnapshot | None) -> None:
        text = " ".join(filter(None, (decision.target, decision.text, decision.reasoning))).casefold()
        match = next((term for term in self.blocked_terms if term.casefold() in text), None)
        if match:
            raise SafetyError(f"tool decision contains blocked operation: {match}")
        if decision.tool == ToolName.CLICK_COORDINATE:
            if not decision.coordinate or snapshot is None:
                raise SafetyError("click_coordinate requires a current screenshot and coordinate")
            x, y = decision.coordinate
            if not 0 <= x < snapshot.width or not 0 <= y < snapshot.height:
                raise SafetyError(f"coordinate {(x, y)} is outside {snapshot.width}x{snapshot.height}")
        if decision.tool == ToolName.WAIT and (decision.wait_seconds or 0) > 30:
            raise SafetyError("wait may not exceed 30 seconds")
