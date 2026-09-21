"""Atomic, versioned persistence for draft, candidate, verified, and historical Profiles."""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ..models import ProfileStatus, TargetAppProfile, utc_now
from .admission import (
    REGISTRY_GATES,
    AdmissionEvidence,
    AdmissionThresholds,
    describe_admission_failure,
    evaluate_admission,
    pick_first_failure,
)
from .compat import load_compatible_profile

# 比赛「3 次连续成功」要求的总回放证据上限（主流程 1 次 + 异步追加 2 次）。
MAX_REPLAY_EVIDENCE = 3


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

    def __init__(
        self,
        root: Path,
        min_interaction_kinds: int = 2,
        *,
        promotion_replay_attempts: int | None = None,
        min_evidence_rounds: int | None = None,
    ):
        self.root = Path(root)
        self.draft_dir = self.root / "draft"
        self.candidate_dir = self.root / "candidate"
        self.history_dir = self.root / "history"
        self.min_interaction_kinds = max(min_interaction_kinds, 1)
        # 2026-09-17 重构：门禁轮次/次数由 Settings 注入，缺省时读取进程配置。
        # 不显式传参时（例如既有测试）同样跟随 Settings 默认值（1 轮 / 1 次）。
        if promotion_replay_attempts is None or min_evidence_rounds is None:
            from ..config import get_settings

            settings = get_settings()
            if promotion_replay_attempts is None:
                promotion_replay_attempts = settings.hypium_replay_attempts
            if min_evidence_rounds is None:
                min_evidence_rounds = settings.profile_verification_rounds
        self.promotion_replay_attempts = max(int(promotion_replay_attempts), 1)
        self.min_evidence_rounds = max(int(min_evidence_rounds), 1)
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
            except OSError, ValueError, json.JSONDecodeError:
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
        """Promote only the persisted candidate after the configured number of independent replay IDs."""
        with self._lock:
            target_app_id = (
                profile_or_id.target_app_id if isinstance(profile_or_id, TargetAppProfile) else profile_or_id
            )
            candidate = self.require(
                target_app_id=target_app_id,
                status=ProfileStatus.CANDIDATE,
            )
            if isinstance(profile_or_id, TargetAppProfile) and candidate.model_dump(
                mode="json"
            ) != profile_or_id.model_dump(mode="json"):
                raise ProfileTransitionError("in-memory candidate does not match persisted candidate")
            if candidate.status != ProfileStatus.CANDIDATE:
                raise ProfileTransitionError(f"only candidate Profiles can be promoted, got {candidate.status}")
            self._validate_admission_assets(candidate, "promotion")

            replay_ids = list(replay_run_ids or candidate.provenance.hypium_replay_run_ids)
            required = self.promotion_replay_attempts
            discovery_run_id = candidate.provenance.discovery_run_id
            allowed = {f"{discovery_run_id}:profile-attempt-{attempt}" for attempt in range(1, MAX_REPLAY_EVIDENCE + 1)}
            if (
                not discovery_run_id
                or len(replay_ids) < required
                or len(set(replay_ids)) != len(replay_ids)
                or not set(replay_ids) <= allowed
            ):
                raise ProfileTransitionError(
                    f"promotion requires {required} independent Hypium replay run ID(s) for the discovery Run"
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

    def append_replay_evidence(
        self,
        target_app_id: str,
        *,
        passed: bool,
        run_id: str | None = None,
        evidence_refs: Iterable[str] | None = None,
        checked_at: datetime | None = None,
    ) -> TargetAppProfile:
        """向 Profile 追加一次 Hypium 回放证据（比赛「3 次连续成功」要求）。

        主流程只内联 ``hypium_replay_attempts`` 次回放（默认 1 次）即完成晋级门禁，
        剩余次数由用户/CI 通过 ``POST /api/profiles/{id}/replay`` 异步追加。

        不变式（计划 §17.6）：
        - 只接受 **candidate / verified** 的 Profile；draft/invalid/superseded 一律拒绝。
        - 锁定的 verified Profile 拒绝追加（``ProfileLockedError``）。
        - 已处于 verified 的 Profile 仅追加审计证据，**不改变** 状态、不修改门禁资产。
        - candidate 累计满 ``promotion_replay_attempts`` 次且全部通过后自动晋级。
        - 同一 ``run_id`` 不能重复追加；证据总数上限 ``MAX_REPLAY_EVIDENCE``。
        """
        with self._lock:
            profile = self.get_any(target_app_id=target_app_id)
            if profile is None:
                raise ProfileNotFoundError(f"Profile not found: {target_app_id}")
            if profile.status not in {ProfileStatus.CANDIDATE, ProfileStatus.VERIFIED}:
                raise ProfileTransitionError(
                    f"replay evidence can only be appended to candidate/verified Profiles, got {profile.status}"
                )
            if profile.status == ProfileStatus.VERIFIED:
                verified_path = self._path_for(self.root, profile.target_app_id)
                if verified_path.exists() and load_compatible_profile(verified_path).locked:
                    raise ProfileLockedError(f"verified Profile is locked: {profile.target_app_id}")

            discovery_run_id = profile.provenance.discovery_run_id or profile.target_app_id
            existing = list(profile.provenance.hypium_replay_run_ids)
            if len(existing) >= MAX_REPLAY_EVIDENCE:
                raise ProfileTransitionError(
                    f"replay evidence is already complete ({MAX_REPLAY_EVIDENCE} attempts recorded)"
                )
            attempt = len(existing) + 1
            evidence_id = run_id or f"{discovery_run_id}:profile-attempt-{attempt}"
            if evidence_id in existing:
                raise ProfileTransitionError(f"replay evidence already recorded: {evidence_id}")

            evidence = dict(profile.provenance.evidence)
            replays = list(evidence.get("hypium_replays") or [])
            # 回填主流程内联回放证据：晋级时只写入 hypium_replay_run_ids，审计列表需要
            # 与之保持一致，否则 consecutive_replay_passes 会漏算主流程那一次。
            recorded = {item.get("run_id") for item in replays}
            for index, existing_id in enumerate(existing, 1):
                if existing_id in recorded:
                    continue
                replays.append(
                    {
                        "attempt": index,
                        "run_id": existing_id,
                        "passed": True,
                        "checked_at": (checked_at or utc_now()).isoformat(),
                        "evidence": [],
                        "backfilled": True,
                    }
                )
            replays.append(
                {
                    "attempt": attempt,
                    "run_id": evidence_id,
                    "passed": bool(passed),
                    "checked_at": (checked_at or utc_now()).isoformat(),
                    "evidence": sorted(evidence_refs or []),
                }
            )
            evidence["hypium_replays"] = replays
            if all(item["passed"] for item in replays):
                evidence["consecutive_replay_passes"] = len(replays)
            else:
                evidence["consecutive_replay_passes"] = 0
            provenance = profile.provenance.model_copy(
                update={"hypium_replay_run_ids": [*existing, evidence_id], "evidence": evidence}, deep=True
            )
            updated = profile.model_copy(update={"provenance": provenance}, deep=True)

            if profile.status == ProfileStatus.VERIFIED:
                # 已 verified：只落盘追加的审计证据，不动状态与门禁资产。
                self.update_verified(updated)
                return updated

            candidate = updated.model_copy(update={"status": ProfileStatus.CANDIDATE}, deep=True)
            self._save_lifecycle(candidate, self.candidate_dir)
            if len(provenance.hypium_replay_run_ids) >= self.promotion_replay_attempts and all(
                item["passed"] for item in replays
            ):
                self.promote(candidate.target_app_id)
                return self.require(target_app_id=candidate.target_app_id, status=ProfileStatus.VERIFIED)
            return candidate

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
            except OSError, ValueError, json.JSONDecodeError:
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
                if candidate_name.is_absolute() or ".." in candidate_name.parts or len(candidate_name.parts) != 2:
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
                except OSError, ValueError, json.JSONDecodeError:
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
                        if resolved.exists() and load_compatible_profile(resolved).target_app_id == target_app_id
                        else []
                    )
                except OSError, ValueError, json.JSONDecodeError:
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
            except OSError, ValueError, json.JSONDecodeError:
                stored = None
            if stored is not None and stored.target_app_id != target_app_id:
                raise ProfileRegistryError(
                    f"Profile identifier path collision: {target_app_id} and {stored.target_app_id}"
                )
        return path

    def _validate_admission_assets(self, profile: TargetAppProfile, transition: str) -> None:
        """准入门禁：唯一实现见 ``profiles/admission.py``（计划 4.1/R15）。"""
        thresholds = self._admission_thresholds()
        failures = evaluate_admission(
            AdmissionEvidence.from_profile(profile, thresholds, include=REGISTRY_GATES),
            thresholds=thresholds,
        )
        gate = pick_first_failure(failures, style="registry")
        if gate is not None:
            raise ProfileTransitionError(
                describe_admission_failure(gate, style="registry", thresholds=thresholds, prefix=f"{transition} ")
            )

    def _admission_thresholds(self) -> AdmissionThresholds:
        """阈值来源：Settings + 本实例的运行态覆盖（interaction kinds / 证据轮数）。"""
        from ..config import get_settings

        return AdmissionThresholds.from_settings(
            get_settings(),
            min_interaction_kinds=self.min_interaction_kinds,
            min_evidence_rounds=self.min_evidence_rounds,
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
        except OSError, ValueError, json.JSONDecodeError:
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
