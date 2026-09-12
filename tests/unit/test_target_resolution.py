from __future__ import annotations

import hashlib

import pytest

from harmony_test_agent.devices import DeviceError
from harmony_test_agent.models import TargetQuery
from harmony_test_agent.targets import TargetAmbiguousError, TargetNotFoundError, TargetResolver
from harmony_test_agent.targets.catalog import CatalogParseError, InstalledApp, parse_bundle_list, parse_installed_app


class CatalogDevice:
    def __init__(self, apps: list[InstalledApp], inspected: dict[str, InstalledApp] | None = None) -> None:
        self.device_id = "test-device"
        self._apps = apps
        self._inspected = inspected or {app.bundle_name: app for app in apps}
        self.inspected_bundles: list[str] = []

    def list_installed_apps(self) -> list[InstalledApp]:
        return list(self._apps)

    def find_installed_apps(self, label: str) -> list[InstalledApp]:
        # 模拟真实适配器的 bm dump 加速器：返回宽松候选，精确归一化交给 resolver。
        needle = " ".join(label.split()).casefold()
        return [app for app in self._apps if needle in " ".join(app.display_name.split()).casefold()]

    def inspect_app(self, bundle_name: str) -> InstalledApp:
        self.inspected_bundles.append(bundle_name)
        try:
            return self._inspected[bundle_name]
        except KeyError as exc:
            raise DeviceError(f"bundle is unavailable: {bundle_name}") from exc


def installed_app(
    bundle_name: str,
    display_name: str,
    *,
    main_ability: str | None = "EntryAbility",
) -> InstalledApp:
    return InstalledApp(
        bundle_name=bundle_name,
        display_name=display_name,
        abilities=(main_ability,) if main_ability else (),
        main_ability=main_ability,
        module_name="entry",
        version_name="1.2.3",
        version_code=12,
    )


def test_bm_parsers_extract_sorted_catalog_and_complete_app_metadata() -> None:
    catalog = """bm diagnostic header
    {
      "bundles": [
        {"bundleName": "com.example.notes"},
        {"bundle_name": "org.demo.reader"},
        {"bundleName": "com.example.notes"},
        {"bundleName": "not-a-bundle"}
      ]
    }
    bm diagnostic footer
    """
    assert parse_bundle_list(catalog) == ["com.example.notes", "org.demo.reader"]

    metadata = """warning: compatibility output
    {
      "bundleInfo": {
        "bundleName": "com.example.notes",
        "label": "Pocket Notes",
        "versionName": "2.4.1",
        "versionCode": 241,
        "signature": "developer-certificate",
        "isSystemApp": false,
        "hapModuleInfos": [{
          "moduleName": "entry",
          "mainAbility": "MainAbility",
          "abilityName": ["MainAbility", "SettingsAbility"]
        }]
      }
    }
    """
    app = parse_installed_app(metadata, expected_bundle="com.example.notes")

    assert app.bundle_name == "com.example.notes"
    assert app.display_name == "Pocket Notes"
    assert app.main_ability == "MainAbility"
    assert app.abilities == ("MainAbility", "SettingsAbility")
    assert app.module_name == "entry"
    assert app.version_name == "2.4.1"
    assert app.version_code == 241
    assert app.signature_sha256 == hashlib.sha256(b"developer-certificate").hexdigest()
    assert app.system_app is False


def test_bm_metadata_parser_rejects_ambiguous_launch_ability() -> None:
    metadata = """
    {
      "bundleName": "com.example.ambiguous",
      "label": "Ambiguous",
      "abilityName": ["FirstAbility", "SecondAbility"]
    }
    """

    with pytest.raises(CatalogParseError, match="no unambiguous launch Ability"):
        parse_installed_app(metadata, expected_bundle="com.example.ambiguous")


def test_resolver_prefers_an_exact_bundle_and_inspects_missing_launch_metadata() -> None:
    catalog_entry = installed_app("com.example.notes", "Pocket Notes", main_ability=None)
    inspected_entry = installed_app("com.example.notes", "Pocket Notes", main_ability="MainAbility")
    device = CatalogDevice([catalog_entry], {inspected_entry.bundle_name: inspected_entry})

    resolved = TargetResolver(device).resolve(TargetQuery(app_name="  Pocket Notes  ", bundle_name="com.example.notes"))

    assert resolved.bundle_name == "com.example.notes"
    assert resolved.target_app_id == "com-example-notes"
    assert resolved.main_ability == "MainAbility"
    assert resolved.device_id == "test-device"
    assert resolved.source == "explicit_override"
    assert device.inspected_bundles == ["com.example.notes"]


def test_resolver_accepts_one_normalized_name_candidate() -> None:
    device = CatalogDevice(
        [
            installed_app("com.example.calendar", "Calendar"),
            installed_app("com.example.reader", "Pocket   Reader"),
        ]
    )

    resolved = TargetResolver(device).resolve(TargetQuery(app_name="reader"))

    assert resolved.bundle_name == "com.example.reader"
    assert resolved.display_name == "Pocket   Reader"
    assert resolved.source == "installed_app"


def test_resolver_reports_all_ambiguous_name_candidates() -> None:
    apps = [
        installed_app("com.example.notes", "Pocket Notes"),
        installed_app("org.example.noteslite", "Notes Lite"),
        installed_app("com.example.calendar", "Calendar"),
    ]

    with pytest.raises(TargetAmbiguousError) as caught:
        TargetResolver(CatalogDevice(apps)).resolve(TargetQuery(app_name="notes"))

    assert [candidate.bundle_name for candidate in caught.value.candidates] == [
        "com.example.notes",
        "org.example.noteslite",
    ]


def test_resolver_reports_no_name_or_bundle_match() -> None:
    device = CatalogDevice([installed_app("com.example.calendar", "Calendar")])

    with pytest.raises(TargetNotFoundError, match="installed application not found"):
        TargetResolver(device).resolve(TargetQuery(app_name="Missing App"))

    with pytest.raises(TargetNotFoundError, match="installed bundle not found"):
        TargetResolver(device).resolve(TargetQuery(bundle_name="com.example.missing"))
