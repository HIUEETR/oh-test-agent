"""``DefectRepository`` + ``DefectRecorder`` 单测：归并、过滤、并发与字段映射。

缺口 5 的核心断言：同一应用同一页面同一动作用同一类异常反复触发 ⇒ ``occurrences`` 累加
而**不新增行**；「偶现 / 必现」因此天然可区分。
"""

from __future__ import annotations

import threading
from pathlib import Path

from harmony_test_agent.analysis.defects import (
    DefectRecorder,
    DefectStatus,
    merge_key,
    record_from_finding,
    stable_defect_id,
)
from harmony_test_agent.models import AnomalyFinding, AnomalyKind
from harmony_test_agent.storage import DefectRepository

BUNDLE = "com.zhihu.hmos"


def _finding(
    kind: AnomalyKind = AnomalyKind.PAGE_UNRESPONSIVE,
    *,
    severity: str = "warning",
    summary_zh: str = "点击后页面结构无变化",
    detail: str = "",
    page_path: str = "pages/Feed",
    action_id: str = "step-1",
    target: str = "ui-hot-list",
    screenshot: str = "",
    **evidence,
) -> AnomalyFinding:
    payload = {"page_path": page_path, "target": target, "action_id": action_id, **evidence}
    return AnomalyFinding(
        kind=kind,
        severity=severity,  # type: ignore[arg-type]
        summary_zh=summary_zh,
        detail=detail,
        source="screenshot",
        page_path=page_path,
        action_id=action_id,
        screenshot=screenshot,
        phase="in_run",
        evidence=payload,
    )


def _repository(tmp_path: Path) -> DefectRepository:
    return DefectRepository(tmp_path / "agent.db")


class TestMergeKey:
    def test_key_is_bundle_kind_page_and_action_target(self) -> None:
        key = merge_key(_finding(), BUNDLE)

        assert key == (BUNDLE, "page_unresponsive", "pages/Feed", "ui-hot-list")

    def test_key_falls_back_to_evidence_bundle_when_argument_is_empty(self) -> None:
        finding = _finding(bundle_name=BUNDLE)

        assert merge_key(finding, "")[0] == BUNDLE

    def test_stable_id_is_deterministic(self) -> None:
        key = merge_key(_finding(), BUNDLE)

        assert stable_defect_id(key) == stable_defect_id(key)
        assert stable_defect_id(key).startswith("defect-")


class TestRecorderMerge:
    def test_second_observation_merges_and_counts_occurrences(self, tmp_path: Path) -> None:
        repository = _repository(tmp_path)
        recorder = DefectRecorder(repository)

        first = recorder.record_one(_finding(), bundle_name=BUNDLE, run_id="run-1")
        second = recorder.record_one(_finding(detail="第二次"), bundle_name=BUNDLE, run_id="run-2")

        assert first is not None and second is not None
        assert first.defect_id == second.defect_id
        assert repository.count() == 1
        stored = repository.get(first.defect_id)
        assert stored is not None
        assert stored.occurrences == 2
        assert stored.last_seen_at >= stored.first_seen_at
        assert len(stored.findings) == 2
        assert stored.run_id == "run-2"

    def test_different_action_target_creates_a_new_record(self, tmp_path: Path) -> None:
        repository = _repository(tmp_path)
        recorder = DefectRecorder(repository)

        recorder.record_one(_finding(target="ui-a"), bundle_name=BUNDLE)
        recorder.record_one(_finding(target="ui-b"), bundle_name=BUNDLE)

        assert repository.count() == 2

    def test_different_page_creates_a_new_record(self, tmp_path: Path) -> None:
        repository = _repository(tmp_path)
        recorder = DefectRecorder(repository)

        recorder.record_one(_finding(page_path="pages/A"), bundle_name=BUNDLE)
        recorder.record_one(_finding(page_path="pages/B"), bundle_name=BUNDLE)

        assert repository.count() == 2

    def test_merge_keeps_the_more_severe_level(self, tmp_path: Path) -> None:
        repository = _repository(tmp_path)
        recorder = DefectRecorder(repository)

        recorder.record_one(_finding(severity="warning"), bundle_name=BUNDLE)
        merged = recorder.record_one(_finding(severity="critical"), bundle_name=BUNDLE)

        assert merged is not None and merged.severity == "critical"

    def test_findings_are_capped_at_twenty(self, tmp_path: Path) -> None:
        from harmony_test_agent.analysis.defects import MAX_FINDINGS_PER_DEFECT

        repository = _repository(tmp_path)
        recorder = DefectRecorder(repository)
        for index in range(MAX_FINDINGS_PER_DEFECT + 5):
            recorder.record_one(_finding(detail=f"第 {index} 次"), bundle_name=BUNDLE)

        record = repository.list(bundle_name=BUNDLE)[0]
        stored = repository.get(record.defect_id)
        assert stored is not None
        assert len(stored.findings) == MAX_FINDINGS_PER_DEFECT
        assert stored.occurrences == MAX_FINDINGS_PER_DEFECT + 5

    def test_record_from_findings_returns_one_record_per_defect(self, tmp_path: Path) -> None:
        repository = _repository(tmp_path)
        recorder = DefectRecorder(repository)

        records = recorder.record_from_findings(
            findings=[
                _finding(target="ui-a"),
                _finding(target="ui-b"),
                _finding(kind=AnomalyKind.CPP_CRASH, severity="critical", target="ui-c"),
            ],
            bundle_name=BUNDLE,
            run_id="run-1",
        )

        assert len(records) == 3
        assert repository.count() == 3

    def test_recorder_survives_a_broken_repository(self) -> None:
        class Broken:
            def find_by_merge_key(self, key):
                raise RuntimeError("db is gone")

            def get(self, defect_id):
                raise RuntimeError("db is gone")

        records = DefectRecorder(Broken()).record_from_findings(findings=[_finding()], bundle_name=BUNDLE)

        assert records == []


