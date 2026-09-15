"""直流模式（DC Mode）专属领域模型。

本模块不修改 ``harmony_test_agent.models``，仅导入 ``CommandResult``、
``ScreenSnapshot``、``UIElement`` 作为类型引用，确保 DC Mode 与 Live Mode
在模型层完全隔离。
"""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..models import CommandResult, UIElement, utc_now

# ---------------------------------------------------------------------------
# 工具层级与名称
# ---------------------------------------------------------------------------


class DcToolTier(IntEnum):
    """HDC 工具暴露层级；高层级包含低层级的全部工具。"""

    L1 = 1  # UI 交互
    L2 = 2  # 观测诊断
    L3 = 3  # 应用管理
    L4 = 4  # 文件操作
    L5 = 5  # 受控 Shell


class DcToolName(StrEnum):
    """DC 模式可用的 23 个工具名称。"""

    # L1 — UI 交互 (8)
    CLICK = "click"
    SWIPE = "swipe"
    INPUT_TEXT = "input_text"
    KEY_EVENT = "key_event"
    BACK = "back"
    WAIT = "wait"
    SCREENSHOT = "screenshot"
    INSPECT_SCREEN = "inspect_screen"
    # L2 — 观测诊断 (6)
    DUMP_UI_HIERARCHY = "dump_ui_hierarchy"
    COLLECT_LOGS = "collect_logs"
    FOREGROUND_APP = "foreground_app"
    LIST_APPS = "list_apps"
    INSPECT_APP = "inspect_app"
    MEMORY_DUMP = "memory_dump"
    # L3 — 应用管理 (5)
    START_APP = "start_app"
    FORCE_STOP_APP = "force_stop_app"
    INSTALL_APP = "install_app"
    UNINSTALL_APP = "uninstall_app"
    CLEAR_APP_DATA = "clear_app_data"
    # L4 — 文件操作 (3)
    FILE_SEND = "file_send"
    FILE_RECV = "file_recv"
    FILE_LIST = "file_list"
    # L5 — 受控 Shell (1)
    EXECUTE_SHELL = "execute_shell"


# 单一数据源：tier → 该层级新增的工具名元组
TIER_TOOLS: dict[DcToolTier, tuple[DcToolName, ...]] = {
    DcToolTier.L1: (
        DcToolName.CLICK,
        DcToolName.SWIPE,
        DcToolName.INPUT_TEXT,
        DcToolName.KEY_EVENT,
        DcToolName.BACK,
        DcToolName.WAIT,
        DcToolName.SCREENSHOT,
        DcToolName.INSPECT_SCREEN,
    ),
    DcToolTier.L2: (
        DcToolName.DUMP_UI_HIERARCHY,
        DcToolName.COLLECT_LOGS,
        DcToolName.FOREGROUND_APP,
        DcToolName.LIST_APPS,
        DcToolName.INSPECT_APP,
        DcToolName.MEMORY_DUMP,
    ),
    DcToolTier.L3: (
        DcToolName.START_APP,
        DcToolName.FORCE_STOP_APP,
        DcToolName.INSTALL_APP,
        DcToolName.UNINSTALL_APP,
        DcToolName.CLEAR_APP_DATA,
    ),
    DcToolTier.L4: (
        DcToolName.FILE_SEND,
        DcToolName.FILE_RECV,
        DcToolName.FILE_LIST,
    ),
    DcToolTier.L5: (DcToolName.EXECUTE_SHELL,),
}

# 工具名 → 所属层级的反向映射
TOOL_TIER: dict[DcToolName, DcToolTier] = {tool: tier for tier, tools in TIER_TOOLS.items() for tool in tools}


def tools_up_to(tier: DcToolTier) -> tuple[DcToolName, ...]:
    """返回 level ≤ tier 的全部工具名。"""
    result: list[DcToolName] = []
    for level in DcToolTier:
        if level <= tier:
            result.extend(TIER_TOOLS[level])
    return tuple(result)


# ---------------------------------------------------------------------------
# 工具调用录制
# ---------------------------------------------------------------------------


class DcToolStatus(StrEnum):
    """工具调用状态机（计划第 5.1 节）。

    ``RUNNING`` 记录在工具执行前就已进入账本，前端因此可以立即显示 running 行；
    ``success`` 保留为兼容字段，但新代码不能再用 ``success=True`` 表示「尚未结束」。
    """

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class DcEffectStatus(StrEnum):
    """设备副作用确认状态：未知副作用不允许自动重放。"""

    NONE = "none"
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"


