"""Public target discovery models and deterministic resolver."""

from ..models import ResolvedTarget, TargetQuery
from .catalog import CatalogParseError, ForegroundApp, InstalledApp
from .resolver import (
    TargetAmbiguousError,
    TargetNotFoundError,
    TargetResolutionError,
    TargetResolver,
)

__all__ = [
    "CatalogParseError",
    "ForegroundApp",
    "InstalledApp",
    "ResolvedTarget",
    "TargetAmbiguousError",
    "TargetNotFoundError",
    "TargetQuery",
    "TargetResolutionError",
    "TargetResolver",
]
