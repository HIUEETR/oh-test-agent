import pytest

from harmony_test_agent.models import ScreenSnapshot, ToolDecision, ToolName
from harmony_test_agent.runtime import SafetyError, SafetyPolicy


def test_blocks_destructive_task():
    with pytest.raises(SafetyError):
        SafetyPolicy().validate_task("打开设置并删除用户数据")


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
