from pathlib import Path

import pytest

from harmony_test_agent.devices import DeviceError, HarmonyDeviceAdapter


def test_hdc_adapter_prefers_configured_executable(tmp_path: Path) -> None:
    executable = tmp_path / "hdc.exe"
    executable.write_bytes(b"")

    adapter = HarmonyDeviceAdapter("127.0.0.1:5555", str(executable))

    assert Path(adapter.hdc_path) == executable


def test_hdc_adapter_rejects_missing_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HDC_PATH", raising=False)
    monkeypatch.setattr("harmony_test_agent.devices.harmony.shutil.which", lambda _: None)

    with pytest.raises(DeviceError, match="set HDC_PATH"):
        HarmonyDeviceAdapter("127.0.0.1:5555", "missing-hdc.exe")


def test_ensure_on_launcher_always_presses_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """桌面已在前台时也必须按 Home：launcher 停留在上次浏览的页，跳过按键会让扫描漏掉主页图标。"""
    from harmony_test_agent.models import CommandResult
    from harmony_test_agent.targets import ForegroundApp

    adapter = HarmonyDeviceAdapter("127.0.0.1:5555", "hdc.exe")
    commands: list[tuple[str, ...]] = []

    def scripted_run(*args: str, timeout: float | None = None, device: bool = True) -> CommandResult:
        commands.append(args)
        return CommandResult(command=" ".join(args), returncode=0)

    monkeypatch.setattr(adapter, "_run", scripted_run)
    monkeypatch.setattr(
        adapter,
        "current_foreground_app",
        lambda: ForegroundApp(bundle_name="com.ohos.sceneboard", ability_name=None),
    )

    assert adapter._ensure_on_launcher() is True
    assert ("shell", "uitest", "uiInput", "keyEvent", "Home") in commands


def test_inspect_app_reports_missing_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    """`bm dump -n` 对不存在的包仍以 0 退出，只打印提示：必须报「未安装」而不是「ambiguous」。"""
    from harmony_test_agent.models import CommandResult

    adapter = HarmonyDeviceAdapter("127.0.0.1:5555", "hdc.exe")
    monkeypatch.setattr(
        adapter,
        "_run",
        lambda *args, **kwargs: CommandResult(
            command=" ".join(args),
            returncode=0,
            stdout="error: failed to get information and the parameters may be wrong.\n",
        ),
    )

    with pytest.raises(DeviceError, match="is not installed"):
        adapter.inspect_app("com.absent.bundle")


def test_inspect_app_reports_failed_command(monkeypatch: pytest.MonkeyPatch) -> None:
    from harmony_test_agent.models import CommandResult

    adapter = HarmonyDeviceAdapter("127.0.0.1:5555", "hdc.exe")
    monkeypatch.setattr(
        adapter,
        "_run",
        lambda *args, **kwargs: CommandResult(command=" ".join(args), returncode=1, stderr="hdc: device offline"),
    )

    with pytest.raises(DeviceError, match="bm dump -n com.example.app failed"):
        adapter.inspect_app("com.example.app")


def _layout_command_adapter(commands: list[tuple[str, ...]], layout: dict) -> HarmonyDeviceAdapter:
    """构造一个只对 dumpLayout/cat 有响应的适配器，记录收到的命令。"""
    import json

    from harmony_test_agent.models import CommandResult

    adapter = HarmonyDeviceAdapter("127.0.0.1:5555", "hdc.exe")

    def scripted_run(*args: str, timeout: float | None = None, device: bool = True) -> CommandResult:
        commands.append(args)
        if "dumpLayout" in args:
            return CommandResult(
                command=" ".join(args),
                returncode=0,
                stdout="DumpLayout saved to: /data/local/tmp/layout_probe.json\n",
            )
        if "cat" in args:
            return CommandResult(command=" ".join(args), returncode=0, stdout=json.dumps(layout))
        return CommandResult(command=" ".join(args), returncode=0)

    assert callable(adapter._run)
    adapter._run = scripted_run  # type: ignore[method-assign]
    return adapter


