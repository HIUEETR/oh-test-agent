"""``CheckpointSpec`` → 两种引擎的代码行。

* ``render_standalone_checkpoint`` — 独立 ``UiDriver`` 脚本（无 devicetest harness）
* ``render_devicetest_checkpoint`` — 官方 ``devicetest`` TestCase（``Step/CHECK/ASSERT``）

已实测存在的 hypium API：``check_component`` / ``check_component_exist`` /
``get_component_property`` / ``check_toast`` / ``start_listen_toast`` / ``current_app`` /
``capture_screen`` / ``switch_component_status``，以及 ``Assert`` 只在
``hypium.checker.assertion`` 下（不在根命名空间）。

``soft=True`` 的语义：独立脚本把失败记进 ``result["soft_failures"]``；
devicetest 只发 ``CHECK``（不抛）并在 except 里 ``MESSAGE``。
"""

from __future__ import annotations

from ..cases.spec import COMPONENT_PROPERTIES, CheckpointKind, CheckpointSpec
from ..models import LocatorKind
from .selectors import render_selector

DEFAULT_INDENT = "        "

#: ``page_signature`` 的锚点默认等待时间（秒）。
ANCHOR_WAIT_SECONDS = 5
#: ``toast`` 检查点的默认超时（秒）。
DEFAULT_TOAST_TIMEOUT = 3
#: ``screenshot_captured`` 未指定文件名时的默认产物名。
DEFAULT_SCREENSHOT_NAME = "final.jpeg"


class CheckpointRenderError(ValueError):
    """IR 检查点无法渲染为某个引擎的代码（例如缺定位器）。"""


def _wait_time(checkpoint: CheckpointSpec) -> int:
    return int(checkpoint.wait_seconds or 0)


def _wait_suffix(checkpoint: CheckpointSpec) -> str:
    """没有显式等待时间时不发射 ``wait_time``，保持历史字面量不变。"""
    wait = _wait_time(checkpoint)
    return f", wait_time={wait}" if wait > 0 else ""


def _selector_or_error(checkpoint: CheckpointSpec, engine: str) -> str:
    if checkpoint.locator is None:
        raise CheckpointRenderError(f"{engine}: checkpoint {checkpoint.kind} requires a locator")
    return render_selector(checkpoint.locator)


def needs_listen_toast(checkpoint: CheckpointSpec) -> bool:
    """该检查点是否要求 setup 阶段先 ``driver.start_listen_toast()``。"""
    return checkpoint.kind == CheckpointKind.TOAST


def _wrap_soft_standalone(lines: list[str], checkpoint: CheckpointSpec, indent: str) -> list[str]:
    inner = f"{indent}    "
    out = [f"{indent}try:"]
    out += [f"{inner}{line}" if line else line for line in lines]
    out.append(f"{indent}except Exception as _exc:")
    out.append(f"{inner}result['soft_failures'].append({{'message': {checkpoint.message_zh!r}, 'error': str(_exc)}})")
    return out


def _wrap_soft_devicetest(lines: list[str], checkpoint: CheckpointSpec, indent: str) -> list[str]:
    """soft 检查点只发 ``CHECK``（不抛），失败信息走 ``MESSAGE``。"""
    _ = checkpoint  # 保留与 _wrap_soft_standalone 一致的签名（standalone 需要 message）
    inner = f"{indent}    "
    out = [f"{indent}try:"]
    out += [f"{inner}{line}" if line else line for line in lines]
    out.append(f"{indent}except Exception as _exc:")
    out.append(f"{inner}MESSAGE(str(_exc))")
    return out


# ---------------------------------------------------------------------------
# 独立 UiDriver 脚本
# ---------------------------------------------------------------------------


def render_standalone_checkpoint(checkpoint: CheckpointSpec, *, indent: str = DEFAULT_INDENT) -> list[str]:
    """渲染一个检查点为独立脚本的代码行（含执行动作后的求值）。"""
    lines = _standalone_body(checkpoint)
    if checkpoint.soft:
        return _wrap_soft_standalone(lines, checkpoint, indent)
    return [f"{indent}{line}" for line in lines]


