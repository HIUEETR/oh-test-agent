"""报告缺陷对账 + 绕路留痕（Phase 3.3 / G3 / G4）。

缺口 5 的核心：**一个 run 若 agent 步骤都跑完但分析抓到 critical ``cppcrash``，
报告顶部状态是 completed、下面是一张红色异常表 —— 没有任何逻辑把两者联系起来。**
本文件把「对账横幅」钉成机器可证。
"""

from __future__ import annotations

import json
from pathlib import Path

from harmony_test_agent.models import (
    ActionResult,
    AnomalyFinding,
    AnomalyKind,
    ExecutionAnalysis,
    RunState,
    RunTrace,
    ToolName,
    WorkaroundRecord,
)
from harmony_test_agent.reporting import ReportBuilder
from harmony_test_agent.storage import ArtifactStore

RUN_ID = "run-defects"
BUNDLE = "com.zhihu.hmos"

RECONCILE = "用例通过 ≠ 应用无缺陷"


def _finding(
    kind: AnomalyKind = AnomalyKind.CPP_CRASH,
    *,
    severity: str = "critical",
    summary_zh: str = "hilog 中出现 C++ 崩溃",
    phase: str = "post_hoc",
    action_id: str = "",
    screenshot: str = "",
) -> AnomalyFinding:
    return AnomalyFinding(
        kind=kind,
        severity=severity,  # type: ignore[arg-type]
        summary_zh=summary_zh,
        detail="cppcrash happened",
        source="hilog",
        action_id=action_id,
        page_path="pages/Feed",
        screenshot=screenshot,
        phase=phase,  # type: ignore[arg-type]
        evidence={"matched_line": "cppcrash happened", "hilog_relative": "hilog_inrun_step-1.txt"},
    )


def _trace(
    *,
    state: RunState = RunState.COMPLETED,
    defects: list[AnomalyFinding] | None = None,
    analysis: ExecutionAnalysis | None = None,
    workaround_count: int = 0,
    actions: list[ActionResult] | None = None,
) -> RunTrace:
    return RunTrace(
        run_id=RUN_ID,
        target_app_id="zhihu",
        task="缺陷对账",
        device_id="SN1",
        state=state,
        defects=defects or [],
        analysis=analysis,
        workaround_count=workaround_count,
        actions=actions or [],
    )


def _build(tmp_path: Path, trace: RunTrace) -> str:
    store = ArtifactStore(tmp_path / "runs")
    path = ReportBuilder(store).build(trace)
    return path.read_text(encoding="utf-8")


class TestReconcileBanner:
    def test_completed_run_with_critical_finding_shows_the_banner(self, tmp_path: Path) -> None:
        """G3 的机器证明：completed + critical ⇒ 顶部出现对账横幅。"""
        trace = _trace(analysis=ExecutionAnalysis(subject="live_run", subject_id=RUN_ID, findings=[_finding()]))

        document = _build(tmp_path, trace)

        assert RECONCILE in document
        assert "1 项 critical 异常" in document
        assert "banner-bad" in document

    def test_banner_appears_before_the_profile_bootstrap_section(self, tmp_path: Path) -> None:
        """插入位置：``{gate_markup}`` 之前，确保第一眼看到。"""
        trace = _trace(analysis=ExecutionAnalysis(subject="live_run", subject_id=RUN_ID, findings=[_finding()]))

        document = _build(tmp_path, trace)

        assert document.index(RECONCILE) < document.index("Profile bootstrap")

    def test_in_run_defect_alone_also_triggers_the_banner(self, tmp_path: Path) -> None:
        """运行中发现的 critical 缺陷同样必须对账（不依赖事后分析）。"""
        trace = _trace(defects=[_finding(phase="in_run", action_id="step-2")])

        document = _build(tmp_path, trace)

        assert RECONCILE in document

    def test_warning_only_findings_do_not_show_the_banner(self, tmp_path: Path) -> None:
        """只在存在 critical 时上横幅（计划风险 7：避免让 completed 看起来像失败）。"""
        trace = _trace(
            analysis=ExecutionAnalysis(
                subject="live_run",
                subject_id=RUN_ID,
                findings=[_finding(kind=AnomalyKind.WHITE_SCREEN, severity="warning")],
            )
        )

        document = _build(tmp_path, trace)

        assert RECONCILE not in document

    def test_failed_run_does_not_show_the_reconcile_banner(self, tmp_path: Path) -> None:
        """run 本身失败时不需要对账提示（失败已经写在状态里）。"""
        trace = _trace(
            state=RunState.FAILED_SCRIPT,
            analysis=ExecutionAnalysis(subject="live_run", subject_id=RUN_ID, findings=[_finding()]),
        )

        document = _build(tmp_path, trace)

        assert RECONCILE not in document

    def test_clean_run_has_neither_banner_nor_defect_section(self, tmp_path: Path) -> None:
        """既无缺陷也无分析 ⇒ 两个新章节都不出现（既有报告输出不变）。"""
        document = _build(tmp_path, _trace())

        assert RECONCILE not in document
        assert "疑似应用缺陷" not in document
        assert "执行结果分析" not in document


