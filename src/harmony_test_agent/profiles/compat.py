"""Converts migration-period Profile JSON into the strict Profile v2 domain model."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..models import ProfileProvenance, ProfileStatus, TargetAppProfile


def migrate_legacy_profile(data: Mapping[str, Any]) -> TargetAppProfile:
    """Validate v2 input or add explicit defaults to a legacy Profile document."""
    payload = deepcopy(dict(data))
    if payload.get("schema_version") == 2:
        return TargetAppProfile.model_validate(payload)

    payload["schema_version"] = 2
    # A legacy file was manually supplied and already used in regression runs, but it
    # has no three-round/Hypium evidence. Keep it usable without claiming verification.
    payload.setdefault("status", ProfileStatus.DRAFT)
    payload.setdefault("locked", False)
    payload.setdefault("module_name", None)
    payload.setdefault("app_version", {})
    payload.setdefault("device_compatibility", {})
    payload.setdefault("assertion_inventory", [])
    payload.setdefault("core_flows", [])
    provenance = dict(payload.get("provenance") or {})
    provenance.setdefault("generator_version", "legacy-profile-compat-v1")
    provenance.setdefault("evidence", {"legacy_schema": 1})
    payload["provenance"] = ProfileProvenance.model_validate(provenance)
    return TargetAppProfile.model_validate(payload)


def load_compatible_profile(path: Path) -> TargetAppProfile:
    """Read a Profile with BOM support and migrate legacy schema in memory."""
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError(f"Profile root must be an object: {path}")
    return migrate_legacy_profile(raw)
