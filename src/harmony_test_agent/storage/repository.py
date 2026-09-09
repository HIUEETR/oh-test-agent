"""使用 SQLite 持久化运行轨迹与按序递增的运行事件。"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from ..models import RunEvent, RunState, RunTrace, utc_now


class RunRepository:
    """通过线程锁保护 SQLite 连接，持久化运行摘要、完整轨迹和事件。"""

    def __init__(self, database_path: Path):
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_path = database_path
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    target_app_id TEXT NOT NULL,
                    task TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    trace_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL,
                    event_id INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, event_id)
                );
            """)

    def save_trace(self, trace: RunTrace) -> None:
        """新增或更新运行轨迹及其可检索摘要。"""
        # RLock 将同一进程内的写入串行化；连接上下文负责提交或回滚单次更新。
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runs(run_id, state, target_app_id, task, updated_at, trace_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    state=excluded.state,
                    updated_at=excluded.updated_at,
                    trace_json=excluded.trace_json
                """,
                (
                    trace.run_id,
                    trace.state,
                    trace.target_app_id,
                    trace.task,
                    utc_now().isoformat(),
                    trace.model_dump_json(),
                ),
            )

    def get_trace(self, run_id: str) -> RunTrace | None:
        """按运行标识读取完整轨迹；不存在时返回空值。"""
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT trace_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return RunTrace.model_validate_json(row["trace_json"]) if row else None

    def list_runs(self, limit: int = 50) -> list[dict[str, str]]:
        """按更新时间倒序返回指定数量的运行摘要。"""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id, state, target_app_id, task, updated_at FROM runs ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_event(self, event: RunEvent) -> None:
        """按运行标识和事件序号保存事件，重复序号会被替换。"""
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO events VALUES (?, ?, ?, ?, ?)",
                (event.run_id, event.event_id, event.type, event.timestamp.isoformat(), event.model_dump_json()),
            )

    def get_events(self, run_id: str, after: int = 0) -> list[RunEvent]:
        """按序返回指定游标之后的运行事件。"""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT event_json FROM events WHERE run_id = ? AND event_id > ? ORDER BY event_id",
                (run_id, after),
            ).fetchall()
        return [RunEvent.model_validate_json(row["event_json"]) for row in rows]

    def mark_stopped(self, run_id: str) -> bool:
        """将既有运行标记为用户停止并记录结束时间。"""
        trace = self.get_trace(run_id)
        if not trace:
            return False
        trace.state = RunState.STOPPED_BY_USER
        trace.ended_at = utc_now()
        self.save_trace(trace)
        return True
