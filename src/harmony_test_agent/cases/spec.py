"""用例中间表示（Case IR）。

把「录制到的操作」与「产出的脚本」解耦：Live 运行轨迹、DC 会话录制、压测请求、
缺陷报告都先归一化成本文件的 ``TestCaseSpec``，再由两个 emitter 分别渲染为

* ``generation/standalone.py`` — 独立 ``UiDriver`` 脚本（保留既有回放门禁契约）
* ``generation/xdevice_case.py`` — 官方 ``devicetest`` TestCase 工程

单向依赖：本文件只从 ``models.py`` import，``models.py`` 永不 import ``cases/``。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..models import (
    AnomalyKind,
    ConfidenceLevel,
    ExecutionAnalysis,
    LocatorKind,
    ScenarioKind,
    StressKind,
    utc_now,
)

# ---------------------------------------------------------------------------
# 动作与定位器
# ---------------------------------------------------------------------------


class StepAction(StrEnum):
    """IR 步骤的动作类型。"""

    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    LONG_CLICK = "long_click"
    INPUT_TEXT = "input_text"
    CLEAR_TEXT = "clear_text"
    SWIPE = "swipe"
    DRAG = "drag"
    FLING = "fling"
    BACK = "back"
    KEY_EVENT = "key_event"
    WAIT = "wait"
    SCREENSHOT = "screenshot"
    SWITCH_STATUS = "switch_status"
    START_APP = "start_app"
    STOP_APP = "stop_app"
    CHECK = "check"
    NOOP_COMMENT = "noop_comment"


# 独立脚本在「无任何可回放操作」时必须复现的历史字面量。
NO_REPLAYABLE_COMMENT = "no replayable operations"

# IR 允许承载的定位器类型；SPATIAL / VLM_BBOX 在 build 期解析为 coordinate。
IR_LOCATOR_KINDS: frozenset[LocatorKind] = frozenset(
    {LocatorKind.KEY, LocatorKind.ID, LocatorKind.TEXT, LocatorKind.TYPE_TEXT, LocatorKind.COORDINATE}
)


class MatchMode(StrEnum):
    """文本/属性匹配模式。"""

    EQUALS = "equals"
    CONTAINS = "contains"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    REGEXP = "regexp"


class LocatorEvidence(BaseModel):
    """定位器的 Profile 证据；这是「Profile 证据流入用例」的通道。"""

    observed_rounds: int = Field(default=0, ge=0)
    unique_match_rounds: int = Field(default=0, ge=0)
    dynamic_pattern: str | None = None
    page_signature: str = ""
    confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM
    source: str = ""


class LocatorSpec(BaseModel):
    """用例 IR 的定位器；可序列化、可审计，并带证据与警告。"""

    model_config = ConfigDict(extra="forbid")

    kind: LocatorKind
    value: str = ""
    match: MatchMode = MatchMode.EQUALS
    coordinate: tuple[int, int] | None = None
    resolution_bound: tuple[int, int] | None = None
    target_label: str = ""
    evidence: LocatorEvidence | None = None
    warning: str | None = None

    @model_validator(mode="after")
    def validate_locator_invariants(self) -> LocatorSpec:
        """对齐 ``models.py`` 的坐标不变式：坐标定位器必须带解析边界与警告。"""
        if self.kind not in IR_LOCATOR_KINDS:
            raise ValueError(f"locator kind {self.kind} must be resolved to coordinate before entering the case IR")
        if self.kind == LocatorKind.COORDINATE:
            if self.coordinate is None:
                raise ValueError("coordinate locator requires a coordinate")
            if self.resolution_bound is None or not self.warning:
                raise ValueError("coordinate locator requires a resolution bound and warning")
        return self


# ---------------------------------------------------------------------------
# 检查点
# ---------------------------------------------------------------------------


class CheckpointKind(StrEnum):
    """检查点原语；两种 emitter 都要能渲染。"""

    ELEMENT_EXISTS = "element_exists"
    ELEMENT_ABSENT = "element_absent"
    TEXT_EQUALS = "text_equals"
    TEXT_CONTAINS = "text_contains"
    PROPERTY_EQUALS = "property_equals"
    TOAST = "toast"
    CURRENT_APP = "current_app"
    PAGE_SIGNATURE = "page_signature"
    SCREENSHOT_CAPTURED = "screenshot_captured"


# ``driver.check_component(expected_equal=True, <prop>=...)`` 支持的属性；
# ``selected`` 只能走 ``get_component_property`` + ``Assert.equal``。
COMPONENT_PROPERTIES: tuple[str, ...] = (
    "id",
    "text",
    "key",
    "type",
    "enabled",
    "focused",
    "clickable",
    "scrollable",
    "checked",
    "checkable",
)
GET_PROPERTY_ONLY: tuple[str, ...] = ("selected",)
CHECKPOINT_PROPERTIES: tuple[str, ...] = COMPONENT_PROPERTIES + GET_PROPERTY_ONLY


class CheckpointSpec(BaseModel):
    """一个可独立渲染为断言的检查点。"""

    model_config = ConfigDict(extra="forbid")

    kind: CheckpointKind
    message_zh: str
    locator: LocatorSpec | None = None
    expected: Any = None
    property_name: (
        Literal[
            "id",
            "text",
            "key",
            "type",
            "enabled",
            "focused",
            "clickable",
            "scrollable",
            "checked",
            "checkable",
            "selected",
        ]
        | None
    ) = None
    soft: bool = False
    wait_seconds: float | None = None
    toast_fuzzy: Literal["equal", "contain"] = "equal"
    page_path: str = ""
    anchors: list[LocatorSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_checkpoint_invariants(self) -> CheckpointSpec:
        if self.kind == CheckpointKind.CURRENT_APP and not self.expected:
            raise ValueError("current_app checkpoint requires a non-empty expected bundle name")
        if self.kind == CheckpointKind.PAGE_SIGNATURE and not self.anchors:
            raise ValueError("page_signature checkpoint requires at least one anchor")
        if self.kind == CheckpointKind.PROPERTY_EQUALS and self.property_name is None:
            raise ValueError("property_equals checkpoint requires property_name")
        if self.kind == CheckpointKind.PROPERTY_EQUALS and not self.locator:
            raise ValueError("property_equals checkpoint requires a locator")
        return self


# ---------------------------------------------------------------------------
# 步骤与用例
# ---------------------------------------------------------------------------


class TestStepSpec(BaseModel):
    """IR 中的一个业务步骤（可挂多个动作后求值的检查点）。"""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    index: int = Field(ge=1)
    action: StepAction
    title_zh: str
    locator: LocatorSpec | None = None
    text: str | None = None
    param_ref: str | None = None
    coordinate: tuple[int, int] | None = None
    direction: Literal["up", "down", "left", "right"] | None = None
    # 精确滑动（计划 5.5）：同时给出 start/end 时脚本渲染 driver.slide，比方向滑动更可控。
    start: tuple[int, int] | None = None
    end: tuple[int, int] | None = None
    slide_time: float | None = None
    wait_seconds: float | None = None
    key: str | None = None
    checked: bool | None = None
    drag_to: tuple[int, int] | None = None
    comment: str = ""
    checkpoints: list[CheckpointSpec] = Field(default_factory=list)
    timeout_seconds: float | None = None

    @property
    def hard_checkpoints(self) -> list[CheckpointSpec]:
        return [item for item in self.checkpoints if not item.soft]


class SetupSpec(BaseModel):
    """确定性初始化：停应用 → 启动 → 等待。"""

    model_config = ConfigDict(extra="forbid")

    stop_app_first: bool = True
    start_app: bool = True
    startup_wait_seconds: float = 2.0
    listen_toast: bool = False
    pre_steps: list[TestStepSpec] = Field(default_factory=list)


class TeardownSpec(BaseModel):
    """收尾：末次截图（独立脚本契约）、可选停应用。"""

    model_config = ConfigDict(extra="forbid")

    capture_final_screenshot: bool = True
    stop_app: bool = False
    post_steps: list[TestStepSpec] = Field(default_factory=list)


class TestParamSpec(BaseModel):
    """数据驱动参数声明。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    type: Literal["string", "int", "float", "bool"] = "string"
    default: Any = None
    description_zh: str = ""


