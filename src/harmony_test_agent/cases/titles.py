"""中文步骤名与检查点名推导。

比赛要求「明确步骤、断言或检查点」，因此用例 IR 的每个步骤与检查点都必须能推导出
一条人类可读的中文标题；本模块是唯一的推导入口，两个 emitter 与前端都复用它。
"""

from __future__ import annotations

from ..models import LocatorKind
from .spec import CheckpointKind, CheckpointSpec, LocatorSpec, StepAction, TestStepSpec

ACTION_VERBS: dict[str, str] = {
    "click": "点击",
    "double_click": "双击",
    "long_click": "长按",
    "input_text": "输入",
    "clear_text": "清空",
    "swipe": "滑动",
    "fling": "快速滑动",
    "drag": "拖拽",
    "back": "返回",
    "wait": "等待",
    "key_event": "按键",
    "screenshot": "截屏",
    "check": "校验",
    "switch_status": "切换开关",
}

DIRECTION_ZH: dict[str, str] = {"up": "向上", "down": "向下", "left": "向左", "right": "向右"}

#: 默认长按时长（秒），与 ``driver.long_click(sel, press_time=...)`` 的默认值一致。
DEFAULT_LONG_PRESS_SECONDS = 2.0


def display_label(locator: LocatorSpec | None) -> str:
    """定位器的中文显示标签。

    优先级：``target_label`` → TEXT 值 → key → id → ``坐标 (x, y)``。
    """
    if locator is None:
        return ""
    if locator.target_label:
        return locator.target_label
    if locator.kind == LocatorKind.TEXT and locator.value:
        return locator.value
    if locator.kind == LocatorKind.TYPE_TEXT and locator.value:
        return locator.value.partition("|")[2] or locator.value
    if locator.kind == LocatorKind.KEY and locator.value:
        return locator.value
    if locator.kind == LocatorKind.ID and locator.value:
        return locator.value
    if locator.coordinate is not None:
        return f"坐标 {locator.coordinate}"
    if locator.value:
        return locator.value
    return ""


def _step_label(step: TestStepSpec) -> str:
    label = display_label(step.locator)
    if label:
        return label
    if step.coordinate is not None:
        return f"坐标 {step.coordinate}"
    return ""


def _step_text(step: TestStepSpec) -> str:
    if step.param_ref:
        return f"{{{step.param_ref}}}"
    return step.text or ""


def step_title_zh(step: TestStepSpec) -> str:
    """把一个 IR 步骤推导为中文标题。"""
    action = step.action
    label = _step_label(step)

    if action == StepAction.CLICK:
        return f"点击「{label}」" if label else "点击页面"
    if action == StepAction.DOUBLE_CLICK:
        return f"双击「{label}」" if label else "双击页面"
    if action == StepAction.LONG_CLICK:
        press_time = step.wait_seconds if step.wait_seconds is not None else DEFAULT_LONG_PRESS_SECONDS
        text = f"长按「{label}」{_trim_float(press_time)} 秒" if label else f"长按页面 {_trim_float(press_time)} 秒"
        return text
    if action == StepAction.INPUT_TEXT:
        return f"在「{label or '输入框'}」输入「{_step_text(step)}」"
    if action == StepAction.CLEAR_TEXT:
        return f"清空「{label or '输入框'}」"
    if action == StepAction.SWIPE:
        return f"{DIRECTION_ZH.get(step.direction or 'up', '向上')}滑动页面"
    if action == StepAction.FLING:
        return f"{DIRECTION_ZH.get(step.direction or 'up', '向上')}快速滑动页面"
    if action == StepAction.DRAG:
        origin = label or (f"坐标 {step.coordinate}" if step.coordinate else "起点")
        target = f"坐标 {step.drag_to}" if step.drag_to else "终点"
        return f"从「{origin}」拖拽到 {target}"
    if action == StepAction.BACK:
        return "按下返回键"
    if action == StepAction.WAIT:
        seconds = step.wait_seconds if step.wait_seconds is not None else 1.0
        return f"等待 {_trim_float(seconds)} 秒"
    if action == StepAction.KEY_EVENT:
        return f"按下 {step.key or '按键'}"
    if action == StepAction.SCREENSHOT:
        return f"截屏「{step.text}」" if step.text else "截屏"
    if action == StepAction.CHECK:
        return f"校验「{label}」" if label else "校验页面状态"
    if action == StepAction.SWITCH_STATUS:
        state = "开启" if step.checked else "关闭"
        return f"切换「{label or '开关'}」为{state}"
    if action == StepAction.START_APP:
        return "启动被测应用"
    if action == StepAction.STOP_APP:
        return "停止被测应用"
    if action == StepAction.NOOP_COMMENT:
        return step.comment or "注释"
    return ACTION_VERBS.get(str(action), str(action))


def checkpoint_title_zh(checkpoint: CheckpointSpec) -> str:
    """把一个检查点推导为中文标题。"""
    kind = checkpoint.kind
    label = display_label(checkpoint.locator)
    if kind == CheckpointKind.ELEMENT_EXISTS:
        return f"检查点：「{label or '目标元素'}」应可见"
    if kind == CheckpointKind.ELEMENT_ABSENT:
        return f"检查点：「{label or '目标元素'}」不应可见"
    if kind == CheckpointKind.TEXT_EQUALS:
        return f"检查点：「{label or '页面'}」文本应为「{checkpoint.expected}」"
    if kind == CheckpointKind.TEXT_CONTAINS:
        return f"检查点：页面应包含文本「{checkpoint.expected}」"
    if kind == CheckpointKind.PROPERTY_EQUALS:
        return f"检查点：「{label or '目标元素'}」的 {checkpoint.property_name} 应为「{checkpoint.expected}」"
    if kind == CheckpointKind.TOAST:
        return f"检查点：应弹出提示「{checkpoint.expected}」"
    if kind == CheckpointKind.CURRENT_APP:
        return f"检查点：前台应用仍为「{checkpoint.expected or '被测应用'}」"
    if kind == CheckpointKind.PAGE_SIGNATURE:
        return f"检查点：页面特征匹配「{checkpoint.page_path or '当前页'}」"
    if kind == CheckpointKind.SCREENSHOT_CAPTURED:
        return f"检查点：截图「{checkpoint.expected or 'final.jpeg'}」已产出"
    return f"检查点：{kind}"


def _trim_float(value: float) -> str:
    """去掉浮点显示里的尾随零，让「等待 1 秒」而不是「等待 1.0 秒」。"""
    text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return text or "0"


__all__ = [
    "ACTION_VERBS",
    "DEFAULT_LONG_PRESS_SECONDS",
    "DIRECTION_ZH",
    "checkpoint_title_zh",
    "display_label",
    "step_title_zh",
]
