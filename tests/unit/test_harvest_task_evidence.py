"""Phase 3.3 回归：任务期证据回收（计划 R5/R3 的最大遗漏收益）。

用真实日历 fixture 构造一份「精简版本次日历 trace」：13 个成功动作里 7 个解析出了真实
key，另外 3 个是时钟/日期格/列表实例 ID 等易变 key，全部曾经被直接丢弃。
"""

from __future__ import annotations

import json
from pathlib import Path

from fakes import CALENDAR_FIXTURES, calendar_foreground, snapshot_from_layout

from harmony_test_agent.agents import AgentOrchestrator
from harmony_test_agent.agents.orchestrator import HarvestedLocator
from harmony_test_agent.config import Settings
from harmony_test_agent.models import (
    ActionResult,
    AssertionResult,
    ConfidenceLevel,
    EventType,
    LocatorCandidate,
    LocatorKind,
    ProfileStatus,
    RunTrace,
    TargetAppProfile,
    ToolName,
)
from harmony_test_agent.profiles import ProfileRegistry
from harmony_test_agent.runtime import RunEventEmitter
from harmony_test_agent.storage import ArtifactStore, RunRepository

T0 = CALENDAR_FIXTURES / "calendar-home-t0.json"

#: 本次日历运行里真实解析成功、可以长期复用的 key。
REUSABLE_KEYS = [
    "tabs_month",
    "phone_add_agenda",
    "new_event",
    "add_agenda_add_remind",
    "add_agenda_title-1789951623657",
    "add_custom_reminder_row",
    "month_view_swiper",
]

#: 明确不该入库的易变 key（时钟 / 日期格 / 列表实例 ID）。
VOLATILE_KEYS = [
    "TimeView_Text_timeText",
    "22___十二_",
    "normal_agenda_list_item193",
]


def _profile() -> TargetAppProfile:
    return TargetAppProfile(
        target_app_id="com-huawei-hmos-calendar",
        display_name="日历",
        bundle_name="com.huawei.hmos.calendar",
        main_ability="EntryAbility",
        provenance={"discovery_run_id": "run-calendar"},
    )


def _trace(snapshot_id: str, *, run_id: str = "run-calendar-harvest") -> RunTrace:
    snapshot = snapshot_from_layout(T0, run_id=run_id)
    actions: list[ActionResult] = []
    for index, key in enumerate([*REUSABLE_KEYS, *VOLATILE_KEYS], start=1):
        actions.append(
            ActionResult(
                step_id=f"step-{index:02d}",
                tool=ToolName.CLICK_ELEMENT,
                params={"target": f"element-{index}"},
                success=True,
                before_snapshot_id=snapshot_id,
                locator=LocatorCandidate(kind=LocatorKind.KEY, value=key),
            )
        )
    actions.append(
        ActionResult(
            step_id="step-99",
            tool=ToolName.ASSERT_VISIBLE,
            params={"target": "tabs_month"},
            success=True,
            before_snapshot_id=snapshot_id,
            locator=LocatorCandidate(kind=LocatorKind.KEY, value="tabs_month"),
            assertion=AssertionResult(
                kind=ToolName.ASSERT_VISIBLE, target="tabs_month", passed=True, message="assertion passed"
            ),
        )
    )
    return RunTrace(
        run_id=run_id,
        target_app_id="com-huawei-hmos-calendar",
        task="打开日历并切换月视图",
        device_id="fake-calendar",
        snapshots=[snapshot.model_copy(update={"snapshot_id": snapshot_id}, deep=True)],
        actions=actions,
        assertions=[
            AssertionResult(kind=ToolName.ASSERT_VISIBLE, target="tabs_month", passed=True, message="assertion passed")
        ],
    )


def _harness(tmp_path: Path):
    settings = Settings(
        agent_provider="mock",
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "home",
        target_profile_path=None,
    )
    orchestrator = AgentOrchestrator(
        settings,
        repository=RunRepository(settings.resolved_database_path),
        artifacts=ArtifactStore(settings.resolved_runtime_dir),
    )
    registry = ProfileRegistry(settings.resolved_profiles_dir)
    return orchestrator, settings, registry


