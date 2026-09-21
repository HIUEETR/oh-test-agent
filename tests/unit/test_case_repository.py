"""``CaseRepository`` 的版本不变式、过滤查询、执行历史与并发写入测试。"""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from harmony_test_agent.cases.spec import (
    CaseExecutionRecord,
    CaseProvenance,
    CheckpointKind,
    CheckpointSpec,
    LocatorSpec,
    ScenarioKind,
    StepAction,
)
from harmony_test_agent.cases.spec import TestCaseSpec as CaseSpec
from harmony_test_agent.cases.spec import TestStepSpec as StepSpec
from harmony_test_agent.models import AnomalyFinding, AnomalyKind, ExecutionAnalysis, LocatorKind, utc_now
from harmony_test_agent.storage.case_repository import CaseRepository
from harmony_test_agent.storage.repository import RunRepository

CASE_ID = "case-20240101T000000Z-abc123"
TARGET_APP = "com.example.notes"
# 固定 provenance 时间：spec_json 的字节级比较依赖它（``CaseProvenance.created_at`` 默认取当前时间）。
CREATED_AT = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture
def repository(tmp_path: Path) -> CaseRepository:
    """每个测试一个独立的 ``agent.db``。"""
    return CaseRepository(tmp_path / "agent.db")


@pytest.fixture
def artifact_dir(tmp_path: Path) -> Path:
    """版本产物目录（真实目录由用例库/发射器创建，这里只存指针）。"""
    return tmp_path / "artifacts" / "cases" / CASE_ID / "v1"


def _spec(
    case_id: str = CASE_ID,
    *,
    slug: str = "login-flow",
    title_zh: str = "登录流程",
    scenario: ScenarioKind = ScenarioKind.CORE_FLOW,
    status: str = "active",
    tags: tuple[str, ...] = ("smoke",),
    target_app_id: str | None = TARGET_APP,
    step_title: str = "点击登录按钮",
) -> CaseSpec:
    """构造一个最小但合法的用例 IR：一步点击 + 一个硬检查点。"""
    locator = LocatorSpec(kind=LocatorKind.KEY, value="login_button")
    checkpoint = CheckpointSpec(kind=CheckpointKind.ELEMENT_EXISTS, message_zh="登录按钮存在", locator=locator)
    step = StepSpec(
        step_id="step-1",
        index=1,
        action=StepAction.CLICK,
        title_zh=step_title,
        locator=locator,
        checkpoints=[checkpoint],
    )
    return CaseSpec(
        case_id=case_id,
        slug=slug,
        title_zh=title_zh,
        scenario=scenario,
        status=status,
        tags=list(tags),
        bundle_name="com.example.notes",
        provenance=CaseProvenance(
            source_kind="manual",
            source_id="manual-1",
            profile_target_app_id=target_app_id,
            created_at=CREATED_AT,
        ),
        steps=[step],
    )


def _execution(
    execution_id: str = "exec-1",
    *,
    case_id: str = CASE_ID,
    version: int = 1,
    status: str = "pending",
    passed: bool = False,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    report_path: str | None = None,
    analysis: ExecutionAnalysis | None = None,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> CaseExecutionRecord:
    """构造一条执行记录。"""
    return CaseExecutionRecord(
        execution_id=execution_id,
        case_id=case_id,
        version=version,
        engine="hypium_standalone",
        device_id="SN-TEST-1",
        status=status,
        passed=passed,
        started_at=started_at or utc_now(),
        ended_at=ended_at,
        report_path=report_path,
        analysis=analysis,
        result=result,
        error=error,
    )


def _stored_spec_json(database_path: Path, case_id: str, version: int) -> str:
    """直连数据库读原始 ``spec_json``，用于证明历史版本行没有被就地改写。"""
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT spec_json FROM cases WHERE case_id = ? AND version = ?",
            (case_id, version),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _version_rows(database_path: Path, case_id: str) -> list[tuple[int, str]]:
    """返回某用例的全部 ``(version, status)``，按版本升序。"""
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT version, status FROM cases WHERE case_id = ? ORDER BY version",
            (case_id,),
        ).fetchall()
    return [(int(row[0]), str(row[1])) for row in rows]


# ---------------------------------------------------------------------------
# 版本与去重
# ---------------------------------------------------------------------------


def test_first_save_creates_version_one(repository: CaseRepository, artifact_dir: Path):
    spec = _spec()

    assert repository.save(spec, artifact_dir=artifact_dir) == 1

    latest = repository.latest(CASE_ID)
    assert latest is not None
    stored, version, status, stored_dir = latest
    assert version == 1
    assert status == "active"
    assert stored_dir == artifact_dir
    assert stored.model_dump() == spec.model_dump()

    by_version = repository.get(CASE_ID, 1)
    assert by_version is not None
    assert by_version[1] == 1
    assert [item.version for item in repository.versions(CASE_ID)] == [1]


