import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from harmony_test_agent.api import create_app
from harmony_test_agent.config import Settings
from harmony_test_agent.generation import HypiumGenerator
from harmony_test_agent.models import (
    ActionResult,
    EventType,
    PageGraph,
    PageNode,
    RunEvent,
    RunState,
    RunTrace,
    ScreenSnapshot,
    TargetAppProfile,
    ToolName,
    UIElement,
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


def test_execute_rejects_diagnostic_script_with_incomplete_reasons(tmp_path: Path):
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
        run_id="run-diagnostic-api",
        target_app_id=profile.target_app_id,
        task="failed",
        device_id="fake-device",
        state=RunState.FAILED_ACTION,
        error="failed source step",
    )
    trace.generated = HypiumGenerator(manager.artifacts).generate(trace, profile)
    manager.repository.save_trace(trace)
    with TestClient(app) as client:
        response = client.post("/api/runs/run-diagnostic-api/execute")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["incomplete_reasons"]
