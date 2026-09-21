import asyncio
from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient
from PIL import Image

from harmony_test_agent.api import create_app
from harmony_test_agent.config import Settings
from harmony_test_agent.generation import HypiumGenerator
from harmony_test_agent.models import (
    ActionResult,
    AssertionDefinition,
    EventType,
    LocatorCandidate,
    LocatorKind,
    PageGraph,
    PageNode,
    ProfileStatus,
    RunEvent,
    RunState,
    RunTrace,
    ScreenSnapshot,
    StableLocator,
    TargetAppProfile,
    ToolName,
    UIElement,
)
from harmony_test_agent.profiles import (
    ProfileLockedError,
    ProfileNotFoundError,
    ProfileTransitionError,
)
from harmony_test_agent.reporting import ReportBuilder


def test_result_api_serves_trace_sse_script_report_and_artifact(tmp_path: Path):
    profile_path = tmp_path / "profile.json"
    profile = TargetAppProfile(
        target_app_id="zhihu-plus",
        display_name="知乎++",
        bundle_name="com.example",
        main_ability="EntryAbility",
        reset_strategy={"kind": "stop_then_start_only", "clear_app_data": False},
    )
    profile_path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    settings = Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=profile_path,
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
    )
    app = create_app(settings)
    manager = app.state.manager
    run_id = "run-api-test"
    run_dir = manager.artifacts.run_dir(run_id)
    image_path = run_dir / "screens" / "screen.png"
    Image.new("RGB", (100, 200), "navy").save(image_path)
    snapshot = ScreenSnapshot(
        snapshot_id="snapshot-api",
        run_id=run_id,
        image_path=image_path,
        image_sha256="abc",
        width=100,
        height=200,
        page_path="pages/Index",
        elements=[UIElement(element_id="search", key="search_key", content="搜索")],
    )
    node = PageNode(
        node_id="page-home",
        signature="signature",
        page_path="pages/Index",
        title="首页",
        snapshot_id=snapshot.snapshot_id,
        image_path=image_path,
        discovered_order=1,
        element_count=1,
    )
    trace = RunTrace(
        run_id=run_id,
        target_app_id="zhihu-plus",
        task="API contract",
        device_id="fake-device",
        state=RunState.COMPLETED,
        snapshots=[snapshot],
        graph=PageGraph(nodes=[node]),
    )
    trace.generated = HypiumGenerator(manager.artifacts).generate(trace, profile)
    event = RunEvent(
        event_id=1,
        run_id=run_id,
        type=EventType.RUN_FINISHED,
        message="done",
    )
    trace.events.append(event)
    manager.repository.add_event(event)
    manager.repository.save_trace(trace)
    manager.artifacts.save_trace(trace)
    ReportBuilder(manager.artifacts).build(trace)

    with TestClient(app) as client:
        assert client.get(f"/api/runs/{run_id}").json()["state"] == "completed"
        assert client.get(f"/api/runs/{run_id}/graph").json()["nodes"][0]["node_id"] == "page-home"
        assert "UiDriver.connect" in client.get(f"/api/runs/{run_id}/script").json()["python"]

        inline = client.get(f"/api/runs/{run_id}/report")
        assert inline.status_code == 200
        assert inline.headers["content-type"].startswith("text/html")
        assert "content-disposition" not in inline.headers

        download = client.get(f"/api/runs/{run_id}/report?download=true")
        assert "attachment" in download.headers["content-disposition"]

        artifact = client.get(f"/api/runs/{run_id}/artifacts/screens/screen.png")
        assert artifact.status_code == 200
        assert artifact.headers["content-type"] == "image/png"

        events = client.get(f"/api/runs/{run_id}/events")
        assert events.status_code == 200
        assert "event: run_finished" in events.text
        assert '"event_id":1' in events.text

        resumed = client.get(
            f"/api/runs/{run_id}/events",
            headers={"Last-Event-ID": "1"},
        )
        assert '"event_id":1' not in resumed.text


def test_profile_api_lists_flat_summaries_and_reads_drafts(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    draft = profiles / "draft"
    draft.mkdir(parents=True)
    profile = TargetAppProfile(
        target_app_id="notes",
        display_name="Notes",
        bundle_name="com.example.notes",
        main_ability="EntryAbility",
    )
    (draft / "notes.json").write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    settings = Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=profiles,
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
    )

    with TestClient(create_app(settings)) as client:
        summaries = client.get("/api/profiles").json()
        detail = client.get("/api/profiles/notes")

    assert summaries[0]["profile_id"] == "draft:notes"
    assert summaries[0]["version_name"] is None
    assert summaries[0]["status"] == "draft"
    assert detail.status_code == 200
    assert detail.json()["bundle_name"] == "com.example.notes"


