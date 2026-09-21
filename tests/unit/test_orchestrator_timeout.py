"""Phase 0.1 回归：``provider.decide`` 超时必须被捕获并带可读错误，绝不能冒泡成 unexpected error。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fakes import FakeLiveDevice, FixedPlanProvider

from harmony_test_agent.agents import AgentOrchestrator
from harmony_test_agent.config import Settings
from harmony_test_agent.models import (
    ExplorationPolicy,
    PlannedStep,
    RunRequest,
    RunState,
    ScreenSnapshot,
    TargetQuery,
    ToolDecision,
    ToolName,
)
from harmony_test_agent.storage import ArtifactStore, RunRepository

STEPS = [
    PlannedStep(step_id="step-01", instruction="启动日历", tool=ToolName.OPEN_APP),
    PlannedStep(step_id="step-99", instruction="结束任务", tool=ToolName.FINISH),
]


class TimeoutProvider(FixedPlanProvider):
    """前 ``timeouts`` 次 ``decide`` 直接抛 ``TimeoutError``，模拟一次慢到超时的模型调用。"""

    def __init__(self, steps: list[PlannedStep], timeouts: int) -> None:
        super().__init__(steps)
        self.timeouts = timeouts
        self.decide_calls = 0
        self.feedbacks: list[str | None] = []

    async def decide(
        self,
        step: PlannedStep,
        snapshot: ScreenSnapshot | None,
        feedback: str | None = None,
    ) -> ToolDecision:
        self.decide_calls += 1
        self.feedbacks.append(feedback)
        if self.decide_calls <= self.timeouts:
            raise TimeoutError
        return await super().decide(step, snapshot, feedback)


def build_settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(
        agent_provider="mock",
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "home",
        target_profile_path=None,
        agent_model_timeout=90,
        **overrides,
    )


async def run_orchestrator(tmp_path: Path, provider: Any, **overrides: Any) -> Any:
    settings = build_settings(tmp_path, **overrides)
    device = FakeLiveDevice()
    orchestrator = AgentOrchestrator(
        settings,
        provider=provider,
        repository=RunRepository(settings.resolved_database_path),
        artifacts=ArtifactStore(settings.resolved_runtime_dir),
        device_factory=lambda _: device,
        settle_seconds=0,
        launch_settle_seconds=0,
    )
    return await orchestrator.run(
        RunRequest(
            target=TargetQuery(app_name="日历"),
            task="打开日历",
            auto_generate=True,
            exploration_policy=ExplorationPolicy(enabled=False, settle_timeout_seconds=0),
        )
    )


async def test_decide_timeout_becomes_readable_failed_model(tmp_path: Path) -> None:
    provider = TimeoutProvider(STEPS, timeouts=99)
    trace = await run_orchestrator(tmp_path, provider, agent_model_retry_limit=0)

    assert trace.state == RunState.FAILED_MODEL
    assert trace.error == "model decision timed out after 90 seconds"
    assert "unexpected error" not in (trace.error or "")
    assert provider.decide_calls == 1


async def test_decide_timeout_retries_once_with_convergence_hint(tmp_path: Path) -> None:
    provider = TimeoutProvider(STEPS, timeouts=1)
    trace = await run_orchestrator(tmp_path, provider, agent_model_retry_limit=1)

    assert trace.state == RunState.COMPLETED, trace.error
    assert provider.decide_calls >= 2
    assert provider.feedbacks[0] is None
    assert "timed out" in (provider.feedbacks[1] or "")


async def test_decide_timeout_exhausts_retries_then_fails(tmp_path: Path) -> None:
    provider = TimeoutProvider(STEPS, timeouts=99)
    trace = await run_orchestrator(tmp_path, provider, agent_model_retry_limit=1)

    assert trace.state == RunState.FAILED_MODEL
    assert trace.error == "model decision timed out after 90 seconds"
    assert provider.decide_calls == 2


@pytest.mark.parametrize("retry_limit", [0, 1, 3])
async def test_retry_limit_bounds_decide_attempts(tmp_path: Path, retry_limit: int) -> None:
    provider = TimeoutProvider(STEPS, timeouts=99)
    trace = await run_orchestrator(tmp_path, provider, agent_model_retry_limit=retry_limit)

    assert trace.state == RunState.FAILED_MODEL
    assert provider.decide_calls == retry_limit + 1


class _ProviderOutage(RuntimeError):
    """模拟 provider 侧故障（真机实测 429 rate_limit_error）。"""


class OutageProvider(FixedPlanProvider):
    name = "outage"
    mock = True

    def __init__(self, steps: list[PlannedStep]) -> None:
        super().__init__(steps)
        self.calls = 0

    async def decide(
        self,
        step: PlannedStep,
        snapshot: ScreenSnapshot | None,
        feedback: str | None = None,
    ) -> ToolDecision:
        self.calls += 1
        raise _ProviderOutage("status_code: 429 rate_limit_error")


async def test_provider_outage_is_reported_as_failed_model(tmp_path: Path) -> None:
    """模型侧故障（429/5xx）必须归类为 FAILED_MODEL 且错误可读，而不是 "unexpected error"。"""
    provider = OutageProvider(STEPS)

    trace = await run_orchestrator(tmp_path, provider, agent_model_retry_backoff_seconds=0)

    assert trace.state == RunState.FAILED_MODEL
    assert "model call failed" in (trace.error or "")
    assert "429" in (trace.error or "")
    assert "unexpected error" not in (trace.error or "")


class FlakyProvider(FixedPlanProvider):
    """前 ``failures`` 次 ``decide`` 抛瞬时 429，之后正常：模拟网关间歇性限流。"""

    def __init__(self, steps: list[PlannedStep], failures: int) -> None:
        super().__init__(steps)
        self.failures = failures
        self.calls = 0

    async def decide(
        self,
        step: PlannedStep,
        snapshot: ScreenSnapshot | None,
        feedback: str | None = None,
    ) -> ToolDecision:
        self.calls += 1
        if self.calls <= self.failures:
            raise _ProviderOutage(
                "status_code: 429, body: {'message': 'Upstream model provider is temporarily unavailable.'}"
            )
        return await super().decide(step, snapshot, feedback)


async def test_transient_provider_outage_is_retried_instead_of_failing_the_run(tmp_path: Path) -> None:
    """一次 429 不该报废整轮运行：退避后重试成功，运行继续（真机两次日历运行都被 429 打断）。"""
    provider = FlakyProvider(STEPS, failures=1)

    trace = await run_orchestrator(tmp_path, provider, agent_model_retry_limit=1, agent_model_retry_backoff_seconds=0)

    assert trace.state == RunState.COMPLETED, trace.error
    # 1 次瞬时 429 + 重试成功 + 第 2 个计划步骤（finish）的决策 = 3 次；若不做瞬时重试则整轮失败。
    assert provider.calls == 3


async def test_transient_retry_is_bounded_by_retry_limit(tmp_path: Path) -> None:
    """持续 429 时重试次数受 ``agent_model_retry_limit`` 约束，不做无界重试。"""
    provider = FlakyProvider(STEPS, failures=99)

    trace = await run_orchestrator(tmp_path, provider, agent_model_retry_limit=1, agent_model_retry_backoff_seconds=0)

    assert trace.state == RunState.FAILED_MODEL
    assert provider.calls == 2
