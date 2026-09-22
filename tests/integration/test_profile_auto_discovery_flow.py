from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from harmony_test_agent.agents import AgentOrchestrator, MockAgentProvider
from harmony_test_agent.agents.providers import PlanningContext
from harmony_test_agent.api import create_app
from harmony_test_agent.config import Settings
from harmony_test_agent.devices import DeviceAdapter
from harmony_test_agent.discovery import (
    AssertionObservation,
    DiscoveryPage,
    DiscoveryResult,
    DiscoveryTransition,
    ExplorationAction,
    ExplorationPolicy,
    LocatorObservation,
    ProfileVerificationResult,
    StabilityLevel,
    StabilityReport,
    VerificationRound,
)
from harmony_test_agent.discovery.stability import StableAssertionEvidence, StableLocatorEvidence
from harmony_test_agent.generation import HypiumGenerator
from harmony_test_agent.models import (
    ActionResult,
    AssertionDefinition,
    AssertionResult,
    BoundingBox,
    CommandResult,
    EventType,
    LocatorCandidate,
    LocatorKind,
    PlannedStep,
    PlanResult,
    ProfileStatus,
    ReplayResult,
    RunRequest,
    RunState,
    RunTrace,
    ScreenSnapshot,
    StableLocator,
    TargetAppProfile,
    ToolName,
    UIElement,
)
from harmony_test_agent.profiles import ProfileRegistry
from harmony_test_agent.storage import ArtifactStore, RunRepository
from harmony_test_agent.targets import ForegroundApp, InstalledApp, ResolvedTarget

BUNDLE_A = "com.example.notes.alpha"
BUNDLE_B = "com.example.notes.beta"
TARGET_A = "com-example-notes-alpha"

# 资产流水线精简后的默认门禁（1 轮设备验证 / 1 次 Hypium 回放）；用例用显式常量
# 而不是字面量 3，避免重构后测试与实现漂移。
_DEFAULT_SETTINGS = Settings(_env_file=None)
EVIDENCE_ROUNDS = _DEFAULT_SETTINGS.profile_verification_rounds
REPLAY_ATTEMPTS = _DEFAULT_SETTINGS.hypium_replay_attempts
TARGET_B = "com-example-notes-beta"


class FlowDevice(DeviceAdapter):
    """Deterministic offline device covering target resolution and the original task."""

    def __init__(self, tmp_path: Path, apps: list[InstalledApp] | None = None) -> None:
        self.tmp_path = tmp_path
        self.device_id = "offline-flow-device"
        self.apps = apps or [_installed(BUNDLE_A, "Notes")]
        self.connected = False
        self.foreground: ForegroundApp | None = None
        self.capture_count = 0

    def connect(self) -> None:
        self.connected = True

    def health_check(self) -> dict[str, object]:
        return {"connected": self.connected, "id": self.device_id, "resolution": [360, 720]}

    def list_installed_apps(self) -> list[InstalledApp]:
        return list(self.apps)

    def inspect_app(self, bundle_name: str) -> InstalledApp:
        return next(item for item in self.apps if item.bundle_name == bundle_name)

    def current_foreground_app(self) -> ForegroundApp | None:
        return self.foreground

    def start_app(
        self,
        bundle_name: str,
        ability_name: str,
        module_name: str | None = None,
    ) -> CommandResult:
        del module_name
        self.foreground = ForegroundApp(bundle_name=bundle_name, ability_name=ability_name)
        return _command("start")

    def stop_app(self, bundle_name: str) -> CommandResult:
        if self.foreground and self.foreground.bundle_name == bundle_name:
            self.foreground = None
        return _command("stop")

    def screenshot(self, output_dir: Path, run_id: str, label: str = "screen") -> ScreenSnapshot:
        output_dir.mkdir(parents=True, exist_ok=True)
        self.capture_count += 1
        image_path = output_dir / f"{label}-{self.capture_count}.png"
        Image.new("RGB", (360, 720), (self.capture_count % 255, 40, 80)).save(image_path)
        elements = [
            UIElement(
                element_id=f"home-{index}",
                key=f"home-key-{index}",
                content=f"Home {index}",
                type="Text",
                bbox=BoundingBox(left=10, top=index * 50, right=200, bottom=index * 50 + 40),
            )
            for index in range(1, 4)
        ]
        return ScreenSnapshot(
            snapshot_id=f"{run_id}-{label}-{self.capture_count}",
            run_id=run_id,
            image_path=image_path,
            image_sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
            width=360,
            height=720,
            page_path="pages/Home",
            elements=elements,
        )

    def collect_ui_hierarchy(self) -> dict:
        return {}

    def collect_logs(self, output_path: Path) -> CommandResult:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("offline fake log", encoding="utf-8")
        return _command("hilog")

    def open_app(self, profile: TargetAppProfile, reset: bool = False) -> CommandResult:
        del reset
        self.foreground = ForegroundApp(
            bundle_name=profile.bundle_name,
            ability_name=profile.main_ability,
        )
        return _command("open")

    def click(self, x: int, y: int) -> CommandResult:
        del x, y
        return _command("click")

    def input_text(self, text: str, x: int | None = None, y: int | None = None) -> CommandResult:
        del text, x, y
        return _command("input")

    def swipe(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        duration: float = 0.5,
    ) -> CommandResult:
        del start, end, duration
        return _command("swipe")

    def back(self) -> CommandResult:
        return _command("back")

    def wait(self, seconds: float) -> CommandResult:
        del seconds
        return _command("wait")

    def close(self) -> None:
        self.connected = False


