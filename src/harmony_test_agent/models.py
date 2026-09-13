"""定义测试运行、页面感知、工具调用及产物交换所使用的领域模型。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    """返回带 UTC 时区信息的当前时间。"""
    return datetime.now(UTC)


class RunMode(StrEnum):
    """列出测试运行支持的任务模式。"""

    REGRESSION = "regression"
    EXPLORATION = "exploration"
    STABILITY = "stability"
    REPRODUCTION = "reproduction"


class RunState(StrEnum):
    """描述测试运行从创建到完成或失败的生命周期状态。"""

    CREATED = "created"
    RESOLVING_TARGET = "resolving_target"
    WAITING_TARGET_SELECTION = "waiting_target_selection"
    PROBING_TARGET = "probing_target"
    PROFILE_REVALIDATING = "profile_revalidating"
    DISCOVERING = "discovering"
    PROFILE_DRAFTING = "profile_drafting"
    PROFILE_VERIFYING = "profile_verifying"
    PREFLIGHT = "preflight"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    GRAPH_UPDATING = "graph_updating"
    SCRIPT_GENERATING = "script_generating"
    SCRIPT_EXECUTING = "script_executing"
    PROFILE_PROMOTING = "profile_promoting"
    COMPLETED = "completed"
    FAILED_DEVICE = "failed_device"
    FAILED_MODEL = "failed_model"
    FAILED_ELEMENT = "failed_element"
    FAILED_ACTION = "failed_action"
    FAILED_ASSERTION = "failed_assertion"
    FAILED_SCRIPT = "failed_script"
    FAILED_TARGET_RESOLUTION = "failed_target_resolution"
    FAILED_TARGET_PROBE = "failed_target_probe"
    FAILED_DISCOVERY = "failed_discovery"
    FAILED_PROFILE_VERIFICATION = "failed_profile_verification"
    FAILED_PROFILE_PROMOTION = "failed_profile_promotion"
    STOPPED_BY_USER = "stopped_by_user"


TERMINAL_STATES = {
    RunState.COMPLETED,
    RunState.FAILED_DEVICE,
    RunState.FAILED_MODEL,
    RunState.FAILED_ELEMENT,
    RunState.FAILED_ACTION,
    RunState.FAILED_ASSERTION,
    RunState.FAILED_SCRIPT,
    RunState.FAILED_TARGET_RESOLUTION,
    RunState.FAILED_TARGET_PROBE,
    RunState.FAILED_DISCOVERY,
    RunState.FAILED_PROFILE_VERIFICATION,
    RunState.FAILED_PROFILE_PROMOTION,
    RunState.STOPPED_BY_USER,
}


class EventType(StrEnum):
    """列出可持久化并推送给客户端的运行事件类型。"""

    RUN_STARTED = "run_started"
    TARGET_CANDIDATES_FOUND = "target_candidates_found"
    TARGET_RESOLVED = "target_resolved"
    TARGET_STARTED = "target_started"
    PROFILE_FOUND = "profile_found"
    PROFILE_REVALIDATION_STARTED = "profile_revalidation_started"
    PROFILE_REVALIDATION_FINISHED = "profile_revalidation_finished"
    DISCOVERY_STARTED = "discovery_started"
    DISCOVERY_PROGRESS = "discovery_progress"
    DISCOVERY_FINISHED = "discovery_finished"
    DISCOVERY_PATH_BLOCKED = "discovery_path_blocked"
    LOCATOR_CANDIDATE_OBSERVED = "locator_candidate_observed"
    PROFILE_LIVE_MODE = "profile_live_mode"
    PROFILE_DRAFT_SAVED = "profile_draft_saved"
    PROFILE_VERIFICATION_ROUND_FINISHED = "profile_verification_round_finished"
    HYPIUM_REPLAY_FINISHED = "hypium_replay_finished"
    PROFILE_PROMOTED = "profile_promoted"
    ORIGINAL_TASK_STARTED = "original_task_started"
    PREFLIGHT_PASSED = "preflight_passed"
    SCREEN_CAPTURED = "screen_captured"
    ELEMENTS_DETECTED = "elements_detected"
    PLAN_CREATED = "plan_created"
    ACTION_STARTED = "action_started"
    ACTION_FINISHED = "action_finished"
    ASSERTION_PASSED = "assertion_passed"
    ASSERTION_FAILED = "assertion_failed"
    PAGE_DISCOVERED = "page_discovered"
    EDGE_CREATED = "edge_created"
    SCRIPT_GENERATED = "script_generated"
    EXECUTION_STARTED = "execution_started"
    EXECUTION_FINISHED = "execution_finished"
    RUN_FAILED = "run_failed"
    RUN_FINISHED = "run_finished"


class ToolName(StrEnum):
    """列出规划器可以请求的受控 UI 工具。"""

    INSPECT_SCREEN = "inspect_screen"
    OPEN_APP = "open_app"
    CLICK_ELEMENT = "click_element"
    CLICK_COORDINATE = "click_coordinate"
    INPUT_TEXT = "input_text"
    SWIPE = "swipe"
    BACK = "back"
    WAIT = "wait"
    ASSERT_VISIBLE = "assert_visible"
    ASSERT_NOT_VISIBLE = "assert_not_visible"
    ASSERT_TEXT = "assert_text"
    FINISH = "finish"


class LocatorKind(StrEnum):
    """标识元素定位候选所采用的匹配方式。"""

    KEY = "key"
    ID = "id"
    TEXT = "text"
    TYPE_TEXT = "type_text"
    SPATIAL = "spatial"
    VLM_BBOX = "vlm_bbox"
    COORDINATE = "coordinate"


class BoundingBox(BaseModel):
    """表示屏幕坐标系中的矩形区域，并提供尺寸与边界判断。"""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        """返回非负矩形宽度。"""
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        """返回非负矩形高度。"""
        return max(0, self.bottom - self.top)

    @property
    def area(self) -> int:
        """返回矩形的非负面积。"""
        return self.width * self.height

    @property
    def center(self) -> tuple[int, int]:
        """返回矩形中心点的整数屏幕坐标。"""
        return ((self.left + self.right) // 2, (self.top + self.bottom) // 2)

    def within(self, width: int, height: int) -> bool:
        """判断矩形是否完整位于给定屏幕尺寸内。"""
        return 0 <= self.left < self.right <= width and 0 <= self.top < self.bottom <= height


class LocatorCandidate(BaseModel):
    """记录一个元素定位候选及其置信分数。"""

    kind: LocatorKind
    value: str
    score: float = Field(default=1, ge=0, le=1)


class UIElement(BaseModel):
    """描述从界面层级或视觉模型中识别出的可交互元素。"""

    element_id: str
    content: str = ""
    type: str = ""
    bbox: BoundingBox | None = None
    key: str = ""
    id: str = ""
    description: str = ""
    clickable: bool = False
    editable: bool = False
    scrollable: bool = False
    enabled: bool = True
    selected: bool = False
    score: float = Field(default=1, ge=0, le=1)
    source: str = "ui_hierarchy"
    locator_candidates: list[LocatorCandidate] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScreenSnapshot(BaseModel):
    """汇总一次屏幕采集的图像、页面信息和识别元素。"""

    snapshot_id: str
    run_id: str
    captured_at: datetime = Field(default_factory=utc_now)
    image_path: Path
    image_sha256: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    page_title: str = ""
    page_path: str = "unknown"
    source: str = "hdc"
    hierarchy_path: Path | None = None
    elements: list[UIElement] = Field(default_factory=list)
    summary: str = ""


class TargetQuery(BaseModel):
    """Identifies an installed target by human-readable label and/or exact bundle name."""

    app_name: str | None = None
    bundle_name: str | None = None

    @model_validator(mode="after")
    def require_identifier(self) -> TargetQuery:
        self.app_name = self.app_name.strip() if self.app_name else None
        self.bundle_name = self.bundle_name.strip() if self.bundle_name else None
        if not self.app_name and not self.bundle_name:
            raise ValueError("app_name or bundle_name is required")
        return self


class ExplorationPolicy(BaseModel):
    """Deterministic discovery limits and explicit opt-ins for sensitive actions."""

    enabled: bool = True
    max_pages: int = Field(default=20, ge=1, le=20)
    max_actions_per_page: int = Field(default=8, ge=1, le=8)
    max_duration_seconds: int = Field(default=900, ge=1, le=900)
    fixed_input_text: str = Field(default="OpenHarmony", min_length=1, max_length=200)
    settle_timeout_seconds: int = Field(default=1, ge=0, le=30)
    restore_retries: int = Field(default=2, ge=0, le=3)
    min_interaction_kinds: int = Field(default=2, ge=1, le=3)
    advisor_enabled: bool = True
    advisor_max_actions: int = Field(default=4, ge=1, le=8)
    advisor_history_turns: int = Field(default=8, ge=2, le=30)
    allow_login: bool = False
    allow_permission: bool = False
    allow_submit: bool = False
    allow_publish: bool = False
    allow_download: bool = False

    default_allowed_actions: ClassVar[frozenset[str]] = frozenset(
        {"navigate", "swipe", "back", "input_fixed_text", "read_only_assertion"}
    )
    explicit_actions: ClassVar[frozenset[str]] = frozenset({"login", "permission", "submit", "publish", "download"})
    always_forbidden_actions: ClassVar[frozenset[str]] = frozenset({"payment", "delete", "uninstall", "clear_data"})

    def allows(self, action: str) -> bool:
        """Return the deterministic policy decision for a normalized action category."""
        normalized = action.strip().lower()
        if normalized in self.always_forbidden_actions:
            return False
        if normalized in self.default_allowed_actions:
            return True
        attribute = f"allow_{normalized}"
        return bool(getattr(self, attribute, False)) if normalized in self.explicit_actions else False


class ProfileStatus(StrEnum):
    """Profile lifecycle states persisted by the registry."""

    DRAFT = "draft"
    CANDIDATE = "candidate"
    VERIFIED = "verified"
    SUPERSEDED = "superseded"
    INVALID = "invalid"
    ABSENT = "absent"


class ConfidenceLevel(StrEnum):
    """Cross-restart confidence assigned to a reusable locator or assertion."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AppVersion(BaseModel):
    version_name: str | None = None
    version_code: int | None = Field(default=None, ge=0)
    signature_sha256: str | None = None


