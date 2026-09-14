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
