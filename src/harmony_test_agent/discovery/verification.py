"""Three-round device verification and Profile admission gates."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field

from ..devices import DeviceAdapter, DeviceError
from ..models import CommandResult, ScreenSnapshot
from ..targets import ForegroundApp, ResolvedTarget
from .explorer import BoundedExplorer, DiscoveryPage, DiscoveryResult, ExplorationAction
from .stability import (
    AssertionObservation,
    LocatorObservation,
    StabilityAnalyzer,
    StabilityReport,
)


class VerificationRound(BaseModel):
    round_number: int
    passed: bool
    snapshot_id: str | None = None
    page_signature: str | None = None
    foreground_bundle: str | None = None
    foreground_ability: str | None = None
    locator_observations: list[LocatorObservation] = Field(default_factory=list)
    resolution: tuple[int, int] | None = None
    assertion_observations: list[AssertionObservation] = Field(default_factory=list)
    recovery_passed: bool = False
    failures: list[str] = Field(default_factory=list)
    visited_page_signatures: list[str] = Field(default_factory=list)
    visited_page_identities: list[str] = Field(default_factory=list)
    action_commands: list[CommandResult] = Field(default_factory=list)
    snapshot_ids: list[str] = Field(default_factory=list)
    snapshots: list[ScreenSnapshot] = Field(default_factory=list)


class ProfileVerificationResult(BaseModel):
    target: ResolvedTarget
    rounds: list[VerificationRound] = Field(default_factory=list)
    stability: StabilityReport = Field(default_factory=StabilityReport)
    passed: bool = False
    failures: list[str] = Field(default_factory=list)
    log_paths: list[Path] = Field(default_factory=list)


AssertionProbe = Callable[[ScreenSnapshot, int, str], list[AssertionObservation]]


class ProfileVerifier:
    """Restart, launch, cross-check and collect three independent evidence rounds."""

    def __init__(
        self,
        device: DeviceAdapter,
        target: ResolvedTarget,
        output_dir: Path,
        run_id: str,
        analyzer: StabilityAnalyzer | None = None,
        should_stop: Callable[[], bool] | None = None,
        min_interaction_kinds: int = 2,
        on_round_started: Callable[[int], None] | None = None,
        on_round_finished: Callable[[VerificationRound], None] | None = None,
    ) -> None:
        self.device = device
        self.target = target
        self.output_dir = output_dir
        self.run_id = run_id
        self.analyzer = analyzer or StabilityAnalyzer()
        self.should_stop = should_stop or (lambda: False)
        self.min_interaction_kinds = max(min_interaction_kinds, 1)
        self.on_round_started = on_round_started
        self.on_round_finished = on_round_finished
        self._replay_resolver = BoundedExplorer(
            device,
            target,
            output_dir,
            run_id,
        )

    def verify(
        self,
        discovery: DiscoveryResult,
        assertion_probe: AssertionProbe | None = None,
    ) -> ProfileVerificationResult:
        result = ProfileVerificationResult(target=self.target)
        all_locators: list[LocatorObservation] = []
        all_assertions: list[AssertionObservation] = []
        self.output_dir.mkdir(parents=True, exist_ok=True)
        core_pages = self._core_pages(discovery, self.min_interaction_kinds)
        if len(core_pages) < 3:
            result.failures.append("no replayable discovery path covers 3 pages")
            self._save(result)
            return result

        def finish_round(round_result: VerificationRound) -> None:
            result.rounds.append(round_result)
            if self.on_round_finished is not None:
                self.on_round_finished(round_result)

        for round_number in range(1, 4):
            if self.on_round_started is not None:
                self.on_round_started(round_number)
            current = VerificationRound(round_number=round_number, passed=False)
            if self.should_stop():
                current.failures.append("stopped by user")
                finish_round(current)
                break
            stopped = self.device.stop_app(self.target.bundle_name)
            started = (
                self.device.start_app(self.target.bundle_name, self.target.main_ability, self.target.module_name)
                if stopped.ok
                else None
            )
            if not stopped.ok or started is None or not started.ok:
                current.failures.append("independent stop/start failed")
                finish_round(current)
                continue
            self.device.wait(0.5)

            foreground = self._target_foreground(current)
            if foreground is None:
                finish_round(current)
                continue
            snapshot = self._replay_resolver._capture_settled(f"profile-verification-{round_number}-00")
            current.resolution = (snapshot.width, snapshot.height)
            current.snapshot_id = snapshot.snapshot_id
            current.snapshot_ids.append(snapshot.snapshot_id)
            current.snapshots.append(snapshot)

            for page_index, page in enumerate(core_pages):
                if page_index:
                    previous = core_pages[page_index - 1]
                    pending = page.path_actions[len(previous.path_actions) :]
                    flow_failed = False
                    for step_index, step in enumerate(pending):
                        try:
                            action = self._replay_resolver.resolve_replay_action(step, snapshot)
                        except DeviceError as exc:
                            current.failures.append(f"core-flow locator failed: {exc}")
                            flow_failed = True
                            break
                        command = self._execute(action, snapshot)
                        current.action_commands.append(command)
                        if not command.ok:
                            current.failures.append(f"core-flow action failed: {action.action_id}")
                            flow_failed = True
                            break
                        self.device.wait(0.5)
                        snapshot = self.device.screenshot(
                            self.output_dir,
                            self.run_id,
                            f"profile-verification-{round_number}-{page_index:02d}-{step_index:02d}",
                        )
                        current.snapshot_ids.append(snapshot.snapshot_id)
                        current.snapshots.append(snapshot)
                        foreground = self._target_foreground(current)
                        if foreground is None:
                            flow_failed = True
                            break
                    if flow_failed:
                        break
                identity = BoundedExplorer._structural_identity(snapshot, foreground)
                if page.structural_identity and identity != page.structural_identity:
                    current.failures.append(f"page identity mismatch: {page.page_id}")
                    break
                signature = BoundedExplorer._snapshot_signature(snapshot, foreground)
                current.visited_page_signatures.append(signature)
                current.visited_page_identities.append(identity)
                current.page_signature = signature
                # 稳定性证据按逻辑页身份分组累计：整树签名每轮随内容抖动变化，无法跨轮聚合。
                locator_observations = self.analyzer.locator_observations(snapshot, round_number, identity)
                assertion_observations = (
                    assertion_probe(snapshot, round_number, identity)
                    if assertion_probe
                    else self._default_assertions(snapshot, round_number, identity)
                )
                current.locator_observations.extend(locator_observations)
                current.assertion_observations.extend(assertion_observations)
                all_locators.extend(locator_observations)
                all_assertions.extend(assertion_observations)

            back_result = self.device.back()
            stopped_again = self.device.stop_app(self.target.bundle_name) if back_result.ok else None
            restarted = (
                self.device.start_app(
                    self.target.bundle_name,
                    self.target.main_ability,
                    self.target.module_name,
                )
                if stopped_again and stopped_again.ok
                else None
            )
            recovered = self.device.current_foreground_app() if restarted and restarted.ok else None
            # 冷启动后截图走 settle 轮询：真实应用的开屏/异步加载需要等待窗口稳定。
            recovery_snapshot = (
                self._replay_resolver._capture_settled(
                    f"profile-verification-{round_number}-recovery",
                )
                if recovered
                else None
            )
            current.recovery_passed = bool(
                recovered
                and recovery_snapshot
                and recovered.bundle_name == self.target.bundle_name
                and (not recovered.ability_name or recovered.ability_name == self.target.main_ability)
                and BoundedExplorer._structural_identity(recovery_snapshot, recovered)
                == (
                    core_pages[0].structural_identity
                    or BoundedExplorer._structural_identity(recovery_snapshot, recovered)
                )
            )
            if not current.recovery_passed:
                current.failures.append("return and restart recovery failed")
            if len(set(current.visited_page_signatures)) < 3:
                current.failures.append("verification round did not replay 3 distinct pages")
            executed_kinds = {action.kind for action in core_pages[-1].path_actions}
            if len(executed_kinds) < self.min_interaction_kinds:
                current.failures.append(
                    f"verification round did not replay {self.min_interaction_kinds} interaction types"
                )
            current.passed = not current.failures
            log_path = self.output_dir / f"round-{round_number:02d}.hilog.txt"
            self.device.collect_logs(log_path)
            result.log_paths.append(log_path)
            finish_round(current)

        result.stability = self.analyzer.analyze(all_locators, all_assertions)
        if len(discovery.interaction_types) < self.min_interaction_kinds:
            result.failures.append(f"fewer than {self.min_interaction_kinds} interaction types")
        # 跨轮重复页按逻辑页身份判定：整树签名会被信息流内容抖动拆散。
        repeated_pages = (
            set.intersection(*(set(item.visited_page_identities) for item in result.rounds))
            if len(result.rounds) == 3
            else set()
        )
        if len(repeated_pages) < 3:
            result.failures.append("fewer than 3 pages repeated in every verification round")
        if result.stability.promotable_locator_count < 3:
            result.failures.append("fewer than 3 stable high/medium locators")
        if result.stability.app_assertion_count < 2:
            result.failures.append("fewer than 2 stable application-level assertions")
        if len(result.rounds) != 3 or not all(item.passed for item in result.rounds):
            result.failures.append("one or more device verification rounds failed")
        result.passed = not result.failures
        self._save(result)
        return result

    @staticmethod
    def _core_pages(discovery: DiscoveryResult, min_interaction_kinds: int = 2) -> list[DiscoveryPage]:
        """选取一条可回放、动作类型达标的更深路径，并携带路径前缀上的全部逻辑页。

        输入等动作不改变逻辑页身份时，中间深度的页与前一深度合并（探索器按身份去重），
        此时沿用同一页对象，保证核心流仍是完整的物理路径。
        """
        candidates = sorted(
            discovery.pages,
            key=lambda item: (len(item.path_actions), item.discovered_order),
        )
        replayable = [
            item
            for item in candidates
            if len({action.kind for action in item.path_actions}) >= max(min_interaction_kinds, 1)
        ]
        deepest = max(replayable, key=lambda item: len(item.path_actions), default=None)
        if deepest is None:
            return []
        pages: list[DiscoveryPage] = []
        carried: DiscoveryPage | None = None
        for depth in range(len(deepest.path_actions) + 1):
            page = next(
                (item for item in candidates if item.path_actions == deepest.path_actions[:depth]),
                None,
            )
            if page is None:
                page = carried
            if page is None:
                continue
            carried = page
            if page.signature not in {item.signature for item in pages}:
                pages.append(page)
        return pages[:4]

    def _target_foreground(self, current: VerificationRound) -> ForegroundApp | None:
        foreground = self.device.current_foreground_app()
        if foreground:
            current.foreground_bundle = foreground.bundle_name
            current.foreground_ability = foreground.ability_name
        if not foreground or foreground.bundle_name != self.target.bundle_name:
            current.failures.append("foreground bundle mismatch")
            return None
        if foreground.ability_name and foreground.ability_name != self.target.main_ability:
            current.failures.append("foreground Ability mismatch")
            return None
        return foreground

    def _execute(self, action: ExplorationAction, snapshot: ScreenSnapshot) -> CommandResult:
        if action.kind == "click":
            assert action.coordinate
            return self.device.click(*action.coordinate)
        if action.kind == "input":
            assert action.coordinate
            return self.device.input_text("OpenHarmony", *action.coordinate)
        if action.kind == "swipe":
            return self.device.swipe(
                (snapshot.width // 2, int(snapshot.height * 0.75)),
                (snapshot.width // 2, int(snapshot.height * 0.25)),
            )
        return self.device.back()

    @staticmethod
    def _default_assertions(
        snapshot: ScreenSnapshot, round_number: int, page_signature: str
    ) -> list[AssertionObservation]:
        stable = [item for item in snapshot.elements if item.key or item.id]
        return [
            AssertionObservation(
                round_number=round_number,
                page_signature=page_signature,
                kind="visible",
                target=item.key or item.id,
                passed=True,
                app_level=True,
            )
            for item in stable[:2]
        ]

    def _save(self, result: ProfileVerificationResult) -> None:
        path = self.output_dir / "profile-verification.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                result.model_dump(mode="json", exclude={"rounds": {"__all__": {"snapshots"}}}),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)
