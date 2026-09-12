from __future__ import annotations

from pathlib import Path

from harmony_test_agent.discovery import (
    BoundedExplorer,
    DiscoveryResult,
    DiscoveryTransition,
    ExplorationAction,
    ExplorationPolicy,
    ProfileVerifier,
    StabilityAnalyzer,
)
from harmony_test_agent.models import BoundingBox, CommandResult, ResolvedTarget, ScreenSnapshot, UIElement
from harmony_test_agent.targets import ForegroundApp


def _ok(command: str = "fake") -> CommandResult:
    return CommandResult(command=command, returncode=0)


def _target() -> ResolvedTarget:
    return ResolvedTarget(
        target_app_id="verified-demo",
        display_name="Verified Demo",
        bundle_name="com.example.verified",
        main_ability="EntryAbility",
        module_name="entry",
        device_id="fake-device",
        source="installed_app",
    )


class FakeVerificationDevice:
    """Three-page device with deterministic restart and replay behavior."""

    def __init__(self, tmp_path: Path, *, dynamic_locators: bool = False) -> None:
        self.tmp_path = tmp_path
        self.dynamic_locators = dynamic_locators
        self.state = 0
        self.serial = 0
        self.stop_calls = 0
        self.start_calls = 0
        self.log_paths: list[Path] = []

    def stop_app(self, bundle_name: str) -> CommandResult:
        assert bundle_name == _target().bundle_name
        self.stop_calls += 1
        self.state = 0
        return _ok("stop")

    def start_app(self, bundle_name: str, ability_name: str, module_name: str | None = None) -> CommandResult:
        assert (bundle_name, ability_name, module_name) == (
            _target().bundle_name,
            _target().main_ability,
            _target().module_name,
        )
        self.start_calls += 1
        self.state = 0
        return _ok("start")

    def current_foreground_app(self) -> ForegroundApp:
        return ForegroundApp(
            bundle_name=_target().bundle_name,
            ability_name=_target().main_ability,
            window_type="main",
        )

    def snapshot_for_page(self, page: int, *, label: str = "fixture") -> ScreenSnapshot:
        key = f"session_{123456 + page}_control" if self.dynamic_locators else f"page_{page}_control"
        return ScreenSnapshot(
            snapshot_id=f"{label}-{page}",
            run_id="verification-run",
            image_path=self.tmp_path / f"page-{page}.png",
            image_sha256=f"hash-{page}",
            width=1080,
            height=1920,
            page_path=f"/flow/{page}",
            elements=[
                UIElement(
                    element_id=f"control-{page}",
                    key=key,
                    clickable=True,
                    bbox=BoundingBox(left=100 + page * 100, top=100, right=180 + page * 100, bottom=180),
                )
            ],
        )

    def screenshot(self, output_dir: Path, run_id: str, label: str = "screen") -> ScreenSnapshot:
        del output_dir, run_id
        self.serial += 1
        return self.snapshot_for_page(self.state, label=f"{label}-{self.serial}")

    def click(self, x: int, y: int) -> CommandResult:
        del x, y
        self.state = min(self.state + 1, 3)
        return _ok("click")

    def input_text(self, text: str, x: int | None = None, y: int | None = None) -> CommandResult:
        del text, x, y
        self.state = min(self.state + 1, 3)
        return _ok("input")

    def swipe(self, start: tuple[int, int], end: tuple[int, int], duration: float = 0.5) -> CommandResult:
        del start, end, duration
        self.state = min(self.state + 1, 3)
        return _ok("swipe")

    def back(self) -> CommandResult:
        self.state = max(0, self.state - 1)
        return _ok("back")

    def wait(self, seconds: float) -> CommandResult:
        del seconds
        return _ok("wait")

    def collect_logs(self, output_path: Path) -> CommandResult:
        self.log_paths.append(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("fake verification log", encoding="utf-8")
        return _ok("hilog")


def _discovery(device: FakeVerificationDevice) -> DiscoveryResult:
    foreground = device.current_foreground_app()
    first = ExplorationAction(action_id="open-page-one", kind="click", coordinate=(200, 140))
    second = ExplorationAction(action_id="fixed-input", kind="input", coordinate=(300, 140))
    third = ExplorationAction(action_id="scroll", kind="swipe", direction="up")
    paths = [[], [first], [first, second], [first, second, third]]
    pages = [
        BoundedExplorer._page(device.snapshot_for_page(index), foreground, index + 1, path)
        for index, path in enumerate(paths)
    ]
    transitions = [
        DiscoveryTransition(
            source_page_id=pages[index].page_id,
            target_page_id=pages[index + 1].page_id,
            action=action,
            before_snapshot_id=f"before-{index}",
            success=True,
        )
        for index, action in enumerate((first, second, third))
    ]
    return DiscoveryResult(
        target=_target(),
        policy=ExplorationPolicy(),
        pages=pages,
        transitions=transitions,
    )


def test_profile_verifier_replays_three_pages_across_three_independent_rounds(tmp_path: Path) -> None:
    device = FakeVerificationDevice(tmp_path)
    verifier = ProfileVerifier(
        device=device,  # type: ignore[arg-type]
        target=_target(),
        output_dir=tmp_path / "verification",
        run_id="verification-run",
    )

    result = verifier.verify(_discovery(device))

    assert result.passed
    assert len(result.rounds) == 3
    assert all(item.passed for item in result.rounds)
    assert all(len(set(item.visited_page_signatures)) >= 3 for item in result.rounds)
    assert all(item.recovery_passed for item in result.rounds)
    assert device.stop_calls == 6
    assert device.start_calls == 6
    assert len(device.log_paths) == 3
    assert result.stability.promotable_locator_count >= 3
    assert result.stability.app_assertion_count >= 2


def test_stability_analyzer_rejects_dynamic_identifier_even_when_seen_in_all_rounds(tmp_path: Path) -> None:
    device = FakeVerificationDevice(tmp_path, dynamic_locators=True)
    analyzer = StabilityAnalyzer()
    observations = []
    for round_number in range(1, 4):
        observations.extend(
            analyzer.locator_observations(
                device.snapshot_for_page(0, label=f"round-{round_number}"),
                round_number,
                "stable-page-signature",
            )
        )

    report = analyzer.analyze(observations, [])

    assert report.promotable_locator_count == 0
    assert report.rejected_locators["key:session_123456_control"] == "identifier appears dynamic"


def test_profile_verifier_blocks_promotion_when_only_dynamic_locators_exist(tmp_path: Path) -> None:
    device = FakeVerificationDevice(tmp_path, dynamic_locators=True)
    verifier = ProfileVerifier(
        device=device,  # type: ignore[arg-type]
        target=_target(),
        output_dir=tmp_path / "dynamic-verification",
        run_id="dynamic-verification-run",
    )

    result = verifier.verify(_discovery(device))

    assert not result.passed
    assert "fewer than 3 stable high/medium locators" in result.failures
    assert result.stability.promotable_locator_count == 0
    assert len(result.rounds) == 3
    assert all(len(set(item.visited_page_signatures)) >= 3 for item in result.rounds)
