from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from harmony_test_agent.discovery import (
    ActionRisk,
    ActionRiskClassifier,
    BoundedExplorer,
    ExplorationAction,
    ExplorationPolicy,
)
from harmony_test_agent.models import BoundingBox, CommandResult, ResolvedTarget, ScreenSnapshot, UIElement
from harmony_test_agent.runtime.safety import SafetyPolicy
from harmony_test_agent.targets import ForegroundApp


def _ok(command: str = "fake") -> CommandResult:
    return CommandResult(command=command, returncode=0)


def _target() -> ResolvedTarget:
    return ResolvedTarget(
        target_app_id="demo",
        display_name="Demo",
        bundle_name="com.example.demo",
        main_ability="EntryAbility",
        module_name="entry",
        device_id="fake-device",
        source="installed_app",
    )


class FakeDiscoveryDevice:
    """Small deterministic device whose click coordinates select a numbered page."""

    def __init__(self, tmp_path: Path, *, home_actions: int = 3) -> None:
        self.tmp_path = tmp_path
        self.home_actions = home_actions
        self.state = 0
        self.foreground_bundle = _target().bundle_name
        self.back_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.screenshot_calls = 0
        self.last_catalog_raw = "fake installed-app catalog"

    def list_installed_apps(self) -> list[object]:
        return []

    def stop_app(self, bundle_name: str) -> CommandResult:
        assert bundle_name == _target().bundle_name
        self.stop_calls += 1
        self.state = 0
        self.foreground_bundle = bundle_name
        return _ok("stop")

    def start_app(self, bundle_name: str, ability_name: str, module_name: str | None = None) -> CommandResult:
        assert (bundle_name, ability_name, module_name) == (
            _target().bundle_name,
            _target().main_ability,
            _target().module_name,
        )
        self.start_calls += 1
        self.state = 0
        self.foreground_bundle = bundle_name
        return _ok("start")

    def current_foreground_app(self) -> ForegroundApp:
        ability = _target().main_ability if self.foreground_bundle == _target().bundle_name else "ForeignAbility"
        return ForegroundApp(bundle_name=self.foreground_bundle, ability_name=ability, window_type="main")

    def screenshot(self, output_dir: Path, run_id: str, label: str = "screen") -> ScreenSnapshot:
        self.screenshot_calls += 1
        return self.snapshot(self.state, run_id=run_id, label=f"{label}-{self.screenshot_calls}")

    def snapshot(self, state: int, *, run_id: str = "discovery", label: str = "expected") -> ScreenSnapshot:
        elements: list[UIElement] = []
        if state == 0:
            for page in range(1, self.home_actions + 1):
                elements.append(
                    UIElement(
                        element_id=f"open-{page}",
                        key=f"open_page_{page}",
                        content=f"Page {page}",
                        type="Button",
                        clickable=True,
                        bbox=BoundingBox(left=page * 100, top=10, right=page * 100 + 20, bottom=30),
                    )
                )
        return ScreenSnapshot(
            snapshot_id=label,
            run_id=run_id,
            image_path=self.tmp_path / f"page-{state}.png",
            image_sha256=f"hash-{state}",
            width=1080,
            height=1920,
            page_path=f"/page/{state}",
            elements=elements,
        )

    def click(self, x: int, y: int) -> CommandResult:
        del y
        if x == 999:
            self.foreground_bundle = "com.example.external"
        else:
            self.state = x // 100
        return _ok("click")

    def input_text(self, text: str, x: int | None = None, y: int | None = None) -> CommandResult:
        del text, x, y
        return _ok("input")

    def swipe(self, start: tuple[int, int], end: tuple[int, int], duration: float = 0.5) -> CommandResult:
        del start, end, duration
        return _ok("swipe")

    def back(self) -> CommandResult:
        self.back_calls += 1
        self.foreground_bundle = _target().bundle_name
        self.state = max(0, self.state - 1)
        return _ok("back")

    def wait(self, seconds: float) -> CommandResult:
        del seconds
        return _ok("wait")

    def collect_logs(self, output_path: Path) -> CommandResult:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("fake log", encoding="utf-8")
        return _ok("hilog")


