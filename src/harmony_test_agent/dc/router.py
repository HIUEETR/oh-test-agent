"""DC 模式 FastAPI 路由：/api/dc/* 端点。

Push-based SSE（asyncio.Queue 消费，非 SQLite 轮询），Last-Event-ID 回放，
产物下载带 Cache-Control immutable。与 Live Mode 路由完全隔离。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..config import Settings
from .models import (
    DcDeviceBusy,
    DcEventType,
    DcProviderUnsupported,
    DcSessionNotFound,
    DcSessionView,
    DcToolTier,
)
from .session import DcSession, DcSessionManager

# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    device_id: str | None = None
    tier: int = Field(default=2, ge=1, le=5)


class CreateSessionResponse(BaseModel):
    session_id: str
    device_id: str
    tier: int
    status: str


class SendMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=10000)


class SendMessageResponse(BaseModel):
    turn_id: str
    status: str


class SetTierRequest(BaseModel):
    tier: int = Field(ge=1, le=5)


class GenerateScriptRequest(BaseModel):
    bundle_name: str = "com.example.app"
    main_ability: str = "EntryAbility"


# ---------------------------------------------------------------------------
# SSE 格式化
# ---------------------------------------------------------------------------


def _format_sse(event: Any) -> str:
    """把 DcEvent 格式化为 SSE wire format。"""
    data = event.model_dump_json() if hasattr(event, "model_dump_json") else json.dumps(event, default=str)
    return f"id: {event.event_id}\nevent: {event.type.value}\ndata: {data}\n\n"


# ---------------------------------------------------------------------------
# 路由工厂
# ---------------------------------------------------------------------------


def create_dc_router(settings: Settings, manager: DcSessionManager) -> APIRouter:
    """创建并返回 DC 模式 APIRouter（prefix=/api/dc）。"""
    router = APIRouter(prefix="/api/dc", tags=["dc"])

    # ------------------------------------------------------------------
    # 会话 CRUD
    # ------------------------------------------------------------------

    @router.post("/sessions", response_model=CreateSessionResponse, status_code=201)
    async def create_session(body: CreateSessionRequest | None = None):
        """创建新的 DC 会话。"""
        body = body or CreateSessionRequest()
        try:
            session = manager.create(device_id=body.device_id, tier=DcToolTier(body.tier))
        except DcDeviceBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # 后台启动设备连接
        asyncio.create_task(_start_session(session), name=f"dc-start-{session.session_id}")
        return CreateSessionResponse(
            session_id=session.session_id,
            device_id=session.device_id,
            tier=session.tier.value,
            status=session.status,
        )

    @router.get("/sessions")
    async def list_sessions():
        """列出活跃会话摘要。"""
        return manager.list_sessions()

    @router.get("/sessions/{session_id}", response_model=DcSessionView)
    async def get_session(session_id: str):
        """获取会话全量投影。"""
        try:
            session = manager.get(session_id)
        except DcSessionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return session.to_view()

    @router.delete("/sessions/{session_id}")
    async def close_session(session_id: str):
        """关闭并移除会话。"""
        closed = await manager.close(session_id)
        if not closed:
            raise HTTPException(status_code=404, detail=f"session {session_id} not found")
        return {"session_id": session_id, "status": "closed"}

    # ------------------------------------------------------------------
    # 对话
    # ------------------------------------------------------------------

    @router.post("/sessions/{session_id}/messages", response_model=SendMessageResponse, status_code=202)
    async def send_message(session_id: str, body: SendMessageRequest):
        """发送用户消息；后台异步执行，立即返回 turn_id。"""
        try:
            session = manager.get(session_id)
        except DcSessionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if session.status == "closed":
            raise HTTPException(status_code=409, detail="session is closed")
        if session.status in ("thinking", "acting"):
            raise HTTPException(status_code=409, detail="session is busy processing a previous message")

        turn_id = f"turn-pending-{session_id[-8:]}"

        async def _run_turn():
            try:
                await session.handle_user_message(body.text)
            except DcProviderUnsupported as exc:
                session._emit(DcEventType.ERROR, str(exc), {"turn_id": turn_id})
            except Exception as exc:
                session._emit(DcEventType.ERROR, f"{type(exc).__name__}: {exc}", {"turn_id": turn_id})

        task = asyncio.create_task(_run_turn(), name=f"dc-turn-{session_id}")
        session._current_task = task

        # 返回实际的 turn_id（从 session 的最新 turn 获取）
        actual_turn_id = session.turns[-1].turn_id if session.turns else turn_id
        return SendMessageResponse(turn_id=actual_turn_id, status="started")

    @router.post("/sessions/{session_id}/stop")
    async def stop_turn(session_id: str):
        """取消当前正在执行的 turn。"""
        try:
            session = manager.get(session_id)
        except DcSessionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if session._current_task and not session._current_task.done():
            session._current_task.cancel()
            return {"session_id": session_id, "status": "cancelling"}
        return {"session_id": session_id, "status": "idle"}

    @router.patch("/sessions/{session_id}/tier")
    async def set_tier(session_id: str, body: SetTierRequest):
        """变更工具层级（下轮生效）。"""
        try:
            session = manager.get(session_id)
        except DcSessionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        session.change_tier(DcToolTier(body.tier))
        return {"session_id": session_id, "tier": session.tier.value}

    # ------------------------------------------------------------------
    # SSE 事件流（push-based）
    # ------------------------------------------------------------------

    @router.get("/sessions/{session_id}/events")
    async def stream_events(
        request: Request,
        session_id: str,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ):
        """SSE 事件流：push-based（asyncio.Queue 消费，无 sleep 轮询）。

        支持 Last-Event-ID 回放：重连时先补发 recent buffer 中的历史事件。
        """
        try:
            session = manager.get(session_id)
        except DcSessionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        queue = session.bus.subscribe()

        async def event_generator():
            try:
                # Last-Event-ID 回放
                if last_event_id:
                    try:
                        after_id = int(last_event_id)
                        for event in session.bus.recent(after_id=after_id):
                            yield _format_sse(event)
                    except ValueError, TypeError:
                        pass

                # 实时消费
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    except TimeoutError:
                        # 心跳：每 30s 发送注释帧保持连接
                        yield ": heartbeat\n\n"
                        continue
                    yield _format_sse(event)
                    if event.type == DcEventType.SESSION_CLOSED:
                        break
            finally:
                session.bus.unsubscribe(queue)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ------------------------------------------------------------------
    # 脚本生成
    # ------------------------------------------------------------------

    @router.post("/sessions/{session_id}/script")
    async def generate_script(
        session_id: str,
        body: GenerateScriptRequest | None = None,
    ):
        """触发 Hypium 脚本生成。"""
        body = body or GenerateScriptRequest()
        try:
            session = manager.get(session_id)
        except DcSessionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not session.recorder.invocations:
            raise HTTPException(status_code=409, detail="no operations recorded yet")
        script = await asyncio.to_thread(session.generate_script, body.bundle_name, body.main_ability)
        return script.model_dump(mode="json")

    @router.get("/sessions/{session_id}/script")
    async def get_script(session_id: str):
        """获取已生成的脚本。"""
        try:
            session = manager.get(session_id)
        except DcSessionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if session.script is None:
            raise HTTPException(status_code=404, detail="script not generated yet")
        return session.script.model_dump(mode="json")

    # ------------------------------------------------------------------
    # 产物下载
    # ------------------------------------------------------------------

    @router.get("/sessions/{session_id}/artifacts/{artifact_path:path}")
    async def download_artifact(session_id: str, artifact_path: str):
        """下载会话产物（截图/日志/脚本）。

        镜像 ``api/app.py:633-639`` 的 path-traversal 安全检查。
        截图响应带 Cache-Control immutable（SHA256 作为 URL 一部分天然去重）。
        """
        try:
            session = manager.get(session_id)
        except DcSessionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        session_dir = session.dir.resolve()
        requested = (session_dir / artifact_path).resolve()
        if not requested.is_relative_to(session_dir) or not requested.is_file():
            raise HTTPException(status_code=404, detail="artifact not found")

        headers: dict[str, str] = {}
        if requested.suffix.lower() in (".jpeg", ".jpg", ".png"):
            headers["Cache-Control"] = "public, max-age=3600, immutable"

        return FileResponse(requested, headers=headers)

    return router


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


async def _start_session(session: DcSession) -> None:
    """后台启动会话：连接设备。"""
    try:
        await session.start()
    except Exception as exc:
        session._emit(DcEventType.ERROR, f"failed to start session: {exc}", {})