class DeviceCompatibility(BaseModel):
    validated_device_types: list[str] = Field(default_factory=list)
    validated_resolutions: list[tuple[int, int]] = Field(default_factory=list)


class ProfileProvenance(BaseModel):
    discovery_run_id: str | None = None
    discovered_at: datetime = Field(default_factory=utc_now)
    verified_at: datetime | None = None
    hypium_replay_run_ids: list[str] = Field(default_factory=list)
    generator_version: str = "profile-discovery-v1"
    evidence: dict[str, Any] = Field(default_factory=dict)


class StableLocator(BaseModel):
    """A reusable locator with cross-restart stability evidence."""

    model_config = ConfigDict(extra="allow")
    name: str
    page_signature: str = ""
    key: str = ""
    id: str = ""
    text: str = ""
    type: str = ""
    locator_priority: int = Field(default=1, ge=1)
    confidence: ConfidenceLevel = ConfidenceLevel.HIGH
    observed_rounds: int = Field(default=0, ge=0)
    unique_match_rounds: int = Field(default=0, ge=0)
    observed_resolutions: list[tuple[int, int]] = Field(default_factory=list)
    source: str = "ui_hierarchy"
    first_observed_at: datetime | None = None
    last_observed_at: datetime | None = None
    dynamic_pattern: str | None = None
    evidence_snapshot_ids: list[str] = Field(default_factory=list)
    coordinate: tuple[int, int] | None = None
    resolution_bound: tuple[int, int] | None = None
    warning: str | None = None


