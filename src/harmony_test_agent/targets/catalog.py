"""Installed application metadata and deterministic ``bm dump`` parsers."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

PARSER_VERSION = "bm-dump-v1"


class CatalogParseError(ValueError):
    """Raised when application identity cannot be established without guessing."""


class InstalledApp(BaseModel):
    """Deterministic identity and launch metadata reported by Bundle Manager."""

    model_config = ConfigDict(frozen=True)

    bundle_name: str
    display_name: str
    abilities: tuple[str, ...] = ()
    main_ability: str | None = None
    module_name: str | None = None
    version_name: str | None = None
    version_code: int | None = None
    signature_sha256: str | None = None
    system_app: bool = False
    raw_output: str = ""
    parser_version: str = PARSER_VERSION


class ForegroundApp(BaseModel):
    """Application identity extracted from the current UI hierarchy."""

    model_config = ConfigDict(frozen=True)

    bundle_name: str
    ability_name: str | None = None
    window_type: str | None = None


def parse_bundle_list(raw: str) -> list[str]:
    """Parse ``bm dump -a`` output without treating diagnostic text as a bundle."""
    if not raw.strip():
        return []
    payload = _json_payload(raw)
    if payload is not None:
        bundles = _collect_values(payload, {"bundlename", "bundle_name", "bundle"})
    else:
        bundles = []
        for line in raw.splitlines():
            value = line.strip().strip("[],'\"")
            match = re.search(r"(?:bundleName|bundle_name)\s*[:=]\s*['\"]?([\w.]+)", value, re.I)
            if match:
                bundles.append(match.group(1))
            elif re.fullmatch(r"[A-Za-z][\w]*(?:\.[\w]+)+", value):
                bundles.append(value)
    return sorted(set(item for item in bundles if _looks_like_bundle(item)))


def parse_installed_app(raw: str, expected_bundle: str | None = None) -> InstalledApp:
    """Parse one ``bm dump -n`` response and fail when launch identity is ambiguous."""
    payload = _json_payload(raw)
    values = _flatten(payload) if payload is not None else _parse_lines(raw)

    bundles = _unique(values, "bundlename", "bundle_name", "bundle")
    if expected_bundle:
        bundles = [value for value in bundles if value == expected_bundle]
    if len(bundles) != 1:
        raise CatalogParseError(f"expected one bundleName, found {bundles or 'none'}")
    bundle = bundles[0]

    abilities = _unique(values, "abilityname", "ability_name")
    main_candidates = _unique(values, "mainability", "mainabilityname", "main_ability")
    main_ability = _select_main_ability(main_candidates, abilities)
    if not main_ability:
        raise CatalogParseError(f"no unambiguous launch Ability for {bundle}")

    display_names = _unique(values, "label", "appname", "displayname", "name")
    display_name = next((name for name in display_names if name not in abilities and name != bundle), bundle)
    modules = _unique(values, "modulename", "module_name")
    version_names = _unique(values, "versionname", "version_name")
    version_codes = _unique(values, "versioncode", "version_code")
    signatures = _unique(values, "signature", "signatureinfo", "fingerprint", "appidentifier")
    signature = signatures[0] if len(signatures) == 1 else None
    if signature and not re.fullmatch(r"[0-9a-fA-F]{64}", signature):
        signature = hashlib.sha256(signature.encode("utf-8")).hexdigest()

    return InstalledApp(
        bundle_name=bundle,
        display_name=display_name,
        abilities=tuple(abilities),
        main_ability=main_ability,
        module_name=modules[0] if len(modules) == 1 else None,
        version_name=version_names[0] if len(version_names) == 1 else None,
        version_code=_one_int(version_codes),
        signature_sha256=signature.lower() if signature else None,
        system_app=_one_bool(values, "issystemapp", "systemapp"),
        raw_output=raw,
    )


def parse_foreground_hierarchy(hierarchy: Mapping[str, Any]) -> ForegroundApp | None:
    """Resolve a unique active/focused foreground application from dumpLayout JSON."""
    candidates: list[tuple[ForegroundApp, bool]] = []
    for node in _walk(hierarchy):
        normalized = {_norm(key): value for key, value in node.items()}
        bundle = _scalar(normalized.get("bundlename"))
        if not bundle or not _looks_like_bundle(bundle):
            continue
        ability = _scalar(normalized.get("abilityname")) or None
        window_type = _scalar(normalized.get("windowtype")) or None
        focused = any(
            _truthy(normalized.get(key))
            for key in ("focused", "isfocused", "active", "isactive", "foreground", "isforeground")
        )
        candidates.append(
            (
                ForegroundApp(
                    bundle_name=bundle,
                    ability_name=ability,
                    window_type=window_type,
                ),
                focused,
            )
        )
    focused = _unique_foreground([item for item, is_focused in candidates if is_focused])
    if len(focused) == 1:
        return focused[0]
    unique = _unique_foreground([item for item, _ in candidates])
    return unique[0] if len(unique) == 1 else None


def _unique_foreground(items: list[ForegroundApp]) -> list[ForegroundApp]:
    result: list[ForegroundApp] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for item in items:
        identity = (item.bundle_name, item.ability_name, item.window_type)
        if identity not in seen:
            seen.add(identity)
            result.append(item)
    return result


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, str)):
        return str(value).strip().casefold() in {"true", "1", "yes", "active", "focused"}
    return False


def _json_payload(raw: str) -> Any | None:
    text = raw.strip()
    starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
    if not starts:
        return None
    start = min(starts)
    for end in range(len(text), start, -1):
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            continue
    return None


def _walk(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _flatten(value: Any) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for node in _walk(value):
        for key, item in node.items():
            if isinstance(item, (str, int, float, bool)):
                result.setdefault(_norm(key), []).append(str(item).strip())
            elif isinstance(item, list) and all(isinstance(part, (str, int)) for part in item):
                result.setdefault(_norm(key), []).extend(str(part).strip() for part in item)
    return result


def _parse_lines(raw: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    pattern = re.compile(r"^\s*['\"]?([\w.-]+)['\"]?\s*[:=]\s*(.*?)\s*[,;]?\s*$")
    for line in raw.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        value = match.group(2).strip().strip("[]{}'\"")
        if value:
            result.setdefault(_norm(match.group(1)), []).append(value)
    return result


def _collect_values(value: Any, keys: set[str]) -> list[str]:
    values = _flatten(value)
    return [item for key in keys for item in values.get(_norm(key), [])]


def _unique(values: Mapping[str, list[str]], *keys: str) -> list[str]:
    result: list[str] = []
    for key in keys:
        for value in values.get(_norm(key), []):
            value = value.strip().strip("'\"")
            if value and value not in result:
                result.append(value)
    return result


def _select_main_ability(main: list[str], abilities: list[str]) -> str | None:
    candidates = list(dict.fromkeys(main))
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        return None
    if len(abilities) == 1:
        return abilities[0]
    entries = [ability for ability in abilities if ability.casefold().endswith("entryability")]
    return entries[0] if len(entries) == 1 else None


def _one_int(values: list[str]) -> int | None:
    if len(values) != 1:
        return None
    match = re.fullmatch(r"\d+", values[0])
    return int(values[0]) if match else None


def _one_bool(values: Mapping[str, list[str]], *keys: str) -> bool:
    candidates = _unique(values, *keys)
    return len(candidates) == 1 and candidates[0].casefold() in {"true", "1", "yes"}


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _scalar(value: Any) -> str:
    return str(value).strip() if isinstance(value, (str, int)) else ""


def _looks_like_bundle(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][\w]*(?:\.[\w]+)+", value))
