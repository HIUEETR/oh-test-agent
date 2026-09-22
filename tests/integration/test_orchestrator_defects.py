"""``AgentOrchestrator`` 的缺陷落库对账（Phase 3.2 / G3）。

钉住三件事：

1. 运行中发现的 finding 与事后分析的 finding 走**同一个 recorder**，同 key 归并成一条；
2. 发 ``DEFECT_RECORDED`` 事件；
3. 缺陷落库失败绝不影响运行结论（advisory）。
"""

from __future__ import annotations

from pathlib import Path

from harmony_test_agent.agents import AgentOrchestrator
from harmony_test_agent.agents.providers import MockAgentProvider
from harmony_test_agent.analysis.defects import DefectRecorder
from harmony_test_agent.config import Settings
from harmony_test_agent.models import (
    AnomalyFinding,
    AnomalyKind,
    EventType,
    RunState,
    RunTrace,
)
from harmony_test_agent.storage import ArtifactStore, DefectRepository, RunRepository

BUNDLE = "com.zhihu.hmos"


def _settings(tmp_path: Path, **overrides) -> Settings:
    payload = {
        "agent_provider": "mock",
        "runtime_dir": tmp_path / "runs",
        "database_path": tmp_path / "agent.db",
        "target_profile_path": None,
        "profiles_dir": tmp_path / "profiles",
        "runtime_home": tmp_path / "home",
        "case_analysis_enabled": False,
    }
    payload.update(overrides)
    return Settings(**payload)


class _Emitter:
    def __init__(self) -> None:
        self.events: list[tuple[EventType, str, dict]] = []

    def emit(self, event_type: EventType, message: str, payload: dict | None = None) -> None:
        self.events.append((event_type, message, payload or {}))

    def defects(self) -> list[dict]:
        return [payload for event_type, _message, payload in self.events if event_type is EventType.DEFECT_RECORDED]


def _orchestrator(tmp_path: Path, *, recorder=None, settings=None) -> AgentOrchestrator:
    resolved = settings or _settings(tmp_path)
    return AgentOrchestrator(
        resolved,
        provider=MockAgentProvider(),
        repository=RunRepository(resolved.resolved_database_path),
        artifacts=ArtifactStore(resolved.resolved_runtime_dir),
        device_factory=lambda _: None,  # 本文件不跑任务循环，只测收尾对账
        defect_recorder=recorder,
    )


def _finding(
    kind: AnomalyKind = AnomalyKind.PAGE_UNRESPONSIVE,
    *,
    severity: str = "critical",
    phase: str = "in_run",
    action_id: str = "S2",
) -> AnomalyFinding:
    return AnomalyFinding(
        kind=kind,
        severity=severity,  # type: ignore[arg-type]
        summary_zh="点击后页面结构无变化",
        detail="结构停滞",
        source="screenshot",
        action_id=action_id,
        page_path="pages/Feed",
        phase=phase,  # type: ignore[arg-type]
        evidence={"target": "ui-hot-list"},
    )


def _trace(*, defects: list[AnomalyFinding] | None = None, analysis=None) -> RunTrace:
    return RunTrace(
        run_id="run-1",
        target_app_id="zhihu",
        task="缺陷对账",
        device_id="SN1",
        state=RunState.COMPLETED,
        defects=defects or [],
        analysis=analysis,
    )


class TestDefectRecording:
    def test_in_run_and_post_hoc_findings_merge_into_one_defect(self, tmp_path: Path) -> None:
        from harmony_test_agent.models import ExecutionAnalysis

        repository = DefectRepository(tmp_path / "agent.db")
        recorder = DefectRecorder(repository)
        orchestrator = _orchestrator(tmp_path, recorder=recorder)
        emitter = _Emitter()
        trace = _trace(
            defects=[_finding(phase="in_run")],
            analysis=ExecutionAnalysis(
                subject="live_run",
                subject_id="run-1",
                findings=[_finding(phase="post_hoc")],
            ),
        )

        orchestrator._record_defects(trace, emitter, trace.analysis)

        assert repository.count() == 1
        record = repository.get(repository.list()[0].defect_id)
        assert record is not None
        assert record.occurrences == 2
        events = emitter.defects()
        assert len(events) == 1
        assert events[0]["defect_id"] == record.defect_id

    def test_no_findings_records_nothing(self, tmp_path: Path) -> None:
        repository = DefectRepository(tmp_path / "agent.db")
        orchestrator = _orchestrator(tmp_path, recorder=DefectRecorder(repository))
        emitter = _Emitter()

        orchestrator._record_defects(_trace(), emitter, None)

        assert repository.count() == 0
        assert emitter.defects() == []

    def test_missing_recorder_is_a_noop(self, tmp_path: Path) -> None:
        orchestrator = _orchestrator(tmp_path, recorder=None)
        emitter = _Emitter()
        trace = _trace(defects=[_finding()])

        orchestrator._record_defects(trace, emitter, None)

        assert emitter.defects() == []

    def test_broken_recorder_never_breaks_the_run(self, tmp_path: Path) -> None:
        class Broken:
            def record_from_findings(self, **_kwargs):
                raise RuntimeError("db is gone")

        orchestrator = _orchestrator(tmp_path, recorder=Broken())
        emitter = _Emitter()

        orchestrator._record_defects(_trace(defects=[_finding()]), emitter, None)  # 不应抛异常

        assert emitter.defects() == []

    def test_attach_analysis_without_analyzer_still_records_in_run_defects(self, tmp_path: Path) -> None:
        """分析器不可用（未注入）时，运行中发现的缺陷仍必须落库。"""
        repository = DefectRepository(tmp_path / "agent.db")
        orchestrator = _orchestrator(tmp_path, recorder=DefectRecorder(repository))
        assert orchestrator.analyzer is None
        emitter = _Emitter()
        trace = _trace(defects=[_finding()])

        orchestrator._attach_analysis(trace, emitter)

        assert repository.count() == 1
        assert trace.analysis is None
        assert emitter.defects()


class TestAnalysisMergeSemantics:
    def test_analysis_failure_does_not_prevent_defect_recording(self, tmp_path: Path) -> None:
        class BrokenAnalyzer:
            def analyze_run(self, *_args, **_kwargs):
                raise RuntimeError("analyzer exploded")

        repository = DefectRepository(tmp_path / "agent.db")
        settings = _settings(tmp_path)
        orchestrator = AgentOrchestrator(
            settings,
            provider=MockAgentProvider(),
            repository=RunRepository(settings.resolved_database_path),
            artifacts=ArtifactStore(settings.resolved_runtime_dir),
            device_factory=lambda _: None,
            analyzer=BrokenAnalyzer(),
            defect_recorder=DefectRecorder(repository),
        )
        emitter = _Emitter()
        trace = _trace(defects=[_finding()])

        orchestrator._attach_analysis(trace, emitter)

        assert trace.analysis is None
        assert repository.count() == 1


class TestReportReconcilesTheTrace:
    def test_report_shows_the_banner_from_trace_defects_only(self, tmp_path: Path) -> None:
        """不注入 recorder 时报告仍然对账（``ReportBuilder`` 自己算缺陷）。"""
        from harmony_test_agent.reporting import ReportBuilder

        artifacts = ArtifactStore(tmp_path / "runs")
        trace = _trace(defects=[_finding()])

        path = ReportBuilder(artifacts).build(trace)

        document = path.read_text(encoding="utf-8")
        assert "用例通过 ≠ 应用无缺陷" in document
        assert "疑似应用缺陷" in document
