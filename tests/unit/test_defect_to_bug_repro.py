"""``defect_to_bug_repro_request`` 单测（Phase 4.1 / G5）。

关键不变式：``ANOMALY_TO_SYMPTOM`` 必须与 ``cases/bug_repro.py`` 的 ``symptom_sentinel``
表**互为反函数** —— 两张表漂移会让「缺陷一键转复现用例」静默生成测不对症状的用例。
"""

from __future__ import annotations

import pytest

from harmony_test_agent.analysis.defects import (
    ANOMALY_TO_SYMPTOM,
    HARD_FAILURE_SUMMARY,
    record_from_finding,
)
from harmony_test_agent.cases.bug_repro import symptom_sentinel
from harmony_test_agent.models import (
    AnomalyFinding,
    AnomalyKind,
    PlannedStep,
    RunTrace,
    ToolName,
)

BUNDLE = "com.zhihu.hmos"

ALL_KINDS = list(AnomalyKind)


def _finding(
    kind: AnomalyKind,
    *,
    severity: str = "critical",
    summary_zh: str = "动作后前台应用丢失，疑似崩溃",
    action_id: str = "S2",
    detail: str = "cppcrash happened",
) -> AnomalyFinding:
    return AnomalyFinding(
        kind=kind,
        severity=severity,  # type: ignore[arg-type]
        summary_zh=summary_zh,
        detail=detail,
        source="hilog",
        action_id=action_id,
        page_path="pages/Feed",
        phase="in_run",
        evidence={"target": "ui-hot-list", "hilog_relative": "hilog_inrun_S2.txt"},
    )


def _trace(*, plan: list[PlannedStep] | None = None) -> RunTrace:
    return RunTrace(
        run_id="run-1",
        target_app_id="zhihu",
        task="缺陷复现",
        device_id="SN1",
        plan=plan
        or [
            PlannedStep(step_id="S1", instruction="打开应用", tool=ToolName.OPEN_APP),
            PlannedStep(
                step_id="S2",
                instruction="点击热搜第一项",
                tool=ToolName.CLICK_ELEMENT,
                target="ui-hot-list",
                expected="热搜详情页应加载完成",
            ),
            PlannedStep(step_id="S3", instruction="返回首页", tool=ToolName.BACK),
        ],
    )


class TestSymptomMapping:
    #: ``cases/bug_repro.py::symptom_sentinel`` 显式识别的 symptom_kind 词汇表。
    #: 两张表必须用同一套词：``ANOMALY_TO_SYMPTOM`` 的**值域**不得超出它。
    SENTINEL_VOCABULARY = frozenset(
        {"crash", "white_screen", "layout", "freeze", "unresponsive", "functional", "other"}
    )

    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_every_anomaly_kind_maps_to_a_known_symptom_kind(self, kind: AnomalyKind) -> None:
        assert kind in ANOMALY_TO_SYMPTOM
        assert ANOMALY_TO_SYMPTOM[kind] in self.SENTINEL_VOCABULARY

    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_mapping_is_the_inverse_of_the_symptom_sentinel_table(self, kind: AnomalyKind) -> None:
        """两张表必须互为反函数：反向表的取值落进正向表的词汇表，且哨兵对每种症状都可构造。

        正向表是 ``cases/bug_repro.py::symptom_sentinel``（``symptom_kind`` → 检查点草案），
        本模块的是反向表（``AnomalyKind`` → ``symptom_kind``）。漂移会让
        「缺陷一键转复现用例」静默生成测不对症状的用例。
        """
        symptom_kind = ANOMALY_TO_SYMPTOM[kind]

        sentinel = symptom_sentinel(symptom_kind, expected="页面应正常响应")

        assert symptom_kind in self.SENTINEL_VOCABULARY
        assert sentinel.kind is not None
        assert sentinel.message_zh

    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_service_side_symptom_kinds_recognise_the_mapped_kind(self, kind: AnomalyKind) -> None:
        """``analysis/service.py::SYMPTOM_KINDS`` 必须认得反向表的取值（否则重跑判不出复现）。"""
        from harmony_test_agent.analysis.service import SYMPTOM_KINDS

        symptom_kind = ANOMALY_TO_SYMPTOM[kind]

        assert symptom_kind in SYMPTOM_KINDS, f"{symptom_kind} 未进 SYMPTOM_KINDS"
        assert kind in SYMPTOM_KINDS[symptom_kind]

    def test_memory_growth_is_mapped_so_it_can_be_reproduced(self) -> None:
        """缺口 5：``MEMORY_GROWTH`` 原本无法转成复现用例（不在任何症状映射里）。"""
        assert ANOMALY_TO_SYMPTOM[AnomalyKind.MEMORY_GROWTH] == "other"


