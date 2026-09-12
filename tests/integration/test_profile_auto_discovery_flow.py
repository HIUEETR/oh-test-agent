from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from harmony_test_agent.agents import AgentOrchestrator, MockAgentProvider
from harmony_test_agent.api import create_app
from harmony_test_agent.config import Settings
from harmony_test_agent.devices import DeviceAdapter
from harmony_test_agent.discovery import (
    AssertionObservation,
    DiscoveryPage,
    DiscoveryResult,
    DiscoveryTransition,
    ExplorationAction,
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
    async def plan(self, task: str, profile: TargetAppProfile, max_steps: int) -> PlanResult:
        del profile
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
) -> TargetAppProfile:
    page_locators = [
        StableLocator(
            name=f"page-{index}",
            page_signature=f"page-{index}",
            key=f"page-key-{index}",
            observed_rounds=3,
            unique_match_rounds=3,
            evidence_snapshot_ids=[f"page-{index}-round-{round_number}" for round_number in range(1, 4)],
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
                observed_rounds=3,
                evidence_snapshot_ids=[f"assertion-{index}-round-{round_number}" for round_number in range(1, 4)],
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


def _promote(registry: ProfileRegistry, profile: TargetAppProfile) -> TargetAppProfile:
    registry.save_candidate(profile)
    registry.promote(
        profile.target_app_id,
        replay_run_ids=[
            "run-profile:profile-attempt-1",
            "run-profile:profile-attempt-2",
            "run-profile:profile-attempt-3",
        ],
    )
    return registry.read(profile.target_app_id)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
        unchanged_screen_limit=2,
    )


def _orchestrator(tmp_path: Path, device: FlowDevice) -> AgentOrchestrator:
    settings = _settings(tmp_path)
    return AgentOrchestrator(
        settings,
        provider=OriginalTaskProvider(),
        repository=RunRepository(settings.resolved_database_path),
        artifacts=ArtifactStore(settings.resolved_runtime_dir),
        device_factory=lambda _: device,
        settle_seconds=0,
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


def _verification_result(*, passed: bool = True) -> ProfileVerificationResult:
    target = _resolved()
    page_signatures = [f"page-{index}" for index in range(1, 5)]
    locator_evidence = [
        StableLocatorEvidence(
            name=f"stable-{index}",
            kind=LocatorKind.KEY,
            value=f"stable-key-{index}",
            level=StabilityLevel.HIGH,
            rounds=(1, 2, 3),
            page_signatures=(signature,),
            unique_each_round=True,
        )
        for index, signature in enumerate(page_signatures, 1)
    ]
    assertion_evidence = [
        StableAssertionEvidence(
            kind="visible",
            target=f"stable-key-{index}",
            rounds=(1, 2, 3),
            page_signatures=(page_signatures[index - 1],),
        )
        for index in range(1, 3)
    ]
    rounds = []
    for round_number in range(1, 4):
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
        rounds.append(
            VerificationRound(
                round_number=round_number,
                passed=passed,
                recovery_passed=passed,
                visited_page_signatures=page_signatures if passed else page_signatures[:1],
                locator_observations=locator_observations,
                assertion_observations=assertion_observations,
                resolution=(360, 720),
            )
        )
    return ProfileVerificationResult(
        target=target,
        rounds=rounds,
        stability=StabilityReport(
            locators=locator_evidence if passed else [],
            assertions=assertion_evidence if passed else [],
        ),
        passed=passed,
        failures=[] if passed else ["three-round verification failed"],
    )


def _patch_discovery(monkeypatch, *, verification_passed: bool) -> None:
    monkeypatch.setattr(
        "harmony_test_agent.agents.orchestrator.BoundedExplorer.explore",
        lambda self: _discovery_result(),
    )
    monkeypatch.setattr(
        "harmony_test_agent.agents.orchestrator.ProfileVerifier.verify",
        lambda self, discovery: _verification_result(passed=verification_passed),
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


async def test_same_run_discovers_promotes_then_executes_original_task(
    tmp_path: Path,
    monkeypatch,
) -> None:
    device = FlowDevice(tmp_path)
    orchestrator = _orchestrator(tmp_path, device)
    _patch_discovery(monkeypatch, verification_passed=True)

    async def successful_replays(runner, generated, run_id: str, attempts: int):
        del runner, generated, run_id
        assert attempts == 3
        return _replays(True, True, True)

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
    assert len(trace.profile_validation_replays) == 3
    assert stored.status == ProfileStatus.VERIFIED
    assert stored.provenance.discovery_run_id == trace.run_id
    assert event_types.index(EventType.PROFILE_PROMOTED) < event_types.index(EventType.ORIGINAL_TASK_STARTED)
    assert any(action.step_id == "task-assert" for action in trace.actions)


async def test_provisional_profile_blocks_formal_hypium_generation_and_execution(
    tmp_path: Path,
    monkeypatch,
) -> None:
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

    draft = ProfileRegistry(orchestrator.settings.resolved_profiles_dir).read(
        TARGET_A,
        ProfileStatus.INVALID,
    )
    assert trace.state == RunState.COMPLETED, trace.error
    assert trace.provisional is True
    assert draft.status == ProfileStatus.INVALID
    assert trace.generated is None
    assert trace.replays == []
    assert replay_called is False


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
    orchestrator = _orchestrator(tmp_path, device)
    _patch_discovery(monkeypatch, verification_passed=True)

    async def failing_replays(runner, generated, run_id: str, attempts: int):
        del runner, generated, run_id
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
