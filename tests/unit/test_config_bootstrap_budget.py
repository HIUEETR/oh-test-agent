"""Phase 4.2 回归（R14）：bootstrap 预算与门禁阈值必须可配置。

原先 ``ExplorationPolicy`` 只能按请求设置，``max_duration_seconds`` 默认 900 且没有对应
Settings 项：本次日历运行为此烧掉 473 s 纯探索，且无法用 env 收紧。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from harmony_test_agent.agents import AgentOrchestrator
from harmony_test_agent.config import Settings
from harmony_test_agent.models import ExplorationPolicy
from harmony_test_agent.storage import ArtifactStore, RunRepository


def _orchestrator(tmp_path: Path, **overrides: object) -> AgentOrchestrator:
    settings = Settings(
        _env_file=None,
        agent_provider="mock",
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "home",
        target_profile_path=None,
        **overrides,
    )
    return AgentOrchestrator(
        settings,
        repository=RunRepository(settings.resolved_database_path),
        artifacts=ArtifactStore(settings.resolved_runtime_dir),
    )


def test_bootstrap_defaults_match_plan_and_policy_bounds() -> None:
    settings = Settings(_env_file=None)

    assert settings.bootstrap_enabled_on_task_run is False
    assert settings.bootstrap_max_pages == 8
    assert settings.bootstrap_max_actions_per_page == 6
    assert settings.bootstrap_max_duration_seconds == 300
    assert settings.bootstrap_advisor_enabled is True
    # 任务期默认关闭每帧稳定轮询：真机实测该轮询要再付一次 dumpLayout+cat（≈6s/帧），
    # 而每步动作后已有固定 settle_seconds 等待。
    assert settings.bootstrap_settle_timeout_seconds == 0

    # 默认值必须落在 ExplorationPolicy 的守恒上界内（tests/unit/test_discovery.py 钉住上界）。
    assert 1 <= settings.bootstrap_max_pages <= 20
    assert 1 <= settings.bootstrap_max_actions_per_page <= 8
    assert 1 <= settings.bootstrap_max_duration_seconds <= 900
    assert 0 <= settings.bootstrap_settle_timeout_seconds <= 30


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bootstrap_max_pages", 0),
        ("bootstrap_max_pages", 21),
        ("bootstrap_max_actions_per_page", 0),
        ("bootstrap_max_actions_per_page", 9),
        ("bootstrap_max_duration_seconds", 29),
        ("bootstrap_max_duration_seconds", 901),
    ],
)
def test_bootstrap_budget_is_bounded(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_bootstrap_budget_is_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOTSTRAP_ENABLED_ON_TASK_RUN", "true")
    monkeypatch.setenv("BOOTSTRAP_MAX_PAGES", "4")
    monkeypatch.setenv("BOOTSTRAP_MAX_ACTIONS_PER_PAGE", "3")
    monkeypatch.setenv("BOOTSTRAP_MAX_DURATION_SECONDS", "120")
    monkeypatch.setenv("BOOTSTRAP_ADVISOR_ENABLED", "false")

    settings = Settings(_env_file=None)

    assert settings.bootstrap_enabled_on_task_run is True
    assert settings.bootstrap_max_pages == 4
    assert settings.bootstrap_max_actions_per_page == 3
    assert settings.bootstrap_max_duration_seconds == 120
    assert settings.bootstrap_advisor_enabled is False


def test_profile_gate_thresholds_are_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROFILE_MIN_STABLE_LOCATORS", "2")
    monkeypatch.setenv("PROFILE_MIN_PAGE_STATES", "1")
    monkeypatch.setenv("PROFILE_MIN_ASSERTIONS", "1")

    settings = Settings(_env_file=None)

    assert settings.profile_min_stable_locators == 2
    assert settings.profile_min_page_states == 1
    assert settings.profile_min_assertions == 1


def test_orchestrator_injects_bootstrap_budget_and_request_wins(tmp_path: Path) -> None:
    orchestrator = _orchestrator(
        tmp_path, bootstrap_max_pages=4, bootstrap_max_duration_seconds=60, bootstrap_settle_timeout_seconds=0
    )

    injected = orchestrator._bootstrap_policy(ExplorationPolicy())

    assert injected.max_pages == 4
    assert injected.max_duration_seconds == 60
    assert injected.max_actions_per_page == 6
    assert injected.settle_timeout_seconds == 0

    explicit = orchestrator._bootstrap_policy(
        ExplorationPolicy(max_pages=12, max_duration_seconds=800, settle_timeout_seconds=2)
    )

    assert explicit.max_pages == 12
    assert explicit.max_duration_seconds == 800
    assert explicit.settle_timeout_seconds == 2
    # 未显式给出的字段仍按预算收紧。
    assert explicit.max_actions_per_page == 6


async def test_task_run_skips_bootstrap_by_default(tmp_path: Path) -> None:
    """默认配置下任务型运行不再前置完整探索（无 DISCOVERY_STARTED）。"""
    from fakes import FakeLiveDevice, FixedPlanProvider

    from harmony_test_agent.models import EventType, PlannedStep, RunRequest, ToolName

    orchestrator = _orchestrator(tmp_path, bootstrap_enabled_on_task_run=False)
    device = FakeLiveDevice()
    orchestrator.device_factory = lambda _: device
    orchestrator.provider = FixedPlanProvider(
        [PlannedStep(step_id="s1", instruction="打开日历", tool=ToolName.OPEN_APP)]
    )

    trace = await orchestrator.run(
        RunRequest(target={"app_name": "日历"}, task="打开日历", auto_generate=False),
        run_id="run-no-bootstrap",
    )

    assert trace.live_mode is True
    event_types = [event.type for event in trace.events]
    assert EventType.DISCOVERY_STARTED not in event_types


async def test_task_run_keeps_bootstrap_when_explicitly_enabled(tmp_path: Path) -> None:
    from fakes import FakeLiveDevice, FixedPlanProvider

    from harmony_test_agent.models import EventType, PlannedStep, RunRequest, ToolName

    orchestrator = _orchestrator(tmp_path, bootstrap_enabled_on_task_run=True)
    device = FakeLiveDevice()
    orchestrator.device_factory = lambda _: device
    orchestrator.provider = FixedPlanProvider(
        [PlannedStep(step_id="s1", instruction="打开日历", tool=ToolName.OPEN_APP)]
    )

    trace = await orchestrator.run(
        RunRequest(target={"app_name": "日历"}, task="打开日历", auto_generate=False),
        run_id="run-with-bootstrap",
    )

    # FakeLiveDevice 的探索只能得到单页：探索确实开始了（事件存在），随后按既有语义降级。
    assert EventType.DISCOVERY_STARTED in [event.type for event in trace.events]
