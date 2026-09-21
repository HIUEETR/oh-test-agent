"""压测用例构建（改进计划 C2 / C3）。

职责：
* ``StressRequest``：``POST /api/cases/stress``、``cli stress`` 与用例库共用的请求契约，字段、
  默认值与边界与计划 C2 逐字一致；``iterations`` 相对 ``Settings.stress_max_iterations``
  的二次校验由 ``cases/safety.py::validate_case_spec`` 在落盘前完成（不在本模块）。
* ``steps_from_exploration_actions``：把 ``Profile.core_flows[0]["steps"]``（``ExplorationAction``
  的 ``model_dump(mode="json")`` 结果）翻译为 ``TestStepSpec``。这段翻译原本内联在
  ``agents/orchestrator.py::_profile_validation_trace``；本模块是它的唯一实现，orchestrator 与
  压测构建共用同一份，禁止再复制。
* ``build_stress_case``：按 ``StressKind`` 推导循环体与每轮检查点，产出 ``TestCaseSpec``。
  ``CaseBuilder.from_stress_request`` 只是一层薄适配（由 integrator 添加），本模块保持模块级
  纯函数形态，便于测试与复用。

安全边界不在本模块：永久禁词、步骤上限、坐标分辨率等由 ``cases/safety.py`` 在保存前统一校验。
本模块不接触真机，只做静态 IR 构建。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ..models import (
    LocatorCandidate,
    LocatorKind,
    ProfileStatus,
    ScenarioKind,
    StableLocator,
    StressKind,
    TargetAppProfile,
    TargetQuery,
)
from .builder import (
    PLACEHOLDER_ABILITY,
    PLACEHOLDER_BUNDLE,
    UNKNOWN_RESOLUTION_BOUND,
    CaseBuilder,
    CaseBuildResult,
    evaluate_confidence,
    evaluate_runnable,
    new_case_id,
    slugify,
)
from .spec import (
    CaseProvenance,
    CheckpointKind,
    CheckpointSpec,
    LocatorSpec,
    SetupSpec,
    StepAction,
    StressSpec,
    TeardownSpec,
    TestCaseSpec,
    TestStepSpec,
)
from .titles import checkpoint_title_zh, step_title_zh

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_FIXED_INPUT_TEXT = "OpenHarmony"
"""Profile 未记录 ``test_data_strategy.fixed_input_text`` 时的固定输入文本（对齐探索策略默认值）。"""

MAX_PAGE_ANCHORS = 3
"""``PAGE_SIGNATURE`` 检查点最多携带的入口页锚点数量（按稳定性排序后取前 N 个）。"""

TIMEOUT_BASE_SECONDS = 60
TIMEOUT_PER_ITERATION_SECONDS = 5
MIN_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 7200
"""``spec.timeout_seconds = min(7200, max(300, 60 + iterations * 5 + duration_budget))`` 的三个边界。"""

STRESS_COUNT_KEYS: tuple[str, ...] = (
    "source_actions",
    "source_assertions",
    "generated_actions",
    "generated_assertions",
    "omitted_actions",
    "failed_actions",
    "coordinate_fallbacks",
)
"""``CaseBuildResult.counts`` 的固定键集合，与 Live/DC builder 保持一致（元数据写出保持同构）。"""

KIND_TITLE_ZH: dict[StressKind, str] = {
    StressKind.REPEAT_CLICK: "重复点击压力测试",
    StressKind.CONTINUOUS_SWIPE: "连续滑动压力测试",
    StressKind.PAGE_ENTER_EXIT: "页面进出压力测试",
    StressKind.SOAK: "长时运行压力测试",
}
"""四种压测的中文用例名基（``StressRequest.title_zh`` 缺省时使用）。"""

_SWIPE_DIRECTIONS: frozenset[str] = frozenset({"up", "down", "left", "right"})
_EVIDENCE_LOCATOR_KINDS: frozenset[LocatorKind] = frozenset(
    {LocatorKind.KEY, LocatorKind.ID, LocatorKind.TEXT, LocatorKind.TYPE_TEXT}
)


# ---------------------------------------------------------------------------
# 请求契约（计划 C2）
# ---------------------------------------------------------------------------


class StressRequest(BaseModel):
    """压测请求（计划 C2；字段、默认值与边界逐字对齐，勿在别处复制定义）。"""

    target: TargetQuery | None = None
    kind: StressKind = StressKind.REPEAT_CLICK
    iterations: int = Field(default=20, ge=1, le=2000)
    duration_budget_seconds: int | None = Field(default=None, ge=10, le=7200)
    body: Literal["profile_core_flow", "existing_case", "inline_steps"] = "profile_core_flow"
    case_id: str | None = None
    steps: list[TestStepSpec] = Field(default_factory=list)
    click_target: str | None = None
    swipe_pair: bool = True
    fail_break: bool = True
    fail_times: int = Field(default=0, ge=0)
    sample_memory_every: int = Field(default=0, ge=0, le=500)
    memory_growth_threshold_kb: int | None = None
    inter_iteration_wait_seconds: float = Field(default=0.5, ge=0, le=10)
    device_id: str | None = None
    auto_execute: bool = False
    title_zh: str | None = None

    @model_validator(mode="after")
    def validate_body_source(self) -> StressRequest:
        """``body`` 与它要求的载荷必须自洽：内联步骤非空、复用既有用例必须给 ``case_id``。"""
        if self.body == "inline_steps" and not self.steps:
            raise ValueError("body=inline_steps 需要提供非空的 steps 列表")
        if self.body == "existing_case" and not (self.case_id or "").strip():
            raise ValueError("body=existing_case 需要提供 case_id")
        return self


# ---------------------------------------------------------------------------
# ExplorationAction dict → IR 步骤（orchestrator 的唯一共享实现）
# ---------------------------------------------------------------------------


def _coordinate_of(value: Any) -> tuple[int, int] | None:
    """把 ``(x, y)`` / ``[x, y]`` 归一化为 ``tuple[int, int]``；其余输入返回 ``None``。"""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return (int(value[0]), int(value[1]))
        except TypeError, ValueError:
            return None
    return None


def _exploration_locator(action: dict[str, Any], target: str) -> LocatorSpec:
    """按 ``CaseBuilder.locator_from_candidate`` 的分支语义，把探索动作定位字段翻译为 IR 定位器。

    ``key`` / ``id`` / ``text`` 直接搬运；``type_text`` 保留 ``type|text`` 形式且
    显示标签优先取语义目标；没有结构化定位器（``locator_kind=coordinate`` 或值为空）时，
    与 ``from_trace`` 一样退化为 ``BY.text(target)`` 的精确匹配。
    """
    kind_text = str(action.get("locator_kind") or LocatorKind.COORDINATE.value)
    value = str(action.get("locator_value") or "")
    if kind_text == LocatorKind.TYPE_TEXT.value and value:
        type_name, _, text = value.partition("|")
        return LocatorSpec(
            kind=LocatorKind.TYPE_TEXT,
            value=f"{type_name}|{text}",
            target_label=target or text or type_name,
        )
    if kind_text in {LocatorKind.KEY.value, LocatorKind.ID.value, LocatorKind.TEXT.value} and value:
        return LocatorSpec(kind=LocatorKind(kind_text), value=value, target_label=target or value)
    return LocatorSpec(kind=LocatorKind.TEXT, value=target, target_label=target)


def _unbound_coordinate_locator(point: tuple[int, int], step_id: str) -> LocatorSpec:
    """探索动作只记录了坐标点、没有分辨率边界，因此按未知边界占位并强制带警告。

    文案与 ``CaseBuilder.from_trace`` 的 ``CLICK_COORDINATE`` 分支在 ``bound=None`` 时逐字一致；
    IR 不变式要求坐标定位器必须同时带 ``resolution_bound`` 与 ``warning``。
    """
    return LocatorSpec(
        kind=LocatorKind.COORDINATE,
        coordinate=point,
        resolution_bound=UNKNOWN_RESOLUTION_BOUND,
        warning=f"{step_id}: click uses a coordinate fallback (resolution bound unknown, recorded as (0, 0))",
    )


def steps_from_exploration_actions(actions: list[dict[str, Any]], fixed_input_text: str) -> list[TestStepSpec]:
    """把 Profile 核心流的 ``ExplorationAction`` dict 列表翻译为 IR 步骤。

    这是 ``agents/orchestrator.py::_profile_validation_trace`` 里那段动作合成逻辑的**唯一**
    共享实现（orchestrator 与压测构建共用）。逐条后果与「orchestrator 合成 ``ActionResult``
    → ``CaseBuilder.from_trace``」一致：

    * ``click`` → 一个 ``CLICK``；
    * ``input`` → 有坐标时先补一个坐标 ``CLICK``（聚焦输入框，与 orchestrator 的 ``-focus`` 动作
      等价）再 ``INPUT_TEXT``，文本固定为 ``fixed_input_text``；没有坐标时只发 ``INPUT_TEXT``
      （orchestrator 在该分支会退化成一次 (0, 0) 的裸坐标点击并丢掉输入文本，属于已知缺陷，
      压测用例不应复刻这种破坏性行为）；
    * ``swipe`` → ``SWIPE``，方向取 ``direction`` 且只接受 up/down/left/right，非法值回落 ``up``；
    * ``back`` → ``BACK``。

    返回的 ``index`` 从 1 开始严格递增。``step_id`` 由动作序号与 ``action_id`` 组成，
    不再携带 orchestrator 的页序号（该信息不在本函数的入参里）。
    """
    steps: list[TestStepSpec] = []
    for position, action in enumerate(actions, start=1):
        kind = str(action.get("kind") or "").strip()
        target = str(action.get("element_id") or action.get("target_text") or "")
        action_id = str(action.get("action_id") or f"{position:02d}")
        step_id = f"flow-{position:02d}-{action_id}"
        if kind == "click":
            steps.append(
                _step(step_id, StepAction.CLICK, locator=_exploration_locator(action, target)),
            )
        elif kind == "input":
            point = _coordinate_of(action.get("coordinate"))
            if point is not None:
                steps.append(
                    _step(
                        f"{step_id}-focus",
                        StepAction.CLICK,
                        coordinate=point,
                        locator=_unbound_coordinate_locator(point, f"{step_id}-focus"),
                    )
                )
            steps.append(
                _step(
                    step_id,
                    StepAction.INPUT_TEXT,
                    locator=_exploration_locator(action, target),
                    text=str(fixed_input_text),
                )
            )
        elif kind == "swipe":
            direction = str(action.get("direction") or "up").lower()
            if direction not in _SWIPE_DIRECTIONS:
                direction = "up"
            steps.append(_step(step_id, StepAction.SWIPE, direction=direction))  # type: ignore[arg-type]
        elif kind == "back":
            steps.append(_step(step_id, StepAction.BACK))
        else:
            raise ValueError(f"不支持的探索动作 kind={kind!r}（仅支持 click/input/swipe/back）")
    _renumber(steps)
    return steps


# ---------------------------------------------------------------------------
# 压测用例构建（计划 C3）
# ---------------------------------------------------------------------------


def build_stress_case(
    builder: CaseBuilder,
    request: StressRequest,
    profile: TargetAppProfile | None,
    base_spec: TestCaseSpec | None = None,
) -> CaseBuildResult:
    """按 ``StressRequest`` 构建压测用例 IR。

    循环体推导（计划 C3）：

    * ``REPEAT_CLICK``：一轮一个 ``CLICK``，目标先在 Profile 稳定定位器库里按
      ``name``/``key``/``text`` 精确再包含地解析，失败退 ``LocatorSpec(TEXT, click_target)``；
    * ``CONTINUOUS_SWIPE``：``swipe_pair`` 为真时发 ``SWIPE up`` + ``SWIPE down`` 一对防漂移；
    * ``PAGE_ENTER_EXIT``：进入 = 核心流步骤翻译，退出 = ``BACK`` ×（进入时 click 数）；
    * ``SOAK``：循环体取 ``existing_case``（``base_spec.steps``）、``inline_steps`` 或核心流，
      ``duration_budget_seconds`` 必填。

    每轮检查点：``REPEAT_CLICK`` / ``CONTINUOUS_SWIPE`` / ``SOAK`` 为
    ``[ELEMENT_EXISTS(入口页锚点), CURRENT_APP]``；``PAGE_ENTER_EXIT`` 为入口页锚点的
    ``PAGE_SIGNATURE``（没有锚点时该 kind 没有检查点，用例降级为 ``draft``）。

    ``body`` / ``case_id`` / ``steps`` 只对 ``SOAK`` 生效；三个计数类 kind 的循环体由 kind
    本身决定，忽略这三个字段。
    """
    if request.kind == StressKind.SOAK and request.duration_budget_seconds is None:
        raise ValueError("SOAK 压测必须提供 duration_budget_seconds（10..7200 秒）")

    warnings: list[str] = []
    bundle_name, main_ability, _placeholder = _resolve_identity(profile, request, warnings)
    if profile is None:
        warnings.append("未提供 Profile：循环体只能来自请求本身，且没有入口页锚点可供检查点使用")
    elif profile.status != ProfileStatus.VERIFIED:
        warnings.append(
            f"Profile {profile.target_app_id} 的状态是 {profile.status.value}（非 verified），产出用例仅作诊断参考"
        )

    body_steps, source_actions, source_assertions = _derive_loop_body(builder, request, profile, base_spec, warnings)
    _renumber(body_steps)
    _fill_titles(body_steps)

    anchors, entry_page_path = _entry_anchors(builder, profile, warnings)
    per_iteration = _per_iteration_checkpoints(request.kind, anchors, bundle_name, entry_page_path)
    for checkpoint in per_iteration:
        checkpoint.message_zh = checkpoint.message_zh or checkpoint_title_zh(checkpoint)

    hard_checkpoints = [cp for step in body_steps for cp in step.checkpoints if not cp.soft]
    hard_checkpoints += [cp for cp in per_iteration if not cp.soft]
    status: Literal["draft", "active"] = "active" if hard_checkpoints else "draft"
    if not hard_checkpoints:
        warnings.append("压测用例没有任何非软检查点，用例降级为 draft")

    # 无硬检查点从阻断条件降为质量因素（循环体照样能跑）；占位身份仍是 runnable blocker。
    replay_eligible, runnable_blockers = evaluate_runnable(
        included_actions=len(body_steps),
        bundle_name=bundle_name,
        main_ability=main_ability,
    )
    confidence_factors: list[str] = []
    if not hard_checkpoints:
        confidence_factors.append("stress case has no non-soft checkpoint")
    confidence = evaluate_confidence(confidence_factors, outcome="completed")

    title = (request.title_zh or "").strip() or f"{KIND_TITLE_ZH[request.kind]}（{request.iterations} 轮）"
    spec = TestCaseSpec(
        case_id=new_case_id(),
        slug=slugify(title),
        title_zh=title[:200],
        scenario=ScenarioKind.STRESS,
        status=status,
        bundle_name=bundle_name,
        main_ability=main_ability,
        device_sn=request.device_id,
        timeout_seconds=_timeout_seconds(request),
        setup=SetupSpec(
            stop_app_first=True,
            start_app=True,
            startup_wait_seconds=_startup_wait_seconds(profile),
            listen_toast=False,
        ),
        steps=[],
        teardown=TeardownSpec(capture_final_screenshot=True, stop_app=False),
        stress=StressSpec(
            kind=request.kind,
            iterations=request.iterations,
            duration_budget_seconds=request.duration_budget_seconds,
            fail_break=request.fail_break,
            fail_times=request.fail_times,
            continues_fail=False,
            body_steps=body_steps,
            per_iteration_checkpoints=per_iteration,
            inter_iteration_wait_seconds=request.inter_iteration_wait_seconds,
            sample_memory_every=request.sample_memory_every,
            memory_growth_threshold_kb=request.memory_growth_threshold_kb,
        ),
        provenance=CaseProvenance(
            source_kind="stress_request",
            source_id=_source_id(request, profile),
            profile_target_app_id=profile.target_app_id if profile is not None else None,
        ),
    )
    counts = {
        "source_actions": source_actions,
        "source_assertions": source_assertions,
        "generated_actions": len(body_steps),
        "generated_assertions": sum(len(step.checkpoints) for step in body_steps) + len(per_iteration),
        "omitted_actions": 0,
        "failed_actions": 0,
        "coordinate_fallbacks": sum(
            1 for step in body_steps if step.locator is not None and step.locator.kind == LocatorKind.COORDINATE
        ),
    }
    return CaseBuildResult(
        spec=spec,
        omitted_actions=[],
        warnings=list(dict.fromkeys(warnings)),
        counts=counts,
        incomplete_reasons=confidence_factors,  # 兼容别名：与 confidence_factors 同值
        confidence_factors=confidence_factors,
        confidence=confidence,
        replay_eligible=replay_eligible,
        runnable_blockers=runnable_blockers,
        promotion_eligible=replay_eligible,
        purpose="acceptance" if replay_eligible else "diagnostic",
        explicit_assertions=len(hard_checkpoints),
    )


# ---------------------------------------------------------------------------
# 循环体推导
# ---------------------------------------------------------------------------


def _derive_loop_body(
    builder: CaseBuilder,
    request: StressRequest,
    profile: TargetAppProfile | None,
    base_spec: TestCaseSpec | None,
    warnings: list[str],
) -> tuple[list[TestStepSpec], int, int]:
    """按 kind 推导循环体；返回 ``(body_steps, source_actions, source_assertions)``。"""
    actions = _core_flow_actions(profile)
    if request.kind == StressKind.REPEAT_CLICK:
        target = _repeat_click_target(request, profile)
        if not target:
            raise ValueError(
                "REPEAT_CLICK 需要 click_target，或在 profile.core_flows[0]['steps'] 里存在一个 click 动作"
            )
        locator = _resolve_click_target(builder, profile, target, warnings)
        return [_step("stress-click-1", StepAction.CLICK, locator=locator)], 0, 0

    if request.kind == StressKind.CONTINUOUS_SWIPE:
        directions = ("up", "down") if request.swipe_pair else ("up",)
        steps = [
            _step(f"stress-swipe-{position}", StepAction.SWIPE, direction=direction)
            for position, direction in enumerate(directions, start=1)
        ]
        if not request.swipe_pair:
            warnings.append("swipe_pair=False：连续滑动只朝一个方向，长时间运行可能滚出内容区")
        return steps, 0, 0

    if request.kind == StressKind.PAGE_ENTER_EXIT:
        if not actions:
            raise ValueError("PAGE_ENTER_EXIT 需要 profile.core_flows[0]['steps'] 提供进入步骤")
        enter = _attach_profile_evidence(
            steps_from_exploration_actions(actions, _fixed_input_text(profile)), builder, profile, warnings
        )
        clicks = sum(1 for step in enter if step.action == StepAction.CLICK)
        if not clicks:
            warnings.append("核心流进入步骤里没有 click，退出步骤数为 0；压测循环不会返回入口页")
        body = enter + [_step(f"stress-back-{position}", StepAction.BACK) for position in range(1, clicks + 1)]
        _renumber(body)
        return body, len(actions), sum(len(step.checkpoints) for step in enter)

    # SOAK
    if request.body == "existing_case":
        if base_spec is None:
            raise ValueError("body=existing_case 需要调用方提供 base_spec（用例库里的源用例）")
        source_steps = list(base_spec.steps)
        source_actions = len(source_steps)
    elif request.body == "inline_steps":
        source_steps = list(request.steps)
        source_actions = len(source_steps)
    else:
        if not actions:
            raise ValueError(
                "SOAK 需要 profile.core_flows[0]['steps']、body=existing_case 或 body=inline_steps 之一提供循环体"
            )
        source_steps = _attach_profile_evidence(
            steps_from_exploration_actions(actions, _fixed_input_text(profile)), builder, profile, warnings
        )
        source_actions = len(actions)
    source_assertions = sum(len(step.checkpoints) for step in source_steps)
    body = [step.model_copy(deep=True) for step in source_steps]
    if not body:
        raise ValueError("SOAK 的循环体为空：源用例 / 内联步骤 / 核心流都没有可执行的步骤")
    _renumber(body)
    return body, source_actions, source_assertions


def _repeat_click_target(request: StressRequest, profile: TargetAppProfile | None) -> str:
    """``click_target`` 缺省时取核心流里第一个 click 的语义目标/定位值。"""
    if request.click_target and request.click_target.strip():
        return request.click_target.strip()
    for action in _core_flow_actions(profile):
        if str(action.get("kind") or "") != "click":
            continue
        value = action.get("locator_value") if str(action.get("locator_kind")) != "coordinate" else None
        candidate = str(value or action.get("target_text") or "").strip()
        if candidate:
            return candidate
    return ""


def _resolve_click_target(
    builder: CaseBuilder,
    profile: TargetAppProfile | None,
    target: str,
    warnings: list[str],
) -> LocatorSpec:
    """在 Profile 稳定定位器库里解析 ``click_target``；命中则带 Profile 证据，否则退文本精确匹配。"""
    matched = _match_stable_locator(profile, target)
    if matched is not None:
        kind, value, name = matched
        warnings.append(f"REPEAT_CLICK 目标 {target!r} 由 Profile 稳定定位器 {name!r} 解析为 {kind.value}={value!r}")
        return builder.locator_from_candidate(LocatorCandidate(kind=kind, value=value), target, profile, warnings)
    warnings.append(f"REPEAT_CLICK 目标 {target!r} 未在 Profile 稳定定位器库里命中，退化为文本精确匹配")
    return LocatorSpec(
        kind=LocatorKind.TEXT,
        value=target,
        target_label=target,
        warning=f"目标 {target!r} 没有 Profile 证据，退化为 BY.text 精确匹配",
    )


def _match_stable_locator(profile: TargetAppProfile | None, target: str) -> tuple[LocatorKind, str, str] | None:
    """在 ``stable_locator_inventory`` 里按 ``name``/``key``/``text`` 精确再包含地解析目标。"""
    if profile is None or not target:
        return None
    for exact in (True, False):
        for locator in profile.stable_locator_inventory:
            fields = [field for field in (locator.name, locator.key, locator.text) if field]
            hit = any(field == target for field in fields) if exact else any(target in field for field in fields)
            if not hit:
                continue
            kind, value = _stable_locator_identity(locator)
            if value:
                return kind, value, locator.name
    return None


def _stable_locator_identity(locator: StableLocator) -> tuple[LocatorKind, str]:
    """稳定定位器的最佳身份，优先级 ``key`` > ``id`` > ``type|text`` > ``text``。

    与 ``AgentOrchestrator._profile_locator`` 的选择顺序一致；本模块不 import orchestrator
    （会引入 ``agents`` 包级循环依赖），因此保留这份三行实现，两处行为已由测试钉住。
    """
    if locator.key:
        return LocatorKind.KEY, locator.key
    if locator.id:
        return LocatorKind.ID, locator.id
    if locator.type and locator.text:
        return LocatorKind.TYPE_TEXT, f"{locator.type}|{locator.text}"
    return LocatorKind.TEXT, locator.text


def _attach_profile_evidence(
    steps: list[TestStepSpec],
    builder: CaseBuilder,
    profile: TargetAppProfile | None,
    warnings: list[str],
) -> list[TestStepSpec]:
    """把核心流翻译出的定位器再过一遍 ``locator_from_candidate``，让 Profile 证据流入 IR。

    这一步等价于 ``from_trace`` 在 Live 路径上做的事（证据、动态 key 前缀泛化及其警告）；
    没有 Profile 时原样返回。
    """
    if profile is None:
        return steps
    for step in steps:
        locator = step.locator
        if locator is None or locator.kind not in _EVIDENCE_LOCATOR_KINDS:
            continue
        step.locator = builder.locator_from_candidate(
            LocatorCandidate(kind=locator.kind, value=locator.value),
            locator.target_label,
            profile,
            warnings,
        )
    return steps


# ---------------------------------------------------------------------------
# 检查点
# ---------------------------------------------------------------------------


def _entry_anchors(
    builder: CaseBuilder,
    profile: TargetAppProfile | None,
    warnings: list[str],
) -> tuple[list[LocatorSpec], str]:
    """入口页 anchors：按 ``unique_match_rounds`` 排序取前 ``MAX_PAGE_ANCHORS`` 个；返回 ``(锚点, 页路径)``。"""
    if profile is None:
        return [], ""
    inventory = list(profile.stable_locator_inventory)
    if not inventory:
        warnings.append("Profile 的 stable_locator_inventory 为空，无法生成入口页锚点检查点")
        return [], ""
    page_keys, page_path = _entry_page_keys(profile)
    candidates = [item for item in inventory if item.page_signature in page_keys] if page_keys else []
    if not candidates:
        if page_keys:
            warnings.append("入口页签名在 stable_locator_inventory 里没有匹配项，回退为全库最稳定的定位器作锚点")
        candidates = inventory
    ranked = sorted(candidates, key=lambda item: (-item.unique_match_rounds, -item.observed_rounds, item.name))
    anchors: list[LocatorSpec] = []
    seen: set[tuple[LocatorKind, str]] = set()
    for item in ranked:
        kind, value = _stable_locator_identity(item)
        if not value or (kind, value) in seen:
            continue
        seen.add((kind, value))
        anchors.append(
            builder.locator_from_candidate(LocatorCandidate(kind=kind, value=value), item.name, profile, warnings)
        )
        if len(anchors) >= MAX_PAGE_ANCHORS:
            break
    if not anchors:
        warnings.append("入口页稳定定位器没有可用的 key/id/text，无法生成锚点检查点")
        return [], page_path
    if ranked[0].unique_match_rounds <= 0:
        warnings.append(
            f"入口页锚点 {ranked[0].name!r} 缺少唯一匹配证据（unique_match_rounds=0），跨设备回放可能不稳定"
        )
    return anchors, page_path


def _entry_page_keys(profile: TargetAppProfile) -> tuple[list[str], str]:
    """核心流首页的页键（结构身份优先，其次 page_id）与用于 ``PAGE_SIGNATURE.page_path`` 的页路径。"""
    flow = profile.core_flows[0] if profile.core_flows else {}
    if not isinstance(flow, dict):
        flow = {}
    page_ids = [str(item) for item in (flow.get("page_ids") or []) if item]
    pages = [str(item) for item in (flow.get("pages") or []) if item]
    keys = [key for key in (pages[0] if pages else "", page_ids[0] if page_ids else "") if key]
    return keys, (page_ids[0] if page_ids else (pages[0] if pages else ""))


def _per_iteration_checkpoints(
    kind: StressKind,
    anchors: list[LocatorSpec],
    bundle_name: str,
    entry_page_path: str,
) -> list[CheckpointSpec]:
    """每轮检查点（全部为非软检查点）。

    * ``PAGE_ENTER_EXIT``：入口页 anchors 的 ``PAGE_SIGNATURE``（退出后应回到入口页）；
    * 其余三种 kind：``ELEMENT_EXISTS(最稳锚点)`` + ``CURRENT_APP``。
    """
    if kind == StressKind.PAGE_ENTER_EXIT:
        if not anchors:
            return []
        return [
            CheckpointSpec(
                kind=CheckpointKind.PAGE_SIGNATURE,
                message_zh="",
                page_path=entry_page_path,
                anchors=[anchor.model_copy(deep=True) for anchor in anchors],
            )
        ]
    checkpoints: list[CheckpointSpec] = []
    if anchors:
        checkpoints.append(
            CheckpointSpec(
                kind=CheckpointKind.ELEMENT_EXISTS,
                message_zh="",
                locator=anchors[0].model_copy(deep=True),
            )
        )
    checkpoints.append(CheckpointSpec(kind=CheckpointKind.CURRENT_APP, message_zh="", expected=bundle_name))
    return checkpoints


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _step(step_id: str, action: StepAction, **kwargs: Any) -> TestStepSpec:
    """构造一个 ``index`` 待重排的 IR 步骤（``build_stress_case`` 结束前统一 renumber）。"""
    return TestStepSpec(step_id=step_id, index=1, action=action, title_zh="", **kwargs)


def _renumber(steps: list[TestStepSpec]) -> None:
    """把步骤的 ``index`` 重排为严格 1..N（IR 不变式）。"""
    for position, step in enumerate(steps, start=1):
        step.index = position


def _fill_titles(steps: list[TestStepSpec]) -> None:
    """补全步骤与检查点的中文标题（比赛要求每条步骤/检查点都有明确中文说明）。"""
    for step in steps:
        step.title_zh = step.title_zh or step_title_zh(step)
        for checkpoint in step.checkpoints:
            checkpoint.message_zh = checkpoint.message_zh or checkpoint_title_zh(checkpoint)


def _core_flow_actions(profile: TargetAppProfile | None) -> list[dict[str, Any]]:
    """``profile.core_flows[0]["steps"]`` 的浅拷贝列表（缺失时为空列表）。"""
    if profile is None or not profile.core_flows:
        return []
    flow = profile.core_flows[0]
    if not isinstance(flow, dict):
        return []
    return [dict(item) for item in (flow.get("steps") or []) if isinstance(item, dict)]


def _fixed_input_text(profile: TargetAppProfile | None) -> str:
    """Profile 记录的核心流固定输入文本（``test_data_strategy.fixed_input_text``）。"""
    if profile is None:
        return DEFAULT_FIXED_INPUT_TEXT
    value = profile.test_data_strategy.get("fixed_input_text")
    return str(value) if value else DEFAULT_FIXED_INPUT_TEXT


def _startup_wait_seconds(profile: TargetAppProfile | None) -> float:
    """与 ``CaseBuilder._assemble`` 同口径的启动等待（``launch_strategy.wait_seconds``，默认 2 秒）。"""
    if profile is None:
        return 2.0
    try:
        return float(profile.launch_strategy.get("wait_seconds", 2))
    except TypeError, ValueError:
        return 2.0


def _resolve_identity(
    profile: TargetAppProfile | None,
    request: StressRequest,
    warnings: list[str],
) -> tuple[str, str, bool]:
    """解析压测用例的 ``bundle_name`` / ``main_ability``；返回 ``(bundle, ability, 是否占位)``。"""
    bundle = profile.bundle_name if profile is not None else ""
    if not bundle and request.target is not None and request.target.bundle_name:
        bundle = request.target.bundle_name
    placeholder = not bundle or bundle == PLACEHOLDER_BUNDLE
    if placeholder:
        bundle = PLACEHOLDER_BUNDLE
        warnings.append(
            f"没有可用的 bundle 名（Profile 缺失或 target 只给了应用名），"
            f"使用占位身份 {PLACEHOLDER_BUNDLE}，脚本缺少真实应用身份因而不可执行"
        )
    ability = (profile.main_ability if profile is not None else "") or PLACEHOLDER_ABILITY
    return bundle, ability, placeholder


def _source_id(request: StressRequest, profile: TargetAppProfile | None) -> str:
    """用例溯源 ID：优先复用源用例 ID，其次 Profile 的应用 ID，最后退到请求形态。"""
    if request.body == "existing_case" and request.case_id:
        return request.case_id
    if profile is not None:
        return profile.target_app_id
    if request.target is not None:
        return request.target.bundle_name or request.target.app_name or "inline-stress-request"
    return "inline-stress-request"


def _timeout_seconds(request: StressRequest) -> int:
    """``min(7200, max(300, 60 + iterations * 5 + duration_budget_seconds))``（计划 C3 逐字公式）。"""
    budget = request.duration_budget_seconds or 0
    computed = TIMEOUT_BASE_SECONDS + request.iterations * TIMEOUT_PER_ITERATION_SECONDS + budget
    return min(MAX_TIMEOUT_SECONDS, max(MIN_TIMEOUT_SECONDS, computed))


__all__ = [
    "DEFAULT_FIXED_INPUT_TEXT",
    "KIND_TITLE_ZH",
    "MAX_PAGE_ANCHORS",
    "MAX_TIMEOUT_SECONDS",
    "MIN_TIMEOUT_SECONDS",
    "STRESS_COUNT_KEYS",
    "TIMEOUT_BASE_SECONDS",
    "TIMEOUT_PER_ITERATION_SECONDS",
    "StressRequest",
    "build_stress_case",
    "steps_from_exploration_actions",
]
