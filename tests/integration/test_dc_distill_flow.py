"""DC 蒸馏端到端集成测试（离线假设备）。

覆盖链路：DC 会话（含断言工具）→ 纯 CPU 蒸馏 → 1 轮设备验证 → 1 次 Hypium 回放
→ verified Profile → 追加 2 次回放证据（比赛「3 次连续成功」要求）。

设备、Hypium 执行与 ProfileVerifier 全部用离线替身，因此不依赖真机；真实设备验收
见计划 §6.7 与最终报告。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from harmony_test_agent.api.app import create_app
from harmony_test_agent.config import Settings
from harmony_test_agent.dc.distill import DcProfileDistiller
from harmony_test_agent.dc.models import DcToolInvocation, DcToolName, DcToolTier, utc_now
from harmony_test_agent.discovery import ProfileVerificationResult, StabilityReport, VerificationRound
from harmony_test_agent.models import (
    BoundingBox,
    CommandResult,
    ProfileStatus,
    ReplayResult,
    ScreenSnapshot,
    UIElement,
)
from harmony_test_agent.profiles import ProfileRegistry

BUNDLE = "com.example.notes"
ABILITY = "MainAbility"


def _element(key: str, content: str, page: int) -> UIElement:
    return UIElement(
        element_id=key,
        key=key,
        content=content,
        type="Button",
        clickable=True,
        enabled=True,
        bbox=BoundingBox(left=20 * page, top=40, right=20 * page + 120, bottom=90),
    )


def _invocation(
    tool: DcToolName,
    *,
    page: int,
    args: dict[str, Any] | None = None,
    element: UIElement | None = None,
    after_snapshot_id: str | None = None,
    invocation_id: str,
) -> DcToolInvocation:
    return DcToolInvocation(
        invocation_id=invocation_id,
        turn_id="turn-1",
        tool=tool,
        tier=DcToolTier.L1,
        args=args or {},
        success=True,
        started_at=utc_now(),
        ended_at=utc_now(),
        duration_ms=4,
        resolved_element=element,
        page_path=f"pages/Page{page}",
        after_snapshot_id=after_snapshot_id,
    )


def _seed_session(client: TestClient) -> str:
    """构造一条覆盖 3 页、含断言工具的 DC 会话。"""
    session = client.app.state.dc_manager.create()
    for page in (1, 2, 3):
        session.snapshot_holder.record(
            ScreenSnapshot(
                snapshot_id=f"snap-{page}",
                run_id=session.session_id,
                image_path=Path(f"page-{page}.png"),
                image_sha256=f"sha-{page}",
                width=1080,
                height=1920,
                page_path=f"pages/Page{page}",
                elements=[_element(f"page{page}_control", f"Page {page}", page)],
            )
        )
    session.recorder.invocations.extend(
        [
            _invocation(
                DcToolName.CLICK,
                page=1,
                args={"x": 40, "y": 60},
                element=_element("page1_control", "Page 1", 1),
                after_snapshot_id="snap-2",
                invocation_id="inv-001",
            ),
            _invocation(
                DcToolName.INPUT_TEXT,
                page=2,
                args={"text": "OpenHarmony", "coordinate": [60, 200]},
                element=_element("page2_control", "Page 2", 2),
                after_snapshot_id="snap-3",
                invocation_id="inv-002",
            ),
            _invocation(
                DcToolName.ASSERT_VISIBLE,
                page=3,
                args={"target": "Page 3"},
                after_snapshot_id="snap-3",
                invocation_id="inv-003",
            ),
            _invocation(
                DcToolName.ASSERT_TEXT,
                page=3,
                args={"target": "Page 3"},
                after_snapshot_id="snap-3",
                invocation_id="inv-004",
            ),
            _invocation(
                DcToolName.SWIPE,
                page=3,
                args={"start": [500, 1500], "end": [500, 500]},
                after_snapshot_id="snap-3",
                invocation_id="inv-005",
            ),
        ]
    )
    return session.session_id


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
        hypium_replay_attempts=1,
        profile_verification_rounds=1,
    )


def test_dc_session_distills_to_verified_profile_with_replay_evidence(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    with (
        patch("harmony_test_agent.dc.session.HarmonyDeviceAdapter.connect", return_value=None),
        patch(
            "harmony_test_agent.dc.session.HarmonyDeviceAdapter.health_check",
            return_value={"connected": True, "id": "mock-device"},
        ),
        TestClient(app) as client,
    ):
        session_id = _seed_session(client)
        registry = ProfileRegistry(
            settings.resolved_profiles_dir,
            promotion_replay_attempts=1,
            min_evidence_rounds=1,
        )
        _stub_device_pipeline(monkeypatch, session_id, registry)

        response = client.post(
            f"/api/dc/sessions/{session_id}/profile/distill",
            json={"bundle_name": BUNDLE, "main_ability": ABILITY},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "verified"
        assert body["pages_covered"] >= 3
        assert body["replay_passed"] is True
        assert body["profile_id"] == f"dc-{session_id}"

        stored = registry.read(f"dc-{session_id}", ProfileStatus.VERIFIED)
        assert stored.bundle_name == BUNDLE
        assert stored.provenance.generated_script_path
        assert Path(stored.provenance.generated_script_path).is_file()
        assert stored.provenance.hypium_replay_run_ids == [f"{session_id}:profile-attempt-1"]

        # 追加 2 次异步回放 → 累计 3 次「连续成功」证据（比赛硬性要求）。
        replay_response = client.post(f"/api/profiles/dc-{session_id}/replay", json={"attempts": 2})
        assert replay_response.status_code == 200, replay_response.text
        assert replay_response.json()["total_replays"] == 3

        final = registry.read(f"dc-{session_id}", ProfileStatus.VERIFIED)
        assert len(final.provenance.hypium_replay_run_ids) == 3
        assert final.provenance.evidence["consecutive_replay_passes"] == 3


def _stub_device_pipeline(monkeypatch, session_id: str, registry: ProfileRegistry) -> None:
    """替换设备验证与 Hypium 执行，返回与真实链路同构的通过结果。"""

    def fake_verify(self, discovery, assertion_probe=None):  # type: ignore[no-untyped-def]
        rounds = [
            VerificationRound(
                round_number=index,
                passed=True,
                snapshot_ids=[f"snap-{index}"],
                snapshots=[
                    ScreenSnapshot(
                        snapshot_id=f"verification-{index}",
                        run_id=self.run_id,
                        image_path=Path(f"verification-{index}.png"),
                        image_sha256=f"vsha-{index}",
                        width=1080,
                        height=1920,
                        page_path="pages/Page1",
                        elements=[_element("page1_control", "Page 1", 1)],
                    )
                ],
                visited_page_signatures=["pages/Page1", "pages/Page2", "pages/Page3"],
                recovery_passed=True,
            )
            for index in range(1, self.rounds + 1)
        ]
        return ProfileVerificationResult(
            target=self.target,
            rounds=rounds,
            stability=StabilityReport(),
            passed=True,
        )

    def fake_candidate(self, prepared, verification):  # type: ignore[no-untyped-def]
        from harmony_test_agent.models import (
            AssertionDefinition,
            StableLocator,
            TargetAppProfile,
        )

        ordered = sorted(prepared.discovery.pages, key=lambda item: len(item.path_actions))
        identities: list[str] = []
        for page in ordered:
            key = page.structural_identity or page.signature
            if key not in identities:
                identities.append(key)
        identities = identities[:3]
        return TargetAppProfile(
            status=ProfileStatus.CANDIDATE,
            target_app_id=prepared.target.target_app_id,
            display_name="DC Distilled",
            bundle_name=prepared.target.bundle_name,
            main_ability=prepared.target.main_ability,
            stable_locator_inventory=[
                StableLocator(
                    name=f"locator-{index}",
                    page_signature=identity,
                    key=f"dc_key_{index}",
                    observed_rounds=1,
                    unique_match_rounds=1,
                    evidence_snapshot_ids=[f"snap-{index}"],
                )
                for index, identity in enumerate(identities, 1)
            ],
            assertion_inventory=[
                AssertionDefinition(
                    name=f"assertion-{index}",
                    kind="visible",
                    target=identity,
                    page_signature=identity,
                    observed_rounds=1,
                    evidence_snapshot_ids=[f"asnap-{index}"],
                )
                for index, identity in enumerate(identities[:2], 1)
            ],
            core_flows=[{"pages": identities, "steps": [], "interaction_types": ["click", "input"]}],
            provenance={
                "discovery_run_id": prepared.run_id,
                "evidence": {"verification_passed": True},
            },
        )

    def fake_execute(self, generated, attempt: int = 1) -> ReplayResult:
        return ReplayResult(
            attempt=attempt,
            command=CommandResult(command="hypium", returncode=0),
            passed=True,
            status="passed",
            evidence_paths=[f"hypium/attempt-{attempt:02d}/stdout.log"],
        )

    monkeypatch.setattr("harmony_test_agent.discovery.verification.ProfileVerifier.verify", fake_verify)
    monkeypatch.setattr(DcProfileDistiller, "_candidate_from_verification", fake_candidate)
    monkeypatch.setattr("harmony_test_agent.runner.hypium.HypiumRunner.execute", fake_execute)


def test_distill_endpoint_requires_recorded_operations(tmp_path: Path) -> None:
    """空会话直接 409：蒸馏前置条件是「有可蒸馏的会话记录」。"""
    settings = _settings(tmp_path)
    app = create_app(settings)
    with (
        patch("harmony_test_agent.dc.session.HarmonyDeviceAdapter.connect", return_value=None),
        patch(
            "harmony_test_agent.dc.session.HarmonyDeviceAdapter.health_check",
            return_value={"connected": True, "id": "mock-device"},
        ),
        TestClient(app) as client,
    ):
        session = client.app.state.dc_manager.create()
        response = client.post(
            f"/api/dc/sessions/{session.session_id}/profile/distill",
            json={"bundle_name": BUNDLE, "main_ability": ABILITY},
        )

    assert response.status_code == 409


@pytest.mark.parametrize("attempts", [1, 2, 3])
def test_async_replay_attempt_bounds(tmp_path: Path, attempts: int) -> None:
    """端点接受 1..3 次追加；超出范围由 pydantic/端点校验拒绝。"""
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post("/api/profiles/unknown/replay", json={"attempts": attempts})

    # 未知 Profile → 404（说明 attempts 校验已通过，进入业务分支）
    assert response.status_code == 404
