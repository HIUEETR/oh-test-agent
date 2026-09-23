"""用例 IR 的静态安全门禁（改进计划 C4）。

职责：在用例落盘/执行之前，用确定性规则检查 ``TestCaseSpec``；返回中文违规说明列表，
空列表表示干净。调用方的两种处置方式：

* ``POST /api/cases/stress`` / ``cli stress``：非空 ⇒ HTTP 422（拒绝保存）；
* DC 与 Live builder：非空 ⇒ 不硬失败，只把产出用例降级为 ``status="draft"`` 并记警告。

对应计划 C4 的规则：

1. 步骤 ``text`` / ``locator.target_label`` / ``key`` 与 ``bug_repro.*`` 文案都要过
   ``SafetyPolicy._permanent_match``（永久禁词 + 凭据词）。这里直接实例化 ``SafetyPolicy()``
   复用运行期词表，绝不复制词表；门控词（登录/提交/…）不在本层重查——录制时已由
   ``SafetyPolicy.validate_decision`` / ``DcShellPolicy`` 查过。
2. ``KEY_EVENT`` 只允许 ``ALLOWED_KEY_EVENTS``（与 DC 回放策略一致，``Power`` 等一律拒绝）。
3. 压测边界：``iterations <= max_iterations``、``duration_budget_seconds <= 7200``、
   单步 ``wait_seconds <= 30``；坐标 locator 必须带解析边界与警告（IR 校验器已保证，
   这里作为纵深防御再断言一次，防止构造后被就地改写）。
4. 压测循环体内禁止 ``START_APP`` / ``STOP_APP``（应用生命周期归 setup/teardown）；
   ``NOOP_COMMENT`` / ``SCREENSHOT`` 允许。
5. 发射的 shell 是固定模板 ``hidumper --mem <bundle>``，``bundle_name`` 又被 IR 正则
   ``^[A-Za-z0-9_.]{1,128}$`` 约束，因此不存在注入面，本文件不做任何 shell 转义。

纯函数约定：除「纯坐标压测循环体追加 ``coordinate-risk`` 标签」这一条有意的副作用外，
本文件不修改传入的 spec（见 ``COORDINATE_RISK_TAG``）。
"""

from __future__ import annotations

from collections.abc import Iterator

from ..models import LocatorKind
from ..runtime.safety import SafetyPolicy
from .spec import BugReproSpec, StepAction, TestCaseSpec, TestStepSpec

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

ALLOWED_KEY_EVENTS: tuple[str, ...] = ("Back", "Home", "Enter", "VolumeUp", "VolumeDown")
"""``KEY_EVENT`` 白名单（计划 C4 规则 2，与 DC 回放策略一致）。"""

_ALLOWED_KEY_EVENTS_CASEFOLD: frozenset[str] = frozenset(item.casefold() for item in ALLOWED_KEY_EVENTS)
"""比较用的小写集合；按键比较**大小写不敏感**（``back`` / ``Back`` 等价），文档化以避免歧义。"""

_KEY_EVENT_ALIASES: dict[str, str] = {
    "home": "Home",
    "enter": "Enter",
    "volumeup": "VolumeUp",
    "volume_up": "VolumeUp",
    "volumedown": "VolumeDown",
    "volume_down": "VolumeDown",
}
"""归一化键（已去空格/连字符并小写）→ ``ALLOWED_KEY_EVENTS`` 的规范拼写。"""


def canonical_key_event(value: str) -> str | None:
    """把录到的按键名归一到 ``ALLOWED_KEY_EVENTS`` 的规范拼写；不在白名单返回 ``None``。

    归一化同时吃掉空格与连字符，因为 ``standalone._render_key_event`` 与
    ``xdevice_case`` 的归一化规则历史上并不一致（前者去空格、后者不去）。
    输出规范拼写后，两个 emitter 与 ``validate_case_spec`` 三方同时命中。

    ``back`` / ``backspace`` **故意不在表里**：Back 由调用方映射成 ``StepAction.BACK``
    （渲染 ``driver.go_back()``），不走 KEY_EVENT。
    """
    normalized = (value or "").strip().lower().replace(" ", "").replace("-", "_")
    return _KEY_EVENT_ALIASES.get(normalized)

MAX_DURATION_BUDGET_SECONDS = 7200
"""压测时长预算硬上限（秒），对齐 ``StressSpec.duration_budget_seconds`` 的 ``le=7200``。"""

MAX_STEP_WAIT_SECONDS = 30.0
"""单步等待硬上限（秒），对齐 ``runtime/safety.py`` 的 ``WAIT`` 决策限制。"""

COORDINATE_RISK_TAG = "coordinate-risk"
"""纯坐标压测循环体的风险标签；这是本模块唯一一处有意的 spec 副作用。"""

_LOOP_FORBIDDEN_ACTIONS: frozenset[StepAction] = frozenset({StepAction.START_APP, StepAction.STOP_APP})
"""压测循环体内禁止的动作（应用生命周期归 setup/teardown）。"""


# ---------------------------------------------------------------------------
# 遍历工具
# ---------------------------------------------------------------------------


def _iter_steps(spec: TestCaseSpec) -> Iterator[tuple[str, int, TestStepSpec]]:
    """按确定性顺序遍历全用例步骤，产出 ``(来源标签, 0 基下标, 步骤)``。

    刻意不复用 ``TestCaseSpec._all_steps()``：违规信息需要区分来源（``steps`` /
    ``stress.body_steps`` / setup / teardown），测试与用户提示都依赖这个定位。
    """
    groups: list[tuple[str, list[TestStepSpec]]] = [
        ("setup.pre_steps", list(spec.setup.pre_steps)),
        ("steps", list(spec.steps)),
        ("teardown.post_steps", list(spec.teardown.post_steps)),
    ]
    if spec.stress is not None:
        groups.append(("stress.body_steps", list(spec.stress.body_steps)))
    for label, steps in groups:
        for position, step in enumerate(steps):
            yield label, position, step


