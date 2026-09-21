"""Phase 3.2 回归（R9）：默认 1 轮晋级出来的 Profile 必须走三页复验，而不是入口页兜底。

原实现把定位器过滤硬编码为 ``observed_rounds >= 3``，而 ``_build_profile`` 写入的
``observed_rounds == profile_verification_rounds``（默认 1）⇒ 过滤结果恒为空 ⇒
``page_locators`` 全 None ⇒ 无条件退回 ``_legacy_entry_revalidate``（只查入口页）。
"""

from __future__ import annotations

from pathlib import Path

from fakes import element

from harmony_test_agent.agents import AgentOrchestrator
from harmony_test_agent.config import Settings
from harmony_test_agent.models import (
    AssertionDefinition,
    CommandResult,
    ResolvedTarget,
    RunTrace,
    ScreenSnapshot,
    StableLocator,
    TargetAppProfile,
)
from harmony_test_agent.storage import ArtifactStore, RunRepository
from harmony_test_agent.targets import ForegroundApp

BUNDLE = "com.example.notes"
ABILITY = "MainAbility"
PAGES = ["page-1", "page-2", "page-3"]


class RevalidationDevice:
    """记录每次复验截图 label，并总是报告定位器可见。"""

    device_id = "revalidate-device"

    def __init__(self) -> None:
        self.labels: list[str] = []
        self.clicks = 0

    def current_foreground_app(self) -> ForegroundApp:
        return ForegroundApp(bundle_name=BUNDLE, ability_name=ABILITY, window_type="main")

    def stop_app(self, bundle_name: str) -> CommandResult:
        del bundle_name
        return CommandResult(command="stop", returncode=0)

    def start_app(self, bundle_name: str, ability_name: str, module_name: str | None = None) -> CommandResult:
        del bundle_name, ability_name, module_name
        return CommandResult(command="start", returncode=0)

    def screenshot(self, output_dir: Path, run_id: str, label: str = "screen") -> ScreenSnapshot:
        output_dir.mkdir(parents=True, exist_ok=True)
        self.labels.append(label)
        image_path = output_dir / f"{label}.png"
        image_path.write_bytes(b"fake-png")
        return ScreenSnapshot(
            snapshot_id=f"snap-{label}",
            run_id=run_id,
            image_path=image_path.resolve(),
            image_sha256="fake",
            width=360,
            height=720,
            page_path="pages/Home",
            elements=[
                element("ui-1", key="page-key-1", bbox=(10, 10, 100, 100), clickable=True),
                element("ui-2", key="page-key-2", bbox=(10, 110, 100, 200), clickable=True),
                element("ui-3", key="page-key-3", bbox=(10, 210, 100, 300), clickable=True),
            ],
        )

    def click(self, x: int, y: int) -> CommandResult:
        del x, y
        self.clicks += 1
        return CommandResult(command="click", returncode=0)

    def input_text(self, text: str, x: int | None = None, y: int | None = None) -> CommandResult:
        del text, x, y
        return CommandResult(command="input", returncode=0)

    def swipe(self, start, end, duration: float = 0.5) -> CommandResult:
        del start, end, duration
        return CommandResult(command="swipe", returncode=0)

    def back(self) -> CommandResult:
        return CommandResult(command="back", returncode=0)

    def wait(self, seconds: float) -> CommandResult:
        del seconds
        return CommandResult(command="wait", returncode=0)


def _profile(rounds: int) -> TargetAppProfile:
    return TargetAppProfile(
        target_app_id="com-example-notes",
        display_name="Notes",
        bundle_name=BUNDLE,
        main_ability=ABILITY,
        reset_strategy={
            "recovery_actions": [
                {"action_id": "restore-1", "kind": "back"},
                {"action_id": "restore-2", "kind": "back"},
            ]
        },
        stable_locator_inventory=[
            StableLocator(
                name=f"locator-{index}",
                page_signature=page,
                key=f"page-key-{index}",
                observed_rounds=rounds,
                unique_match_rounds=rounds,
                evidence_snapshot_ids=[f"snap-{index}"],
            )
            for index, page in enumerate(PAGES, start=1)
        ],
        assertion_inventory=[
            AssertionDefinition(
                name=f"assertion-{index}",
                kind="visible",
                target=f"page-key-{index}",
                page_signature=page,
                observed_rounds=rounds,
                evidence_snapshot_ids=[f"snap-{index}"],
            )
            for index, page in enumerate(PAGES, start=1)
        ],
        core_flows=[{"pages": list(PAGES), "steps": [], "interaction_types": ["click", "back"]}],
    )


def _orchestrator(tmp_path: Path, rounds: int) -> AgentOrchestrator:
    settings = Settings(
        agent_provider="mock",
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "home",
        target_profile_path=None,
        profile_verification_rounds=rounds,
    )
    return AgentOrchestrator(
        settings,
        repository=RunRepository(settings.resolved_database_path),
        artifacts=ArtifactStore(settings.resolved_runtime_dir),
    )


def _resolved() -> ResolvedTarget:
    return ResolvedTarget(
        target_app_id="com-example-notes",
        display_name="Notes",
        bundle_name=BUNDLE,
        main_ability=ABILITY,
        device_id="revalidate-device",
        source="installed_app",
    )


def _trace() -> RunTrace:
    return RunTrace(run_id="run-revalidate", target_app_id="com-example-notes", task="revalidate", device_id="d")


def test_single_round_profile_uses_three_page_revalidation(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path, rounds=1)
    device = RevalidationDevice()

    passed = orchestrator._quick_revalidate(device, _resolved(), _profile(rounds=1), _trace())

    assert passed is True
    # 三页复验：标签为 revalidate-00/01/02；legacy 兜底只截一张 "revalidate"。
    assert device.labels == ["revalidate-00", "revalidate-01", "revalidate-02"]
    assert device.clicks == 0


def test_three_round_profile_also_uses_three_page_revalidation(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path, rounds=3)
    device = RevalidationDevice()

    passed = orchestrator._quick_revalidate(device, _resolved(), _profile(rounds=3), _trace())

    assert passed is True
    assert device.labels == ["revalidate-00", "revalidate-01", "revalidate-02"]


def test_insufficient_rounds_falls_back_to_legacy_entry_check(tmp_path: Path) -> None:
    """轮数确实不足（低于配置门槛）时保持兜底语义，不假装做了三页复验。"""
    orchestrator = _orchestrator(tmp_path, rounds=3)
    device = RevalidationDevice()

    passed = orchestrator._quick_revalidate(device, _resolved(), _profile(rounds=1), _trace())

    assert passed is True
    assert device.labels == ["revalidate"]


def test_missing_page_locator_mapping_falls_back_to_legacy(tmp_path: Path) -> None:
    """页面身份与定位器不在同一哈希空间（旧版 Profile）时退回入口页强定位器检查。"""
    orchestrator = _orchestrator(tmp_path, rounds=1)
    device = RevalidationDevice()
    profile = _profile(rounds=1).model_copy(
        update={
            "stable_locator_inventory": [
                item.model_copy(update={"page_signature": "legacy-tree-signature"})
                for item in _profile(rounds=1).stable_locator_inventory
            ]
        },
        deep=True,
    )

    passed = orchestrator._quick_revalidate(device, _resolved(), profile, _trace())

    assert passed is True
    assert device.labels == ["revalidate"]