class TestStepsFromTracePlan:
    def test_steps_are_truncated_at_the_failing_action(self) -> None:
        record = record_from_finding(_finding(AnomalyKind.CPP_CRASH), bundle_name=BUNDLE, run_id="run-1")

        from harmony_test_agent.analysis.defects import defect_to_bug_repro_request

        request = defect_to_bug_repro_request(record, trace=_trace())

        assert request.repro_steps_nl == ["打开应用", "点击热搜第一项"]

    def test_expected_comes_from_the_failing_step(self) -> None:
        from harmony_test_agent.analysis.defects import defect_to_bug_repro_request

        record = record_from_finding(_finding(AnomalyKind.CPP_CRASH), bundle_name=BUNDLE, run_id="run-1")

        request = defect_to_bug_repro_request(record, trace=_trace())

        assert request.expected == "热搜详情页应加载完成"

    def test_missing_expected_falls_back_to_a_readable_default(self) -> None:
        from harmony_test_agent.analysis.defects import defect_to_bug_repro_request

        plan = [PlannedStep(step_id="S2", instruction="点击热搜", tool=ToolName.CLICK_ELEMENT)]
        record = record_from_finding(_finding(AnomalyKind.CPP_CRASH), bundle_name=BUNDLE, run_id="run-1")

        request = defect_to_bug_repro_request(record, trace=_trace(plan=plan))

        assert request.expected == HARD_FAILURE_SUMMARY

    def test_missing_trace_still_yields_usable_steps(self) -> None:
        from harmony_test_agent.analysis.defects import defect_to_bug_repro_request

        record = record_from_finding(_finding(AnomalyKind.CPP_CRASH), bundle_name=BUNDLE, run_id="run-1")

        request = defect_to_bug_repro_request(record, trace=None)

        assert request.repro_steps_nl
        assert request.expected == HARD_FAILURE_SUMMARY

    def test_unknown_action_id_falls_back_to_the_last_plan_step(self) -> None:
        from harmony_test_agent.analysis.defects import defect_to_bug_repro_request

        record = record_from_finding(
            _finding(AnomalyKind.CPP_CRASH, action_id="S99"), bundle_name=BUNDLE, run_id="run-1"
        )

        request = defect_to_bug_repro_request(record, trace=_trace())

        assert request.repro_steps_nl == ["打开应用", "点击热搜第一项", "返回首页"]


class TestRequestFields:
    def test_preconditions_carry_identity_page_and_device(self) -> None:
        from harmony_test_agent.analysis.defects import defect_to_bug_repro_request

        record = record_from_finding(_finding(AnomalyKind.CPP_CRASH), bundle_name=BUNDLE, run_id="run-1")
        record = record.model_copy(update={"device_id": "SN1"}, deep=True)

        request = defect_to_bug_repro_request(record, trace=_trace())

        joined = " ".join(request.preconditions)
        assert BUNDLE in joined
        assert "pages/Feed" in joined
        assert "SN1" in joined

    def test_actual_includes_the_summary_and_evidence_excerpt(self) -> None:
        from harmony_test_agent.analysis.defects import defect_to_bug_repro_request

        record = record_from_finding(_finding(AnomalyKind.CPP_CRASH), bundle_name=BUNDLE, run_id="run-1")

        request = defect_to_bug_repro_request(record, trace=_trace())

        assert "动作后前台应用丢失" in request.actual
        assert "cppcrash happened" in request.actual

    def test_title_matches_the_record_title(self) -> None:
        from harmony_test_agent.analysis.defects import defect_to_bug_repro_request

        record = record_from_finding(_finding(AnomalyKind.CPP_CRASH), bundle_name=BUNDLE, run_id="run-1")

        request = defect_to_bug_repro_request(record, trace=_trace())

        assert request.title == record.title_zh

    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_every_kind_produces_a_valid_request(self, kind: AnomalyKind) -> None:
        from harmony_test_agent.analysis.defects import defect_to_bug_repro_request

        record = record_from_finding(_finding(kind), bundle_name=BUNDLE, run_id="run-1")

        request = defect_to_bug_repro_request(record, trace=_trace())

        assert request.symptom_kind == ANOMALY_TO_SYMPTOM[kind]
        assert request.repro_steps_nl
        assert request.expected
        assert request.actual
