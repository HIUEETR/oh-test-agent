"""Atomic, versioned persistence for draft, candidate, verified, and historical Profiles."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from ..models import ProfileStatus, TargetAppProfile, utc_now
from .compat import load_compatible_profile


class ProfileRegistryError(RuntimeError):
    """Base error for deterministic Profile registry operations."""


class ProfileNotFoundError(ProfileRegistryError):
    """Raised when no requested Profile exists."""


class ProfileLockedError(ProfileRegistryError):
    """Raised when an automatic write would replace a locked Profile."""


class ProfileTransitionError(ProfileRegistryError):
    """Raised for an invalid lifecycle transition or insufficient promotion evidence."""


class ProfileRegistry:
    """Stores lifecycle versions with schema validation, backups, and atomic replacement."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.draft_dir = self.root / "draft"
        self.candidate_dir = self.root / "candidate"
        self.history_dir = self.root / "history"
        self._lock = threading.RLock()
        for directory in (self.root, self.draft_dir, self.candidate_dir, self.history_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def list(self, status: ProfileStatus | str | None = None) -> list[TargetAppProfile]:
        """List validated Profiles, optionally restricted to one lifecycle location."""
        normalized = ProfileStatus(status) if status is not None else None
        paths: list[Path] = []
        if normalized in (None, ProfileStatus.VERIFIED):
            paths.extend(path for path in self.root.glob("*.json") if path.is_file())
        if normalized in (None, ProfileStatus.DRAFT, ProfileStatus.INVALID):
            paths.extend(self.draft_dir.glob("*.json"))
        if normalized in (None, ProfileStatus.CANDIDATE):
            paths.extend(self.candidate_dir.glob("*.json"))
        if normalized == ProfileStatus.SUPERSEDED:
            paths.extend(self.history_dir.glob("*/*.json"))
        profiles: list[TargetAppProfile] = []
        for path in sorted(paths):
            try:
                profiles.append(load_compatible_profile(path))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return [profile for profile in profiles if normalized is None or profile.status == normalized]

    def list_profiles(self, status: ProfileStatus | str | None = None) -> list[TargetAppProfile]:
        """Compatibility alias for callers preferring an explicit method name."""
        return self.list(status)

    def get(
        self,
        *,
        target_app_id: str | None = None,
        bundle_name: str | None = None,
        status: ProfileStatus | str | None = ProfileStatus.VERIFIED,
    ) -> TargetAppProfile | None:
        """Return one exact Profile without guessing between duplicate matches."""
        if not target_app_id and not bundle_name:
            raise ValueError("target_app_id or bundle_name is required")
        matches = [
            profile
            for profile in self.list(status)
            if (not target_app_id or profile.target_app_id == target_app_id)
            and (not bundle_name or profile.bundle_name == bundle_name)
        ]
        if len(matches) > 1:
            raise ProfileRegistryError("multiple Profiles matched the exact query")
        return matches[0] if matches else None

    def get_any(
        self,
        *,
        target_app_id: str | None = None,
        bundle_name: str | None = None,
        preferred_status: ProfileStatus | str | None = None,
    ) -> TargetAppProfile | None:
        """Return one active Profile using a deterministic lifecycle priority."""
        statuses = (
            [ProfileStatus(preferred_status)]
            if preferred_status is not None
            else [
                ProfileStatus.VERIFIED,
                ProfileStatus.CANDIDATE,
                ProfileStatus.DRAFT,
                ProfileStatus.INVALID,
            ]
        )
        for status in statuses:
            profile = self.get(
                target_app_id=target_app_id,
                bundle_name=bundle_name,
                status=status,
            )
            if profile is not None:
                return profile
        return None

    def require(self, **query: str) -> TargetAppProfile:
        """Get a Profile or raise a typed not-found error."""
        profile = self.get(**query)
        if profile is None:
            raise ProfileNotFoundError(f"Profile not found: {query}")
        return profile

    def read(self, target_app_id: str, status: ProfileStatus | str = ProfileStatus.VERIFIED) -> TargetAppProfile:
        """Read a Profile by its stable application ID."""
        return self.require(target_app_id=target_app_id, status=status)

    def save_draft(self, profile: TargetAppProfile) -> Path:
        """Persist an exploration draft or invalid draft atomically."""
        if profile.status not in {ProfileStatus.DRAFT, ProfileStatus.INVALID}:
            profile = profile.model_copy(update={"status": ProfileStatus.DRAFT}, deep=True)
        return self._save_lifecycle(profile, self.draft_dir)

    def save_candidate(self, profile: TargetAppProfile) -> Path:
        """Persist a device-validated Profile awaiting Hypium replay gates."""
        if profile.status not in {ProfileStatus.DRAFT, ProfileStatus.CANDIDATE}:
            raise ProfileTransitionError(f"cannot transition {profile.status} to candidate")
        self._validate_admission_assets(profile, "candidate")

        candidate = profile.model_copy(update={"status": ProfileStatus.CANDIDATE}, deep=True)
        with self._lock:
            path = self._path_for(self.candidate_dir, candidate.target_app_id)
            self._atomic_write(path, candidate)
            self._path_for(self.draft_dir, candidate.target_app_id).unlink(missing_ok=True)
            return path

    def promote(
        self,
        profile_or_id: TargetAppProfile | str,
        *,
        replay_run_ids: Iterable[str] | None = None,
        verified_at: datetime | None = None,
    ) -> Path:
        """Promote only the persisted candidate after three independent replay IDs."""
        with self._lock:
            target_app_id = (
                profile_or_id.target_app_id
                if isinstance(profile_or_id, TargetAppProfile)
                else profile_or_id
            )
            candidate = self.require(
                target_app_id=target_app_id,
                status=ProfileStatus.CANDIDATE,
            )
            if (
                isinstance(profile_or_id, TargetAppProfile)
                and candidate.model_dump(mode="json") != profile_or_id.model_dump(mode="json")
            ):
                raise ProfileTransitionError("in-memory candidate does not match persisted candidate")
            if candidate.status != ProfileStatus.CANDIDATE:
                raise ProfileTransitionError(f"only candidate Profiles can be promoted, got {candidate.status}")
            self._validate_admission_assets(candidate, "promotion")

            replay_ids = list(replay_run_ids or candidate.provenance.hypium_replay_run_ids)
            expected = {
                f"{candidate.provenance.discovery_run_id}:profile-attempt-{attempt}"
                for attempt in range(1, 4)
            }
            if (
                len(replay_ids) != 3
                or set(replay_ids) != expected
                or not candidate.provenance.discovery_run_id
            ):
                raise ProfileTransitionError(
                    "promotion requires three independent Hypium replay run IDs for the discovery Run"
                )

            current_path = self._path_for(self.root, candidate.target_app_id)
            current = None
            if current_path.exists():
                current = load_compatible_profile(current_path)
                if current.locked:
                    raise ProfileLockedError(f"verified Profile is locked: {current.target_app_id}")

            provenance = candidate.provenance.model_copy(
                update={"verified_at": verified_at or utc_now(), "hypium_replay_run_ids": replay_ids}, deep=True
            )
            verified = candidate.model_copy(
                update={"status": ProfileStatus.VERIFIED, "provenance": provenance}, deep=True
            )
            backup = self._backup(current_path, current) if current is not None else None
            try:
                self._atomic_write(current_path, verified)
            except Exception:
                if backup is not None:
                    backup.unlink(missing_ok=True)
                raise
            candidate_path = self._path_for(self.candidate_dir, candidate.target_app_id)
            candidate_path.unlink(missing_ok=True)
            self._path_for(self.draft_dir, candidate.target_app_id).unlink(missing_ok=True)
            return current_path

    def update_verified(self, profile: TargetAppProfile) -> Path:
        """Persist verification metadata without changing immutable Profile content."""
        if profile.status != ProfileStatus.VERIFIED:
            raise ProfileTransitionError("only verified Profiles can be updated in place")
        with self._lock:
            path = self._path_for(self.root, profile.target_app_id)
            if not path.exists():
                raise ProfileNotFoundError(f"verified Profile not found: {profile.target_app_id}")
            current = load_compatible_profile(path)
            if self._immutable_profile_payload(current) != self._immutable_profile_payload(profile):
                raise ProfileTransitionError(
                    "verified Profile metadata update cannot change identity or admission assets"
                )
            updated = current.model_copy(update={"provenance": profile.provenance}, deep=True)
            self._atomic_write(path, updated, allow_locked=True)
            return path

    def lock(self, target_app_id: str, *, locked: bool = True) -> TargetAppProfile:
        """Atomically change the manual-overwrite lock on the current verified Profile."""
        with self._lock:
            path = self._path_for(self.root, target_app_id)
            if not path.exists():
                raise ProfileNotFoundError(f"verified Profile not found: {target_app_id}")
            profile = load_compatible_profile(path).model_copy(update={"locked": locked}, deep=True)
            self._atomic_write(path, profile, allow_locked=True)
            return profile

    def set_locked(self, target_app_id: str, locked: bool = True) -> TargetAppProfile:
        """Compatibility alias for lock management APIs."""
        return self.lock(target_app_id, locked=locked)

    def history(self, bundle_name: str) -> list[TargetAppProfile]:
        """Read valid superseded versions for a bundle in timestamp order."""
        directory = self.history_dir / self._safe_component(bundle_name)
        profiles: list[TargetAppProfile] = []
        for path in sorted(directory.glob("*.json")):
            try:
                profiles.append(load_compatible_profile(path))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return profiles

    def invalidate(self, target_app_id: str, reason: str) -> Path:
        """Move a failing verified Profile to an auditable invalid draft without deleting history."""
        with self._lock:
            current_path = self._path_for(self.root, target_app_id)
            profile = load_compatible_profile(current_path)
            if profile.locked:
                return current_path
            evidence = dict(profile.provenance.evidence)
            evidence["invalidation_reason"] = reason
            invalid = profile.model_copy(
                update={
                    "status": ProfileStatus.INVALID,
                    "provenance": profile.provenance.model_copy(update={"evidence": evidence}, deep=True),
                },
                deep=True,
            )
            path = self._path_for(self.draft_dir, target_app_id)
            backup = self._backup(current_path, profile)
            try:
                self._atomic_write(path, invalid)
            except Exception:
                backup.unlink(missing_ok=True)
                raise
            try:
                current_path.unlink(missing_ok=True)
            except Exception:
                path.unlink(missing_ok=True)
                backup.unlink(missing_ok=True)
                raise
            return path

    def rollback(
        self,
        target_app_id: str,
        backup_path: Path | None = None,
        *,
        backup_name: str | None = None,
    ) -> Path:
        """Atomically restore a validated historical Profile while preserving the current version."""
        with self._lock:
            current_path = self._path_for(self.root, target_app_id)
            current = load_compatible_profile(current_path) if current_path.exists() else None
            if backup_path is None and backup_name:
                candidate_name = Path(backup_name)
                if (
                    candidate_name.is_absolute()
                    or ".." in candidate_name.parts
                    or len(candidate_name.parts) != 2
                ):
                    raise ProfileRegistryError("backup name is outside Profile history")
                if current:
                    expected_directories = {self._safe_component(current.bundle_name)}
                else:
                    expected_directories = {
                        path.parent.name
                        for path in self.history_dir.glob("*/*.json")
                        if self._history_matches(path, target_app_id)
                    }
                if candidate_name.parts[0] not in expected_directories:
                    raise ProfileRegistryError("backup name does not match Profile history")
                backup_path = self.history_dir / candidate_name
            if current and current.locked:
                raise ProfileLockedError(f"verified Profile is locked: {target_app_id}")
            candidates = list(self.history_dir.glob("*/*.json"))
            valid_candidates: list[Path] = []
            for item in candidates:
                try:
                    if load_compatible_profile(item).target_app_id == target_app_id:
                        valid_candidates.append(item)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
            candidates = valid_candidates
            if backup_path is not None:
                resolved = Path(backup_path).resolve()
                history_root = self.history_dir.resolve()
                if not resolved.is_relative_to(history_root):
                    raise ProfileRegistryError("backup path is outside Profile history")
                try:
                    candidates = (
                        [resolved]
                        if resolved.exists()
                        and load_compatible_profile(resolved).target_app_id == target_app_id
                        else []
                    )
                except (OSError, ValueError, json.JSONDecodeError):
                    candidates = []
            if not candidates:
                raise ProfileNotFoundError(f"Profile history not found: {target_app_id}")
            historical = load_compatible_profile(sorted(candidates)[-1])
            if historical.target_app_id != target_app_id:
                raise ProfileRegistryError("historical Profile identity does not match target")
            provenance = historical.provenance.model_copy(update={"verified_at": utc_now()}, deep=True)
            restored = historical.model_copy(
                update={"status": ProfileStatus.VERIFIED, "locked": False, "provenance": provenance}, deep=True
            )
            backup = self._backup(current_path, current) if current else None
            try:
                self._atomic_write(current_path, restored)
            except Exception:
                if backup is not None:
                    backup.unlink(missing_ok=True)
                raise
            return current_path

    def _save_lifecycle(self, profile: TargetAppProfile, directory: Path) -> Path:
        with self._lock:
            path = self._path_for(directory, profile.target_app_id)
            self._atomic_write(path, profile)
            return path

    def _atomic_write(self, path: Path, profile: TargetAppProfile, *, allow_locked: bool = False) -> None:
        # Round-trip through the schema before touching the destination.
        validated = TargetAppProfile.model_validate(profile.model_dump(mode="python"))
        if path.exists() and not allow_locked and load_compatible_profile(path).locked:
            raise ProfileLockedError(f"Profile is locked: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        payload = json.dumps(validated.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            # Validate bytes exactly as they will be installed.
            TargetAppProfile.model_validate_json(temporary.read_text(encoding="utf-8"))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _backup(self, path: Path, profile: TargetAppProfile) -> Path:
        superseded = profile.model_copy(update={"status": ProfileStatus.SUPERSEDED}, deep=True)
        directory = self.history_dir / self._safe_component(profile.bundle_name)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        backup = directory / f"{stamp}.json"
        self._atomic_write(backup, superseded, allow_locked=True)
        return backup

    def _path_for(self, directory: Path, target_app_id: str) -> Path:
        path = directory / f"{self._safe_component(target_app_id)}.json"
        if path.exists():
            try:
                stored = load_compatible_profile(path)
            except (OSError, ValueError, json.JSONDecodeError):
                stored = None
            if stored is not None and stored.target_app_id != target_app_id:
                raise ProfileRegistryError(
                    f"Profile identifier path collision: {target_app_id} and {stored.target_app_id}"
                )
        return path

    @staticmethod
    def _validate_admission_assets(profile: TargetAppProfile, transition: str) -> None:
        locators = profile.stable_locator_inventory
        if len(locators) < 3:
            raise ProfileTransitionError(f"{transition} requires three stable locators")
        if len({item.page_signature for item in locators}) < 3:
            raise ProfileTransitionError(f"{transition} requires stable locators on three pages")
        admitted_locators = [
            item
            for item in locators
            if item.observed_rounds >= 3
            and item.unique_match_rounds >= 3
            and item.evidence_snapshot_ids
        ]
        if len(admitted_locators) < 3:
            raise ProfileTransitionError(
                f"{transition} requires three-round unique locator evidence"
            )
        assertions = profile.assertion_inventory
        if len(assertions) < 2:
            raise ProfileTransitionError(f"{transition} requires two application-level assertions")
        if any(item.observed_rounds < 3 or not item.evidence_snapshot_ids for item in assertions):
            raise ProfileTransitionError(
                f"{transition} requires three-round assertion evidence"
            )
        if not profile.core_flows or len(profile.core_flows[0].get("pages", [])) < 3:
            raise ProfileTransitionError(
                f"{transition} requires a replayable three-page core flow"
            )
        if len(set(profile.core_flows[0].get("interaction_types", []))) < 3:
            raise ProfileTransitionError(f"{transition} requires three interaction types")
        evidence = profile.provenance.evidence
        if not evidence.get("verification_passed"):
            raise ProfileTransitionError(
                f"{transition} requires passed device verification evidence"
            )
        if evidence.get("cross_bundle_recovery_failed", False):
            raise ProfileTransitionError(
                f"{transition} cannot contain unrecovered cross-bundle violations"
            )

    @staticmethod
    def _immutable_profile_payload(profile: TargetAppProfile) -> dict[str, object]:
        payload = profile.model_dump(mode="json", exclude={"provenance"})
        payload.pop("locked", None)
        return payload

    @staticmethod
    def _history_matches(path: Path, target_app_id: str) -> bool:
        try:
            return load_compatible_profile(path).target_app_id == target_app_id
        except (OSError, ValueError, json.JSONDecodeError):
            return False

    @staticmethod
    def _safe_component(value: str) -> str:
        value = value.strip()
        if not value or value in {".", ".."}:
            raise ValueError("Profile identifier cannot be empty")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
        if not safe:
            raise ValueError(f"invalid Profile identifier: {value!r}")
        return safe