def _explorer(tmp_path: Path, device: FakeDiscoveryDevice, **policy: object) -> BoundedExplorer:
    return BoundedExplorer(
        device=device,  # type: ignore[arg-type]
        target=_target(),
        output_dir=tmp_path / "discovery",
        run_id="run-discovery",
        policy=ExplorationPolicy(**policy),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("max_pages", 21), ("max_actions_per_page", 9), ("max_duration_seconds", 901)],
)
def test_exploration_policy_rejects_limits_above_hard_caps(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        ExplorationPolicy(**{field: value})


def test_explorer_enforces_page_and_per_page_action_limits_with_fake_device(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path, home_actions=4)

    result = _explorer(tmp_path, device, max_pages=2, max_actions_per_page=2).explore()

    assert len(result.pages) == 2
    home_transitions = [item for item in result.transitions if item.source_page_id == result.pages[0].page_id]
    assert len(home_transitions) == 2
    assert all(item.success for item in home_transitions)


def test_risk_classifier_keeps_forbidden_actions_blocked_and_requires_explicit_opt_in() -> None:
    classifier = ActionRiskClassifier()

    forbidden, forbidden_reason, permission = classifier.classify("确认购买并支付", ExplorationPolicy())
    login_blocked, _, login_permission = classifier.classify("登录账户", ExplorationPolicy())
    login_allowed, _, allowed_permission = classifier.classify("登录账户", ExplorationPolicy(allow_login=True))
    uncertain, _, _ = classifier.classify("输入验证码", ExplorationPolicy())

    assert (forbidden, permission) == (ActionRisk.FORBIDDEN, None)
    assert "permanently forbidden" in forbidden_reason
    assert (login_blocked, login_permission) == (ActionRisk.REQUIRES_OPT_IN, "login")
    assert (login_allowed, allowed_permission) == (ActionRisk.DEFAULT_ALLOWED, "login")
    assert uncertain == ActionRisk.BLOCKED_UNCERTAIN
    assert SafetyPolicy(ExplorationPolicy())._blocked_match("确认提交") == "确认"
    assert SafetyPolicy(ExplorationPolicy(allow_submit=True))._blocked_match("确认提交") is None


def test_cross_bundle_navigation_is_blocked_and_target_is_recovered(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path)
    explorer = _explorer(tmp_path, device)
    before = device.snapshot(0)
    foreground = device.current_foreground_app()
    page = explorer._page(before, foreground, 1)
    action = ExplorationAction(
        action_id="leave-app",
        kind="click",
        coordinate=(999, 20),
        target_text="Open external app",
    )

    transition, after = explorer._perform(page, before, foreground, action, 0)

    assert after is None
    assert not transition.success
    assert transition.foreground_after is not None
    assert transition.foreground_after.bundle_name == "com.example.external"
    assert transition.blocked_reason == "cross-bundle navigation blocked: com.example.external"
    assert device.back_calls == 1
    assert device.current_foreground_app().bundle_name == _target().bundle_name


def test_restore_path_relaunches_and_replays_every_action_to_expected_page(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path)
    explorer = _explorer(tmp_path, device)
    path = [
        ExplorationAction(action_id="to-one", kind="click", coordinate=(110, 20), target_text="Page 1"),
        ExplorationAction(action_id="to-two", kind="click", coordinate=(210, 20), target_text="Page 2"),
    ]
    expected = device.snapshot(2)
    device.state = 7

    restored, foreground = explorer._restore_path(path, expected, device.current_foreground_app(), sequence=10)

    assert restored.page_path == "/page/2"
    assert foreground.bundle_name == _target().bundle_name
    assert device.stop_calls == 1
    assert device.start_calls == 1
