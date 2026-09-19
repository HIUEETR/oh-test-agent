"""DC 模式 SSE 事件流契约测试（async + ASGITransport）。

为什么不用同步 TestClient：``send_message`` 通过 ``asyncio.create_task`` 在后台
执行整轮对话，而 starlette 的同步 TestClient 为每个请求创建并销毁独立 portal，
后台任务会被立刻取消（turn 状态变成 cancelled）。改用 httpx ``ASGITransport``
让应用运行在测试自己的事件循环里，后台轮次才能跑完。

为什么需要并发 DELETE：httpx 0.28 的 ``ASGITransport`` 会缓冲整个响应
（``await self.app(...)`` 之后才返回 Response），无限 SSE 流因此不会自然结束。
测试用后台任务在读取期间关闭会话，触发 ``SESSION_CLOSED`` 让生成器收尾；
生产环境由浏览器长连接持续消费，不受影响。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from harmony_test_agent.api.app import create_app
from harmony_test_agent.config import Settings
from harmony_test_agent.dc.models import DcEventType
from harmony_test_agent.dc.session import DcEventBus


def _fake_screenshot_jpeg(self: Any, output_dir: Path, label: str = "screen") -> tuple[Path, bytes, int, int]:
    """替换真实 HDC 截图：在会话 screens/ 下落盘一张固定 JPEG。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "dc_test.jpeg"
    path.write_bytes(b"fake-jpeg-bytes")
    return path, b"fake-jpeg-bytes", 1080, 2232


@pytest.fixture
def app(tmp_path: Path) -> Any:
    settings = Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
    )
    return create_app(settings)


@pytest.fixture
def patched_device() -> Iterator[None]:
    """Mock 设备连接、健康检查与截图采集，避免触碰真实 HDC。"""
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


@pytest.fixture
async def client(app: Any) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client


async def _wait_idle(client: AsyncClient, session_id: str, timeout: float = 15.0) -> dict[str, Any]:
    """等待后台轮次结束，返回会话投影。"""
    deadline = time.monotonic() + timeout
    view: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = await client.get(f"/api/dc/sessions/{session_id}")
        assert response.status_code == 200
        view = response.json()
        turns = view.get("turns") or []
        if view.get("status") == "idle" and turns and turns[-1]["status"] != "running":
            return view
        await asyncio.sleep(0.05)
    raise AssertionError(f"session {session_id} did not become idle: {view}")


