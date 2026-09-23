"""``TestCaseSpec`` → 官方 xdevice / devicetest 工程。

与独立 ``UiDriver`` 脚本并列的第二个 emitter，产出一棵可直接 ``xdevice run`` 的目录树：

```
xdevice/
  testcases/HarmonyAgentCases/
    testfile.json          # xdevice 用例描述，与发射代码严格一致
    test_case.py           # devicetest.core.test_case.TestCase 子类
  testlist.txt             # 单行：HarmonyAgentCases
  reports/                 # -rp 目标，xdevice 写 <ts>/report/summary_report.html
  logs/                    # -le 本地执行日志
```

设计约束：

1. 生成的代码必须能通过 ``compile()``（``render`` 内部自检，写得出来就一定能编译）。
2. ``Step`` 编号从 1 开始并按发射顺序递增；**每个 IR 步骤与每个独占一个 ``Step`` 的
   检查点**都占用一个序号（比赛要求「明确步骤、断言或检查点」在官方报告里可见）。
   编号按方法独立计数（setup / 测试方法 / teardown 各自从 1 开始）。
3. 中文标题一律来自 ``cases/titles.py``，检查点代码一律来自 ``generation/checkpoints.py``，
   选择器一律来自 ``generation/selectors.py``；本文件不重复实现它们。
4. import 只来自 ``devicetest.core.test_case`` 与 ``hypium``；``loop`` 只在真的发射
   ``@loop`` 装饰器时才导入。
5. 压测有两种形态：纯计数类且无内存采样用 ``@loop`` 装饰器（``test_stress_iteration``）；
   ``SOAK`` 或带内存采样时改用显式 ``for`` + deadline 循环（``test_main``），
   因为装饰器无法表达时长预算与采样节奏。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..cases.spec import (
    NO_REPLAYABLE_COMMENT,
    CheckpointSpec,
    StepAction,
    StressSpec,
    TestCaseSpec,
    TestStepSpec,
)
from ..cases.titles import DEFAULT_LONG_PRESS_SECONDS, checkpoint_title_zh, display_label, step_title_zh
from ..models import LocatorKind, StressKind
from .checkpoints import (
    DEFAULT_INDENT,
    DEFAULT_SCREENSHOT_NAME,
    needs_listen_toast,
    render_devicetest_checkpoint,
)
from .selectors import render_coordinate, render_selector

# ---------------------------------------------------------------------------
# 常量（xdevice 工程契约）
# ---------------------------------------------------------------------------

XDEVICE_SUITE_NAME = "HarmonyAgentCases"
XDEVICE_CASE_CLASS = "HarmonyAgentCase"
#: Phase 0 真机 spike（2026-09-21，devicetest 6.1.0.210）实测：devicetest 要求
#: **用例文件名与 TestCase 类名一致**，否则以 ``Script-0203016
#: The file name of the test case must be the same as the class name`` 直接 FAILED。
#: 因此文件名必须由类名派生，而不是计划假设的 ``test_case.py``。
XDEVICE_CASE_FILE = f"{XDEVICE_CASE_CLASS}.py"
XDEVICE_MAIN_METHOD = "test_main"
XDEVICE_STRESS_METHOD = "test_stress_iteration"

XDEVICE_TESTS_DIR = "testcases"
XDEVICE_TESTFILE_JSON = "testfile.json"
XDEVICE_TESTLIST_FILE = "testlist.txt"
XDEVICE_REPORTS_DIR = "reports"
XDEVICE_LOGS_DIR = "logs"

#: ``testfile.json`` 里 ``driver.type`` 的取值。
XDEVICE_DRIVER_TYPE = "DeviceTest"

#: IR ``key_event`` 白名单 → ``hypium.KeyCode`` 数值（见计划 A9；Power 等一律拒绝）。
_KEY_CODES: dict[str, tuple[int, str]] = {
    "back": (2, "BACK"),
    "home": (1, "HOME"),
    "enter": (2054, "ENTER"),
    "volumeup": (16, "VOLUME_UP"),
    "volume_up": (16, "VOLUME_UP"),
    "volumedown": (17, "VOLUME_DOWN"),
    "volume_down": (17, "VOLUME_DOWN"),
}

#: 生成文件里的标准库 import 行（与计划 A6 模板逐字对齐）。
_GENERATED_STDLIB_IMPORTS = ("import json, os, re, time", "from pathlib import Path")


class XDeviceRenderError(ValueError):
    """IR 无法渲染为官方 devicetest 用例（例如缺定位器的动作）。"""


# ---------------------------------------------------------------------------
# 产物描述
# ---------------------------------------------------------------------------


@dataclass
class XDeviceArtifact:
    """一棵已落盘的 xdevice 工程目录。"""

    root: Path
    suite: str
    case_file: Path
    testfile_path: Path
    testlist_path: Path
    reports_dir: Path
    logs_dir: Path
    python_text: str
    testfile: dict
    class_name: str
    method: str
    timeout_seconds: int


# ---------------------------------------------------------------------------
# 源码文本工具
# ---------------------------------------------------------------------------


def _py_str(text: str) -> str:
    """渲染 Python 字符串字面量；优先双引号（与计划模板一致），必要时回落到 ``repr``。"""
    if any(char in text for char in ('"', "\\", "\n", "\r", "\t")):
        return repr(text)
    return f'"{text}"'


def _py_float(value: float) -> str:
    """渲染浮点字面量（``3`` → ``3.0``）。"""
    return repr(float(value))


def _py_number(value: float) -> str:
    """渲染数值字面量：整数不带小数点（``press_time=2``），小数保留原样（``1.5``）。"""
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return repr(number)


def _docstring(text: str) -> str:
    """让文本可以安全地嵌进三引号文档字符串（转义反斜杠与所有双引号）。"""
    return text.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# 方法体累积器
# ---------------------------------------------------------------------------


class _MethodBody:
    """按方法累积代码行，并维护 ``Step`` 编号计数器（每个方法独立从 1 开始）。"""

    def __init__(self, header: str, *, indent: str = DEFAULT_INDENT):
        self.lines: list[str] = [header]
        self.indent = indent
        self.number = 0

    def step(self, title: str) -> None:
        """发射一个带序号的 ``Step``，并把计数器推进一位。"""
        self.number += 1
        marker = f"{self.number}. {title}"
        self.lines.append(f"{self.indent}Step({_py_str(marker)})")

    def raw(self, line: str) -> None:
        """发射一行原始代码（空行不缩进）。"""
        self.lines.append(f"{self.indent}{line}" if line else "")


@dataclass(frozen=True)
class _EmitContext:
    """渲染动作行时需要的用例级上下文。"""

    bundle_name: str
    defaults: dict[str, Any]


# ---------------------------------------------------------------------------
# 动作渲染
# ---------------------------------------------------------------------------


def _target(step: TestStepSpec) -> tuple[str, str] | None:
    """返回 ``(表达式, 行尾注释)``；无法定位时返回 ``None``。

    坐标定位器（或只有 ``coordinate`` 的步骤）走 hypium 的坐标兜底，并带上
    ``# coordinate fallback for ...`` 注释，方便在官方报告里审计降级原因。
    """
    if step.locator is not None and step.locator.kind != LocatorKind.COORDINATE:
        return render_selector(step.locator), ""
    if step.locator is not None and step.locator.coordinate is not None:
        return render_selector(step.locator), _coordinate_comment(step)
    if step.coordinate is not None:
        return render_coordinate(step.coordinate), _coordinate_comment(step)
    return None


def _coordinate_comment(step: TestStepSpec) -> str:
    label = display_label(step.locator) or step.step_id
    return f"  # coordinate fallback for {_py_str(label)}"


def _text_expression(step: TestStepSpec, context: _EmitContext) -> str:
    """输入文本的字面量，或数据驱动参数读取表达式。"""
    if step.param_ref:
        default = context.defaults.get(step.param_ref)
        return f"param({step.param_ref!r}, {default!r})"
    return _py_str(step.text or "")


def _action_lines(step: TestStepSpec, context: _EmitContext) -> list[str]:
    """把一个 IR 步骤渲染为若干行 devicetest 代码（不含缩进）。"""
    action = step.action

    if action in {StepAction.CLICK, StepAction.DOUBLE_CLICK, StepAction.LONG_CLICK}:
        target = _target(step)
        if target is None:
            return [f"# skipped: {action} (no locator or coordinate)"]
        expression, comment = target
        if action == StepAction.CLICK:
            if step.optional:
                # 条件步骤：控件状态条件存在（真机复盘 dc-20260922T171655Z-6fff3547），
                # 命中才点，未命中不算失败。检查点仍然照常硬判。
                return [
                    "# 条件步骤：控件状态条件存在，未命中即跳过",
                    f"_conditional_target = driver.find_component({expression})",
                    "if _conditional_target is not None:",
                    f"    driver.touch(_conditional_target){comment}",
                ]
            return [f"driver.touch({expression}){comment}"]
        if action == StepAction.DOUBLE_CLICK:
            return [f"driver.double_click({expression}){comment}"]
        press_time = step.wait_seconds if step.wait_seconds is not None else DEFAULT_LONG_PRESS_SECONDS
        return [f"driver.long_click({expression}, press_time={_py_number(press_time)}){comment}"]

    if action == StepAction.INPUT_TEXT:
        target = _target(step)
        if target is None:
            return ["# skipped: input_text (no locator or coordinate)"]
        expression, comment = target
        return [f"driver.input_text({expression}, {_text_expression(step, context)}){comment}"]

    if action == StepAction.CLEAR_TEXT:
        target = _target(step)
        if target is None:
            return ["# skipped: clear_text (no locator or coordinate)"]
        expression, comment = target
        return [f"driver.clear_text({expression}){comment}"]

    if action == StepAction.SWIPE:
        if step.start is not None and step.end is not None:
            # 计划 5.5：显式起止坐标渲染为 driver.slide（xdevice 与 hypium 同款 API）。
            start = render_coordinate(step.start)
            end = render_coordinate(step.end)
            if step.slide_time is not None:
                return [f"driver.slide({start}, {end}, slide_time={float(step.slide_time)!r})"]
            return [f"driver.slide({start}, {end})"]
        return [f"driver.swipe({(step.direction or 'up').upper()!r})"]

    if action == StepAction.FLING:
        return [f"driver.fling({(step.direction or 'up').upper()!r})"]

    if action == StepAction.DRAG:
        target = _target(step)
        if target is None or step.drag_to is None:
            return ["# skipped: drag (no start point or drag_to)"]
        expression, comment = target
        return [f"driver.drag({expression}, {render_coordinate(step.drag_to)}){comment}"]

    if action == StepAction.BACK:
        return ["driver.go_back()"]

    if action == StepAction.KEY_EVENT:
        key_code = _KEY_CODES.get((step.key or "").replace("-", "_").lower())
        if key_code is None:
            return [f"# skipped: key_event {_py_str(step.key or '')} is not in the key whitelist"]
        code, member = key_code
        return [f"driver.press_key({code})  # KeyCode.{member}"]

    if action == StepAction.WAIT:
        seconds = step.wait_seconds if step.wait_seconds is not None else 1.0
        return [f"driver.wait({_py_float(seconds)})"]

    if action == StepAction.SCREENSHOT:
        name = step.text or DEFAULT_SCREENSHOT_NAME
        return [f"driver.capture_screen(str(self._report_dir() / {_py_str(name)}))"]

    if action == StepAction.SWITCH_STATUS:
        target = _target(step)
        if target is None:
            return ["# skipped: switch_status (no locator or coordinate)"]
        expression, comment = target
        checked = True if step.checked is None else bool(step.checked)
        return [f"driver.switch_component_status({expression}, checked={checked}){comment}"]

    if action == StepAction.START_APP:
        return [f"# start_app: {context.bundle_name} (handled by case setup)"]

    if action == StepAction.STOP_APP:
        return ["driver.stop_app(BUNDLE_NAME)"]

    if action == StepAction.CHECK:
        label = display_label(step.locator) or step.step_id
        return [f"# check action: {label}"]

    if action == StepAction.NOOP_COMMENT:
        return [f"pass  # {step.comment or NO_REPLAYABLE_COMMENT}"]

    raise XDeviceRenderError(f"unsupported step action {action}")


# ---------------------------------------------------------------------------
# 循环形态
# ---------------------------------------------------------------------------


def uses_explicit_loop(stress: StressSpec) -> bool:
    """``SOAK`` 或内存采样只能靠显式 ``for``/deadline 循环表达，``@loop`` 做不到。"""
    return stress.kind == StressKind.SOAK or stress.needs_explicit_loop


def resolve_method(spec: TestCaseSpec) -> str:
    """压测用例的测试方法名：计数类走 ``@loop``，其余走显式循环的 ``test_main``。"""
    if spec.stress is not None and not uses_explicit_loop(spec.stress):
        return XDEVICE_STRESS_METHOD
    return XDEVICE_MAIN_METHOD


def _iter_checkpoints(spec: TestCaseSpec) -> list[CheckpointSpec]:
    """用例里所有检查点（含压测循环体与每轮检查点）。"""
    found: list[CheckpointSpec] = []
    for group in (spec.setup.pre_steps, spec.steps, spec.teardown.post_steps):
        for step in group:
            found.extend(step.checkpoints)
    if spec.stress is not None:
        for step in spec.stress.body_steps:
            found.extend(step.checkpoints)
        found.extend(spec.stress.per_iteration_checkpoints)
    return found


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------


class XDeviceEmitter:
    """把 ``TestCaseSpec`` 渲染为一个可直接 ``xdevice run`` 的工程目录。"""

    def render(self, spec: TestCaseSpec, root: Path, *, version: int = 1) -> XDeviceArtifact:
        """写出整棵 xdevice 工程目录并返回产物描述。"""
        root = Path(root)
        suite_dir = root / XDEVICE_TESTS_DIR / XDEVICE_SUITE_NAME
        reports_dir = root / XDEVICE_REPORTS_DIR
        logs_dir = root / XDEVICE_LOGS_DIR
        for directory in (suite_dir, reports_dir, logs_dir):
            directory.mkdir(parents=True, exist_ok=True)

        method = resolve_method(spec)
        python_text = self._render_python(spec, version=version, method=method)
        case_file = suite_dir / XDEVICE_CASE_FILE
        compile(python_text, str(case_file), "exec")

        testfile = self._testfile(spec, version=version, method=method)
        testfile_path = suite_dir / XDEVICE_TESTFILE_JSON
        testlist_path = root / XDEVICE_TESTLIST_FILE

        case_file.write_text(python_text, encoding="utf-8")
        testfile_path.write_text(json.dumps(testfile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        testlist_path.write_text(XDEVICE_SUITE_NAME + "\n", encoding="utf-8")

        return XDeviceArtifact(
            root=root,
            suite=XDEVICE_SUITE_NAME,
            case_file=case_file,
            testfile_path=testfile_path,
            testlist_path=testlist_path,
            reports_dir=reports_dir,
            logs_dir=logs_dir,
            python_text=python_text,
            testfile=testfile,
            class_name=XDEVICE_CASE_CLASS,
            method=method,
            timeout_seconds=int(spec.timeout_seconds),
        )

    # -- testfile.json -----------------------------------------------------

    @staticmethod
    def _testfile(spec: TestCaseSpec, *, version: int, method: str) -> dict:
        """xdevice 用例描述；``tests[0]`` 必须与发射的类/方法逐字一致。

        Phase 0 真机 spike（2026-09-21）实测：``driver.py_file`` 与
        ``tests[].testfile`` 都是相对 ``-tcpath`` 的路径，因此必须带上
        ``<suite>/`` 这一层，否则 xdevice 解析出 0 个用例
        （``Test Summary: ... total: 0, unavailable: 2``）。
        """
        relative_case = f"{XDEVICE_SUITE_NAME}/{XDEVICE_CASE_FILE}"
        return {
            "description": f"harmony_test_agent generated case {spec.case_id} v{version} ({spec.scenario})",
            "environment": [],
            "kits": [],
            "driver": {
                "type": XDEVICE_DRIVER_TYPE,
                "py_file": [relative_case],
                "timeout": int(spec.timeout_seconds),
            },
            "tests": [
                {
                    "name": method,
                    "testfile": f"./{relative_case}",
                    "class": XDEVICE_CASE_CLASS,
                    "suite": XDEVICE_SUITE_NAME,
                }
            ],
        }

    # -- test_case.py ------------------------------------------------------

    def _render_python(self, spec: TestCaseSpec, *, version: int, method: str) -> str:
        """渲染完整 ``test_case.py`` 源码。"""
        stress = spec.stress
        decorated = method == XDEVICE_STRESS_METHOD
        defaults = {item.name: item.default for item in spec.params}
        context = _EmitContext(bundle_name=spec.bundle_name, defaults=defaults)

        lines = self._module_header(spec, version=version)
        lines.append("from __future__ import annotations")
        lines.append("")
        lines.extend(_GENERATED_STDLIB_IMPORTS)
        lines.append("")
        lines.append(f"from devicetest.core.test_case import {self._devicetest_import(use_loop=decorated)}")
        lines.append("from hypium import BY, MatchPattern, UiDriver")
        lines.append("")
        lines.extend(self._module_constants(spec, stress=stress))
        lines.append("")
        lines.append("")
        if any(step.param_ref for step in self._emitted_steps(spec)):
            lines.extend(_PARAM_HELPER_LINES)
            lines.append("")
            lines.append("")
        lines.extend(self._class_header(spec))
        lines.extend(_CLASS_INIT_LINES)
        lines.append("")
        lines.extend(_DEVICE_SN_LINES)
        lines.append("")
        lines.extend(_REPORT_DIR_LINES)
        lines.append("")
        if stress is not None and uses_explicit_loop(stress) and stress.sample_memory_every > 0:
            lines.extend(_SAMPLE_PSS_LINES)
            lines.append("")
        lines.extend(self._class_setup(spec, context))
        lines.append("")
        if decorated:
            lines.extend(self._class_decorated_stress(spec, stress, context))
        elif stress is not None:
            lines.extend(self._class_soak_main(spec, stress, context))
        else:
            lines.extend(self._class_main(spec, context))
        lines.append("")
        lines.extend(self._class_teardown(spec, context))
        return "\n".join(lines) + "\n"

    @staticmethod
    def _devicetest_import(*, use_loop: bool) -> str:
        names = ["ASSERT", "CHECK", "MESSAGE", "Step", "TestCase"]
        if use_loop:
            names.append("loop")
        return ", ".join(names)

    @staticmethod
    def _emitted_steps(spec: TestCaseSpec) -> list[TestStepSpec]:
        steps = list(spec.setup.pre_steps) + list(spec.steps) + list(spec.teardown.post_steps)
        if spec.stress is not None:
            steps += list(spec.stress.body_steps)
        return steps

    @staticmethod
    def _module_header(spec: TestCaseSpec, *, version: int) -> list[str]:
        provenance = spec.provenance
        title = f"Auto-generated by harmony_test_agent ({provenance.generator_version}). DO NOT EDIT."
        source = f"{provenance.source_kind}:{provenance.source_id}"
        profile = provenance.profile_target_app_id or "-"
        return [
            "# -*- coding: utf-8 -*-",
            f'"""{_docstring(title)}',
            "",
            f"case_id: {_docstring(spec.case_id)}  version: {version}  scenario: {spec.scenario}",
            f"source: {_docstring(source)}   profile: {_docstring(profile)}",
            '"""',
        ]

    @staticmethod
    def _module_constants(spec: TestCaseSpec, *, stress: StressSpec | None) -> list[str]:
        defaults = json.dumps({item.name: item.default for item in spec.params}, ensure_ascii=False)
        lines = [
            f"BUNDLE_NAME = {_py_str(spec.bundle_name)}",
            f"MAIN_ABILITY = {_py_str(spec.main_ability)}",
            f"STARTUP_WAIT_SECONDS = {_py_float(spec.setup.startup_wait_seconds)}",
            f"DEFAULT_DEVICE_SN = {_py_str(spec.device_sn or '')}",
            "PARAMS = {**" + defaults + ', **json.loads(os.environ.get("HARMONY_AGENT_CASE_PARAMS", "{}") or "{}")}',
        ]
        if stress is None:
            return lines
        lines.extend(
            [
                f"ITERATIONS = {int(stress.iterations)}",
                f"DEADLINE_SECONDS = {_py_float(stress.duration_budget_seconds or 0)}",
                f"MEMORY_SAMPLE_EVERY = {int(stress.sample_memory_every)}",
                f"MEMORY_GROWTH_THRESHOLD_KB = {int(stress.memory_growth_threshold_kb or 0)}",
                f"INTER_ITERATION_WAIT_SECONDS = {_py_float(stress.inter_iteration_wait_seconds)}",
                f"STEP_LOG_INTERVAL = {int(stress.step_log_interval)}",
            ]
        )
        return lines

    @staticmethod
    def _class_header(spec: TestCaseSpec) -> list[str]:
        return [
            f"class {XDEVICE_CASE_CLASS}(TestCase):",
            f'    """{_docstring(spec.title_zh)}"""',
            "",
        ]

    def _class_setup(self, spec: TestCaseSpec, context: _EmitContext) -> list[str]:
        body = _MethodBody("    def setup(self):")
        body.raw("self.driver = UiDriver.connect(device_sn=self._device_sn())")
        if spec.setup.stop_app_first:
            body.raw("self.driver.stop_app(BUNDLE_NAME)")
        if spec.setup.start_app:
            body.raw("self.driver.start_app(BUNDLE_NAME, MAIN_ABILITY)")
        listen_toast = spec.setup.listen_toast or any(needs_listen_toast(cp) for cp in _iter_checkpoints(spec))
        if listen_toast:
            body.raw("self.driver.start_listen_toast()")
        body.raw("self.driver.wait(STARTUP_WAIT_SECONDS)")
        if spec.setup.pre_steps:
            body.raw("driver = self.driver")
            self._emit_steps(body, spec.setup.pre_steps, context)
        return body.lines

    def _class_main(self, spec: TestCaseSpec, context: _EmitContext) -> list[str]:
        body = _MethodBody(f"    def {XDEVICE_MAIN_METHOD}(self):")
        body.raw("driver = self.driver")
        self._emit_steps(body, spec.steps, context)
        self._emit_final_screenshot(body, spec)
        return body.lines

    def _class_decorated_stress(
        self,
        spec: TestCaseSpec,
        stress: StressSpec,
        context: _EmitContext,
    ) -> list[str]:
        body = _MethodBody(
            f"    @loop(times=ITERATIONS, fail_break={stress.fail_break}, "
            f"fail_times={int(stress.fail_times)}, continues_fail={stress.continues_fail})",
        )
        body.lines.append(f"    def {XDEVICE_STRESS_METHOD}(self):")
        body.raw("driver = self.driver")
        self._emit_steps(
            body,
            stress.body_steps,
            context,
            trailing_checkpoints=stress.per_iteration_checkpoints,
        )
        self._emit_final_screenshot(body, spec)
        return body.lines

    def _class_soak_main(self, spec: TestCaseSpec, stress: StressSpec, context: _EmitContext) -> list[str]:
        body = _MethodBody(f"    def {XDEVICE_MAIN_METHOD}(self):")
        body.raw("driver = self.driver")
        body.raw("started = time.monotonic()")
        body.raw("previous_pss_kb = None")
        body.raw("for _iteration in range(1, ITERATIONS + 1):")
        outer = body.indent
        inner = outer + "    "
        body.indent = inner
        body.raw("if DEADLINE_SECONDS > 0 and time.monotonic() - started >= DEADLINE_SECONDS:")
        body.indent = inner + "    "
        body.raw('MESSAGE(f"soak deadline reached after {_iteration - 1} iterations")')
        body.raw("break")
        body.indent = inner
        body.raw("if _iteration % STEP_LOG_INTERVAL == 0:")
        body.indent = inner + "    "
        body.raw('Step(f"第 {_iteration} 轮")')
        body.indent = inner
        self._emit_steps(
            body,
            stress.body_steps,
            context,
            trailing_checkpoints=stress.per_iteration_checkpoints,
        )
        if stress.sample_memory_every > 0:
            body.raw("if MEMORY_SAMPLE_EVERY > 0 and _iteration % MEMORY_SAMPLE_EVERY == 0:")
            sample = inner + "    "
            body.indent = sample
            body.raw("current_pss_kb = self._sample_pss(driver)")
            body.raw("if (")
            body.raw("    previous_pss_kb is not None")
            body.raw("    and MEMORY_GROWTH_THRESHOLD_KB > 0")
            body.raw("    and current_pss_kb - previous_pss_kb > MEMORY_GROWTH_THRESHOLD_KB")
            body.raw("):")
            body.indent = sample + "    "
            body.raw("raise AssertionError(")
            body.raw('    f"PSS grew by {current_pss_kb - previous_pss_kb} KB after {_iteration} iterations"')
            body.raw(")")
            body.indent = sample
            body.raw("previous_pss_kb = current_pss_kb")
            body.indent = inner
        if stress.inter_iteration_wait_seconds > 0:
            body.raw(f"time.sleep({_py_float(stress.inter_iteration_wait_seconds)})")
        body.indent = outer
        self._emit_final_screenshot(body, spec)
        return body.lines

    def _class_teardown(self, spec: TestCaseSpec, context: _EmitContext) -> list[str]:
        body = _MethodBody("    def teardown(self):")
        body.raw("driver = self.driver")
        body.raw("if driver is not None:")
        body.indent = DEFAULT_INDENT + "    "
        self._emit_steps(body, spec.teardown.post_steps, context)
        if spec.teardown.capture_final_screenshot and self._capture_in_teardown(spec):
            body.raw(f"driver.capture_screen(str(self._report_dir() / {_py_str(DEFAULT_SCREENSHOT_NAME)}))")
        if spec.teardown.stop_app:
            body.raw("driver.stop_app(BUNDLE_NAME)")
        body.raw("try:")
        body.indent = DEFAULT_INDENT + "        "
        body.raw("driver.close()")
        body.indent = DEFAULT_INDENT + "    "
        body.raw("except Exception:")
        body.indent = DEFAULT_INDENT + "        "
        body.raw("pass")
        return body.lines

    @staticmethod
    def _capture_in_teardown(spec: TestCaseSpec) -> bool:
        """``@loop`` 压测的测试方法会被执行 N 次，末次截图只能放 teardown 做一次。"""
        return spec.stress is not None and not uses_explicit_loop(spec.stress)

    def _emit_final_screenshot(self, body: _MethodBody, spec: TestCaseSpec) -> None:
        if spec.teardown.capture_final_screenshot and not self._capture_in_teardown(spec):
            body.raw(f"driver.capture_screen(str(self._report_dir() / {_py_str(DEFAULT_SCREENSHOT_NAME)}))")

    def _emit_steps(
        self,
        body: _MethodBody,
        steps: list[TestStepSpec],
        context: _EmitContext,
        *,
        trailing_checkpoints: list[CheckpointSpec] | None = None,
    ) -> None:
        """按「步骤 Step → 动作 → 检查点 Step → 断言」的顺序发射，并共用同一套编号。"""
        for step in steps:
            body.step(step_title_zh(step))
            for line in _action_lines(step, context):
                body.raw(line)
            for checkpoint in step.checkpoints:
                self._emit_checkpoint(body, checkpoint)
        for checkpoint in trailing_checkpoints or ():
            self._emit_checkpoint(body, checkpoint)

    @staticmethod
    def _emit_checkpoint(body: _MethodBody, checkpoint: CheckpointSpec) -> None:
        """每个检查点独占一个 ``Step``，随后是渲染好的断言代码。"""
        body.step(checkpoint_title_zh(checkpoint))
        body.lines.extend(render_devicetest_checkpoint(checkpoint, indent=body.indent))


