"""DC 蒸馏的两处「失败被抹掉」（Phase 3.5）。

缺口 5 末条：失败的断言与失败的 invocation 在产物层面被丢掉两次 ——
``cases/builder.py`` 直接 omit（理由 ``"invocation failed"``），``dc/distill.py`` 只取
``inv.success`` 且把 ``passed`` **硬编码成 True**。「我测出了一个 bug」因此被彻底抹掉。
"""

from __future__ import annotations

import json
from pathlib import Path

from harmony_test_agent.analysis.defects import DefectRecorder
from harmony_test_agent.cases.builder import CaseBuilder, CaseBuildResult
from harmony_test_agent.dc.models import DcToolInvocation, DcToolName, DcToolStatus, DcToolTier
from harmony_test_agent.models import utc_now
from harmony_test_agent.storage import DefectRepository

BUNDLE = "com.zhihu.hmos"


def _invocation(
    tool: DcToolName,
    *,
    success: bool,
    status: DcToolStatus | None = None,
    args: dict | None = None,
    page_path: str = "pages/Feed",
    error: str = "",
    invocation_id: str = "inv-1",
) -> DcToolInvocation:
    return DcToolInvocation(
        invocation_id=invocation_id,
        turn_id="turn-1",
        tool=tool,
        tier=DcToolTier.L1,
        args=args or {"target": "ui-hot-list"},
        success=success,
        status=status or (DcToolStatus.SUCCEEDED if success else DcToolStatus.FAILED),
        started_at=utc_now(),
        ended_at=utc_now(),
        page_path=page_path,
        error=error or None,
    )


def _distiller():
    """构造一个不跑 ``__init__`` 的 distiller：这些方法只依赖传入参数。"""
    from harmony_test_agent.dc.distill import DcProfileDistiller

    return DcProfileDistiller.__new__(DcProfileDistiller)


class TestAssertionObservationsUseTheRealResult:
    def test_passed_reflects_the_real_status_not_a_hardcoded_true(self) -> None:
        """``passed`` 必须是真实结果 —— 历史实现硬编码 ``True``，掩盖 ``success``/``status`` 矛盾。"""
        distiller = _distiller()
        invocation = _invocation(
            DcToolName.ASSERT_TEXT,
            success=True,
            status=DcToolStatus.TIMED_OUT,
            args={"text": "热搜"},
        )

        observations = distiller._assertion_observations([invocation])

        assert len(observations) == 1
        assert observations[0].passed is False, "status=TIMED_OUT 的断言不得标成通过"

    def test_successful_assertion_is_kept(self) -> None:
        distiller = _distiller()
        invocation = _invocation(DcToolName.ASSERT_VISIBLE, success=True, args={"target": "ui-present"})

        observations = distiller._assertion_observations([invocation])

        assert len(observations) == 1
        assert observations[0].passed is True
        assert observations[0].target == "ui-present"

    def test_non_assert_tools_are_ignored(self) -> None:
        distiller = _distiller()
        invocation = _invocation(DcToolName.CLICK, success=True, args={"x": 1, "y": 2})

        assert distiller._assertion_observations([invocation]) == []


class TestFailedAssertionsAreRecorded:
    def test_failed_assertion_enters_the_failed_observation_list(self) -> None:
        distiller = _distiller()
        invocations = [
            _invocation(
                DcToolName.ASSERT_VISIBLE,
                success=False,
                args={"target": "ui-missing"},
                error="element not found: 热搜",
                page_path="pages/Search",
            )
        ]

        entries = distiller._failed_assertion_observations(invocations)

        assert len(entries) == 1
        assert entries[0]["tool"] == "assert_visible"
        assert entries[0]["target"] == "ui-missing"
        assert entries[0]["page_path"] == "pages/Search"
        assert "element not found" in entries[0]["error"]
        assert entries[0]["status"] == "failed"

    def test_successful_assertions_are_not_in_the_failed_list(self) -> None:
        distiller = _distiller()
        invocations = [_invocation(DcToolName.ASSERT_VISIBLE, success=True, args={"target": "ui-present"})]

        assert distiller._failed_assertion_observations(invocations) == []