def test_identical_spec_is_deduplicated(repository: CaseRepository, artifact_dir: Path):
    assert repository.save(_spec(), artifact_dir=artifact_dir) == 1
    # 同一个 spec 对象、以及内容等价的新对象，都不应新增版本。
    assert repository.save(_spec(), artifact_dir=artifact_dir) == 1
    # 去重以 spec_json 为准：仅产物目录变化同样复用已有版本号。
    assert repository.save(_spec(), artifact_dir=artifact_dir.parent / "v2") == 1

    assert [item.version for item in repository.versions(CASE_ID)] == [1]
    assert len(_version_rows(repository.database_path, CASE_ID)) == 1


def test_changed_spec_appends_version_and_keeps_old_row(repository: CaseRepository, artifact_dir: Path):
    assert repository.save(_spec(), artifact_dir=artifact_dir) == 1
    first_json = _stored_spec_json(repository.database_path, CASE_ID, 1)

    assert repository.save(_spec(title_zh="登录流程（回归）"), artifact_dir=artifact_dir) == 2

    # 不可变性：v1 行的 spec_json 一个字节都没变。
    assert _stored_spec_json(repository.database_path, CASE_ID, 1) == first_json
    assert _version_rows(repository.database_path, CASE_ID) == [(1, "active"), (2, "active")]

    version_one = repository.get(CASE_ID, 1)
    assert version_one is not None
    assert version_one[0].title_zh == "登录流程"

    latest = repository.latest(CASE_ID)
    assert latest is not None
    assert latest[1] == 2
    assert latest[0].title_zh == "登录流程（回归）"
    assert [item.version for item in repository.versions(CASE_ID)] == [2, 1]


def test_status_change_appends_new_version(repository: CaseRepository, artifact_dir: Path):
    assert repository.save(_spec(), artifact_dir=artifact_dir, status="active") == 1

    assert repository.save(_spec(), artifact_dir=artifact_dir, status="archived") == 2

    latest = repository.latest(CASE_ID)
    assert latest is not None
    assert latest[2] == "archived"
    assert latest[0].status == "archived"
    # 状态迁移不就地改写旧行。
    assert _version_rows(repository.database_path, CASE_ID) == [(1, "active"), (2, "archived")]


def test_unknown_status_is_rejected(repository: CaseRepository, artifact_dir: Path):
    with pytest.raises(ValueError):
        repository.save(_spec(), artifact_dir=artifact_dir, status="retired")

    assert repository.versions(CASE_ID) == []


def test_unknown_case_or_version_returns_none(repository: CaseRepository):
    assert repository.latest(CASE_ID) is None
    assert repository.get(CASE_ID) is None
    assert repository.get(CASE_ID, 1) is None
    assert repository.versions(CASE_ID) == []


# ---------------------------------------------------------------------------
# 列表过滤
# ---------------------------------------------------------------------------


@pytest.fixture
def library(repository: CaseRepository, tmp_path: Path) -> CaseRepository:
    """四个用例：两个应用、三种场景、三种状态、四组标签，按 A→D 顺序落库。"""
    specs = [
        _spec(
            "case-20240101T000000Z-aaa111",
            slug="alpha-core",
            title_zh="阿尔法核心流程",
            scenario=ScenarioKind.CORE_FLOW,
            status="active",
            tags=("smoke", "alpha"),
            target_app_id="com.example.alpha",
        ),
        _spec(
            "case-20240101T000000Z-bbb222",
            slug="beta-explore",
            title_zh="贝塔探索",
            scenario=ScenarioKind.EXPLORATORY,
            status="draft",
            tags=("regression",),
            target_app_id="com.example.beta",
        ),
        _spec(
            "case-20240101T000000Z-ccc333",
            slug="alpha-smoke",
            title_zh="阿尔法冒烟",
            scenario=ScenarioKind.SMOKE,
            status="archived",
            tags=("smoke",),
            target_app_id="com.example.alpha",
        ),
        _spec(
            "case-20240101T000000Z-ddd444",
            slug="alpha-smoke-long",
            title_zh="阿尔法长冒烟",
            scenario=ScenarioKind.SMOKE,
            status="active",
            tags=("smoke-long",),
            target_app_id="com.example.alpha",
        ),
    ]
    for spec in specs:
        # save() 的 status 参数是权威值：要落成 draft/archived 必须显式传（默认 active）。
        repository.save(spec, artifact_dir=tmp_path / "artifacts" / "cases" / spec.case_id / "v1", status=spec.status)
    return repository