def _step_sensitive_strings(step: TestStepSpec) -> Iterator[tuple[str, str]]:
    """产出步骤里需要过永久禁词表的 ``(字段名, 文本)``。"""
    if step.text:
        yield "text", step.text
    if step.locator is not None and step.locator.target_label:
        yield "locator.target_label", step.locator.target_label
    if step.key:
        yield "key", step.key


def _bug_repro_sensitive_strings(bug: BugReproSpec) -> Iterator[tuple[str, str]]:
    """产出 ``bug_repro.*`` 里需要过永久禁词表的 ``(字段名, 文本)``。"""
    yield "bug_repro.symptom", bug.symptom
    yield "bug_repro.expected", bug.expected
    yield "bug_repro.actual", bug.actual
    for position, text in enumerate(bug.preconditions):
        yield f"bug_repro.preconditions[{position}]", text
    for position, text in enumerate(bug.repro_steps_nl):
        yield f"bug_repro.repro_steps_nl[{position}]", text


def _is_coordinate_step(step: TestStepSpec) -> bool:
    """步骤是否「坐标驱动」：坐标 locator、裸 ``coordinate`` 字段，或显式 ``drag_to`` 终点。"""
    if step.coordinate is not None or step.drag_to is not None:
        return True
    return step.locator is not None and step.locator.kind == LocatorKind.COORDINATE


def _step_ref(label: str, position: int, step: TestStepSpec) -> str:
    return f"{label}[{position}]（step_id={step.step_id}）"


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def validate_case_spec(
    spec: TestCaseSpec,
    *,
    policy: SafetyPolicy | None = None,
    max_iterations: int,
) -> list[str]:
    """校验用例 IR 的安全约束，返回中文违规说明列表（空列表 = 干净）。

    ``policy`` 省略时实例化默认 ``SafetyPolicy()``（只为复用永久禁词表，不查门控词）；
    ``max_iterations`` 由调用方传入 ``Settings.stress_max_iterations``（默认 2000）。

    有意的副作用：纯坐标的压测循环体会在 ``spec.tags`` 追加 ``COORDINATE_RISK_TAG``
    （幂等，不重复追加）；除此之外不修改 ``spec``。
    """
    active_policy = policy or SafetyPolicy()
    violations: list[str] = []

    for label, position, step in _iter_steps(spec):
        reference = _step_ref(label, position, step)
        for field_name, text in _step_sensitive_strings(step):
            match = active_policy._permanent_match(text)
            if match:
                violations.append(f"{reference} 的 {field_name} 含永久禁用词「{match}」：{text!r}")
        if step.action == StepAction.KEY_EVENT:
            key = (step.key or "").strip()
            if key.casefold() not in _ALLOWED_KEY_EVENTS_CASEFOLD:
                violations.append(
                    f"{reference} 的 KEY_EVENT 按键 {key or '(空)'} 不在白名单 "
                    f"{list(ALLOWED_KEY_EVENTS)} 内（大小写不敏感比较）"
                )
        if step.wait_seconds is not None and step.wait_seconds > MAX_STEP_WAIT_SECONDS:
            violations.append(
                f"{reference} 的 wait_seconds={step.wait_seconds} 超过单步等待上限 {MAX_STEP_WAIT_SECONDS:g} 秒"
            )
        locator = step.locator
        if locator is not None and locator.kind == LocatorKind.COORDINATE:
            # 纵深防御：IR 校验器已拦过一次，这里再断言，防止构造后被就地改写。
            if locator.resolution_bound is None:
                violations.append(f"{reference} 的坐标 locator 缺少 resolution_bound（无法在别的分辨率回放）")
            if not locator.warning:
                violations.append(f"{reference} 的坐标 locator 缺少 warning 提示")

    if spec.bug_repro is not None:
        for field_name, text in _bug_repro_sensitive_strings(spec.bug_repro):
            match = active_policy._permanent_match(text)
            if match:
                violations.append(f"{field_name} 含永久禁用词「{match}」：{text!r}")

    if spec.stress is not None:
        stress = spec.stress
        if stress.iterations > max_iterations:
            violations.append(f"stress.iterations={stress.iterations} 超过上限 {max_iterations}")
        if stress.duration_budget_seconds is not None and stress.duration_budget_seconds > MAX_DURATION_BUDGET_SECONDS:
            violations.append(
                f"stress.duration_budget_seconds={stress.duration_budget_seconds} "
                f"超过上限 {MAX_DURATION_BUDGET_SECONDS} 秒"
            )
        for position, step in enumerate(stress.body_steps):
            if step.action in _LOOP_FORBIDDEN_ACTIONS:
                violations.append(
                    f"stress.body_steps[{position}]（step_id={step.step_id}）使用了 {step.action.value}，"
                    "压测循环体内禁止应用生命周期动作（归 setup/teardown）"
                )
        if stress.body_steps and all(_is_coordinate_step(step) for step in stress.body_steps):
            if COORDINATE_RISK_TAG not in spec.tags:
                spec.tags.append(COORDINATE_RISK_TAG)

    return violations


__all__ = [
    "ALLOWED_KEY_EVENTS",
    "COORDINATE_RISK_TAG",
    "MAX_DURATION_BUDGET_SECONDS",
    "MAX_STEP_WAIT_SECONDS",
    "canonical_key_event",
    "validate_case_spec",
]
