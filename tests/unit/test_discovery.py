from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from harmony_test_agent.devices import DeviceError
from harmony_test_agent.discovery import (
    ActionRisk,
    ActionRiskClassifier,
    BoundedExplorer,
    ExplorationAction,
    ExplorationPolicy,
)
from harmony_test_agent.models import CommandResult, ResolvedTarget, ScreenSnapshot
from harmony_test_agent.perception.normalizer import normalize_layout, page_path
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
        self.screenshot_labels: list[str] = []
        self.collect_calls = 0
        self.home_texts: list[str] = []
        self.home_cards: list[str] = []
        self.wrong_launch_states: list[int] = []
        self.foreign_start_after: int | None = None
        self.foreign_state = 9
        self.force_poll_state: int | None = None
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
        if self.wrong_launch_states:
            self.state = self.wrong_launch_states.pop(0)
        elif self.foreign_start_after is not None and self.start_calls > self.foreign_start_after:
            self.state = self.foreign_state
        else:
            self.state = 0
        self.foreground_bundle = bundle_name
        return _ok("start")

    def current_foreground_app(self) -> ForegroundApp:
        ability = _target().main_ability if self.foreground_bundle == _target().bundle_name else "ForeignAbility"
        return ForegroundApp(bundle_name=self.foreground_bundle, ability_name=ability, window_type="main")

    def screenshot(self, output_dir: Path, run_id: str, label: str = "screen") -> ScreenSnapshot:
        self.screenshot_calls += 1
        self.screenshot_labels.append(label)
        return self.snapshot(self.state, run_id=run_id, label=f"{label}-{self.screenshot_calls}")

    def collect_ui_hierarchy(self) -> dict:
        self.collect_calls += 1
        if self.force_poll_state is not None:
            return self._hierarchy(self.force_poll_state)
        return self._hierarchy(self.state)

    def _hierarchy(self, state: int) -> dict:
        children: list[dict[str, object]] = []
        for page in range(state + 1, self.home_actions + 1):
            children.append(
                {
                    "attributes": {
                        "key": f"open_page_{page}",
                        "text": f"Page {page}",
                        "type": "Button",
                        "clickable": "true",
                        "visible": "true",
                        "enabled": "true",
                        "bounds": f"[{page * 100},10][{page * 100 + 20},30]",
                    },
                    "children": [],
                }
            )
        if state == 0:
            for text in self.home_texts:
                children.append(
                    {
                        "attributes": {
                            "text": text,
                            "type": "Text",
                            "visible": "true",
                            "enabled": "true",
                            "bounds": "[0,100][400,140]",
                        },
                        "children": [],
                    }
                )
            for index, card_key in enumerate(self.home_cards):
                children.append(
                    {
                        "attributes": {
                            "key": card_key,
                            "text": "Card",
                            "type": "Text",
                            "clickable": "true",
                            "visible": "true",
                            "enabled": "true",
                            "bounds": f"[0,{200 + index * 160}][1080,{320 + index * 160}]",
                        },
                        "children": [],
                    }
                )
        return {"attributes": {"pagePath": f"/page/{state}"}, "children": children}

    def snapshot(self, state: int, *, run_id: str = "discovery", label: str = "expected") -> ScreenSnapshot:
        hierarchy = self._hierarchy(state)
        return ScreenSnapshot(
            snapshot_id=label,
            run_id=run_id,
            image_path=self.tmp_path / f"page-{state}.png",
            image_sha256=f"hash-{state}",
            width=1080,
            height=1920,
            page_path=page_path(hierarchy),
            elements=normalize_layout(hierarchy, 1080, 1920),
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
    [
        ("max_pages", 21),
        ("max_actions_per_page", 9),
        ("max_duration_seconds", 901),
        ("settle_timeout_seconds", 31),
        ("restore_retries", 4),
    ],
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
        ExplorationAction(
            action_id="to-one",
            kind="click",
            locator_kind="key",
            locator_value="open_page_1",
            target_text="Page 1",
        ),
        ExplorationAction(
            action_id="to-two",
            kind="click",
            locator_kind="key",
            locator_value="open_page_2",
            target_text="Page 2",
        ),
    ]
    expected = device.snapshot(2)
    device.state = 7

    restored, foreground = explorer._restore_path(path, expected, device.current_foreground_app(), sequence=10)

    assert restored.page_path == "/page/2"
    assert foreground.bundle_name == _target().bundle_name
    assert device.stop_calls == 1
    assert device.start_calls == 1


