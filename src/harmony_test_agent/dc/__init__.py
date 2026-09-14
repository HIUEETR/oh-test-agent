"""直流模式（DC Mode）公共导出。

DC Mode 与 Live Mode 完全隔离：独立的会话生命周期、事件总线、安全策略、
脚本生成器和前端 store。仅通过组合复用底层能力（HarmonyDeviceAdapter、
ArtifactStore、HypiumGenerator 静态方法、OpenAICompatibleProvider._model）。
"""

from .generator import DcHypiumGenerator
from .hdc import DcHdcExecutor
from .models import (
    DcChatRequest,
    DcChatResponse,
    DcEvent,
    DcEventType,
    DcScriptArtifact,
    DcSessionView,
    DcToolInvocation,
    DcToolName,
    DcToolTier,
    DcTurnRecord,
    DcTurnStatus,
)
from .provider import DcChatProvider, MockDcChatProvider, create_dc_provider
from .router import create_dc_router
from .safety import DcShellPolicy
from .session import DcEventBus, DcSession, DcSessionManager
from .tools import DcActionRecorder, DcSnapshotHolder, DcToolContext, build_tools

__all__ = [
    "DcActionRecorder",
    "DcChatProvider",
    "DcChatRequest",
    "DcChatResponse",
    "DcEvent",
    "DcEventBus",
    "DcEventType",
    "DcHdcExecutor",
    "DcHypiumGenerator",
    "DcScriptArtifact",
    "DcSession",
    "DcSessionManager",
    "DcSessionView",
    "DcShellPolicy",
    "DcSnapshotHolder",
    "DcToolContext",
    "DcToolInvocation",
    "DcToolName",
    "DcToolTier",
    "DcTurnRecord",
    "DcTurnStatus",
    "MockDcChatProvider",
    "build_tools",
    "create_dc_provider",
    "create_dc_router",
]