def _build_dc(invocations: list[DcToolInvocation]) -> CaseBuildResult:
    builder = CaseBuilder(min_observed_rounds=1)
    return builder.from_dc_invocations(
        "dc-1",
        "SN1",
        invocations,
        bundle_name=BUNDLE,
        main_ability="EntryAbility",
        snapshots=[],
    )


class TestBuilderSourceFailures:
    def test_failed_invocation_is_omitted_but_recorded(self) -> None:
        """失败动作不进脚本（脚本里确实不该有失败动作），但必须留痕。"""
        invocations = [
            _invocation(DcToolName.CLICK, success=False, args={"x": 1, "y": 2}, error="device_error: rejected"),
            _invocation(DcToolName.CLICK, success=True, args={"x": 10, "y": 20}, invocation_id="inv-2"),
        ]

        built = _build_dc(invocations)

        reasons = [item["reason"] for item in built.omitted_actions]
        assert "invocation failed" in reasons
        assert len(built.source_failures) == 1
        failure = built.source_failures[0]
        assert failure["tool"] == "click"
        assert failure["status"] == "failed"
        assert "rejected" in failure["error"]

    def test_clean_recording_has_no_source_failures(self) -> None:
        built = _build_dc([_invocation(DcToolName.CLICK, success=True, args={"x": 10, "y": 20})])

        assert built.source_failures == []


class TestSourceFailuresArePersisted:
    def test_config_json_carries_source_failures(self, tmp_path: Path) -> None:
        """失败留痕必须写进 case 的 config JSON（可追溯）。"""
        from harmony_test_agent.cases.library import CaseLibrary
        from harmony_test_agent.storage.case_repository import CaseRepository

        built = _build_dc(
            [
                _invocation(DcToolName.CLICK, success=False, args={"x": 1, "y": 2}, error="device_error"),
                _invocation(DcToolName.CLICK, success=True, args={"x": 10, "y": 20}, invocation_id="inv-2"),
            ]
        )
        cases_root = tmp_path / "cases"
        library = CaseLibrary(CaseRepository(tmp_path / "agent.db"), cases_root, min_observed_rounds=1)

        record = library.save_built(built, device_sn="SN1")

        version_dir = cases_root / record.case_id / f"v{record.version}" / "standalone"
        configs = sorted(version_dir.glob("*.json"))
        assert configs, "standalone 配置必须存在"
        payload = json.loads(configs[0].read_text(encoding="utf-8"))
        assert payload["source_failures"]
        assert payload["source_failures"][0]["tool"] == "click"


def _reproduce_spec(defect_id: str, *, case_id: str = "case-20260101T000000Z-abc123", with_tag: bool = True):
    from harmony_test_agent.cases.spec import (
        BugReproSpec,
        ScenarioKind,
        StepAction,
        TestCaseSpec,
        TestStepSpec,
    )

    return TestCaseSpec(
        case_id=case_id,
        slug=f"case-{case_id.rsplit('-', 1)[-1]}",
        title_zh="复现：点击后页面无响应",
        bundle_name=BUNDLE,
        main_ability="EntryAbility",
        # draft 状态：本文件只验证「重跑结果 → 缺陷状态」的回写映射，不构造完整 IR 检查点。
        status="draft",
        scenario=ScenarioKind.BUG_REPRODUCTION if with_tag else ScenarioKind.CORE_FLOW,
        tags=[f"defect:{defect_id}"] if with_tag else [],
        steps=[TestStepSpec(step_id="s1", index=1, action=StepAction.BACK, title_zh="返回首页")],
        bug_repro=(
            BugReproSpec(
                symptom="点击后页面无响应",
                symptom_kind="unresponsive",
                repro_steps_nl=["打开应用", "点击热搜"],
                expected="页面应正常响应",
                actual="结构停滞",
            )
            if with_tag
            else None
        ),
        provenance={"source_kind": "bug_report", "source_id": defect_id},
    )