async def _collect_events(app: Any, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """消费 SSE 事件流（Last-Event-ID 回放 + 实时帧），读到 turn_finished 即停。"""

    async def _close_session() -> None:
        await asyncio.sleep(0.3)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as closer:
            await closer.delete(f"/api/dc/sessions/{session_id}")

    closer = asyncio.create_task(_close_session())
    events: list[dict[str, Any]] = []
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as sse_client:
        async with sse_client.stream(
            "GET",
            f"/api/dc/sessions/{session_id}/events",
            headers={"Last-Event-ID": "0"},
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                events.append(json.loads(line[len("data: ") :]))
                if events[-1]["type"] == "turn_finished" or len(events) >= limit:
                    break
    await closer
    return events


class TestDcTurnEventStream:
    """一轮对话的事件序列契约：截图事件必须带可下载的会话相对路径。"""

    async def test_turn_event_sequence_and_snapshot_artifact(
        self, app: Any, client: AsyncClient, patched_device: None
    ) -> None:
        create_resp = await client.post("/api/dc/sessions", json={"tier": 1})
        session_id = create_resp.json()["session_id"]

        send_resp = await client.post(f"/api/dc/sessions/{session_id}/messages", json={"text": "截个图看看"})
        assert send_resp.status_code == 202
        view = await _wait_idle(client, session_id)

        # 会话视图中的截图路径必须是会话相对 POSIX 路径，且能拼成可下载的 artifact URL
        snapshot_path = view["latest_snapshot_path"]
        assert snapshot_path == "screens/dc_test.jpeg"
        assert "\\" not in snapshot_path
        assert not Path(snapshot_path).is_absolute()
        artifact_resp = await client.get(f"/api/dc/sessions/{session_id}/artifacts/{snapshot_path}")
        assert artifact_resp.status_code == 200
        assert artifact_resp.content == b"fake-jpeg-bytes"

        events = await _collect_events(app, session_id)
        types = [event["type"] for event in events]

        assert "turn_started" in types
        assert "screenshot_captured" in types
        assert "assistant_message" in types
        assert types[-1] == "turn_finished"
        # 时序：turn_started → screenshot_captured → assistant_message → turn_finished
        assert types.index("turn_started") < types.index("screenshot_captured")
        assert types.index("screenshot_captured") < types.index("assistant_message")
        assert types.index("assistant_message") < types.index("turn_finished")

        screenshot_event = next(event for event in events if event["type"] == "screenshot_captured")
        assert screenshot_event["payload"]["snapshot_path"] == "screens/dc_test.jpeg"
        assert screenshot_event["payload"]["source"] == "context"

    async def test_mock_provider_emits_thinking_and_agent_text(
        self, app: Any, client: AsyncClient, patched_device: None
    ) -> None:
        create_resp = await client.post("/api/dc/sessions", json={})
        session_id = create_resp.json()["session_id"]

        await client.post(f"/api/dc/sessions/{session_id}/messages", json={"text": "你好"})
        await _wait_idle(client, session_id)

        events = await _collect_events(app, session_id)
        step_events = [event for event in events if event["type"] in ("thinking", "agent_text")]

        assert [event["type"] for event in step_events] == ["thinking", "agent_text"]
        assert all(event["payload"]["turn_id"].startswith("turn-") for event in step_events)
        assert step_events[0]["payload"]["text"]
        # 步骤块按事件时序排在最终总结之前
        types = [event["type"] for event in events]
        assert types.index("thinking") < types.index("assistant_message")

    async def test_session_view_keeps_relative_path_and_steps(self, client: AsyncClient, patched_device: None) -> None:
        create_resp = await client.post("/api/dc/sessions", json={})
        session_id = create_resp.json()["session_id"]

        await client.post(f"/api/dc/sessions/{session_id}/messages", json={"text": "截图"})
        view = await _wait_idle(client, session_id)

        assert view["latest_snapshot_path"] == "screens/dc_test.jpeg"
        # 刷新历史（前端全量重建）依赖该 steps 列表还原思考/叙述块
        kinds = [step["kind"] for step in view["turns"][0]["steps"]]
        assert kinds == ["thinking", "agent_text"]


class TestMessageDeltaReplayPolicy:
    """token 级增量不进回放缓冲，但必须实时送达订阅者。

    背景：``DcEventBus._recent`` 只有 ``dc_event_buffer_size``（默认 500）条，
    一轮对话的 message_delta 轻松上千条。若把增量也塞进回放缓冲，Last-Event-ID
    补发能力会被草稿挤空——重连客户端反而收不到工具事件与终态事件。
    """

    def test_deltas_are_broadcast_but_not_buffered_for_replay(self) -> None:
        bus = DcEventBus(buffer_size=10)
        queue = bus.subscribe()

        bus.emit("dc-1", DcEventType.TURN_STARTED, "开始")
        bus.emit("dc-1", DcEventType.MESSAGE_DELTA, "增量", {"delta": "你", "stream_key": "turn-1:m1:text"})
        bus.emit("dc-1", DcEventType.AGENT_TEXT, "叙述", {"text": "你好", "stream_key": "turn-1:m1:text"})

        # 实时订阅者拿到全部三类事件（顺序不变）
        live = [queue.get_nowait().type for _ in range(3)]
        assert live == [DcEventType.TURN_STARTED, DcEventType.MESSAGE_DELTA, DcEventType.AGENT_TEXT]

        # 回放缓冲里没有草稿，但全量事件仍在
        assert [event.type for event in bus.recent()] == [DcEventType.TURN_STARTED, DcEventType.AGENT_TEXT]

    def test_replay_after_id_ignores_delta_ids(self) -> None:
        bus = DcEventBus(buffer_size=10)

        first = bus.emit("dc-1", DcEventType.TURN_STARTED, "开始")
        bus.emit("dc-1", DcEventType.MESSAGE_DELTA, "增量", {"delta": "你"})
        bus.emit("dc-1", DcEventType.MESSAGE_DELTA, "增量", {"delta": "好"})
        last = bus.emit("dc-1", DcEventType.TURN_FINISHED, "结束")

        replayed = bus.recent(after_id=first.event_id)
        assert [event.type for event in replayed] == [DcEventType.TURN_FINISHED]
        # event_id 仍单调递增（回放过滤只依赖它，允许出现空洞）
        assert last.event_id > first.event_id
