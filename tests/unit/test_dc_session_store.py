"""DC 会话快照仓库与「历史会话恢复」单元测试。

覆盖：落盘往返、命令输出截断、历史序列化（剥图片/thinking）与反序列化、
损坏快照降级、无快照历史会话合成、DcSession.snapshot/apply_snapshot、
按 turns 重建纯文本上下文、设备轮次互斥（冲突轮次 BLOCKED）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic_ai.messages import (
    BinaryContent,
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    UserPromptPart,
)

from harmony_test_agent.config import Settings
from harmony_test_agent.dc.models import (
    DcEventType,
    DcScriptArtifact,
    DcSessionNotFound,
    DcSessionSnapshot,
    DcStepKind,
    DcStepRecord,
    DcToolInvocation,
    DcToolName,
    DcToolTier,
    DcTurnRecord,
    DcTurnStatus,
)
from harmony_test_agent.dc.provider import MockDcChatProvider
from harmony_test_agent.dc.session import DcSession, DcSessionManager
from harmony_test_agent.dc.store import COMMAND_OUTPUT_LIMIT, DcSessionStore
from harmony_test_agent.models import CommandResult
from harmony_test_agent.storage.artifacts import ArtifactStore


@contextmanager
def patched_device() -> Iterator[None]:
    """Mock 设备连接/健康检查，避免单测触碰真实 HDC。"""
    with (
        patch("harmony_test_agent.dc.session.HarmonyDeviceAdapter.connect", return_value=None),
        patch(
            "harmony_test_agent.dc.session.HarmonyDeviceAdapter.health_check",
            return_value={"connected": True, "id": "mock-device"},
        ),
    ):
        yield


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
    )


def make_store(tmp_path: Path) -> DcSessionStore:
    return DcSessionStore(tmp_path / "runs")


def make_session(
    tmp_path: Path,
    store: DcSessionStore | None = None,
    session_id: str = "dc-test-session",
    device_id: str = "mock-device",
) -> DcSession:
    settings = make_settings(tmp_path)
    resolved_store = store or DcSessionStore(settings.resolved_runtime_dir)
    return DcSession(
        session_id=session_id,
        device_id=device_id,
        tier=DcToolTier.L1,
        settings=settings,
        artifacts=ArtifactStore(settings.resolved_runtime_dir),
        provider=MockDcChatProvider(),
        store=resolved_store,
    )


def completed_turn(turn_id: str = "turn-1", summary: str = "任务完成") -> DcTurnRecord:
    return DcTurnRecord(
        turn_id=turn_id,
        user_message="截个图看看",
        status=DcTurnStatus.COMPLETED,
        agent_summary=summary,
        invocation_ids=["inv-1"],
        steps=[DcStepRecord(step=1, kind=DcStepKind.THINKING, text="先看看界面")],
    )


def invocation(stdout: str = "ok") -> DcToolInvocation:
    return DcToolInvocation(
        invocation_id="inv-1",
        turn_id="turn-1",
        tool=DcToolName.LIST_APPS,
        tier=DcToolTier.L2,
        args={"query": "com.example"},
        command=CommandResult(command="hdc shell bm dump -a", returncode=0, stdout=stdout),
    )


class TestSnapshotRoundTrip:
    def test_save_then_load_preserves_session_state(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        snapshot = DcSessionSnapshot(
            session_id="dc-1",
            device_id="127.0.0.1:5555",
            tier=DcToolTier.L3,
            status="closed",
            turns=[completed_turn()],
            invocations=[invocation()],
            script=DcScriptArtifact(python_path="x.py", python_text="print(1)"),
            latest_snapshot_path="screens/dc_1.jpeg",
        )

        path = store.save(snapshot)
        assert path == store.path_for("dc-1")
        assert path.exists()

        loaded = store.load("dc-1")
        assert loaded is not None
        assert loaded.has_snapshot is True
        assert loaded.tier is DcToolTier.L3
        assert loaded.device_id == "127.0.0.1:5555"
        assert [turn.turn_id for turn in loaded.turns] == ["turn-1"]
        assert loaded.turns[0].steps[0].text == "先看看界面"
        assert loaded.invocations[0].tool is DcToolName.LIST_APPS
        assert loaded.script is not None and loaded.script.python_text == "print(1)"
        assert loaded.latest_snapshot_path == "screens/dc_1.jpeg"

    def test_long_command_output_is_truncated_on_disk(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.save(
            DcSessionSnapshot(session_id="dc-1", invocations=[invocation(stdout="A" * (COMMAND_OUTPUT_LIMIT + 5000))])
        )

        loaded = store.load("dc-1")
        assert loaded is not None
        stdout = loaded.invocations[0].command.stdout
        assert stdout is not None
        assert stdout.startswith("A" * 100)
        assert "truncated 5000 chars" in stdout
        assert len(stdout) < COMMAND_OUTPUT_LIMIT + 100

    def test_missing_session_returns_none(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        assert store.load("dc-absent") is None

    def test_corrupt_snapshot_degrades_to_directory_entry(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        path = store.path_for("dc-broken")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

        loaded = store.load("dc-broken")

        assert loaded is not None
        assert loaded.has_snapshot is False
        assert loaded.turns == []


class TestHistorySerialization:
    def test_images_replaced_and_thinking_dropped(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        history = [
            ModelRequest(
                parts=[
                    UserPromptPart(content="看看当前界面"),
                    BinaryContent(data=b"jpeg-bytes", media_type="image/jpeg"),
                ]
            ),
            ModelResponse(
                parts=[
                    ThinkingPart(content="SECRET_THOUGHT"),
                    TextPart(content="我来截图"),
                    ToolCallPart(tool_name="screenshot", args={}),
                ]
            ),
        ]

        payload = store.dump_history(history)
        serialized = str(payload)

        assert payload
        assert "[screenshot omitted from history]" in serialized
        assert "SECRET_THOUGHT" not in serialized
        assert "jpeg-bytes" not in serialized

    def test_tool_call_context_survives_round_trip(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        history = [
            ModelRequest(parts=[UserPromptPart(content="探测"), BinaryContent(data=b"jpeg", media_type="image/jpeg")]),
            ModelResponse(parts=[TextPart(content="先探测"), ToolCallPart(tool_name="ping", args={"n": 1})]),
        ]

        payload = store.dump_history(history)
        restored = store.load_history(payload)

        # 请求侧图片若被替换为 TextPart，会让整个 ModelRequest 校验失败并丢光工具上下文
        assert restored is not None
        assert len(restored) == 2
        kinds = [getattr(part, "part_kind", None) for message in restored for part in message.parts]
        assert "tool-call" in kinds
        assert "text" in kinds
        assert "[screenshot omitted from history]" in str(restored[0].parts[0].content)

    def test_invalid_payload_returns_none(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        assert store.load_history([{"part_kind": "text"}]) is None
        assert store.load_history([]) is None

    def test_failed_dump_returns_empty(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        assert store.dump_history([]) == []
        # 非消息对象无法序列化 → 返回空列表，调用方回退文本上下文
        assert store.dump_history([object()]) == []


class TestListSummaries:
    def test_sorted_by_last_active_and_includes_orphan_dirs(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.save(
            DcSessionSnapshot(
                session_id="dc-old",
                device_id="dev",
                turns=[completed_turn()],
                last_active_at=completed_turn().started_at.replace(year=2020),
            )
        )
        # 只有产物目录、没有快照的历史会话（升级前创建的会话）
        orphan = store.session_dir("dc-orphan")
        (orphan / "generated").mkdir(parents=True, exist_ok=True)
        (orphan / "generated" / "dc_test_dc_orphan.py").write_text("print('old')\n", encoding="utf-8")

        summaries = store.list_summaries()
        by_id = {item.session_id: item for item in summaries}

        assert set(by_id) == {"dc-old", "dc-orphan"}
        assert by_id["dc-old"].has_snapshot is True
        assert by_id["dc-orphan"].has_snapshot is False
        assert by_id["dc-orphan"].script is not None
        assert by_id["dc-orphan"].script.python_text == "print('old')\n"
        # 最近的排在最前（orphan 目录刚创建）
        assert summaries[0].session_id == "dc-orphan"

    def test_summary_projection(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.save(DcSessionSnapshot(session_id="dc-1", device_id="dev", turns=[completed_turn()]))

        summaries = store.list_summaries()
        summary = summaries[0].summary(active=False)

        assert summary.turn_count == 1
        assert summary.active is False
        assert summary.restorable is True


class TestSessionPersistence:
    def test_snapshot_and_apply_restores_turns_invocations_and_script(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        session = make_session(tmp_path, store)
        session.turns.append(completed_turn())
        session.recorder.invocations.append(invocation())
        session.script = DcScriptArtifact(python_path="x.py", python_text="print(1)")
        session.save_state()

        restored = make_session(tmp_path, store)
        snapshot = store.load(session.session_id)
        assert snapshot is not None
        restored.apply_snapshot(snapshot)

        assert [turn.turn_id for turn in restored.turns] == ["turn-1"]
        assert [inv.invocation_id for inv in restored.recorder.invocations] == ["inv-1"]
        assert restored.script is not None
        assert restored.to_view().restored is True
        # Mock Provider 不累积 history → 按 turns 重建纯文本上下文
        assert restored.to_view().restored_context == "text"
        assert restored.status == "idle"

    def test_rebuilt_history_uses_turns_and_tool_digest(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        session = make_session(tmp_path, store)
        session.turns.append(completed_turn(summary=""))
        session.recorder.invocations.append(invocation())
        session.save_state()

        restored = make_session(tmp_path, store)
        snapshot = store.load(session.session_id)
        assert snapshot is not None
        restored.apply_snapshot(snapshot)

        assert len(restored.history) == 2
        assert restored.history[0].parts[0].content == "截个图看看"
        digest = restored.history[1].parts[0].content
        assert "工具调用记录" in digest
        assert "list_apps" in digest

    def test_full_history_is_preferred_when_available(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        session = make_session(tmp_path, store)
        session.history = [
            ModelRequest(parts=[UserPromptPart(content="上一轮")]),
            ModelResponse(parts=[TextPart(content="上一轮回答")]),
        ]
        session.save_state()

        restored = make_session(tmp_path, store)
        snapshot = store.load(session.session_id)
        assert snapshot is not None
        assert snapshot.history_kind == "model_messages"
        restored.apply_snapshot(snapshot)

        assert restored.to_view().restored_context == "full"
        assert restored.history[1].parts[0].content == "上一轮回答"

    def test_save_state_without_store_is_noop(self, tmp_path: Path) -> None:
        session = make_session(tmp_path, store=None)
        session.store = None

        session.save_state()  # 不应抛异常


class TestDeviceTurnLock:
    def test_second_session_turn_is_blocked_while_device_is_busy(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        artifacts = ArtifactStore(settings.resolved_runtime_dir)
        manager = DcSessionManager(settings, artifacts)
        first = manager.create(device_id="dev-1")
        second = manager.create(device_id="dev-1")

        async def scenario() -> DcTurnRecord:
            lock = manager.device_turn_lock("dev-1")
            async with lock:
                return await second.handle_user_message("并发消息")

        turn = asyncio.run(scenario())

        assert turn.status is DcTurnStatus.BLOCKED
        assert "busy" in (turn.error or "")
        assert second.status == "idle"
        assert [event.type for event in second.bus.recent()] == [DcEventType.ERROR]
        assert first.session_id != second.session_id

    def test_multiple_sessions_on_same_device_are_allowed(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        manager = DcSessionManager(settings, ArtifactStore(settings.resolved_runtime_dir))

        first = manager.create(device_id="dev-1")
        second = manager.create(device_id="dev-1")

        assert len(manager.sessions) == 2
        assert first.device_lock is second.device_lock


class TestManagerListingAndResume:
    def test_list_sessions_merges_memory_and_disk(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        manager = DcSessionManager(settings, ArtifactStore(settings.resolved_runtime_dir))
        active = manager.create(device_id="dev-1")

        store = DcSessionStore(settings.resolved_runtime_dir)
        store.save(DcSessionSnapshot(session_id="dc-disk-only", device_id="dev-1", turns=[completed_turn()]))

        summaries = {item.session_id: item for item in manager.list_sessions()}

        assert summaries[active.session_id].active is True
        assert summaries["dc-disk-only"].active is False
        assert summaries["dc-disk-only"].turn_count == 1
        assert manager.list_sessions()[0].active is True

    def test_resume_loads_disk_session_and_caches_it(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        manager = DcSessionManager(settings, ArtifactStore(settings.resolved_runtime_dir))
        store = DcSessionStore(settings.resolved_runtime_dir)
        store.save(DcSessionSnapshot(session_id="dc-1", device_id="dev-1", turns=[completed_turn()]))

        with patched_device():
            resumed = asyncio.run(manager.resume("dc-1"))
            again = asyncio.run(manager.resume("dc-1"))

        assert resumed.session_id == "dc-1"
        assert [turn.turn_id for turn in resumed.turns] == ["turn-1"]
        assert manager.get_or_none("dc-1") is resumed
        # 幂等：再次 resume 返回同一实例
        assert again is resumed

    def test_resume_unknown_session_raises(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        manager = DcSessionManager(settings, ArtifactStore(settings.resolved_runtime_dir))

        with pytest.raises(DcSessionNotFound):
            asyncio.run(manager.resume("dc-absent"))
