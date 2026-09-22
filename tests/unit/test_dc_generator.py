"""DcHypiumGenerator 脚本生成测试：无断言、无 Profile、工具映射。"""

from __future__ import annotations

import json
from pathlib import Path

from harmony_test_agent.dc.generator import DcHypiumGenerator
from harmony_test_agent.dc.models import DcScriptArtifact, DcToolInvocation, DcToolName, DcToolTier, utc_now
from harmony_test_agent.models import BoundingBox, UIElement
from harmony_test_agent.storage.artifacts import ArtifactStore


def _invocation(
    tool: DcToolName,
    args: dict | None = None,
    success: bool = True,
    invocation_id: str = "inv-001",
    resolved_element: UIElement | None = None,
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
        resolved_element=resolved_element,
    )


def _assertion(
    tool: DcToolName,
    target: str,
    *,
    key: str = "",
    success: bool = True,
    invocation_id: str = "inv-assert",
) -> DcToolInvocation:
    """构造一条断言工具调用；``key`` 非空时附带结构化元素证据。"""
    element = (
        UIElement(
            element_id=key or target,
            key=key,
            content=target,
            bbox=BoundingBox(left=0, top=0, right=10, bottom=10),
        )
        if key
        else None
    )
    return _invocation(
        tool,
        {"target": target},
        success=success,
        invocation_id=invocation_id,
        resolved_element=element,
    )


