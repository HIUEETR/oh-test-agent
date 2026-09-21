"""POST /api/profiles/{id}/replay 契约测试（Phase 4：比赛「3 次连续成功」要求）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harmony_test_agent.api.app import create_app
from harmony_test_agent.config import Settings
from harmony_test_agent.models import (
    AssertionDefinition,
    CommandResult,
    ProfileStatus,
    ReplayResult,
    StableLocator,
    TargetAppProfile,
)
from harmony_test_agent.profiles import ProfileRegistry

TARGET_APP_ID = "com-example-notes"
BUNDLE_NAME = "com.example.notes"


def _profile(*, attempts: int = 1, rounds: int = 1, status: ProfileStatus = ProfileStatus.CANDIDATE):
    return TargetAppProfile(
        status=status,
        target_app_id=TARGET_APP_ID,
        display_name="Notes",
        bundle_name=BUNDLE_NAME,
        main_ability="MainAbility",
        stable_locator_inventory=[
            StableLocator(
                name=f"locator-{index}",
                page_signature=f"page-{index}",
                key=f"key-{index}",
                observed_rounds=rounds,
                unique_match_rounds=rounds,
                evidence_snapshot_ids=[f"snap-{index}"],
            )
            for index in range(1, 4)
        ],
        assertion_inventory=[
            AssertionDefinition(
                name=f"assertion-{index}",
                kind="visible",
                target=f"key-{index}",
                page_signature=f"page-{index}",
                observed_rounds=rounds,
                evidence_snapshot_ids=[f"asnap-{index}"],
            )
            for index in range(1, 3)
        ],
        core_flows=[{"pages": ["page-1", "page-2", "page-3"], "steps": [], "interaction_types": ["click", "input"]}],
        provenance={
            "discovery_run_id": "run-profile",
            "evidence": {"verification_passed": True},
        },
    )


def _settings(tmp_path: Path, *, attempts: int = 1) -> Settings:
    return Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
        hypium_replay_attempts=attempts,
        profile_verification_rounds=1,
    )


def _registry(settings: Settings, *, attempts: int = 1) -> ProfileRegistry:
    return ProfileRegistry(
        settings.resolved_profiles_dir,
        promotion_replay_attempts=attempts,
        min_evidence_rounds=1,
    )


def _seed_script(settings: Settings) -> Path:
    path = settings.resolved_runtime_dir / "run-profile" / "generated" / "test_run_profile.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("print('noop')\n", encoding="utf-8")
    path.with_suffix(".json").write_text("{}\n", encoding="utf-8")
    return path


def _install_replay(monkeypatch, *, passed: bool):
    """替换 HypiumRunner.execute，避免真实设备执行。"""

    def fake_execute(self, generated, attempt: int = 1) -> ReplayResult:
        return ReplayResult(
            attempt=attempt,
            command=CommandResult(command="hypium", returncode=0 if passed else 1),
            passed=passed,
            status="passed" if passed else "failed",
            evidence_paths=[f"hypium/attempt-{attempt:02d}/stdout.log"],
        )

    # 统一构造点迁移到 runner/factory.py（Phase 1）：HypiumRunner 不再是 api.app 的属性，
    # 直接 patch 类方法本体，对所有构造点生效。
    monkeypatch.setattr("harmony_test_agent.runner.hypium.HypiumRunner.execute", fake_execute)


@pytest.fixture
def env(tmp_path: Path):
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, settings


@pytest.fixture
def env_factory(tmp_path: Path):
    """按门禁次数构建独立应用实例（API 内部 Registry 读取同一 Settings）。"""

    def build(attempts: int):
        settings = _settings(tmp_path / f"gate-{attempts}", attempts=attempts)
        app = create_app(settings)
        return TestClient(app), settings

    return build


def test_replay_profile_appends_evidence(env, monkeypatch) -> None:
    """已 verified 的 Profile：追加只补充审计证据，状态不变（计划 §17.6 不变式）。"""
    client, settings = env
    registry = _registry(settings)
    registry.save_candidate(_profile())
    registry.promote(TARGET_APP_ID, replay_run_ids=["run-profile:profile-attempt-1"])
    registry.update_verified(
        registry.read(TARGET_APP_ID).model_copy(
            update={
                "provenance": registry.read(TARGET_APP_ID).provenance.model_copy(
                    update={"generated_script_path": str(_seed_script(settings))}, deep=True
                )
            },
            deep=True,
        )
    )
    _install_replay(monkeypatch, passed=True)

    response = client.post(f"/api/profiles/{TARGET_APP_ID}/replay", json={"attempts": 2})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "verified"
    assert body["total_replays"] == 3
    assert body["max_replays"] == 3
    assert [item["passed"] for item in body["results"]] == [True, True]

    stored = registry.read(TARGET_APP_ID)
    assert stored.status == ProfileStatus.VERIFIED
    assert len(stored.provenance.hypium_replay_run_ids) == 3
    assert stored.provenance.evidence["consecutive_replay_passes"] == 3


@pytest.mark.parametrize("gate", [1, 3])
def test_replay_profile_promotes_after_gate_attempts(env_factory, monkeypatch, gate: int) -> None:
    """candidate 累计满门禁次数后自动晋级；门禁由 HYPIUM_REPLAY_ATTEMPTS 决定。"""
    client, settings = env_factory(gate)
    try:
        registry = _registry(settings, attempts=gate)
        registry.save_candidate(_profile())
        candidate = registry.read(TARGET_APP_ID, ProfileStatus.CANDIDATE)
        registry._atomic_write(
            registry.candidate_dir / f"{TARGET_APP_ID}.json",
            candidate.model_copy(
                update={
                    "provenance": candidate.provenance.model_copy(
                        update={"generated_script_path": str(_seed_script(settings))}, deep=True
                    )
                },
                deep=True,
            ),
        )
        _install_replay(monkeypatch, passed=True)

        response = client.post(f"/api/profiles/{TARGET_APP_ID}/replay", json={"attempts": 1})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total_replays"] == 1
        expected_status = "verified" if gate == 1 else "candidate"
        assert body["status"] == expected_status

        # 继续追加到门禁次数后必然晋级（比赛「3 次连续成功」路径）。
        remaining = gate - 1
        if remaining:
            follow_up = client.post(f"/api/profiles/{TARGET_APP_ID}/replay", json={"attempts": remaining})
            assert follow_up.status_code == 200, follow_up.text
            assert follow_up.json()["status"] == "verified"

        stored = registry.read(TARGET_APP_ID)
        assert stored.status == ProfileStatus.VERIFIED
        assert len(stored.provenance.hypium_replay_run_ids) == gate
        assert stored.provenance.hypium_replay_run_ids == [
            f"run-profile:profile-attempt-{index}" for index in range(1, gate + 1)
        ]
    finally:
        client.close()


def test_replay_profile_rejects_missing_script(env) -> None:
    client, settings = env
    registry = _registry(settings)
    registry.save_candidate(_profile())

    response = client.post(f"/api/profiles/{TARGET_APP_ID}/replay", json={"attempts": 1})

    assert response.status_code == 409
    assert "no generated Hypium script" in response.json()["detail"]


def test_replay_profile_rejects_locked_profile(env, monkeypatch) -> None:
    client, settings = env
    registry = _registry(settings)
    registry.save_candidate(_profile())
    registry.promote(TARGET_APP_ID, replay_run_ids=["run-profile:profile-attempt-1"])
    current = registry.read(TARGET_APP_ID)
    registry.update_verified(
        current.model_copy(
            update={
                "provenance": current.provenance.model_copy(
                    update={"generated_script_path": str(_seed_script(settings))}, deep=True
                )
            },
            deep=True,
        )
    )
    registry.lock(TARGET_APP_ID, locked=True)
    _install_replay(monkeypatch, passed=True)

    response = client.post(f"/api/profiles/{TARGET_APP_ID}/replay", json={"attempts": 1})

    assert response.status_code == 409
    assert "locked" in response.json()["detail"]


def test_replay_profile_validates_attempts(env) -> None:
    client, _ = env

    for attempts in (0, 4, "two"):
        response = client.post(f"/api/profiles/{TARGET_APP_ID}/replay", json={"attempts": attempts})
        assert response.status_code == 422, attempts


def test_replay_profile_not_found(env) -> None:
    client, _ = env

    response = client.post("/api/profiles/does-not-exist/replay", json={"attempts": 1})

    assert response.status_code == 404


def test_replay_profile_stops_after_failure(env, monkeypatch) -> None:
    """一次回放失败即中止后续尝试，且不晋级。"""
    client, settings = env
    registry = _registry(settings, attempts=3)
    registry.save_candidate(_profile())
    candidate = registry.read(TARGET_APP_ID, ProfileStatus.CANDIDATE)
    registry._atomic_write(
        registry.candidate_dir / f"{TARGET_APP_ID}.json",
        candidate.model_copy(
            update={
                "provenance": candidate.provenance.model_copy(
                    update={"generated_script_path": str(_seed_script(settings))}, deep=True
                )
            },
            deep=True,
        ),
    )
    _install_replay(monkeypatch, passed=False)

    response = client.post(f"/api/profiles/{TARGET_APP_ID}/replay", json={"attempts": 3})

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["passed"] is False
    assert body["status"] == "candidate"
