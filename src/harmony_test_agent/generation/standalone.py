"""``TestCaseSpec`` → 数据驱动的独立 ``UiDriver`` 脚本。

产物契约（**必须保留**，``runner/hypium.py`` 与 Profile 晋级门禁依赖）：

* ``generated_result.json`` 至少有 ``{run_id, passed, error[, traceback]}``，新增 key 只追加
* ``REPORT_DIR`` 由 ``HARMONY_AGENT_REPORT_DIR`` 决定，末次截图写 ``final.jpeg``
* 失败时额外写 ``failure.jpeg``
* 裸调用 ``python <script>`` 必须可用（``--params`` 走 ``parse_known_args``）

与历史模板的差异（全部为**追加**）：``CASE_ID`` / ``DEFAULT_PARAMS`` / ``param()`` /
``soft_failures`` / ``stress`` 统计。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..cases.spec import (
    NO_REPLAYABLE_COMMENT,
    CaseProvenance,
    CheckpointKind,
    CheckpointSpec,
    LocatorKind,
    ScenarioKind,
    SetupSpec,
    StepAction,
    StressSpec,
    TestCaseSpec,
    TestStepSpec,
)
from .checkpoints import DEFAULT_INDENT, needs_listen_toast, render_standalone_checkpoint
from .selectors import render_coordinate, render_selector

#: 独立脚本产物里的常量名（与 IR 字段一一对应）。
ITERATIONS_CONST = "ITERATIONS"
DEADLINE_CONST = "DEADLINE_SECONDS"
MEMORY_SAMPLE_CONST = "MEMORY_SAMPLE_EVERY"
MEMORY_THRESHOLD_CONST = "MEMORY_GROWTH_THRESHOLD_KB"
STEP_LOG_INTERVAL_CONST = "STEP_LOG_INTERVAL"
INTER_ITERATION_WAIT_CONST = "INTER_ITERATION_WAIT"

#: ``KEY_EVENT`` 的按键名 → hypium ``KeyCode`` 成员；``back`` 直接走 ``go_back()``。
KEYCODE_BY_KEY: dict[str, str] = {
    "home": "HOME",
    "enter": "ENTER",
    "volumeup": "VOLUME_UP",
    "volume_up": "VOLUME_UP",
    "volumedown": "VOLUME_DOWN",
    "volume_down": "VOLUME_DOWN",
}


@dataclass
class StandaloneArtifact:
    """独立脚本产物：源码文本 + 与 ``test_<safe>.json`` 同 schema 的配置。"""

    python_text: str
    config: dict[str, Any] = field(default_factory=dict)


class StandaloneEmitter:
    """把用例 IR 渲染为可独立执行的 ``UiDriver`` 脚本。"""

    # -- 对外入口 ---------------------------------------------------------

    @staticmethod
    def shell_spec(
        *,
        bundle_name: str,
        main_ability: str,
        startup_wait_seconds: float,
        device_sn: str | None,
        title_zh: str,
    ) -> TestCaseSpec:
        """构造一个仅承载文档头部信息的空壳用例（供兼容薄壳复用同一模板）。"""
        from ..cases.builder import new_case_id, slugify

        return TestCaseSpec(
            case_id=new_case_id(),
            slug=slugify(title_zh),
            title_zh=title_zh[:200] or "未命名用例",
            scenario=ScenarioKind.SMOKE,
            status="draft",
            bundle_name=bundle_name,
            main_ability=main_ability,
            device_sn=device_sn,
            setup=SetupSpec(startup_wait_seconds=startup_wait_seconds),
            steps=[TestStepSpec(step_id="shell", index=1, action=StepAction.NOOP_COMMENT, title_zh="空壳")],
            provenance=CaseProvenance(source_kind="manual", source_id="shell"),
        )

    def render(self, spec: TestCaseSpec, *, run_id: str, device_id: str) -> StandaloneArtifact:
        """渲染完整脚本与配置。"""
        body = self.render_body(spec)
        text = self.render_document(spec, body, run_id=run_id, device_id=device_id)
        return StandaloneArtifact(python_text=text, config=self.render_config(spec, run_id=run_id, device_id=device_id))

    def render_document(
        self,
        spec: TestCaseSpec,
        body: list[str],
        *,
        run_id: str,
        device_id: str,
    ) -> str:
        """用给定脚本体渲染完整脚本文档（兼容薄壳也走同一条模板）。"""
        return _document(spec, body, run_id=run_id, device_id=device_id)

    def render_config(self, spec: TestCaseSpec, *, run_id: str, device_id: str) -> dict[str, Any]:
        """IR 侧可见的配置键；调用方可用既有键覆盖以保持历史 schema。"""
        return {
            "case_id": spec.case_id,
            "case_slug": spec.slug,
            "scenario": str(spec.scenario),
            "case_status": spec.status,
            "target_app_id": spec.provenance.profile_target_app_id,
            "bundle_name": spec.bundle_name,
            "main_ability": spec.main_ability,
            "device_id": device_id,
            "timeout_seconds": spec.timeout_seconds,
            "startup_wait_seconds": spec.setup.startup_wait_seconds,
            "generated_from_run_id": run_id,
            "source_kind": spec.provenance.source_kind,
            "source_id": spec.provenance.source_id,
            "step_count": len(spec.steps),
            "hard_checkpoint_count": sum(
                1 for step in spec.steps for checkpoint in step.checkpoints if not checkpoint.soft
            ),
            "tags": list(spec.tags),
        }

    # -- 脚本体 -----------------------------------------------------------

    def render_body(self, spec: TestCaseSpec) -> list[str]:
        """渲染 ``main()`` 内的语句块（含压测循环）。"""
        if spec.stress is not None:
            return self._stress_body(spec, spec.stress)
        lines: list[str] = []
        numbered = _numbered(spec)
        lines += self._steps_block(numbered)
        if spec.teardown.capture_final_screenshot:
            lines.append('        driver.capture_screen(str(REPORT_DIR / "final.jpeg"))')
        if spec.teardown.post_steps:
            lines += self._steps_block(_numbered_steps(spec.teardown.post_steps, start=_next_number(numbered)))
        if spec.teardown.stop_app:
            lines.append("        driver.stop_app(BUNDLE_NAME)")
        return lines

    def _steps_block(self, numbered: list[tuple[int, TestStepSpec]]) -> list[str]:
        """按 ``(序号, 步骤)`` 渲染块；``NOOP_COMMENT`` 只发射注释行、不占序号。"""
        lines: list[str] = []
        for number, step in numbered:
            if step.action == StepAction.NOOP_COMMENT:
                if step.comment == NO_REPLAYABLE_COMMENT:
                    lines.append("        pass  # no replayable operations")
                elif not step.comment:
                    # 与历史模板一致：脚本体为空时发射裸 ``pass``。
                    lines.append("        pass")
                else:
                    lines.append(f"        # {step.comment}")
                continue
            lines.append(f"        # 步骤 {number}：{step.title_zh}")
            lines += self._render_action(step)
            for checkpoint in step.checkpoints:
                lines += render_standalone_checkpoint(checkpoint, indent=DEFAULT_INDENT)
        return lines

    def _render_action(self, step: TestStepSpec) -> list[str]:
        action = step.action
        selector = render_selector(step.locator) if step.locator is not None else None

        if action == StepAction.CLICK:
            if step.locator is not None and step.locator.kind == LocatorKind.COORDINATE:
                return [f"        driver.touch({selector}){_coordinate_comment(step)}"]
            if selector is None:
                raise ValueError(f"step {step.step_id!r} of action click requires a locator or coordinate")
            return [f"        driver.touch({selector})"]
        if action == StepAction.DOUBLE_CLICK:
            return [f"        driver.double_click({_selector_or_point(step)})"]
        if action == StepAction.LONG_CLICK:
            press_time = step.wait_seconds if step.wait_seconds is not None else 2.0
            return [f"        driver.long_click({_selector_or_point(step)}, press_time={press_time!r})"]
        if action == StepAction.INPUT_TEXT:
            return [f"        driver.input_text({_selector_or_point(step)}, {_text_expression(step)})"]
        if action == StepAction.CLEAR_TEXT:
            return [f"        driver.clear_text({_selector_or_point(step)})"]
        if action == StepAction.SWIPE:
            return [f"        driver.swipe({(step.direction or 'up').upper()!r})"]
        if action == StepAction.FLING:
            return [f"        driver.fling({(step.direction or 'up').upper()!r})"]
        if action == StepAction.DRAG:
            start = _selector_or_point(step)
            end = render_coordinate(step.drag_to) if step.drag_to is not None else "None"
            return [f"        driver.drag({start}, {end})"]
        if action == StepAction.BACK:
            return ["        driver.go_back()"]
        if action == StepAction.KEY_EVENT:
            return _render_key_event(step)
        if action == StepAction.WAIT:
            return [f"        driver.wait({float(step.wait_seconds if step.wait_seconds is not None else 1)!r})"]
        if action == StepAction.SCREENSHOT:
            name = step.text or "step.jpeg"
            return [f"        driver.capture_screen(str(REPORT_DIR / {name!r}))"]
        if action == StepAction.SWITCH_STATUS:
            return [
                f"        driver.switch_component_status({_selector_or_point(step)}, checked={bool(step.checked)!r})"
            ]
        if action == StepAction.START_APP:
            return ["        driver.start_app(BUNDLE_NAME, MAIN_ABILITY)"]
        if action == StepAction.STOP_APP:
            return ["        driver.stop_app(BUNDLE_NAME)"]
        if action == StepAction.CHECK:
            return []
        raise ValueError(f"unsupported step action {action} in the standalone emitter")

    # -- 压测循环 ---------------------------------------------------------

    def _stress_body(self, spec: TestCaseSpec, stress: StressSpec) -> list[str]:
        lines: list[str] = [
            '        stress_stats = {"iterations_completed": 0, "memory_samples": []}',
            f"        deadline = time.monotonic() + {DEADLINE_CONST} if {DEADLINE_CONST} else None",
            f"        for iteration in range(1, {ITERATIONS_CONST} + 1):",
            "            if deadline is not None and time.monotonic() > deadline:",
            "                break",
            f"            if {STEP_LOG_INTERVAL_CONST} > 0 and iteration % {STEP_LOG_INTERVAL_CONST} == 0:",
            '                print(f"stress iteration {iteration}")',
        ]
        # 循环体：步骤与检查点整体缩进到 for 内部
        body: list[str] = []
        body += self._steps_block(_numbered_steps(stress.body_steps, start=1))
        for checkpoint in stress.per_iteration_checkpoints:
            body += render_standalone_checkpoint(checkpoint, indent=DEFAULT_INDENT)
        lines += ["    " + line if line.strip() else line for line in body]
        lines += [
            f"            if {MEMORY_SAMPLE_CONST} and iteration % {MEMORY_SAMPLE_CONST} == 0:",
            "                pss = _sample_pss(driver, iteration)",
            "                if pss is not None:",
            '                    stress_stats["memory_samples"].append({"iteration": iteration, "pss_kb": pss})',
            '            stress_stats["iterations_completed"] = iteration',
            f"            driver.wait({INTER_ITERATION_WAIT_CONST})",
            '        result["stress"] = stress_stats',
            f"        if {MEMORY_THRESHOLD_CONST} is not None and len(stress_stats['memory_samples']) >= 2:",
            "            _first = stress_stats['memory_samples'][0]['pss_kb']",
            "            _last = stress_stats['memory_samples'][-1]['pss_kb']",
            f"            if _last - _first > {MEMORY_THRESHOLD_CONST}:",
            "                raise RuntimeError(",
            '                    f"memory growth {_last - _first} KB exceeds threshold "',
            f'                    f"{{{MEMORY_THRESHOLD_CONST}}} KB"',
            "                )",
        ]
        if spec.teardown.capture_final_screenshot:
            lines.append('        driver.capture_screen(str(REPORT_DIR / "final.jpeg"))')
        return lines


# ---------------------------------------------------------------------------
# 单步渲染辅助
# ---------------------------------------------------------------------------


def _numbered(spec: TestCaseSpec) -> list[tuple[int, TestStepSpec]]:
    """``setup.pre_steps`` + ``steps`` 的连续编号视图。"""
    combined = list(spec.setup.pre_steps) + list(spec.steps)
    return _numbered_steps(combined, start=1)


def _next_number(numbered: list[tuple[int, TestStepSpec]]) -> int:
    """下一个可用序号（``NOOP_COMMENT`` 不占号）。"""
    highest = 0
    for number, step in numbered:
        if step.action != StepAction.NOOP_COMMENT:
            highest = max(highest, number)
    return highest + 1


def _numbered_steps(steps: list[TestStepSpec], start: int = 1) -> list[tuple[int, TestStepSpec]]:
    """给步骤编号；``NOOP_COMMENT`` 不占序号（它本身就是注释行）。"""
    numbered: list[tuple[int, TestStepSpec]] = []
    number = start
    for step in steps:
        if step.action == StepAction.NOOP_COMMENT:
            numbered.append((number, step))
            continue
        numbered.append((number, step))
        number += 1
    return numbered


def _coordinate_comment(step: TestStepSpec) -> str:
    label = (step.locator.target_label if step.locator is not None else "") or ""
    return f"  # coordinate fallback for {label!r}" if label else "  # coordinate fallback"


def _selector_or_point(step: TestStepSpec) -> str:
    if step.locator is not None:
        return render_selector(step.locator)
    if step.coordinate is not None:
        return render_coordinate(step.coordinate)
    raise ValueError(f"step {step.step_id!r} requires a locator or a coordinate")


def _text_expression(step: TestStepSpec) -> str:
    if step.param_ref:
        default = step.text or ""
        return f"param({step.param_ref!r}, {default!r})"
    return repr(step.text or "")


def _render_key_event(step: TestStepSpec) -> list[str]:
    key = (step.key or "").strip()
    normalized = key.lower().replace(" ", "").replace("-", "_")
    if normalized in {"back", "backspace"}:
        return ["        driver.go_back()"]
    member = KEYCODE_BY_KEY.get(normalized)
    if member is None:
        return [f"        # unsupported key_event: {key}"]
    return [f"        driver.press_key(KeyCode.{member})"]


def _keycode_needed(spec: TestCaseSpec) -> bool:
    steps = _all_spec_steps(spec)
    return any(
        step.action == StepAction.KEY_EVENT
        and (step.key or "").strip().lower().replace(" ", "").replace("-", "_") not in {"back", "backspace"}
        for step in steps
    )


def _all_spec_steps(spec: TestCaseSpec) -> list[TestStepSpec]:
    steps = list(spec.setup.pre_steps) + list(spec.steps) + list(spec.teardown.post_steps)
    if spec.stress is not None:
        steps += list(spec.stress.body_steps)
    return steps


def _all_checkpoints(spec: TestCaseSpec) -> list[CheckpointSpec]:
    found: list[CheckpointSpec] = []
    for step in _all_spec_steps(spec):
        found += list(step.checkpoints)
    if spec.stress is not None:
        found += list(spec.stress.per_iteration_checkpoints)
    return found


def _needs_assert(spec: TestCaseSpec) -> bool:
    """产物是否需要 ``Assert``：``current_app`` / ``selected`` 属性 / 截图三类检查点才有。"""
    for checkpoint in _all_checkpoints(spec):
        if checkpoint.kind in {CheckpointKind.SCREENSHOT_CAPTURED, CheckpointKind.CURRENT_APP}:
            return True
        if checkpoint.kind == CheckpointKind.PROPERTY_EQUALS:
            return True
    return False


def _needs_params(spec: TestCaseSpec) -> bool:
    return bool(spec.params) or any(step.param_ref for step in _all_spec_steps(spec))


def _uses_toast(spec: TestCaseSpec) -> bool:
    return any(needs_listen_toast(checkpoint) for checkpoint in _all_checkpoints(spec))


# ---------------------------------------------------------------------------
# 文档模板
# ---------------------------------------------------------------------------


def _document(spec: TestCaseSpec, body: list[str], *, run_id: str, device_id: str) -> str:
    """渲染脚本文档；头部与 ``generated_result.json`` 契约与历史模板兼容。"""
    startup_wait = spec.setup.startup_wait_seconds
    stress = spec.stress
    statements = "\n".join(body) or "        pass"

    imports = ["import json", "import os", "import traceback"]
    if _needs_params(spec):
        imports.insert(0, "import argparse")
    if stress is not None:
        imports += ["import re", "import time"]
    imports = sorted(set(imports))
    hypium_imports = "from hypium import BY, MatchPattern, UiDriver"
    if _keycode_needed(spec):
        hypium_imports = "from hypium import BY, KeyCode, MatchPattern, UiDriver"
    extra_imports = ["from hypium.checker.assertion import Assert"] if _needs_assert(spec) else []

    defaults = {item.name: item.default for item in spec.params}
    setup_lines = _setup_lines(spec)
    stress_constants = _stress_constants(stress)
    helpers = _helper_functions(stress, needs_params=_needs_params(spec))

    parts: list[str] = []
    parts.append("from __future__ import annotations\n")
    parts.append("\n".join(imports) + "\n")
    parts.append("from pathlib import Path\n")
    parts.append("")
    parts.append(hypium_imports)
    parts.extend(extra_imports)
    parts.append("")
    parts.append(f'DEVICE_ID = os.environ.get("HARMONY_AGENT_DEVICE_SN", {device_id!r})')
    parts.append(f"BUNDLE_NAME = {spec.bundle_name!r}")
    parts.append(f"MAIN_ABILITY = {spec.main_ability!r}")
    parts.append(f"STARTUP_WAIT_SECONDS = {startup_wait!r}")
    parts.append(f"CASE_ID = {spec.case_id!r}")
    if stress_constants:
        parts.extend(stress_constants)
    parts.append(f"DEFAULT_PARAMS = {json.dumps(defaults, ensure_ascii=False)}")
    parts.append('DEFAULT_REPORT_DIR = Path(__file__).resolve().parent.parent / "reports"')
    parts.append('REPORT_DIR = Path(os.environ.get("HARMONY_AGENT_REPORT_DIR", DEFAULT_REPORT_DIR))')
    parts.append('RESULT_PATH = REPORT_DIR / "generated_result.json"')
    if helpers:
        parts.append("")
        parts.append("")
        parts.append("\n\n".join(helpers))
    parts.append("")
    parts.append("")
    parts.append("def main() -> int:")
    parts.append("    REPORT_DIR.mkdir(parents=True, exist_ok=True)")
    parts.append(
        "    result = {"
        f'"run_id": {run_id!r}, "passed": False, "error": None, '
        f'"case_id": CASE_ID, "soft_failures": []'
        "}"
    )
    parts.append("    driver = None")
    parts.append("    try:")
    parts.append(
        '        driver = UiDriver.connect(device_sn=DEVICE_ID, report_path=str(REPORT_DIR), log_level="info")'
    )
    parts.extend(setup_lines)
    parts.append(statements)
    parts.append('        result["passed"] = True')
    parts.append("        return 0")
    parts.append("    except Exception as exc:")
    parts.append('        result["error"] = {"type": type(exc).__name__, "message": str(exc)}')
    parts.append('        result["traceback"] = traceback.format_exc()')
    parts.append("        if driver is not None:")
    parts.append("            try:")
    parts.append('                driver.capture_screen(str(REPORT_DIR / "failure.jpeg"))')
    parts.append("            except Exception:")
    parts.append("                pass")
    parts.append("        return 1")
    parts.append("    finally:")
    parts.append('        RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")')
    parts.append("        if driver is not None:")
    parts.append("            try:")
    parts.append("                driver.close()")
    parts.append("            except Exception:")
    parts.append("                pass")
    parts.append("")
    parts.append("")
    parts.append('if __name__ == "__main__":')
    parts.append("    raise SystemExit(main())")
    return "\n".join(parts) + "\n"


def _setup_lines(spec: TestCaseSpec) -> list[str]:
    """确定性初始化：停应用 → 启动 → 等待（→ 可选 toast 监听）。"""
    setup = spec.setup
    lines: list[str] = []
    if setup.stop_app_first:
        lines.append("        driver.stop_app(BUNDLE_NAME)")
    if setup.start_app:
        lines.append("        driver.start_app(BUNDLE_NAME, MAIN_ABILITY)")
    lines.append("        driver.wait(STARTUP_WAIT_SECONDS)")
    if _uses_toast(spec):
        lines.append("        driver.start_listen_toast()")
    return lines


def _stress_constants(stress: StressSpec | None) -> list[str]:
    if stress is None:
        return []
    threshold = stress.memory_growth_threshold_kb
    return [
        f"{ITERATIONS_CONST} = {stress.iterations!r}",
        f"{DEADLINE_CONST} = {float(stress.duration_budget_seconds or 0)!r}",
        f"{MEMORY_SAMPLE_CONST} = {stress.sample_memory_every!r}",
        f"{MEMORY_THRESHOLD_CONST} = {threshold!r}",
        f"{STEP_LOG_INTERVAL_CONST} = {stress.step_log_interval!r}",
        f"{INTER_ITERATION_WAIT_CONST} = {stress.inter_iteration_wait_seconds!r}",
    ]


def _helper_functions(stress: StressSpec | None, *, needs_params: bool) -> list[str]:
    helpers: list[str] = []
    if needs_params:
        helpers.append(
            "def _load_params() -> dict:\n"
            "    parser = argparse.ArgumentParser(add_help=False)\n"
            '    parser.add_argument("--params", default=os.environ.get("HARMONY_AGENT_CASE_PARAMS", "{}"))\n'
            "    parsed, _ = parser.parse_known_args()\n"
            "    try:\n"
            '        overrides = json.loads(parsed.params or "{}")\n'
            "    except json.JSONDecodeError:\n"
            "        overrides = {}\n"
            "    return {**DEFAULT_PARAMS, **(overrides if isinstance(overrides, dict) else {})}\n"
            "\n"
            "\n"
            "PARAMS = _load_params()\n"
            "\n"
            "\n"
            "def param(name, default=None):\n"
            "    return PARAMS.get(name, default)"
        )
    if stress is not None:
        helpers.append(
            "def _sample_pss(driver, iteration: int):\n"
            '    """采集一次被测应用的 PSS（KB）；解析失败返回 None，原始输出永远归档。"""\n'
            "    try:\n"
            '        output = driver.shell(f"hidumper --mem {BUNDLE_NAME}")\n'
            "    except Exception:\n"
            "        return None\n"
            "    try:\n"
            '        (REPORT_DIR / f"mem_{iteration}.txt").write_text(str(output), encoding="utf-8")\n'
            "    except Exception:\n"
            "        pass\n"
            '    matches = re.findall(r"Total\\s+Pss\\D*(\\d+)", str(output))\n'
            '    matches = matches or re.findall(r"(\\d+)\\s*KB", str(output))\n'
            "    if not matches:\n"
            "        return None\n"
            "    try:\n"
            "        return int(matches[-1])\n"
            "    except (TypeError, ValueError):\n"
            "        return None"
        )
    return helpers


__all__ = [
    "DEADLINE_CONST",
    "INTER_ITERATION_WAIT_CONST",
    "ITERATIONS_CONST",
    "KEYCODE_BY_KEY",
    "MEMORY_SAMPLE_CONST",
    "MEMORY_THRESHOLD_CONST",
    "STEP_LOG_INTERVAL_CONST",
    "StandaloneArtifact",
    "StandaloneEmitter",
]