def _config(result: DcScriptArtifact) -> dict:
    return json.loads(Path(result.python_path).with_suffix(".json").read_text(encoding="utf-8"))


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

    # ------------------------------------------------------------------
    # Phase 3（2026-09-17）：断言渲染 + replay_eligible 条件判定
    # ------------------------------------------------------------------

    def test_dc_script_with_assertions_is_replay_eligible(self, tmp_path: Path) -> None:
        """含显式断言 + 非占位应用身份 → replay_eligible=True 且渲染检查点。"""
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        invocations = [
            _assertion(DcToolName.ASSERT_VISIBLE, "搜索", key="home_search_button"),
            _invocation(DcToolName.CLICK, {"x": 100, "y": 200}, invocation_id="inv-002"),
            _assertion(DcToolName.ASSERT_NOT_VISIBLE, "加载中", invocation_id="inv-003"),
            _assertion(DcToolName.ASSERT_TEXT, "OpenHarmony", invocation_id="inv-004"),
        ]
        result = generator.generate(
            session_id="dc-test-assertions",
            device_id="127.0.0.1:5555",
            invocations=invocations,
            bundle_name="com.zhihu.plus",
            main_ability="MainAbility",
        )

        assert result.replay_eligible is True
        assert result.explicit_assertions == 3
        assert "driver.check_component_exist(BY.key('home_search_button'), expect_exist=True)" in result.python_text
        assert "driver.check_component_exist(BY.text('加载中'), expect_exist=False)" in result.python_text

        config = _config(result)
        assert config["purpose"] == "acceptance"
        assert config["replay_eligible"] is True
        assert config["explicit_assertions"] == 3

    def test_dc_script_without_assertions_is_still_runnable(self, tmp_path: Path) -> None:
        """无断言只降置信度：脚本照样可执行（acceptance），仍不注入兜底断言。"""
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        invocations = [_invocation(DcToolName.CLICK, {"x": 100, "y": 200})]
        result = generator.generate(
            session_id="dc-test-no-assertions",
            device_id="127.0.0.1:5555",
            invocations=invocations,
            bundle_name="com.zhihu.plus",
            main_ability="MainAbility",
        )

        assert result.replay_eligible is True
        assert result.confidence == "medium"
        assert result.confidence_factors == ["no explicit assert_* tool call was recorded"]
        assert result.explicit_assertions == 0
        assert "check_component_exist" not in result.python_text
        config = _config(result)
        assert config["purpose"] == "acceptance"
        assert config["confidence"] == "medium"

    def test_dc_script_placeholder_identity_is_not_replay_eligible(self, tmp_path: Path) -> None:
        """占位 bundle：即使有断言也物理上跑不到目标应用 ⇒ 不可执行（runnable blocker）。"""
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        invocations = [_assertion(DcToolName.ASSERT_VISIBLE, "搜索")]
        result = generator.generate(
            session_id="dc-test-placeholder",
            device_id="127.0.0.1:5555",
            invocations=invocations,
        )

        assert result.replay_eligible is False
        assert result.runnable_blockers == ["app identity is a placeholder (com.example.app/EntryAbility)"]

    def test_dc_script_accepts_default_entry_ability_as_real_identity(self, tmp_path: Path) -> None:
        """``EntryAbility`` 是鸿蒙工程的默认且常见的真实 ability 名，不构成占位。

        计划的 G3 原文把它当占位哨兵，但那会让真实运行
        （``com.github.zhuoyi233.zhplus / EntryAbility``）永远不可执行，违背 G1。
        """
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        result = generator.generate(
            session_id="dc-test-default-ability",
            device_id="127.0.0.1:5555",
            invocations=[_assertion(DcToolName.ASSERT_VISIBLE, "首页")],
            bundle_name="com.zhihu.plus",
            main_ability="EntryAbility",
        )

        assert result.replay_eligible is True
        assert result.runnable_blockers == []
        assert _config(result)["purpose"] == "acceptance"

    def test_dc_script_purpose_acceptance_vs_recording(self, tmp_path: Path) -> None:
        """purpose 随可执行性变化：真实身份 → acceptance；占位身份 → dc_recording。"""
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        eligible = generator.generate(
            session_id="dc-test-purpose-ok",
            device_id="d",
            invocations=[_assertion(DcToolName.ASSERT_VISIBLE, "首页")],
            bundle_name="com.demo.app",
            main_ability="MainAbility",
        )
        recording = generator.generate(
            session_id="dc-test-purpose-ko",
            device_id="d",
            invocations=[_invocation(DcToolName.BACK, {})],
            bundle_name="com.example.app",
            main_ability="MainAbility",
        )

        assert _config(eligible)["purpose"] == "acceptance"
        assert _config(recording)["purpose"] == "dc_recording"

    def test_failed_assertion_does_not_count_as_explicit_assertion(self, tmp_path: Path) -> None:
        """失败的断言不算 explicit_assertions：不能让置信度被误判成 high。"""
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        invocations = [
            _assertion(DcToolName.ASSERT_VISIBLE, "不存在的元素", success=False, invocation_id="inv-001"),
            _invocation(DcToolName.BACK, {}, invocation_id="inv-002"),
        ]
        result = generator.generate(
            session_id="dc-test-failed-assertion",
            device_id="d",
            invocations=invocations,
            bundle_name="com.demo.app",
            main_ability="MainAbility",
        )

        assert result.explicit_assertions == 0
        # 仍有一条可回放动作（BACK → go_back）且身份真实 ⇒ 可执行，只是置信度为 medium。
        assert result.replay_eligible is True
        assert result.confidence == "medium"

    # ------------------------------------------------------------------
    # 改动 A：resolved_element → 结构化选择器；改动 C2：swipe 警告聚合
    # ------------------------------------------------------------------

    def _element(self, *, key: str = "", element_id: str = "") -> UIElement:
        return UIElement(
            element_id=key or element_id,
            key=key,
            id=element_id,
            content="搜索",
            clickable=True,
            bbox=BoundingBox(left=0, top=0, right=100, bottom=50),
        )

    def test_click_with_resolved_element_uses_key_selector(self, tmp_path: Path) -> None:
        """命中元素带 key → 生成 BY.key 选择器，不再退化为坐标。"""
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        invocations = [
            _invocation(
                DcToolName.CLICK,
                {"x": 10, "y": 20},
                resolved_element=self._element(key="home_search"),
            )
        ]
        result = generator.generate(
            session_id="dc-test-resolved-key",
            device_id="127.0.0.1:5555",
            invocations=invocations,
        )

        assert "driver.touch(BY.key('home_search'))" in result.python_text
        assert "coordinate fallback" not in result.python_text

    def test_click_with_resolved_element_id_uses_id_selector(self, tmp_path: Path) -> None:
        """只有 id 没有 key 时退到 BY.id（仍是结构化选择器，非坐标）。"""
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        invocations = [
            _invocation(
                DcToolName.CLICK,
                {"x": 10, "y": 20},
                resolved_element=self._element(element_id="submit_button"),
            )
        ]
        result = generator.generate(
            session_id="dc-test-resolved-id",
            device_id="127.0.0.1:5555",
            invocations=invocations,
        )

        assert "driver.touch(BY.id('submit_button'))" in result.python_text
        assert "coordinate fallback" not in result.python_text

    def test_click_without_resolved_element_keeps_coordinate_fallback(self, tmp_path: Path) -> None:
        """未命中元素（resolved_element=None）时保持坐标写法：改动 A 的边界行为不变。"""
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        invocations = [_invocation(DcToolName.CLICK, {"x": 10, "y": 20})]
        result = generator.generate(
            session_id="dc-test-coordinate-fallback",
            device_id="127.0.0.1:5555",
            invocations=invocations,
        )

        assert "driver.touch((10, 20))  # coordinate fallback" in result.python_text

    def test_input_text_with_resolved_element_uses_key_selector(self, tmp_path: Path) -> None:
        """input_text 命中元素带 key → BY.key 定位器，且不再产生 semantic fallback 警告。

        这是改动 A 在 input_text 上的效果链终点：只补录 resolved_element 而渲染层
        不消费它，脚本仍会写 ``BY.text('输入框')`` 并留下警告墙里的那条 fallback。
        """
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        invocations = [
            _invocation(
                DcToolName.INPUT_TEXT,
                {"text": "hello", "coordinate": [10, 20]},
                resolved_element=self._element(key="search_input"),
            )
        ]
        result = generator.generate(
            session_id="dc-test-input-text-locator",
            device_id="127.0.0.1:5555",
            invocations=invocations,
        )

        assert "driver.input_text(BY.key('search_input'), 'hello')" in result.python_text
        assert not [warning for warning in result.warnings if "fell back to exact text" in warning]

    def test_input_text_without_resolved_element_keeps_text_fallback(self, tmp_path: Path) -> None:
        """未命中元素时仍写 BY.text('输入框') 并保留 fallback 警告（边界行为不变）。"""
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        invocations = [_invocation(DcToolName.INPUT_TEXT, {"text": "hello", "coordinate": [10, 20]})]
        result = generator.generate(
            session_id="dc-test-input-text-fallback",
            device_id="127.0.0.1:5555",
            invocations=invocations,
        )

        assert "driver.input_text(BY.text('输入框'), 'hello')" in result.python_text
        assert any("fell back to exact text" in warning for warning in result.warnings)

    def test_swipe_warnings_are_aggregated_by_direction(self, tmp_path: Path) -> None:
        """同类 swipe 推断警告聚合为一条并带计数，方向顺序固定（改动 C2）。"""
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        invocations = [
            _invocation(DcToolName.SWIPE, {"start": [100, 500], "end": [100, 200]}, invocation_id="inv-sw-1"),
            _invocation(DcToolName.SWIPE, {"start": [100, 600], "end": [100, 300]}, invocation_id="inv-sw-2"),
            _invocation(DcToolName.SWIPE, {"start": [500, 100], "end": [100, 100]}, invocation_id="inv-sw-3"),
        ]
        result = generator.generate(
            session_id="dc-test-swipe-aggregate",
            device_id="127.0.0.1:5555",
            invocations=invocations,
        )

        swipe_warnings = [warning for warning in result.warnings if "swipe direction inferred" in warning]
        assert swipe_warnings == [
            "swipe direction inferred as UP x2 (from start/end coordinates)",
            "swipe direction inferred as LEFT x1 (from start/end coordinates)",
        ]
        # 不再逐条携带 invocation_id 刷屏
        assert not any(warning.startswith("inv-sw-") for warning in result.warnings)
        # 三条 swipe 仍然各自渲染成脚本行
        assert result.python_text.count("driver.swipe(") == 3

    def test_swipe_with_explicit_direction_is_not_counted(self, tmp_path: Path) -> None:
        """显式给出 direction 的 swipe 不是推断结果，不产生聚合警告。"""
        artifacts = ArtifactStore(tmp_path / "runs")
        generator = DcHypiumGenerator(artifacts)
        invocations = [
            _invocation(
                DcToolName.SWIPE,
                {"direction": "DOWN", "start": [100, 200], "end": [100, 500]},
                invocation_id="inv-sw-explicit",
            )
        ]
        result = generator.generate(
            session_id="dc-test-swipe-explicit",
            device_id="127.0.0.1:5555",
            invocations=invocations,
        )

        assert not any("swipe direction inferred" in warning for warning in result.warnings)