def test_run_request_moves_temporary_test_out_of_discovery_policy() -> None:
    from harmony_test_agent.api.app import _build_run_request

    request = _build_run_request(
        {
            "target": {"bundle_name": "com.example.notes"},
            "discovery": {"temporary_test": True, "max_pages": 4},
        }
    )

    assert request.temporary_test is True
    assert request.exploration_policy.max_pages == 4


class _ProfileRegistryStub:
    def __init__(self, *, profile: TargetAppProfile | None = None) -> None:
        self.profile = profile
        self.get_any_error: Exception | None = None
        self.get_error: Exception | None = None
        self.lock_error: Exception | None = None
        self.rollback_error: Exception | None = None
        self.invalidate_error: Exception | None = None

    def get_any(self, **_: object) -> TargetAppProfile | None:
        if self.get_any_error:
            raise self.get_any_error
        return self.profile

    def get(self, **_: object) -> TargetAppProfile | None:
        if self.get_error:
            raise self.get_error
        return self.profile

    def set_locked(self, target_app_id: str, locked: bool = True) -> TargetAppProfile:
        if self.lock_error:
            raise self.lock_error
        if self.profile is None:
            raise ProfileNotFoundError(target_app_id)
        return self.profile.model_copy(update={"locked": locked})

    def rollback(self, *_: object, **__: object) -> Path:
        if self.rollback_error:
            raise self.rollback_error
        if self.profile is None:
            raise ProfileNotFoundError("Profile history not found")
        return Path("profiles/notes.json")

    def invalidate(self, *_: object, **__: object) -> Path:
        if self.invalidate_error:
            raise self.invalidate_error
        return Path("profiles/draft/notes.json")


def _profile_api_client(tmp_path: Path) -> tuple[TestClient, object, _ProfileRegistryStub]:
    settings = Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
    )
    app = create_app(settings)
    page_locators = [
        StableLocator(
            name=f"page-{index}",
            page_signature=f"page-{index}",
            key=f"page-key-{index}",
            observed_rounds=3,
            unique_match_rounds=3,
            evidence_snapshot_ids=[f"snapshot-round-{round}" for round in range(1, 4)],
        )
        for index in range(1, 4)
    ]
    assertions = [
        AssertionDefinition(
            name=f"assertion-{index}",
            kind="visible",
            target=f"page-key-{index}",
            page_signature=f"page-{index}",
            observed_rounds=3,
            evidence_snapshot_ids=[f"snapshot-round-{round}" for round in range(1, 4)],
        )
        for index in range(1, 3)
    ]
    profile = TargetAppProfile(
        target_app_id="notes",
        display_name="Notes",
        bundle_name="com.example.notes",
        main_ability="EntryAbility",
        status=ProfileStatus.VERIFIED,
        stable_locator_inventory=page_locators,
        assertion_inventory=assertions,
        provenance={
            "verified_at": "2026-01-01T00:00:00Z",
            "discovery_run_id": "run-profile",
            "hypium_replay_run_ids": [
                "run-profile:profile-attempt-1",
                "run-profile:profile-attempt-2",
                "run-profile:profile-attempt-3",
            ],
            "evidence": {"verification_passed": True, "cross_bundle_violations": 0},
        },
    )
    registry = _ProfileRegistryStub(profile=profile)
    app.state.manager.profile_registry = registry
    return TestClient(app, raise_server_exceptions=False), app, registry


def test_profile_api_preserves_success_response_contracts(tmp_path: Path) -> None:
    client, app, _ = _profile_api_client(tmp_path)
    app.state.manager.start = Mock(return_value="run-profile-verify")

    with client:
        verify = client.post("/api/profiles/notes/verify", json={"status": "verified"})
        locked = client.post("/api/profiles/notes/lock", json={"locked": True})
        rollback = client.post("/api/profiles/notes/rollback", json={})
        invalidated = client.post("/api/profiles/notes/invalidate", json={"reason": "app version changed"})

    assert verify.status_code == 202
    assert verify.json() == {
        "run_id": "run-profile-verify",
        "state": "created",
        "profile_id": "notes",
    }
    assert locked.status_code == 200
    assert locked.json()["locked"] is True
    assert rollback.json()["status"] == "verified"
    assert invalidated.json()["status"] == "invalid"