def test_collect_task_evidence_keeps_reusable_keys_and_drops_volatile(tmp_path: Path) -> None:
    orchestrator, _, _ = _harness(tmp_path)
    trace = _trace("snap-harvest")

    harvested = orchestrator._collect_task_evidence(trace, _profile())

    values = [item.candidate.value for item in harvested]
    assert set(REUSABLE_KEYS) <= set(values)
    assert not set(VOLATILE_KEYS) & set(values)
    # 所有动作都发生在同一帧 → 同一结构身份。
    assert len({item.page_signature for item in harvested}) == 1
    assert all(item.page_signature for item in harvested)


def test_dynamic_identifier_is_folded_to_a_stable_pattern(tmp_path: Path) -> None:
    orchestrator, _, _ = _harness(tmp_path)
    harvested = orchestrator._collect_task_evidence(_trace("snap-harvest"), _profile())

    dynamic = next(item for item in harvested if item.candidate.value.startswith("add_agenda_title-"))
    locator = AgentOrchestrator._stable_locator_from_candidate(dynamic, "run-calendar-harvest")

    assert locator.dynamic_pattern == "add_agenda_title-#"
    assert locator.confidence == ConfidenceLevel.MEDIUM
    assert locator.warning
    assert locator.key == "add_agenda_title-1789951623657"


def test_harvest_merges_into_draft_and_accumulates_rounds(tmp_path: Path) -> None:
    orchestrator, settings, registry = _harness(tmp_path)
    trace = _trace("snap-harvest")
    emitter = RunEventEmitter(trace, orchestrator.repository, orchestrator.artifacts)

    first = orchestrator._harvest_task_evidence(trace, _profile(), registry, emitter)
    assert first is not None
    assert first.status == ProfileStatus.DRAFT
    assert first.provenance.evidence.get("verification_passed") is not True
    assert len(first.stable_locator_inventory) == len(REUSABLE_KEYS)

    second = orchestrator._harvest_task_evidence(trace, first, registry, emitter)
    assert second is not None
    assert len(second.stable_locator_inventory) == len(REUSABLE_KEYS)
    assert {item.observed_rounds for item in second.stable_locator_inventory} == {2}
    assert second.stable_locator_inventory[0].source == "live_task_harvest"

    # 回收产物只写 draft，不产生任何 verified/candidate 晋级。
    assert not registry.list(ProfileStatus.VERIFIED)
    assert not registry.list(ProfileStatus.CANDIDATE)
    draft_path = settings.resolved_profiles_dir / "draft" / "com-huawei-hmos-calendar.json"
    payload = json.loads(draft_path.read_text(encoding="utf-8"))
    assert payload["status"] == ProfileStatus.DRAFT
    assert len(payload["assertion_inventory"]) >= 1

    event_types = [event.type for event in trace.events]
    assert EventType.LOCATOR_CANDIDATE_OBSERVED in event_types
    assert EventType.PROFILE_HARVESTED in event_types
    assert EventType.PROFILE_PROMOTED not in event_types


def test_harvest_does_not_fabricate_verification_evidence(tmp_path: Path) -> None:
    """回收永远不能把 Profile 变成 VERIFIED——必须真的跑绿一次设备验证。"""
    orchestrator, _, registry = _harness(tmp_path)
    trace = _trace("snap-harvest")
    emitter = RunEventEmitter(trace, orchestrator.repository, orchestrator.artifacts)

    # 用一份「凑够 3 定位器 / 3 页 / 2 断言」但不含 verification_passed 的 Profile 直接回收。
    rich = TargetAppProfile.model_validate(
        {
            **_profile().model_dump(mode="python"),
            "stable_locator_inventory": [
                {
                    "name": f"page-{index}",
                    "page_signature": f"page-{index}",
                    "key": f"page-key-{index}",
                    "observed_rounds": 1,
                    "unique_match_rounds": 1,
                    "evidence_snapshot_ids": [f"snap-{index}"],
                }
                for index in range(1, 4)
            ],
            "assertion_inventory": [
                {
                    "name": f"assertion-{index}",
                    "kind": "visible",
                    "target": f"page-key-{index}",
                    "page_signature": f"page-{index}",
                    "observed_rounds": 1,
                    "evidence_snapshot_ids": [f"snap-{index}"],
                }
                for index in range(1, 3)
            ],
            "core_flows": [{"pages": ["page-1", "page-2", "page-3"], "steps": [], "interaction_types": []}],
        }
    )

    merged = orchestrator._harvest_task_evidence(trace, rich, registry, emitter)

    assert merged is not None
    assert merged.status == ProfileStatus.DRAFT
    assert not registry.list(ProfileStatus.CANDIDATE)
    assert not registry.list(ProfileStatus.VERIFIED)