class AssertionDefinition(BaseModel):
    """An application-level UI assertion and the observations that support it."""

    name: str
    kind: str
    target: str
    expected: Any = None
    page_signature: str = ""
    confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM
    observed_rounds: int = Field(default=0, ge=0)
    evidence_snapshot_ids: list[str] = Field(default_factory=list)


class TargetAppProfile(BaseModel):
    """Versioned target identity, launch policy, locators, assertions, and audit evidence."""

    model_config = ConfigDict(extra="allow")
    schema_version: Literal[1, 2] = 2
    status: ProfileStatus = ProfileStatus.DRAFT
    locked: bool = False
    target_app_id: str
    display_name: str
    bundle_name: str
    main_ability: str = "EntryAbility"
    module_name: str | None = None
    app_version: AppVersion = Field(default_factory=AppVersion)
    device_compatibility: DeviceCompatibility = Field(default_factory=DeviceCompatibility)
    device_selector: dict[str, Any] = Field(default_factory=dict)
    launch_strategy: dict[str, Any] = Field(default_factory=dict)
    reset_strategy: dict[str, Any] = Field(default_factory=dict)
    test_data_strategy: dict[str, Any] = Field(default_factory=dict)
    permission_and_popup_strategy: dict[str, Any] = Field(default_factory=dict)
    stable_locator_inventory: list[StableLocator] = Field(default_factory=list)
    assertion_inventory: list[AssertionDefinition] = Field(default_factory=list)
    core_flows: list[dict[str, Any]] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)
    provenance: ProfileProvenance = Field(default_factory=ProfileProvenance)

    @model_validator(mode="after")
    def validate_profile_invariants(self) -> TargetAppProfile:
        if self.reset_strategy.get("clear_app_data") is True:
            raise ValueError("Profile reset_strategy cannot clear application data")
        forbidden = {"payment", "delete", "uninstall", "clear_data"}
        for action in forbidden:
            if self.permission_and_popup_strategy.get(action) not in {None, "always_blocked"}:
                raise ValueError(f"Profile cannot allow permanently forbidden action: {action}")
        for locator in self.stable_locator_inventory:
            if locator.coordinate is not None and (locator.resolution_bound is None or not locator.warning):
                raise ValueError("coordinate locators require a resolution bound and warning")
        if self.status == ProfileStatus.VERIFIED:
            if self.provenance.verified_at is None:
                raise ValueError("verified Profile requires provenance.verified_at")
            replay_ids = self.provenance.hypium_replay_run_ids
            if len(replay_ids) != 3 or len(set(replay_ids)) != 3:
                raise ValueError("verified Profile requires three unique Hypium replay run IDs")
            if not self.provenance.evidence.get("verification_passed"):
                raise ValueError("verified Profile requires passed device verification evidence")
            if len({item.page_signature for item in self.stable_locator_inventory}) < 3:
                raise ValueError("verified Profile requires locators on three pages")
            if len(self.assertion_inventory) < 2:
                raise ValueError("verified Profile requires two application assertions")
        return self


