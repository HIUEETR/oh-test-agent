"""Phase 4 API 契约：未确认副作用的 409/needs_attention、/resolve 处置与 /stop 语义。

计划第 6 节 Phase 4 要求：旧轮次副作用未知时新轮次必须被明确阻塞（409 或
needs_attention 事件），不能静默排队或抢占设备；``stop`` 必须先置取消标记再
取消任务，并把未确认的动作交给人工处置。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from harmony_test_agent.api.app import create_app
from harmony_test_agent.config import Settings
from harmony_test_agent.dc.models import (
    DcChatResponse,
    DcContinuationContext,
    DcEffectStatus,
    DcEventType,
    DcToolInvocation,
    DcToolName,
    DcToolStatus,
    DcToolTier,
    DcTurnStatus,
)


def _fake_screenshot_jpeg(
    self: Any,
    output_dir: Path,
    label: str = "screen",
    on_phase: Any = None,
) -> tuple[Path, bytes, int, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "dc_test.jpeg"
    path.write_bytes(b"fake-jpeg-bytes")
    return path, b"fake-jpeg-bytes", 1080, 2232


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def app(settings: Settings) -> Any:
    return create_app(settings)


@pytest.fixture
def patched_device() -> Iterator[None]:
    with (
        patch("harmony_test_agent.dc.session.HarmonyDeviceAdapter.connect", return_value=None),
        patch(
            "harmony_test_agent.dc.session.HarmonyDeviceAdapter.health_check",
            return_value={"connected": True, "id": "mock-device"},
        ),
        patch("harmony_test_agent.dc.session.HarmonyDeviceAdapter.collect_ui_hierarchy", return_value={}),
        patch("harmony_test_agent.dc.hdc.DcHdcExecutor.screenshot_jpeg", _fake_screenshot_jpeg),
    ):
        yield


async def make_client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def create_session(client: AsyncClient) -> str:
    response = await client.post("/api/dc/sessions", json={"tier": 1})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def unknown_effect_invocation() -> DcToolInvocation:
    return DcToolInvocation(
        invocation_id="inv-click",
        turn_id="turn-old",
        tool=DcToolName.CLICK,
        tier=DcToolTier.L1,
        args={"x": 540, "y": 1200},
        success=False,
        status=DcToolStatus.TIMED_OUT,
        effect_status=DcEffectStatus.UNKNOWN,
        error_code="tool_timeout",
    )


class BlockingProvider:
    """一直等待的 provider，用于验证 stop 的两阶段语义。"""

    name = "blocking"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def chat(self, request: Any) -> DcChatResponse:
        self.started.set()
        await asyncio.sleep(30)
        return DcChatResponse()


async def wait_status(session: Any, target: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if session.status == target:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"session {session.session_id} did not reach status {target}")


class TestAttentionGate:
    async def test_unconfirmed_effect_blocks_message_with_409(self, app: Any, patched_device: None) -> None:
        async with await make_client(app) as client:
            session_id = await create_session(client)
            session = app.state.dc_manager.get(session_id)
            invocation = unknown_effect_invocation()
            session.recorder.invocations.append(invocation)
            session.pending_attention = invocation

            response = await client.post(f"/api/dc/sessions/{session_id}/messages", json={"text": "继续"})

            assert response.status_code == 409
            assert "unconfirmed" in response.json()["detail"]
            events = [event for event in session.bus.recent() if event.type == DcEventType.NEEDS_ATTENTION]
            assert events, "被阻塞时必须发出 needs_attention 事件"
            assert events[-1].payload["options"] == ["reobserve", "confirm_effect", "retry", "terminate"]

    async def test_resolve_reobserve_unblocks_next_message(self, app: Any, patched_device: None) -> None:
        async with await make_client(app) as client:
            session_id = await create_session(client)
            session = app.state.dc_manager.get(session_id)
            session.recorder.invocations.append(unknown_effect_invocation())
            session.pending_attention = session.recorder.invocations[-1]
            session.continuation = DcContinuationContext(
                previous_turn_id="turn-old",
                previous_status=DcTurnStatus.NEEDS_ATTENTION,
                original_user_goal="点击保存",
                reconcile_required=True,
                effect_status=DcEffectStatus.UNKNOWN,
            )

            resolved = await client.post(f"/api/dc/sessions/{session_id}/resolve", json={"action": "reobserve"})
            assert resolved.status_code == 200
            assert resolved.json()["continuation"]["reconcile_required"] is False

            sent = await client.post(f"/api/dc/sessions/{session_id}/messages", json={"text": "继续"})
            assert sent.status_code == 202, sent.text

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                view = (await client.get(f"/api/dc/sessions/{session_id}")).json()
                if view["status"] == "idle" and view["turns"] and view["turns"][-1]["status"] != "running":
                    break
                await asyncio.sleep(0.05)
            assert view["turns"][-1]["status"] == DcTurnStatus.COMPLETED.value

    async def test_resolve_rejects_unknown_action(self, app: Any, patched_device: None) -> None:
        async with await make_client(app) as client:
            session_id = await create_session(client)
            response = await client.post(f"/api/dc/sessions/{session_id}/resolve", json={"action": "explode"})
            assert response.status_code == 422


class TestStopFlow:
    async def test_stop_sets_cancel_marker_and_records_interrupted_turn(self, app: Any, patched_device: None) -> None:
        async with await make_client(app) as client:
            session_id = await create_session(client)
            session = app.state.dc_manager.get(session_id)
            provider = BlockingProvider()
            session.provider = provider  # type: ignore[assignment]

            sent = await client.post(f"/api/dc/sessions/{session_id}/messages", json={"text": "打开日历"})
            assert sent.status_code == 202
            await asyncio.wait_for(provider.started.wait(), timeout=10)

            stopped = await client.post(f"/api/dc/sessions/{session_id}/stop")
            assert stopped.status_code == 200
            body = stopped.json()
            assert body["status"] == "cancelling"

            await wait_status(session, "idle")
            view = (await client.get(f"/api/dc/sessions/{session_id}")).json()
            assert view["turns"][-1]["status"] == DcTurnStatus.CANCELLED.value

            event_types = [event.type for event in session.bus.recent()]
            assert DcEventType.TURN_CANCEL_REQUESTED in event_types
            assert DcEventType.TURN_INTERRUPTED in event_types
            assert DcEventType.TURN_FINISHED in event_types

    async def test_stop_without_running_turn_reports_idle(self, app: Any, patched_device: None) -> None:
        async with await make_client(app) as client:
            session_id = await create_session(client)
            stopped = await client.post(f"/api/dc/sessions/{session_id}/stop")
            assert stopped.status_code == 200
            assert stopped.json()["status"] == "idle"
            assert stopped.json()["attention_required"] is False
