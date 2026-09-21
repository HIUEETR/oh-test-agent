"""使用 SQLite 持久化可复用用例库的版本行、摘要与执行历史。

与 :class:`~harmony_test_agent.storage.repository.RunRepository` 共用同一个
``artifacts/agent.db``：数据库只存 IR spec 本体（``spec_json``）与产物目录指针
（``artifact_dir``），脚本、xdevice 工程、报告、截图等大文本产物仍留在文件系统。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from ..cases.spec import CaseExecutionRecord, CaseSummary, TestCaseSpec
from ..models import ExecutionAnalysis, utc_now

# 用例状态白名单；与 ``TestCaseSpec.status`` 的 Literal 保持一致（draft → active → archived）。
CASE_STATUSES: tuple[str, ...] = ("draft", "active", "archived")

# 执行记录的统一列清单，保证新增/读取两侧列顺序一致。
_EXECUTION_COLUMNS = (
    "execution_id, case_id, version, engine, device_id, status, passed, "
    "started_at, ended_at, report_path, analysis_json, result_json, error"
)


def _canonical_json(payload: str) -> str:
    """把 JSON 文本规范化为稳定形式，用于「内容是否变化」的比较。"""
    try:
        return json.dumps(json.loads(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except json.JSONDecodeError:
        # 理论上不会发生（写入侧一定是合法 JSON）；退化时按原文比较，绝不抛异常。
        return payload


class CaseRepository:
    """用例库仓库：``(case_id, version)`` 行不可变，任何内容/状态变更都追加新版本。"""

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
        """幂等建表：可在与 ``RunRepository`` 共享的 ``agent.db`` 上安全重复执行。"""
        with self._lock, self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS cases (
                    case_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    slug TEXT NOT NULL,
                    title_zh TEXT NOT NULL,
                    scenario TEXT NOT NULL,
                    status TEXT NOT NULL,
                    target_app_id TEXT,
                    bundle_name TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    artifact_dir TEXT NOT NULL,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY (case_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_cases_target ON cases(target_app_id, status);
                CREATE TABLE IF NOT EXISTS case_executions (
                    execution_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    engine TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    report_path TEXT,
                    analysis_json TEXT,
                    result_json TEXT,
                    error TEXT
                );
            """)
            # 追加列 ``error``：``CaseExecutionRecord.error`` 需要落库；老库（按 B2 原始 DDL 建表）
            # 通过 ALTER 补齐，与 ``RunRepository._initialize`` 的迁移写法一致。
            columns = {row[1] for row in connection.execute("PRAGMA table_info(case_executions)").fetchall()}
            if "error" not in columns:
                connection.execute("ALTER TABLE case_executions ADD COLUMN error TEXT")

    # -- 写入 -------------------------------------------------------------

    def save(self, spec: TestCaseSpec, *, artifact_dir: Path, status: str = "active") -> int:
        """保存一个用例版本并返回版本号；与最新版本内容完全相同则复用已有版本号。"""
        normalized = self._normalize(spec, status)
        spec_json = normalized.model_dump_json()
        case_id = normalized.case_id
        # RLock 串行化同进程写入；连接上下文把「读最新版本 + 追加」包成单个事务，
        # 因此并发 save 不会算出同一个 version。
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT version, spec_json FROM cases WHERE case_id = ? ORDER BY version DESC LIMIT 1",
                (case_id,),
            ).fetchone()
            if row is not None and _canonical_json(row["spec_json"]) == _canonical_json(spec_json):
                # 去重：同一来源重复 generate 不灌水版本号。
                return int(row["version"])
            version = int(row["version"]) + 1 if row is not None else 1
            connection.execute(
                """
                INSERT INTO cases(
                    case_id, version, slug, title_zh, scenario, status, target_app_id,
                    bundle_name, source_kind, source_id, created_at, spec_json, artifact_dir, tags_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    case_id,
                    version,
                    normalized.slug,
                    normalized.title_zh,
                    str(normalized.scenario),
                    normalized.status,
                    normalized.provenance.profile_target_app_id,
                    normalized.bundle_name,
                    normalized.provenance.source_kind,
                    normalized.provenance.source_id,
                    utc_now().isoformat(),
                    spec_json,
                    str(artifact_dir),
                    json.dumps(list(normalized.tags), ensure_ascii=False),
                ),
            )
        return version

    def patch(
        self,
        case_id: str,
        *,
        slug: str | None = None,
        tags: list[str] | None = None,
        status: str | None = None,
    ) -> int:
        """按需覆盖 slug/tags/status，并追加一个新版本行（返回新版本号）。"""
        current = self.latest(case_id)
        if current is None:
            raise KeyError(case_id)
        spec, version, current_status, artifact_dir = current
        updates: dict[str, object] = {}
        if slug is not None:
            updates["slug"] = slug
        if tags is not None:
            updates["tags"] = list(tags)
        if status is not None:
            updates["status"] = status
        if not updates:
            return version
        # 走 model_validate 而不是 model_copy：非法 slug / 非法状态组合必须在落库前被拒。
        patched = TestCaseSpec.model_validate({**spec.model_dump(mode="json"), **updates})
        return self.save(patched, artifact_dir=artifact_dir, status=status or current_status)

    def record_execution(self, execution: CaseExecutionRecord) -> None:
        """按 ``execution_id`` 新增或更新一次执行记录（pending 行可被后续结果覆盖）。"""
        with self._lock, self._connect() as connection:
            connection.execute(
                f"""
                INSERT INTO case_executions({_EXECUTION_COLUMNS})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(execution_id) DO UPDATE SET
                    case_id=excluded.case_id,
                    version=excluded.version,
                    engine=excluded.engine,
                    device_id=excluded.device_id,
                    status=excluded.status,
                    passed=excluded.passed,
                    started_at=excluded.started_at,
                    ended_at=excluded.ended_at,
                    report_path=excluded.report_path,
                    analysis_json=excluded.analysis_json,
                    result_json=excluded.result_json,
                    error=excluded.error
                """,
                (
                    execution.execution_id,
                    execution.case_id,
                    execution.version,
                    execution.engine,
                    execution.device_id,
                    execution.status,
                    int(execution.passed),
                    execution.started_at.isoformat(),
                    execution.ended_at.isoformat() if execution.ended_at else None,
                    execution.report_path,
                    execution.analysis.model_dump_json() if execution.analysis else None,
                    json.dumps(execution.result, ensure_ascii=False) if execution.result is not None else None,
                    execution.error,
                ),
            )

    # -- 读取 -------------------------------------------------------------

    def latest(self, case_id: str) -> tuple[TestCaseSpec, int, str, Path] | None:
        """读取某用例的最新版本；用例不存在时返回空值。"""
        return self.get(case_id)

    def get(self, case_id: str, version: int | None = None) -> tuple[TestCaseSpec, int, str, Path] | None:
        """读取指定版本；``version`` 为空时取最新版本。返回 ``(spec, 版本, 状态, 产物目录)``。"""
        sql = "SELECT version, status, spec_json, artifact_dir FROM cases WHERE case_id = ?"
        params: tuple[object, ...] = (case_id,)
        if version is None:
            sql += " ORDER BY version DESC LIMIT 1"
        else:
            sql += " AND version = ?"
            params += (version,)
        with self._lock, self._connect() as connection:
            row = connection.execute(sql, params).fetchone()
        return self._record(row) if row is not None else None

    def list(
        self,
        *,
        target_app_id: str | None = None,
        scenario: str | None = None,
        tag: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[CaseSummary]:
        """按条件返回每个用例的最新版本摘要，按落库时间倒序，并遵守 ``limit``。"""
        if limit <= 0:
            return []
        conditions: list[str] = []
        params: list[object] = []
        if target_app_id is not None:
            conditions.append("c.target_app_id = ?")
            params.append(target_app_id)
        if scenario is not None:
            conditions.append("c.scenario = ?")
            params.append(scenario)
        if status is not None:
            conditions.append("c.status = ?")
            params.append(status)
        if tag is not None:
            # json_each 做数组成员匹配：避免把整个 tags_json 当字符串做子串命中。
            conditions.append("EXISTS (SELECT 1 FROM json_each(c.tags_json) WHERE json_each.value = ?)")
            params.append(tag)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT c.* FROM cases AS c
                JOIN (SELECT case_id, MAX(version) AS version FROM cases GROUP BY case_id) AS newest
                  ON newest.case_id = c.case_id AND newest.version = c.version
                {where}
                ORDER BY c.created_at DESC, c.rowid DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            last_statuses = self._last_execution_statuses(connection, [row["case_id"] for row in rows])
        return [self._summary(row, last_statuses.get(row["case_id"])) for row in rows]

    def versions(self, case_id: str) -> list[CaseSummary]:
        """返回某用例的全部版本摘要（新版本在前）。

        ``last_execution_status`` 与该用例最近一次执行一致（按 ``case_id`` 统计，不区分版本）。
        """
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM cases WHERE case_id = ? ORDER BY version DESC",
                (case_id,),
            ).fetchall()
            last_statuses = self._last_execution_statuses(connection, [case_id])
        return [self._summary(row, last_statuses.get(row["case_id"])) for row in rows]

    def list_executions(self, case_id: str, limit: int = 20) -> list[CaseExecutionRecord]:
        """按开始时间倒序返回某用例的执行历史。"""
        if limit <= 0:
            return []
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT {_EXECUTION_COLUMNS} FROM case_executions
                WHERE case_id = ?
                ORDER BY started_at DESC, rowid DESC
                LIMIT ?
                """,
                (case_id, limit),
            ).fetchall()
        return [self._execution(row) for row in rows]

    def get_execution(self, execution_id: str) -> CaseExecutionRecord | None:
        """按执行标识读取一次执行记录；不存在时返回空值。"""
        with self._lock, self._connect() as connection:
            row = connection.execute(
                f"SELECT {_EXECUTION_COLUMNS} FROM case_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        return self._execution(row) if row is not None else None

    # -- 内部工具 ---------------------------------------------------------

    @staticmethod
    def _normalize(spec: TestCaseSpec, status: str) -> TestCaseSpec:
        """把请求状态并进 spec 并重新校验，避免把非法状态组合写进库里。"""
        if status not in CASE_STATUSES:
            raise ValueError(f"unknown case status: {status!r}")
        if status == spec.status:
            return spec
        return TestCaseSpec.model_validate({**spec.model_dump(mode="json"), "status": status})

    @staticmethod
    def _record(row: sqlite3.Row) -> tuple[TestCaseSpec, int, str, Path]:
        """把 cases 行还原为 ``(spec, 版本, 状态, 产物目录)``。"""
        spec = TestCaseSpec.model_validate_json(row["spec_json"])
        return spec, int(row["version"]), row["status"], Path(row["artifact_dir"])

    @staticmethod
    def _last_execution_statuses(connection: sqlite3.Connection, case_ids: list[str]) -> dict[str, str]:
        """一次查询取回这批用例各自最近一次执行的状态（``started_at`` 落后者胜出）。"""
        if not case_ids:
            return {}
        placeholders = ", ".join("?" for _ in case_ids)
        rows = connection.execute(
            f"""
            SELECT case_id, status FROM case_executions
            WHERE case_id IN ({placeholders})
            ORDER BY started_at, rowid
            """,
            case_ids,
        ).fetchall()
        statuses: dict[str, str] = {}
        for row in rows:
            statuses[row["case_id"]] = row["status"]
        return statuses

    @staticmethod
    def _summary(row: sqlite3.Row, last_execution_status: str | None) -> CaseSummary:
        """由 cases 行与 spec 计算列表项；步数/硬检查点数与 IR 遍历口径一致。"""
        spec = TestCaseSpec.model_validate_json(row["spec_json"])
        steps = [*spec.setup.pre_steps, *spec.steps, *spec.teardown.post_steps]
        hard_checkpoints = sum(len(step.hard_checkpoints) for step in steps)
        if spec.stress is not None:
            steps += list(spec.stress.body_steps)
            hard_checkpoints += sum(len(step.hard_checkpoints) for step in spec.stress.body_steps)
            hard_checkpoints += sum(1 for item in spec.stress.per_iteration_checkpoints if not item.soft)
        return CaseSummary(
            case_id=row["case_id"],
            version=int(row["version"]),
            slug=row["slug"],
            title_zh=row["title_zh"],
            scenario=row["scenario"],
            status=row["status"],
            tags=json.loads(row["tags_json"] or "[]"),
            target_app_id=row["target_app_id"],
            bundle_name=row["bundle_name"],
            source_kind=row["source_kind"],
            source_id=row["source_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            artifact_dir=row["artifact_dir"],
            step_count=len(steps),
            hard_checkpoint_count=hard_checkpoints,
            last_execution_status=last_execution_status,
        )

    @staticmethod
    def _execution(row: sqlite3.Row) -> CaseExecutionRecord:
        """把 case_executions 行还原为 ``CaseExecutionRecord``（含 analysis/result 反序列化）。"""
        return CaseExecutionRecord(
            execution_id=row["execution_id"],
            case_id=row["case_id"],
            version=int(row["version"]),
            engine=row["engine"],
            device_id=row["device_id"],
            status=row["status"],
            passed=bool(row["passed"]),
            started_at=datetime.fromisoformat(row["started_at"]),
            ended_at=datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None,
            report_path=row["report_path"],
            analysis=ExecutionAnalysis.model_validate_json(row["analysis_json"]) if row["analysis_json"] else None,
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"],
        )


__all__ = ["CASE_STATUSES", "CaseRepository"]