class ResolvedTarget(BaseModel):
    """Immutable application identity produced by deterministic target resolution."""

    model_config = ConfigDict(frozen=True)
    target_app_id: str
    display_name: str
    bundle_name: str
    main_ability: str
    module_name: str | None = None
    version_name: str | None = None
    version_code: int | None = Field(default=None, ge=0)
    signature_sha256: str | None = None
    device_id: str
    source: Literal["verified_profile", "installed_app", "explicit_override"]
    profile_snapshot: TargetAppProfile | None = None


class PlannedStep(BaseModel):
    """描述规划器生成的单个测试步骤及其预期结果。"""

    step_id: str
    instruction: str
    tool: ToolName
    target: str | None = None
    text: str | None = None
    coordinate: tuple[int, int] | None = None
    direction: Literal["up", "down", "left", "right"] | None = None
    wait_seconds: float | None = None
    expected: str | None = None


class PlanResult(BaseModel):
    """封装一次规划结果及其完成条件。"""

    goal: str
    steps: list[PlannedStep]
    model_used: str
    mock: bool = False


class ToolDecision(BaseModel):
    """表示模型针对当前步骤选择的工具及其参数。"""

    tool: ToolName
    target: str | None = None
    text: str | None = None
    coordinate: tuple[int, int] | None = None
    direction: Literal["up", "down", "left", "right"] | None = None
    wait_seconds: float | None = None
    reasoning: str = ""


class CommandResult(BaseModel):
    """记录底层设备命令的输出、返回码和执行耗时。"""

    command: str
    args: list[str] = Field(default_factory=list)
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        """判断底层命令是否以零返回码成功结束。"""
        return self.returncode == 0 and not self.timed_out


class AssertionResult(BaseModel):
    """记录一次界面断言的目标、结果和说明。"""

    kind: str
    target: str
    passed: bool
    message: str


class ActionResult(BaseModel):
    """记录一个工具步骤的命令、断言、定位器及时间信息。"""

    step_id: str
    tool: ToolName
    params: dict[str, Any] = Field(default_factory=dict)
    success: bool
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime = Field(default_factory=utc_now)
    duration_ms: int = 0
    before_snapshot_id: str | None = None
    after_snapshot_id: str | None = None
    command: CommandResult | None = None
    assertion: AssertionResult | None = None
    locator: LocatorCandidate | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)