class OriginalTaskProvider(MockAgentProvider):
    async def plan(self, task: str, context: PlanningContext, max_steps: int) -> PlanResult:
        del context
        steps = [
            PlannedStep(step_id="task-open", instruction="open target", tool=ToolName.OPEN_APP),
            PlannedStep(
                step_id="task-assert",
                instruction="check target home",
                tool=ToolName.ASSERT_VISIBLE,
                target="home-key-1",
            ),
            PlannedStep(step_id="task-finish", instruction="finish", tool=ToolName.FINISH),
        ]
        return PlanResult(goal=task, steps=steps[:max_steps], model_used=self.name, mock=True)


def _installed(bundle_name: str, display_name: str) -> InstalledApp:
    return InstalledApp(
        bundle_name=bundle_name,
        display_name=display_name,
        abilities=("MainAbility",),
        main_ability="MainAbility",
        module_name="entry",
        version_name="1.0.0",
        version_code=1,
    )


def _command(name: str, *, passed: bool = True) -> CommandResult:
    return CommandResult(command=name, args=[name], returncode=0 if passed else 1)


def _profile(
    *,
    bundle_name: str = BUNDLE_A,
    target_app_id: str = TARGET_A,
    display_name: str = "Notes",
    rounds: int = EVIDENCE_ROUNDS,
) -> TargetAppProfile:
    page_locators = [
        StableLocator(
            name=f"page-{index}",
            page_signature=f"page-{index}",
            key=f"page-key-{index}",
            observed_rounds=rounds,
            unique_match_rounds=rounds,
            evidence_snapshot_ids=[f"page-{index}-round-{round_number}" for round_number in range(1, rounds + 1)],
        )
        for index in range(1, 4)
    ]
    return TargetAppProfile(
        target_app_id=target_app_id,
        display_name=display_name,
        bundle_name=bundle_name,
        main_ability="MainAbility",
        module_name="entry",
        reset_strategy={
            "recovery_actions": [
                {"kind": "click", "coordinate": [20, 20]},
                {"kind": "swipe", "direction": "up"},
            ]
        },
        stable_locator_inventory=page_locators,
        assertion_inventory=[
            AssertionDefinition(
                name=f"assertion-{index}",
                kind="visible",
                target=f"page-key-{index}",
                page_signature=f"page-{index}",
                observed_rounds=rounds,
                evidence_snapshot_ids=[
                    f"assertion-{index}-round-{round_number}" for round_number in range(1, rounds + 1)
                ],
            )
            for index in range(1, 3)
        ],
        core_flows=[
            {
                "pages": ["page-1", "page-2", "page-3"],
                "steps": [],
                "interaction_types": ["click", "input", "swipe"],
            }
        ],
        provenance={
            "discovery_run_id": "run-profile",
            "evidence": {
                "verification_passed": True,
                "cross_bundle_violations": 0,
            },
        },
    )


def _replay_ids(attempts: int = REPLAY_ATTEMPTS) -> list[str]:
    return [f"run-profile:profile-attempt-{attempt}" for attempt in range(1, attempts + 1)]


def _promote(
    registry: ProfileRegistry,
    profile: TargetAppProfile,
    *,
    attempts: int = REPLAY_ATTEMPTS,
) -> TargetAppProfile:
    registry.save_candidate(profile)
    registry.promote(profile.target_app_id, replay_run_ids=_replay_ids(attempts))
    return registry.read(profile.target_app_id)


