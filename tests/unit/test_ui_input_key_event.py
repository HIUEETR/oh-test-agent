"""``uitest uiInput keyEvent`` 的两个真机陷阱（计划外发现，复盘 dc-20260923T180535Z-1bed642e）。

1. **按键名不被接受**：真机 usage 原文是 ``keyEvent <keyID/Back/Home/Power> [displayId]`` ——
   只有 ``Back``/``Home``/``Power`` 三个名字合法，``Enter``/``VolumeUp``/``VolumeDown`` 必须传
   数字 keyID。旧实现把模型给的按键名原样透传，``Enter`` 因此从未生效。
2. **失败被记成成功**：参数非法时 ``uitest`` 打印整页 usage 但**退出码仍是 0**，
   ``CommandResult.ok`` 为真 ⇒ 录制账本写下 ``succeeded`` / ``effect_status=confirmed``。
   模型看不到失败，只能改点软键盘补偿，那次点击又被原样写进生成脚本。

``devices/harmony.py`` 早就为坐标越界的 swipe 处理过同一个陷阱（``_ui_input_result``），
但 DC 的 ``DcHdcExecutor.key_event`` 是独立实现、绕过了它。现在两处共用
``devices/base.py`` 的同一份判定，标记表不会再各自漂移。
"""

from __future__ import annotations

import pytest

from harmony_test_agent.dc.hdc import DcHdcExecutor
from harmony_test_agent.devices.base import (
    UI_INPUT_REJECTION_MARKERS,
    normalize_key_name,
    reject_ui_input_usage,
    ui_input_key_argument,
)
from harmony_test_agent.models import CommandResult

# 真机采集的原始输出（dc-20260923T180535Z-1bed642e 的 invocations[13]）。
REAL_USAGE_OUTPUT = (
    "Invalid parameters. \n\nUSAGE : \nhelp                    print uiInput usage\n"
    "keyEvent <keyID/Back/Home/Power> [displayId]    inject keyEvent\n"
)


def result(*, returncode: int = 0, stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(command="hdc", args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# 按键名 → uitest 实参
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Enter 是本次复盘的主角：必须翻成数字 keyID，否则 uitest 直接拒绝。
        ("Enter", "2054"),
        ("enter", "2054"),
        ("ENTER", "2054"),
        (" Enter ", "2054"),
        # 音量键有三种常见写法，归一后都要落到同一个 keyID。
        ("VolumeUp", "16"),
        ("volume_up", "16"),
        ("volume-up", "16"),
        ("Volume Down", "17"),
        # 这三个名字 uitest 原生接受，保留名字（并统一成设备认的大小写）。
        ("Back", "Back"),
        ("back", "Back"),
        ("Home", "Home"),
        ("home", "Home"),
        ("Power", "Power"),
    ],
)
def test_key_argument_translation(raw: str, expected: str) -> None:
    assert ui_input_key_argument(raw) == expected


@pytest.mark.parametrize("raw", ["Menu", "KEYCODE_ENTER", "return", "", "   "])
def test_unknown_keys_are_passed_through_for_the_device_to_reject(raw: str) -> None:
    """白名单外的按键**不猜**：原样透传，让设备拒绝，再由 reject_ui_input_usage 转成失败。"""
    assert ui_input_key_argument(raw) == raw.strip()


def test_normalize_key_name_matches_the_generated_script_side() -> None:
    """归一规则必须与 ``generation/standalone.py::_render_key_event`` 一致。

    两边不一致就会出现「录制时按键生效、生成脚本却认不出」或反过来的错配。
    """
    assert normalize_key_name("Volume Up") == "volumeup"
    assert normalize_key_name("volume-up") == "volume_up"
    assert normalize_key_name("  ENTER ") == "enter"
    # 两种归一结果都必须在按键表里，否则「录制时按得动、生成脚本认不出」。
    from harmony_test_agent.devices.base import _UI_INPUT_KEY_CODES

    assert normalize_key_name("Volume Up") in _UI_INPUT_KEY_CODES
    assert normalize_key_name("volume-up") in _UI_INPUT_KEY_CODES


def test_key_codes_agree_with_the_xdevice_emitter() -> None:
    """数字 keyID 必须与 ``generation/xdevice_case.py::_KEY_CODES`` 的数值一致。

    两处各写一份数值是漂移隐患：录制时按下的键与生成脚本里 ``press_key`` 的键必须是同一个。
    """
    from harmony_test_agent.generation.xdevice_case import _KEY_CODES

    for raw in ("enter", "Enter", "VolumeUp", "volume_up", "VolumeDown", "volume-down"):
        argument = ui_input_key_argument(raw)
        assert argument.isdigit(), f"{raw!r} 应翻成数字 keyID，实际 {argument!r}"
        canonical = normalize_key_name(raw)
        assert int(argument) == _KEY_CODES[canonical][0]


# ---------------------------------------------------------------------------
# usage 拒绝 → 显式失败
# ---------------------------------------------------------------------------


def test_real_usage_output_is_turned_into_an_explicit_failure() -> None:
    """真机原始输出：退出码 0 + ``Invalid parameters.`` + 整页 usage ⇒ 必须判失败。"""
    before = result(returncode=0, stdout=REAL_USAGE_OUTPUT)
    assert before.ok is True, "前置条件：裸返回码确实会被当成成功"

    after = reject_ui_input_usage(before)

    assert after.ok is False
    assert after.returncode == 1
    assert after.stderr.startswith("uiInput rejected the arguments:")
    assert "Invalid parameters." in after.stderr


@pytest.mark.parametrize("marker", UI_INPUT_REJECTION_MARKERS)
def test_every_rejection_marker_is_detected(marker: str) -> None:
    assert reject_ui_input_usage(result(stdout=f"noise {marker} noise")).ok is False


def test_clean_output_is_left_untouched() -> None:
    clean = result(returncode=0, stdout="ok")

    after = reject_ui_input_usage(clean)

    assert after.ok is True
    assert after.returncode == 0
    assert after.stderr == ""


# ---------------------------------------------------------------------------
# DC 执行器接线
# ---------------------------------------------------------------------------


@pytest.fixture
def executor(monkeypatch: pytest.MonkeyPatch) -> DcHdcExecutor:
    monkeypatch.setattr(DcHdcExecutor, "_find_hdc", staticmethod(lambda configured: "hdc"))
    return DcHdcExecutor("127.0.0.1:5555")


def test_dc_key_event_sends_the_numeric_key_id(executor: DcHdcExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, ...]] = []

    def fake_run(*args: str, **kwargs: object) -> CommandResult:
        sent.append(args)
        return result(stdout="ok")

    monkeypatch.setattr(executor, "_run", fake_run)

    assert executor.key_event("Enter").ok is True
    assert sent == [("shell", "uitest", "uiInput", "keyEvent", "2054")]


def test_dc_key_event_keeps_named_keys(executor: DcHdcExecutor, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, ...]] = []
    monkeypatch.setattr(executor, "_run", lambda *a, **k: sent.append(a) or result(stdout="ok"))

    executor.key_event("Back")
    executor.key_event("home")

    assert [item[-1] for item in sent] == ["Back", "Home"]


def test_dc_key_event_reports_failure_instead_of_silent_success(
    executor: DcHdcExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归钉子：设备拒绝时 ``ok`` 必须为假，录制账本才不会写下 succeeded。"""
    monkeypatch.setattr(executor, "_run", lambda *a, **k: result(stdout=REAL_USAGE_OUTPUT))

    assert executor.key_event("Menu").ok is False