class PageNode(BaseModel):
    """表示运行过程中发现的一个页面节点，并兼容旧版 image_path。"""

    node_id: str
    signature: str
    page_path: str
    title: str
    snapshot_id: str
    artifact_path: Path | None = None
    image_path: Path | None = None
    discovered_order: int
    element_count: int

    @model_validator(mode="after")
    def populate_artifact_path(self) -> PageNode:
        """为旧 Trace 从绝对 image_path 提取 Run 内相对截图路径。"""
        if self.artifact_path is not None:
            if self.artifact_path.is_absolute():
                raise ValueError("artifact_path must be relative to the Run directory")
            return self
        if self.image_path is None:
            raise ValueError("artifact_path or image_path is required")
        parts = self.image_path.parts
        if self.snapshot_id:
            run_markers = [index for index, part in enumerate(parts) if part.startswith("run-")]
            if run_markers and run_markers[-1] + 1 < len(parts):
                self.artifact_path = Path(*parts[run_markers[-1] + 1 :])
                return self
        for directory in ("screens", "layouts", "commands", "generated", "hypium", "reports"):
            if directory in parts:
                index = len(parts) - 1 - list(reversed(parts)).index(directory)
                self.artifact_path = Path(*parts[index:])
                return self
        self.artifact_path = Path("screens") / self.image_path.name
        return self


class PageEdge(BaseModel):
    """表示由某个动作触发的页面跳转关系。"""

    edge_id: str
    source: str
    target: str
    action: ToolName
    target_description: str = ""
    confidence: float = Field(default=1, ge=0, le=1)


class PageGraph(BaseModel):
    """保存运行过程中累计构建的页面节点与跳转边。"""

    nodes: list[PageNode] = Field(default_factory=list)
    edges: list[PageEdge] = Field(default_factory=list)


class GeneratedArtifact(BaseModel):
    """记录生成的 Hypium 脚本、用途、完整性及可回放资格。"""

    python_path: Path
    config_path: Path
    metadata_path: Path
    generated_at: datetime = Field(default_factory=utc_now)
    purpose: Literal["acceptance", "diagnostic"] = "diagnostic"
    replay_eligible: bool = False
    source_agent_outcome: Literal["completed", "failed", "stopped", "unknown"] = "unknown"
    source_action_count: int = 0
    included_action_count: int = 0
    omitted_action_count: int = 0
    counts: dict[str, int] = Field(default_factory=dict)
    incomplete_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ReplayError(BaseModel):
    """描述回放资格、进程或生成结果产生的结构化错误。"""

    kind: Literal["ineligible", "timeout", "process_exit", "missing_result", "invalid_result", "script_error"]
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ReplayResult(BaseModel):
    """记录一次回放的状态、结构化错误与 Run 内相对证据路径。"""

    attempt: int
    command: CommandResult
    report_path: Path | None = None
    passed: bool = False
    status: Literal["passed", "failed", "timed_out", "ineligible", "invalid_result"] = "failed"
    exit_code: int | None = None
    timed_out: bool = False
    error: ReplayError | None = None
    evidence_paths: list[str] = Field(default_factory=list)
    generated_result_path: str | None = None
    generated_result: dict[str, Any] | None = None

    @model_validator(mode="after")
    def populate_legacy_status(self) -> ReplayResult:
        """从旧 ReplayResult 的 command/passed 补全退出码、超时和状态。"""
        if self.exit_code is None:
            self.exit_code = self.command.returncode
        self.timed_out = self.timed_out or self.command.timed_out
        if self.timed_out:
            self.status = "timed_out"
        elif self.passed and self.status == "failed":
            self.status = "passed"
        return self


class RunEvent(BaseModel):
    """表示带有运行内序号和结构化负载的事件。"""

    event_id: int
    run_id: str
    type: EventType
    timestamp: datetime = Field(default_factory=utc_now)
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)