class DcToolInvocation(BaseModel):
    """一次工具调用的完整录制记录。

    ``status`` 是权威状态字段；``success`` 仅为旧快照/旧前端的兼容字段。
    运行中的记录 ``ended_at`` 为 ``None``，``duration_ms`` 由前端按 ``started_at`` 推导。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    invocation_id: str
    turn_id: str
    tool: DcToolName
    tier: DcToolTier
    args: dict[str, Any] = Field(default_factory=dict)
    success: bool = True
    status: DcToolStatus = DcToolStatus.SUCCEEDED
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    duration_ms: int = 0
    last_progress_at: datetime | None = None
    deadline_at: datetime | None = None
    effect_status: DcEffectStatus = DcEffectStatus.NONE
    command_id: str | None = None
    phase: str = ""
    result_summary: str = ""
    cancellable: bool = True
    error_code: str | None = None
    command: CommandResult | None = None
    before_snapshot_id: str | None = None
    after_snapshot_id: str | None = None
    resolved_element: UIElement | None = None
    error: str | None = None

    @property
    def is_running(self) -> bool:
        """记录是否仍在执行中。"""
        return self.status == DcToolStatus.RUNNING


# ---------------------------------------------------------------------------
# 对话轮次
# ---------------------------------------------------------------------------


class DcTurnStatus(StrEnum):
    """一轮对话的执行状态。

    - ``CANCELLED``：用户/系统显式请求终止（``stop_turn``）
    - ``INTERRUPTED``：执行环境中断（进程重启、SSE 断开、会话关闭）
    - ``NEEDS_ATTENTION``：设备副作用未知，必须人工确认后才能继续
    """

    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    NEEDS_USER = "needs_user"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    NEEDS_ATTENTION = "needs_attention"


class DcStepKind(StrEnum):
    """一轮执行中模型侧步骤的类型。"""

    THINKING = "thinking"  # 原生推理（reasoning_content / ThinkingPart）
    AGENT_TEXT = "agent_text"  # 可见叙述文本（工具调用前后的说明）


class DcStepRecord(BaseModel):
    """模型侧的一步（思考或叙述）；用于刷新历史时还原步骤块。"""

    step: int = 0
    kind: DcStepKind = DcStepKind.AGENT_TEXT
    text: str = ""


class DcTurnRecord(BaseModel):
    """一轮用户消息 → Agent 自主执行的完整记录。"""

    turn_id: str
    user_message: str
    status: DcTurnStatus = DcTurnStatus.RUNNING
    agent_summary: str = ""
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    invocation_ids: list[str] = Field(default_factory=list)
    steps: list[DcStepRecord] = Field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------


class DcEventType(StrEnum):
    """DC 模式 SSE 事件类型；与 Live Mode 的 EventType 完全独立。"""

    SESSION_CREATED = "session_created"
    SESSION_CLOSED = "session_closed"
    TURN_STARTED = "turn_started"
    TURN_FINISHED = "turn_finished"
    TURN_CANCEL_REQUESTED = "turn_cancel_requested"
    TURN_INTERRUPTED = "turn_interrupted"
    CONTEXT_CAPTURE_STARTED = "context_capture_started"
    CONTEXT_CAPTURE_FINISHED = "context_capture_finished"
    CONTEXT_CHECKPOINT = "context_checkpoint"
    MODEL_CALL_STARTED = "model_call_started"
    MODEL_CALL_PROGRESS = "model_call_progress"
    MODEL_CALL_FINISHED = "model_call_finished"
    MODEL_CALL_FAILED = "model_call_failed"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_PROGRESS = "tool_call_progress"
    TOOL_CALL_FINISHED = "tool_call_finished"
    SCREENSHOT_CAPTURED = "screenshot_captured"
    UI_TREE_CAPTURED = "ui_tree_captured"
    ASSISTANT_MESSAGE = "assistant_message"
    THINKING = "thinking"  # 模型原生推理（reasoning_content / ThinkingPart）
    AGENT_TEXT = "agent_text"  # 模型可见叙述文本（每步 TextPart，非最终总结）
    SCRIPT_GENERATED = "script_generated"
    TIER_CHANGED = "tier_changed"
    NEEDS_ATTENTION = "needs_attention"
    ERROR = "error"


DC_EVENT_TYPES: tuple[str, ...] = tuple(event.value for event in DcEventType)

# 有设备副作用的工具：超时/取消后副作用未知，禁止自动重放
SIDE_EFFECT_TOOLS: frozenset[DcToolName] = frozenset(
    {
        DcToolName.CLICK,
        DcToolName.SWIPE,
        DcToolName.INPUT_TEXT,
        DcToolName.KEY_EVENT,
        DcToolName.BACK,
        DcToolName.START_APP,
        DcToolName.FORCE_STOP_APP,
        DcToolName.INSTALL_APP,
        DcToolName.UNINSTALL_APP,
        DcToolName.CLEAR_APP_DATA,
        DcToolName.FILE_SEND,
        DcToolName.EXECUTE_SHELL,
    }
)


class DcEvent(BaseModel):
    """DC 模式事件；结构对齐 RunEvent 以复用 SSE 编码约定。"""

    event_id: int
    session_id: str
    type: DcEventType
    timestamp: datetime = Field(default_factory=utc_now)
    message: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 脚本产物
# ---------------------------------------------------------------------------


class DcScriptArtifact(BaseModel):
    """DC 模式生成的 Hypium 脚本产物。"""

    python_path: str
    python_text: str = ""
    warnings: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utc_now)
    included_operations: int = 0
    omitted_operations: list[dict[str, str]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 公开连续性摘要（与 pydantic-ai 原始消息分离）
# ---------------------------------------------------------------------------


class DcContinuationContext(BaseModel):
    """跨轮次的公开连续性摘要（计划第 6 节 Phase 3）。

    只包含用户可见事实：原始目标、公开叙述、工具结果与最后已确认界面。
    隐藏 ``ThinkingPart`` 原文绝不进入本摘要，也不进入下一轮模型上下文。
    """

    previous_turn_id: str = ""
    previous_status: DcTurnStatus = DcTurnStatus.RUNNING
    original_user_goal: str = ""
    public_agent_summary: str = ""
    public_agent_steps: list[str] = Field(default_factory=list)
    completed_operations: list[str] = Field(default_factory=list)
    active_or_unknown_operation: str | None = None
    last_snapshot_path: str | None = None
    last_page_path: str | None = None
    last_foreground_app: str | None = None
    last_progress_at: datetime | None = None
    effect_status: DcEffectStatus = DcEffectStatus.NONE
    reconcile_required: bool = False
    context_version: int = 0
    updated_at: datetime = Field(default_factory=utc_now)

    def to_prompt(self) -> str:
        """渲染为注入下一轮 prompt 的公开文本。"""
        lines = [
            "上一轮公开连续性摘要（来自服务端会话事实，不是推测）：",
            f"- 上一轮 ID：{self.previous_turn_id or '无'}",
            f"- 上一轮状态：{self.previous_status.value}",
            f"- 原始用户目标：{self.original_user_goal or '无'}",
        ]
        if self.public_agent_summary:
            lines.append(f"- 上一轮公开总结：{self.public_agent_summary}")
        if self.public_agent_steps:
            lines.append("- 已完成的公开步骤：")
            lines.extend(f"    {index}. {step}" for index, step in enumerate(self.public_agent_steps[-12:], start=1))
        if self.completed_operations:
            lines.append("- 已完成的设备操作：")
            lines.extend(f"    - {item}" for item in self.completed_operations[-20:])
        if self.active_or_unknown_operation:
            lines.append(f"- 未完成/未确认的操作：{self.active_or_unknown_operation}")
        lines.append(f"- 最后已确认界面：{self.last_page_path or '未知'}")
        lines.append(f"- 最后前台应用：{self.last_foreground_app or '未知'}")
        lines.append(f"- 副作用确认状态：{self.effect_status.value}")
        if self.reconcile_required:
            lines.append(
                "- 约束：上一次动作的结果未确认。下一步必须先重新采集截图/UI 层级/前台应用，"
                "不得重放旧坐标、旧 bbox 或旧决策；确认设备状态后再决定继续。"
            )
        else:
            lines.append("- 约束：继续前如需依赖具体坐标，仍应基于最新观测，不要凭记忆复用旧坐标。")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 会话视图
# ---------------------------------------------------------------------------


class DcSessionView(BaseModel):
    """GET /api/dc/sessions/{id} 的公开投影。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    session_id: str
    device_id: str
    tier: DcToolTier
    status: str = "idle"
    created_at: datetime = Field(default_factory=utc_now)
    turns: list[DcTurnRecord] = Field(default_factory=list)
    invocations: list[DcToolInvocation] = Field(default_factory=list)
    latest_snapshot_path: str | None = None
    script: DcScriptArtifact | None = None
    # 历史会话恢复标记：restored_context 表示模型上下文还原的完整度
    restored: bool = False
    restored_context: Literal["none", "full", "text"] = "none"
    # 实时可观测字段：当前轮次与公开连续性摘要
    active_turn_id: str | None = None
    continuation: DcContinuationContext | None = None