class TestReRunWriteback:
    def _library(self, tmp_path: Path, repository: DefectRepository):
        from harmony_test_agent.cases.library import CaseLibrary
        from harmony_test_agent.storage.case_repository import CaseRepository

        return CaseLibrary(
            CaseRepository(tmp_path / "agent.db"),
            tmp_path / "cases",
            min_observed_rounds=1,
            defect_repository=repository,
        )

    def _seed_defect(self, repository: DefectRepository) -> str:
        from harmony_test_agent.models import AnomalyFinding, AnomalyKind

        finding = AnomalyFinding(
            kind=AnomalyKind.PAGE_UNRESPONSIVE,
            severity="critical",
            summary_zh="点击后页面无响应",
            detail="结构停滞",
            source="screenshot",
            action_id="step-1",
            page_path="pages/Feed",
            phase="in_run",
            evidence={"target": "ui-hot-list"},
        )
        record = DefectRecorder(repository).record_one(finding, bundle_name=BUNDLE, run_id="run-1")
        assert record is not None
        return record.defect_id

    def _execution(self, spec, *, execution_id: str, status: str, passed: bool, analysis=None):
        from harmony_test_agent.cases.spec import CaseExecutionRecord

        return CaseExecutionRecord(
            execution_id=execution_id,
            case_id=spec.case_id,
            version=1,
            engine="hypium_standalone",
            device_id="SN1",
            status=status,
            passed=passed,
            started_at=utc_now(),
            analysis=analysis,
        )

    def _analysis(self, **overrides):
        from harmony_test_agent.models import ExecutionAnalysis

        payload = {
            "subject": "case_execution",
            "subject_id": "exec-1",
            "symptom_reproduced": None,
        }
        payload.update(overrides)
        return ExecutionAnalysis(**payload)  # type: ignore[arg-type]

    def test_symptom_reproduced_true_confirms_the_defect(self, tmp_path: Path) -> None:
        from harmony_test_agent.analysis.defects import DefectStatus

        repository = DefectRepository(tmp_path / "agent.db")
        defect_id = self._seed_defect(repository)
        library = self._library(tmp_path, repository)
        spec = _reproduce_spec(defect_id)
        execution = self._execution(
            spec,
            execution_id="exec-1",
            status="passed",
            passed=True,
            analysis=self._analysis(symptom_reproduced=True),
        )

        library._record_defects_for_execution(spec=spec, execution=execution)

        stored = repository.get(defect_id)
        assert stored is not None
        assert stored.status is DefectStatus.CONFIRMED
        assert stored.repro_execution_id == "exec-1"

    def test_symptom_reproduced_false_marks_not_reproduced(self, tmp_path: Path) -> None:
        from harmony_test_agent.analysis.defects import DefectStatus

        repository = DefectRepository(tmp_path / "agent.db")
        defect_id = self._seed_defect(repository)
        library = self._library(tmp_path, repository)
        spec = _reproduce_spec(defect_id)
        execution = self._execution(
            spec,
            execution_id="exec-2",
            status="failed",
            passed=False,
            analysis=self._analysis(subject_id="exec-2", symptom_reproduced=False),
        )

        library._record_defects_for_execution(spec=spec, execution=execution)

        stored = repository.get(defect_id)
        assert stored is not None
        assert stored.status is DefectStatus.NOT_REPRODUCED

    def test_unknown_conclusion_keeps_suspected_and_records_notes(self, tmp_path: Path) -> None:
        from harmony_test_agent.analysis.defects import DefectStatus

        repository = DefectRepository(tmp_path / "agent.db")
        defect_id = self._seed_defect(repository)
        library = self._library(tmp_path, repository)
        spec = _reproduce_spec(defect_id)
        execution = self._execution(
            spec,
            execution_id="exec-3",
            status="error",
            passed=False,
            analysis=self._analysis(subject_id="exec-3", symptom_reproduced=None),
        )

        library._record_defects_for_execution(spec=spec, execution=execution)

        stored = repository.get(defect_id)
        assert stored is not None
        assert stored.status is DefectStatus.SUSPECTED
        assert "结论未知" in stored.notes

    def test_defect_is_never_auto_confirmed_without_analysis(self, tmp_path: Path) -> None:
        """没有分析结果时绝不能把缺陷标成 confirmed（分析是唯一判据）。"""
        from harmony_test_agent.analysis.defects import DefectStatus

        repository = DefectRepository(tmp_path / "agent.db")
        defect_id = self._seed_defect(repository)
        library = self._library(tmp_path, repository)
        spec = _reproduce_spec(defect_id)
        execution = self._execution(spec, execution_id="exec-6", status="passed", passed=True, analysis=None)

        library._record_defects_for_execution(spec=spec, execution=execution)

        stored = repository.get(defect_id)
        assert stored is not None
        assert stored.status is DefectStatus.SUSPECTED

    def test_no_defect_tag_is_a_noop(self, tmp_path: Path) -> None:
        repository = DefectRepository(tmp_path / "agent.db")
        defect_id = self._seed_defect(repository)
        library = self._library(tmp_path, repository)
        spec = _reproduce_spec(defect_id, case_id="case-20260101T000000Z-def456", with_tag=False)
        execution = self._execution(
            spec,
            execution_id="exec-4",
            status="passed",
            passed=True,
            analysis=self._analysis(subject_id="exec-4", symptom_reproduced=True),
        )

        library._record_defects_for_execution(spec=spec, execution=execution)

        stored = repository.get(defect_id)
        assert stored is not None
        assert stored.status.value == "suspected"

    def test_rereun_findings_are_recorded_as_defects(self, tmp_path: Path) -> None:
        """重跑发现的异常同样入库（不只服务复现用例）。"""
        from harmony_test_agent.models import AnomalyFinding, AnomalyKind

        repository = DefectRepository(tmp_path / "agent.db")
        library = self._library(tmp_path, repository)
        spec = _reproduce_spec("defect-unused", case_id="case-20260101T000000Z-fff999", with_tag=False)
        finding = AnomalyFinding(
            kind=AnomalyKind.CPP_CRASH,
            severity="critical",
            summary_zh="hilog 中出现 C++ 崩溃",
            source="hilog",
            page_path="pages/Feed",
            action_id="step-9",
            evidence={"target": "ui-x"},
        )
        execution = self._execution(
            spec,
            execution_id="exec-5",
            status="failed",
            passed=False,
            analysis=self._analysis(subject_id="exec-5", findings=[finding]),
        )

        library._record_defects_for_execution(spec=spec, execution=execution)

        summaries = repository.list(bundle_name=BUNDLE)
        assert summaries and summaries[0].kind.value == "cppcrash"
        assert summaries[0].case_id == spec.case_id

    def test_missing_repository_is_a_noop(self, tmp_path: Path) -> None:
        """未注入缺陷仓库时（单测默认 / 关掉分析）本链路必须完全静默。"""
        from harmony_test_agent.cases.library import CaseLibrary
        from harmony_test_agent.storage.case_repository import CaseRepository

        library = CaseLibrary(CaseRepository(tmp_path / "agent.db"), tmp_path / "cases", min_observed_rounds=1)
        spec = _reproduce_spec("defect-x")
        execution = self._execution(
            spec,
            execution_id="exec-7",
            status="passed",
            passed=True,
            analysis=self._analysis(symptom_reproduced=True),
        )

        library._record_defects_for_execution(spec=spec, execution=execution)  # 不应抛异常