def test_profile_api_maps_not_found_and_invalid_requests(tmp_path: Path) -> None:
    client, _, registry = _profile_api_client(tmp_path)

    with client:
        registry.profile = None
        assert client.get("/api/profiles/missing").status_code == 404
        assert client.post("/api/profiles/missing/verify", json={}).status_code == 404
        assert client.post("/api/profiles/missing/lock", json={}).status_code == 404
        assert client.post("/api/profiles/missing/rollback", json={}).status_code == 404
        assert client.post("/api/profiles/missing/invalidate", json={"reason": "stale"}).status_code == 404

        assert client.post("/api/profiles/notes/verify", json={"status": "unknown"}).status_code == 422
        assert client.post("/api/profiles/notes/lock", json={"locked": "yes"}).status_code == 422
        assert (
            client.post(
                "/api/profiles/notes/rollback",
                json={"backup_path": "one.json", "backup_name": "two.json"},
            ).status_code
            == 422
        )
        assert client.post("/api/profiles/notes/invalidate", json={"reason": "  "}).status_code == 422


def test_profile_api_maps_lock_conflicts_and_transition_errors(tmp_path: Path) -> None:
    client, _, registry = _profile_api_client(tmp_path)

    with client:
        registry.get_any_error = ProfileTransitionError("invalid transition")
        assert client.post("/api/profiles/notes/verify", json={}).status_code == 422
        registry.get_any_error = None

        registry.lock_error = ProfileLockedError("locked")
        assert client.post("/api/profiles/notes/lock", json={}).status_code == 409

        registry.lock_error = None
        registry.rollback_error = ProfileLockedError("locked")
        assert client.post("/api/profiles/notes/rollback", json={}).status_code == 409

        registry.rollback_error = ProfileTransitionError("invalid transition")
        assert client.post("/api/profiles/notes/rollback", json={}).status_code == 422

        registry.profile = registry.profile.model_copy(update={"locked": True})
        assert client.post("/api/profiles/notes/invalidate", json={"reason": "stale"}).status_code == 409


def test_profile_api_leaves_unhandled_errors_as_500(tmp_path: Path) -> None:
    client, app, registry = _profile_api_client(tmp_path)
    app.state.manager.start = Mock(side_effect=RuntimeError("unexpected"))

    with client:
        verify = client.post("/api/profiles/notes/verify", json={})
        assert verify.status_code == 500

        registry.get_any_error = RuntimeError("unexpected")
        assert client.get("/api/profiles/notes").status_code == 500
        registry.get_any_error = None

        registry.lock_error = RuntimeError("unexpected")
        assert client.post("/api/profiles/notes/lock", json={}).status_code == 500
        registry.lock_error = None

        registry.rollback_error = RuntimeError("unexpected")
        assert client.post("/api/profiles/notes/rollback", json={}).status_code == 500
        registry.rollback_error = None

        registry.get_error = RuntimeError("unexpected")
        assert client.post("/api/profiles/notes/invalidate", json={"reason": "stale"}).status_code == 500


def _eligible_trace(run_id: str, manager, profile: TargetAppProfile) -> RunTrace:
    trace = RunTrace(
        run_id=run_id,
        target_app_id=profile.target_app_id,
        task="replay contract",
        device_id="fake-device",
        state=RunState.COMPLETED,
        agent_outcome="completed",
        actions=[
            ActionResult(
                step_id="assert",
                tool=ToolName.ASSERT_VISIBLE,
                success=True,
                params={"target": "搜索"},
            ),
            ActionResult(step_id="finish", tool=ToolName.FINISH, success=True),
        ],
    )
    trace.generated = HypiumGenerator(manager.artifacts).generate(trace, profile)
    manager.repository.save_trace(trace)
    manager.artifacts.save_trace(trace)
    return trace


