"""启动器桌面扫描建立“显示名 → bundle”映射的离线单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

from harmony_test_agent.devices import DeviceError, HarmonyDeviceAdapter
from harmony_test_agent.devices.harmony import _LAUNCHER_SCAN_MAX_PAGES
from harmony_test_agent.models import CommandResult
from harmony_test_agent.targets.catalog import ForegroundApp, parse_launcher_labels


def icon(text: str, bundle: str) -> dict:
    """按真机 dumpLayout 实测的启动器图标节点形态构造样例。"""
    return {
        "attributes": {"type": "GridItem", "key": f"SwiperPage_GridItem_[{bundle}___50331649___0,1]", "text": ""},
        "children": [
            {"attributes": {"type": "Image", "key": f"Container_AppIcon_Image_{bundle}EntryAbilityentry0", "text": ""}},
            {"attributes": {"type": "Text", "key": "icon-label-uuid", "text": text}},
        ],
    }


def page(*children: dict) -> dict:
    return {
        "attributes": {"type": "WindowScene", "key": "session11", "bounds": "[0,0][1320,2232]"},
        "children": list(children),
    }


PAGE_ONE = page(
    icon("设置", "com.huawei.hmos.settings"),
    icon("日历", "com.huawei.hmos.calendar"),
    {"attributes": {"type": "TextClock", "key": "ClockStatusView", "text": "01:36"}},
)
PAGE_TWO = page(
    icon("知乎++", "com.github.zhuoyi233.zhplus"),
    icon("网易云音乐", "com.example.neteasymusic"),
)
PAGE_BUNDLES = (
    "com.huawei.hmos.calendar",
    "com.github.zhuoyi233.zhplus",
    "com.example.neteasymusic",
    "com.huawei.hmos.settings",
)


def bundle_metadata(bundle: str, label: str) -> str:
    return json.dumps(
        {
            "bundleInfo": {
                "bundleName": bundle,
                "label": label,
                "versionName": "1.0.0",
                "versionCode": 1,
                "isSystemApp": False,
                "hapModuleInfos": [
                    {"moduleName": "entry", "mainAbility": "EntryAbility", "abilityName": ["EntryAbility"]}
                ],
            }
        }
    )


class ScriptedDevice(HarmonyDeviceAdapter):
    """以脚本化输出驱动真实适配器逻辑的离线设备。"""

    def __init__(
        self,
        tmp_path: Path,
        *,
        bundles: str,
        metadata: dict[str, str] | None = None,
        foreground: list[str] | None = None,
        pages: list[dict] | None = None,
        collect_error: Exception | None = None,
    ) -> None:
        super().__init__("127.0.0.1:5555", str(tmp_path / "hdc.exe"))
        self._bundle_list = bundles
        self._metadata = metadata or {}
        self._foreground = list(foreground or [])
        self._pages = list(pages or [])
        self._page_index = 0
        self._collect_error = collect_error
        self.run_calls: list[tuple[str, ...]] = []
        self.collect_calls = 0
        self.swipe_calls = 0

    def _run(self, *args: str, timeout: float | None = None, device: bool = True) -> CommandResult:
        self.run_calls.append(args)
        if args[:4] == ("shell", "bm", "dump", "-a"):
            return self._result(self._bundle_list)
        if args[:4] == ("shell", "bm", "dump", "-n"):
            payload = self._metadata.get(args[4])
            if payload is None:
                return self._result("", returncode=1, stderr=f"not found: {args[4]}")
            return self._result(payload)
        return self._result("")

    def collect_ui_hierarchy(self) -> dict:
        self.collect_calls += 1
        if self._collect_error is not None:
            raise self._collect_error
        if self._page_index < len(self._pages):
            collected = self._pages[self._page_index]
            self._page_index += 1
            return collected
        return self._pages[-1] if self._pages else page()

    def current_foreground_app(self) -> ForegroundApp | None:
        if not self._foreground:
            return None
        return ForegroundApp(bundle_name=self._foreground.pop(0), ability_name=None)

    def swipe(self, start: tuple[int, int], end: tuple[int, int], duration: float = 0.5) -> CommandResult:
        self.swipe_calls += 1
        return self._run("shell", "uitest", "uiInput", "swipe", "0", "0", "0", "0", "1000")

    @staticmethod
    def _result(stdout: str, returncode: int = 0, stderr: str = "") -> CommandResult:
        return CommandResult(command="scripted", args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_parse_launcher_labels_pairs_icon_text_with_bundle_and_ignores_noise() -> None:
    mapping, signature = parse_launcher_labels(PAGE_ONE, PAGE_BUNDLES)

    assert mapping == {"com.huawei.hmos.settings": "设置", "com.huawei.hmos.calendar": "日历"}
    assert "ClockStatusView" in signature
    assert "SwiperPage_GridItem_[com.huawei.hmos.settings___50331649___0,1]" in signature


def test_parse_launcher_labels_prefers_longest_bundle_match() -> None:
    hierarchy = page(icon("智能助手", "com.a.b.long"))

    mapping, _ = parse_launcher_labels(hierarchy, ["com.a.b", "com.a.b.long"])

    assert mapping == {"com.a.b.long": "智能助手"}


def test_parse_launcher_labels_tolerates_empty_hierarchy() -> None:
    assert parse_launcher_labels({}, []) == ({}, ())


def test_display_size_reads_root_bounds(tmp_path: Path) -> None:
    adapter = HarmonyDeviceAdapter("127.0.0.1:5555", str(tmp_path / "hdc.exe"))

    assert adapter._display_size(page()) == (1320, 2232)
    assert adapter._display_size({"attributes": {}}) is None


def test_find_installed_apps_resolves_launcher_label_across_pages(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("harmony_test_agent.devices.harmony.time.sleep", lambda seconds: None)
    device = ScriptedDevice(
        tmp_path,
        bundles="\n".join(PAGE_BUNDLES) + "\n",
        metadata={bundle: bundle_metadata(bundle, "$string:app_name") for bundle in PAGE_BUNDLES},
        foreground=["com.ohos.sceneboard"],
        pages=[PAGE_ONE, PAGE_TWO],
    )

    apps = device.find_installed_apps("知乎++")

    assert [(app.bundle_name, app.display_name, app.main_ability) for app in apps] == [
        ("com.github.zhuoyi233.zhplus", "知乎++", "EntryAbility")
    ]
    assert device.collect_calls == 3
    assert device.swipe_calls == 2
    assert not any("Home" in " ".join(args) for args in device.run_calls)


def test_find_installed_apps_presses_home_when_not_on_launcher(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("harmony_test_agent.devices.harmony.time.sleep", lambda seconds: None)
    device = ScriptedDevice(
        tmp_path,
        bundles="\n".join(PAGE_BUNDLES) + "\n",
        metadata={bundle: bundle_metadata(bundle, "$string:app_name") for bundle in PAGE_BUNDLES},
        foreground=["com.example.other", "com.ohos.sceneboard"],
        pages=[PAGE_ONE, PAGE_TWO],
    )

    apps = device.find_installed_apps("知乎++")

    assert [app.bundle_name for app in apps] == ["com.github.zhuoyi233.zhplus"]
    assert any("Home" in " ".join(args) for args in device.run_calls)


def test_find_installed_apps_falls_back_to_catalog_when_launcher_scan_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("harmony_test_agent.devices.harmony.time.sleep", lambda seconds: None)
    bundles = ("com.example.settingsapp", "com.github.zhuoyi233.zhplus")
    device = ScriptedDevice(
        tmp_path,
        bundles="\n".join(bundles) + "\n",
        metadata={
            "com.example.settingsapp": bundle_metadata("com.example.settingsapp", "Settings"),
            "com.github.zhuoyi233.zhplus": bundle_metadata("com.github.zhuoyi233.zhplus", "$string:app_name"),
        },
        collect_error=DeviceError("dumpLayout failed"),
    )

    apps = device.find_installed_apps("Settings")

    assert [(app.bundle_name, app.display_name) for app in apps] == [("com.example.settingsapp", "Settings")]


def test_find_installed_apps_skips_unresolvable_launcher_candidates(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("harmony_test_agent.devices.harmony.time.sleep", lambda seconds: None)
    launcher_page = page(icon("音乐A", "com.example.musica"), icon("音乐B", "com.example.musicb"))
    device = ScriptedDevice(
        tmp_path,
        bundles="com.example.musica\ncom.example.musicb\n",
        metadata={"com.example.musicb": bundle_metadata("com.example.musicb", "$string:app_name")},
        foreground=["com.ohos.sceneboard"],
        pages=[launcher_page],
    )

    apps = device.find_installed_apps("音乐")

    assert [(app.bundle_name, app.display_name) for app in apps] == [("com.example.musicb", "音乐B")]


def test_launcher_scan_stops_at_max_pages(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("harmony_test_agent.devices.harmony.time.sleep", lambda seconds: None)
    device = ScriptedDevice(
        tmp_path,
        bundles="\n".join(f"com.test.p{index}" for index in range(_LAUNCHER_SCAN_MAX_PAGES)) + "\n",
        foreground=["com.ohos.sceneboard"],
    )

    def fake_collect() -> dict:
        index = device.collect_calls
        device.collect_calls += 1
        return page(icon(f"App{index}", f"com.test.p{index}"))

    device.collect_ui_hierarchy = fake_collect

    mapping = device.launcher_app_names()

    assert device.collect_calls == _LAUNCHER_SCAN_MAX_PAGES
    assert device.swipe_calls == _LAUNCHER_SCAN_MAX_PAGES - 1
    assert len(mapping) == _LAUNCHER_SCAN_MAX_PAGES
