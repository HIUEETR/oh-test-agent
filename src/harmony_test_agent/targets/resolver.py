"""Resolve user supplied application names or bundle names without guessing."""

from __future__ import annotations

import re
from typing import Literal

from ..devices.base import DeviceAdapter, DeviceError
from ..models import ResolvedTarget, TargetQuery
from .catalog import InstalledApp


class TargetResolutionError(RuntimeError):
    """Base class for deterministic target resolution failures."""


class TargetNotFoundError(TargetResolutionError):
    """No installed application matched the request."""


class TargetAmbiguousError(TargetResolutionError):
    """Several applications matched and user selection is required."""

    def __init__(self, candidates: list[InstalledApp]):
        super().__init__("multiple installed applications match the target")
        self.candidates = candidates


class TargetResolver:
    """Apply exact-first matching and expose ambiguous candidates to callers."""

    def __init__(self, device: DeviceAdapter):
        self.device = device

    def resolve(self, query: TargetQuery) -> ResolvedTarget:
        if query.bundle_name:
            try:
                matches = [self.device.inspect_app(query.bundle_name)]
            except DeviceError:
                installed = self.device.list_installed_apps()
                if any(app.bundle_name == query.bundle_name for app in installed):
                    raise
                raise TargetNotFoundError(f"installed bundle not found: {query.bundle_name}") from None
            if len(matches) != 1:
                raise TargetAmbiguousError(matches)
            selected = matches[0]
            if query.app_name and _name(query.app_name) != _name(selected.display_name):
                raise TargetNotFoundError(
                    f"bundle {query.bundle_name} has label {selected.display_name!r}, not {query.app_name!r}"
                )
            source: Literal["installed_app", "explicit_override"] = "explicit_override"
        else:
            assert query.app_name is not None
            apps = self.device.find_installed_apps(query.app_name)
            apps = [app for app in apps if not _blocked_implicit_system_target(app)]
            selected = self._resolve_name(apps, query.app_name)
            source = "installed_app"

        if not selected.main_ability:
            selected = self.device.inspect_app(selected.bundle_name)
        if not selected.main_ability:
            raise TargetResolutionError(f"application has no unambiguous launch Ability: {selected.bundle_name}")
        return ResolvedTarget(
            target_app_id=_app_id(selected.bundle_name),
            display_name=selected.display_name,
            bundle_name=selected.bundle_name,
            main_ability=selected.main_ability,
            module_name=selected.module_name,
            version_name=selected.version_name,
            version_code=selected.version_code,
            signature_sha256=selected.signature_sha256,
            device_id=getattr(self.device, "device_id", "unknown"),
            source=source,
        )

    @staticmethod
    def _resolve_name(apps: list[InstalledApp], label: str) -> InstalledApp:
        needle = _name(label)
        exact = [app for app in apps if _name(app.display_name) == needle]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise TargetAmbiguousError(exact)
        candidates = [app for app in apps if needle in _name(app.display_name) or _name(app.display_name) in needle]
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            raise TargetAmbiguousError(candidates)
        raise TargetNotFoundError(f"installed application not found: {label}")


def _name(value: str) -> str:
    return " ".join(value.split()).casefold()


def _app_id(bundle_name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", bundle_name.casefold()).strip("-")
    return value or "target-app"


_BLOCKED_SYSTEM_BUNDLE_MARKERS = (
    "launcher",
    "settings",
    "inputmethod",
    "systemui",
    "scenemanager",
    "uitest",
)


def _blocked_implicit_system_target(app: InstalledApp) -> bool:
    bundle = app.bundle_name.casefold()
    return app.system_app or any(marker in bundle for marker in _BLOCKED_SYSTEM_BUNDLE_MARKERS)
