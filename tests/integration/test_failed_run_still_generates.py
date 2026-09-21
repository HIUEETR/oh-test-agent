"""Phase 0.3 回归：断言失败运行同样必须收尾产物（计划 R3，直击「23 分钟零产物」）。"""

from __future__ import annotations

from pathlib import Path

from fakes import FakeLiveDevice, FixedPlanProvider

from harmony_test_agent.agents import AgentOrchestrator
from harmony_test_agent.config import Settings
from harmony_test_agent.models import (
    EventType,
    ExplorationPolicy,
    PlannedStep,
    RunRequest,
    RunState,
    TargetQuery,
    ToolName,
)
from harmony_test_agent.storage import ArtifactStore, RunRepository

STEPS = [
    PlannedStep(step_id="step-01", instruction="启动日历", tool=ToolName.OPEN_APP),
    PlannedStep(step_id="step-02", instruction="切换到月视图", tool=ToolName.CLICK_ELEMENT, target="tabs_month"),
    PlannedStep(
        step_id="step-03",
        instruction="断言不存在的控件可见",
        tool=ToolName.ASSERT_VISIBLE,
        target="不存在的控件",
    ),
    PlannedStep(step_id="step-99", instruction="结束任务", tool=ToolName.FINISH),
]


async def test_failed_run_still_generates_diagnostic_artifacts(tmp_path: Path) -> None:
    settings = Settings(
        agent_provider="mock",
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "home",
        target_profile_path=None,
        agent_step_recovery_limit=0,
    )
    device = FakeLiveDevice()
    orchestrator = AgentOrchestrator(
        settings,
        provider=FixedPlanProvider(STEPS),
        repository=RunRepository(settings.resolved_database_path),
        artifacts=ArtifactStore(settings.resolved_runtime_dir),
        device_factory=lambda _: device,
        settle_seconds=0,
        launch_settle_seconds=0,
    )
    trace = await orchestrator.run(
        RunRequest(
            target=TargetQuery(app_name="日历"),
            task="打开日历并切换到月视图",
            auto_generate=True,
            exploration_policy=ExplorationPolicy(enabled=False, settle_timeout_seconds=0),
        )
    )

    assert trace.state == RunState.FAILED_ASSERTION
    assert trace.error and "target is not in current screen" in trace.error

    # 失败运行照样留下脚本、用例 IR 与报告：本次实测 13 个成功动作、7 个真实定位器全被丢弃。
    generated = trace.generated
    assert generated is not None
    assert generated.purpose == "diagnostic"
    assert generated.replay_eligible is False
    assert generated.python_path.exists()
    assert generated.case_spec_path is not None and generated.case_spec_path.exists()
    assert generated.included_action_count >= 1

    run_dir = settings.resolved_runtime_dir / trace.run_id
    assert (run_dir / "reports" / "report.html").exists()
    assert (run_dir / "generated").is_dir()

    event_types = [event.type for event in trace.events]
    assert EventType.SCRIPT_GENERATED in event_types
    # 失败事件按失败类型区分：断言失败发 ASSERTION_FAILED，其余失败发 RUN_FAILED。
    assert EventType.ASSERTION_FAILED in event_types
    assert trace.agent_outcome == "failed"


async def test_failure_before_any_action_does_not_force_generation(tmp_path: Path) -> None:
    """目标解析阶段就失败（零成功动作）时不生成无意义脚本。"""
    settings = Settings(
        agent_provider="mock",
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "home",
        target_profile_path=None,
    )
    device = FakeLiveDevice()
    orchestrator = AgentOrchestrator(
        settings,
        provider=FixedPlanProvider(STEPS),
        repository=RunRepository(settings.resolved_database_path),
        artifacts=ArtifactStore(settings.resolved_runtime_dir),
        device_factory=lambda _: device,
        settle_seconds=0,
        launch_settle_seconds=0,
    )
    trace = await orchestrator.run(
        RunRequest(
            target=TargetQuery(app_name="不存在的应用"),
            task="打开日历",
            exploration_policy=ExplorationPolicy(enabled=False, settle_timeout_seconds=0),
        )
    )

    assert trace.state == RunState.FAILED_TARGET_RESOLUTION
    assert trace.actions == []
    assert trace.generated is None