def test_collect_ui_hierarchy_skips_font_attributes() -> None:
    """`-a`（附带字体属性）真机实测 6.54s/126.8KB，不传 3.16s/117.0KB，属性集合完全相同。"""
    commands: list[tuple[str, ...]] = []
    layout = {"attributes": {"type": "root", "bounds": "[0,0][10,10]"}, "children": []}
    adapter = _layout_command_adapter(commands, layout)

    assert adapter.collect_ui_hierarchy() == layout

    dump_commands = [command for command in commands if "dumpLayout" in command]
    assert dump_commands == [("shell", "uitest", "dumpLayout")]
    assert all("-a" not in command for command in dump_commands)


def test_screenshot_captures_image_and_hierarchy_concurrently(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """图像链路与层级链路是独立只读 HDC 动作：并行只付较大者，串行要付两者之和。"""
    import threading
    import time

    adapter = HarmonyDeviceAdapter("127.0.0.1:5555", "hdc.exe")
    active = {"count": 0, "peak": 0}
    lock = threading.Lock()

    def enter() -> None:
        with lock:
            active["count"] += 1
            active["peak"] = max(active["peak"], active["count"])

    def leave() -> None:
        with lock:
            active["count"] -= 1

    model_image = tmp_path / "frame.device.jpeg"
    model_image.write_bytes(b"jpeg-bytes")

    def fake_capture_png(remote_path: str, local_path: Path) -> tuple[int, int, Path]:
        enter()
        try:
            time.sleep(0.3)
            local_path.write_bytes(b"png-bytes")
            assert remote_path.startswith("/data/local/tmp/")
            return 1320, 2232, model_image
        finally:
            leave()

    layout = {
        "attributes": {"type": "root", "bounds": "[0,0][1320,2232]"},
        "children": [{"attributes": {"type": "Button", "text": "首页", "bounds": "[0,0][100,100]", "key": "btn_home"}}],
    }

    def fake_hierarchy() -> dict:
        enter()
        try:
            time.sleep(0.3)
            return layout
        finally:
            leave()

    monkeypatch.setattr(adapter, "_capture_png", fake_capture_png)
    monkeypatch.setattr(adapter, "collect_ui_hierarchy", fake_hierarchy)

    started = time.monotonic()
    snapshot = adapter.screenshot(tmp_path / "screens", "run-probe", "step_01_after")
    elapsed = time.monotonic() - started

    assert active["peak"] == 2, "两条采集链路必须同时在跑，否则退化成串行"
    assert elapsed < 0.58, f"并行采集应接近单条链路耗时，实测 {elapsed:.2f}s"
    assert snapshot.width == 1320 and snapshot.height == 2232
    assert snapshot.model_image_path == model_image.resolve()
    assert [element.key for element in snapshot.elements] == ["btn_home"]


def test_ui_input_rejection_is_reported_as_failure() -> None:
    """`uitest uiInput swipe` 参数非法时打印 usage 但退出码仍为 0：必须转成失败。

    真机实测 `uiInput swipe 1118 2231 1118 0 11500`（终点 y=0）就是这样被静默记成成功的。
    """
    from harmony_test_agent.models import CommandResult

    adapter = HarmonyDeviceAdapter("127.0.0.1:5555", "hdc.exe")
    adapter._run = lambda *args, **kwargs: CommandResult(  # type: ignore[method-assign]
        command=" ".join(args),
        returncode=0,
        stdout="Please confirm that the coordinate values are correct. \n\nUSAGE : \nswipe/drag ...",
    )

    result = adapter.swipe((1118, 2231), (1118, 0))

    assert result.ok is False
    assert "rejected the arguments" in result.stderr


def test_ui_input_success_is_not_flagged() -> None:
    from harmony_test_agent.models import CommandResult

    adapter = HarmonyDeviceAdapter("127.0.0.1:5555", "hdc.exe")
    adapter._run = lambda *args, **kwargs: CommandResult(  # type: ignore[method-assign]
        command=" ".join(args), returncode=0, stdout="No Error"
    )

    assert adapter.swipe((1118, 1400), (1118, 600)).ok is True
    assert adapter.click(10, 20).ok is True