class RunTrace(BaseModel):
    """Self-contained record of a run, including frozen target and Profile inputs."""

    run_id: str
    target_app_id: str
    task: str
    mode: RunMode = RunMode.REGRESSION
    device_id: str
    state: RunState = RunState.CREATED
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    model_used: str = "mock"
    model_mock: bool = True
    phase: Literal["bootstrap", "task"] = "bootstrap"
    provisional: bool = False
    live_mode: bool = False
    target_query: TargetQuery | None = None
    resolved_target: ResolvedTarget | None = None
    profile_status_at_start: ProfileStatus | None = None
    profile_snapshot: TargetAppProfile | None = None
    exploration_policy: ExplorationPolicy = Field(default_factory=ExplorationPolicy)
    discovery_result: dict[str, Any] | None = None
    verification_result: dict[str, Any] | None = None
    target_candidates: list[dict[str, Any]] = Field(default_factory=list)
    profile_validation_generated: GeneratedArtifact | None = None
    profile_validation_replays: list[ReplayResult] = Field(default_factory=list)
    plan: list[PlannedStep] = Field(default_factory=list)
    snapshots: list[ScreenSnapshot] = Field(default_factory=list)
    actions: list[ActionResult] = Field(default_factory=list)
    assertions: list[AssertionResult] = Field(default_factory=list)
    graph: PageGraph = Field(default_factory=PageGraph)
    events: list[RunEvent] = Field(default_factory=list)
    generated: GeneratedArtifact | None = None
    replays: list[ReplayResult] = Field(default_factory=list)
    agent_outcome: Literal["completed", "failed", "stopped", "unknown"] = "unknown"
    agent_error: str | None = None
    replay_status: Literal["not_requested", "not_eligible", "pending", "passed", "failed", "partial"] = "not_requested"
    replay_total: int = 0
    replay_completed: int = 0
    replay_passed: int = 0
    error: str | None = None

    @model_validator(mode="after")
    def populate_compatibility_summary(self) -> RunTrace:
        """同步冻结 Profile，并从旧 Trace 补全 Agent 与独立回放汇总。"""
        if (
            self.profile_snapshot is None
            and self.resolved_target is not None
            and self.resolved_target.profile_snapshot is not None
        ):
            self.profile_snapshot = self.resolved_target.profile_snapshot.model_copy(deep=True)
        if self.profile_status_at_start is None and self.profile_snapshot is not None:
            self.profile_status_at_start = self.profile_snapshot.status

        legacy_replay_failure = (
            self.state == RunState.FAILED_SCRIPT
            and bool(self.actions)
            and self.actions[-1].tool == ToolName.FINISH
            and self.actions[-1].success
        )
        if self.agent_outcome == "unknown":
            if self.state == RunState.COMPLETED or legacy_replay_failure:
                self.agent_outcome = "completed"
            elif self.state == RunState.STOPPED_BY_USER:
                self.agent_outcome = "stopped"
            elif self.state in TERMINAL_STATES:
                self.agent_outcome = "failed"
        if self.agent_error is None and self.error and not legacy_replay_failure:
            self.agent_error = self.error
        if self.replays:
            if self.replay_total == 0:
                self.replay_total = len(self.replays)
            self.replay_completed = len(self.replays)
            self.replay_passed = sum(item.passed for item in self.replays)
            if self.replay_passed == self.replay_total and self.replay_completed == self.replay_total:
                self.replay_status = "passed"
            elif self.replay_passed:
                self.replay_status = "partial"
            elif self.replay_completed:
                self.replay_status = "failed"
        return self


class VisionElement(BaseModel):
    """描述视觉模型识别出的候选界面元素。"""

    content: str
    type: str = "unknown"
    bbox: BoundingBox
    clickable: bool = False
    editable: bool = False
    score: float = Field(default=0.5, ge=0, le=1)
    reason: str = ""


class VisionObservation(BaseModel):
    """封装视觉模型对当前页面的标题、摘要和元素观察。"""

    page_title: str = ""
    summary: str = ""
    elements: list[VisionElement] = Field(default_factory=list)


class RunRequest(BaseModel):
    """Creates a run from a target query while retaining the legacy target_app_id entry point."""

    target: TargetQuery | None = None
    target_app_id: str | None = None
    task: str = "启动应用，探索可达页面，验证返回和重启恢复"
    mode: RunMode = RunMode.REGRESSION
    device_id: str | None = None
    max_steps: int = Field(default=20, ge=1, le=100)
    auto_generate: bool = True
    auto_execute: bool = False
    exploration_policy: ExplorationPolicy = Field(default_factory=ExplorationPolicy)
    temporary_test: bool = False
    bootstrap_only: bool = False

    @model_validator(mode="after")
    def normalize_target_contract(self) -> RunRequest:
        if self.target is None and self.target_app_id:
            # Legacy app IDs are resolved by the compatibility adapter/registry.
            self.target = TargetQuery(app_name=self.target_app_id)
        if self.target is None:
            # Keep the historical default during the documented migration period.
            self.target_app_id = self.target_app_id or "zhihu-plus"
            self.target = TargetQuery(app_name=self.target_app_id)
        elif not self.target_app_id:
            self.target_app_id = self.target.bundle_name or self.target.app_name
        return self
