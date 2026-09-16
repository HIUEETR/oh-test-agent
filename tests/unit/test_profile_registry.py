from __future__ import annotations

import json
from pathlib import Path

import pytest

from harmony_test_agent.models import (
    AssertionDefinition,
    ProfileStatus,
    StableLocator,
    TargetAppProfile,
)
from harmony_test_agent.profiles import (
    ProfileLockedError,
    ProfileRegistry,
    ProfileRegistryError,
    ProfileTransitionError,
    load_compatible_profile,
)

TARGET_APP_ID = "com-example-notes"
BUNDLE_NAME = "com.example.notes"


def profile(display_name: str = "Pocket Notes", *, rounds: int = 1) -> TargetAppProfile:
    """构造一个满足准入门禁的 Profile；``rounds`` 决定证据轮次（默认 1 轮）。"""
    return TargetAppProfile(
        target_app_id=TARGET_APP_ID,
        display_name=display_name,
        bundle_name=BUNDLE_NAME,
        main_ability="MainAbility",
        module_name="entry",
        stable_locator_inventory=[
            StableLocator(
                name=f"locator-{index}",
                page_signature=f"page-{index}",
                key=f"key-{index}",
                observed_rounds=rounds,
                unique_match_rounds=rounds,
                evidence_snapshot_ids=[
                    f"snapshot-{index}-round-{round_number}" for round_number in range(1, rounds + 1)
                ],
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


def make_registry(tmp_path: Path, *, rounds: int = 1, attempts: int = 1) -> ProfileRegistry:
    """构造门禁参数显式的 Registry，避免用例依赖进程级 Settings。"""
    return ProfileRegistry(
        tmp_path / "profiles",
        promotion_replay_attempts=attempts,
        min_evidence_rounds=rounds,
    )


def replay_ids_for(attempts: int = 1) -> list[str]:
    return [f"run-profile:profile-attempt-{attempt}" for attempt in range(1, attempts + 1)]


def promote_profile(
    registry: ProfileRegistry,
    value: TargetAppProfile,
    replay_ids: tuple[str, ...] | None = None,
) -> Path:
    registry.save_candidate(value)
    return registry.promote(
        value.target_app_id,
        replay_run_ids=list(replay_ids) if replay_ids is not None else replay_ids_for(),
    )


def test_promotion_rejects_candidates_without_admission_assets(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    empty = TargetAppProfile(
        target_app_id=TARGET_APP_ID,
        display_name="Empty",
        bundle_name=BUNDLE_NAME,
        main_ability="MainAbility",
    )
    with pytest.raises(ProfileTransitionError, match="three stable locators"):
        registry.save_candidate(empty)


@pytest.mark.parametrize("rounds", [1, 3])
def test_admission_evidence_rounds_are_configurable(tmp_path: Path, rounds: int) -> None:
    """准入门禁的「轮次」维度可配置：1 轮（精简默认）与 3 轮（历史）都要能晋级。"""
    registry = make_registry(tmp_path, rounds=rounds, attempts=1)
    value = profile(f"Rounds {rounds}", rounds=rounds)

    candidate_path = registry.save_candidate(value)

    assert candidate_path.exists()
    verified = load_compatible_profile(registry.promote(TARGET_APP_ID, replay_run_ids=replay_ids_for(1)))
    assert verified.status == ProfileStatus.VERIFIED


def test_admission_rejects_insufficient_evidence_rounds(tmp_path: Path) -> None:
    """配 3 轮门禁时，只有 1 轮证据的 Profile 不得进入 candidate。"""
    registry = make_registry(tmp_path, rounds=3, attempts=1)

    with pytest.raises(ProfileTransitionError, match="3-round unique locator evidence"):
        registry.save_candidate(profile("One Round Only", rounds=1))


def test_registry_persists_draft_candidate_and_verified_lifecycle(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    value = profile()

    draft_path = registry.save_draft(value)
    assert draft_path == registry.draft_dir / f"{TARGET_APP_ID}.json"
    assert load_compatible_profile(draft_path).status == ProfileStatus.DRAFT

    candidate_path = registry.save_candidate(value)
    assert candidate_path == registry.candidate_dir / f"{TARGET_APP_ID}.json"
    assert registry.read(TARGET_APP_ID, ProfileStatus.CANDIDATE).status == ProfileStatus.CANDIDATE

    with pytest.raises(ProfileTransitionError, match="independent Hypium replay run ID"):
        registry.promote(TARGET_APP_ID, replay_run_ids=["same-run", "same-run", "same-run"])
    assert candidate_path.exists()
    assert not (registry.root / f"{TARGET_APP_ID}.json").exists()

    verified_path = registry.promote(
        TARGET_APP_ID,
        replay_run_ids=[
            "run-profile:profile-attempt-1",
            "run-profile:profile-attempt-2",
            "run-profile:profile-attempt-3",
        ],
    )
    verified = load_compatible_profile(verified_path)

    assert verified_path == registry.root / f"{TARGET_APP_ID}.json"
    assert verified.status == ProfileStatus.VERIFIED
    assert verified.provenance.verified_at is not None
    assert verified.provenance.hypium_replay_run_ids == [
        "run-profile:profile-attempt-1",
        "run-profile:profile-attempt-2",
        "run-profile:profile-attempt-3",
    ]
    assert not candidate_path.exists()
    assert not draft_path.exists()
    assert list(registry.root.rglob("*.tmp")) == []


@pytest.mark.parametrize("attempts", [1, 3])
def test_promote_requires_configurable_replay_ids(tmp_path: Path, attempts: int) -> None:
    """晋级门禁的 replay 次数由 Settings.hypium_replay_attempts 决定。

    ``attempts=1``（精简默认）时 1 个 replay ID 即可晋级；``attempts=3``（历史行为）
    时必须 3 个，少于门禁次数一律拒绝。
    """
    registry = make_registry(tmp_path, attempts=attempts)
    registry.save_candidate(profile("Configurable"))

    if attempts > 1:
        with pytest.raises(ProfileTransitionError, match=f"requires {attempts} independent"):
            registry.promote(TARGET_APP_ID, replay_run_ids=replay_ids_for(1))
        assert registry.get_any(target_app_id=TARGET_APP_ID).status == ProfileStatus.CANDIDATE

    verified = load_compatible_profile(registry.promote(TARGET_APP_ID, replay_run_ids=replay_ids_for(attempts)))
    assert verified.status == ProfileStatus.VERIFIED
    assert len(verified.provenance.hypium_replay_run_ids) == attempts


def test_atomic_write_preserves_previous_profile_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = make_registry(tmp_path)
    path = registry.save_draft(profile("Original Name"))
    original_bytes = path.read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError(f"cannot replace {destination} with {source}")

    monkeypatch.setattr("harmony_test_agent.profiles.registry.os.replace", fail_replace)

    with pytest.raises(OSError, match="cannot replace"):
        registry.save_draft(profile("New Name"))

    assert path.read_bytes() == original_bytes
    assert load_compatible_profile(path).display_name == "Original Name"
    assert list(registry.root.rglob("*.tmp")) == []


def test_locked_verified_profile_rejects_automatic_replacement(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    verified_path = promote_profile(registry, profile("Approved Name"))

    locked = registry.lock(TARGET_APP_ID)
    assert locked.locked is True
    assert load_compatible_profile(verified_path).locked is True

    candidate_path = registry.save_candidate(profile("Discovered Replacement"))
    with pytest.raises(ProfileLockedError, match="locked"):
        registry.promote(
            TARGET_APP_ID,
            replay_run_ids=[
                "run-profile:profile-attempt-1",
                "run-profile:profile-attempt-2",
                "run-profile:profile-attempt-3",
            ],
        )

    assert registry.read(TARGET_APP_ID).display_name == "Approved Name"
    assert registry.read(TARGET_APP_ID).locked is True
    assert candidate_path.exists()
    assert registry.history(BUNDLE_NAME) == []


def test_replacement_creates_history_and_rollback_restores_prior_version(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    promote_profile(registry, profile("Version One"))
    promote_profile(
        registry,
        profile("Version Two"),
        (
            "run-profile:profile-attempt-1",
            "run-profile:profile-attempt-2",
            "run-profile:profile-attempt-3",
        ),
    )

    history_before_rollback = registry.history(BUNDLE_NAME)
    assert len(history_before_rollback) == 1
    assert history_before_rollback[0].display_name == "Version One"
    assert history_before_rollback[0].status == ProfileStatus.SUPERSEDED
    assert registry.read(TARGET_APP_ID).display_name == "Version Two"

    previous_path = next(
        path
        for path in registry.history_dir.rglob("*.json")
        if load_compatible_profile(path).display_name == "Version One"
    )
    restored_path = registry.rollback(TARGET_APP_ID, backup_name=str(previous_path.relative_to(registry.history_dir)))
    restored = load_compatible_profile(restored_path)

    assert restored.display_name == "Version One"
    assert restored.status == ProfileStatus.VERIFIED
    assert restored.locked is False
    assert restored.provenance.verified_at is not None
    assert {item.display_name for item in registry.history(BUNDLE_NAME)} == {
        "Version One",
        "Version Two",
    }


def test_registry_listing_skips_corrupt_files_and_keeps_valid_profiles(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    valid_path = registry.save_draft(profile("Valid Draft"))
    (registry.root / "broken.json").write_text('{"status": "verified"', encoding="utf-8")
    (registry.draft_dir / "not-an-object.json").write_text("[]", encoding="utf-8")
    (registry.candidate_dir / "invalid-model.json").write_text(
        json.dumps({"schema_version": 2, "status": "candidate"}),
        encoding="utf-8",
    )

    listed = registry.list()

    assert [(item.target_app_id, item.display_name) for item in listed] == [(TARGET_APP_ID, "Valid Draft")]
    assert registry.get(target_app_id=TARGET_APP_ID, status=ProfileStatus.DRAFT) is not None
    assert valid_path.exists()


def test_get_any_finds_non_verified_profiles_and_rollback_rejects_traversal(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    registry.save_draft(profile("Draft"))

    assert registry.get_any(target_app_id=TARGET_APP_ID).status == ProfileStatus.DRAFT

    with pytest.raises(ProfileRegistryError, match="outside Profile history"):
        registry.rollback(TARGET_APP_ID, backup_name="../escape.json")


def test_get_any_prefers_verified_when_lifecycle_files_coexist(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    promote_profile(registry, profile("Verified"))
    draft = profile("New Draft").model_copy(update={"status": ProfileStatus.DRAFT})
    registry._atomic_write(registry.draft_dir / f"{TARGET_APP_ID}.json", draft)

    selected = registry.get_any(target_app_id=TARGET_APP_ID)

    assert selected is not None
    assert selected.status == ProfileStatus.VERIFIED
    assert selected.display_name == "Verified"


def test_update_verified_persists_quick_verification_metadata(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    promote_profile(registry, profile())
    current = registry.read(TARGET_APP_ID)
    evidence = dict(current.provenance.evidence)
    evidence["quick_verification"] = {"passed": True, "run_id": "run-1"}
    updated = current.model_copy(
        update={"provenance": current.provenance.model_copy(update={"evidence": evidence}, deep=True)},
        deep=True,
    )

    registry.update_verified(updated)

    assert registry.read(TARGET_APP_ID).provenance.evidence["quick_verification"]["passed"] is True


# ---------------------------------------------------------------------------
# 手动追加 Hypium 回放证据（Phase 4：比赛「3 次连续成功」要求）
# ---------------------------------------------------------------------------


def test_append_replay_evidence_accumulates_to_promote(tmp_path: Path) -> None:
    """candidate 累计满门禁次数且全部通过后自动晋级为 verified。"""
    registry = make_registry(tmp_path, attempts=3)
    registry.save_candidate(profile("Accumulating"))

    first = registry.append_replay_evidence(TARGET_APP_ID, passed=True)
    assert first.status == ProfileStatus.CANDIDATE
    assert first.provenance.hypium_replay_run_ids == ["run-profile:profile-attempt-1"]

    second = registry.append_replay_evidence(TARGET_APP_ID, passed=True)
    assert second.status == ProfileStatus.CANDIDATE
    assert second.provenance.hypium_replay_run_ids == [
        "run-profile:profile-attempt-1",
        "run-profile:profile-attempt-2",
    ]

    third = registry.append_replay_evidence(TARGET_APP_ID, passed=True)
    assert third.status == ProfileStatus.VERIFIED
    assert third.provenance.verified_at is not None
    assert third.provenance.hypium_replay_run_ids == [
        "run-profile:profile-attempt-1",
        "run-profile:profile-attempt-2",
        "run-profile:profile-attempt-3",
    ]
    assert third.provenance.evidence["consecutive_replay_passes"] == 3
    assert registry.read(TARGET_APP_ID).status == ProfileStatus.VERIFIED
    assert not (registry.candidate_dir / f"{TARGET_APP_ID}.json").exists()


def test_append_replay_evidence_promotes_immediately_with_single_attempt_gate(tmp_path: Path) -> None:
    """默认门禁（1 次）下，主流程 1 次内联回放即可晋级；随后追加只补充审计证据。"""
    registry = make_registry(tmp_path, attempts=1)
    registry.save_candidate(profile("Single Gate"))

    verified = registry.append_replay_evidence(TARGET_APP_ID, passed=True)
    assert verified.status == ProfileStatus.VERIFIED

    # 已 verified：只追加证据，状态与门禁资产不变（计划 §17.6 不变式）。
    extended = registry.append_replay_evidence(TARGET_APP_ID, passed=True)
    assert extended.status == ProfileStatus.VERIFIED
    assert len(extended.provenance.hypium_replay_run_ids) == 2

    completed = registry.append_replay_evidence(TARGET_APP_ID, passed=True)
    assert len(completed.provenance.hypium_replay_run_ids) == 3
    assert completed.provenance.evidence["consecutive_replay_passes"] == 3

    with pytest.raises(ProfileTransitionError, match="already complete"):
        registry.append_replay_evidence(TARGET_APP_ID, passed=True)


def test_append_replay_evidence_rejects_non_candidate_states(tmp_path: Path) -> None:
    """draft 阶段不得追加回放证据：门禁语义要求先通过设备验证。"""
    registry = make_registry(tmp_path)
    registry.save_draft(profile("Draft Only"))

    with pytest.raises(ProfileTransitionError, match="candidate/verified"):
        registry.append_replay_evidence(TARGET_APP_ID, passed=True)


def test_append_replay_evidence_rejects_locked_verified_profile(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    promote_profile(registry, profile("Locked"))
    registry.lock(TARGET_APP_ID)

    with pytest.raises(ProfileLockedError, match="locked"):
        registry.append_replay_evidence(TARGET_APP_ID, passed=True)


def test_append_replay_evidence_does_not_promote_after_failure(tmp_path: Path) -> None:
    """一次回放失败即中止累计：失败证据不得推高 consecutive 计数。"""
    registry = make_registry(tmp_path, attempts=2)
    registry.save_candidate(profile("Failing"))

    after_failure = registry.append_replay_evidence(TARGET_APP_ID, passed=False)

    assert after_failure.status == ProfileStatus.CANDIDATE
    assert after_failure.provenance.evidence["consecutive_replay_passes"] == 0