class DataSetSpec(BaseModel):
    """一组参数取值（数据集）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    values: dict[str, Any] = Field(default_factory=dict)


class StressSpec(BaseModel):
    """压力测试循环体与预算。"""

    model_config = ConfigDict(extra="forbid")

    kind: StressKind
    iterations: int = Field(default=20, ge=1, le=5000)
    duration_budget_seconds: int | None = Field(default=None, ge=10, le=7200)
    fail_break: bool = True
    fail_times: int = Field(default=0, ge=0)
    continues_fail: bool = False
    body_steps: list[TestStepSpec] = Field(default_factory=list)
    per_iteration_checkpoints: list[CheckpointSpec] = Field(default_factory=list)
    inter_iteration_wait_seconds: float = Field(default=0.5, ge=0, le=10)
    sample_memory_every: int = Field(default=0, ge=0, le=1000)
    memory_growth_threshold_kb: int | None = None
    step_log_interval: int = Field(default=10, ge=1)

    @property
    def needs_explicit_loop(self) -> bool:
        """时长预算或内存采样只能靠显式 ``for``/deadline 循环表达，``@loop`` 做不到。"""
        return self.duration_budget_seconds is not None or self.sample_memory_every > 0


class BugReproSpec(BaseModel):
    """缺陷复现语义：断言「期望行为」，通过 = 缺陷未复现。"""

    model_config = ConfigDict(extra="forbid")

    symptom: str
    symptom_kind: Literal["crash", "freeze", "white_screen", "unresponsive", "layout", "functional", "other"] = (
        "functional"
    )
    preconditions: list[str] = Field(default_factory=list)
    repro_steps_nl: list[str] = Field(default_factory=list)
    expected: str
    actual: str


class CaseProvenance(BaseModel):
    """用例来源溯源。"""

    model_config = ConfigDict(extra="forbid")

    source_kind: Literal["live_run", "dc_session", "bug_report", "stress_request", "manual", "profile_core_flow"]
    source_id: str
    profile_target_app_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    generator_version: str = "case-ir-v1"


class TestCaseSpec(BaseModel):
    """可复用 UI 自动化测试用例的中间表示。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    case_id: str = Field(pattern=r"^case-[A-Za-z0-9T.Z-]+-[a-f0-9]{6}$")
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    title_zh: str = Field(min_length=1, max_length=200)
    scenario: ScenarioKind
    status: Literal["draft", "active", "archived"] = "active"
    tags: list[str] = Field(default_factory=list)
    bundle_name: str = Field(pattern=r"^[A-Za-z0-9_.]{1,128}$")
    main_ability: str = "EntryAbility"
    device_sn: str | None = None
    timeout_seconds: int = Field(default=300, ge=30, le=7200)
    params: list[TestParamSpec] = Field(default_factory=list)
    datasets: list[DataSetSpec] = Field(default_factory=list)
    setup: SetupSpec = Field(default_factory=SetupSpec)
    steps: list[TestStepSpec] = Field(default_factory=list)
    teardown: TeardownSpec = Field(default_factory=TeardownSpec)
    stress: StressSpec | None = None
    bug_repro: BugReproSpec | None = None
    provenance: CaseProvenance

    @model_validator(mode="after")
    def validate_invariants(self) -> TestCaseSpec:
        """七条 IR 不变式；任一违反即拒绝构造。"""
        # 1. STRESS ⟺ stress 存在
        if (self.scenario == ScenarioKind.STRESS) != (self.stress is not None):
            raise ValueError("stress scenario requires a stress spec and vice versa")
        # 2. BUG_REPRODUCTION ⟹ bug_repro 存在
        if self.scenario == ScenarioKind.BUG_REPRODUCTION and self.bug_repro is None:
            raise ValueError("bug_reproduction scenario requires a bug_repro spec")
        # 3. 非压测用例至少一步，且 index 严格 1..N
        if self.scenario != ScenarioKind.STRESS and not self.steps:
            raise ValueError("non-stress case requires at least one step")
        self._validate_step_indices(self.steps, "steps")
        if self.stress is not None:
            self._validate_step_indices(self.stress.body_steps, "stress.body_steps")
        self._validate_step_indices(self.setup.pre_steps, "setup.pre_steps")
        self._validate_step_indices(self.teardown.post_steps, "teardown.post_steps")
        # 4. param_ref 必须能在 params 里找到
        known = {item.name for item in self.params}
        for step in self._all_steps():
            if step.param_ref and step.param_ref not in known:
                raise ValueError(f"step {step.step_id!r} references unknown param {step.param_ref!r}")
        # 5. 非 draft 用例必须至少一个硬检查点
        if self.status != "draft" and not self._hard_checkpoints():
            raise ValueError("a non-draft case requires at least one non-soft checkpoint")
        # 6. TOAST 检查点 ⟹ setup.listen_toast
        if any(cp.kind == CheckpointKind.TOAST for cp in self._all_checkpoints()) and not self.setup.listen_toast:
            raise ValueError("toast checkpoint requires setup.listen_toast")
        # 7. CURRENT_APP.expected / PAGE_SIGNATURE.anchors 非空（坐标 locator 由 LocatorSpec 自校验）
        for checkpoint in self._all_checkpoints():
            if checkpoint.kind == CheckpointKind.CURRENT_APP and not checkpoint.expected:
                raise ValueError("current_app checkpoint requires a non-empty expected bundle name")
            if checkpoint.kind == CheckpointKind.PAGE_SIGNATURE and not checkpoint.anchors:
                raise ValueError("page_signature checkpoint requires at least one anchor")
        return self

    # -- 内部工具 ---------------------------------------------------------

    def _all_steps(self) -> list[TestStepSpec]:
        steps = list(self.setup.pre_steps) + list(self.steps) + list(self.teardown.post_steps)
        if self.stress is not None:
            steps += list(self.stress.body_steps)
        return steps

    def _all_checkpoints(self) -> list[CheckpointSpec]:
        found: list[CheckpointSpec] = []
        for step in self._all_steps():
            found += list(step.checkpoints)
        if self.stress is not None:
            found += list(self.stress.per_iteration_checkpoints)
        return found

    def _hard_checkpoints(self) -> list[CheckpointSpec]:
        found = [cp for cp in self._all_checkpoints() if not cp.soft]
        return found

    @staticmethod
    def _validate_step_indices(steps: list[TestStepSpec], label: str) -> None:
        for position, step in enumerate(steps, start=1):
            if step.index != position:
                raise ValueError(f"{label}[{position - 1}].index must be {position}, got {step.index}")

    def renumber(self) -> TestCaseSpec:
        """把 steps（以及压测循环体）的 index 重排为严格 1..N。"""
        for position, step in enumerate(self.steps, start=1):
            step.index = position
        if self.stress is not None:
            for position, step in enumerate(self.stress.body_steps, start=1):
                step.index = position
        for position, step in enumerate(self.setup.pre_steps, start=1):
            step.index = position
        for position, step in enumerate(self.teardown.post_steps, start=1):
            step.index = position
        return self


