"""Phase 3 验收（G1/G2 的机器可验证形式）：首次运行即产出可复用 Profile，第二次运行复用它。

1. 第一次运行（bootstrap 验证失败降级实时模式）→ ``profiles/draft/<app>.json`` 的
   ``stable_locator_inventory`` 非空且 ``status == "draft"``；
2. 第二次运行 → 发 ``PROFILE_INCREMENTAL``、**不再**发 ``DISCOVERY_STARTED``、
   ``executor.stable_locators`` 非空；
3. 第二次运行后重复定位器的 ``observed_rounds`` 递增（累加生效）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from test_profile_auto_discovery_flow import (
    BUNDLE_A,
    TARGET_A,
    FlowDevice,
    OriginalTaskProvider,
    _discovery_result,
    _settings,
)

from harmony_test_agent.agents import AgentOrchestrator
from harmony_test_agent.models import EventType, ExplorationPolicy, ProfileStatus, RunRequest, RunState
from harmony_test_agent.profiles import ProfileRegistry
from harmony_test_agent.runtime import ToolExecutor
from harmony_test_agent.storage import ArtifactStore, RunRepository


def _partial_verification_result():
    from test_profile_auto_discovery_flow import _verification_result

    return _verification_result(passed=True).model_copy(
        update={"passed": False, "failures": ["verification round did not replay 3 distinct page states: 1 observed"]},
        deep=True,
    )


def _build(tmp_path: Path, device: FlowDevice, monkeypatch, *, patch_bootstrap: bool):
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
    if patch_bootstrap:
        monkeypatch.setattr(
            "harmony_test_agent.agents.orchestrator.BoundedExplorer.explore",
            lambda self: _discovery_result(),
        )
        monkeypatch.setattr(
            "harmony_test_agent.agents.orchestrator.ProfileVerifier.verify",
            lambda self, discovery: _partial_verification_result(),
        )
    return orchestrator


async def test_first_run_yields_reusable_draft_and_second_run_reuses_it(tmp_path: Path, monkeypatch) -> None:
    device = FlowDevice(tmp_path)
    orchestrator = _build(tmp_path, device, monkeypatch, patch_bootstrap=True)

    first = await orchestrator.run(
        RunRequest(target={"bundle_name": BUNDLE_A}, task="check target home", auto_generate=True),
        run_id="run-first-yield",
    )
    assert first.state == RunState.COMPLETED, first.error

    draft_path = orchestrator.settings.resolved_profiles_dir / "draft" / f"{TARGET_A}.json"
    assert draft_path.is_file(), "首次运行必须留下可复用草稿"
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    assert draft["status"] == ProfileStatus.DRAFT
    assert len(draft["stable_locator_inventory"]) >= 1
    assert draft["provenance"]["evidence"]["verification_passed"] is False
    # 绝不因为「有定位器」就伪造验证通过证据。
    registry = ProfileRegistry(orchestrator.settings.resolved_profiles_dir)
    assert not registry.list(ProfileStatus.VERIFIED)

    # ---- 第二次运行：复用草稿，不再做完整 bootstrap 探索 ----
    captured: list[Any] = []

    class RecordingExecutor(ToolExecutor):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            captured.append(self)

    monkeypatch.setattr("harmony_test_agent.agents.orchestrator.ToolExecutor", RecordingExecutor)

    second = await orchestrator.run(
        RunRequest(target={"bundle_name": BUNDLE_A}, task="check target home", auto_generate=True),
        run_id="run-second-reuse",
    )
    assert second.state == RunState.COMPLETED, second.error

    event_types = [event.type for event in second.events]
    assert EventType.PROFILE_INCREMENTAL in event_types
    assert EventType.DISCOVERY_STARTED not in event_types
    assert EventType.PROFILE_LIVE_MODE not in event_types
    # 增量模式不是「复验通过」：数据源必须保持 installed_app。
    assert second.resolved_target is not None
    assert second.resolved_target.source != "verified_profile"

    assert captured, "增量模式仍必须构造 ToolExecutor"
    assert captured[0].stable_locators, "draft 定位器必须作为 executor.stable_locators 被实际使用"
    draft_locator_keys = {item["key"] for item in draft["stable_locator_inventory"] if item["key"]}
    assert {item.key for item in captured[0].stable_locators if item.key} & draft_locator_keys

    merged = json.loads(draft_path.read_text(encoding="utf-8"))
    harvested = [item for item in merged["stable_locator_inventory"] if item.get("source") == "live_task_harvest"]
    assert harvested, "第二次运行必须继续累加任务期证据"
    assert max(item["observed_rounds"] for item in harvested) > 1
    assert any(event.type == EventType.PROFILE_HARVESTED for event in second.events)


async def test_first_run_harvests_task_locators_even_without_bootstrap(tmp_path: Path, monkeypatch) -> None:
    """没有任何探索（exploration 关闭）时，任务期回收仍保证首次运行留下草稿。"""
    device = FlowDevice(tmp_path)
    orchestrator = _build(tmp_path, device, monkeypatch, patch_bootstrap=False)

    trace = await orchestrator.run(
        RunRequest(
            target={"bundle_name": BUNDLE_A},
            task="check target home",
            auto_generate=True,
            exploration_policy=ExplorationPolicy(enabled=False),
        ),
        run_id="run-live-only-yield",
    )
    assert trace.state == RunState.COMPLETED, trace.error
    assert trace.live_mode is True

    draft_path = orchestrator.settings.resolved_profiles_dir / "draft" / f"{TARGET_A}.json"
    assert draft_path.is_file()
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    assert draft["status"] == ProfileStatus.DRAFT
    assert len(draft["stable_locator_inventory"]) >= 1
    assert draft["stable_locator_inventory"][0]["source"] == "live_task_harvest"
