"""Phase 1 端到端契约（API + SSE）：工具执行期间账本与事件流可见。

回归背景：旧实现里工具完成后才追加 invocation，前端在工具运行期间只能看到
「0 步」。这里用一个真实调用 ``list_apps`` 的 provider 驱动整轮对话，断言：

- 工具执行期间 ``DcSession.to_view()`` 已含 ``running`` 记录（前端 GET 对账可见）；
- SSE 发出成对的 ``tool_call_started`` / ``tool_call_finished``，同一 invocation_id；
- 执行超过心跳间隔时还有 ``tool_call_progress``（长静默不再无解释）；
- 轮次结束后同一条记录变 ``succeeded``，``turn.invocation_ids`` 与其一致。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from harmony_test_agent.api.app import create_app
from harmony_test_agent.config import Settings
from harmony_test_agent.dc.models import DcChatResponse, DcEventType
from harmony_test_agent.dc.tools import tool_list_apps
from harmony_test_agent.models import CommandResult

BUNDLES = ["com.example.calendar", "com.example.browser"]


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


def bm_dump_all() -> CommandResult:
    stdout = "".join(f"  bundleName: {bundle}\n" for bundle in BUNDLES)
    return CommandResult(command="hdc shell bm dump -a", returncode=0, stdout=stdout)


class LedgerProbeProvider:
    """真实调用一次 ``list_apps`` 工具，并在工具运行中读取会话投影。"""

    name = "ledger-probe"

    def __init__(self) -> None:
        self.session: Any = None
        self.release = threading.Event()
        self.tool_started = threading.Event()
        self.running_view: list[tuple[str, str]] = []
        self.mid_turn_ids: list[str] = []

    def list_bundle_names(self, timeout: float = 20) -> CommandResult:
        self.tool_started.set()
        self.release.wait(timeout=10)
        return bm_dump_all()

    async def chat(self, request: Any) -> DcChatResponse:
        ctx = SimpleNamespace(deps=request.tool_context)
        task = asyncio.create_task(tool_list_apps(ctx, query=None))
        deadline = time.monotonic() + 5
        while not self.tool_started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

        view = self.session.to_view()
        self.running_view = [(inv.invocation_id, inv.status.value) for inv in view.invocations]
        self.mid_turn_ids = [view.active_turn_id] if view.active_turn_id else []

        await asyncio.sleep(0.6)  # 超过心跳间隔，确保有 progress 事件
        self.release.set()
        await task
        return DcChatResponse(output_text="已完成列举", history=[], tool_call_count=1)


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
        dc_progress_interval=0.2,
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


class TestLiveLedgerEndToEnd:
    async def test_running_invocation_visible_during_tool_and_settled_after(
        self, app: Any, patched_device: None
    ) -> None:
        async with await make_client(app) as client:
            session_id = (await client.post("/api/dc/sessions", json={"tier": 2})).json()["session_id"]
            session = app.state.dc_manager.get(session_id)
            provider = LedgerProbeProvider()
            session.provider = provider  # type: ignore[assignment]
            provider.session = session
            with patch(
                "harmony_test_agent.dc.session.DcHdcExecutor.list_bundle_names",
                provider.list_bundle_names,
            ):
                sent = await client.post(f"/api/dc/sessions/{session_id}/messages", json={"text": "列出应用"})
                assert sent.status_code == 202

                deadline = time.monotonic() + 15
                view: dict[str, Any] = {}
                while time.monotonic() < deadline:
                    view = (await client.get(f"/api/dc/sessions/{session_id}")).json()
                    turns = view.get("turns") or []
                    if view["status"] == "idle" and turns and turns[-1]["status"] != "running":
                        break
                    await asyncio.sleep(0.05)

            assert provider.running_view, "工具运行期间会话投影必须能看到调用"
            invocation_id, status = provider.running_view[0]
            assert status == "running"
            assert provider.mid_turn_ids, "运行期间必须能拿到 active_turn_id"

            assert len(view["invocations"]) == 1, "同一调用不能出现第二条记录"
            invocation = view["invocations"][0]
            assert invocation["invocation_id"] == invocation_id
            assert invocation["status"] == "succeeded"
            assert invocation["ended_at"] is not None
            assert invocation["effect_status"] == "none"

            turn = view["turns"][-1]
            assert turn["status"] == "completed"
            assert turn["invocation_ids"] == [invocation_id]

            event_types = [event.type for event in session.bus.recent()]
            assert DcEventType.TOOL_CALL_STARTED in event_types
            assert DcEventType.TOOL_CALL_PROGRESS in event_types
            assert DcEventType.TOOL_CALL_FINISHED in event_types

            started = [event for event in session.bus.recent() if event.type == DcEventType.TOOL_CALL_STARTED][-1]
            finished = [event for event in session.bus.recent() if event.type == DcEventType.TOOL_CALL_FINISHED][-1]
            assert started.payload["invocation_id"] == finished.payload["invocation_id"] == invocation_id
            assert started.payload["status"] == "running"
            assert finished.payload["status"] == "succeeded"
            assert finished.payload["duration_ms"] >= 0

    async def test_sse_stream_carries_tool_lifecycle_events(self, app: Any, patched_device: None) -> None:
        """SSE 回放必须带完整工具生命周期（``httpx.ASGITransport`` 会缓冲响应体，
        因此按既有约定：轮次结束后用 Last-Event-ID 回放并主动关闭会话结束流）。"""

        async def close_session(session_id: str) -> None:
            await asyncio.sleep(0.3)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as closer:
                await closer.delete(f"/api/dc/sessions/{session_id}")

        async with await make_client(app) as client:
            session_id = (await client.post("/api/dc/sessions", json={"tier": 2})).json()["session_id"]
            session = app.state.dc_manager.get(session_id)
            provider = LedgerProbeProvider()
            session.provider = provider  # type: ignore[assignment]
            provider.session = session

            with patch(
                "harmony_test_agent.dc.session.DcHdcExecutor.list_bundle_names",
                provider.list_bundle_names,
            ):
                await client.post(f"/api/dc/sessions/{session_id}/messages", json={"text": "列出应用"})
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    view = (await client.get(f"/api/dc/sessions/{session_id}")).json()
                    turns = view.get("turns") or []
                    if turns and turns[-1]["status"] != "running":
                        break
                    await asyncio.sleep(0.05)

            closer = asyncio.create_task(close_session(session_id))
            events: list[dict[str, Any]] = []
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as sse_client:
                async with sse_client.stream(
                    "GET",
                    f"/api/dc/sessions/{session_id}/events",
                    headers={"Last-Event-ID": "0"},
                ) as response:
                    assert response.status_code == 200
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            events.append(json.loads(line[len("data: ") :]))
                        if len(events) >= 80:
                            break
            await closer

            types = [event["type"] for event in events]
            assert "tool_call_started" in types
            assert "tool_call_progress" in types
            assert "tool_call_finished" in types
            assert "turn_finished" in types

            started = next(event for event in events if event["type"] == "tool_call_started")
            finished = next(event for event in events if event["type"] == "tool_call_finished")
            assert started["payload"]["invocation_id"] == finished["payload"]["invocation_id"]
            assert started["payload"]["status"] == "running"
            assert finished["payload"]["status"] == "succeeded"
            assert types.index("tool_call_started") < types.index("tool_call_finished")