# ---------------------------------------------------------------------------
# 用例库记录
# ---------------------------------------------------------------------------


class CaseRecord(BaseModel):
    """一条用例（含完整 IR）与它在库中的版本信息。"""

    case_id: str
    version: int = Field(ge=1)
    slug: str
    title_zh: str
    scenario: ScenarioKind
    status: Literal["draft", "active", "archived"] = "active"
    tags: list[str] = Field(default_factory=list)
    target_app_id: str | None = None
    bundle_name: str
    source_kind: str
    source_id: str
    created_at: datetime = Field(default_factory=utc_now)
    artifact_dir: str = ""
    spec: TestCaseSpec
    confidence: Literal["high", "medium", "low"] = "low"
    """质量分档（非阻断，来自入库时写下的 ``standalone/test_*.json``）。"""
    confidence_factors: list[str] = Field(default_factory=list)
    """质量顾虑清单（非阻断）。"""
    promotion_eligible: bool = False
    """能否作为 Profile 晋级证据；与「能否执行」解耦。"""
    promotion_blockers: list[str] = Field(default_factory=list)
    """不能作为 Profile 晋级证据的原因（provisional / live_mode）。"""


class CaseSummary(BaseModel):
    """用例列表项（不含 IR 主体）。"""

    case_id: str
    version: int = Field(ge=1)
    slug: str
    title_zh: str
    scenario: ScenarioKind
    status: Literal["draft", "active", "archived"] = "active"
    tags: list[str] = Field(default_factory=list)
    target_app_id: str | None = None
    bundle_name: str
    source_kind: str
    source_id: str
    created_at: datetime = Field(default_factory=utc_now)
    artifact_dir: str = ""
    step_count: int = 0
    hard_checkpoint_count: int = 0
    last_execution_status: str | None = None


