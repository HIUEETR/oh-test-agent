"""把四种来源归一化为用例 IR。

来源：
* Live 运行轨迹 ``RunTrace.actions``（复刻 ``generation/hypium.py`` 的逐分支语义）
* DC 会话录制 ``DcToolInvocation``（复刻 ``dc/generator.py`` 的逐分支语义）
* 压测请求 ``StressRequest``（见 ``cases/stress.py`` 的调用方）
* 缺陷报告 ``BugReproPlan``（见 ``cases/bug_repro.py``）

**兼容性契约**：omit reason 字符串与 ``counts`` 的 key 必须与历史实现一字不差
（``tests/unit/test_generation.py`` / ``tests/unit/test_dc_generator.py`` 做精确子串断言）。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from ..models import (
    ActionResult,
    LocatorCandidate,
    LocatorKind,
    RunState,
    RunTrace,
    ScenarioKind,
    ScreenSnapshot,
    TargetAppProfile,
    ToolName,
    UIElement,
    utc_now,
)
from .spec import (
    NO_REPLAYABLE_COMMENT,
    CaseProvenance,
    CheckpointKind,
    CheckpointSpec,
    LocatorEvidence,
    LocatorSpec,
    MatchMode,
    SetupSpec,
    StepAction,
    TeardownSpec,
    TestCaseSpec,
    TestStepSpec,
)
from .titles import checkpoint_title_zh

# Hypium Driver 的 swipe 只接受大写方向枚举（RIGHT/LEFT/UP/DOWN）。
SWIPE_DIRECTIONS = frozenset({"UP", "DOWN", "LEFT", "RIGHT"})

#: ``SWIPE`` 推断警告的方向顺序：保证同一输入的警告列表稳定可断言。
SWIPE_DIRECTION_ORDER = ("UP", "DOWN", "LEFT", "RIGHT")

#: DC 侧「不可回放」工具（观测/管理/shell）；用字符串值表达，避免在模块导入期
#: 触发 ``harmony_test_agent.dc`` 包初始化（``dc/__init__`` 会 import 本模块）。
#: ``dc/generator.py`` 把同一集合投影回 ``DcToolName`` 供既有契约测试使用。
NON_REPLAYABLE_DC_TOOLS: frozenset[str] = frozenset(
    {
        "screenshot",
        "inspect_screen",
        "dump_ui_hierarchy",
        "collect_logs",
        "foreground_app",
        "list_apps",
        "inspect_app",
        "memory_dump",
        "force_stop_app",
        "install_app",
        "uninstall_app",
        "clear_app_data",
        "file_send",
        "file_recv",
        "file_list",
        "execute_shell",
    }
)

#: 动态 key 前缀泛化的历史正则（``generation/hypium.py``）。
#:
#: 真机复盘（run-20260921T053514Z-8418044b 的回放失败）：鸿蒙的实例 key 既可能是
#: ``feed_card_20240101``（下划线分隔），也可能是 ``add_agenda_title-1789969034729``
#: （连字符 + 毫秒时间戳）。旧正则只认下划线，后者会被当成稳定 key 原样写进脚本，
#: 回放时必然 ``Can't find component with [BY.key(...)]``。
_DYNAMIC_LOCATOR = re.compile(r"(.+?[_-])\d{8,}")


def _dynamic_pattern_variants(prefix: str, value: str) -> set[str]:
    """前缀泛化的等价写法：``foo-``/``foo_`` 与收割侧记录的 ``foo-#`` 必须互相认得。

    任务期证据回收（``orchestrator._stable_locator_from_candidate``）把动态标识折叠为
    ``<prefix>#`` 存进 ``dynamic_pattern``；而选择器渲染需要的是可直接用于 starts_with 的
    裸前缀，因此比较时同时接受这两种写法。
    """
    return {prefix, re.sub(r"\d+", "#", value)}


#: DC 占位应用身份。
PLACEHOLDER_BUNDLE = "com.example.app"
PLACEHOLDER_ABILITY = "EntryAbility"

#: 坐标兜底在未知分辨率时的占位边界（仍带警告，绝不静默）。
UNKNOWN_RESOLUTION_BOUND = (0, 0)

#: 兜底检查点的中文说明。
FALLBACK_CHECKPOINT_MESSAGE = "检查点：应可见一个已观察到的稳定元素"

#: ``confidence`` 三档中判为 low 的质量因素前缀。
LOW_CONFIDENCE_PREFIXES: tuple[str, ...] = (
    "source agent outcome is failed",
    "source trace contains failed actions",
    "source trace does not end with a successful FINISH action",
)

ConfidenceLevel = Literal["high", "medium", "low"]


def evaluate_runnable(
    *,
    included_actions: int,
    bundle_name: str,
    main_ability: str,
) -> tuple[bool, list[str]]:
    """可执行性判定：**只有 2 条物理必要条件**。

    任何质量、验证、Profile 状态相关的顾虑都不得进入这里——它们属于 confidence 层
    （``evaluate_confidence``）或晋级层（``_promotion_blockers``）。缺任一条的脚本在物理上
    根本无法运行：没有可回放动作，或应用身份还是占位值。

    **身份占位只认 ``bundle_name``**：``EntryAbility`` 是鸿蒙工程的默认且常见的**真实**
    ability 名——本案 ``com.github.zhuoyi233.zhplus / EntryAbility`` 就是真实身份，把它当成
    占位会让默认路径下的真实脚本永远不可执行（计划的 G1 与 Phase 4 验收都要求它可执行）。
    因此这里只在 bundle 缺失/等于哨兵 ``com.example.app``，或 ability 为空（脚本连启动参数都
    没有）时判为不可执行。
    """
    blockers: list[str] = []
    if included_actions <= 0:
        blockers.append("script has no replayable action")
    if not bundle_name or bundle_name == PLACEHOLDER_BUNDLE or not main_ability:
        blockers.append(f"app identity is a placeholder ({PLACEHOLDER_BUNDLE}/{PLACEHOLDER_ABILITY})")
    return not blockers, blockers


def evaluate_confidence(factors: list[str], *, outcome: str) -> ConfidenceLevel:
    """按质量因素把脚本分到 high/medium/low 三档（**非阻断**，只影响徽章与排序）。"""
    if outcome == "failed" or any(factor.startswith(LOW_CONFIDENCE_PREFIXES) for factor in factors):
        return "low"
    return "medium" if factors else "high"


@dataclass
class CaseBuildResult:
    """一次 IR 构建的产物与全部审计信息。"""

    spec: TestCaseSpec
    omitted_actions: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    incomplete_reasons: list[str] = field(default_factory=list)
    """.. deprecated:: 与 ``confidence_factors`` 同值的兼容别名（旧 JSON 产物 / 旧 API 消费方）。"""
    replay_eligible: bool = False
    """脚本是否可执行（runnable）；质量看 ``confidence``，晋级看 ``promotion_eligible``。"""
    purpose: Literal["acceptance", "diagnostic"] = "diagnostic"
    explicit_assertions: int = 0
    source_agent_outcome: Literal["completed", "failed", "stopped", "unknown"] = "unknown"
    confidence: ConfidenceLevel = "low"
    confidence_factors: list[str] = field(default_factory=list)
    promotion_eligible: bool = False
    promotion_blockers: list[str] = field(default_factory=list)
    runnable_blockers: list[str] = field(default_factory=list)


def new_case_id() -> str:
    """生成 ``case-<UTC 时间戳>-<6 位十六进制>`` 形式的用例 ID。"""
    return f"case-{utc_now():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def _swipe_points_from_params(params: dict[str, Any]) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    """从动作参数里读出显式滑动起止坐标（计划 5.5），非法输入一律忽略。"""

    def point(value: Any) -> tuple[int, int] | None:
        if isinstance(value, (list, tuple)) and len(value) == 2:
            try:
                return (int(value[0]), int(value[1]))
            except TypeError, ValueError:
                return None
        return None

    return point(params.get("start")), point(params.get("end"))


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str, *, fallback: str = "case") -> str:
    """把中文/任意文本压成 ``[a-z0-9-]`` 的 slug；纯中文会退化为 fallback + 短哈希。"""
    ascii_part = _SLUG_STRIP.sub("-", text.lower()).strip("-")
    ascii_part = ascii_part[:48].strip("-")
    if not ascii_part:
        ascii_part = f"{fallback}-{uuid.uuid4().hex[:6]}"
    return ascii_part[:64].strip("-") or "case"


def format_args(args: dict[str, Any]) -> str:
    """把工具参数格式化为短字符串（``dc/generator.py::_format_args`` 的等价实现）。"""
    if not args:
        return ""
    parts = [f"{key}={value!r}" for key, value in args.items() if value is not None]
    text = ", ".join(parts)
    return text[:120] + "..." if len(text) > 120 else text


class CaseBuilder:
    """把轨迹 / DC 录制 / 压测 / 缺陷报告构建为 ``TestCaseSpec``。"""

    def __init__(self, *, min_observed_rounds: int = 3, inject_fallback_assertion: bool = True):
        self.min_observed_rounds = max(int(min_observed_rounds), 1)
        self.inject_fallback_assertion = inject_fallback_assertion

    # ------------------------------------------------------------------
    # Live 运行轨迹
    # ------------------------------------------------------------------

    def from_trace(
        self,
        trace: RunTrace,
        profile: TargetAppProfile,
        *,
        case_id: str | None = None,
        slug: str | None = None,
        scenario: ScenarioKind = ScenarioKind.CORE_FLOW,
    ) -> CaseBuildResult:
        """把 Live 轨迹构建为用例 IR（逐分支复刻 ``HypiumGenerator._render_actions``）。"""
        warnings: list[str] = []
        omitted: list[dict[str, str]] = []
        steps: list[TestStepSpec] = []
        generated_actions = 0
        generated_assertions = 0
        explicit_assertions = 0
        coordinate_fallbacks = 0

        def omit(action: ActionResult, reason: str) -> None:
            omitted.append({"step_id": action.step_id, "tool": str(action.tool), "reason": reason})

        def add_step(action: StepAction, **kwargs: Any) -> TestStepSpec:
            step = TestStepSpec(
                step_id=kwargs.pop("step_id", f"step-{len(steps) + 1}"),
                index=len(steps) + 1,
                action=action,
                title_zh="",
                **kwargs,
            )
            steps.append(step)
            return step

        def attach(checkpoint: CheckpointSpec) -> None:
            """断言挂到前一个步骤；无前置步骤时新建一个纯 CHECK 步骤。"""
            checkpoint.message_zh = checkpoint.message_zh or checkpoint_title_zh(checkpoint)
            if steps:
                steps[-1].checkpoints.append(checkpoint)
            else:
                add_step(StepAction.CHECK, checkpoints=[checkpoint], locator=checkpoint.locator)

        for action in trace.actions:
            if not action.success:
                omit(action, action.error or "source action failed")
                continue
            tool = action.tool
            if tool == ToolName.OPEN_APP:
                omit(action, "OPEN_APP is replaced by deterministic stop/start/wait setup")
            elif tool in {ToolName.INSPECT_SCREEN, ToolName.FINISH}:
                omit(action, f"{tool} is an agent-control action")
            elif tool == ToolName.CLICK_ELEMENT and self._is_nondeterministic_system_click(trace, action):
                omit(action, "desktop AppIcon launch click is replaced by deterministic app setup")
            elif tool == ToolName.CLICK_ELEMENT:
                target = action.params.get("target")
                coordinate = self._runtime_element_coordinate(trace, action)
                recovered = self._recorded_key_locator(trace, action, action.locator)
                if recovered is not None:
                    add_step(
                        StepAction.CLICK,
                        step_id=action.step_id,
                        locator=self.locator_from_candidate(recovered, target, profile, warnings),
                    )
                    warnings.append(
                        f"{action.step_id}: runtime locator recovered as "
                        f"{recovered.kind}:{recovered.value!r} from the recorded frame"
                    )
                elif (
                    action.locator and action.locator.kind in {LocatorKind.SPATIAL, LocatorKind.VLM_BBOX} and coordinate
                ):
                    add_step(
                        StepAction.CLICK,
                        step_id=action.step_id,
                        coordinate=coordinate,
                        locator=self.coordinate_locator(
                            coordinate,
                            bound=self._snapshot_bound(trace, action.before_snapshot_id) or self._trace_bound(trace),
                            label=str(target or ""),
                            warning=f"{action.step_id}: runtime element {target!r} uses coordinate {coordinate}",
                        ),
                    )
                    warnings.append(f"{action.step_id}: runtime element {target!r} uses coordinate {coordinate}")
                    coordinate_fallbacks += 1
                else:
                    locator = self.locator_from_candidate(action.locator, target, profile, warnings)
                    add_step(StepAction.CLICK, step_id=action.step_id, locator=locator)
                generated_actions += 1
            elif tool == ToolName.CLICK_COORDINATE:
                raw = action.params.get("coordinate")
                point = tuple(raw) if raw else (0, 0)
                add_step(
                    StepAction.CLICK,
                    step_id=action.step_id,
                    coordinate=(int(point[0]), int(point[1])),
                    locator=self.coordinate_locator(
                        (int(point[0]), int(point[1])),
                        bound=self._snapshot_bound(trace, action.before_snapshot_id) or self._trace_bound(trace),
                        label="",
                        warning=f"{action.step_id}: click uses a coordinate fallback",
                    ),
                )
                warnings.append(f"{action.step_id}: click uses a coordinate fallback")
                coordinate_fallbacks += 1
                generated_actions += 1
            elif tool == ToolName.INPUT_TEXT:
                target = action.params.get("target") or "输入框"
                locator = self.locator_from_candidate(action.locator, target, profile, warnings)
                add_step(
                    StepAction.INPUT_TEXT,
                    step_id=action.step_id,
                    locator=locator,
                    text=str(action.params.get("text", "")),
                )
                generated_actions += 1
            elif tool == ToolName.SWIPE:
                direction = str(action.params.get("direction") or "up").upper()
                if direction not in SWIPE_DIRECTIONS:
                    warnings.append(
                        f"{action.step_id}: unknown swipe direction {action.params.get('direction')!r}, fallback to UP"
                    )
                    direction = "UP"
                start_point, end_point = _swipe_points_from_params(action.params)
                add_step(
                    StepAction.SWIPE,
                    step_id=action.step_id,
                    direction=direction.lower(),  # type: ignore[arg-type]
                    start=start_point,
                    end=end_point,
                )
                generated_actions += 1
            elif tool == ToolName.BACK:
                add_step(StepAction.BACK, step_id=action.step_id)
                generated_actions += 1
            elif tool == ToolName.WAIT:
                add_step(
                    StepAction.WAIT,
                    step_id=action.step_id,
                    wait_seconds=float(action.params.get("wait_seconds") or 1),
                )
                generated_actions += 1
            elif tool == ToolName.ASSERT_VISIBLE:
                target = action.params.get("target") or action.params.get("text")
                locator = self.locator_from_candidate(action.locator, target, profile, warnings)
                attach(CheckpointSpec(kind=CheckpointKind.ELEMENT_EXISTS, message_zh="", locator=locator))
                generated_assertions += 1
                explicit_assertions += 1
            elif tool == ToolName.ASSERT_TEXT:
                # 修正历史 bug：ASSERT_TEXT 过去渲染为 check_component_exist，根本没有校验文本。
                expected = action.params.get("text") or action.params.get("target") or ""
                locator = self.locator_from_candidate(action.locator, action.params.get("target"), profile, warnings)
                if locator is not None and locator.kind not in {LocatorKind.KEY, LocatorKind.ID}:
                    locator = None
                if locator is not None:
                    locator = self._align_text_assertion_locator(
                        trace, action, locator, str(expected), profile, warnings
                    )
                # 只有 KEY/ID 钉死了具体控件时才用精确 ``text=``；否则用包含匹配，
                # 与运行时 ``evaluate_assertion`` 的 target_variants 模糊匹配保持一致，
                # 避免生成脚本比录制时更严格而在回放中抖动失败。
                attach(
                    CheckpointSpec(
                        kind=CheckpointKind.TEXT_EQUALS if locator is not None else CheckpointKind.TEXT_CONTAINS,
                        message_zh="",
                        locator=locator,
                        expected=str(expected),
                    )
                )
                generated_assertions += 1
                explicit_assertions += 1
            elif tool == ToolName.ASSERT_NOT_VISIBLE:
                locator = self.locator_from_candidate(action.locator, action.params.get("target"), profile, warnings)
                attach(CheckpointSpec(kind=CheckpointKind.ELEMENT_ABSENT, message_zh="", locator=locator))
                generated_assertions += 1
                explicit_assertions += 1
            else:
                omit(action, f"unsupported replay tool: {tool}")

        if explicit_assertions == 0 and self.inject_fallback_assertion:
            warnings.append("source trace has no successful explicit assertion")
            stable = self._first_stable_locator(trace)
            if stable is not None:
                locator = stable
                add_step(
                    StepAction.CHECK,
                    step_id="fallback-assertion",
                    locator=locator,
                    checkpoints=[
                        CheckpointSpec(
                            kind=CheckpointKind.ELEMENT_EXISTS,
                            message_zh=FALLBACK_CHECKPOINT_MESSAGE,
                            locator=locator.model_copy(deep=True),
                        )
                    ],
                )
                warnings.append("generated a fallback assertion from an observed stable locator")
                generated_assertions += 1
            else:
                warnings.append("no stable UI assertion was available")

        if not steps:
            # 历史模板在脚本体为空时发射裸 ``pass``；IR 的「非压测至少一步」不变式
            # 用一条空注释步骤承载它（emitter 对空注释渲染为 ``pass``）。
            add_step(StepAction.NOOP_COMMENT, step_id="empty-body", comment="")

        counts = {
            "source_actions": len(trace.actions),
            "source_assertions": len(trace.assertions),
            "generated_actions": generated_actions,
            "generated_assertions": generated_assertions,
            "omitted_actions": len(omitted),
            "failed_actions": sum(not action.success for action in trace.actions),
            "coordinate_fallbacks": coordinate_fallbacks,
        }
        outcome = self._agent_outcome(trace)
        confidence_factors = self._confidence_factors(
            trace,
            outcome,
            explicit_assertions=explicit_assertions,
            omitted=omitted,
            warnings=warnings,
        )
        promotion_blockers = self._promotion_blockers(trace)
        included_actions = counts["generated_actions"] + counts["generated_assertions"]
        replay_eligible, runnable_blockers = evaluate_runnable(
            included_actions=included_actions,
            bundle_name=profile.bundle_name,
            main_ability=profile.main_ability,
        )
        confidence = evaluate_confidence(confidence_factors, outcome=outcome)
        spec = self._assemble(
            steps=steps,
            profile=profile,
            trace=trace,
            case_id=case_id,
            slug=slug,
            scenario=scenario,
            bundle_name=profile.bundle_name,
            main_ability=profile.main_ability,
            device_sn=trace.device_id,
            startup_wait=float(profile.launch_strategy.get("wait_seconds", 2)),
            source_kind="live_run",
            source_id=trace.run_id,
        )
        return CaseBuildResult(
            spec=spec,
            omitted_actions=omitted,
            warnings=warnings,
            counts=counts,
            incomplete_reasons=confidence_factors,  # 兼容别名：与 confidence_factors 同值
            confidence_factors=confidence_factors,
            confidence=confidence,
            replay_eligible=replay_eligible,
            runnable_blockers=runnable_blockers,
            promotion_eligible=replay_eligible and not promotion_blockers,
            promotion_blockers=promotion_blockers,
            purpose="acceptance" if replay_eligible else "diagnostic",
            explicit_assertions=explicit_assertions,
            source_agent_outcome=outcome,  # type: ignore[arg-type]
        )

    # ------------------------------------------------------------------
    # DC 会话录制
    # ------------------------------------------------------------------

    def from_dc_invocations(
        self,
        session_id: str,
        device_id: str,
        invocations: list[Any],
        *,
        bundle_name: str,
        main_ability: str,
        snapshots: list[ScreenSnapshot] | None = None,
        case_id: str | None = None,
        slug: str | None = None,
    ) -> CaseBuildResult:
        """把 DC 会话录制构建为用例 IR（复刻 ``dc/generator.py`` 的逐分支语义）。

        ``invocations`` 的元素类型是 ``dc.models.DcToolInvocation``；此处故意用 ``Any``
        并在函数体内延迟 import，避免 ``cases`` ↔ ``dc`` 的包级循环依赖。
        """
        from ..dc.models import DcToolName  # 延迟 import：避免 harmony_test_agent.dc 包初始化成环

        non_replayable = NON_REPLAYABLE_DC_TOOLS
        assert_tools = {DcToolName.ASSERT_VISIBLE, DcToolName.ASSERT_NOT_VISIBLE, DcToolName.ASSERT_TEXT}

        warnings: list[str] = []
        omitted: list[dict[str, str]] = []
        steps: list[TestStepSpec] = []
        swipe_inferred: dict[str, int] = {}
        included_count = 0
        bundle_for_bounds = None if not snapshots else (snapshots[0].width, snapshots[0].height)

        def add_step(action: StepAction, **kwargs: Any) -> TestStepSpec:
            step = TestStepSpec(
                step_id=kwargs.pop("step_id", f"step-{len(steps) + 1}"),
                index=len(steps) + 1,
                action=action,
                title_zh="",
                **kwargs,
            )
            steps.append(step)
            return step

        def attach(checkpoint: CheckpointSpec) -> None:
            checkpoint.message_zh = checkpoint.message_zh or checkpoint_title_zh(checkpoint)
            if steps:
                steps[-1].checkpoints.append(checkpoint)
            else:
                add_step(StepAction.CHECK, checkpoints=[checkpoint], locator=checkpoint.locator)

        for invocation in invocations:
            if not invocation.success:
                omitted.append(
                    {
                        "invocation_id": invocation.invocation_id,
                        "tool": invocation.tool.value,
                        "reason": "invocation failed",
                    }
                )
                continue

            if invocation.tool in non_replayable:
                omitted.append(
                    {
                        "invocation_id": invocation.invocation_id,
                        "tool": invocation.tool.value,
                        "reason": "not replayable in Hypium (observation/management/shell)",
                    }
                )
                add_step(
                    StepAction.NOOP_COMMENT,
                    step_id=invocation.invocation_id,
                    comment=f"skipped: {invocation.tool.value} {format_args(invocation.args)}".rstrip(),
                )
                continue

            if invocation.tool == DcToolName.KEY_EVENT:
                key = str(invocation.args.get("key", ""))
                if key.lower() != "back":
                    omitted.append(
                        {
                            "invocation_id": invocation.invocation_id,
                            "tool": invocation.tool.value,
                            "reason": f"key_event({key!r}) is not replayable (only Back maps to go_back)",
                        }
                    )
                    add_step(
                        StepAction.NOOP_COMMENT,
                        step_id=invocation.invocation_id,
                        comment=f"skipped: key_event({key!r})",
                    )
                    continue

            produced, counted = self._dc_step(
                invocation,
                DcToolName,
                add_step=add_step,
                attach=attach,
                warnings=warnings,
                swipe_inferred=swipe_inferred,
                bound=bundle_for_bounds,
            )
            if not produced:
                omitted.append(
                    {
                        "invocation_id": invocation.invocation_id,
                        "tool": invocation.tool.value,
                        "reason": "could not map to action",
                    }
                )
                continue
            if counted:
                included_count += 1

        for direction in SWIPE_DIRECTION_ORDER:
            count = swipe_inferred.get(direction, 0)
            if count:
                warnings.append(f"swipe direction inferred as {direction} x{count} (from start/end coordinates)")

        if included_count == 0:
            warnings.append("no replayable operations were recorded")
            add_step(StepAction.NOOP_COMMENT, step_id="no-replayable-operations", comment=NO_REPLAYABLE_COMMENT)

        explicit_assertions = sum(1 for item in invocations if item.success and item.tool in assert_tools)
        replay_eligible, runnable_blockers = evaluate_runnable(
            included_actions=included_count,
            bundle_name=bundle_name,
            main_ability=main_ability,
        )
        confidence_factors: list[str] = []
        if explicit_assertions == 0:
            # 「无显式断言」从阻断条件降为 medium 置信度：脚本照样能跑，只是没有检查点。
            confidence_factors.append("no explicit assert_* tool call was recorded")
        confidence = evaluate_confidence(confidence_factors, outcome="completed")

        counts = {
            "source_actions": len(invocations),
            "source_assertions": explicit_assertions,
            "generated_actions": included_count,
            "generated_assertions": sum(len(step.checkpoints) for step in steps),
            "omitted_actions": len(omitted),
            "failed_actions": sum(not item.success for item in invocations),
            "coordinate_fallbacks": sum(
                1 for step in steps if step.locator is not None and step.locator.kind == LocatorKind.COORDINATE
            ),
        }
        spec = self._assemble(
            steps=steps,
            profile=None,
            trace=None,
            case_id=case_id,
            slug=slug,
            scenario=ScenarioKind.CORE_FLOW,
            bundle_name=bundle_name,
            main_ability=main_ability,
            device_sn=device_id,
            startup_wait=2.0,
            source_kind="dc_session",
            source_id=session_id,
        )
        return CaseBuildResult(
            spec=spec,
            omitted_actions=omitted,
            warnings=warnings,
            counts=counts,
            incomplete_reasons=confidence_factors,  # 兼容别名：与 confidence_factors 同值
            confidence_factors=confidence_factors,
            confidence=confidence,
            replay_eligible=replay_eligible,
            runnable_blockers=runnable_blockers,
            promotion_eligible=replay_eligible,
            promotion_blockers=[],
            purpose="acceptance" if replay_eligible else "diagnostic",
            explicit_assertions=explicit_assertions,
        )

    def _dc_step(
        self,
        invocation: Any,
        dc_tool_name: Any,
        *,
        add_step: Any,
        attach: Any,
        warnings: list[str],
        swipe_inferred: dict[str, int],
        bound: tuple[int, int] | None,
    ) -> tuple[bool, bool]:
        """把一个可回放的 DC 调用转为 IR 步骤；返回 ``(是否产出内容, 是否计入 included_count)``。"""
        tool = invocation.tool
        args = dict(invocation.args or {})
        element: UIElement | None = invocation.resolved_element
        locator: LocatorSpec | None = None

        if tool == dc_tool_name.START_APP:
            add_step(
                StepAction.NOOP_COMMENT,
                step_id=invocation.invocation_id,
                comment=f"start_app: {args.get('bundle_name', '?')} (handled by script setup)",
            )
            return True, True

        if tool == dc_tool_name.CLICK:
            if element and (element.key or element.id):
                kind = LocatorKind.KEY if element.key else LocatorKind.ID
                value = element.key or element.id
                locator = LocatorSpec(
                    kind=kind,
                    value=value,
                    target_label=value,
                    evidence=LocatorEvidence(source="dc_resolved_element"),
                )
                add_step(StepAction.CLICK, step_id=invocation.invocation_id, locator=locator, text=value)
            else:
                coordinate = args.get("coordinate") or [args.get("x", 0), args.get("y", 0)]
                point = (int(coordinate[0]), int(coordinate[1]))
                add_step(
                    StepAction.CLICK,
                    step_id=invocation.invocation_id,
                    coordinate=point,
                    locator=self.coordinate_locator(
                        point,
                        bound=bound,
                        label="",
                        warning=f"{invocation.invocation_id}: click fell back to coordinate {point}",
                    ),
                )
            return True, True

        if tool == dc_tool_name.SWIPE:
            direction = str(args.get("direction", "")).upper()
            if direction not in SWIPE_DIRECTIONS:
                start = args.get("start", [0, 0])
                end = args.get("end", [0, 0])
                if isinstance(start, list) and isinstance(end, list) and len(start) == 2 and len(end) == 2:
                    dx = end[0] - start[0]
                    dy = end[1] - start[1]
                    if dx > 0 and abs(dx) > abs(dy):
                        direction = "RIGHT"
                    elif dx < 0 and abs(dx) > abs(dy):
                        direction = "LEFT"
                    elif dy > 0:
                        direction = "DOWN"
                    else:
                        direction = "UP"
                else:
                    direction = "UP"
                swipe_inferred[direction] = swipe_inferred.get(direction, 0) + 1
            add_step(
                StepAction.SWIPE,
                step_id=invocation.invocation_id,
                direction=direction.lower(),  # type: ignore[arg-type]
            )
            return True, True

        if tool == dc_tool_name.INPUT_TEXT:
            if element and (element.key or element.id):
                kind = LocatorKind.KEY if element.key else LocatorKind.ID
                value = element.key or element.id
                locator = LocatorSpec(
                    kind=kind,
                    value=value,
                    target_label=value,
                    evidence=LocatorEvidence(source="dc_resolved_element"),
                )
            else:
                locator = self.locator_from_candidate(None, "输入框", None, warnings)
            add_step(
                StepAction.INPUT_TEXT,
                step_id=invocation.invocation_id,
                locator=locator,
                text=str(args.get("text", "")),
            )
            return True, True

        if tool == dc_tool_name.BACK:
            add_step(StepAction.BACK, step_id=invocation.invocation_id)
            return True, True

        if tool == dc_tool_name.WAIT:
            seconds = args.get("wait_seconds", args.get("seconds", 1))
            add_step(
                StepAction.WAIT,
                step_id=invocation.invocation_id,
                wait_seconds=float(seconds or 1),
            )
            return True, True

        if tool in {dc_tool_name.ASSERT_VISIBLE, dc_tool_name.ASSERT_NOT_VISIBLE, dc_tool_name.ASSERT_TEXT}:
            target = str(args.get("target") or args.get("text") or "")
            if element and (element.key or element.id):
                kind = LocatorKind.KEY if element.key else LocatorKind.ID
                value = element.key or element.id
                locator = LocatorSpec(
                    kind=kind,
                    value=value,
                    target_label=target or value,
                    evidence=LocatorEvidence(source="dc_resolved_element"),
                )
            else:
                locator = None
            if tool == dc_tool_name.ASSERT_VISIBLE:
                if locator is None:
                    locator = self.locator_from_candidate(None, target, None, warnings)
                attach(CheckpointSpec(kind=CheckpointKind.ELEMENT_EXISTS, message_zh="", locator=locator))
            elif tool == dc_tool_name.ASSERT_NOT_VISIBLE:
                if locator is None:
                    locator = self.locator_from_candidate(None, target, None, warnings)
                attach(CheckpointSpec(kind=CheckpointKind.ELEMENT_ABSENT, message_zh="", locator=locator))
            else:
                attach(
                    CheckpointSpec(
                        # 与 Live 侧同一规则：无结构化定位器时用包含匹配（对齐运行时模糊匹配）。
                        kind=CheckpointKind.TEXT_EQUALS if locator is not None else CheckpointKind.TEXT_CONTAINS,
                        message_zh="",
                        locator=locator,
                        expected=target,
                    )
                )
            # 历史实现里断言也会渲染成一行脚本体，因此同样计入 included_count
            # （``evaluate_runnable`` 的「至少 1 个可回放动作」依赖这个口径）。
            # 历史实现把 Back 键事件映射到 ToolName.BACK 并渲染 driver.go_back()；
            # 其他按键由调用方在进入本函数前就 omit 掉了。
            return True, True

        if tool == dc_tool_name.KEY_EVENT:
            if str(args.get("key", "")).lower() == "back":
                add_step(StepAction.BACK, step_id=invocation.invocation_id)
                return True, True
            return False, False

        return False, False

    # ------------------------------------------------------------------
    # 压测 / 缺陷复现（实现在各自的模块里，这里只做统一入口）
    # ------------------------------------------------------------------

    def from_stress_request(
        self,
        request: Any,
        profile: TargetAppProfile | None = None,
        base_spec: TestCaseSpec | None = None,
    ) -> CaseBuildResult:
        """把压测请求构建为用例 IR（委托 ``cases/stress.py``，避免模块成环）。"""
        from .stress import build_stress_case

        return build_stress_case(self, request, profile, base_spec=base_spec)

    def from_bug_repro(
        self,
        plan: Any,
        request: Any,
        profile: TargetAppProfile | None = None,
    ) -> CaseBuildResult:
        """把缺陷复现计划构建为用例 IR（委托 ``cases/bug_repro.py``）。"""
        from .bug_repro import build_bug_repro_case

        return build_bug_repro_case(self, plan, request, profile)

    # ------------------------------------------------------------------
    # 定位器策略
    # ------------------------------------------------------------------

    def _align_text_assertion_locator(
        self,
        trace: RunTrace,
        action: ActionResult,
        locator: LocatorSpec,
        expected: str,
        profile: TargetAppProfile | None,
        warnings: list[str],
    ) -> LocatorSpec:
        """把 TEXT_EQUALS 检查点对齐到「真正持有该文本的元素」。

        真机复盘（run-20260921T063745Z-ba36e30e 的知乎++回放）：``assert_text 'OpenHarmony'`` 的
        运行时定位器是**容器** key ``p2_search_input_container``，脚本因此断言「容器的 text 等于
        OpenHarmony」——容器自身 text 为空，回放必然失败。断言帧里真正的文本持有者是输入框本身，
        这里改用它的 key/id，并保留精确 ``text=`` 语义。
        """
        if not expected:
            return locator
        snapshot = self._assertion_frame(trace, action)
        if snapshot is None:
            return locator
        recorded = self._element_for_locator(snapshot, locator)
        if recorded is not None and self._holds_text(recorded, expected):
            return locator
        holder = next(
            (item for item in snapshot.elements if self._holds_text(item, expected)),
            None,
        )
        if holder is None:
            return locator
        candidate = self._key_id_candidate(holder)
        if candidate is None or candidate.value == locator.value:
            return locator
        warnings.append(
            f"{action.step_id}: text assertion re-targeted from {locator.value!r} to "
            f"{candidate.value!r} (the element that actually holds {expected!r})"
        )
        return self.locator_from_candidate(candidate, expected, profile, warnings)

    @staticmethod
    def _assertion_frame(trace: RunTrace, action: ActionResult) -> ScreenSnapshot | None:
        """断言求值所用帧：优先 after 帧，其次 before 帧。"""
        for snapshot_id in (action.after_snapshot_id, action.before_snapshot_id):
            if not snapshot_id:
                continue
            snapshot = next((item for item in trace.snapshots if item.snapshot_id == snapshot_id), None)
            if snapshot is not None:
                return snapshot
        return None

    @staticmethod
    def _element_for_locator(snapshot: ScreenSnapshot, locator: LocatorSpec) -> UIElement | None:
        for item in snapshot.elements:
            if (locator.kind == LocatorKind.KEY and item.key == locator.value) or (
                locator.kind == LocatorKind.ID and item.id == locator.value
            ):
                return item
        return None

    @staticmethod
    def _holds_text(element: UIElement, expected: str) -> bool:
        wanted = expected.strip()
        if not wanted:
            return False
        return wanted in {element.content.strip(), element.description.strip()}

    @staticmethod
    def _key_id_candidate(element: UIElement) -> LocatorCandidate | None:
        for candidate in element.locator_candidates:
            if candidate.kind in {LocatorKind.KEY, LocatorKind.ID} and candidate.value:
                return candidate
        if element.key:
            return LocatorCandidate(kind=LocatorKind.KEY, value=element.key, score=1)
        if element.id:
            return LocatorCandidate(kind=LocatorKind.ID, value=element.id, score=1)
        return None

    @staticmethod
    def _recorded_key_locator(
        trace: RunTrace,
        action: ActionResult,
        runtime_locator: LocatorCandidate | None,
    ) -> LocatorCandidate | None:
        """运行时只拿到空间/坐标回退时，按 ``params['target']`` 回查原始帧取 key/id 定位器。

        真机复盘（run-20260921T053514Z-8418044b 的回放）：第 13 步「确认保存」在运行时是
        SPATIAL 回退，生成脚本因此写成 ``driver.touch((1212, 237))``；回放时那一下没有落到
        保存按钮上，编辑页一直开着，随后的 ``agenda_item_title`` 断言必然失败。原始帧里该
        元素其实带 ``add_agenda_comfrim`` key——恢复出来就能渲染成稳定的 key 选择器。

        只恢复 key/id：内容文本选择器对坐标回退不是稳定替代，保持坐标兜底语义不变。
        """
        if runtime_locator is not None and runtime_locator.kind in {
            LocatorKind.KEY,
            LocatorKind.ID,
            LocatorKind.TEXT,
            LocatorKind.TYPE_TEXT,
        }:
            return None
        target = str(action.params.get("target") or "")
        if not target or not action.before_snapshot_id:
            return None
        snapshot = next(
            (item for item in trace.snapshots if item.snapshot_id == action.before_snapshot_id),
            None,
        )
        if snapshot is None:
            return None
        element = next((item for item in snapshot.elements if item.element_id == target), None)
        if element is None:
            element = next((item for item in snapshot.elements if target in {item.key, item.id}), None)
        if element is None:
            return None
        for candidate in element.locator_candidates:
            if candidate.kind in {LocatorKind.KEY, LocatorKind.ID} and candidate.value:
                return candidate
        if element.key:
            return LocatorCandidate(kind=LocatorKind.KEY, value=element.key, score=1)
        if element.id:
            return LocatorCandidate(kind=LocatorKind.ID, value=element.id, score=1)
        # 自绘/图标按钮常常既无 key 也无 id，但它的宿主容器带 key（``add_agenda_comfrim`` 包裹
        # 一个无 key 的 Button）——取「包含该元素中心的最小带 key 元素」作为归属控件。
        # 面积相同时取层级更靠后（更靠上层）的那个：弹层里的确认按钮与背景页的 more_menu
        # 在 dump 里 bbox 完全一致，只有顺序能区分。
        if element.bbox is None:
            return None
        center_x, center_y = element.bbox.center
        owners = [
            (index, item)
            for index, item in enumerate(snapshot.elements)
            if item.bbox is not None
            and (item.key or item.id)
            and item.bbox.left <= center_x <= item.bbox.right
            and item.bbox.top <= center_y <= item.bbox.bottom
        ]
        if not owners:
            return None
        _, owner = min(owners, key=lambda pair: (pair[1].bbox.area, -pair[0]))
        if owner.key:
            return LocatorCandidate(kind=LocatorKind.KEY, value=owner.key, score=0.9)
        return LocatorCandidate(kind=LocatorKind.ID, value=owner.id, score=0.9)

    def locator_from_candidate(
        self,
        locator: LocatorCandidate | None,
        target: str | None,
        profile: TargetAppProfile | None,
        warnings: list[str],
    ) -> LocatorSpec:
        """把运行时候选定位器提升为带 Profile 证据的 IR 定位器。

        逐条复刻 ``HypiumGenerator._selector`` 的语义与**警告文案**：
        动态 key 有跨轮证据 → 前缀泛化 + ``validated dynamic ...`` 警告；
        无证据 → 保留精确值 + ``unvalidated dynamic ...`` 警告（该警告仍会喂给
        ``incomplete_reasons``）；完全无定位器 → ``BY.text(target)`` 兜底 + 语义回退警告。
        """
        if locator is not None and locator.kind in {LocatorKind.KEY, LocatorKind.ID}:
            dynamic = _DYNAMIC_LOCATOR.fullmatch(locator.value)
            if dynamic:
                prefix = dynamic.group(1)
                evidence = self._stable_locator_evidence(locator, prefix, profile)
                if evidence is not None:
                    # 前缀可能是 ``foo-``（连字符 + 毫秒时间戳）或 ``foo_``：Hypium 的
                    # starts_with 对二者同样有效，用前缀本身作为选择器值。
                    warnings.append(
                        f"validated dynamic {'key' if locator.kind == LocatorKind.KEY else 'id'} "
                        f"{locator.value!r} generalized to unique prefix {prefix!r}"
                    )
                    return LocatorSpec(
                        kind=locator.kind,
                        value=prefix,
                        match=MatchMode.STARTS_WITH,
                        target_label=target or prefix,
                        evidence=evidence,
                    )
                warnings.append(
                    f"unvalidated dynamic {'key' if locator.kind == LocatorKind.KEY else 'id'} "
                    f"{locator.value!r} retained as an exact diagnostic selector"
                )
            return LocatorSpec(
                kind=locator.kind,
                value=locator.value,
                target_label=target or locator.value,
                evidence=self._exact_locator_evidence(locator, profile),
            )
        if locator is not None and locator.kind == LocatorKind.TEXT:
            return LocatorSpec(
                kind=LocatorKind.TEXT,
                value=locator.value,
                target_label=target or locator.value,
                evidence=self._exact_locator_evidence(locator, profile),
            )
        if locator is not None and locator.kind == LocatorKind.TYPE_TEXT:
            type_name, _, text = locator.value.partition("|")
            return LocatorSpec(
                kind=LocatorKind.TYPE_TEXT,
                value=f"{type_name}|{text}",
                target_label=target or text or type_name,
            )
        warnings.append(f"semantic target {target!r} fell back to exact text")
        return LocatorSpec(kind=LocatorKind.TEXT, value=target or "", target_label=target or "")

    def coordinate_locator(
        self,
        point: tuple[int, int],
        *,
        bound: tuple[int, int] | None,
        label: str,
        warning: str,
    ) -> LocatorSpec:
        """构造坐标定位器；IR 不变式要求坐标定位器必须带解析边界与警告。"""
        resolution = bound or UNKNOWN_RESOLUTION_BOUND
        text = warning if bound else f"{warning} (resolution bound unknown, recorded as {resolution})"
        return LocatorSpec(
            kind=LocatorKind.COORDINATE,
            coordinate=(int(point[0]), int(point[1])),
            resolution_bound=(int(resolution[0]), int(resolution[1])),
            target_label=label,
            warning=text,
        )

    def _stable_locator_evidence(
        self, locator: LocatorCandidate, prefix: str, profile: TargetAppProfile | None
    ) -> LocatorEvidence | None:
        if profile is None:
            return None
        method = "key" if locator.kind == LocatorKind.KEY else "id"
        threshold = self.min_observed_rounds
        accepted_patterns = _dynamic_pattern_variants(prefix, locator.value)
        for item in profile.stable_locator_inventory:
            if (
                getattr(item, method) == locator.value
                and item.dynamic_pattern in accepted_patterns
                and item.observed_rounds >= threshold
                and item.unique_match_rounds >= threshold
            ):
                return LocatorEvidence(
                    observed_rounds=item.observed_rounds,
                    unique_match_rounds=item.unique_match_rounds,
                    dynamic_pattern=item.dynamic_pattern,
                    page_signature=item.page_signature,
                    confidence=item.confidence,
                    source="profile_stable_locator",
                )
        return None

    def _exact_locator_evidence(
        self, locator: LocatorCandidate, profile: TargetAppProfile | None
    ) -> LocatorEvidence | None:
        """精确值命中某个 StableLocator 时，把 Profile 证据带进用例 IR。"""
        if profile is None:
            return None
        method = "key" if locator.kind == LocatorKind.KEY else "id" if locator.kind == LocatorKind.ID else None
        for item in profile.stable_locator_inventory:
            matched = (
                (method == "key" and item.key == locator.value)
                or (method == "id" and item.id == locator.value)
                or (method is None and item.text == locator.value)
            )
            if matched:
                return LocatorEvidence(
                    observed_rounds=item.observed_rounds,
                    unique_match_rounds=item.unique_match_rounds,
                    dynamic_pattern=item.dynamic_pattern,
                    page_signature=item.page_signature,
                    confidence=item.confidence,
                    source="profile_stable_locator",
                )
        return None

    # ------------------------------------------------------------------
    # 兼容性辅助（从 generation/hypium.py 原样搬入）
    # ------------------------------------------------------------------

    @staticmethod
    def _agent_outcome(trace: RunTrace) -> str:
        if trace.agent_outcome != "unknown":
            return trace.agent_outcome
        if trace.state == RunState.COMPLETED:
            return "completed"
        if trace.state == RunState.STOPPED_BY_USER:
            return "stopped"
        if trace.error or any(not action.success for action in trace.actions):
            return "failed"
        if trace.actions and trace.actions[-1].tool == ToolName.FINISH and trace.actions[-1].success:
            return "completed"
        return "unknown"

    @staticmethod
    def _confidence_factors(
        trace: RunTrace,
        outcome: str,
        *,
        explicit_assertions: int,
        omitted: list[dict[str, str]],
        warnings: list[str],
    ) -> list[str]:
        """质量顾虑清单：**不阻断执行**，只决定 ``confidence`` 分档与前端提示。

        文案与历史 ``incomplete_reasons`` 逐字一致（``tests/unit/test_case_builder_trace.py``
        钉住这些字符串），只是**去掉** ``provisional`` / ``live_mode`` 两条——它们属于
        Profile 晋级资格（``_promotion_blockers``），与「这脚本能不能跑」无关。
        """
        factors: list[str] = []
        if outcome != "completed":
            factors.append(f"source agent outcome is {outcome}")
        if trace.agent_error:
            factors.append(f"source agent error: {trace.agent_error}")
        if any(not action.success for action in trace.actions):
            factors.append("source trace contains failed actions")
        if not trace.actions or trace.actions[-1].tool != ToolName.FINISH or not trace.actions[-1].success:
            factors.append("source trace does not end with a successful FINISH action")
        if explicit_assertions == 0:
            factors.append("source trace has no successful explicit assertion")
        if any(item["reason"].startswith("unsupported replay tool") for item in omitted):
            factors.append("source trace contains unsupported replay actions")
        if any(item.startswith("unvalidated dynamic ") for item in warnings):
            factors.append("source trace contains a dynamic locator without stable unique-prefix evidence")
        return list(dict.fromkeys(factors))

    @staticmethod
    def _promotion_blockers(trace: RunTrace) -> list[str]:
        """Profile 晋级证据资格：与「能否执行」完全解耦的防御性标注。

        晋级实际使用 ``trace.profile_validation_generated`` 与
        ``provenance.generated_script_path``，任务脚本本就不参与晋级；这两个字段只供 API
        在误用时给出明确拒绝理由。
        """
        blockers: list[str] = []
        if trace.provisional:
            blockers.append("provisional trace is not Profile-promotion evidence")
        if trace.live_mode:
            blockers.append("live-mode trace is not Profile-promotion evidence")
        return blockers

    @staticmethod
    def _is_nondeterministic_system_click(trace: RunTrace, action: ActionResult) -> bool:
        target = str(action.params.get("target") or "")
        if "appicon" in target.lower():
            return True
        if not action.before_snapshot_id:
            return False
        snapshot = next((item for item in trace.snapshots if item.snapshot_id == action.before_snapshot_id), None)
        if snapshot is None:
            return False
        element = next((item for item in snapshot.elements if item.element_id == target), None)
        if element is None:
            return False
        launch_markers = " ".join(
            value or "" for value in (element.type, element.source, element.key, element.id, element.content)
        ).lower()
        return (
            "appicon" in launch_markers
            or "keyhidekbd" in launch_markers
            or (
                "launcher" in snapshot.page_path.lower()
                and trace.target_app_id.replace("-", "") in launch_markers.replace("-", "")
            )
        )

    @staticmethod
    def _runtime_element_coordinate(trace: RunTrace, action: ActionResult) -> tuple[int, int] | None:
        target = action.params.get("target")
        if not target or not action.before_snapshot_id:
            return None
        snapshot = next((item for item in trace.snapshots if item.snapshot_id == action.before_snapshot_id), None)
        if snapshot is None:
            return None
        element = next((item for item in snapshot.elements if item.element_id == target), None)
        return element.bbox.center if element and element.bbox else None

    @staticmethod
    def _snapshot_bound(trace: RunTrace, snapshot_id: str | None) -> tuple[int, int] | None:
        if not snapshot_id:
            return None
        snapshot = next((item for item in trace.snapshots if item.snapshot_id == snapshot_id), None)
        if snapshot is None:
            return None
        return (snapshot.width, snapshot.height)

    @staticmethod
    def _trace_bound(trace: RunTrace) -> tuple[int, int] | None:
        for snapshot in reversed(trace.snapshots):
            return (snapshot.width, snapshot.height)
        return None

    @staticmethod
    def _first_stable_locator(trace: RunTrace) -> LocatorSpec | None:
        """复刻 ``HypiumGenerator._first_stable_locator``，返回 IR 定位器。"""
        for snapshot in reversed(trace.snapshots):
            for element in snapshot.elements:
                if element.key and element.enabled and not re.search(r"_\d{8,}$", element.key):
                    return LocatorSpec(kind=LocatorKind.KEY, value=element.key, target_label=element.key)
                if element.content and element.enabled:
                    return LocatorSpec(kind=LocatorKind.TEXT, value=element.content, target_label=element.content)
        return None

    # ------------------------------------------------------------------
    # 组装
    # ------------------------------------------------------------------

    @staticmethod
    def _assemble(
        *,
        steps: list[TestStepSpec],
        profile: TargetAppProfile | None,
        trace: RunTrace | None,
        case_id: str | None,
        slug: str | None,
        scenario: ScenarioKind,
        bundle_name: str,
        main_ability: str,
        device_sn: str | None,
        startup_wait: float,
        source_kind: str,
        source_id: str,
    ) -> TestCaseSpec:
        for position, step in enumerate(steps, start=1):
            step.index = position
            step.title_zh = step.title_zh or _derive_step_title(step)
        hard = sum(1 for step in steps for checkpoint in step.checkpoints if not checkpoint.soft)
        title = _case_title(trace=trace, source_kind=source_kind, steps=steps)
        return TestCaseSpec(
            case_id=case_id or new_case_id(),
            slug=slug or slugify(title),
            title_zh=title,
            scenario=scenario,
            status="active" if hard else "draft",
            bundle_name=bundle_name,
            main_ability=main_ability,
            device_sn=device_sn,
            setup=SetupSpec(
                stop_app_first=True,
                start_app=True,
                startup_wait_seconds=startup_wait,
                listen_toast=any(
                    checkpoint.kind == CheckpointKind.TOAST for step in steps for checkpoint in step.checkpoints
                ),
            ),
            steps=steps,
            teardown=TeardownSpec(capture_final_screenshot=True, stop_app=False),
            provenance=CaseProvenance(
                source_kind=source_kind,  # type: ignore[arg-type]
                source_id=source_id,
                profile_target_app_id=profile.target_app_id if profile is not None else None,
            ),
        )


def _derive_step_title(step: TestStepSpec) -> str:
    from .titles import step_title_zh

    return step_title_zh(step)


def _case_title(*, trace: RunTrace | None, source_kind: str, steps: list[TestStepSpec]) -> str:
    if trace is not None and trace.task:
        return trace.task.strip()[:200] or "未命名用例"
    first = next((step for step in steps if step.action != StepAction.NOOP_COMMENT), None)
    if first is not None:
        return first.title_zh[:200] or "未命名用例"
    return f"未命名用例（{source_kind}）"


__all__ = [
    "FALLBACK_CHECKPOINT_MESSAGE",
    "LOW_CONFIDENCE_PREFIXES",
    "PLACEHOLDER_ABILITY",
    "PLACEHOLDER_BUNDLE",
    "SWIPE_DIRECTIONS",
    "SWIPE_DIRECTION_ORDER",
    "UNKNOWN_RESOLUTION_BOUND",
    "CaseBuildResult",
    "CaseBuilder",
    "evaluate_confidence",
    "evaluate_runnable",
    "format_args",
    "new_case_id",
    "slugify",
]
