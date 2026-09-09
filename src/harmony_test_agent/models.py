"""定义测试运行、页面感知、工具调用及产物交换所使用的领域模型。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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
    """列出可持久化并推送给客户端的运行事件类型。"""

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


class StableLocator(BaseModel):
    """描述目标应用配置中可跨运行复用的稳定定位信息。"""

    model_config = ConfigDict(extra="allow")
    name: str
    key: str = ""
    id: str = ""
    text: str = ""
    type: str = ""
    locator_priority: int = 1


class TargetAppProfile(BaseModel):
    """保存目标应用启动信息、安全约束和稳定定位清单。"""

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
    """表示运行过程中发现的一个页面节点。"""

    node_id: str
    signature: str
    page_path: str
    title: str
    snapshot_id: str
    image_path: Path
    discovered_order: int
    element_count: int


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
    """记录生成的 Hypium 脚本、配置、元数据及警告。"""

    python_path: Path
    config_path: Path
    metadata_path: Path
    generated_at: datetime = Field(default_factory=utc_now)
    warnings: list[str] = Field(default_factory=list)


class ReplayResult(BaseModel):
    """记录一次生成脚本的回放命令和通过状态。"""

    attempt: int
    command: CommandResult
    report_path: Path | None = None
    passed: bool = False


class RunEvent(BaseModel):
    """表示带有运行内序号和结构化负载的事件。"""

    event_id: int
    run_id: str
    type: EventType
    timestamp: datetime = Field(default_factory=utc_now)
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)


class RunTrace(BaseModel):
    """聚合一次测试运行的计划、快照、动作、断言、产物和最终状态。"""

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
    """定义创建测试运行时可由调用方指定的参数。"""

    target_app_id: str = "zhihu-plus"
    task: str
    mode: RunMode = RunMode.REGRESSION
    device_id: str | None = None
    max_steps: int = Field(default=20, ge=1, le=100)
    auto_generate: bool = True
    auto_execute: bool = False
