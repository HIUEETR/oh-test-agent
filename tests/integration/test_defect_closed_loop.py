"""缺陷 → 复现用例闭环的集成测试（Phase 4 / G5）。

一条 critical finding 必须能一键转成 :class:`BugReproRequest` → 生成确认用例 → 重跑 →
``symptom_reproduced`` 回写 ``DefectRecord.status``，给出「已复现 / 未复现」。
"""

from __future__ import annotations

from pathlib import Path

from harmony_test_agent.analysis.defects import (
    DefectRecorder,
    DefectStatus,
    defect_to_bug_repro_request,
)
from harmony_test_agent.cases.builder import CaseBuilder
from harmony_test_agent.cases.library import DEFECT_TAG_PREFIX, CaseLibrary
from harmony_test_agent.models import AnomalyFinding, AnomalyKind, ExecutionAnalysis
from harmony_test_agent.storage import CaseRepository, DefectRepository

BUNDLE = "com.zhihu.hmos"


def _seed_defect(repository: DefectRepository, *, run_id: str = "run-1") -> tuple[str, AnomalyFinding]:
    finding = AnomalyFinding(
        kind=AnomalyKind.PAGE_UNRESPONSIVE,
        severity="critical",
        summary_zh="点击「热搜」第 1 项后页面无响应",
        detail="动作 step-2 后页面结构指纹与动作前完全相同",
        source="screenshot",
        action_id="step-2",
        page_path="pages/Feed",
        phase="in_run",
        evidence={"target": "ui-hot-list-item-1"},
    )
    record = DefectRecorder(repository).record_one(finding, bundle_name=BUNDLE, run_id=run_id, device_id="SN1")
    assert record is not None
    return record.defect_id, finding


def _library(tmp_path: Path, repository: DefectRepository) -> CaseLibrary:
    return CaseLibrary(
        CaseRepository(tmp_path / "agent.db"),
        tmp_path / "cases",
        min_observed_rounds=1,
        defect_repository=repository,
    )


class TestClosedLoop:
    def test_defect_to_case_to_rereun_to_confirmed(self, tmp_path: Path) -> None:
        """完整闭环：运行中缺陷 → 转复现用例（带 defect 标签）→ 重跑判为已复现 → confirmed。"""
        repository = DefectRepository(tmp_path / "agent.db")
        defect_id, finding = _seed_defect(repository)
        library = _library(tmp_path, repository)

        # 1) 缺陷 → BugReproRequest（走真实的转换函数）
        record = repository.get(defect_id)
        assert record is not None
        request = defect_to_bug_repro_request(record, trace=None)
        assert request.symptom_kind == "unresponsive"
        assert request.repro_steps_nl

        # 2) 构建并保存复现用例，并把 defect:<id> 记进标签
        builder = CaseBuilder(min_observed_rounds=1)
        plan = _bug_repro_plan(request)
        built = builder.from_bug_repro(plan, request, None)
        built.spec.tags = [*built.spec.tags, f"{DEFECT_TAG_PREFIX}{defect_id}"]
        case_record = library.save_built(built, device_sn="SN1")
        repository.attach_repro_case(defect_id, case_id=case_record.case_id)

        stored = repository.get(defect_id)
        assert stored is not None
        assert stored.repro_case_id == case_record.case_id

        # 3) 重跑判为「已复现」（分析给出 symptom_reproduced=True）
        execution = _execution(case_record, symptom_reproduced=True)
        library._record_defects_for_execution(spec=built.spec, execution=execution)

        final = repository.get(defect_id)
        assert final is not None
        assert final.status is DefectStatus.CONFIRMED
        assert final.repro_execution_id == execution.execution_id
        # 证据链保留（即使被 confirmed，findings 也不丢）
        assert final.findings and final.findings[0].kind is finding.kind

    def test_not_reproduced_path(self, tmp_path: Path) -> None:
        repository = DefectRepository(tmp_path / "agent.db")
        defect_id, _finding = _seed_defect(repository)
        library = _library(tmp_path, repository)
        record = repository.get(defect_id)
        assert record is not None
        request = defect_to_bug_repro_request(record, trace=None)
        built = CaseBuilder(min_observed_rounds=1).from_bug_repro(_bug_repro_plan(request), request, None)
        built.spec.tags = [*built.spec.tags, f"{DEFECT_TAG_PREFIX}{defect_id}"]
        case_record = library.save_built(built, device_sn="SN1")

        library._record_defects_for_execution(
            spec=built.spec, execution=_execution(case_record, symptom_reproduced=False)
        )

        final = repository.get(defect_id)
        assert final is not None
        assert final.status is DefectStatus.NOT_REPRODUCED

    def test_second_observation_after_confirmation_keeps_evidence(self, tmp_path: Path) -> None:
        """确认后的缺陷再次被观测：``occurrences`` 继续累加，状态不被覆盖回 suspected。"""
        repository = DefectRepository(tmp_path / "agent.db")
        defect_id, finding = _seed_defect(repository)
        library = _library(tmp_path, repository)
        record = repository.get(defect_id)
        assert record is not None
        request = defect_to_bug_repro_request(record, trace=None)
        built = CaseBuilder(min_observed_rounds=1).from_bug_repro(_bug_repro_plan(request), request, None)
        built.spec.tags = [*built.spec.tags, f"{DEFECT_TAG_PREFIX}{defect_id}"]
        case_record = library.save_built(built, device_sn="SN1")
        library._record_defects_for_execution(
            spec=built.spec, execution=_execution(case_record, symptom_reproduced=True)
        )

        DefectRecorder(repository).record_one(finding, bundle_name=BUNDLE, run_id="run-2")

        final = repository.get(defect_id)
        assert final is not None
        assert final.occurrences == 2
        assert final.status is DefectStatus.CONFIRMED


def _bug_repro_plan(request) -> object:
    """构造一份最小复现计划（与 ``api/cases.py::_plan_bug_repro`` 产物同形）。

    本测试关注的是缺陷状态机闭环，不关注计划质量，因此直接给确定性计划 +
    由 ``symptom_sentinel`` 生成的症状检查点。
    """
    from harmony_test_agent.cases.bug_repro import BugReproPlan, symptom_sentinel
    from harmony_test_agent.models import PlannedStep, ToolName

    plan = BugReproPlan(
        title_zh=request.title,
        steps=[
            PlannedStep(step_id="S1", instruction="点击热搜第 1 项", tool=ToolName.CLICK_ELEMENT, target="ui-hot-list"),
        ],
        model_used="test",
        mock=True,
    )
    plan.symptom_checkpoint = symptom_sentinel(request.symptom_kind, request.expected)
    return plan


def _execution(case_record, *, symptom_reproduced: bool):
    from harmony_test_agent.cases.spec import CaseExecutionRecord
    from harmony_test_agent.models import utc_now

    return CaseExecutionRecord(
        execution_id=f"exec-{case_record.case_id.rsplit('-', 1)[-1]}",
        case_id=case_record.case_id,
        version=case_record.version,
        engine="hypium_standalone",
        device_id="SN1",
        status="passed" if symptom_reproduced else "failed",
        passed=symptom_reproduced,
        started_at=utc_now(),
        analysis=ExecutionAnalysis(
            subject="case_execution",
            subject_id=f"exec-{case_record.case_id.rsplit('-', 1)[-1]}",
            symptom_reproduced=symptom_reproduced,
        ),
    )
