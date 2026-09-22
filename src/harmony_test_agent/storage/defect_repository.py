"""使用 SQLite 持久化应用缺陷（``DefectRecord``）。

与 :class:`~harmony_test_agent.storage.repository.RunRepository` /
:class:`~harmony_test_agent.storage.case_repository.CaseRepository` 共用同一个
``artifacts/agent.db``：摘要列供查询与过滤，完整记录落 ``record_json`` blob。

并发写：照 ``case_repository.py`` 的成例，``threading.RLock`` + 单连接 + 幂等 PRAGMA 迁移。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from ..analysis.defects import (
    DefectRecord,
    DefectStatus,
    DefectSummary,
    merge_key,
)
from ..models import AnomalyFinding, utc_now

_COLUMNS = (
    "defect_id, bundle_name, kind, severity, status, title_zh, "
    "run_id, session_id, case_id, page_path, action_id, device_id, "
    "occurrences, first_seen_at, last_seen_at, record_json"
)


class DefectRepository:
    """缺陷仓库：``defect_id`` 主键，归并更新（同 key 不新增行）。"""

    def __init__(self, database_path: Path):
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_path = database_path
        self._lock = threading.RLock()
        self._initialize()

    # -- 连接与建表 -------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        """幂等建表：可在与 ``RunRepository`` / ``CaseRepository`` 共享的库上安全重复执行。"""
        with self._lock, self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS defects (
                    defect_id TEXT PRIMARY KEY,
                    bundle_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    status TEXT NOT NULL,
                    title_zh TEXT NOT NULL,
                    run_id TEXT,
                    session_id TEXT,
                    case_id TEXT,
                    page_path TEXT,
                    action_id TEXT,
                    device_id TEXT,
                    occurrences INTEGER NOT NULL DEFAULT 1,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    record_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_defects_bundle ON defects(bundle_name, status);
                CREATE INDEX IF NOT EXISTS idx_defects_kind ON defects(kind, severity);
                CREATE INDEX IF NOT EXISTS idx_defects_run ON defects(run_id);
                CREATE INDEX IF NOT EXISTS idx_defects_session ON defects(session_id);
                CREATE INDEX IF NOT EXISTS idx_defects_repro ON defects(case_id);
            """)
            # 追加列 ``case_id``：老库（按计划原始 DDL 建表）通过 ALTER 补齐，
            # 与 ``RunRepository._initialize`` / ``CaseRepository._initialize`` 的迁移写法一致。
            columns = {row[1] for row in connection.execute("PRAGMA table_info(defects)").fetchall()}
            if "case_id" not in columns:
                connection.execute("ALTER TABLE defects ADD COLUMN case_id TEXT")

    # -- 写入 -------------------------------------------------------------

    def save(self, record: DefectRecord) -> None:
        """按 ``defect_id`` 新增或整行覆盖。"""
        with self._lock, self._connect() as connection:
            connection.execute(
                f"""
                INSERT INTO defects({_COLUMNS})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(defect_id) DO UPDATE SET
                    bundle_name=excluded.bundle_name,
                    kind=excluded.kind,
                    severity=excluded.severity,
                    status=excluded.status,
                    title_zh=excluded.title_zh,
                    run_id=excluded.run_id,
                    session_id=excluded.session_id,
                    case_id=excluded.case_id,
                    page_path=excluded.page_path,
                    action_id=excluded.action_id,
                    device_id=excluded.device_id,
                    occurrences=excluded.occurrences,
                    first_seen_at=excluded.first_seen_at,
                    last_seen_at=excluded.last_seen_at,
                    record_json=excluded.record_json
                """,
                (
                    record.defect_id,
                    record.bundle_name,
                    str(record.kind),
                    record.severity,
                    str(record.status),
                    record.title_zh,
                    record.run_id,
                    record.session_id,
                    record.case_id,
                    record.page_path,
                    record.action_id,
                    record.device_id,
                    record.occurrences,
                    record.first_seen_at.isoformat(),
                    record.last_seen_at.isoformat(),
                    record.model_dump_json(),
                ),
            )

    def patch(
        self,
        defect_id: str,
        *,
        status: DefectStatus | str | None = None,
        notes: str | None = None,
        repro_case_id: str | None = None,
        repro_execution_id: str | None = None,
    ) -> DefectRecord | None:
        """人工处置 / 回写复现用例；缺陷不存在时返回 ``None``。"""
        record = self.get(defect_id)
        if record is None:
            return None
        updates: dict[str, object] = {}
        if status is not None:
            updates["status"] = DefectStatus(str(status))
        if notes is not None:
            updates["notes"] = notes
        if repro_case_id is not None:
            updates["repro_case_id"] = repro_case_id
        if repro_execution_id is not None:
            updates["repro_execution_id"] = repro_execution_id
        if not updates:
            return record
        updated = record.model_copy(update=updates, deep=True)
        self.save(updated)
        return updated

    def attach_repro_case(
        self, defect_id: str, *, case_id: str, execution_id: str | None = None
    ) -> DefectRecord | None:
        """把「由本缺陷生成的复现用例」挂回缺陷（重跑结果未知时状态保持 suspected）。"""
        return self.patch(defect_id, repro_case_id=case_id, repro_execution_id=execution_id)

    # -- 读取 -------------------------------------------------------------

    def get(self, defect_id: str) -> DefectRecord | None:
        """按 id 读取完整记录；不存在时返回 ``None``。"""
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM defects WHERE defect_id = ?",
                (defect_id,),
            ).fetchone()
        return self._record(row) if row is not None else None

    def find_by_merge_key(self, key: tuple[str, ...]) -> DefectRecord | None:
        """按归并键查找已有记录。

        归并键含 ``page_path`` 与 ``action_target``，后者不是独立列，因此先按
        ``(bundle_name, kind, page_path)`` 缩小候选，再在应用层用同一条
        :func:`~harmony_test_agent.analysis.defects.merge_key` 精确比对。
        """
        bundle_name, kind, page_path, _target = key
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT record_json FROM defects
                WHERE bundle_name = ? AND kind = ? AND COALESCE(page_path, '') = ?
                ORDER BY last_seen_at DESC
                """,
                (bundle_name, kind, page_path),
            ).fetchall()
        for row in rows:
            record = self._record(row)
            if record is None:
                continue
            if any(merge_key(finding, record.bundle_name) == key for finding in record.findings):
                return record
        return None

    def latest_for_finding(self, finding: AnomalyFinding) -> DefectRecord | None:
        """按 finding 自身的归属信息查找已归并的缺陷。"""
        return self.find_by_merge_key(merge_key(finding, str(finding.evidence.get("bundle_name") or "")))

    def list(
        self,
        *,
        bundle_name: str | None = None,
        kind: str | None = None,
        severity: str | None = None,
        status: str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        case_id: str | None = None,
        limit: int = 100,
    ) -> list[DefectSummary]:
        """按条件返回缺陷摘要，按 ``last_seen_at`` 倒序。"""
        if limit <= 0:
            return []
        conditions: list[str] = []
        params: list[object] = []
        for column, value in (
            ("bundle_name", bundle_name),
            ("kind", kind),
            ("severity", severity),
            ("status", status),
            ("run_id", run_id),
            ("session_id", session_id),
            ("case_id", case_id),
        ):
            if value is not None:
                conditions.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT record_json FROM defects
                {where}
                ORDER BY last_seen_at DESC, rowid DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        summaries: list[DefectSummary] = []
        for row in rows:
            record = self._record(row)
            if record is not None:
                summaries.append(self.summary(record))
        return summaries

    def list_for_bundle(self, bundle_name: str, limit: int = 100) -> list[DefectSummary]:
        """某应用的全部缺陷。"""
        return self.list(bundle_name=bundle_name, limit=limit)

    def list_for_run(self, run_id: str, limit: int = 100) -> list[DefectSummary]:
        """某次运行发现的缺陷。"""
        return self.list(run_id=run_id, limit=limit)

    def list_for_case(self, case_id: str, limit: int = 100) -> list[DefectSummary]:
        """某复现用例关联的缺陷。"""
        return self.list(case_id=case_id, limit=limit)

    def count(self) -> int:
        """缺陷总数（供健康检查与测试使用）。"""
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS total FROM defects").fetchone()
        return int(row["total"]) if row is not None else 0

    @property
    def write_lock(self) -> threading.RLock:
        """写事务锁。

        ``DefectRecorder.record_one`` 是「读已有记录 → 归并 → 覆盖写」的读-改-写序列，
        只锁单条 SQL 会丢更新（并发下 ``occurrences`` 少算）。归并必须持有同一把锁，
        因此把它交给 recorder 使用 —— 本仓库是这些记录的唯一写入方。
        """
        return self._lock

    # -- 内部工具 ---------------------------------------------------------

    @staticmethod
    def summary(record: DefectRecord) -> DefectSummary:
        """把完整记录投影成列表项。"""
        return DefectSummary(
            defect_id=record.defect_id,
            bundle_name=record.bundle_name,
            kind=record.kind,
            severity=record.severity,
            status=record.status,
            title_zh=record.title_zh,
            summary_zh=record.summary_zh,
            page_path=record.page_path,
            action_id=record.action_id,
            occurrences=record.occurrences,
            first_seen_at=record.first_seen_at,
            last_seen_at=record.last_seen_at,
            run_id=record.run_id,
            session_id=record.session_id,
            case_id=record.case_id,
            repro_case_id=record.repro_case_id,
            finding_count=len(record.findings),
        )

    @staticmethod
    def _record(row: sqlite3.Row | None) -> DefectRecord | None:
        """把 defects 行的 blob 还原为完整记录；损坏行降级为 ``None``。"""
        if row is None:
            return None
        try:
            return DefectRecord.model_validate_json(row["record_json"])
        except ValueError, TypeError:
            return None


def merge_records(records: list[DefectRecord]) -> list[DefectRecord]:
    """把同一 ``defect_id`` 的多条记录合并为一条（``findings`` 去重、``occurrences`` 取最大）。

    用于「运行中发现的 finding」与「事后分析发现的 finding」在收尾时的对账合并：
    两者可能落在同一个归并键上，报告只应展示一条缺陷。
    """
    merged: dict[str, DefectRecord] = {}
    for record in records:
        existing = merged.get(record.defect_id)
        if existing is None:
            merged[record.defect_id] = record
            continue
        findings = list(existing.findings)
        seen = {(item.kind, item.action_id, item.detail[:200]) for item in findings}
        for finding in record.findings:
            key = (finding.kind, finding.action_id, finding.detail[:200])
            if key not in seen:
                seen.add(key)
                findings.append(finding)
        paths = list(dict.fromkeys([*existing.evidence_paths, *record.evidence_paths]))
        merged[record.defect_id] = existing.model_copy(
            update={
                "occurrences": max(existing.occurrences, record.occurrences),
                "findings": findings,
                "evidence_paths": paths,
                "last_seen_at": max(existing.last_seen_at, record.last_seen_at),
                "first_seen_at": min(existing.first_seen_at, record.first_seen_at),
                "run_id": existing.run_id or record.run_id,
                "session_id": existing.session_id or record.session_id,
                "case_id": existing.case_id or record.case_id,
            },
            deep=True,
        )
    return list(merged.values())


def utc_now_iso() -> str:
    """当前 UTC 时间的 ISO 字符串（供外部拼 SQL 使用）。"""
    return utc_now().isoformat()


__all__ = ["DefectRepository", "merge_records", "utc_now_iso"]