def test_list_returns_only_latest_version_per_case(repository: CaseRepository, artifact_dir: Path):
    assert repository.save(_spec(), artifact_dir=artifact_dir) == 1
    assert repository.save(_spec(title_zh="登录流程 v2"), artifact_dir=artifact_dir) == 2

    items = repository.list()

    assert [item.version for item in items] == [2]
    assert items[0].title_zh == "登录流程 v2"
    assert items[0].step_count == 1
    assert items[0].hard_checkpoint_count == 1
    assert items[0].tags == ["smoke"]
    assert items[0].target_app_id == TARGET_APP


def test_list_filters(library: CaseRepository):
    def slugs(**kwargs: Any) -> set[str]:
        return {item.slug for item in library.list(**kwargs)}

    assert slugs(target_app_id="com.example.alpha") == {"alpha-core", "alpha-smoke", "alpha-smoke-long"}
    assert slugs(scenario="smoke") == {"alpha-smoke", "alpha-smoke-long"}
    assert slugs(status="draft") == {"beta-explore"}
    assert slugs(status="archived") == {"alpha-smoke"}
    assert slugs(tag="regression") == {"beta-explore"}
    assert slugs(target_app_id="com.example.alpha", tag="smoke") == {"alpha-core", "alpha-smoke"}
    assert slugs(target_app_id="com.example.notes") == set()


def test_list_tag_filter_matches_membership_not_substring(library: CaseRepository):
    # "smoke" 只能命中成员完全等于 smoke 的行，不能命中 smoke-long，也不能命中子串。
    matched = library.list(tag="smoke")
    assert {item.slug for item in matched} == {"alpha-core", "alpha-smoke"}
    assert all("smoke" in item.tags for item in matched)

    assert [item.slug for item in library.list(tag="smoke-long")] == ["alpha-smoke-long"]
    assert library.list(tag="smok") == []
    assert [item.slug for item in library.list(tag="alpha")] == ["alpha-core"]


def test_list_is_newest_first_and_honours_limit(library: CaseRepository):
    ordered = library.list()
    assert [item.slug for item in ordered] == [
        "alpha-smoke-long",
        "alpha-smoke",
        "beta-explore",
        "alpha-core",
    ]

    limited = library.list(limit=2)
    assert [item.slug for item in limited] == ["alpha-smoke-long", "alpha-smoke"]
    assert library.list(limit=0) == []
    assert [item.slug for item in library.list(limit=1, status="archived")] == ["alpha-smoke"]


# ---------------------------------------------------------------------------
# 执行历史
# ---------------------------------------------------------------------------


def test_summary_reports_last_execution_status(repository: CaseRepository, artifact_dir: Path):
    repository.save(_spec(), artifact_dir=artifact_dir)
    assert repository.list()[0].last_execution_status is None

    repository.record_execution(_execution("exec-old", status="failed", started_at=utc_now() - timedelta(minutes=5)))
    assert repository.list()[0].last_execution_status == "failed"

    repository.record_execution(_execution("exec-new", status="passed", passed=True, started_at=utc_now()))
    assert repository.list()[0].last_execution_status == "passed"


def test_record_execution_round_trip_and_upsert(repository: CaseRepository, artifact_dir: Path):
    repository.save(_spec(), artifact_dir=artifact_dir)
    repository.record_execution(_execution("exec-1", status="pending"))

    pending = repository.get_execution("exec-1")
    assert pending is not None
    assert pending.status == "pending"
    assert pending.passed is False
    assert pending.analysis is None
    assert pending.result is None

    analysis = ExecutionAnalysis(
        subject="case_execution",
        subject_id="exec-1",
        bundle_name="com.example.notes",
        device_id="SN-TEST-1",
        healthy=False,
        findings=[
            AnomalyFinding(
                kind=AnomalyKind.WHITE_SCREEN,
                severity="critical",
                summary_zh="疑似白屏",
                source="screenshot",
            )
        ],
        log_coverage="partial",
    )
    repository.record_execution(
        _execution(
            "exec-1",
            status="failed",
            passed=False,
            ended_at=utc_now(),
            report_path="artifacts/cases/case-20240101T000000Z-abc123/v1/hypium/attempt-01/summary_report.html",
            analysis=analysis,
            result={"stdout_tail": "Result: FAIL", "exit_code": 1},
            error="断言失败：登录按钮不存在",
        )
    )

    updated = repository.get_execution("exec-1")
    assert updated is not None
    assert updated.status == "failed"
    assert updated.ended_at is not None
    assert updated.report_path is not None and updated.report_path.endswith("summary_report.html")
    assert updated.analysis is not None
    assert updated.analysis.healthy is False
    assert updated.analysis.log_coverage == "partial"
    assert [item.kind for item in updated.analysis.findings] == [AnomalyKind.WHITE_SCREEN]
    assert updated.result == {"stdout_tail": "Result: FAIL", "exit_code": 1}
    assert updated.error == "断言失败：登录按钮不存在"

    # upsert：同一 execution_id 只保留一行（pending → failed 是更新，不是新增）。
    history = repository.list_executions(CASE_ID)
    assert [item.execution_id for item in history] == ["exec-1"]

    repository.record_execution(_execution("exec-2", status="error", error="xd: 进程退出码 1", ended_at=utc_now()))
    second = repository.get_execution("exec-2")
    assert second is not None
    assert second.error == "xd: 进程退出码 1"


