"""缺陷处置 API（``/api/defects*``）：列表 / 详情 / 人工判定 / 证据文件 / 转复现用例。

独立成 ``api/defects.py``（而不是塞进 ``api/app.py``）：缺陷是 Phase 3 新增的一等产物，
路由与 ``api/cases.py`` 同构，便于单独测试与后续扩展。

**声明顺序**：``/api/defects/{defect_id}/artifacts/{path:path}`` 必须在
``/api/defects/{defect_id}`` 之后但在任何通配之前 —— 参照 ``api/cases.py`` 已踩过的坑
（``/api/cases/executions/{id}`` 必须早于 ``/api/cases/{id}``）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import FileResponse

from ..analysis.defects import ANOMALY_TO_SYMPTOM, DefectStatus, defect_to_bug_repro_request
from ..config import Settings
from ..storage import ArtifactStore, DefectRepository, RunRepository

logger = logging.getLogger(__name__)

#: 证据文件允许的后缀（与报告建链接的后缀同口径）。
EVIDENCE_SUFFIXES = frozenset({".jpeg", ".jpg", ".png", ".webp", ".json", ".log", ".txt", ".xml", ".html", ".zip"})

MAX_LIST_LIMIT = 500


def create_defects_router(
    *,
    settings: Settings,
    repository: DefectRepository,
    run_repository: RunRepository | None = None,
    artifacts: ArtifactStore | None = None,
    bug_repro_factory: Callable[..., Any] | None = None,
    execution_callback: Callable[..., Any] | None = None,
) -> APIRouter:
    """创建缺陷路由。

    ``bug_repro_factory`` 由 ``api/cases.py`` 注入（复用其既有 bug-repro 实现路径）；
    缺失时 ``to-bug-repro`` 返回 501，缺陷查询与处置仍然可用。
    """
    router = APIRouter(prefix="/api/defects", tags=["defects"])

    # ------------------------------------------------------------------
    # 列表与详情
    # ------------------------------------------------------------------

    @router.get("")
    async def list_defects(
        bundle_name: str | None = None,
        kind: str | None = None,
        severity: str | None = None,
        status: str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        case_id: str | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIST_LIMIT)] = 100,
    ) -> dict[str, Any]:
        """按条件列出缺陷摘要，按 ``last_seen_at`` 倒序。"""
        _validate_status(status)
        summaries = repository.list(
            bundle_name=bundle_name,
            kind=kind,
            severity=severity,
            status=status,
            run_id=run_id,
            session_id=session_id,
            case_id=case_id,
            limit=limit,
        )
        return {"total": len(summaries), "defects": [item.model_dump(mode="json") for item in summaries]}

    @router.get("/{defect_id}")
    async def get_defect(defect_id: str) -> dict[str, Any]:
        """读取一条缺陷的完整记录。"""
        record = repository.get(defect_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"defect {defect_id} not found")
        return record.model_dump(mode="json")

    @router.patch("/{defect_id}")
    async def patch_defect(
        defect_id: str,
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict[str, Any]:
        """人工处置：``status`` 与 ``notes``。

        ``status`` 只接受 :class:`DefectStatus` 的取值；``dismissed`` 用于「误报 / 非缺陷」。
        """
        body = payload or {}
        raw_status = body.get("status")
        _validate_status(raw_status)
        notes = body.get("notes")
        if notes is not None and not isinstance(notes, str):
            raise HTTPException(status_code=422, detail="notes must be a string")
        updated = repository.patch(defect_id, status=raw_status, notes=notes)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"defect {defect_id} not found")
        return updated.model_dump(mode="json")

    # ------------------------------------------------------------------
    # 证据文件（必须在任何通配之前）
    # ------------------------------------------------------------------

    @router.get("/{defect_id}/artifacts/{artifact_path:path}")
    async def download_artifact(defect_id: str, artifact_path: str):
        """下载缺陷证据文件；路径穿越防护照抄 ``api/app.py`` 的成例。"""
        record = repository.get(defect_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"defect {defect_id} not found")
        if artifacts is None:
            raise HTTPException(status_code=501, detail="artifact store is unavailable")
        root = _artifact_root(record, artifacts)
        if root is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        requested = (root / artifact_path).resolve()
        if not requested.is_relative_to(root.resolve()) or not requested.is_file():
            raise HTTPException(status_code=404, detail="artifact not found")
        if requested.suffix.lower() not in EVIDENCE_SUFFIXES:
            raise HTTPException(status_code=404, detail="artifact not found")
        headers: dict[str, str] = {}
        if requested.suffix.lower() in (".jpeg", ".jpg", ".png", ".webp"):
            headers["Cache-Control"] = "public, max-age=3600, immutable"
        return FileResponse(requested, headers=headers)

    # ------------------------------------------------------------------
    # 转复现用例（Phase 4）
    # ------------------------------------------------------------------

    @router.post("/{defect_id}/to-bug-repro", status_code=201)
    async def to_bug_repro(
        defect_id: str,
        auto_execute: Annotated[bool, Query()] = False,
    ) -> dict[str, Any]:
        """把一条缺陷转成复现用例（可带 ``auto_execute=true`` 立即重跑）。

        重跑结果由 ``cases/library.py`` 的执行完成回调回写 ``record.status``
        （``symptom_reproduced`` → ``confirmed`` / ``not_reproduced``）。
        """
        record = repository.get(defect_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"defect {defect_id} not found")
        if record.status is DefectStatus.DISMISSED:
            raise HTTPException(status_code=409, detail="dismissed defect cannot be turned into a repro case")
        if bug_repro_factory is None:
            raise HTTPException(status_code=501, detail="bug reproduction pipeline is unavailable")

        trace = None
        if run_repository is not None and record.run_id:
            trace = run_repository.get_trace(record.run_id)
        request = defect_to_bug_repro_request(record, trace=trace)
        try:
            created = bug_repro_factory(
                record=record,
                request=request,
                trace=trace,
                auto_execute=auto_execute,
            )
            # ``await`` 必须在 try 内：异步 factory 的异常只在 await 时才抛。
            created = await _maybe_await(created)
        except Exception as exc:  # noqa: BLE001 - 上游失败要如实报告，不吞
            logger.warning("defect %s to bug-repro failed: %s: %s", defect_id, type(exc).__name__, exc)
            raise HTTPException(status_code=502, detail=f"cannot create repro case: {exc}") from exc

        case_id = _case_id(created)
        execution_id = _execution_id(created)
        if case_id and auto_execute and execution_callback is not None and not execution_id:
            execution_id = await _maybe_await(execution_callback(case_id=case_id, request=request))
        if case_id:
            repository.attach_repro_case(
                defect_id,
                case_id=case_id,
                execution_id=str(execution_id) if execution_id else None,
            )
        return {
            "defect_id": defect_id,
            "case": _dump(created),
            "case_id": case_id,
            "execution_id": str(execution_id) if execution_id else None,
            "symptom_kind": ANOMALY_TO_SYMPTOM.get(record.kind, "other"),
        }

    return router


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _validate_status(status: Any) -> None:
    """校验状态取值；``None`` 视为未提供。"""
    if status is None:
        return
    allowed = {item.value for item in DefectStatus}
    if str(status) not in allowed:
        raise HTTPException(status_code=422, detail=f"unknown defect status: {status!r}")


def _artifact_root(record: Any, artifacts: ArtifactStore) -> Path | None:
    """证据文件所在根目录：优先运行目录，其次会话目录。"""
    if record.run_id:
        run_dir = artifacts.run_dir(record.run_id)
        if run_dir.is_dir():
            return run_dir
    if record.session_id:
        session_dir = artifacts.run_dir(record.session_id)
        if session_dir.is_dir():
            return session_dir
    return None


def _case_id(created: Any) -> str | None:
    """从 bug-repro 返回值里取 case_id（兼容 pydantic 模型与 dict）。"""
    if created is None:
        return None
    for attribute in ("case_id",):
        value = getattr(created, attribute, None)
        if value:
            return str(value)
    if isinstance(created, dict):
        value = created.get("case_id")
        if value:
            return str(value)
    nested = getattr(created, "spec", None)
    value = getattr(nested, "case_id", None)
    return str(value) if value else None


def _execution_id(created: Any) -> str | None:
    """从 bug-repro 返回值里取执行标识（``auto_execute=true`` 时才有）。"""
    if created is None:
        return None
    value = getattr(created, "execution_id", None)
    if not value and isinstance(created, dict):
        value = created.get("execution_id")
    return str(value) if value else None


def _dump(created: Any) -> Any:
    """把返回值转成 JSON 可序列化结构。"""
    dumper = getattr(created, "model_dump", None)
    if callable(dumper):
        return dumper(mode="json")
    return created


async def _maybe_await(value: Any) -> Any:
    """同时接受同步与异步回调。"""
    if hasattr(value, "__await__"):
        return await value
    return value


__all__ = ["EVIDENCE_SUFFIXES", "MAX_LIST_LIMIT", "create_defects_router"]