def test_snapshot_signature_ignores_volatile_texts(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path)
    explorer = _explorer(tmp_path, device)
    foreground = device.current_foreground_app()

    device.home_texts = ["正在加载首页…", "06", ":", "46"]
    loading = device.snapshot(0)
    device.home_texts = ["加载中", "06", ":", "47"]
    reloaded = device.snapshot(0)
    device.home_texts = ["推荐内容", "如何看待 DeepSeek V4 PRO 9月14日之后继续提供服务？"]
    loaded = device.snapshot(0)

    assert explorer._snapshot_signature(loading, foreground) == explorer._snapshot_signature(reloaded, foreground)
    assert explorer._snapshot_signature(loading, foreground) != explorer._snapshot_signature(loaded, foreground)


def test_restore_path_accepts_content_drift_when_structure_matches(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path)
    explorer = _explorer(tmp_path, device)
    foreground = device.current_foreground_app()

    device.home_texts = ["正在加载首页…"]
    expected = device.snapshot(0)
    device.home_texts = [
        "推荐内容",
        "如何看待 DeepSeek V4 PRO 9月14日之后继续提供服务？",
        "2178 赞同",
        "06, :, 47",
    ]

    restored, restored_foreground = explorer._restore_path([], expected, foreground, sequence=0)

    assert restored.page_path == "/page/0"
    assert restored_foreground.bundle_name == _target().bundle_name
    assert device.stop_calls == 1
    assert device.start_calls == 1


def test_restore_path_retries_until_restored_page_matches(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path)
    explorer = _explorer(tmp_path, device)
    device.wrong_launch_states = [9]
    expected = device.snapshot(0)

    restored, _ = explorer._restore_path([], expected, device.current_foreground_app(), sequence=0)

    assert restored.page_path == "/page/0"
    assert device.start_calls == 2


def test_restore_path_raises_with_diff_detail_after_retries_exhausted(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path)
    explorer = _explorer(tmp_path, device)
    device.wrong_launch_states = [9, 9, 9, 9]
    expected = device.snapshot(0)

    with pytest.raises(DeviceError) as excinfo:
        explorer._restore_path([], expected, device.current_foreground_app(), sequence=0)

    message = str(excinfo.value)
    assert "does not match the queued page structure" in message
    assert "/page/9" in message
    assert device.start_calls == 3


def test_restore_path_ignores_key_differences_when_interactive_structure_matches(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path)
    explorer = _explorer(tmp_path, device)
    foreground = device.current_foreground_app()

    device.home_texts = ["推荐内容"]
    device.home_cards = ["p2_search_hot_12"]
    expected = device.snapshot(0)
    device.home_cards = ["p2_search_history_设计师称中国客厅已失去意义"]

    restored, _ = explorer._restore_path([], expected, foreground, sequence=0)

    assert restored.page_path == "/page/0"
    assert device.start_calls == 1


def test_restore_path_fails_when_expected_interactive_structure_is_missing(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path)
    explorer = _explorer(tmp_path, device)
    foreground = device.current_foreground_app()

    device.home_cards = ["p2_home_feed_card_answer_2082075214809249635"]
    expected = device.snapshot(0)
    device.home_cards = []

    with pytest.raises(DeviceError) as excinfo:
        explorer._restore_path([], expected, foreground, sequence=0)

    message = str(excinfo.value)
    assert "missing widgets=['Text/True/False/False']" in message
    assert device.start_calls == 3


def test_explore_skips_pages_that_cannot_be_restored(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path, home_actions=3)
    device.foreign_start_after = 1
    explorer = _explorer(tmp_path, device, max_pages=3, max_actions_per_page=2)

    result = explorer.explore()

    assert result.stop_reason == "queue_exhausted"
    assert result.pages == []
    assert not any(item.success for item in result.transitions)


def test_explore_stops_candidates_on_source_page_that_cannot_be_restored(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path, home_actions=3)
    device.foreign_start_after = 2
    explorer = _explorer(tmp_path, device, max_pages=3, max_actions_per_page=3)

    result = explorer.explore()

    assert result.stop_reason == "queue_exhausted"
    assert len(result.transitions) == 1
    assert result.transitions[0].success
    assert result.pages


def test_capture_settled_returns_initial_frame_when_hierarchy_is_stable(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path)
    explorer = _explorer(tmp_path, device)

    snapshot = explorer._capture_settled("restore-005-000")

    assert snapshot.page_path == "/page/0"
    assert device.screenshot_labels == ["restore-005-000"]
    assert device.collect_calls == 1


def test_capture_settled_refreshes_when_hierarchy_never_stabilizes(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path)
    device.force_poll_state = 9
    explorer = _explorer(tmp_path, device)

    snapshot = explorer._capture_settled("restore-005-000")

    assert device.screenshot_labels == ["restore-005-000", "restore-005-000-settled"]
    assert snapshot.page_path == "/page/0"
    assert snapshot.snapshot_id.startswith("restore-005-000-settled")
