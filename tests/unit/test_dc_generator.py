"""DcHypiumGenerator 脚本生成测试：无断言、无 Profile、工具映射。"""

from __future__ import annotations

from pathlib import Path

from harmony_test_agent.dc.generator import DcHypiumGenerator
from harmony_test_agent.dc.models import DcToolInvocation, DcToolName, DcToolTier, utc_now
from harmony_test_agent.storage.artifacts import ArtifactStore


def _invocation(
    tool: DcToolName,
    args: dict | None = None,
    success: bool = True,
    invocation_id: str = "inv-001",
) -> DcToolInvocation:
    return DcToolInvocation(
        invocation_id=invocation_id,
        turn_id="turn-001",
        tool=tool,
        tier=DcToolTier.L1,
        args=args or {},
        success=success,
        started_at=utc_now(),
        ended_at=utc_now(),
        duration_ms=100,
    )


class TestDcHypiumGenerator:
    """DC 脚本生成器测试。"""

    def test_no_fallback_assertion(self, tmp_path: Path) -> None:
        """关键差异：无断言时不注入兜底断言（与 hypium.py:191-199 相反）。"""
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        invocations = [
            _invocation(DcToolName.CLICK, {"x": 100, "y": 200}, invocation_id="inv-001"),
            _invocation(DcToolName.WAIT, {"seconds": 2}, invocation_id="inv-002"),
        ]
        result = generator.generate(
            session_id="dc-test-001",
            device_id="127.0.0.1:5555",
            invocations=invocations,
        )
        # 脚本中不应包含 check_component_exist（兜底断言）
        assert "check_component_exist" not in result.python_text
        # 脚本中不应包含 fallback assertion 警告
        assert not any("fallback assertion" in w for w in result.warnings)

    def test_script_contains_driver_connect(self, tmp_path: Path) -> None:
        """生成的脚本必须包含 UiDriver.connect。"""
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        invocations = [_invocation(DcToolName.CLICK, {"x": 100, "y": 200})]
        result = generator.generate(
            session_id="dc-test-002",
            device_id="127.0.0.1:5555",
            invocations=invocations,
        )
        assert "UiDriver.connect" in result.python_text

    def test_click_coordinate_maps_to_touch(self, tmp_path: Path) -> None:
        """click(坐标) → driver.touch((x, y))。"""
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        invocations = [_invocation(DcToolName.CLICK, {"x": 100, "y": 200})]
        result = generator.generate(
            session_id="dc-test-003",
            device_id="127.0.0.1:5555",
            invocations=invocations,
        )
        assert "driver.touch((100, 200))" in result.python_text

    def test_swipe_maps_to_driver_swipe(self, tmp_path: Path) -> None:
        """swipe → driver.swipe("UP")。"""
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        invocations = [_invocation(DcToolName.SWIPE, {"start": [100, 500], "end": [100, 200], "duration": 0.5})]
        result = generator.generate(
            session_id="dc-test-004",
            device_id="127.0.0.1:5555",
            invocations=invocations,
        )
        assert "driver.swipe(" in result.python_text

    def test_back_maps_to_go_back(self, tmp_path: Path) -> None:
        """back → driver.go_back()。"""
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        invocations = [_invocation(DcToolName.BACK, {})]
        result = generator.generate(
            session_id="dc-test-005",
            device_id="127.0.0.1:5555",
            invocations=invocations,
        )
        assert "driver.go_back()" in result.python_text

    def test_input_text_maps_to_driver_input(self, tmp_path: Path) -> None:
        """input_text → driver.input_text(...)。"""
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        invocations = [_invocation(DcToolName.INPUT_TEXT, {"text": "hello", "x": 100, "y": 200})]
        result = generator.generate(
            session_id="dc-test-006",
            device_id="127.0.0.1:5555",
            invocations=invocations,
        )
        assert "driver.input_text(" in result.python_text
        assert "'hello'" in result.python_text

    def test_non_replayable_becomes_comment(self, tmp_path: Path) -> None:
        """L2/L4/L5 工具生成 # skipped 注释，不生成执行代码。"""
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        invocations = [
            _invocation(DcToolName.COLLECT_LOGS, {}, invocation_id="inv-001"),
            _invocation(DcToolName.EXECUTE_SHELL, {"argv": ["ls"]}, invocation_id="inv-002"),
            _invocation(DcToolName.FILE_SEND, {"local": "a", "remote": "b"}, invocation_id="inv-003"),
        ]
        result = generator.generate(
            session_id="dc-test-007",
            device_id="127.0.0.1:5555",
            invocations=invocations,
        )
        assert "# skipped: collect_logs" in result.python_text
        assert "# skipped: execute_shell" in result.python_text
        assert "# skipped: file_send" in result.python_text
        assert result.included_operations == 0
        assert len(result.omitted_operations) == 3

    def test_failed_invocation_omitted(self, tmp_path: Path) -> None:
        """失败的工具调用不进入脚本。"""
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        invocations = [
            _invocation(DcToolName.CLICK, {"x": 100, "y": 200}, success=False, invocation_id="inv-001"),
        ]
        result = generator.generate(
            session_id="dc-test-008",
            device_id="127.0.0.1:5555",
            invocations=invocations,
        )
        assert result.included_operations == 0
        assert any("failed" in o["reason"] for o in result.omitted_operations)

    def test_script_saved_to_dc_directory(self, tmp_path: Path) -> None:
        """脚本落盘到 artifacts.run_dir("dc-<id>")/generated/。"""
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        invocations = [_invocation(DcToolName.CLICK, {"x": 100, "y": 200})]
        result = generator.generate(
            session_id="dc-test-009",
            device_id="127.0.0.1:5555",
            invocations=invocations,
        )
        script_path = Path(result.python_path)
        assert script_path.exists()
        assert "dc-test-009" in str(script_path)
        assert "generated" in str(script_path)

    def test_purpose_is_dc_recording(self, tmp_path: Path) -> None:
        """生成的 config 标记 purpose=dc_recording, replay_eligible=False。"""
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        invocations = [_invocation(DcToolName.CLICK, {"x": 100, "y": 200})]
        result = generator.generate(
            session_id="dc-test-010",
            device_id="127.0.0.1:5555",
            invocations=invocations,
        )
        # 检查 config JSON
        import json

        config_path = Path(result.python_path).with_suffix(".json")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert config["purpose"] == "dc_recording"
        assert config["replay_eligible"] is False

    def test_empty_invocations_produces_pass(self, tmp_path: Path) -> None:
        """无操作时脚本体为 pass。"""
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        result = generator.generate(
            session_id="dc-test-011",
            device_id="127.0.0.1:5555",
            invocations=[],
        )
        assert "pass" in result.python_text
        assert result.included_operations == 0
