"""DC 截图事件路径与工具事件 payload 测试。

覆盖方案 §1.2 / §1.3：SCREENSHOT_CAPTURED 必须携带「会话相对 POSIX 路径」，
``to_view()`` 的 latest_snapshot_path 同理，TOOL_CALL_FINISHED 携带 args 与
result_summary。
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from harmony_test_agent.config import Settings
from harmony_test_agent.dc.models import DcEventType, DcToolName, DcToolTier
from harmony_test_agent.dc.provider import MockDcChatProvider
from harmony_test_agent.dc.session import DcSession
from harmony_test_agent.dc.tools import DcActionRecorder, relative_artifact_path
from harmony_test_agent.models import CommandResult
from harmony_test_agent.storage.artifacts import ArtifactStore

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
    )


def make_session(tmp_path: Path) -> DcSession:
    settings = make_settings(tmp_path)
    return DcSession(
        session_id="dc-test-session",
        device_id="mock-device",
        tier=DcToolTier.L1,
        settings=settings,
        artifacts=ArtifactStore(settings.runtime_dir),
        provider=MockDcChatProvider(),
    )


def no_ui_hierarchy() -> Any:
    raise RuntimeError("no device in unit test")


# ---------------------------------------------------------------------------
# 相对路径助手
# ---------------------------------------------------------------------------


class TestRelativeArtifactPath:
    def test_inside_session_dir_returns_posix_relative(self, tmp_path: Path) -> None:
        base = tmp_path / "dc-1"
        rel = relative_artifact_path(base / "screens" / "dc_1.jpeg", base)

        assert rel == "screens/dc_1.jpeg"
        assert "\\" not in rel
        assert not Path(rel).is_absolute()

    def test_outside_session_dir_returns_none(self, tmp_path: Path) -> None:
        base = tmp_path / "dc-1"
        assert relative_artifact_path(tmp_path / "other" / "x.jpeg", base) is None

    def test_none_inputs_return_none(self, tmp_path: Path) -> None:
        assert relative_artifact_path(None, tmp_path) is None
        assert relative_artifact_path(tmp_path / "x.jpeg", None) is None

    def test_session_helper_delegates(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        assert session._rel_artifact(session.dir / "screens" / "dc_1.jpeg") == "screens/dc_1.jpeg"


# ---------------------------------------------------------------------------
# _capture_context 事件
# ---------------------------------------------------------------------------


class TestCaptureContextEvent:
    async def test_success_event_has_relative_snapshot_path(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        session.device.collect_ui_hierarchy = no_ui_hierarchy  # type: ignore[method-assign]
        shots = session.dir / "screens"
        shots.mkdir(parents=True, exist_ok=True)
        jpeg_path = shots / "dc_1.jpeg"
        jpeg_path.write_bytes(b"fake-jpeg")

        def fake_screenshot_jpeg(output_dir: Path, label: str = "screen") -> tuple[Path, bytes, int, int]:
            assert output_dir == shots
            return jpeg_path, b"fake-jpeg", 1080, 2232

        session.hdc.screenshot_jpeg = fake_screenshot_jpeg  # type: ignore[method-assign]

        jpeg_bytes, _ = await session._capture_context()

        assert jpeg_bytes == b"fake-jpeg"
        events = [event for event in session.bus.recent() if event.type == DcEventType.SCREENSHOT_CAPTURED]
        assert len(events) == 1
        payload = events[0].payload
        assert payload["snapshot_path"] == "screens/dc_1.jpeg"
        assert payload["source"] == "context"
        assert payload["width"] == 1080
        assert "\\" not in str(payload["snapshot_path"])

    async def test_failure_event_reports_error_and_null_path(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        session.device.collect_ui_hierarchy = no_ui_hierarchy  # type: ignore[method-assign]

        def boom(output_dir: Path, label: str = "screen") -> tuple[Path, bytes, int, int]:
            raise RuntimeError("device offline")

        session.hdc.screenshot_jpeg = boom  # type: ignore[method-assign]

        jpeg_bytes, _ = await session._capture_context()

        assert jpeg_bytes == b""
        events = [event for event in session.bus.recent() if event.type == DcEventType.SCREENSHOT_CAPTURED]
        assert len(events) == 1
        assert events[0].payload["snapshot_path"] is None
        assert "device offline" in str(events[0].payload["error"])


class TestToViewRelativePath:
    def test_latest_snapshot_path_is_relative(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        shots = session.dir / "screens"
        shots.mkdir(parents=True, exist_ok=True)
        jpeg_path = shots / "dc_9.jpeg"
        jpeg_path.write_bytes(b"x")
        session.snapshot_holder.update_jpeg(jpeg_path, b"x", 1080, 2232)

        view = session.to_view()

        assert view.latest_snapshot_path == "screens/dc_9.jpeg"

    def test_no_snapshot_yields_none(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        assert session.to_view().latest_snapshot_path is None


# ---------------------------------------------------------------------------
# 工具事件 payload
# ---------------------------------------------------------------------------


class TestToolCallFinishedPayload:
    async def test_payload_contains_args_and_result_summary(self, tmp_path: Path) -> None:
        recorder = DcActionRecorder()
        emitted: list[tuple[DcEventType, str, dict[str, Any]]] = []
        recorder.set_emitter(lambda event_type, message, payload: emitted.append((event_type, message, payload)))
        deps = SimpleNamespace(action_timeout=5.0, turn_id="turn-1")
        ctx = SimpleNamespace(deps=deps)

        def device_fn() -> CommandResult:
            return CommandResult(command="click", args=["1", "2"], returncode=0, stdout="clicked")

        result = await recorder.run(ctx, DcToolName.CLICK, {"x": 1, "y": 2}, device_fn)  # type: ignore[arg-type]

        assert result == "ok: clicked"
        finished = [event for event in emitted if event[0] == DcEventType.TOOL_CALL_FINISHED]
        assert len(finished) == 1
        payload = finished[0][2]
        assert payload["args"] == {"x": 1, "y": 2}
        assert payload["result_summary"] == "ok: clicked"
        assert payload["success"] is True
        assert "duration_ms" in payload

    async def test_failure_payload_carries_error(self, tmp_path: Path) -> None:
        recorder = DcActionRecorder()
        emitted: list[tuple[DcEventType, str, dict[str, Any]]] = []
        recorder.set_emitter(lambda event_type, message, payload: emitted.append((event_type, message, payload)))
        ctx = SimpleNamespace(deps=SimpleNamespace(action_timeout=5.0, turn_id="turn-1"))

        def device_fn() -> CommandResult:
            return CommandResult(command="click", returncode=1, stderr="boom")

        await recorder.run(ctx, DcToolName.CLICK, {"x": 1, "y": 2}, device_fn)  # type: ignore[arg-type]

        finished = [event for event in emitted if event[0] == DcEventType.TOOL_CALL_FINISHED][0]
        assert finished[2]["success"] is False
        assert finished[2]["error"] == "boom"
        assert finished[2]["result_summary"].startswith("FAILED")


class TestSessionStepRecording:
    """Provider emit 的思考/叙述既发事件也记入 turn.steps（刷新历史可还原）。"""

    async def test_handle_user_message_records_steps(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        session.device.collect_ui_hierarchy = no_ui_hierarchy  # type: ignore[method-assign]
        session.hdc.screenshot_jpeg = lambda output_dir, label="screen": (
            _write_fake_jpeg(output_dir),
            b"fake-jpeg",
            1080,
            2232,
        )

        turn = await session.handle_user_message("你好")

        assert [step.kind.value for step in turn.steps] == ["thinking", "agent_text"]
        assert "Mock" in turn.steps[0].text
        assert session.to_view().turns[0].steps == turn.steps

    async def test_events_carry_turn_id(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        session.device.collect_ui_hierarchy = no_ui_hierarchy  # type: ignore[method-assign]
        session.hdc.screenshot_jpeg = lambda output_dir, label="screen": (
            _write_fake_jpeg(output_dir),
            b"fake-jpeg",
            1080,
            2232,
        )

        turn = await session.handle_user_message("你好")

        step_events = [
            event for event in session.bus.recent() if event.type in (DcEventType.THINKING, DcEventType.AGENT_TEXT)
        ]
        assert step_events
        assert all(event.payload["turn_id"] == turn.turn_id for event in step_events)
        assert [event.event_id for event in step_events] == sorted(event.event_id for event in step_events)


def _write_fake_jpeg(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"dc_{int(time.time())}.jpeg"
    path.write_bytes(b"fake-jpeg")
    return path
