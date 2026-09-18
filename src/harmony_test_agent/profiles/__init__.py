"""Profile v2 registry and legacy compatibility helpers."""

from .compat import load_compatible_profile, migrate_legacy_profile
from .registry import (
    MAX_REPLAY_EVIDENCE,
    ProfileLockedError,
    ProfileNotFoundError,
    ProfileRegistry,
    ProfileRegistryError,
    ProfileTransitionError,
)

__all__ = [
    "MAX_REPLAY_EVIDENCE",
    "ProfileLockedError",
    "ProfileNotFoundError",
    "ProfileRegistry",
    "ProfileRegistryError",
    "ProfileTransitionError",
    "load_compatible_profile",
    "migrate_legacy_profile",
]