def _settings(
    tmp_path: Path,
    *,
    rounds: int | None = None,
    attempts: int | None = None,
    bootstrap: bool = True,
) -> Settings:
    overrides: dict[str, int] = {}
    if rounds is not None:
        overrides["profile_verification_rounds"] = rounds
    if attempts is not None:
        overrides["hypium_replay_attempts"] = attempts
    return Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
        unchanged_screen_limit=2,
        # 本文件验证的是「同一次运行先探索晋级再执行任务」的显式流水线：计划 4.2 之后
        # 任务型运行默认不再前置完整探索（BOOTSTRAP_ENABLED_ON_TASK_RUN=false），
        # 因此这里显式打开；新默认由 test_bootstrap_disabled_by_default_on_task_run 覆盖。
        bootstrap_enabled_on_task_run=bootstrap,
        **overrides,
    )


def _orchestrator(
    tmp_path: Path,
    device: FlowDevice,
    *,
    rounds: int | None = None,
    attempts: int | None = None,
) -> AgentOrchestrator:
    settings = _settings(tmp_path, rounds=rounds, attempts=attempts)
    return AgentOrchestrator(
        settings,
        provider=OriginalTaskProvider(),
        repository=RunRepository(settings.resolved_database_path),
        artifacts=ArtifactStore(settings.resolved_runtime_dir),
        device_factory=lambda _: device,
        settle_seconds=0,
        launch_settle_seconds=0,
    )


def _resolved(device_id: str = "offline-flow-device") -> ResolvedTarget:
    return ResolvedTarget(
        target_app_id=TARGET_A,
        display_name="Notes",
        bundle_name=BUNDLE_A,
        main_ability="MainAbility",
        module_name="entry",
        version_name="1.0.0",
        version_code=1,
        device_id=device_id,
        source="installed_app",
    )


def _discovery_result() -> DiscoveryResult:
    target = _resolved()
    click = ExplorationAction(
        action_id="click-page",
        kind="click",
        element_id="home-1",
        locator_kind="key",
        locator_value="home-key-1",
        target_text="Home 1",
        coordinate=(30, 70),
    )
    input_text = ExplorationAction(
        action_id="input-page",
        kind="input",
        element_id="input-1",
        locator_kind="key",
        locator_value="input-key-1",
        target_text="Input",
        coordinate=(30, 120),
    )
    swipe = ExplorationAction(action_id="swipe-page", kind="swipe", direction="up")
    paths = [[], [click], [click, input_text], [click, input_text, swipe]]
    pages = [
        DiscoveryPage(
            page_id=f"page-{index + 1}",
            signature=f"page-{index + 1}",
            page_path=f"pages/Page{index + 1}",
            bundle_name=BUNDLE_A,
            ability_name="MainAbility",
            snapshot_id=f"discovery-{index + 1}",
            image_path=Path(f"discovery-{index + 1}.png"),
            element_count=3,
            discovered_order=index + 1,
            path_actions=path,
        )
        for index, path in enumerate(paths)
    ]
    transitions = [
        DiscoveryTransition(
            source_page_id=pages[index].page_id,
            target_page_id=pages[index + 1].page_id,
            action=action,
            before_snapshot_id=pages[index].snapshot_id,
            after_snapshot_id=pages[index + 1].snapshot_id,
            success=True,
        )
        for index, action in enumerate((click, input_text, swipe))
    ]
    return DiscoveryResult(
        target=target,
        policy=RunRequest(target={"bundle_name": BUNDLE_A}).exploration_policy,
        pages=pages,
        transitions=transitions,
    )


def _verification_result(*, passed: bool = True, rounds: int = EVIDENCE_ROUNDS) -> ProfileVerificationResult:
    target = _resolved()
    page_signatures = [f"page-{index}" for index in range(1, 5)]
    observed = tuple(range(1, rounds + 1))
    locator_evidence = [
        StableLocatorEvidence(
            name=f"stable-{index}",
            kind=LocatorKind.KEY,
            value=f"stable-key-{index}",
            level=StabilityLevel.HIGH,
            rounds=observed,
            page_signatures=(signature,),
            unique_each_round=True,
        )
        for index, signature in enumerate(page_signatures, 1)
    ]
    assertion_evidence = [
        StableAssertionEvidence(
            kind="visible",
            target=f"stable-key-{index}",
            rounds=observed,
            page_signatures=(page_signatures[index - 1],),
        )
        for index in range(1, 3)
    ]
    verification_rounds = []
    for round_number in range(1, rounds + 1):
        locator_observations = [
            LocatorObservation(
                round_number=round_number,
                page_signature=signature,
                kind=LocatorKind.KEY,
                value=f"stable-key-{index}",
            )
            for index, signature in enumerate(page_signatures, 1)
        ]
        assertion_observations = [
            AssertionObservation(
                round_number=round_number,
                page_signature=page_signatures[index - 1],
                kind="visible",
                target=f"stable-key-{index}",
                passed=True,
            )
            for index in range(1, 3)
        ]
        verification_rounds.append(
            VerificationRound(
                round_number=round_number,
                passed=passed,
                recovery_passed=passed,
                visited_page_signatures=page_signatures if passed else page_signatures[:1],
                locator_observations=locator_observations,
                assertion_observations=assertion_observations,
                resolution=(360, 720),
                snapshot_ids=[f"snapshot-round-{round_number}"],
            )
        )
    return ProfileVerificationResult(
        target=target,
        rounds=verification_rounds,
        stability=StabilityReport(
            locators=locator_evidence if passed else [],
            assertions=assertion_evidence if passed else [],
        ),
        passed=passed,
        failures=[] if passed else ["device verification failed"],
    )


