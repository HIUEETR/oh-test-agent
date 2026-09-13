import pytest

from harmony_test_agent.models import ScreenSnapshot, ToolDecision, ToolName
from harmony_test_agent.runtime import SafetyError, SafetyPolicy


def test_blocks_destructive_task():
    with pytest.raises(SafetyError):
        SafetyPolicy().validate_task("打开设置并删除用户数据")


def test_task_text_with_gated_word_is_not_rejected():
    """任务描述里的日常用语（如"确认"）不应导致整个运行失败；门控留给工具决策层面。"""
    SafetyPolicy().validate_task("打开应用并确认页面能正常刷新")


def test_task_text_with_credential_word_is_still_rejected():
    with pytest.raises(SafetyError, match="密码"):
        SafetyPolicy().validate_task("打开应用并输入密码")


def test_gated_word_still_blocks_element_decision():
    """任务文本放行后，点击带"确认"字样的按钮仍受 allow_submit 门控。"""
    with pytest.raises(SafetyError, match="确认"):
        SafetyPolicy().validate_decision(
            ToolDecision(tool=ToolName.CLICK_ELEMENT, target="确认"),
            snapshot=None,
        )


def test_rejects_coordinate_outside_screen(tmp_path):
    snapshot = ScreenSnapshot(
        snapshot_id="s",
        run_id="r",
        image_path=tmp_path / "x.png",
        image_sha256="x",
        width=100,
        height=200,
    )
    with pytest.raises(SafetyError):
        SafetyPolicy().validate_decision(
            ToolDecision(tool=ToolName.CLICK_COORDINATE, coordinate=(101, 10)),
            snapshot,
        )


def test_reasoning_may_describe_blocked_ui_without_executing_it() -> None:
    SafetyPolicy().validate_decision(
        ToolDecision(tool=ToolName.WAIT, wait_seconds=2, reasoning="页面正在恢复登录状态"),
        snapshot=None,
    )


def test_executable_target_still_blocks_sensitive_operation() -> None:
    with pytest.raises(SafetyError, match="登录"):
        SafetyPolicy().validate_decision(
            ToolDecision(tool=ToolName.CLICK_ELEMENT, target="登录"),
            snapshot=None,
        )