def test_harvest_skips_verified_profile(tmp_path: Path) -> None:
    """真机复盘：已验证 Profile 不得被回收改写成可晋级的 candidate。

    run-20260921T063745Z-ba36e30e（知乎++ verified Profile）实测：回收把任务期定位器并进
    已验证 Profile，产出一份**继承 verification_passed=True** 的 candidate——之后可被
    append_replay_evidence 直接晋级，等于用未经设备验证的定位器替换已验证资产。
    """
    orchestrator, settings, registry = _harness(tmp_path)
    trace = _trace("snap-harvest")
    emitter = RunEventEmitter(trace, orchestrator.repository, orchestrator.artifacts)

    verified = _verified_profile()
    (settings.resolved_profiles_dir / f"{verified.target_app_id}.json").write_text(
        verified.model_dump_json(indent=2), encoding="utf-8"
    )
    before = (settings.resolved_profiles_dir / f"{verified.target_app_id}.json").read_text(encoding="utf-8")

    merged = orchestrator._harvest_task_evidence(trace, verified, registry, emitter)

    assert merged is None
    assert not registry.list(ProfileStatus.CANDIDATE)
    after = (settings.resolved_profiles_dir / f"{verified.target_app_id}.json").read_text(encoding="utf-8")
    assert after == before
    assert not any(event.type == EventType.PROFILE_HARVESTED for event in trace.events)


def test_merge_profile_evidence_never_inherits_verification_passed() -> None:
    """纵深防御：合并结果即便来自已验证 Profile，也必须清掉 verification_passed。"""
    from harmony_test_agent.agents.orchestrator import AgentOrchestrator as AO

    trace = _trace("snap-harvest")
    collected = [
        HarvestedLocator(
            candidate=LocatorCandidate(kind=LocatorKind.KEY, value="tabs_month"),
            page_signature="page-x",
            snapshot_id="snap-harvest",
        )
    ]

    merged = AO._merge_profile_evidence(trace, _verified_profile(), collected, [])

    assert merged.status == ProfileStatus.DRAFT
    assert merged.provenance.evidence.get("verification_passed") is False
    assert merged.provenance.evidence.get("verification_invalidated_by_harvest") is True


def _verified_profile() -> TargetAppProfile:
    """一份已验证 Profile（证据齐全），用于验证「回收不得改写已验证资产」。"""
    return TargetAppProfile.model_validate(
        {
            **_profile().model_dump(mode="python"),
            "status": ProfileStatus.VERIFIED,
            "stable_locator_inventory": [
                {
                    "name": f"page-{index}",
                    "page_signature": f"page-{index}",
                    "key": f"page-key-{index}",
                    "observed_rounds": 1,
                    "unique_match_rounds": 1,
                    "evidence_snapshot_ids": [f"snap-{index}"],
                }
                for index in range(1, 4)
            ],
            "assertion_inventory": [
                {
                    "name": f"assertion-{index}",
                    "kind": "visible",
                    "target": f"page-key-{index}",
                    "page_signature": f"page-{index}",
                    "observed_rounds": 1,
                    "evidence_snapshot_ids": [f"snap-{index}"],
                }
                for index in range(1, 3)
            ],
            "core_flows": [{"pages": ["page-1", "page-2", "page-3"], "steps": [], "interaction_types": []}],
            "provenance": {
                "verified_at": "2026-09-21T00:00:00Z",
                "hypium_replay_run_ids": ["run-old:profile-attempt-1"],
                "evidence": {"verification_passed": True},
            },
        }
    )


def test_harvested_locator_dataclass_exposes_candidate() -> None:
    item = HarvestedLocator(
        candidate=LocatorCandidate(kind=LocatorKind.KEY, value="tabs_month"),
        page_signature="page-x",
        snapshot_id="snap-x",
    )
    assert item.candidate.value == "tabs_month"
    assert item.page_signature == "page-x"


def test_calendar_fixture_identity_matches_foreground_helper() -> None:
    """fixture 与 helper 的 bundle/window_type 组合稳定：回收的 page_signature 可跨运行复现。"""
    from harmony_test_agent.discovery import BoundedExplorer

    snapshot = snapshot_from_layout(T0)
    foreground = calendar_foreground()
    assert BoundedExplorer._structural_identity(snapshot, foreground)
    assert foreground.bundle_name == "com.huawei.hmos.calendar"
    assert Path(T0).is_file()