#: 数据驱动参数的读取助手；仅当用例真的用到 ``param_ref`` 时才发射。
_PARAM_HELPER_LINES = [
    "def param(name, default=None):",
    '    """读取数据驱动参数：``HARMONY_AGENT_CASE_PARAMS`` 覆盖 IR 里的默认值。"""',
    "    return PARAMS.get(name, default)",
]

_CLASS_INIT_LINES = [
    "    def __init__(self, tag, configs=None):",
    "        # Phase 0 真机 spike 实测：devicetest 驱动只用 1 个位置参数实例化用例类，",
    "        # 因此 configs 必须有默认值，否则以 Script-0203002 直接 FAILED。",
    "        super().__init__(tag, configs or {})",
    "        self.driver = None",
]

#: ``_device_sn`` 优先级：env ``HARMONY_AGENT_DEVICE_SN`` > ``self.device1.device_sn`` > 常量。
_DEVICE_SN_LINES = [
    "    def _device_sn(self):",
    '        env_sn = os.environ.get("HARMONY_AGENT_DEVICE_SN", "")',
    "        if env_sn:",
    "            return env_sn",
    '        device_sn = getattr(self.device1, "device_sn", "") if self.device1 is not None else ""',
    "        return device_sn or DEFAULT_DEVICE_SN",
]

