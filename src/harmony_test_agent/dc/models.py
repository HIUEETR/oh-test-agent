"""直流模式（DC Mode）专属领域模型。

本模块不修改 ``harmony_test_agent.models``，仅导入 ``CommandResult``、
``ScreenSnapshot``、``UIElement`` 作为类型引用，确保 DC Mode 与 Live Mode
在模型层完全隔离。
"""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Any

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


class DcToolInvocation(BaseModel):
    """一次工具调用的完整录制记录。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    invocation_id: str
    turn_id: str
    tool: DcToolName
    tier: DcToolTier
    args: dict[str, Any] = Field(default_factory=dict)
    success: bool = True
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime = Field(default_factory=utc_now)
    duration_ms: int = 0
    command: CommandResult | None = None
    before_snapshot_id: str | None = None
    after_snapshot_id: str | None = None
    resolved_element: UIElement | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# 对话轮次
# ---------------------------------------------------------------------------


class DcTurnStatus(StrEnum):
    """一轮对话的执行状态。"""

    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    NEEDS_USER = "needs_user"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_FINISHED = "tool_call_finished"
    SCREENSHOT_CAPTURED = "screenshot_captured"
    UI_TREE_CAPTURED = "ui_tree_captured"
    ASSISTANT_MESSAGE = "assistant_message"
    THINKING = "thinking"  # 模型原生推理（reasoning_content / ThinkingPart）
    AGENT_TEXT = "agent_text"  # 模型可见叙述文本（每步 TextPart，非最终总结）
    SCRIPT_GENERATED = "script_generated"
    TIER_CHANGED = "tier_changed"
    ERROR = "error"


DC_EVENT_TYPES: tuple[str, ...] = tuple(event.value for event in DcEventType)


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


class DcSessionNotFound(DcError):
    """会话不存在。"""


class DcDeviceBusy(DcError):
    """设备已被其他会话占用。"""