def _patch_discovery(
    monkeypatch,
    *,
    verification_passed: bool,
    rounds: int = EVIDENCE_ROUNDS,
) -> None:
    monkeypatch.setattr(
        "harmony_test_agent.agents.orchestrator.BoundedExplorer.explore",
        lambda self: _discovery_result(),
    )
    monkeypatch.setattr(
        "harmony_test_agent.agents.orchestrator.ProfileVerifier.verify",
        lambda self, discovery: _verification_result(passed=verification_passed, rounds=rounds),
    )


def _replays(*passed: bool) -> list[ReplayResult]:
    return [
        ReplayResult(
            attempt=index,
            command=_command(f"hypium-{index}", passed=value),
            passed=value,
        )
        for index, value in enumerate(passed, 1)
    ]


async def test_ambiguous_target_waits_for_selection_then_resumes_same_run(tmp_path: Path) -> None:
    apps = [_installed(BUNDLE_A, "Notes"), _installed(BUNDLE_B, "Notes")]
    device = FlowDevice(tmp_path, apps)
    orchestrator = _orchestrator(tmp_path, device)
    registry = ProfileRegistry(orchestrator.settings.resolved_profiles_dir)
    _promote(
        registry,
        _profile(bundle_name=BUNDLE_B, target_app_id=TARGET_B, display_name="Notes"),
    )
    run_id = "run-ambiguous-resume"

    pending = asyncio.create_task(
        orchestrator.run(
            RunRequest(target={"app_name": "Notes"}, task="check target home", auto_generate=False),
            run_id=run_id,
        )
    )
    for _ in range(100):
        waiting = orchestrator.repository.get_trace(run_id)
        if waiting and waiting.state == RunState.WAITING_TARGET_SELECTION:
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("run never entered target-selection wait state")

    assert {item["bundle_name"] for item in waiting.target_candidates} == {BUNDLE_A, BUNDLE_B}
    selection = await orchestrator.select_target(run_id, bundle_name=BUNDLE_B)
    trace = await pending

    assert selection["accepted"] is True
    assert trace.run_id == run_id
    assert trace.state == RunState.COMPLETED
    assert trace.resolved_target is not None
    assert trace.resolved_target.bundle_name == BUNDLE_B
    assert any(event.type == EventType.TARGET_CANDIDATES_FOUND for event in trace.events)
    assert any(event.type == EventType.ORIGINAL_TASK_STARTED for event in trace.events)


@pytest.mark.parametrize("attempts", [1, 3])
async def test_same_run_discovers_promotes_then_executes_original_task(
    tmp_path: Path,
    monkeypatch,
    attempts: int,
) -> None:
    """编排器把 Settings.hypium_replay_attempts 透传给回放门禁（1 次精简 / 3 次历史）。"""
    device = FlowDevice(tmp_path)
    orchestrator = _orchestrator(tmp_path, device, attempts=attempts)
    _patch_discovery(monkeypatch, verification_passed=True)

    async def successful_replays(runner, generated, run_id: str, attempt_count: int, emitter=None):
        del runner, generated, run_id, emitter
        assert attempt_count == attempts
        return _replays(*([True] * attempts))

    monkeypatch.setattr(orchestrator, "_run_replays", successful_replays)
    trace = await orchestrator.run(
        RunRequest(
            target={"bundle_name": BUNDLE_A},
            task="check target home",
            auto_generate=False,
        ),
        run_id="run-discover-promote-task",
    )

    stored = ProfileRegistry(orchestrator.settings.resolved_profiles_dir).read(TARGET_A)
    event_types = [event.type for event in trace.events]
    assert trace.state == RunState.COMPLETED, trace.error
    assert trace.phase == "task"
    assert trace.profile_validation_generated is not None
    assert len(trace.profile_validation_replays) == attempts
    assert stored.status == ProfileStatus.VERIFIED
    assert stored.provenance.discovery_run_id == trace.run_id
    assert len(stored.provenance.hypium_replay_run_ids) == attempts
    assert event_types.index(EventType.PROFILE_PROMOTED) < event_types.index(EventType.ORIGINAL_TASK_STARTED)
    assert any(action.step_id == "task-assert" for action in trace.actions)


