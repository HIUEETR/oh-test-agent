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


def profile(display_name: str = "Pocket Notes") -> TargetAppProfile:
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
                observed_rounds=3,
                unique_match_rounds=3,
                evidence_snapshot_ids=[f"snapshot-{index}-round-{round_number}" for round_number in range(1, 4)],
            )
            for index in range(1, 4)
        ],
        assertion_inventory=[
            AssertionDefinition(
                name=f"assertion-{index}",
                kind="visible",
                target=f"key-{index}",
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


def promote_profile(
    registry: ProfileRegistry,
    value: TargetAppProfile,
    replay_ids: tuple[str, str, str] = (
        "run-profile:profile-attempt-1",
        "run-profile:profile-attempt-2",
        "run-profile:profile-attempt-3",
    ),
) -> Path:
    registry.save_candidate(value)
    return registry.promote(value.target_app_id, replay_run_ids=replay_ids)


def test_promotion_rejects_candidates_without_admission_assets(tmp_path: Path) -> None:
    registry = ProfileRegistry(tmp_path / "profiles")
    empty = TargetAppProfile(
        target_app_id=TARGET_APP_ID,
        display_name="Empty",
        bundle_name=BUNDLE_NAME,
        main_ability="MainAbility",
    )
    with pytest.raises(ProfileTransitionError, match="three stable locators"):
        registry.save_candidate(empty)


def test_registry_persists_draft_candidate_and_verified_lifecycle(tmp_path: Path) -> None:
    registry = ProfileRegistry(tmp_path / "profiles")
    value = profile()

    draft_path = registry.save_draft(value)
    assert draft_path == registry.draft_dir / f"{TARGET_APP_ID}.json"
    assert load_compatible_profile(draft_path).status == ProfileStatus.DRAFT

    candidate_path = registry.save_candidate(value)
    assert candidate_path == registry.candidate_dir / f"{TARGET_APP_ID}.json"
    assert registry.read(TARGET_APP_ID, ProfileStatus.CANDIDATE).status == ProfileStatus.CANDIDATE

    with pytest.raises(ProfileTransitionError, match="three independent"):
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


def test_atomic_write_preserves_previous_profile_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProfileRegistry(tmp_path / "profiles")
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
    registry = ProfileRegistry(tmp_path / "profiles")
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
    registry = ProfileRegistry(tmp_path / "profiles")
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
    registry = ProfileRegistry(tmp_path / "profiles")
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
    registry = ProfileRegistry(tmp_path / "profiles")
    registry.save_draft(profile("Draft"))

    assert registry.get_any(target_app_id=TARGET_APP_ID).status == ProfileStatus.DRAFT

    with pytest.raises(ProfileRegistryError, match="outside Profile history"):
        registry.rollback(TARGET_APP_ID, backup_name="../escape.json")


def test_get_any_prefers_verified_when_lifecycle_files_coexist(tmp_path: Path) -> None:
    registry = ProfileRegistry(tmp_path / "profiles")
    promote_profile(registry, profile("Verified"))
    draft = profile("New Draft").model_copy(update={"status": ProfileStatus.DRAFT})
    registry._atomic_write(registry.draft_dir / f"{TARGET_APP_ID}.json", draft)

    selected = registry.get_any(target_app_id=TARGET_APP_ID)

    assert selected is not None
    assert selected.status == ProfileStatus.VERIFIED
    assert selected.display_name == "Verified"


def test_update_verified_persists_quick_verification_metadata(tmp_path: Path) -> None:
    registry = ProfileRegistry(tmp_path / "profiles")
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
