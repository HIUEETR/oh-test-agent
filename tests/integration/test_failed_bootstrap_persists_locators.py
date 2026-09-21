"""Phase 1.2 回归：引导验证失败时也必须把「填充过的」草稿落盘并可复用（计划 R5）。

原实现把 :648 那个 ``verification=None`` 的构造态草稿改写成 INVALID 就落盘，
``_build_profile`` 的守卫使它按构造就是 0 定位器 / 0 断言——即使验证阶段真的采到了
定位器也一样被丢弃。
"""

from __future__ import annotations

import json
from pathlib import Path

from test_profile_auto_discovery_flow import (
    BUNDLE_A,
    TARGET_A,
    FlowDevice,
    OriginalTaskProvider,
    _discovery_result,
    _verification_result,
)

from harmony_test_agent.agents import AgentOrchestrator
from harmony_test_agent.config import Settings
from harmony_test_agent.models import EventType, ProfileStatus, RunRequest, RunState
from harmony_test_agent.profiles import ProfileRegistry
from harmony_test_agent.storage import ArtifactStore, RunRepository


def _partial_verification_result():
    """验证未通过，但已采到定位器与断言证据（部分失败轮次的真实形态）。"""
    base = _verification_result(passed=True)
    return base.model_copy(
        update={
            "passed": False,
            "failures": ["verification round did not replay 3 distinct pages"],
        },
        deep=True,
    )


def _settings(tmp_path: Path, *, harvest: bool = True) -> Settings:
    return Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
        unchanged_screen_limit=2,
        profile_harvest_enabled=harvest,
        # 本文件验证 bootstrap 验证失败时的草稿落盘语义，故显式打开完整探索
        # （计划 4.2 之后任务型运行默认不前置探索）。
        bootstrap_enabled_on_task_run=True,
    )


async def test_failed_bootstrap_persists_locator_filled_draft(tmp_path: Path, monkeypatch) -> None:
    device = FlowDevice(tmp_path)
    settings = _settings(tmp_path)
    orchestrator = AgentOrchestrator(
        settings,
        provider=OriginalTaskProvider(),
        repository=RunRepository(settings.resolved_database_path),
        artifacts=ArtifactStore(settings.resolved_runtime_dir),
        device_factory=lambda _: device,
        settle_seconds=0,
        launch_settle_seconds=0,
    )
    monkeypatch.setattr(
        "harmony_test_agent.agents.orchestrator.BoundedExplorer.explore",
        lambda self: _discovery_result(),
    )
    monkeypatch.setattr(
        "harmony_test_agent.agents.orchestrator.ProfileVerifier.verify",
        lambda self, discovery: _partial_verification_result(),
    )

    trace = await orchestrator.run(
        RunRequest(target={"bundle_name": BUNDLE_A}, task="check target home", auto_generate=True),
        run_id="run-failed-bootstrap-harvest",
    )

    # 验证失败后按既有语义降级为实时模式继续任务，而不是整个 run 失败。
    assert trace.live_mode is True
    assert trace.state == RunState.COMPLETED, trace.error

    draft_path = settings.resolved_profiles_dir / "draft" / f"{TARGET_A}.json"
    assert draft_path.is_file()
    payload = json.loads(draft_path.read_text(encoding="utf-8"))
    assert payload["status"] == ProfileStatus.DRAFT
    assert len(payload["stable_locator_inventory"]) >= 1
    assert len(payload["assertion_inventory"]) >= 1
    assert payload["provenance"]["evidence"]["verification_passed"] is False
    assert payload["provenance"]["evidence"]["verification_failures"]

    # 运行内冻结快照与分析产物路径上的是同一份填充草稿，而不是 0 定位器构造态。
    assert trace.profile_snapshot is not None
    assert trace.profile_snapshot.status == ProfileStatus.DRAFT
    assert len(trace.profile_snapshot.stable_locator_inventory) >= 1
    assert (settings.resolved_runtime_dir / trace.run_id / "discovery" / "draft-profile.json").is_file()

    registry = ProfileRegistry(settings.resolved_profiles_dir)
    assert not registry.list(ProfileStatus.VERIFIED)
    assert registry.get_any(bundle_name=BUNDLE_A) is not None


