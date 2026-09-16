from __future__ import annotations

from pathlib import Path

import pytest

from harmony_test_agent.agents import AgentOrchestrator
from harmony_test_agent.discovery import (
    BoundedExplorer,
    DiscoveryPage,
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

    def collect_ui_hierarchy(self) -> dict:
        # settle 轮询读到空层级即等待超时后刷新截图，不影响确定性断言。
        return {}

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
    first = ExplorationAction(
        action_id="open-page-one",
        kind="click",
        locator_kind="key",
        locator_value=device.snapshot_for_page(0).elements[0].key,
        coordinate=(140, 140),
    )
    second = ExplorationAction(
        action_id="fixed-input",
        kind="input",
        locator_kind="key",
        locator_value=device.snapshot_for_page(1).elements[0].key,
        coordinate=(240, 140),
    )
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


@pytest.mark.parametrize("rounds", [1, 3])
def test_profile_verifier_replays_three_pages_across_independent_rounds(tmp_path: Path, rounds: int) -> None:
    """轮次可配置：1 轮（资产流水线精简默认）与 3 轮（历史行为）都必须能通过。"""
    device = FakeVerificationDevice(tmp_path)
    verifier = ProfileVerifier(
        device=device,  # type: ignore[arg-type]
        target=_target(),
        output_dir=tmp_path / "verification",
        run_id="verification-run",
        rounds=rounds,
    )

    result = verifier.verify(_discovery(device))

    assert result.passed
    assert len(result.rounds) == rounds
    assert all(item.passed for item in result.rounds)
    assert all(len(set(item.visited_page_signatures)) >= 3 for item in result.rounds)
    assert all(item.recovery_passed for item in result.rounds)
    assert device.stop_calls == 2 * rounds
    assert device.start_calls == 2 * rounds
    assert len(device.log_paths) == rounds
    assert result.stability.promotable_locator_count >= 3
    assert result.stability.app_assertion_count >= 2


def test_verification_single_round_passes_with_three_pages(tmp_path: Path) -> None:
    """1 轮内访问 3 页 + 3 个稳定定位器 + 2 个断言 → passed=True。

    这是 Phase 1 的核心收益：Profile 首次生成不再需要 3 次独立重启回放。
    """
    device = FakeVerificationDevice(tmp_path)
    verifier = ProfileVerifier(
        device=device,  # type: ignore[arg-type]
        target=_target(),
        output_dir=tmp_path / "single-round",
        run_id="single-round",
        rounds=1,
    )

    result = verifier.verify(_discovery(device))

    assert result.passed, result.failures
    assert len(result.rounds) == 1
    assert verifier.analyzer.required_rounds == 1
    assert result.stability.promotable_locator_count >= 3
    assert result.stability.app_assertion_count >= 2
    assert device.stop_calls == 2
    assert device.start_calls == 2


def test_verifier_honors_configurable_min_interaction_kinds(tmp_path: Path) -> None:
    """click+input 两条腿的路径：min=2 时可通过，min=3 时记录原因失败。"""
    device = FakeVerificationDevice(tmp_path)
    click = ExplorationAction(
        action_id="open-page-one",
        kind="click",
        locator_kind="key",
        locator_value=device.snapshot_for_page(0).elements[0].key,
        coordinate=(140, 140),
    )
    enter = ExplorationAction(
        action_id="fixed-input",
        kind="input",
        locator_kind="key",
        locator_value=device.snapshot_for_page(1).elements[0].key,
        coordinate=(240, 140),
    )
    paths = [[], [click], [click, enter]]
    foreground = device.current_foreground_app()
    pages = [
        BoundedExplorer._page(device.snapshot_for_page(index), foreground, index + 1, path)
        for index, path in enumerate(paths)
    ]
    discovery = DiscoveryResult(
        target=_target(),
        policy=ExplorationPolicy(),
        pages=pages,
        transitions=[
            DiscoveryTransition(
                source_page_id=pages[0].page_id,
                target_page_id=pages[1].page_id,
                action=click,
                before_snapshot_id="before-click",
                success=True,
            ),
            DiscoveryTransition(
                source_page_id=pages[1].page_id,
                target_page_id=pages[2].page_id,
                action=enter,
                before_snapshot_id="before-input",
                success=True,
            ),
        ],
    )

    tolerant = ProfileVerifier(
        device=device,  # type: ignore[arg-type]
        target=_target(),
        output_dir=tmp_path / "verification-min2",
        run_id="verification-min2",
        min_interaction_kinds=2,
    )
    assert tolerant.verify(discovery).passed

    strict = ProfileVerifier(
        device=device,  # type: ignore[arg-type]
        target=_target(),
        output_dir=tmp_path / "verification-min3",
        run_id="verification-min3",
        min_interaction_kinds=3,
    )
    strict_result = strict.verify(discovery)
    assert not strict_result.passed
    assert any("no replayable discovery path covers 3 pages" in item for item in strict_result.failures)


@pytest.mark.parametrize("rounds", [1, 3])
def test_stability_analyzer_rejects_dynamic_identifier_even_when_seen_in_all_rounds(
    tmp_path: Path, rounds: int
) -> None:
    device = FakeVerificationDevice(tmp_path, dynamic_locators=True)
    analyzer = StabilityAnalyzer(required_rounds=rounds)
    observations = []
    for round_number in range(1, rounds + 1):
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


@pytest.mark.parametrize("rounds", [1, 3])
def test_profile_verifier_blocks_promotion_when_only_dynamic_locators_exist(tmp_path: Path, rounds: int) -> None:
    device = FakeVerificationDevice(tmp_path, dynamic_locators=True)
    verifier = ProfileVerifier(
        device=device,  # type: ignore[arg-type]
        target=_target(),
        output_dir=tmp_path / "dynamic-verification",
        run_id="dynamic-verification-run",
        rounds=rounds,
    )

    result = verifier.verify(_discovery(device))

    assert not result.passed
    assert "fewer than 3 stable high/medium locators" in result.failures
    assert result.stability.promotable_locator_count == 0
    assert len(result.rounds) == rounds
    assert all(len(set(item.visited_page_signatures)) >= 3 for item in result.rounds)


@pytest.mark.parametrize("rounds", [1, 3])
def test_profile_verifier_streams_round_lifecycle_callbacks(tmp_path: Path, rounds: int) -> None:
    """每轮开始/结束即时回调：编排器据此逐轮推送事件，而不是验证全部结束后补发。"""
    device = FakeVerificationDevice(tmp_path)
    started: list[int] = []
    finished: list[int] = []
    verifier = ProfileVerifier(
        device=device,  # type: ignore[arg-type]
        target=_target(),
        output_dir=tmp_path / "verification-callbacks",
        run_id="verification-callbacks",
        on_round_started=started.append,
        on_round_finished=lambda round_result: finished.append(round_result.round_number),
        rounds=rounds,
    )

    result = verifier.verify(_discovery(device))

    expected = list(range(1, rounds + 1))
    assert result.passed
    assert started == expected
    assert finished == expected
    assert [round_result.round_number for round_result in result.rounds] == expected


def test_core_flow_pages_share_locator_identity_space(tmp_path: Path) -> None:
    """core_flows.pages 必须与验证定位器的页签名同空间（结构身份）。

    旧实现写入发现期整树签名（内容敏感），快速复验的页面→定位器映射恒为空，
    导致每次启动都判定 Profile 失效并重新全量探索。
    """
    device = FakeVerificationDevice(tmp_path)
    verifier = ProfileVerifier(
        device=device,  # type: ignore[arg-type]
        target=_target(),
        output_dir=tmp_path / "verification-identity",
        run_id="verification-identity",
    )
    discovery = _discovery(device)
    result = verifier.verify(discovery)
    assert result.passed

    profile = AgentOrchestrator._build_profile(_target(), discovery, result, "verification-identity")

    flow_pages = set(profile.core_flows[0]["pages"])
    locator_pages = {item.page_signature for item in profile.stable_locator_inventory}
    assert len(flow_pages) >= 3
    assert flow_pages <= locator_pages
    # 页面身份来自结构身份而不是发现期整树签名
    assert flow_pages == {page.structural_identity for page in discovery.pages if page.structural_identity}


class _DriftingHomeDevice(FakeVerificationDevice):
    """首页在验证期发生结构漂移的假设备：可注入额外控件或改变原控件可点击性。"""

    def __init__(self, tmp_path: Path, *, home_clickable: bool = True) -> None:
        super().__init__(tmp_path)
        self.home_clickable = home_clickable
        self.extra_control: str | None = None

    def snapshot_for_page(self, page: int, *, label: str = "fixture") -> ScreenSnapshot:
        snapshot = super().snapshot_for_page(page, label=label)
        if page != 0:
            return snapshot
        elements = [element.model_copy(update={"clickable": self.home_clickable}) for element in snapshot.elements]
        if self.extra_control:
            elements.append(
                UIElement(
                    element_id=self.extra_control,
                    key=self.extra_control,
                    type="Button",
                    clickable=True,
                    enabled=True,
                    bbox=BoundingBox(left=10, top=10, right=60, bottom=50),
                )
            )
        return snapshot.model_copy(update={"elements": elements})


def test_verification_first_page_accepts_structural_superset(tmp_path: Path) -> None:
    """冷启动首页比探索期多出内容控件（结构超集）时，第一页身份按子集匹配放行。"""
    device = _DriftingHomeDevice(tmp_path)
    discovery = _discovery(device)
    device.extra_control = "promo-banner"

    verifier = ProfileVerifier(
        device=device,  # type: ignore[arg-type]
        target=_target(),
        output_dir=tmp_path / "superset",
        run_id="superset-run",
    )
    result = verifier.verify(discovery)

    assert result.passed
    assert not any("page identity mismatch" in failure for failure in result.failures)


def test_verification_first_page_still_rejects_different_structure(tmp_path: Path) -> None:
    """可交互结构不同（控件不再可点击）时，子集匹配不通过，身份不匹配照常失败。"""
    device = _DriftingHomeDevice(tmp_path)
    discovery = _discovery(device)
    device.home_clickable = False

    verifier = ProfileVerifier(
        device=device,  # type: ignore[arg-type]
        target=_target(),
        output_dir=tmp_path / "different",
        run_id="different-run",
    )
    result = verifier.verify(discovery)

    assert not result.passed
    assert any(
        "page identity mismatch" in round_failure
        for round_result in result.rounds
        for round_failure in round_result.failures
    )


def test_identity_subset_requires_identity_features(tmp_path: Path) -> None:
    """旧探索结果没有身份特征字段时，子集匹配不可用（维持全等校验）。"""
    device = FakeVerificationDevice(tmp_path)
    snapshot = device.snapshot_for_page(0)
    page = DiscoveryPage(
        page_id="legacy",
        signature="legacy-signature",
        page_path=snapshot.page_path,
        bundle_name=_target().bundle_name,
        snapshot_id=snapshot.snapshot_id,
        image_path=snapshot.image_path,
        element_count=len(snapshot.elements),
        discovered_order=1,
    )

    assert ProfileVerifier._identity_subset(page, snapshot) is False


def test_discovery_page_serializes_identity_features(tmp_path: Path) -> None:
    device = FakeVerificationDevice(tmp_path)
    page = BoundedExplorer._page(device.snapshot_for_page(0), device.current_foreground_app(), 1)

    restored = DiscoveryPage.model_validate(page.model_dump(mode="json"))

    assert restored.identity_keys == page.identity_keys and restored.identity_keys
    assert restored.identity_interactive == page.identity_interactive and restored.identity_interactive
