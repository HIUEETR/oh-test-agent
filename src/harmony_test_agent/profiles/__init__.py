"""Profile v2 registry and legacy compatibility helpers."""

from .compat import load_compatible_profile, migrate_legacy_profile
from .registry import (
    ProfileLockedError,
    ProfileNotFoundError,
    ProfileRegistry,
    ProfileRegistryError,
    ProfileTransitionError,
)

__all__ = [
    "ProfileLockedError",
    "ProfileNotFoundError",
    "ProfileRegistry",
    "ProfileRegistryError",
    "ProfileTransitionError",
    "load_compatible_profile",
    "migrate_legacy_profile",
]