class DcSessionSummary(BaseModel):
    """GET /api/dc/sessions 列表项；active=False 表示可从磁盘恢复的历史会话。"""

    session_id: str
    device_id: str
    tier: int
    status: str = "idle"
    created_at: datetime = Field(default_factory=utc_now)
    last_active_at: datetime = Field(default_factory=utc_now)
    turn_count: int = 0
    invocation_count: int = 0
    active: bool = False
    script_available: bool = False
    restorable: bool = True


class DcSessionSnapshot(BaseModel):
    """落盘到 ``<session_dir>/dc_session.json`` 的会话状态。

    用于历史会话列表与恢复：turns/invocations/script 直接复用会话投影模型，
    ``history`` 为已剥离图片与 thinking 的 pydantic-ai 消息 JSON。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    schema_version: int = 2
    session_id: str
    device_id: str = ""
    tier: DcToolTier = DcToolTier.L2
    status: str = "idle"
    created_at: datetime = Field(default_factory=utc_now)
    last_active_at: datetime = Field(default_factory=utc_now)
    turns: list[DcTurnRecord] = Field(default_factory=list)
    invocations: list[DcToolInvocation] = Field(default_factory=list)
    script: DcScriptArtifact | None = None
    latest_snapshot_path: str | None = None
    history: list[Any] = Field(default_factory=list)
    history_kind: Literal["none", "model_messages"] = "none"
    has_snapshot: bool = True
    # 公开连续性摘要：取消/中断后下一轮仍能继承目标与已完成状态
    continuation: DcContinuationContext | None = None

    def summary(self, *, active: bool) -> DcSessionSummary:
        """投影为列表项。"""
        return DcSessionSummary(
            session_id=self.session_id,
            device_id=self.device_id,
            tier=self.tier.value,
            status=self.status,
            created_at=self.created_at,
            last_active_at=self.last_active_at,
            turn_count=len(self.turns),
            invocation_count=len(self.invocations),
            active=active,
            script_available=self.script is not None,
            restorable=True,
        )


# ---------------------------------------------------------------------------
# Provider 通信契约
# ---------------------------------------------------------------------------


class DcChatRequest(BaseModel):
    """Provider.chat 的输入契约。

    ``tools`` 和 ``tool_context`` 故意使用 ``Any`` 以避免在 ABC 边界泄漏
    pydantic-ai 类型；OpenAI 实现内部做强转。

    ``emit`` 为 Provider 在执行过程中实时回传事件的回调，签名为
    ``(event_type_value: str, message: str, payload: dict) -> None``；为 ``None``
    时 Provider 跳过事件发射（单测直接调用 ``chat()`` 时使用）。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    user_prompt: str
    screenshot_jpeg: bytes = b""
    ui_tree_digest: str = ""
    history: list[Any] = Field(default_factory=list)
    tools: list[Any] = Field(default_factory=list)
    tool_context: Any = None
    emit: Any = None
    # 进度与超时契约（Phase 2）
    turn_id: str = ""
    model_timeout: float = 90.0
    progress_interval: float = 1.0
    # 轮次总预算的绝对截止时间；为 None 表示不做轮次级约束
    turn_deadline: datetime | None = None
    # 公开连续性摘要文本（由 DcSession 生成后注入 prompt）
    continuation_prompt: str = ""


class DcChatResponse(BaseModel):
    """Provider.chat 的输出契约。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    output_text: str = ""
    history: list[Any] = Field(default_factory=list)
    tool_call_count: int = 0


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class DcError(RuntimeError):
    """DC 模式基础异常。"""


class DcSafetyError(DcError):
    """安全策略拦截。"""


class DcProviderUnsupported(DcError):
    """当前 Provider 不支持 DC 对话。"""


class DcTurnBudgetExceeded(DcError):
    """单轮工具调用超出预算。"""


class DcTurnTimeout(DcError):
    """单轮总预算（``DC_TURN_TIMEOUT``）耗尽。"""


class DcModelTimeout(DcError):
    """单次模型调用超出 ``AGENT_MODEL_TIMEOUT``。"""


class DcDeviceReconciling(DcError):
    """设备正处于上一轮未确认动作的对账期，新轮次必须等待或人工处理。"""


class DcSessionNotFound(DcError):
    """会话不存在。"""


class DcDeviceBusy(DcError):
    """设备已被其他会话占用。"""