async def test_failed_bootstrap_without_evidence_stays_invalid(tmp_path: Path, monkeypatch) -> None:
    """完全无定位器时保持 INVALID：如实表达这次探索一无所获。

    关闭任务期回收（``profile_harvest_enabled=False``）以隔离 Phase 1.2 的语义：回收会在
    收尾时把任务期真实定位器累加进来，从而把 INVALID 提升为可复用 DRAFT（见下一个用例）。
    """
    device = FlowDevice(tmp_path)
    settings = _settings(tmp_path, harvest=False)
    orchestrator = AgentOrchestrator(
        settings,
        provider=OriginalTaskProvider(),
        repository=RunRepository(settings.resolved_database_path),
        artifacts=ArtifactStore(settings.resolved_runtime_dir),
        device_factory=lambda _: device,
        settle_seconds=0,
        launch_settle_seconds=0,
    )
    monkeypatch.setattr(
        "harmony_test_agent.agents.orchestrator.BoundedExplorer.explore",
        lambda self: _discovery_result(),
    )
    monkeypatch.setattr(
        "harmony_test_agent.agents.orchestrator.ProfileVerifier.verify",
        lambda self, discovery: _verification_result(passed=False),
    )

    trace = await orchestrator.run(
        RunRequest(target={"bundle_name": BUNDLE_A}, task="check target home", auto_generate=False),
        run_id="run-empty-bootstrap-draft",
    )

    draft_path = settings.resolved_profiles_dir / "draft" / f"{TARGET_A}.json"
    assert draft_path.is_file()
    payload = json.loads(draft_path.read_text(encoding="utf-8"))
    assert payload["status"] == ProfileStatus.INVALID
    assert payload["stable_locator_inventory"] == []
    assert trace.profile_snapshot is not None
    assert trace.profile_snapshot.status == ProfileStatus.INVALID


async def test_task_harvest_promotes_empty_invalid_draft_to_draft(tmp_path: Path, monkeypatch) -> None:
    """任务期回收把「一无所获」的 INVALID 草稿变成可复用 DRAFT（计划 3.3/G1）。"""
    device = FlowDevice(tmp_path)
    settings = _settings(tmp_path)
    orchestrator = AgentOrchestrator(
        settings,
        provider=OriginalTaskProvider(),
        repository=RunRepository(settings.resolved_database_path),
        artifacts=ArtifactStore(settings.resolved_runtime_dir),
        device_factory=lambda _: device,
        settle_seconds=0,
        launch_settle_seconds=0,
    )
    monkeypatch.setattr(
        "harmony_test_agent.agents.orchestrator.BoundedExplorer.explore",
        lambda self: _discovery_result(),
    )
    monkeypatch.setattr(
        "harmony_test_agent.agents.orchestrator.ProfileVerifier.verify",
        lambda self, discovery: _verification_result(passed=False),
    )

    trace = await orchestrator.run(
        RunRequest(target={"bundle_name": BUNDLE_A}, task="check target home", auto_generate=False),
        run_id="run-empty-bootstrap-harvest",
    )

    payload = json.loads((settings.resolved_profiles_dir / "draft" / f"{TARGET_A}.json").read_text(encoding="utf-8"))
    assert payload["status"] == ProfileStatus.DRAFT
    assert len(payload["stable_locator_inventory"]) >= 1
    assert payload["provenance"]["evidence"]["verification_passed"] is False
    assert any(event.type == EventType.PROFILE_HARVESTED for event in trace.events)
    registry = ProfileRegistry(settings.resolved_profiles_dir)
    assert not registry.list(ProfileStatus.VERIFIED)