class TestFieldMapping:
    def test_record_from_finding_maps_every_field(self) -> None:
        finding = _finding(screenshot="screens/step-1.png")

        record = record_from_finding(finding, bundle_name=BUNDLE, run_id="run-9", device_id="SN1")

        assert record.bundle_name == BUNDLE
        assert record.run_id == "run-9"
        assert record.device_id == "SN1"
        assert record.kind is AnomalyKind.PAGE_UNRESPONSIVE
        assert record.severity == "warning"
        assert record.page_path == "pages/Feed"
        assert record.action_id == "step-1"
        assert record.status is DefectStatus.SUSPECTED
        assert record.occurrences == 1
        assert "screens/step-1.png" in record.evidence_paths
        assert record.defect_id == finding.defect_id

    def test_title_falls_back_to_kind_when_summary_is_empty(self) -> None:
        finding = _finding(summary_zh="")

        record = record_from_finding(finding, bundle_name=BUNDLE)

        assert "page_unresponsive" in record.title_zh


class TestRepositoryQueries:
    def test_filters_by_bundle_kind_severity_status(self, tmp_path: Path) -> None:
        repository = _repository(tmp_path)
        recorder = DefectRecorder(repository)
        recorder.record_one(_finding(severity="warning"), bundle_name=BUNDLE)
        recorder.record_one(_finding(kind=AnomalyKind.CPP_CRASH, severity="critical"), bundle_name="com.other.app")

        assert len(repository.list(bundle_name=BUNDLE)) == 1
        assert len(repository.list(kind="cppcrash")) == 1
        assert len(repository.list(severity="critical")) == 1
        assert len(repository.list(status="suspected")) == 2
        assert len(repository.list(status="dismissed")) == 0

    def test_limit_is_respected(self, tmp_path: Path) -> None:
        repository = _repository(tmp_path)
        recorder = DefectRecorder(repository)
        for index in range(5):
            recorder.record_one(_finding(target=f"ui-{index}"), bundle_name=BUNDLE)

        assert len(repository.list(bundle_name=BUNDLE, limit=2)) == 2
        assert repository.list(bundle_name=BUNDLE, limit=0) == []

    def test_lookup_by_run_and_case(self, tmp_path: Path) -> None:
        repository = _repository(tmp_path)
        recorder = DefectRecorder(repository)
        recorder.record_one(_finding(target="ui-a"), bundle_name=BUNDLE, run_id="run-1")
        recorder.record_one(_finding(target="ui-b"), bundle_name=BUNDLE, run_id="run-2", case_id="case-1")

        assert len(repository.list_for_run("run-1")) == 1
        assert len(repository.list_for_run("run-2")) == 1
        assert len(repository.list_for_case("case-1")) == 1

    def test_patch_sets_status_and_notes(self, tmp_path: Path) -> None:
        repository = _repository(tmp_path)
        record = DefectRecorder(repository).record_one(_finding(), bundle_name=BUNDLE)
        assert record is not None

        updated = repository.patch(record.defect_id, status=DefectStatus.DISMISSED, notes="误报：动画帧")

        assert updated is not None
        assert updated.status is DefectStatus.DISMISSED
        assert updated.notes == "误报：动画帧"
        assert repository.patch("defect-absent", status=DefectStatus.CONFIRMED) is None

    def test_attach_repro_case(self, tmp_path: Path) -> None:
        repository = _repository(tmp_path)
        record = DefectRecorder(repository).record_one(_finding(), bundle_name=BUNDLE)
        assert record is not None

        updated = repository.attach_repro_case(record.defect_id, case_id="case-9", execution_id="exec-9")

        assert updated is not None and updated.repro_case_id == "case-9"
        assert updated.repro_execution_id == "exec-9"
        # 重跑结果未知时状态保持 suspected
        assert updated.status is DefectStatus.SUSPECTED


class TestRepositoryDurability:
    def test_reopening_the_database_keeps_records(self, tmp_path: Path) -> None:
        path = tmp_path / "agent.db"
        recorder = DefectRecorder(DefectRepository(path))
        record = recorder.record_one(_finding(), bundle_name=BUNDLE)
        assert record is not None

        reopened = DefectRepository(path)

        assert reopened.count() == 1
        assert reopened.get(record.defect_id) is not None

    def test_shares_the_database_with_run_and_case_repositories(self, tmp_path: Path) -> None:
        """三个仓库必须能安全共用同一个 ``agent.db``（计划风险 6）。"""
        from harmony_test_agent.storage import CaseRepository, RunRepository

        path = tmp_path / "agent.db"
        RunRepository(path)
        CaseRepository(path)
        defects = DefectRepository(path)
        recorder = DefectRecorder(defects)

        recorder.record_one(_finding(), bundle_name=BUNDLE)

        assert RunRepository(path).list_runs(limit=1) == []
        assert CaseRepository(path).list(limit=1) == []
        assert defects.count() == 1

    def test_concurrent_writes_do_not_lose_records(self, tmp_path: Path) -> None:
        """RLock + 单连接：多线程并发写同一缺陷只应累加 occurrences。"""
        repository = _repository(tmp_path)
        recorder = DefectRecorder(repository)
        errors: list[Exception] = []

        def worker() -> None:
            try:
                for _ in range(5):
                    recorder.record_one(_finding(), bundle_name=BUNDLE)
            except Exception as exc:  # noqa: BLE001 - 收集后统一断言
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert repository.count() == 1
        record = repository.get(repository.list(bundle_name=BUNDLE)[0].defect_id)
        assert record is not None
        assert record.occurrences == 20
