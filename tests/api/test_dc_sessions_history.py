"""DC 历史会话契约测试：落盘、列表、恢复并继续对话。

用 httpx ``ASGITransport``（同 ``test_dc_event_stream.py``）：``send_message`` 通过
``asyncio.create_task`` 在后台执行整轮对话，同步 TestClient 的每请求 portal 会把
后台任务取消掉。
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
from harmony_test_agent.devices.base import DeviceError

SNAPSHOT_FILE = "dc_session.json"


def _fake_screenshot_jpeg(self: Any, output_dir: Path, label: str = "screen") -> tuple[Path, bytes, int, int]:
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
    """Mock 设备连接、健康检查、截图与 UI 层级，避免触碰真实 HDC。"""
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


async def wait_idle(
    client: AsyncClient, session_id: str, expected_turns: int = 1, timeout: float = 15.0
) -> dict[str, Any]:
    """等待后台轮次结束：必须等到第 expected_turns 轮出现且不再是 running。"""
    deadline = time.monotonic() + timeout
    view: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = await client.get(f"/api/dc/sessions/{session_id}")
        assert response.status_code == 200
        view = response.json()
        turns = view.get("turns") or []
        if view.get("status") == "idle" and len(turns) >= expected_turns and turns[-1]["status"] != "running":
            return view
        await asyncio.sleep(0.05)
    raise AssertionError(f"session {session_id} did not become idle: {view}")


async def send_and_wait(client: AsyncClient, session_id: str, text: str, expected_turns: int = 1) -> dict[str, Any]:
    response = await client.post(f"/api/dc/sessions/{session_id}/messages", json={"text": text})
    assert response.status_code == 202, response.text
    return await wait_idle(client, session_id, expected_turns)


class TestSessionPersistence:
    async def test_turn_writes_snapshot_and_listing_reports_active(
        self, app: Any, settings: Settings, patched_device: None
    ) -> None:
        async with await make_client(app) as client:
            create_resp = await client.post("/api/dc/sessions", json={"tier": 1})
            session_id = create_resp.json()["session_id"]
            await send_and_wait(client, session_id, "你好")

            snapshot_path = settings.resolved_runtime_dir / session_id / SNAPSHOT_FILE
            assert snapshot_path.is_file()

            listed = (await client.get("/api/dc/sessions")).json()
            entry = next(item for item in listed if item["session_id"] == session_id)
            assert entry["active"] is True
            assert entry["turn_count"] == 1
            assert entry["status"] == "idle"

    async def test_two_sessions_on_same_device_are_allowed(self, app: Any, patched_device: None) -> None:
        async with await make_client(app) as client:
            first = await client.post("/api/dc/sessions", json={})
            second = await client.post("/api/dc/sessions", json={})

            assert first.status_code == 201
            assert second.status_code == 201, "同一设备的第二个会话不应再被 409 拒绝"

            listed = (await client.get("/api/dc/sessions")).json()
            assert {item["session_id"] for item in listed} >= {
                first.json()["session_id"],
                second.json()["session_id"],
            }

    async def test_closed_session_stays_listed_as_history(self, app: Any, patched_device: None) -> None:
        async with await make_client(app) as client:
            session_id = (await client.post("/api/dc/sessions", json={})).json()["session_id"]
            await send_and_wait(client, session_id, "第一轮")

            assert (await client.delete(f"/api/dc/sessions/{session_id}")).status_code == 200

            listed = (await client.get("/api/dc/sessions")).json()
            entry = next(item for item in listed if item["session_id"] == session_id)
            assert entry["active"] is False
            assert entry["turn_count"] == 1
            assert entry["status"] == "closed"


class TestSessionResume:
    async def test_resume_after_restart_and_continue_conversation(
        self, settings: Settings, app: Any, patched_device: None
    ) -> None:
        async with await make_client(app) as client:
            session_id = (await client.post("/api/dc/sessions", json={})).json()["session_id"]
            view = await send_and_wait(client, session_id, "第一轮")
            assert view["restored"] is False

        # 模拟服务重启：同一 runtime_dir 上的全新 app/manager，内存里没有该会话
        restarted = create_app(settings)
        async with await make_client(restarted) as client:
            listed = (await client.get("/api/dc/sessions")).json()
            entry = next(item for item in listed if item["session_id"] == session_id)
            assert entry["active"] is False

            assert (await client.get(f"/api/dc/sessions/{session_id}")).status_code == 404

            resume_resp = await client.post(f"/api/dc/sessions/{session_id}/resume")
            assert resume_resp.status_code == 200, resume_resp.text
            resumed = resume_resp.json()
            assert resumed["restored"] is True
            assert [turn["user_message"] for turn in resumed["turns"]] == ["第一轮"]
            # Mock Provider 不累积 history → 上下文按 turns 重建
            assert resumed["restored_context"] == "text"

            # 恢复后可以继续对话，且新快照累积两轮
            continued = await send_and_wait(client, session_id, "第二轮", expected_turns=2)
            assert [turn["user_message"] for turn in continued["turns"]] == ["第一轮", "第二轮"]
            assert continued["restored"] is True

            listed = (await client.get("/api/dc/sessions")).json()
            entry = next(item for item in listed if item["session_id"] == session_id)
            assert entry["active"] is True
            assert entry["turn_count"] == 2

    async def test_resume_is_idempotent_for_active_session(self, app: Any, patched_device: None) -> None:
        async with await make_client(app) as client:
            session_id = (await client.post("/api/dc/sessions", json={})).json()["session_id"]

            first = await client.post(f"/api/dc/sessions/{session_id}/resume")
            second = await client.post(f"/api/dc/sessions/{session_id}/resume")

            assert first.status_code == 200
            assert second.status_code == 200
            assert first.json()["session_id"] == second.json()["session_id"]

    async def test_resume_unknown_session_returns_404(self, app: Any, patched_device: None) -> None:
        async with await make_client(app) as client:
            response = await client.post("/api/dc/sessions/dc-20990101T000000Z-deadbeef/resume")

            assert response.status_code == 404

    async def test_resume_surfaces_device_failure(self, settings: Settings, app: Any, patched_device: None) -> None:
        async with await make_client(app) as client:
            session_id = (await client.post("/api/dc/sessions", json={})).json()["session_id"]
            await send_and_wait(client, session_id, "第一轮")

        restarted = create_app(settings)
        async with await make_client(restarted) as client:
            with patch(
                "harmony_test_agent.dc.session.HarmonyDeviceAdapter.connect",
                side_effect=DeviceError("device offline"),
            ):
                response = await client.post(f"/api/dc/sessions/{session_id}/resume")

            assert response.status_code == 409
            assert "cannot resume session" in response.json()["detail"]
            # 恢复失败不应把会话从历史列表中抹掉
            listed = (await client.get("/api/dc/sessions")).json()
            assert any(item["session_id"] == session_id for item in listed)


class TestDeviceTurnExclusion:
    async def test_concurrent_turn_on_same_device_is_blocked(self, app: Any, patched_device: None) -> None:
        """两个会话同时驱动同一设备：后者收到明确的 ERROR 事件，而不是静默排队。"""

        async def slow_chat(self: Any, request: Any) -> Any:
            from harmony_test_agent.dc.models import DcChatResponse

            await asyncio.sleep(1.0)
            return DcChatResponse(output_text="完成", history=list(request.history or []), tool_call_count=0)

        async with await make_client(app) as client:
            first = (await client.post("/api/dc/sessions", json={})).json()["session_id"]
            second = (await client.post("/api/dc/sessions", json={})).json()["session_id"]

            with patch("harmony_test_agent.dc.provider.MockDcChatProvider.chat", slow_chat):
                await client.post(f"/api/dc/sessions/{first}/messages", json={"text": "慢轮次"})
                await asyncio.sleep(0.2)
                await client.post(f"/api/dc/sessions/{second}/messages", json={"text": "并发轮次"})
                await asyncio.sleep(0.4)

                blocked = (await client.get(f"/api/dc/sessions/{second}")).json()

            assert blocked["turns"][-1]["status"] == "blocked"
            assert "busy" in (blocked["turns"][-1]["error"] or "")
            assert blocked["status"] == "idle"