_REPORT_DIR_LINES = [
    "    def _report_dir(self) -> Path:",
    "        try:",
    "            return Path(self.get_case_report_path())",
    "        except Exception:",
    '            return Path(os.environ.get("HARMONY_AGENT_REPORT_DIR", "."))',
]

#: 压测采内存：``hidumper --mem`` 的 Total Pss 累加；解析失败返回 0（不误报增长）。
_SAMPLE_PSS_LINES = [
    "    def _sample_pss(self, driver) -> int:",
    '        """采样设备总 PSS（KB）；任何异常都退化为 0，避免把采样失败当成内存增长。"""',
    "        try:",
    '            output = driver.shell("hidumper --mem")',
    "        except Exception:",
    "            return 0",
    "        total = 0",
    r'        for match in re.finditer(r"Total\s+Pss:\s*(\d+)", output or ""):',
    "            total += int(match.group(1))",
    "        return total",
]


__all__ = [
    "XDEVICE_CASE_CLASS",
    "XDEVICE_CASE_FILE",
    "XDEVICE_LOGS_DIR",
    "XDEVICE_MAIN_METHOD",
    "XDEVICE_REPORTS_DIR",
    "XDEVICE_STRESS_METHOD",
    "XDEVICE_SUITE_NAME",
    "XDEVICE_TESTFILE_JSON",
    "XDEVICE_TESTLIST_FILE",
    "XDEVICE_TESTS_DIR",
    "XDeviceArtifact",
    "XDeviceEmitter",
    "XDeviceRenderError",
    "resolve_method",
    "uses_explicit_loop",
]