def test_list_executions_is_newest_first_and_honours_limit(repository: CaseRepository, artifact_dir: Path):
    repository.save(_spec(), artifact_dir=artifact_dir)
    now = utc_now()
    repository.record_execution(_execution("exec-1", started_at=now - timedelta(minutes=3)))
    repository.record_execution(_execution("exec-2", started_at=now - timedelta(minutes=2)))
    repository.record_execution(_execution("exec-3", started_at=now - timedelta(minutes=1)))

    history = repository.list_executions(CASE_ID)
    assert [item.execution_id for item in history] == ["exec-3", "exec-2", "exec-1"]
    assert history[0].started_at > history[-1].started_at
    assert [item.execution_id for item in repository.list_executions(CASE_ID, limit=2)] == ["exec-3", "exec-2"]
    assert repository.list_executions(CASE_ID, limit=0) == []


def test_get_execution_unknown_returns_none(repository: CaseRepository):
    assert repository.get_execution("exec-missing") is None


# ---------------------------------------------------------------------------
# patch
# ---------------------------------------------------------------------------


def test_patch_appends_version_and_preserves_untouched_fields(repository: CaseRepository, artifact_dir: Path):
    assert repository.save(_spec(), artifact_dir=artifact_dir) == 1

    assert repository.patch(CASE_ID, slug="login-flow-v2", tags=["smoke", "regression"]) == 2

    latest = repository.latest(CASE_ID)
    assert latest is not None
    assert latest[1] == 2
    assert latest[0].slug == "login-flow-v2"
    assert latest[0].tags == ["smoke", "regression"]
    assert latest[0].title_zh == "登录流程"
    assert latest[3] == artifact_dir

    # v1 行不受 patch 影响。
    version_one = repository.get(CASE_ID, 1)
    assert version_one is not None
    assert version_one[0].slug == "login-flow"
    assert version_one[0].tags == ["smoke"]

    assert repository.patch(CASE_ID, status="archived") == 3
    assert repository.latest(CASE_ID)[2] == "archived"
    # 无字段变更时不新增版本。
    assert repository.patch(CASE_ID) == 3

    assert [item.version for item in repository.list()] == [3]
    assert repository.list()[0].status == "archived"


def test_patch_rejects_unknown_case_and_invalid_slug(repository: CaseRepository, artifact_dir: Path):
    with pytest.raises(KeyError):
        repository.patch("case-20240101T000000Z-ffffff", status="archived")

    repository.save(_spec(), artifact_dir=artifact_dir)
    with pytest.raises(ValidationError):
        repository.patch(CASE_ID, slug="非法 Slug")
    with pytest.raises(ValueError):
        repository.patch(CASE_ID, status="retired")

    assert [item.version for item in repository.versions(CASE_ID)] == [1]


# ---------------------------------------------------------------------------
# 共享数据库文件与并发
# ---------------------------------------------------------------------------


def test_repository_is_reentrant_on_shared_database(tmp_path: Path):
    database_path = tmp_path / "agent.db"
    first = CaseRepository(database_path)
    first.save(_spec(), artifact_dir=tmp_path / "v1")

    # 重新构造不丢数据；与 RunRepository 共用 agent.db 时两者互不破坏。
    second = CaseRepository(database_path)
    RunRepository(database_path)

    latest = second.latest(CASE_ID)
    assert latest is not None
    assert latest[1] == 1
    assert [item.version for item in second.list()] == [1]
    assert first.latest(CASE_ID) is not None


def test_concurrent_saves_do_not_lose_versions(repository: CaseRepository, artifact_dir: Path):
    total = 8
    barrier = threading.Barrier(total)

    def worker(index: int) -> int:
        barrier.wait(timeout=10)
        spec = _spec(title_zh=f"并发用例 {index}")
        return repository.save(spec, artifact_dir=artifact_dir)

    with ThreadPoolExecutor(max_workers=total) as pool:
        versions = list(pool.map(worker, range(total)))

    assert sorted(versions) == list(range(1, total + 1))
    # 8 个互不相同的 spec 必须留下 8 行，且版本号连续无重复。
    assert [item.version for item in repository.versions(CASE_ID)] == list(range(total, 0, -1))
    assert [item.version for item in repository.versions(CASE_ID)] == sorted(
        {item.version for item in repository.versions(CASE_ID)}, reverse=True
    )
    assert repository.latest(CASE_ID) is not None
    assert repository.latest(CASE_ID)[1] == total
