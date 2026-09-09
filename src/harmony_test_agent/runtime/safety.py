"""在设备操作前校验任务文本、工具参数和坐标边界。"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import ScreenSnapshot, ToolDecision, ToolName


class SafetyError(RuntimeError):
    """表示任务或工具决策违反运行期安全约束。"""

    pass


@dataclass(slots=True)
class SafetyPolicy:
    """拦截敏感操作，并校验坐标点击和等待时长等执行边界。"""

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
        """拒绝包含敏感操作词的自然语言任务。"""
        lowered = task.casefold()
        match = next((term for term in self.blocked_terms if term.casefold() in lowered), None)
        if match:
            raise SafetyError(f"task contains blocked operation: {match}")

    def validate_decision(self, decision: ToolDecision, snapshot: ScreenSnapshot | None) -> None:
        """在执行前校验工具文本、坐标依赖与等待上限。"""
        text = " ".join(filter(None, (decision.target, decision.text))).casefold()
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