async def test_profile_provenance_records_generated_script_for_async_replay(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """晋级后的 Profile 必须记录门禁脚本路径，供 POST /api/profiles/{id}/replay 复用。"""
    device = FlowDevice(tmp_path)
    orchestrator = _orchestrator(tmp_path, device)
    _patch_discovery(monkeypatch, verification_passed=True)

    async def successful_replays(runner, generated, run_id: str, attempt_count: int, emitter=None):
        del runner, generated, run_id, emitter
        return _replays(*([True] * REPLAY_ATTEMPTS))

    monkeypatch.setattr(orchestrator, "_run_replays", successful_replays)

    await orchestrator.run(
        RunRequest(target={"bundle_name": BUNDLE_A}, task="check target home", auto_generate=False),
        run_id="run-script-path",
    )

    stored = ProfileRegistry(orchestrator.settings.resolved_profiles_dir).read(TARGET_A)
    assert stored.provenance.generated_script_path
    assert Path(stored.provenance.generated_script_path).is_file()


async def test_legacy_three_round_verification_and_replay_still_promotes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """回滚开关：PROFILE_VERIFICATION_ROUNDS=3 + HYPIUM_REPLAY_ATTEMPTS=3 恢复旧行为。"""
    device = FlowDevice(tmp_path)
    orchestrator = _orchestrator(tmp_path, device, rounds=3, attempts=3)
    _patch_discovery(monkeypatch, verification_passed=True, rounds=3)

    async def successful_replays(runner, generated, run_id: str, attempt_count: int, emitter=None):
        del runner, generated, run_id, emitter
        assert attempt_count == 3
        return _replays(True, True, True)

    monkeypatch.setattr(orchestrator, "_run_replays", successful_replays)
    trace = await orchestrator.run(
        RunRequest(target={"bundle_name": BUNDLE_A}, task="check target home", auto_generate=False),
        run_id="run-legacy-three-rounds",
    )

    stored = ProfileRegistry(orchestrator.settings.resolved_profiles_dir).read(TARGET_A)
    assert trace.state == RunState.COMPLETED, trace.error
    assert stored.status == ProfileStatus.VERIFIED
    assert len(stored.provenance.hypium_replay_run_ids) == 3


async def test_provisional_profile_blocks_formal_hypium_generation_and_execution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """临时测试（provisional）不得执行正式 Hypium，但产物仍以 diagnostic 形式保留（计划 R2/R3）。"""
    device = FlowDevice(tmp_path)
    orchestrator = _orchestrator(tmp_path, device)
    _patch_discovery(monkeypatch, verification_passed=False)
    replay_called = False

    async def forbidden_replay(*args, **kwargs):
        nonlocal replay_called
        replay_called = True
        raise AssertionError("provisional runs must not execute formal Hypium")

    monkeypatch.setattr(orchestrator, "_run_replays", forbidden_replay)
    trace = await orchestrator.run(
        RunRequest(
            target={"bundle_name": BUNDLE_A},
            task="check target home",
            temporary_test=True,
            auto_generate=True,
            auto_execute=True,
        ),
        run_id="run-provisional",
    )

    registry = ProfileRegistry(orchestrator.settings.resolved_profiles_dir)
    draft = registry.get_any(bundle_name=BUNDLE_A)
    assert trace.state == RunState.COMPLETED, trace.error
    assert trace.provisional is True
    # 失败草稿按新语义落盘：有定位器（含任务期回收）→ DRAFT，完全为空 → INVALID。
    assert draft is not None
    if draft.stable_locator_inventory:
        assert draft.status == ProfileStatus.DRAFT
    else:
        assert draft.status == ProfileStatus.INVALID
    assert trace.replays == []
    assert replay_called is False
    # 自动回放被关闭 ≠ 脚本不可用：provisional 只把晋级资格判为 False，脚本立即可执行。
    assert trace.generated is not None
    assert trace.generated.purpose == "acceptance"
    assert trace.generated.replay_eligible is True
    assert trace.generated.promotion_eligible is False
    assert "provisional trace is not Profile-promotion evidence" in trace.generated.promotion_blockers
    assert trace.generated.python_path.exists()


def test_trace_only_generation_uses_frozen_profile_after_external_profile_change(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "runs")
    original = _profile()
    trace = RunTrace(
        run_id="run-frozen-profile",
        target_app_id=original.target_app_id,
        task="frozen generation",
        device_id="offline-flow-device",
        profile_snapshot=original.model_copy(deep=True),
        assertions=[AssertionResult(kind="visible", target="home-key-1", passed=True, message="visible")],
        actions=[
            ActionResult(step_id="open", tool=ToolName.OPEN_APP, success=True),
            ActionResult(
                step_id="assert",
                tool=ToolName.ASSERT_VISIBLE,
                params={"target": "home-key-1"},
                success=True,
                assertion=AssertionResult(
                    kind="visible",
                    target="home-key-1",
                    passed=True,
                    message="visible",
                ),
                locator=LocatorCandidate(kind=LocatorKind.KEY, value="home-key-1"),
            ),
        ],
    )
    registry = ProfileRegistry(tmp_path / "profiles")
    original_path = registry.save_draft(original)
    assert original_path.exists()
    changed = original.model_copy(
        update={"bundle_name": "com.example.changed", "main_ability": "ChangedAbility"},
        deep=True,
    )
    registry.save_draft(changed)
    assert registry.read(TARGET_A, ProfileStatus.DRAFT).bundle_name == "com.example.changed"

    generated = HypiumGenerator(artifacts).generate(trace)
    config = json.loads(generated.config_path.read_text(encoding="utf-8"))
    script = generated.python_path.read_text(encoding="utf-8")

    assert config["bundle_name"] == BUNDLE_A
    assert config["main_ability"] == "MainAbility"
    assert BUNDLE_A in script
    assert "com.example.changed" not in script
    assert trace.profile_snapshot.bundle_name == BUNDLE_A


async def test_failed_admission_replay_keeps_candidate_and_prevents_promotion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    device = FlowDevice(tmp_path)
    orchestrator = _orchestrator(tmp_path, device, attempts=3)
    _patch_discovery(monkeypatch, verification_passed=True)

    async def failing_replays(runner, generated, run_id: str, attempts: int, emitter=None):
        del runner, generated, run_id, emitter
        assert attempts == 3
        return _replays(True, False, True)

    monkeypatch.setattr(orchestrator, "_run_replays", failing_replays)
    trace = await orchestrator.run(
        RunRequest(target={"bundle_name": BUNDLE_A}, task="check target home", auto_generate=False),
        run_id="run-replay-failure",
    )

    registry = ProfileRegistry(orchestrator.settings.resolved_profiles_dir)
    candidate = registry.read(TARGET_A, ProfileStatus.CANDIDATE)
    assert trace.state == RunState.FAILED_SCRIPT
    assert "replay gate failed" in (trace.error or "")
    assert [item.passed for item in trace.profile_validation_replays] == [True, False, True]
    assert candidate.status == ProfileStatus.CANDIDATE
    assert registry.get(target_app_id=TARGET_A, status=ProfileStatus.VERIFIED) is None
    assert not any(event.type == EventType.PROFILE_PROMOTED for event in trace.events)


async def test_failed_quick_revalidation_keeps_verified_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """快速复验失败只记录证据并转入完整探索，不得销毁既有 verified Profile。"""
    device = FlowDevice(tmp_path)
    orchestrator = _orchestrator(tmp_path, device)
    registry = ProfileRegistry(orchestrator.settings.resolved_profiles_dir)
    _promote(registry, _profile())
    _patch_discovery(monkeypatch, verification_passed=True)

    async def successful_replays(runner, generated, run_id: str, attempts: int, emitter=None):
        del runner, generated, run_id, emitter
        return _replays(True, True, True)

    monkeypatch.setattr(orchestrator, "_run_replays", successful_replays)
    trace = await orchestrator.run(
        RunRequest(target={"bundle_name": BUNDLE_A}, task="check target home", auto_generate=False),
        run_id="run-keep-verified",
    )

    assert trace.state == RunState.COMPLETED, trace.error
    revalidation = [event for event in trace.events if event.type == EventType.PROFILE_REVALIDATION_FINISHED]
    assert revalidation[-1].payload["passed"] is False
    assert revalidation[-1].payload["kept"] is True
    assert any(event.type == EventType.PROFILE_VERIFICATION_STARTED for event in trace.events)
    # 旧 verified Profile 未被失效，新一轮探索晋级后自然覆盖
    stored = registry.read(TARGET_A, ProfileStatus.VERIFIED)
    assert stored.provenance.discovery_run_id == trace.run_id
    assert any(
        item.provenance.evidence.get("quick_verification", {}).get("passed") is False
        for item in registry.history(BUNDLE_A)
    )


@pytest.mark.parametrize("attempts", [1, 3])
async def test_replay_events_stream_per_attempt(tmp_path: Path, monkeypatch, attempts: int) -> None:
    """Hypium 回放事件逐次推送：每次尝试的开始/完成交错出现，而非全部结束后补发。"""
    device = FlowDevice(tmp_path)
    orchestrator = _orchestrator(tmp_path, device, attempts=attempts)
    _patch_discovery(monkeypatch, verification_passed=True)
    monkeypatch.setattr(
        "harmony_test_agent.agents.orchestrator.HypiumRunner.execute",
        lambda self, generated, attempt: _replays(True)[0].model_copy(update={"attempt": attempt}),
    )
    trace = await orchestrator.run(
        RunRequest(target={"bundle_name": BUNDLE_A}, task="check target home", auto_generate=False),
        run_id="run-stream-replays",
    )

    assert trace.state == RunState.COMPLETED, trace.error
    types = [event.type for event in trace.events]
    started_index = [index for index, item in enumerate(types) if item == EventType.HYPIUM_REPLAY_STARTED]
    finished_index = [index for index, item in enumerate(types) if item == EventType.HYPIUM_REPLAY_FINISHED]
    assert len(started_index) == attempts
    assert len(finished_index) == attempts
    assert all(start < finish for start, finish in zip(started_index, finished_index, strict=True))
    assert all(finish < start for finish, start in zip(finished_index[:-1], started_index[1:], strict=True))
    assert [item.attempt for item in trace.profile_validation_replays] == list(range(1, attempts + 1))


class _RecordingDevice(FlowDevice):
    """记录关键调用时刻的包装设备：断言 OPEN_APP 与 after 截图之间存在启动静默期。"""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.timeline: list[tuple[str, float]] = []

    def _mark(self, name: str) -> None:
        self.timeline.append((name, time.monotonic()))

    def open_app(self, profile, reset: bool = False):
        result = super().open_app(profile, reset)
        self._mark("open_app")
        return result

    def current_foreground_app(self):
        self._mark("foreground_poll")
        return super().current_foreground_app()

    def screenshot(self, output_dir: Path, run_id: str, label: str = "screen"):
        self._mark(f"screenshot:{label}")
        return super().screenshot(output_dir, run_id, label)


async def test_open_app_waits_launch_settle_before_after_capture(tmp_path: Path) -> None:
    """任务阶段 OPEN_APP 后进入冷启动静默期：轮询前台且静默满预算后才截 after 帧。

    回归背景：`aa start` 返回即截图会截到启动 logo 页（应用冷启动 2-5 秒）。
    """
    device = _RecordingDevice(tmp_path)
    settings = _settings(tmp_path)
    orchestrator = AgentOrchestrator(
        settings,
        provider=OriginalTaskProvider(),
        repository=RunRepository(settings.resolved_database_path),
        artifacts=ArtifactStore(settings.resolved_runtime_dir),
        device_factory=lambda _: device,
        settle_seconds=0,
        launch_settle_seconds=0.25,
    )
    trace = await orchestrator.run(
        RunRequest(
            target={"bundle_name": BUNDLE_A},
            task="check target home",
            auto_generate=True,
            exploration_policy=ExplorationPolicy(enabled=False),
        ),
        run_id="run-launch-settle",
    )

    assert trace.state == RunState.COMPLETED, trace.error
    marks = device.timeline
    start_index = max(index for index, (name, _) in enumerate(marks) if name == "open_app")
    after_index = next(index for index, (name, _) in enumerate(marks) if name == "screenshot:step_01_after")
    polls_between = [1 for name, _ in marks[start_index:after_index] if name == "foreground_poll"]
    assert polls_between, "launch settle must poll foreground before the after capture"
    assert marks[after_index][1] - marks[start_index][1] >= 0.25


def test_profile_api_lifecycle_errors_and_explicit_history_rollback(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    registry = ProfileRegistry(settings.resolved_profiles_dir)
    _promote(registry, _profile(display_name="Notes v1"))
    _promote(registry, _profile(display_name="Notes v2"))

    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        summaries = client.get("/api/profiles")
        current_summary = next(
            item
            for item in summaries.json()
            if item["target_app_id"] == TARGET_A and item["status"] == ProfileStatus.VERIFIED
        )
        backup_name = current_summary["history"][0]["backup_name"]

        invalid_status = client.post(
            f"/api/profiles/{TARGET_A}/verify",
            json={"status": "retired"},
        )
        missing = client.get("/api/profiles/missing-profile")
        restored = client.post(
            f"/api/profiles/{TARGET_A}/rollback",
            json={"backup_name": backup_name},
        )
        restored_detail = client.get(f"/api/profiles/{TARGET_A}")
        locked = client.post(f"/api/profiles/{TARGET_A}/lock", json={"locked": True})
        locked_rollback = client.post(
            f"/api/profiles/{TARGET_A}/rollback",
            json={"backup_name": backup_name},
        )

    assert summaries.status_code == 200
    assert missing.status_code == 404
    assert invalid_status.status_code == 422
    assert restored.status_code == 200
    assert restored.json()["status"] == ProfileStatus.VERIFIED
    assert restored_detail.json()["display_name"] == "Notes v1"
    assert locked.status_code == 200
    assert locked_rollback.status_code == 409


async def test_live_mode_runs_original_task_without_profile(tmp_path: Path) -> None:
    """无 verified Profile 且探索关闭时进入实时模式：任务照常执行，产物以诊断脚本保留。"""
    device = FlowDevice(tmp_path)
    orchestrator = _orchestrator(tmp_path, device)

    trace = await orchestrator.run(
        RunRequest(
            target={"bundle_name": BUNDLE_A},
            task="check target home",
            auto_generate=True,
            auto_execute=True,
            exploration_policy=ExplorationPolicy(enabled=False),
        ),
        run_id="run-live-mode",
    )

    assert trace.state == RunState.COMPLETED, trace.error
    assert trace.live_mode is True
    # 磁盘无任何 Profile：运行期为生成产物合成一份最小快照（计划 R2/G1），
    # 它不写盘、不构成晋级证据；脚本本身立即可执行（计划 G1）。
    assert trace.profile_snapshot is not None
    assert trace.profile_snapshot.provenance.evidence["live_mode"] is True
    assert trace.generated is not None
    assert trace.generated.purpose == "acceptance"
    assert trace.generated.replay_eligible is True
    assert trace.generated.promotion_eligible is False
    # 自动回放仍然关闭（失败会把整个 run 判为 FAILED_SCRIPT），但脚本已可手动执行。
    assert trace.replays == []
    assert any(event.type == EventType.PROFILE_LIVE_MODE for event in trace.events)
    assert any(event.type == EventType.ORIGINAL_TASK_STARTED for event in trace.events)
    assert any(action.step_id == "task-assert" for action in trace.actions)


async def test_failed_profile_verification_downgrades_to_live_mode(tmp_path: Path) -> None:
    """探索在静态假设备上只能得到单页，验证失败后降级实时模式继续任务而非整个 run 失败。"""
    device = FlowDevice(tmp_path)
    orchestrator = _orchestrator(tmp_path, device)

    trace = await orchestrator.run(
        RunRequest(
            target={"bundle_name": BUNDLE_A},
            task="check target home",
            auto_generate=True,
        ),
        run_id="run-verification-downgrade",
    )

    assert trace.state == RunState.COMPLETED, trace.error
    assert trace.live_mode is True
    # 失败草稿保留为证据（无定位器 ⇒ INVALID，有回收定位器 ⇒ DRAFT），实时模式仍从它生成可执行脚本。
    assert trace.profile_snapshot is not None
    expected_status = ProfileStatus.DRAFT if trace.profile_snapshot.stable_locator_inventory else ProfileStatus.INVALID
    assert trace.profile_snapshot.status == expected_status
    assert trace.generated is not None
    assert trace.generated.purpose == "acceptance"
    assert trace.generated.replay_eligible is True
    assert trace.generated.promotion_eligible is False
    live_events = [event for event in trace.events if event.type == EventType.PROFILE_LIVE_MODE]
    assert live_events
    registry = ProfileRegistry(orchestrator.settings.resolved_profiles_dir)
    assert not registry.list(ProfileStatus.VERIFIED)


async def test_bootstrap_only_still_fails_when_verification_fails(tmp_path: Path) -> None:
    """bootstrap_only 语义保持：Profile 准备失败时 run 明确失败，不降级。"""
    device = FlowDevice(tmp_path)
    orchestrator = _orchestrator(tmp_path, device)

    trace = await orchestrator.run(
        RunRequest(
            target={"bundle_name": BUNDLE_A},
            task="check target home",
            bootstrap_only=True,
        ),
        run_id="run-bootstrap-fail",
    )

    assert trace.state == RunState.FAILED_PROFILE_VERIFICATION
    assert trace.live_mode is False
