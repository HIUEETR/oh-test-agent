from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class RunMode(StrEnum):
    REGRESSION = "regression"
    EXPLORATION = "exploration"
    STABILITY = "stability"
    REPRODUCTION = "reproduction"


class RunState(StrEnum):
    CREATED = "created"
    PREFLIGHT = "preflight"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    GRAPH_UPDATING = "graph_updating"
    SCRIPT_GENERATING = "script_generating"
    SCRIPT_EXECUTING = "script_executing"
    COMPLETED = "completed"
    FAILED_DEVICE = "failed_device"
    FAILED_MODEL = "failed_model"
    FAILED_ELEMENT = "failed_element"
    FAILED_ACTION = "failed_action"
    FAILED_ASSERTION = "failed_assertion"
    FAILED_SCRIPT = "failed_script"
    STOPPED_BY_USER = "stopped_by_user"


TERMINAL_STATES = {
    RunState.COMPLETED,
    RunState.FAILED_DEVICE,
    RunState.FAILED_MODEL,
    RunState.FAILED_ELEMENT,
    RunState.FAILED_ACTION,
    RunState.FAILED_ASSERTION,
    RunState.FAILED_SCRIPT,
    RunState.STOPPED_BY_USER,
}


class EventType(StrEnum):
    RUN_STARTED = "run_started"
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
    KEY = "key"
    ID = "id"
    TEXT = "text"
    TYPE_TEXT = "type_text"
    SPATIAL = "spatial"
    VLM_BBOX = "vlm_bbox"
    COORDINATE = "coordinate"


class BoundingBox(BaseModel):
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def center(self) -> tuple[int, int]:
        return ((self.left + self.right) // 2, (self.top + self.bottom) // 2)

    def within(self, width: int, height: int) -> bool:
        return 0 <= self.left < self.right <= width and 0 <= self.top < self.bottom <= height


class LocatorCandidate(BaseModel):
    kind: LocatorKind
    value: str
    score: float = Field(default=1, ge=0, le=1)


class UIElement(BaseModel):
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


class StableLocator(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    key: str = ""
    id: str = ""
    text: str = ""
    type: str = ""
    locator_priority: int = 1


class TargetAppProfile(BaseModel):
    model_config = ConfigDict(extra="allow")
    target_app_id: str
    display_name: str
    bundle_name: str
    main_ability: str = "EntryAbility"
    device_selector: dict[str, Any] = Field(default_factory=dict)
    launch_strategy: dict[str, Any] = Field(default_factory=dict)
    reset_strategy: dict[str, Any] = Field(default_factory=dict)
    test_data_strategy: dict[str, Any] = Field(default_factory=dict)
    permission_and_popup_strategy: dict[str, Any] = Field(default_factory=dict)
    stable_locator_inventory: list[StableLocator] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)


class PlannedStep(BaseModel):
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
    goal: str
    steps: list[PlannedStep]
    model_used: str
    mock: bool = False


class ToolDecision(BaseModel):
    tool: ToolName
    target: str | None = None
    text: str | None = None
    coordinate: tuple[int, int] | None = None
    direction: Literal["up", "down", "left", "right"] | None = None
    wait_seconds: float | None = None
    reasoning: str = ""


class CommandResult(BaseModel):
    command: str
    args: list[str] = Field(default_factory=list)
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class AssertionResult(BaseModel):
    kind: str
    target: str
    passed: bool
    message: str


class ActionResult(BaseModel):
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
    node_id: str
    signature: str
    page_path: str
    title: str
    snapshot_id: str
    image_path: Path
    discovered_order: int
    element_count: int


class PageEdge(BaseModel):
    edge_id: str
    source: str
    target: str
    action: ToolName
    target_description: str = ""
    confidence: float = Field(default=1, ge=0, le=1)


class PageGraph(BaseModel):
    nodes: list[PageNode] = Field(default_factory=list)
    edges: list[PageEdge] = Field(default_factory=list)


class GeneratedArtifact(BaseModel):
    python_path: Path
    config_path: Path
    metadata_path: Path
    generated_at: datetime = Field(default_factory=utc_now)
    warnings: list[str] = Field(default_factory=list)


class ReplayResult(BaseModel):
    attempt: int
    command: CommandResult
    report_path: Path | None = None
    passed: bool = False


class RunEvent(BaseModel):
    event_id: int
    run_id: str
    type: EventType
    timestamp: datetime = Field(default_factory=utc_now)
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)


class RunTrace(BaseModel):
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
    plan: list[PlannedStep] = Field(default_factory=list)
    snapshots: list[ScreenSnapshot] = Field(default_factory=list)
    actions: list[ActionResult] = Field(default_factory=list)
    assertions: list[AssertionResult] = Field(default_factory=list)
    graph: PageGraph = Field(default_factory=PageGraph)
    events: list[RunEvent] = Field(default_factory=list)
    generated: GeneratedArtifact | None = None
    replays: list[ReplayResult] = Field(default_factory=list)
    error: str | None = None


class VisionElement(BaseModel):
    content: str
    type: str = "unknown"
    bbox: BoundingBox
    clickable: bool = False
    editable: bool = False
    score: float = Field(default=0.5, ge=0, le=1)
    reason: str = ""


class VisionObservation(BaseModel):
    page_title: str = ""
    summary: str = ""
    elements: list[VisionElement] = Field(default_factory=list)


class RunRequest(BaseModel):
    target_app_id: str = "zhihu-plus"
    task: str
    mode: RunMode = RunMode.REGRESSION
    device_id: str | None = None
    max_steps: int = Field(default=20, ge=1, le=100)
    auto_generate: bool = True
    auto_execute: bool = False
