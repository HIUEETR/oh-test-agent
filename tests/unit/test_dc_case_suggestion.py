"""Phase 7 回归（R16）：DC 轮次结束后自动沉淀可复用身份。

本次 DC 会话跑通了任务却 ``script: None``、``generated/`` 为空、session 上 ``bundle_name: None``：
任务完成却没留下任何可复用产物，用户点「生成脚本」还要手填 bundle。
"""

from __future__ import annotations

from pathlib import Path

from harmony_test_agent.config import Settings
from harmony_test_agent.dc.models import (
    DcEventType,
    DcToolInvocation,
    DcToolName,
    DcToolStatus,
    DcToolTier,
    DcTurnRecord,
    DcTurnStatus,
)
from harmony_test_agent.dc.provider import MockDcChatProvider
from harmony_test_agent.dc.session import DcSession
from harmony_test_agent.dc.store import DcSessionStore
from harmony_test_agent.models import CommandResult
from harmony_test_agent.storage.artifacts import ArtifactStore

BUNDLE = "com.huawei.hmos.calendar"
ABILITY = "MainAbility"


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
        **overrides,
    )


def _session(tmp_path: Path, **overrides: object) -> DcSession:
    settings = _settings(tmp_path, **overrides)
    return DcSession(
        session_id="dc-yield",
        device_id="mock-device",
        tier=DcToolTier.L2,
        settings=settings,
        artifacts=ArtifactStore(settings.resolved_runtime_dir),
        provider=MockDcChatProvider(),
        store=DcSessionStore(settings.resolved_runtime_dir),
    )


def _invocation(
    invocation_id: str,
    tool: DcToolName,
    *,
    success: bool = True,
    turn_id: str = "turn-1",
    args: dict[str, object] | None = None,
    result_summary: str = "",
) -> DcToolInvocation:
    return DcToolInvocation(
        invocation_id=invocation_id,
        turn_id=turn_id,
        tool=tool,
        tier=DcToolTier.L2,
        args=args or {},
        status=DcToolStatus.SUCCEEDED if success else DcToolStatus.FAILED,
        success=success,
        command=CommandResult(command="hdc", returncode=0 if success else 1),
        result_summary=result_summary,
    )


def _turn(turn_id: str = "turn-1", *, status: DcTurnStatus = DcTurnStatus.COMPLETED) -> DcTurnRecord:
    return DcTurnRecord(
        turn_id=turn_id,
        user_message="打开日历并切换到月视图",
        status=status,
        agent_summary="完成",
    )


def _events(session: DcSession, event_type: DcEventType) -> list:
    return [event for event in session.bus.recent() if event.type == event_type]