def test_execute_queues_replay_persists_each_attempt_and_rejects_concurrent_request(tmp_path: Path, monkeypatch):
    profile_path = tmp_path / "profile.json"
    profile = TargetAppProfile(
        target_app_id="zhihu-plus",
        display_name="知乎++",
        bundle_name="com.example",
        main_ability="EntryAbility",
    )
    profile_path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    settings = Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=profile_path,
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
    )
    app = create_app(settings)
    manager = app.state.manager
    trace = _eligible_trace("run-replay", manager, profile)
    assert trace.generated and trace.generated.replay_eligible
    release = asyncio.Event()

    async def blocked_run(run_id: str):
        await release.wait()

    monkeypatch.setattr(manager, "_run_replay", blocked_run)
    with TestClient(app) as client:
        accepted = client.post("/api/runs/run-replay/execute?attempts=3")
        assert accepted.status_code == 202
        queued = client.get("/api/runs/run-replay").json()
        assert queued["state"] == "completed"
        assert queued["agent_outcome"] == "completed"
        assert queued["replay_status"] == "pending"
        assert queued["replay_total"] == 3
        duplicate = client.post("/api/runs/run-replay/execute?attempts=1")
        assert duplicate.status_code == 409
        release.set()


def test_execute_rejects_unrunnable_script_with_runnable_blockers(tmp_path: Path):
    """没有可回放动作（G3 第一条物理必要条件）⇒ 409 + runnable_blockers。

    注意失败动作本身**不再**阻断执行：源结局只决定 confidence（见下一个用例）。
    """
    profile_path = tmp_path / "profile.json"
    profile = TargetAppProfile(target_app_id="zhihu-plus", display_name="知乎++", bundle_name="com.example")
    profile_path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    settings = Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=profile_path,
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
    )
    app = create_app(settings)
    manager = app.state.manager
    # 全部动作都被 omit（桌面图标启动点击 + 失败动作）⇒ included_action_count == 0。
    snapshot = ScreenSnapshot(
        snapshot_id="desktop",
        run_id="run-unrunnable-api",
        image_path=tmp_path / "desktop.png",
        image_sha256="abc",
        width=100,
        height=200,
        elements=[UIElement(element_id="target-icon", type="AppIcon", clickable=True)],
    )
    trace = RunTrace(
        run_id="run-unrunnable-api",
        target_app_id=profile.target_app_id,
        task="failed",
        device_id="fake-device",
        state=RunState.FAILED_ACTION,
        error="failed source step",
        snapshots=[snapshot],
        actions=[
            ActionResult(
                step_id="desktop-launch",
                tool=ToolName.CLICK_ELEMENT,
                success=True,
                params={"target": "target-icon"},
                before_snapshot_id="desktop",
            ),
            ActionResult(step_id="failed", tool=ToolName.BACK, success=False, error="device disconnected"),
        ],
    )
    trace.generated = HypiumGenerator(manager.artifacts).generate(trace, profile)
    manager.repository.save_trace(trace)
    with TestClient(app) as client:
        response = client.post("/api/runs/run-unrunnable-api/execute")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["message"] == "script is not runnable"
        assert detail["runnable_blockers"] == ["script has no replayable action"]


def test_failed_trace_is_still_runnable_and_keeps_low_confidence(tmp_path: Path):
    """失败轨迹只要有 ≥1 个可回放动作 + 真实身份就能执行，质量顾虑只体现在 confidence。"""
    profile_path = tmp_path / "profile.json"
    profile = TargetAppProfile(target_app_id="zhihu-plus", display_name="知乎++", bundle_name="com.example")
    profile_path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    settings = Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=profile_path,
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
    )
    app = create_app(settings)
    manager = app.state.manager
    trace = RunTrace(
        run_id="run-failed-but-runnable",
        target_app_id=profile.target_app_id,
        task="失败但可执行",
        device_id="fake-device",
        state=RunState.FAILED_ACTION,
        agent_outcome="failed",
        error="failed source step",
        actions=[
            ActionResult(
                step_id="click",
                tool=ToolName.CLICK_ELEMENT,
                success=True,
                params={"target": "搜索"},
                locator=LocatorCandidate(kind=LocatorKind.KEY, value="search_input"),
            ),
            ActionResult(step_id="failed", tool=ToolName.BACK, success=False, error="device disconnected"),
        ],
    )
    trace.generated = HypiumGenerator(manager.artifacts).generate(trace, profile)
    manager.repository.save_trace(trace)
    assert trace.generated is not None

    with TestClient(app) as client:
        detail = client.get("/api/runs/run-failed-but-runnable/script").json()
        assert detail["purpose"] == "acceptance"
        assert detail["acceptance_replay_enabled"] is True
        assert detail["confidence"] == "low"
        assert "source trace contains failed actions" in detail["confidence_factors"]
        assert detail["runnable_blockers"] == []

        accepted = client.post("/api/runs/run-failed-but-runnable/execute?attempts=1")
        assert accepted.status_code == 202