def _standalone_body(checkpoint: CheckpointSpec) -> list[str]:
    kind = checkpoint.kind
    message = checkpoint.message_zh

    if kind == CheckpointKind.ELEMENT_EXISTS:
        selector = _selector_or_error(checkpoint, "standalone")
        return [f"driver.check_component_exist({selector}, expect_exist=True{_wait_suffix(checkpoint)})"]
    if kind == CheckpointKind.ELEMENT_ABSENT:
        selector = _selector_or_error(checkpoint, "standalone")
        return [f"driver.check_component_exist({selector}, expect_exist=False{_wait_suffix(checkpoint)})"]
    if kind == CheckpointKind.TEXT_EQUALS:
        expected = checkpoint.expected if checkpoint.expected is not None else ""
        if checkpoint.locator is not None and checkpoint.locator.kind in {LocatorKind.KEY, LocatorKind.ID}:
            selector = render_selector(checkpoint.locator)
            return [f"driver.check_component({selector}, expected_equal=True, text={expected!r})"]
        return [f"driver.check_component_exist(BY.text({expected!r}), expect_exist=True{_wait_suffix(checkpoint)})"]
    if kind == CheckpointKind.TEXT_CONTAINS:
        expected = checkpoint.expected if checkpoint.expected is not None else ""
        return [
            "driver.check_component_exist("
            f"BY.text({expected!r}, MatchPattern.CONTAINS), expect_exist=True{_wait_suffix(checkpoint)})"
        ]
    if kind == CheckpointKind.PROPERTY_EQUALS:
        selector = _selector_or_error(checkpoint, "standalone")
        prop = checkpoint.property_name
        expected = checkpoint.expected
        if prop in COMPONENT_PROPERTIES:
            return [f"driver.check_component({selector}, expected_equal=True, {prop}={expected!r})"]
        return [f"Assert(driver).equal(driver.get_component_property({selector}, {prop!r}), {expected!r}, {message!r})"]
    if kind == CheckpointKind.TOAST:
        expected = checkpoint.expected if checkpoint.expected is not None else ""
        timeout = int(checkpoint.wait_seconds or DEFAULT_TOAST_TIMEOUT)
        return [f"driver.check_toast({expected!r}, fuzzy={checkpoint.toast_fuzzy!r}, timeout={timeout})"]
    if kind == CheckpointKind.CURRENT_APP:
        return [
            "_bundle, _ability = driver.current_app()",
            f"Assert(driver).equal(_bundle, {checkpoint.expected!r}, {message!r})",
        ]
    if kind == CheckpointKind.PAGE_SIGNATURE:
        lines = [f"# page {checkpoint.page_path}".rstrip()]
        for anchor in checkpoint.anchors:
            lines.append(
                f"driver.check_component_exist({render_selector(anchor)}, expect_exist=True, "
                f"wait_time={ANCHOR_WAIT_SECONDS})"
            )
        return lines
    if kind == CheckpointKind.SCREENSHOT_CAPTURED:
        name = str(checkpoint.expected or DEFAULT_SCREENSHOT_NAME)
        return [
            f"_shot = driver.capture_screen(str(REPORT_DIR / {name!r}))",
            f"Assert(driver).is_true(bool(_shot), True, {message!r})",
        ]
    raise CheckpointRenderError(f"standalone: unsupported checkpoint kind {kind}")


# ---------------------------------------------------------------------------
# 官方 devicetest TestCase
# ---------------------------------------------------------------------------


def render_devicetest_checkpoint(checkpoint: CheckpointSpec, *, indent: str = DEFAULT_INDENT) -> list[str]:
    """渲染一个检查点为官方 devicetest TestCase 的代码行。"""
    lines = _devicetest_body(checkpoint)
    if checkpoint.soft:
        return _wrap_soft_devicetest(lines, checkpoint, indent)
    return [f"{indent}{line}" for line in lines]