class TestWorkaroundBanner:
    def test_workaround_count_renders_a_warning_banner(self, tmp_path: Path) -> None:
        trace = _trace(workaround_count=3)

        document = _build(tmp_path, trace)

        assert "3 个步骤是靠恢复循环绕路完成的" in document
        assert "banner-warn" in document

    def test_zero_workarounds_render_no_banner(self, tmp_path: Path) -> None:
        document = _build(tmp_path, _trace())

        assert "靠恢复循环绕路完成" not in document


class TestStepCardRendersFailuresAndWorkarounds:
    def _action(self, **overrides) -> ActionResult:
        payload = {
            "step_id": "step-3",
            "tool": ToolName.CLICK_ELEMENT,
            "params": {"target": "ui-hot-list"},
            "success": True,
        }
        payload.update(overrides)
        return ActionResult(**payload)

    def test_failed_step_renders_its_error(self, tmp_path: Path) -> None:
        """历史实现完全不渲染 ``action.error``，失败原因在报告里不可见。"""
        action = self._action(success=False, error="element not found: 热搜")

        document = _build(tmp_path, _trace(actions=[action]))

        assert "✗ 失败：element not found: 热搜" in document

    def test_workaround_is_rendered_with_the_original_error(self, tmp_path: Path) -> None:
        action = self._action(
            workaround=WorkaroundRecord(
                original_step_id="step-3",
                original_instruction="点击热搜",
                original_error="element not found: 热搜",
                corrective_tool="click_element",
                corrective_target="ui-feed-card",
            )
        )

        document = _build(tmp_path, _trace(actions=[action], workaround_count=1))

        assert "↳ 绕路" in document
        assert "click_element → ui-feed-card" in document
        assert "element not found: 热搜" in document

    def test_action_anomaly_is_rendered(self, tmp_path: Path) -> None:
        action = self._action(anomaly=_finding(kind=AnomalyKind.PAGE_UNRESPONSIVE, severity="warning", phase="in_run"))

        document = _build(tmp_path, _trace(actions=[action]))

        assert "运行中发现异常" in document
        assert "page_unresponsive" in document


class TestDefectSection:
    def test_defect_section_lists_id_kind_severity_and_title(self, tmp_path: Path) -> None:
        trace = _trace(analysis=ExecutionAnalysis(subject="live_run", subject_id=RUN_ID, findings=[_finding()]))

        document = _build(tmp_path, trace)

        assert "<h2>疑似应用缺陷</h2>" in document
        assert "defect-" in document
        assert "cppcrash" in document
        assert "hilog 中出现 C++ 崩溃" in document

    def test_critical_rows_are_red_and_warning_rows_are_yellow(self, tmp_path: Path) -> None:
        trace = _trace(
            analysis=ExecutionAnalysis(
                subject="live_run",
                subject_id=RUN_ID,
                findings=[
                    _finding(),
                    _finding(
                        kind=AnomalyKind.WHITE_SCREEN,
                        severity="warning",
                        summary_zh="检测到白屏",
                    ),
                ],
            )
        )

        document = _build(tmp_path, trace)

        assert "defect-critical" in document
        assert "defect-warning" in document

    def test_defects_are_sorted_by_severity(self, tmp_path: Path) -> None:
        trace = _trace(
            analysis=ExecutionAnalysis(
                subject="live_run",
                subject_id=RUN_ID,
                findings=[
                    _finding(kind=AnomalyKind.WHITE_SCREEN, severity="warning", summary_zh="白屏"),
                    _finding(kind=AnomalyKind.CPP_CRASH, severity="critical", summary_zh="崩溃"),
                ],
            )
        )

        document = _build(tmp_path, trace)

        section = document[document.index("疑似应用缺陷") :]
        assert section.index("崩溃") < section.index("白屏")

    def test_analysis_table_shows_the_phase_column(self, tmp_path: Path) -> None:
        trace = _trace(
            analysis=ExecutionAnalysis(
                subject="live_run",
                subject_id=RUN_ID,
                findings=[_finding(phase="in_run"), _finding(kind=AnomalyKind.JS_CRASH, phase="post_hoc")],
            )
        )

        document = _build(tmp_path, trace)

        assert "<th>阶段</th>" in document
        assert "运行中" in document
        assert "事后" in document

    def test_json_report_carries_defects_and_workaround_count(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path / "runs")
        trace = _trace(defects=[_finding(phase="in_run")], workaround_count=2)
        path = ReportBuilder(store).build(trace)

        payload = json.loads((path.parent / "report.json").read_text(encoding="utf-8"))

        assert payload["defects"][0]["phase"] == "in_run"
        assert payload["workaround_count"] == 2
