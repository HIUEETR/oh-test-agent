"""在设备操作前校验任务文本、工具参数和坐标边界。"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import ExplorationPolicy, ScreenSnapshot, ToolDecision, ToolName


class SafetyError(RuntimeError):
    """表示任务或工具决策违反运行期安全约束。"""

    pass


@dataclass(slots=True)
class SafetyPolicy:
    """执行确定性的三级安全策略，模型不能解除永久禁用项。"""

    exploration_policy: ExplorationPolicy | None = None
    min_target_area: int = 16

    always_blocked: tuple[str, ...] = (
        "支付",
        "付款",
        "购买",
        "确认购买",
        "删除",
        "移除",
        "卸载",
        "清除数据",
        "恢复出厂",
        "系统设置",
        "payment",
        "pay now",
        "purchase",
        "delete",
        "remove",
        "uninstall",
        "clear data",
        "factory reset",
        "system settings",
    )
    gated_terms: dict[str, tuple[str, ...]] | None = None

    def __post_init__(self) -> None:
        if self.gated_terms is None:
            self.gated_terms = {
                "allow_login": ("登录", "登陆", "注册", "login", "sign in", "register"),
                "allow_permission": ("授权", "允许权限", "permission", "authorize", "allow access"),
                "allow_submit": ("提交", "发送", "确认", "submit", "send", "confirm"),
                "allow_publish": ("发布", "发表", "publish", "post"),
                "allow_download": ("下载", "保存到本地", "download", "save file"),
            }

    def _permanent_match(self, text: str) -> str | None:
        """永久禁用项与凭证词：在任务文本层面也应拒绝。"""
        lowered = text.casefold()
        permanent = next((term for term in self.always_blocked if term.casefold() in lowered), None)
        if permanent:
            return permanent
        credentials = (
            "密码",
            "验证码",
            "password",
            "captcha",
            "otp",
            "passcode",
            "pin",
        )
        return next((term for term in credentials if term.casefold() in lowered), None)

    def _gated_match(self, text: str) -> str | None:
        """门控词按 allow_* 开关匹配；只在工具决策层面调用，避免误伤任务描述里的日常用语。"""
        lowered = text.casefold()
        policy = self.exploration_policy
        for permission, terms in (self.gated_terms or {}).items():
            if not bool(policy and getattr(policy, permission, False)):
                candidates = [(lowered.find(term.casefold()), term) for term in terms if term.casefold() in lowered]
                if candidates:
                    return min(candidates)[1]
        return None

    def _blocked_match(self, text: str) -> str | None:
        return self._permanent_match(text) or self._gated_match(text)

    def validate_task(self, task: str) -> None:
        """任务描述只拦永久禁用项与凭证词；"确认/提交/发送"等门控词留给工具决策层面拦截。"""
        match = self._permanent_match(task)
        if match:
            raise SafetyError(f"task contains blocked operation: {match}")

    def validate_decision(self, decision: ToolDecision, snapshot: ScreenSnapshot | None) -> None:
        text = " ".join(filter(None, (decision.target, decision.text)))
        match = self._blocked_match(text)
        if match:
            raise SafetyError(f"tool decision contains blocked operation: {match}")
        if decision.tool == ToolName.CLICK_COORDINATE:
            if not decision.coordinate or snapshot is None:
                raise SafetyError("click_coordinate requires a current screenshot and coordinate")
            x, y = decision.coordinate
            if not 0 <= x < snapshot.width or not 0 <= y < snapshot.height:
                raise SafetyError(f"coordinate {(x, y)} is outside {snapshot.width}x{snapshot.height}")
        if decision.tool == ToolName.SWIPE and snapshot is not None:
            # 计划 5.5：显式起止坐标同样必须做边界校验（缺帧时由执行器给出 FAILED_ELEMENT）。
            for label, point in (("start", decision.start), ("end", decision.end)):
                if point is None:
                    continue
                x, y = point
                if not 0 <= x < snapshot.width or not 0 <= y < snapshot.height:
                    raise SafetyError(f"swipe {label} {(x, y)} is outside {snapshot.width}x{snapshot.height}")
        if decision.tool == ToolName.WAIT and (decision.wait_seconds or 0) > 30:
            raise SafetyError("wait may not exceed 30 seconds")