def _devicetest_body(checkpoint: CheckpointSpec) -> list[str]:
    kind = checkpoint.kind
    message = checkpoint.message_zh

    if kind == CheckpointKind.ELEMENT_EXISTS:
        selector = _selector_or_error(checkpoint, "devicetest")
        return [f"driver.check_component_exist({selector}, expect_exist=True{_wait_suffix(checkpoint)})"]
    if kind == CheckpointKind.ELEMENT_ABSENT:
        selector = _selector_or_error(checkpoint, "devicetest")
        return [f"driver.check_component_exist({selector}, expect_exist=False{_wait_suffix(checkpoint)})"]
    if kind == CheckpointKind.TEXT_EQUALS:
        expected = checkpoint.expected if checkpoint.expected is not None else ""
        if checkpoint.locator is not None and checkpoint.locator.kind in {LocatorKind.KEY, LocatorKind.ID}:
            selector = render_selector(checkpoint.locator)
            body = [f"actual = driver.get_component_property({selector}, 'text')"]
            body.append(f"CHECK({message!r}, {expected!r}, actual)")
            body.append(f"ASSERT({expected!r}, actual)")
            return body
        return [f"driver.check_component_exist(BY.text({expected!r}), expect_exist=True{_wait_suffix(checkpoint)})"]
    if kind == CheckpointKind.TEXT_CONTAINS:
        expected = checkpoint.expected if checkpoint.expected is not None else ""
        return [
            "driver.check_component_exist("
            f"BY.text({expected!r}, MatchPattern.CONTAINS), expect_exist=True{_wait_suffix(checkpoint)})"
        ]
    if kind == CheckpointKind.PROPERTY_EQUALS:
        selector = _selector_or_error(checkpoint, "devicetest")
        prop = checkpoint.property_name
        expected = checkpoint.expected
        body = [f"actual = driver.get_component_property({selector}, {prop!r})"]
        body.append(f"CHECK({message!r}, {expected!r}, actual)")
        body.append(f"ASSERT({expected!r}, actual)")
        return body
    if kind == CheckpointKind.TOAST:
        expected = checkpoint.expected if checkpoint.expected is not None else ""
        timeout = int(checkpoint.wait_seconds or DEFAULT_TOAST_TIMEOUT)
        return [f"driver.check_toast({expected!r}, fuzzy={checkpoint.toast_fuzzy!r}, timeout={timeout})"]
    if kind == CheckpointKind.CURRENT_APP:
        return [
            "bundle, ability = driver.current_app()",
            f"CHECK({message!r}, {checkpoint.expected!r}, bundle)",
            f"ASSERT({checkpoint.expected!r}, bundle)",
        ]
    if kind == CheckpointKind.PAGE_SIGNATURE:
        lines = [f"# page {checkpoint.page_path}".rstrip()]
        for anchor in checkpoint.anchors:
            lines.append(
                f"driver.check_component_exist({render_selector(anchor)}, expect_exist=True, "
                f"wait_time={ANCHOR_WAIT_SECONDS})"
            )
        return lines
    if kind == CheckpointKind.SCREENSHOT_CAPTURED:
        name = str(checkpoint.expected or DEFAULT_SCREENSHOT_NAME)
        return [
            f"_shot = driver.capture_screen(str(self._report_dir() / {name!r}))",
            f"CHECK({message!r}, True, bool(_shot))",
            "ASSERT(True, bool(_shot))",
        ]
    raise CheckpointRenderError(f"devicetest: unsupported checkpoint kind {kind}")


def render_devicetest_step_header(checkpoint: CheckpointSpec, number: int) -> str:
    """``page_signature`` 在官方报告里独占一个 ``Step``。"""
    return f"Step({f'{number}. '}{checkpoint.message_zh!r})"


__all__ = [
    "ANCHOR_WAIT_SECONDS",
    "DEFAULT_INDENT",
    "DEFAULT_SCREENSHOT_NAME",
    "DEFAULT_TOAST_TIMEOUT",
    "CheckpointRenderError",
    "needs_listen_toast",
    "render_devicetest_checkpoint",
    "render_devicetest_step_header",
    "render_standalone_checkpoint",
]