class CaseExecutionRequest(BaseModel):
    """重跑一个用例的请求。"""

    model_config = ConfigDict(extra="forbid")

    engine: Literal["hypium_standalone", "xdevice_devicetest"] = "hypium_standalone"
    device_sn: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    attempt: int = Field(default=1, ge=1, le=3)
    version: int | None = None


class CaseExecutionRecord(BaseModel):
    """一次用例执行记录（含分析）。"""

    execution_id: str
    case_id: str
    version: int = Field(ge=1)
    engine: Literal["hypium_standalone", "xdevice_devicetest"]
    device_id: str = ""
    status: Literal["pending", "passed", "failed", "timed_out", "error", "invalid_result"] = "pending"
    passed: bool = False
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    report_path: str | None = None
    analysis: ExecutionAnalysis | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


__all__ = [
    "CHECKPOINT_PROPERTIES",
    "COMPONENT_PROPERTIES",
    "GET_PROPERTY_ONLY",
    "IR_LOCATOR_KINDS",
    "NO_REPLAYABLE_COMMENT",
    "AnomalyKind",
    "BugReproSpec",
    "CaseExecutionRecord",
    "CaseExecutionRequest",
    "CaseProvenance",
    "CaseRecord",
    "CaseSummary",
    "CheckpointKind",
    "CheckpointSpec",
    "DataSetSpec",
    "ExecutionAnalysis",
    "LocatorEvidence",
    "LocatorSpec",
    "MatchMode",
    "ScenarioKind",
    "SetupSpec",
    "StepAction",
    "StressKind",
    "StressSpec",
    "TeardownSpec",
    "TestParamSpec",
    "TestStepSpec",
    "TestCaseSpec",
]