def test_completed_turn_suggests_reusable_identity(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.observed_identity = (BUNDLE, ABILITY)
    session.recorder.invocations.append(_invocation("inv-1", DcToolName.CLICK))
    session.recorder.invocations.append(_invocation("inv-2", DcToolName.SCREENSHOT))
    turn = _turn()

    session._finalize_turn(turn, DcTurnStatus.COMPLETED)

    suggested = _events(session, DcEventType.CASE_SUGGESTED)
    assert len(suggested) == 1
    payload = suggested[0].payload
    assert payload["bundle_name"] == BUNDLE
    assert payload["main_ability"] == ABILITY
    # 可回放步数只数真正能进脚本的工具（screenshot 属于不可回放工具）。
    assert payload["replayable_steps"] == 1
    assert payload["turn_id"] == "turn-1"

    view = session.to_view()
    assert view.suggested_bundle_name == BUNDLE
    assert view.suggested_main_ability == ABILITY
    assert view.suggested_step_count == 1


def test_generate_script_uses_the_suggested_identity(tmp_path: Path) -> None:
    """身份写回会话后，生成脚本不再需要用户手填 bundle。"""
    session = _session(tmp_path)
    session.observed_identity = (BUNDLE, ABILITY)
    session.recorder.invocations.append(_invocation("inv-1", DcToolName.CLICK))
    session._finalize_turn(_turn(), DcTurnStatus.COMPLETED)

    script = session.generate_script()

    assert BUNDLE in Path(script.python_path).read_text(encoding="utf-8")


def test_stale_foreground_app_does_not_hijack_the_generated_script(tmp_path: Path) -> None:
    """真机复盘 dc-20260922T171655Z-6fff3547：会话开始时的遗留前台不得锁死脚本身份。

    那次会话开始时前台是上一个任务遗留的网易云音乐（层级观测因此记下它），录制动作却全在
    会话中途 ``start_app`` 起的知乎++。旧实现把首次观测当最高优先级，生成的脚本 setup 驱动
    网易云、脚本体是知乎++ 的步骤，回放第一步就 ``Can't find component``。
    """
    session = _session(tmp_path)
    session.observed_identity = ("com.example.neteasymusic", "EntryAbility")
    session.recorder.invocations.append(
        _invocation(
            "inv-start",
            DcToolName.START_APP,
            args={"bundle_name": BUNDLE, "ability_name": ABILITY},
        )
    )
    session.recorder.invocations.append(
        _invocation(
            "inv-fg",
            DcToolName.FOREGROUND_APP,
            result_summary=f"bundle={BUNDLE}, ability={ABILITY}",
        )
    )
    session.recorder.invocations.append(_invocation("inv-click", DcToolName.CLICK))

    session._finalize_turn(_turn(), DcTurnStatus.COMPLETED)
    script = session.generate_script()

    assert session.suggested_identity == (BUNDLE, ABILITY)
    assert f"BUNDLE_NAME = '{BUNDLE}'" in Path(script.python_path).read_text(encoding="utf-8")
    assert not any("contradicts" in warning for warning in script.warnings)


def test_explicit_identity_that_contradicts_the_recording_is_warned(tmp_path: Path) -> None:
    """显式指定别的目标应用仍然生成，但必须把矛盾写进 warnings（可审计，不静默）。"""
    session = _session(tmp_path)
    session.recorder.invocations.append(
        _invocation(
            "inv-fg",
            DcToolName.FOREGROUND_APP,
            result_summary=f"bundle={BUNDLE}, ability={ABILITY}",
        )
    )

    script = session.generate_script("com.example.other", "EntryAbility")

    assert any("contradicts the recorded session identity" in warning for warning in script.warnings)
    assert "com.example.other" in Path(script.python_path).read_text(encoding="utf-8")


def test_failed_turn_does_not_suggest(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.observed_identity = (BUNDLE, ABILITY)
    session.recorder.invocations.append(_invocation("inv-1", DcToolName.CLICK))

    session._finalize_turn(_turn(status=DcTurnStatus.FAILED), DcTurnStatus.FAILED)

    assert _events(session, DcEventType.CASE_SUGGESTED) == []
    assert session.to_view().suggested_bundle_name is None


def test_turn_without_replayable_recording_does_not_suggest(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.observed_identity = (BUNDLE, ABILITY)
    session.recorder.invocations.append(_invocation("inv-1", DcToolName.SCREENSHOT))
    session.recorder.invocations.append(_invocation("inv-2", DcToolName.CLICK, success=False))

    session._finalize_turn(_turn(), DcTurnStatus.COMPLETED)

    assert _events(session, DcEventType.CASE_SUGGESTED) == []


def test_unknown_identity_does_not_suggest(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.recorder.invocations.append(_invocation("inv-1", DcToolName.CLICK))

    session._finalize_turn(_turn(), DcTurnStatus.COMPLETED)

    assert _events(session, DcEventType.CASE_SUGGESTED) == []


def test_auto_resolve_can_be_disabled(tmp_path: Path) -> None:
    session = _session(tmp_path, dc_auto_resolve_identity=False)
    session.observed_identity = (BUNDLE, ABILITY)
    session.recorder.invocations.append(_invocation("inv-1", DcToolName.CLICK))

    session._finalize_turn(_turn(), DcTurnStatus.COMPLETED)

    assert _events(session, DcEventType.CASE_SUGGESTED) == []
    assert session.to_view().suggested_bundle_name is None


def test_identity_falls_back_to_start_app_recording(tmp_path: Path) -> None:
    """会话自观测身份缺失时，用最近一次成功 start_app 的参数兜底。"""
    session = _session(tmp_path)
    session.recorder.invocations.append(
        _invocation(
            "inv-1",
            DcToolName.START_APP,
            args={"bundle_name": BUNDLE, "ability_name": ABILITY},
        )
    )

    session._finalize_turn(_turn(), DcTurnStatus.COMPLETED)

    suggested = _events(session, DcEventType.CASE_SUGGESTED)
    assert suggested and suggested[0].payload["bundle_name"] == BUNDLE


def test_case_suggested_event_type_is_in_dc_event_catalog() -> None:
    from harmony_test_agent.dc.models import DC_EVENT_TYPES

    assert "case_suggested" in DC_EVENT_TYPES
