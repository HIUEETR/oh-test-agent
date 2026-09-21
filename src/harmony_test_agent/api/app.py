"""构建测试代理的 FastAPI 接口，并管理目标、Profile、后台运行任务及产物访问。"""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import inspect
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import ValidationError

from ..agents import AgentOrchestrator
from ..config import Settings, get_settings
from ..devices import DeviceError, HarmonyDeviceAdapter
from ..generation import HypiumGenerator
from ..models import (
    TERMINAL_STATES,
    CommandResult,
    GeneratedArtifact,
    ProfileStatus,
    ReplayError,
    ReplayResult,
    RunRequest,
    RunState,
    TargetAppProfile,
    TargetQuery,
    utc_now,
)
from ..profiles import (
    MAX_REPLAY_EVIDENCE,
    ProfileLockedError,
    ProfileNotFoundError,
    ProfileRegistryError,
    ProfileTransitionError,
)
from ..reporting import ReportBuilder
from ..runner import HypiumRunner, XDeviceRunner
from ..storage import ArtifactStore, RunRepository, ScriptCatalog
from ..targets import TargetAmbiguousError, TargetNotFoundError, TargetResolver

_SMOKE_TASK = "启动应用，探索可达页面，验证返回和重启恢复。"


class RunManager:
    """持有 API 共享依赖，并管理运行任务、目标选择和可选领域服务。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.repository = RunRepository(settings.resolved_database_path)
        self.artifacts = ArtifactStore(settings.resolved_runtime_dir)
        self.tasks: dict[str, asyncio.Task] = {}
        self.replay_tasks: dict[str, asyncio.Task] = {}
        self.replay_locks: dict[str, asyncio.Lock] = {}
        # 手动追加回放（POST /api/profiles/{id}/replay）的 per-Profile 锁：
        # 避免同一 Profile 并发追加导致回放证据乱序或重复。
        self.profile_replay_locks: dict[str, asyncio.Lock] = {}
        self.orchestrators: dict[str, AgentOrchestrator] = {}
        self.profile_registry = _build_optional_service(
            (
                ("harmony_test_agent.targets.profiles", "ProfileRegistry"),
                ("harmony_test_agent.profiles.registry", "ProfileRegistry"),
            ),
            root=_profile_dir(settings),
            settings=settings,
            artifacts=self.artifacts,
            repository=self.repository,
            profile_dir=_profile_dir(settings),
            # 门禁参数必须跟随本实例的 Settings，而不是进程级 get_settings() 缓存：
            # 否则 API 与编排器会用不同的轮次/次数判定同一份 Profile。
            promotion_replay_attempts=settings.hypium_replay_attempts,
            min_evidence_rounds=settings.profile_verification_rounds,
        )
        self.target_resolver = None
        # 可复用用例库：与 RunRepository 共用同一个 agent.db，产物落 artifacts/cases/。
        from ..cases.library import CaseLibrary
        from ..storage.case_repository import CaseRepository

        self.case_repository = CaseRepository(settings.resolved_database_path)
        self.case_library = CaseLibrary(
            self.case_repository,
            settings.resolved_cases_dir,
            min_observed_rounds=settings.profile_verification_rounds,
            runner_factory=lambda root, timeout: HypiumRunner(root, timeout),
            xdevice_runner_factory=lambda root, timeout: XDeviceRunner(root, timeout),
            analyzer=_execution_analyzer(settings),
        )

    def start(self, request: RunRequest) -> str:
        """创建运行标识并在当前事件循环中启动后台测试任务。"""
        run_id = f"run-{utc_now():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        orchestrator = AgentOrchestrator(
            self.settings,
            repository=self.repository,
            artifacts=self.artifacts,
            case_library=self.case_library,
            analyzer=_execution_analyzer(self.settings),
        )
        self.orchestrators[run_id] = orchestrator
        task = asyncio.create_task(orchestrator.run(request, run_id=run_id), name=run_id)
        self.tasks[run_id] = task

        def cleanup(_: asyncio.Task) -> None:
            self.orchestrators.pop(run_id, None)
            self.tasks.pop(run_id, None)

        task.add_done_callback(cleanup)
        return run_id

    async def select_target(self, run_id: str, selection: dict[str, Any]) -> Any:
        """Validate one candidate selection before resuming a waiting Run."""
        orchestrator = self.orchestrators.get(run_id)
        trace = self.repository.get_trace(run_id)
        if not orchestrator or not trace or trace.state != RunState.WAITING_TARGET_SELECTION:
            raise HTTPException(status_code=409, detail="run is not waiting for target selection")
        candidate_id = selection.get("candidate_id")
        bundle_name = selection.get("bundle_name")
        matches = [
            item
            for item in trace.target_candidates
            if (candidate_id and item.get("candidate_id") == candidate_id)
            or (bundle_name and item.get("bundle_name") == bundle_name)
        ]
        if len(matches) != 1:
            raise HTTPException(status_code=422, detail="selection is not in the run candidate set")
        selected_bundle = matches[0].get("bundle_name")
        if not selected_bundle:
            raise HTTPException(status_code=422, detail="selected candidate has no bundle_name")
        try:
            return await orchestrator.select_target(run_id, bundle_name=str(selected_bundle))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def stop(self, run_id: str) -> bool:
        """请求运行在下一个安全检查点停止，并标记已持久化的非活动运行。"""
        orchestrator = self.orchestrators.get(run_id)
        if orchestrator:
            orchestrator.request_stop(run_id)
        return bool(orchestrator) or self.repository.mark_stopped(run_id)

    async def shutdown(self, timeout: float = 8.0) -> None:
        """Request cooperative stops and bound how long shutdown waits for cleanup."""
        tasks = [*self.tasks.values(), *self.replay_tasks.values()]
        for run_id, orchestrator in list(self.orchestrators.items()):
            orchestrator.request_stop(run_id)
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if not task.cancelled():
                task.exception()

    def replay_running(self, run_id: str) -> bool:
        """Return whether this process is already replaying the Run."""
        task = self.replay_tasks.get(run_id)
        lock = self.replay_locks.get(run_id)
        return bool((task and not task.done()) or (lock and lock.locked()))

    def recover_stale_replay(self, trace) -> None:
        """Convert an unowned persisted pending replay into a retryable terminal state."""
        if trace.replay_status != "pending" or self.replay_running(trace.run_id):
            return
        trace.replay_status = "failed"
        trace.replay_completed = len(trace.replays)
        trace.replay_passed = sum(item.passed for item in trace.replays)
        self._save_trace_and_report(trace)

    def start_replay(self, run_id: str, attempts: int) -> None:
        """Persist the queued state and execute attempts in a background task."""
        trace = self.repository.get_trace(run_id)
        if trace is None or trace.generated is None:
            raise ValueError("generated artifact is missing")
        lock = self.replay_locks.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            raise RuntimeError("Hypium replay is already running")
        trace.replays = []
        trace.replay_status = "pending"
        trace.replay_total = attempts
        trace.replay_completed = 0
        trace.replay_passed = 0
        self._save_trace_and_report(trace)
        task = asyncio.create_task(self._run_replay(run_id), name=f"replay-{run_id}")
        self.replay_tasks[run_id] = task

        def cleanup(completed: asyncio.Task) -> None:
            self.replay_tasks.pop(run_id, None)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(cleanup)

    async def _run_replay(self, run_id: str) -> None:
        """Execute and persist every replay attempt independently."""
        trace = self.repository.get_trace(run_id)
        if trace is None or trace.generated is None:
            return
        runner = HypiumRunner(self.settings.resolved_runtime_home)
        lock = self.replay_locks.setdefault(run_id, asyncio.Lock())
        try:
            await lock.acquire()
            for attempt in range(1, trace.replay_total + 1):
                result = await asyncio.to_thread(runner.execute, trace.generated, attempt)
                trace.replays.append(result)
                trace.replay_completed = len(trace.replays)
                trace.replay_passed = sum(item.passed for item in trace.replays)
                trace.replay_status = "pending"
                self._save_trace_and_report(trace)
            if trace.replay_passed == trace.replay_total:
                trace.replay_status = "passed"
            elif trace.replay_passed:
                trace.replay_status = "partial"
            else:
                trace.replay_status = "failed"
        except asyncio.CancelledError:
            trace.replay_status = "failed"
            raise
        except Exception as exc:
            trace.replay_status = "failed"
            trace.replays.append(_unexpected_replay_result(trace.replay_completed + 1, exc))
            trace.replay_completed = len(trace.replays)
            trace.replay_passed = sum(item.passed for item in trace.replays)
        finally:
            self._save_trace_and_report(trace)
            if lock.locked():
                lock.release()

    def _save_trace_and_report(self, trace) -> None:
        self.repository.save_trace(trace)
        self.artifacts.save_trace(trace)
        ReportBuilder(self.artifacts).build(trace)


def create_app(settings: Settings | None = None) -> FastAPI:
    """使用给定配置创建并装配 FastAPI 应用。"""
    settings = settings or get_settings()
    manager = RunManager(settings)

    # 直流模式（DC Mode）：与 Live Mode 完全隔离的会话管理器
    from ..dc import DcSessionManager, create_dc_router

    dc_manager = DcSessionManager(settings, manager.artifacts)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        dc_manager.start_reaper()
        yield
        await manager.shutdown()
        await dc_manager.shutdown()

    app = FastAPI(title="OpenHarmony Multimodal Test Agent", version="0.2.0", lifespan=lifespan)
    app.state.manager = manager
    app.state.dc_manager = dc_manager
    app.include_router(create_dc_router(settings, dc_manager))
    from .cases import create_cases_router

    app.include_router(
        create_cases_router(
            settings=settings,
            library=manager.case_library,
            repository=manager.case_repository,
            run_repository=manager.repository,
            registry=manager.profile_registry,
            dc_manager=dc_manager,
        )
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.harmony_cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health/live")
    async def live_health():
        return {"status": "ok"}

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

    @app.get("/api/targets")
    async def targets(device_id: str | None = None):
        """列出设备中的已安装应用，供应用名消歧和手动选择。"""
        if manager.target_resolver:
            return await _invoke_optional(
                manager.target_resolver,
                ("list_targets", "list_installed", "list_installed_apps"),
                device_id=device_id,
            )
        try:
            return await _list_targets_from_device(settings, device_id)
        except DeviceError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/targets/resolve")
    async def resolve_target(payload: Annotated[dict[str, Any], Body()]):
        """按应用名或 bundleName 解析唯一目标；多匹配时返回候选而不猜测。"""
        query_data = payload.get("target", payload)
        if not isinstance(query_data, dict):
            raise HTTPException(status_code=422, detail="target must be an object")
        try:
            query = TargetQuery.model_validate(query_data)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        try:
            device = HarmonyDeviceAdapter(
                payload.get("device_id") or settings.harmony_device,
                settings.hdc_path,
                settings.agent_action_timeout,
            )
            try:
                resolved = await asyncio.to_thread(TargetResolver(device).resolve, query)
            finally:
                device.close()
        except TargetAmbiguousError as exc:
            return {
                "status": "selection_required",
                "candidates": [item.model_dump(mode="json") for item in exc.candidates],
            }
        except TargetNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (DeviceError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            "status": "resolved",
            "target": resolved.model_dump(mode="json"),
            "candidates": [],
        }

    @app.get("/api/profiles")
    async def list_profiles():
        """列出 verified、candidate、draft 与无效 Profile 摘要。"""
        if manager.profile_registry:
            profiles = await _invoke_optional(manager.profile_registry, ("list", "list_profiles", "all"))
            return [_profile_summary(item, manager.profile_registry) for item in profiles]
        return _fallback_profile_list(settings)

    @app.get("/api/profiles/{profile_id}")
    async def get_profile(profile_id: str):
        target_app_id, preferred_status = _split_profile_identifier(profile_id)
        if manager.profile_registry:
            try:
                stored = await _invoke_optional(
                    manager.profile_registry,
                    ("get_any", "get", "get_profile", "load"),
                    target_app_id=target_app_id,
                    profile_id=target_app_id,
                    identifier=target_app_id,
                    preferred_status=preferred_status,
                    status=preferred_status,
                )
                if stored is not None:
                    return stored
            except ProfileNotFoundError, KeyError, FileNotFoundError:
                pass
            except (ProfileTransitionError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        profile = _fallback_profile_get(settings, profile_id) or _fallback_profile_get(settings, target_app_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="profile not found")
        return profile

    @app.post("/api/profiles/{profile_id}/verify", status_code=status.HTTP_202_ACCEPTED)
    async def verify_profile(profile_id: str, payload: Annotated[dict[str, Any] | None, Body()] = None):
        payload = payload or {}
        if not manager.profile_registry:
            raise HTTPException(status_code=501, detail="Profile verification service is unavailable")
        target_app_id, identifier_status = _split_profile_identifier(profile_id)
        preferred_status = payload.get("status", identifier_status)
        if preferred_status is not None:
            try:
                preferred_status = ProfileStatus(preferred_status)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="invalid Profile status") from exc
        try:
            profile = manager.profile_registry.get_any(target_app_id=target_app_id, preferred_status=preferred_status)
            if profile is None:
                raise ProfileNotFoundError(f"Profile not found: {target_app_id}")
        except (ProfileNotFoundError, KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ProfileTransitionError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            request = RunRequest(
                target={"bundle_name": profile.bundle_name},
                task=payload.get("task") or _SMOKE_TASK,
                device_id=payload.get("device_id"),
                bootstrap_only=True,
                auto_generate=False,
                auto_execute=False,
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"run_id": manager.start(request), "state": RunState.CREATED, "profile_id": profile_id}

    @app.post("/api/profiles/{profile_id}/replay")
    async def replay_profile(profile_id: str, payload: Annotated[dict[str, Any] | None, Body()] = None):
        """向 candidate/verified Profile 异步追加 Hypium 回放证据。

        比赛「3 次连续成功」要求：主流程内联 1 次（``HYPIUM_REPLAY_ATTEMPTS``），
        剩余 2 次由用户/CI 通过本端点在设备空闲时追加，主流程因此不被阻塞。

        candidate 累计满门禁次数且全部通过后自动晋级 verified；已 verified 的 Profile
        只追加审计证据，状态与门禁资产不变。锁定或 draft/invalid 的 Profile 一律拒绝。
        """
        payload = payload or {}
        attempts = payload.get("attempts", 1)
        if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 3:
            raise HTTPException(status_code=422, detail="attempts must be an integer between 1 and 3")
        if not manager.profile_registry:
            raise HTTPException(status_code=501, detail="Profile registry is unavailable")

        target_app_id, _ = _split_profile_identifier(profile_id)
        try:
            profile = manager.profile_registry.get_any(target_app_id=target_app_id)
        except (ProfileNotFoundError, KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ProfileTransitionError, ProfileRegistryError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if profile is None:
            raise HTTPException(status_code=404, detail=f"profile {profile_id} not found")

        script_path = profile.provenance.generated_script_path
        if not script_path or not Path(script_path).is_file():
            raise HTTPException(
                status_code=409,
                detail="no generated Hypium script recorded for this Profile; run the pipeline first",
            )

        lock = manager.profile_replay_locks.setdefault(target_app_id, asyncio.Lock())
        if lock.locked():
            raise HTTPException(status_code=409, detail="replay is already running for this Profile")

        runner = HypiumRunner(manager.settings.resolved_runtime_home)
        generated = GeneratedArtifact(
            python_path=Path(script_path),
            config_path=Path(script_path).with_suffix(".json"),
            metadata_path=Path(script_path).with_suffix(".json"),
            purpose="acceptance",
            replay_eligible=True,
        )
        results: list[dict[str, Any]] = []
        async with lock:
            for offset in range(attempts):
                before = manager.profile_registry.get_any(target_app_id=target_app_id)
                known_ids = list(before.provenance.hypium_replay_run_ids) if before is not None else []
                try:
                    replay = await asyncio.to_thread(runner.execute, generated, offset + 1)
                except (OSError, ValueError) as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                try:
                    # 证据 ID 由 registry 统一命名（{discovery_run_id}:profile-attempt-N）：
                    # 只有该命名空间内的 ID 才能被 promote 接受，因此这里不自定义 run_id。
                    updated = await asyncio.to_thread(
                        manager.profile_registry.append_replay_evidence,
                        target_app_id,
                        passed=replay.passed,
                        evidence_refs=replay.evidence_paths,
                    )
                except ProfileLockedError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                except (ProfileTransitionError, ProfileRegistryError) as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                except (ProfileNotFoundError, KeyError, FileNotFoundError) as exc:
                    raise HTTPException(status_code=404, detail=str(exc)) from exc
                recorded = [item for item in updated.provenance.hypium_replay_run_ids if item not in known_ids]
                results.append(
                    {
                        "attempt": offset + 1,
                        "run_id": recorded[-1] if recorded else None,
                        "passed": replay.passed,
                        "status": replay.status,
                        "evidence_paths": list(replay.evidence_paths),
                        "profile_status": updated.status.value,
                        "total_replays": len(updated.provenance.hypium_replay_run_ids),
                    }
                )
                if not replay.passed:
                    break

        final = manager.profile_registry.get_any(target_app_id=target_app_id)
        return {
            "profile_id": target_app_id,
            "results": results,
            "status": final.status.value if final is not None else None,
            "total_replays": len(final.provenance.hypium_replay_run_ids) if final is not None else 0,
            "max_replays": MAX_REPLAY_EVIDENCE,
        }

    @app.post("/api/profiles/{profile_id}/lock")
    async def lock_profile(profile_id: str, payload: Annotated[dict[str, Any] | None, Body()] = None):
        payload = payload or {}
        if "locked" in payload and not isinstance(payload["locked"], bool):
            raise HTTPException(status_code=422, detail="locked must be a boolean")
        locked = payload.get("locked", True)
        target_app_id, _ = _split_profile_identifier(profile_id)
        if manager.profile_registry:
            try:
                return await _invoke_optional(
                    manager.profile_registry,
                    ("set_locked", "lock", "lock_profile"),
                    target_app_id=target_app_id,
                    profile_id=target_app_id,
                    identifier=target_app_id,
                    locked=locked,
                )
            except (ProfileNotFoundError, KeyError, FileNotFoundError) as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ProfileLockedError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except (ProfileTransitionError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _fallback_profile_lock(settings, target_app_id, locked)

    @app.post("/api/profiles/{profile_id}/rollback")
    async def rollback_profile(profile_id: str, payload: Annotated[dict[str, Any] | None, Body()] = None):
        payload = payload or {}
        if not manager.profile_registry:
            raise HTTPException(status_code=501, detail="Profile registry is unavailable")
        backup_path = payload.get("backup_path")
        backup_name = payload.get("backup_name")
        if backup_path is not None and not isinstance(backup_path, str):
            raise HTTPException(status_code=422, detail="backup_path must be a string")
        if backup_name is not None and not isinstance(backup_name, str):
            raise HTTPException(status_code=422, detail="backup_name must be a string")
        if backup_path and backup_name:
            raise HTTPException(status_code=422, detail="provide only one backup selector")
        target_app_id, _ = _split_profile_identifier(profile_id)
        try:
            path = await asyncio.to_thread(
                manager.profile_registry.rollback,
                target_app_id,
                Path(backup_path) if backup_path else None,
                backup_name=backup_name,
            )
        except (ProfileNotFoundError, KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProfileLockedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ProfileTransitionError, ProfileRegistryError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"profile_id": profile_id, "path": path, "status": "verified"}

    @app.post("/api/profiles/{profile_id}/invalidate")
    async def invalidate_profile(profile_id: str, payload: Annotated[dict[str, Any], Body()]):
        if not manager.profile_registry:
            raise HTTPException(status_code=501, detail="Profile registry is unavailable")
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise HTTPException(status_code=422, detail="a non-empty invalidation reason is required")
        target_app_id, _ = _split_profile_identifier(profile_id)
        try:
            current = manager.profile_registry.get(target_app_id=target_app_id, status=ProfileStatus.VERIFIED)
            if current is None:
                raise ProfileNotFoundError(f"verified Profile not found: {target_app_id}")
            if current.locked:
                raise ProfileLockedError(f"verified Profile is locked: {target_app_id}")
            path = await asyncio.to_thread(manager.profile_registry.invalidate, target_app_id, reason.strip())
        except (ProfileNotFoundError, KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProfileLockedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ProfileTransitionError, ProfileRegistryError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"profile_id": profile_id, "path": path, "status": "invalid"}

    @app.get("/api/runs")
    async def list_runs(limit: int = Query(default=50, ge=1, le=200)):
        return manager.repository.list_runs(limit)

    @app.post("/api/runs", status_code=status.HTTP_202_ACCEPTED)
    async def create_run(payload: Annotated[dict[str, Any], Body()]):
        """接受新目标契约并继续兼容历史 target_app_id 请求。"""
        try:
            request = _build_run_request(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
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

    @app.post("/api/runs/{run_id}/target-selection")
    async def target_selection(run_id: str, selection: Annotated[dict[str, Any], Body()]):
        if not (selection.get("candidate_id") or selection.get("bundle_name")):
            raise HTTPException(status_code=422, detail="candidate_id or bundle_name is required")
        result = await manager.select_target(run_id, selection)
        return result if result is not None else {"run_id": run_id, "accepted": True}

    @app.get("/api/runs/{run_id}/discovery")
    async def discovery(run_id: str):
        """返回探索阶段、边界用量、候选资产和门禁结果。"""
        trace = _trace_or_404(manager, run_id)
        data = trace.model_dump(mode="json")
        discovery = data.get("discovery_result") or {}
        verification = data.get("verification_result") or {}
        return {
            **discovery,
            "phase": data.get("phase", "bootstrap"),
            "profile_status": (data.get("profile_snapshot") or {}).get("status") or data.get("profile_status_at_start"),
            "resolved_target": data.get("resolved_target"),
            "target_candidates": data.get("target_candidates", []),
            "pages_discovered": len(discovery.get("pages", [])),
            "actions_executed": len(discovery.get("transitions", [])),
            "blocked_paths": discovery.get("blocked_actions", []),
            "validation_rounds": verification.get("rounds", []),
            "replays": data.get("profile_validation_replays", []),
            "provisional": data.get("provisional", False),
            "live_mode": data.get("live_mode", False),
        }

    @app.post("/api/runs/{run_id}/stop")
    async def stop_run(run_id: str):
        if not manager.stop(run_id):
            raise HTTPException(status_code=404, detail="run not found")
        return {"run_id": run_id, "state": RunState.STOPPED_BY_USER}

    @app.get("/api/runs/{run_id}/events")
    async def events(
        run_id: str,
        after: int = Query(default=0, ge=0),
        last_event_id: int | None = Header(default=None, alias="Last-Event-ID"),
    ):
        if manager.repository.get_trace(run_id) is None and run_id not in manager.tasks:
            raise HTTPException(status_code=404, detail="run not found")

        async def stream():
            cursor = max(after, last_event_id or 0)
            while True:
                emitted = manager.repository.get_events(run_id, cursor)
                for event in emitted:
                    cursor = event.event_id
                    yield f"id: {event.event_id}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"
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
        return _trace_or_404(manager, run_id).graph

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
            "generated_at": trace.generated.generated_at,
            "purpose": trace.generated.purpose,
            "diagnostic": trace.generated.purpose == "diagnostic",
            "acceptance_replay_enabled": trace.generated.replay_eligible,
            "source_agent_outcome": trace.generated.source_agent_outcome,
            "source_action_count": trace.generated.source_action_count,
            "included_action_count": trace.generated.included_action_count,
            "incomplete_reasons": trace.generated.incomplete_reasons,
        }

    @app.post("/api/runs/{run_id}/generate")
    async def generate(run_id: str):
        """仅使用不可变 RunTrace 重新生成，不读取当前磁盘 Profile。"""
        trace = _trace_or_404(manager, run_id)
        trace.generated = await asyncio.to_thread(
            HypiumGenerator(
                manager.artifacts, min_observed_rounds=manager.settings.profile_verification_rounds
            ).generate,
            trace,
        )
        manager.repository.save_trace(trace)
        manager.artifacts.save_trace(trace)
        ReportBuilder(manager.artifacts).build(trace)
        return trace.generated

    @app.post("/api/runs/{run_id}/execute", status_code=status.HTTP_202_ACCEPTED)
    async def execute(run_id: str, attempts: int = Query(default=3, ge=1, le=3)):
        """Queue eligible Hypium replays and return before device execution starts."""
        trace = _trace_or_404(manager, run_id)
        if not trace.generated:
            raise HTTPException(status_code=409, detail="generate the Hypium script first")
        if trace.provisional or trace.live_mode:
            raise HTTPException(
                status_code=409,
                detail="provisional or live-mode runs cannot execute formal Hypium regression cases",
            )
        if manager.replay_running(run_id):
            raise HTTPException(status_code=409, detail="Hypium replay is already running")
        if trace.replay_status == "pending":
            manager.recover_stale_replay(trace)
        if not trace.generated.replay_eligible:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "diagnostic script is not eligible for acceptance replay",
                    "incomplete_reasons": trace.generated.incomplete_reasons,
                },
            )
        try:
            manager.start_replay(run_id, attempts)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"run_id": run_id, "status": "pending", "attempts": attempts}

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

    # ------------------------------------------------------------------
    # 统一脚本目录（Live 运行 + 直流会话）
    # ------------------------------------------------------------------

    @app.get("/api/scripts")
    async def list_scripts():
        """列出全部已生成的 Hypium 脚本（最新在前）。"""
        return ScriptCatalog(manager.artifacts.runtime_dir).list_entries()

    @app.get("/api/scripts/{script_id:path}")
    async def get_script_detail(script_id: str):
        """返回单个脚本的源码、config 与目录项。"""
        catalog = ScriptCatalog(manager.artifacts.runtime_dir)
        try:
            entry, python_text, config = catalog.read(script_id)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"entry": entry, "python": python_text, "config": config}

    return app


def _build_run_request(payload: dict[str, Any]) -> RunRequest:
    data = dict(payload)
    data.setdefault("task", _SMOKE_TASK)
    target = data.get("target") if isinstance(data.get("target"), dict) else {}
    fields = RunRequest.model_fields
    if "target" in fields:
        data["target"] = target
    elif "target_app_id" in fields and not data.get("target_app_id"):
        data["target_app_id"] = target.get("target_app_id") or target.get("bundle_name") or "zhihu-plus"
    discovery = data.pop("discovery", None)
    if discovery is not None:
        if not isinstance(discovery, dict):
            raise ValueError("discovery must be an object")
        policy = dict(discovery)
        data["temporary_test"] = bool(policy.pop("temporary_test", data.get("temporary_test", False)))
        data["exploration_policy"] = policy
    return RunRequest.model_validate(data)


def _execution_analyzer(settings: Settings) -> Any | None:
    """构造执行结果分析器；关闭开关或依赖缺失时返回 ``None``（功能降级，不阻塞）。"""
    if not settings.case_analysis_enabled:
        return None
    try:
        from ..analysis.service import ExecutionAnalyzer

        return ExecutionAnalyzer(
            device_factory=lambda device_id: HarmonyDeviceAdapter(
                device_id, settings.hdc_path, settings.agent_action_timeout
            )
        )
    except Exception:  # noqa: BLE001 - 分析能力缺失只降级为「不分析」
        return None


def _profile_dir(settings: Settings) -> Path:
    configured = getattr(settings, "resolved_profiles_dir", None) or getattr(settings, "profiles_dir", None)
    if configured:
        return Path(configured)
    return settings.resolved_target_profile_path.parent


def _build_optional_service(candidates: tuple[tuple[str, str], ...], **dependencies: Any) -> Any | None:
    for module_name, symbol_name in candidates:
        try:
            symbol = getattr(importlib.import_module(module_name), symbol_name)
        except ImportError, AttributeError:
            continue
        try:
            return _call_filtered(symbol, dependencies)
        except TypeError:
            continue
    return None


def _call_filtered(callable_object: Any, values: dict[str, Any]) -> Any:
    signature = inspect.signature(callable_object)
    accepts_kwargs = any(parameter.kind == parameter.VAR_KEYWORD for parameter in signature.parameters.values())
    arguments = (
        values if accepts_kwargs else {key: value for key, value in values.items() if key in signature.parameters}
    )
    return callable_object(**arguments)


async def _invoke_optional(service: Any, names: tuple[str, ...], **values: Any) -> Any:
    for name in names:
        method = getattr(service, name, None)
        if not callable(method):
            continue
        if inspect.iscoroutinefunction(method):
            return await _call_filtered(method, values)
        result = await asyncio.to_thread(_call_filtered, method, values)
        if inspect.isawaitable(result):
            return await result
        return result
    raise HTTPException(status_code=501, detail=f"service does not implement any of: {', '.join(names)}")


async def _list_targets_from_device(settings: Settings, device_id: str | None) -> Any:
    device = HarmonyDeviceAdapter(
        device_id or settings.harmony_device, settings.hdc_path, settings.agent_action_timeout
    )
    try:
        return await _invoke_optional(device, ("list_installed_apps", "list_applications", "installed_apps"))
    finally:
        device.close()


def _resolve_from_candidates(query: dict[str, Any], candidates: Any) -> dict[str, Any]:
    items = candidates if isinstance(candidates, list) else []
    bundle_name = str(query.get("bundle_name") or "").casefold()
    app_name = str(query.get("app_name") or "").casefold()
    matches = []
    for item in items:
        data = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
        bundle = str(data.get("bundle_name") or data.get("bundleName") or "")
        name = str(data.get("display_name") or data.get("app_name") or data.get("label") or "")
        if (bundle_name and bundle.casefold() == bundle_name) or (
            not bundle_name and app_name and name.casefold() == app_name
        ):
            matches.append(data)
    if len(matches) == 1:
        return {"status": "resolved", "target": matches[0], "candidates": matches}
    if len(matches) > 1:
        return {"status": "selection_required", "candidates": matches}
    return {"status": "not_found", "candidates": []}


def _fallback_profile_list(settings: Settings) -> list[dict[str, Any]]:
    profiles = []
    for path in sorted(_profile_dir(settings).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            data.setdefault("profile_id", data.get("target_app_id", path.stem))
            data.setdefault("status", "verified" if data.get("schema_version", 1) == 1 else "draft")
            data.setdefault("locked", False)
            profiles.append(data)
        except (OSError, json.JSONDecodeError) as exc:
            profiles.append({"profile_id": path.stem, "status": "invalid", "error": str(exc)})
    return profiles


def _profile_summary(profile: Any, registry: Any | None = None) -> dict[str, Any]:
    """Flatten the stable fields consumed by the Profile management UI."""
    data = profile.model_dump(mode="json") if hasattr(profile, "model_dump") else dict(profile)
    target_id = data.get("target_app_id")
    profile_status = data.get("status")
    profile_id = target_id if profile_status == ProfileStatus.VERIFIED else f"{profile_status}:{target_id}"
    version = data.get("app_version") or {}
    provenance = data.get("provenance") or {}
    evidence = provenance.get("evidence") or {}
    history = []
    if registry is not None and data.get("bundle_name"):
        history_dir = registry.history_dir / registry._safe_component(data["bundle_name"])
        history = []
        for path in sorted(history_dir.glob("*.json")):
            try:
                json.loads(path.read_text(encoding="utf-8-sig"))
            except OSError, json.JSONDecodeError:
                continue
            history.append({"backup_name": str(path.relative_to(registry.history_dir)).replace("\\", "/")})
    return {
        "profile_id": profile_id,
        "target_app_id": target_id,
        "display_name": data.get("display_name"),
        "bundle_name": data.get("bundle_name"),
        "main_ability": data.get("main_ability"),
        "version_name": version.get("version_name"),
        "version_code": version.get("version_code"),
        "status": data.get("status"),
        "locked": bool(data.get("locked", False)),
        "quick_verification": evidence.get("quick_verification"),
        "history": history,
        # 比赛「3 次连续成功」进度：主流程内联 1 次 + 异步追加 2 次。
        "hypium_replay_run_ids": list(provenance.get("hypium_replay_run_ids") or []),
        "max_replays": MAX_REPLAY_EVIDENCE,
        "consecutive_replay_passes": evidence.get("consecutive_replay_passes"),
        "generated_script_path": provenance.get("generated_script_path"),
    }


def _split_profile_identifier(profile_id: str) -> tuple[str, ProfileStatus | None]:
    """Split lifecycle-qualified IDs emitted by the list endpoint."""
    status_name, separator, target_app_id = profile_id.partition(":")
    if separator and target_app_id:
        try:
            return target_app_id, ProfileStatus(status_name)
        except ValueError:
            pass
    return profile_id, None


def _fallback_profile_get(settings: Settings, profile_id: str) -> dict[str, Any] | None:
    for profile in _fallback_profile_list(settings):
        if (
            profile.get("profile_id") == profile_id
            or profile.get("target_app_id") == profile_id
            or profile.get("bundle_name") == profile_id
        ):
            return profile
    return None


def _fallback_profile_lock(settings: Settings, profile_id: str, locked: bool) -> dict[str, Any]:
    profile = _fallback_profile_get(settings, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    path = next(
        (
            candidate
            for candidate in _profile_dir(settings).glob("*.json")
            if candidate.stem == profile.get("target_app_id")
        ),
        None,
    )
    if path is None:
        raise HTTPException(status_code=404, detail="profile file not found")
    profile.pop("profile_id", None)
    profile.pop("status", None)
    profile["locked"] = locked
    TargetAppProfile.model_validate(profile)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return {"profile_id": profile_id, "locked": locked}


def _trace_or_404(manager: RunManager, run_id: str):
    trace = manager.repository.get_trace(run_id)
    if not trace:
        raise HTTPException(status_code=404, detail="run not found")
    return trace


def _unexpected_replay_result(attempt: int, exc: Exception) -> ReplayResult:
    """Convert an unexpected runner failure into a persisted structured result."""
    message = f"{type(exc).__name__}: {exc}"
    return ReplayResult(
        attempt=attempt,
        command=CommandResult(command="HypiumRunner.execute", returncode=None),
        status="failed",
        error=ReplayError(kind="process_exit", message=message),
    )
