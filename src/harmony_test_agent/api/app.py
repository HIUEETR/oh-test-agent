from __future__ import annotations

import asyncio
import importlib.metadata
import json
import uuid

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from ..agents import AgentOrchestrator
from ..config import Settings, get_settings
from ..devices import DeviceError, HarmonyDeviceAdapter
from ..generation import HypiumGenerator
from ..models import TERMINAL_STATES, RunRequest, RunState, utc_now
from ..reporting import ReportBuilder
from ..runner import HypiumRunner
from ..storage import ArtifactStore, RunRepository


class RunManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.repository = RunRepository(settings.resolved_database_path)
        self.artifacts = ArtifactStore(settings.resolved_runtime_dir)
        self.tasks: dict[str, asyncio.Task] = {}
        self.orchestrators: dict[str, AgentOrchestrator] = {}

    def start(self, request: RunRequest) -> str:
        run_id = f"run-{utc_now():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        orchestrator = AgentOrchestrator(
            self.settings,
            repository=self.repository,
            artifacts=self.artifacts,
        )
        self.orchestrators[run_id] = orchestrator
        task = asyncio.create_task(orchestrator.run(request, run_id=run_id), name=run_id)
        self.tasks[run_id] = task

        def cleanup(_: asyncio.Task) -> None:
            self.orchestrators.pop(run_id, None)
            self.tasks.pop(run_id, None)

        task.add_done_callback(cleanup)
        return run_id

    def stop(self, run_id: str) -> bool:
        orchestrator = self.orchestrators.get(run_id)
        if orchestrator:
            orchestrator.request_stop(run_id)
        return bool(orchestrator) or self.repository.mark_stopped(run_id)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    manager = RunManager(settings)
    app = FastAPI(title="OpenHarmony Multimodal Test Agent", version="0.1.0")
    app.state.manager = manager
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:15173",
            "http://localhost:15173",
        ],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    async def health():
        device_data: dict[str, object]
        try:
            device = HarmonyDeviceAdapter(settings.harmony_device, settings.hdc_path, settings.agent_action_timeout)
            device_data = await asyncio.to_thread(device.health_check)
            device.close()
        except DeviceError as exc:
            device_data = {"connected": False, "id": settings.harmony_device, "error": str(exc)}
        try:
            hypium_version = importlib.metadata.version("hypium")
            hypium_importable = True
        except importlib.metadata.PackageNotFoundError:
            hypium_version = None
            hypium_importable = False
        return {
            "status": "ok",
            "model": {
                "configured": settings.model_configured,
                "vision_configured": settings.vision_model_configured,
                "provider": settings.agent_provider,
            },
            "device": device_data,
            "hypium": {"importable": hypium_importable, "version": hypium_version},
        }

    @app.get("/api/devices")
    async def devices():
        try:
            device = HarmonyDeviceAdapter(settings.harmony_device, settings.hdc_path, settings.agent_action_timeout)
            result = await asyncio.to_thread(device.health_check)
            device.close()
            return [result] if result.get("connected") else []
        except DeviceError:
            return []

    @app.get("/api/runs")
    async def list_runs(limit: int = Query(default=50, ge=1, le=200)):
        return manager.repository.list_runs(limit)

    @app.post("/api/runs", status_code=status.HTTP_202_ACCEPTED)
    async def create_run(request: RunRequest):
        return {"run_id": manager.start(request), "state": RunState.CREATED}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        trace = manager.repository.get_trace(run_id)
        if not trace:
            task = manager.tasks.get(run_id)
            if task and not task.done():
                return {"run_id": run_id, "state": RunState.CREATED}
            raise HTTPException(status_code=404, detail="run not found")
        return trace

    @app.post("/api/runs/{run_id}/stop")
    async def stop_run(run_id: str):
        if not manager.stop(run_id):
            raise HTTPException(status_code=404, detail="run not found")
        return {"run_id": run_id, "state": RunState.STOPPED_BY_USER}

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, after: int = Query(default=0, ge=0)):
        async def stream():
            cursor = after
            while True:
                found = manager.repository.get_events(run_id, cursor)
                for event in found:
                    cursor = event.event_id
                    yield (f"id: {event.event_id}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n")
                trace = manager.repository.get_trace(run_id)
                if trace and trace.state in TERMINAL_STATES and not manager.repository.get_events(run_id, cursor):
                    break
                if not trace and run_id not in manager.tasks:
                    yield f"event: error\ndata: {json.dumps({'detail': 'run not found'})}\n\n"
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/runs/{run_id}/graph")
    async def graph(run_id: str):
        trace = _trace_or_404(manager, run_id)
        return trace.graph

    @app.get("/api/runs/{run_id}/script")
    async def script(run_id: str):
        trace = _trace_or_404(manager, run_id)
        if not trace.generated:
            raise HTTPException(status_code=404, detail="script has not been generated")
        return {
            "python_path": trace.generated.python_path,
            "config_path": trace.generated.config_path,
            "python": trace.generated.python_path.read_text(encoding="utf-8"),
            "config": json.loads(trace.generated.config_path.read_text(encoding="utf-8")),
            "warnings": trace.generated.warnings,
        }

    @app.post("/api/runs/{run_id}/generate")
    async def generate(run_id: str):
        trace = _trace_or_404(manager, run_id)
        profile = manager.artifacts.load_profile(settings.resolved_target_profile_path)
        trace.generated = await asyncio.to_thread(HypiumGenerator(manager.artifacts).generate, trace, profile)
        manager.repository.save_trace(trace)
        manager.artifacts.save_trace(trace)
        ReportBuilder(manager.artifacts).build(trace)
        return trace.generated

    @app.post("/api/runs/{run_id}/execute")
    async def execute(run_id: str, attempts: int = Query(default=3, ge=1, le=3)):
        trace = _trace_or_404(manager, run_id)
        if not trace.generated:
            raise HTTPException(status_code=409, detail="generate the Hypium script first")
        trace.state = RunState.SCRIPT_EXECUTING
        runner = HypiumRunner(settings.resolved_runtime_home)
        trace.replays = await asyncio.to_thread(runner.execute_repeated, trace.generated, attempts)
        trace.state = RunState.COMPLETED if all(item.passed for item in trace.replays) else RunState.FAILED_SCRIPT
        manager.repository.save_trace(trace)
        manager.artifacts.save_trace(trace)
        ReportBuilder(manager.artifacts).build(trace)
        return {"passed": all(item.passed for item in trace.replays), "replays": trace.replays}

    @app.get("/api/runs/{run_id}/report")
    async def report(run_id: str, download: bool = False):
        trace = _trace_or_404(manager, run_id)
        report_path = manager.artifacts.run_dir(run_id) / "reports" / "report.html"
        if not report_path.exists():
            ReportBuilder(manager.artifacts).build(trace)
        filename = f"{run_id}-report.html" if download else None
        return FileResponse(report_path, media_type="text/html", filename=filename)

    @app.get("/api/runs/{run_id}/artifacts/{artifact_path:path}")
    async def artifact(run_id: str, artifact_path: str):
        run_dir = manager.artifacts.run_dir(run_id).resolve()
        requested = (run_dir / artifact_path).resolve()
        if not requested.is_relative_to(run_dir) or not requested.is_file():
            raise HTTPException(status_code=404, detail="artifact not found")
        return FileResponse(requested)

    return app


def _trace_or_404(manager: RunManager, run_id: str):
    trace = manager.repository.get_trace(run_id)
    if not trace:
        raise HTTPException(status_code=404, detail="run not found")
    return trace


app = create_app()
