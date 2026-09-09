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
