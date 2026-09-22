"""``/api/cases`` 路由工厂：可复用用例库的读写、来源构建与重跑。

路由声明顺序有硬约束：``/api/cases/executions/{execution_id}`` 必须早于
``/api/cases/{case_id}``，否则前者会被后者的路径参数吞掉。

分析（``ExecutionAnalysis``）只是附加信息，**永不翻转** ``passed``。
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import FileResponse

from ..cases.builder import CaseBuilder
from ..cases.library import CaseLibrary
from ..cases.safety import validate_case_spec
from ..cases.spec import (
    CaseExecutionRecord,
    CaseExecutionRequest,
    CaseRecord,
    CaseSummary,
    ScenarioKind,
)
from ..cases.stress import StressRequest
from ..config import Settings
from ..models import BugReproRequest, TargetAppProfile, TargetQuery, utc_now
from ..storage.case_repository import CaseRepository
from ..storage.repository import RunRepository

__all__ = ["StressRequest", "create_cases_router"]


def create_cases_router(
    *,
    settings: Settings,
    library: CaseLibrary,
    repository: CaseRepository,
    run_repository: RunRepository,
    registry: Any | None = None,
    dc_manager: Any | None = None,
) -> APIRouter:
    """装配用例库路由。

    Args:
        settings: 进程配置（超时、干预开关、用例目录）。
        library: 用例库门面（落盘、版本、重跑）。
        repository: 用例仓库（执行记录的 pending 行由路由先落库）。
        run_repository: 读取 Live 运行 trace。
        registry: Profile Registry；用于把目标查询解析成 Profile 证据。
        dc_manager: DC 会话管理器；用于读取已持久化的会话快照。
    """
    router = APIRouter()
    execution_locks: dict[str, asyncio.Lock] = {}
    builder = CaseBuilder(min_observed_rounds=settings.profile_verification_rounds)

    # ------------------------------------------------------------------
    # 执行记录（必须早于 /{case_id}）
    # ------------------------------------------------------------------

    @router.get("/api/cases/executions/{execution_id}", response_model=CaseExecutionRecord)
    async def get_execution(execution_id: str) -> CaseExecutionRecord:
        record = library.get_execution(execution_id)
        if record is None:
            raise HTTPException(status_code=404, detail="execution not found")
        return record

    # ------------------------------------------------------------------
    # 来源构建
    # ------------------------------------------------------------------

    @router.post("/api/cases/from-run/{run_id}", response_model=CaseRecord, status_code=status.HTTP_201_CREATED)
    async def from_run(run_id: str, force: bool = Query(default=False)) -> CaseRecord:
        trace = run_repository.get_trace(run_id)
        if trace is None:
            raise HTTPException(status_code=404, detail="run not found")
        if trace.generated is None:
            raise HTTPException(status_code=409, detail="run has no generated artifact yet")
        if trace.profile_snapshot is None:
            raise HTTPException(status_code=422, detail="run has no frozen profile snapshot")
        record = await asyncio.to_thread(library.build_from_run, trace)
        if record is None:
            if not force:
                raise HTTPException(
                    status_code=422,
                    detail="run has no runnable script; retry with ?force=true to store it as a draft",
                )
            built = await asyncio.to_thread(builder.from_trace, trace, trace.profile_snapshot)
            built.spec.status = "draft"
            record = await asyncio.to_thread(library.save_built, built, device_sn=trace.device_id)
        return record

    @router.post("/api/cases/from-dc/{session_id}", response_model=CaseRecord, status_code=status.HTTP_201_CREATED)
    async def from_dc(
        session_id: str,
        body: dict[str, str] | None = None,
        force: bool = Query(default=False),
    ) -> CaseRecord:
        snapshot = _load_dc_snapshot(dc_manager, session_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="dc session snapshot not found")
        payload = body or {}
        bundle_name = payload.get("bundle_name") or ""
        main_ability = payload.get("main_ability") or ""
        if not bundle_name or not main_ability:
            bundle_name, main_ability = _session_identity(snapshot, bundle_name, main_ability)
        if not bundle_name or bundle_name == "com.example.app":
            raise HTTPException(status_code=422, detail="a real bundle_name is required to save a case")
        record = await asyncio.to_thread(library.build_from_dc, snapshot, bundle_name, main_ability)
        if record is None:
            # 只有「物理上跑不起来」（缺可回放动作 / 身份占位）才会走到这里；
            # force=true 使错误路径与 from-run 对齐：降级为 draft 强制入库。
            if not force:
                raise HTTPException(status_code=422, detail="dc session does not qualify as a reusable case")
            builder_result = await asyncio.to_thread(
                builder.from_dc_invocations,
                session_id,
                str(getattr(snapshot, "device_id", "") or ""),
                list(getattr(snapshot, "invocations", None) or []),
                bundle_name=bundle_name,
                main_ability=main_ability,
            )
            builder_result.spec.status = "draft"
            record = await asyncio.to_thread(
                library.save_built, builder_result, device_sn=str(getattr(snapshot, "device_id", "") or "")
            )
        return record

    @router.post("/api/cases/bug-repro", response_model=CaseRecord, status_code=status.HTTP_201_CREATED)
    async def bug_repro(request: BugReproRequest) -> CaseRecord:
        profile = _profile_for(registry, request.target)
        provider = _provider(settings)
        plan = await _plan_bug_repro(provider, request, profile)
        if plan is None:
            raise HTTPException(status_code=503, detail="no provider is available to plan the bug reproduction")
        try:
            built = await asyncio.to_thread(builder.from_bug_repro, plan, request, profile)
        except ValueError as exc:
            # 计划里的检查点草案无法映射为合法 IR（例如 white_screen 哨兵缺少可用锚点）。
            raise HTTPException(status_code=422, detail=f"bug repro plan is not satisfiable: {exc}") from exc
        record = await asyncio.to_thread(library.save_built, built, device_sn=request.device_id)
        if request.auto_execute:
            _schedule_execution(execution_locks, library, repository, record, CaseExecutionRequest())
        return record

    @router.post("/api/cases/stress", response_model=CaseRecord, status_code=status.HTTP_201_CREATED)
    async def stress(request: StressRequest) -> CaseRecord:
        profile = _profile_for(registry, request.target)
        base_spec = None
        if request.body == "existing_case" and request.case_id:
            existing = library.get(request.case_id)
            if existing is None:
                raise HTTPException(status_code=404, detail="base case not found")
            base_spec = existing.spec
        built = await asyncio.to_thread(builder.from_stress_request, request, profile, base_spec)
        violations = validate_case_spec(built.spec, max_iterations=settings.stress_max_iterations)
        if violations:
            raise HTTPException(status_code=422, detail={"violations": violations})
        record = await asyncio.to_thread(library.save_built, built, device_sn=request.device_id)
        if request.auto_execute:
            _schedule_execution(execution_locks, library, repository, record, CaseExecutionRequest())
        return record

    # ------------------------------------------------------------------
    # 用例读取 / 变更
    # ------------------------------------------------------------------

    @router.get("/api/cases", response_model=list[CaseSummary])
    async def list_cases(
        target_app_id: str | None = None,
        scenario: ScenarioKind | None = None,
        tag: str | None = None,
        case_status: str | None = Query(default=None, alias="status"),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> list[CaseSummary]:
        return library.list(
            target_app_id=target_app_id,
            scenario=str(scenario) if scenario else None,
            tag=tag,
            status=case_status,
            limit=limit,
        )

    @router.get("/api/cases/{case_id}/versions", response_model=list[CaseSummary])
    async def case_versions(case_id: str) -> list[CaseSummary]:
        versions = library.versions(case_id)
        if not versions:
            raise HTTPException(status_code=404, detail="case not found")
        return versions

    @router.get("/api/cases/{case_id}/executions", response_model=list[CaseExecutionRecord])
    async def case_executions(case_id: str, limit: int = Query(default=20, ge=1, le=200)) -> list[CaseExecutionRecord]:
        if library.get(case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        return library.list_executions(case_id, limit=limit)

    @router.get("/api/cases/{case_id}", response_model=CaseRecord)
    async def get_case(case_id: str, version: int | None = None) -> CaseRecord:
        record = library.get(case_id, version)
        if record is None:
            raise HTTPException(status_code=404, detail="case not found")
        return record

    @router.patch("/api/cases/{case_id}", response_model=CaseRecord)
    async def patch_case(case_id: str, body: dict[str, Any]) -> CaseRecord:
        record = library.patch(
            case_id,
            slug=body.get("slug"),
            tags=body.get("tags"),
            status=body.get("status"),
        )
        if record is None:
            raise HTTPException(status_code=404, detail="case not found")
        return record

    @router.post("/api/cases/{case_id}/execute", status_code=status.HTTP_202_ACCEPTED)
    async def execute_case(case_id: str, body: CaseExecutionRequest | None = None) -> dict[str, Any]:
        request = body or CaseExecutionRequest()
        record = library.get(case_id, request.version)
        if record is None:
            raise HTTPException(status_code=404, detail="case not found")
        execution_id = _schedule_execution(execution_locks, library, repository, record, request)
        return {"execution_id": execution_id, "status": "pending"}

    @router.get("/api/cases/{case_id}/artifacts/{path:path}")
    async def case_artifact(case_id: str, path: str, version: int | None = None) -> FileResponse:
        record = library.get(case_id, version)
        if record is None:
            raise HTTPException(status_code=404, detail="case not found")
        root = Path(record.artifact_dir).resolve()
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            raise HTTPException(status_code=404, detail="artifact not found")
        return FileResponse(candidate)

    @router.get("/api/profiles/{profile_id}/cases", response_model=list[CaseSummary])
    async def profile_cases(profile_id: str) -> list[CaseSummary]:
        return library.list(target_app_id=profile_id, limit=200)

    return router


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


async def _run_execution(
    lock: asyncio.Lock,
    library: CaseLibrary,
    record: CaseRecord,
    request: CaseExecutionRequest,
    execution_id: str,
) -> None:
    """后台执行一个用例；异常不抛出到事件循环（终态记录由用例库落库）。"""
    async with lock:
        try:
            await asyncio.to_thread(
                library.execute,
                record.case_id,
                version=request.version or record.version,
                engine=request.engine,
                device_sn=request.device_sn,
                params=request.params,
                attempt=request.attempt,
                execution_id=execution_id,
            )
        except Exception:  # noqa: BLE001 - 后台任务必须吞掉异常，否则会静默丢失
            pass


def _schedule_execution(
    locks: dict[str, asyncio.Lock],
    library: CaseLibrary,
    repository: CaseRepository,
    record: CaseRecord,
    request: CaseExecutionRequest,
) -> str:
    """先落一条 ``pending`` 执行记录，再排队后台执行，返回可轮询的 execution_id。"""
    execution_id = f"exec-{utc_now():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    repository.record_execution(
        CaseExecutionRecord(
            execution_id=execution_id,
            case_id=record.case_id,
            version=request.version or record.version,
            engine=request.engine,
            device_id=request.device_sn or record.spec.device_sn or "",
            status="pending",
        )
    )
    lock = locks.setdefault(record.case_id, asyncio.Lock())
    asyncio.create_task(_run_execution(lock, library, record, request, execution_id))
    return execution_id


def _load_dc_snapshot(dc_manager: Any, session_id: str) -> Any | None:
    if dc_manager is None:
        return None
    store = getattr(dc_manager, "store", None)
    if store is not None:
        snapshot = store.load(session_id)
        if snapshot is not None:
            return snapshot
    session = dc_manager.get(session_id) if hasattr(dc_manager, "get") else None
    return session.snapshot() if session is not None else None


def _session_identity(snapshot: Any, bundle_name: str, main_ability: str) -> tuple[str, str]:
    """从 DC 快照的既有推断器补全应用身份；失败时保留传入值。"""
    try:
        from ..dc.distill import resolve_distill_identity

        resolved = resolve_distill_identity(snapshot)
    except Exception:  # noqa: BLE001 - 身份缺失由调用方转成 422
        return bundle_name, main_ability
    if isinstance(resolved, tuple) and len(resolved) == 2:
        return bundle_name or str(resolved[0] or ""), main_ability or str(resolved[1] or "")
    return bundle_name, main_ability


def _profile_for(registry: Any, target: TargetQuery | None) -> TargetAppProfile | None:
    """尽力把目标查询解析为 Profile；解析不到就返回 ``None``（不阻塞请求）。"""
    if registry is None or target is None:
        return None
    getter = getattr(registry, "get_any", None) or getattr(registry, "get", None)
    if getter is None:
        return None
    try:
        return getter(target_app_id=target.app_name, bundle_name=target.bundle_name)
    except Exception:  # noqa: BLE001 - 未配置或匹配歧义都只降级为「无 Profile 证据」
        return None


def _provider(settings: Settings) -> Any | None:
    try:
        from ..agents.providers import create_provider

        return create_provider(settings)
    except Exception:  # noqa: BLE001 - Provider 缺失时端点返回 503
        return None


async def _plan_bug_repro(provider: Any | None, request: BugReproRequest, profile: TargetAppProfile | None) -> Any:
    """调用 provider 的缺陷复现规划；兼容同步与异步实现。"""
    if provider is None:
        return None
    planner = getattr(provider, "plan_bug_repro", None)
    if planner is None:
        return None
    from ..agents.providers import PlanningContext

    context = (
        PlanningContext.from_profile(profile)
        if profile is not None
        else PlanningContext(
            target_app_id=(request.target.app_name if request.target else None) or "unknown",
            display_name=(request.target.app_name if request.target else None) or "unknown",
            bundle_name=(request.target.bundle_name if request.target else None) or "com.example.app",
            main_ability="EntryAbility",
        )
    )
    result = planner(request, context, request.max_steps)
    if inspect.isawaitable(result):
        result = await result
    return result
