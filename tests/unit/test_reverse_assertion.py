"""改动 2 回归：反向断言（``expects_defect`` / ``polarity="unexpected"``）。

**语义反转**是这次修复的最根本一条：普通断言的缺陷模型是「断言失败 = 发现问题」，而探索性
复现的断言是「断言**成功** = 发现问题」。真机复盘 run-20260922T141003Z-6bf8bf42 step-7 的
计划 ``expected`` 写着「点击第一个海报后该海报仍可见，页面未跳转，说明点击无反应」，断言
``passed=true`` —— agent 正确发现了缺陷，系统却把它记成一条通过的断言 ⇒
``state=completed``、``defects=[]``。

红线：反向上报是 **advisory** —— 只写 ``ActionResult.anomaly`` / ``trace.defects`` 并发
``ANOMALY_DETECTED``，绝不改变 ``result.success`` / ``trace.state`` / 断言的 ``passed``。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from harmony_test_agent.agents import AgentOrchestrator
from harmony_test_agent.agents.providers import MockAgentProvider
from harmony_test_agent.cases.builder import CaseBuilder
from harmony_test_agent.cases.spec import CheckpointKind, CheckpointSpec
from harmony_test_agent.config import Settings
from harmony_test_agent.generation.checkpoints import (
    UNEXPECTED_POLARITY_COMMENT,
    render_devicetest_checkpoint,
    render_standalone_checkpoint,
)
from harmony_test_agent.models import (
    ActionResult,
    AnomalyKind,
    AssertionResult,
    EventType,
    PlannedStep,
    RunState,
    RunTrace,
    ScreenSnapshot,
    TargetAppProfile,
    ToolName,
)
from harmony_test_agent.storage import ArtifactStore, RunRepository

BUNDLE = "com.example.neteasymusic"
EXPECTED = "点击后该海报仍可见，页面未跳转，说明点击无反应"
TARGET = "每日推荐卡片播放按钮"


class _Emitter:
    """只实现 orchestrator 真正调用的 ``emit``（不碰仓库 / 产物）。"""

    def __init__(self) -> None:
        self.events: list[tuple[EventType, str, dict]] = []

    def emit(self, event_type: EventType, message: str, payload: dict | None = None) -> None:
        self.events.append((event_type, message, payload or {}))

    def anomalies(self) -> list[dict]:
        return [payload for event_type, _message, payload in self.events if event_type is EventType.ANOMALY_DETECTED]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        agent_provider="mock",
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "home",
    )


def _orchestrator(tmp_path: Path) -> AgentOrchestrator:
    settings = _settings(tmp_path)
    return AgentOrchestrator(
        settings,
        provider=MockAgentProvider(),
        repository=RunRepository(settings.resolved_database_path),
        artifacts=ArtifactStore(settings.resolved_runtime_dir),
        device_factory=lambda _: None,
    )


def _step(*, expects_defect: bool = True) -> PlannedStep:
    return PlannedStep(
        step_id="step-7",
        instruction="点击首页第一个海报并确认是否无反应",
        tool=ToolName.ASSERT_VISIBLE,
        target=TARGET,
        expected=EXPECTED,
        expects_defect=expects_defect,
    )


def _result(*, passed: bool = True, expects_defect: bool = True) -> ActionResult:
    return ActionResult(
        step_id="step-7",
        tool=ToolName.ASSERT_VISIBLE,
        params={"target": TARGET},
        success=True,
        assertion=AssertionResult(
            kind="assert_visible",
            target=TARGET,
            passed=passed,
            message="assertion passed using UI element: 每日推荐卡片播放按钮",
            expects_defect=expects_defect,
        ),
    )


def _after(tmp_path: Path) -> ScreenSnapshot:
    return ScreenSnapshot(
        snapshot_id="snap-after",
        run_id="run-1",
        image_path=tmp_path / "runs" / "run-1" / "screens" / "step_07_after.png",
        image_sha256="digest",
        width=1320,
        height=2232,
        page_path="pages/Index",
    )


class TestConfirmedDefectFinding:
    def test_passing_reverse_assertion_records_a_defect(self, tmp_path: Path) -> None:
        orchestrator = _orchestrator(tmp_path)
        emitter = _Emitter()
        trace = RunTrace(run_id="run-1", target_app_id="netease", task="任务", device_id="SN1")
        result = _result()

        finding = orchestrator._record_confirmed_defect(trace, emitter, result, _step(), _after(tmp_path))

        assert finding is not None
        assert trace.defects == [finding]
        assert result.anomaly is finding
        assert finding.kind is AnomalyKind.NO_OP_NAVIGATION
        assert finding.severity == "warning"
        assert finding.phase == "in_run"
        assert finding.action_id == "step-7"
        assert finding.page_path == "pages/Index"
        assert EXPECTED in finding.summary_zh
        assert finding.evidence["polarity"] == "unexpected"
        assert finding.evidence["assertion_target"] == TARGET
        assert finding.evidence["target"] == TARGET  # 与运行中检测共用归并键
        events = emitter.anomalies()
        assert len(events) == 1
        assert events[0]["kind"] == "noop_navigation"

    def test_advisory_red_line_is_respected(self, tmp_path: Path) -> None:
        """不改成功、不改状态、不改断言结论 —— 缺陷只是附加结论。"""
        orchestrator = _orchestrator(tmp_path)
        trace = RunTrace(
            run_id="run-1", target_app_id="netease", task="任务", device_id="SN1", state=RunState.COMPLETED
        )
        result = _result()

        orchestrator._record_confirmed_defect(trace, _Emitter(), result, _step(), _after(tmp_path))

        assert result.success is True
        assert result.assertion is not None and result.assertion.passed is True
        assert trace.state is RunState.COMPLETED
        assert trace.agent_error is None

    def test_failed_reverse_assertion_records_nothing(self, tmp_path: Path) -> None:
        """现象没出现 ⇒ 断言失败 ⇒ 那是正常的失败，不是缺陷。"""
        orchestrator = _orchestrator(tmp_path)
        trace = RunTrace(run_id="run-1", target_app_id="netease", task="任务", device_id="SN1")
        result = _result(passed=False)

        assert orchestrator._record_confirmed_defect(trace, _Emitter(), result, _step(), _after(tmp_path)) is None
        assert trace.defects == []
        assert result.anomaly is None

    def test_plain_assertion_records_nothing(self, tmp_path: Path) -> None:
        """``expects_defect=False`` 的历史语义不变：断言通过 = 行为正常。"""
        orchestrator = _orchestrator(tmp_path)
        trace = RunTrace(run_id="run-1", target_app_id="netease", task="任务", device_id="SN1")
        result = _result(expects_defect=False)

        assert (
            orchestrator._record_confirmed_defect(
                trace, _Emitter(), result, _step(expects_defect=False), _after(tmp_path)
            )
            is None
        )
        assert trace.defects == []

    def test_action_without_assertion_records_nothing(self, tmp_path: Path) -> None:
        orchestrator = _orchestrator(tmp_path)
        trace = RunTrace(run_id="run-1", target_app_id="netease", task="任务", device_id="SN1")
        result = ActionResult(step_id="step-7", tool=ToolName.CLICK_ELEMENT, params={}, success=True)

        assert orchestrator._record_confirmed_defect(trace, _Emitter(), result, _step(), _after(tmp_path)) is None

    def test_screenshot_is_stored_as_a_run_relative_path(self, tmp_path: Path) -> None:
        orchestrator = _orchestrator(tmp_path)
        trace = RunTrace(run_id="run-1", target_app_id="netease", task="任务", device_id="SN1")

        finding = orchestrator._record_confirmed_defect(trace, _Emitter(), _result(), _step(), _after(tmp_path))

        assert finding is not None
        assert finding.screenshot == "screens/step_07_after.png"


class TestCheckpointPolarity:
    def test_unexpected_requires_a_message(self) -> None:
        with pytest.raises(ValidationError):
            CheckpointSpec(kind=CheckpointKind.ELEMENT_EXISTS, message_zh="", polarity="unexpected")

    def test_default_polarity_is_expected(self) -> None:
        checkpoint = CheckpointSpec(kind=CheckpointKind.ELEMENT_EXISTS, message_zh="应可见首页")

        assert checkpoint.polarity == "expected"
        assert checkpoint.reported_defect_kind is AnomalyKind.NO_OP_NAVIGATION  # 缺省推导，未显式给出

    def test_defect_kind_defaults_per_checkpoint_kind(self) -> None:
        assert (
            CheckpointSpec(
                kind=CheckpointKind.ELEMENT_ABSENT, message_zh="不该在却在", polarity="unexpected"
            ).reported_defect_kind
            is AnomalyKind.LAYOUT_ANOMALY
        )
        assert (
            CheckpointSpec(
                kind=CheckpointKind.ELEMENT_EXISTS,
                message_zh="点击后仍在",
                polarity="unexpected",
                defect_kind=AnomalyKind.PAGE_UNRESPONSIVE,
            ).reported_defect_kind
            is AnomalyKind.PAGE_UNRESPONSIVE
        )

    def test_json_round_trip(self) -> None:
        checkpoint = CheckpointSpec(kind=CheckpointKind.ELEMENT_EXISTS, message_zh="点击后仍在", polarity="unexpected")

        restored = CheckpointSpec.model_validate(checkpoint.model_dump(mode="json"))

        assert restored == checkpoint
        assert restored.polarity == "unexpected"

    def test_old_case_spec_without_the_new_fields_still_loads(self) -> None:
        """``extra="forbid"`` 下旧 ``case_spec.json`` 反序列化必须走默认值。"""
        legacy = {
            "kind": "element_exists",
            "message_zh": "首页应可见",
            "locator": {"kind": "text", "value": "首页", "match": "equals"},
        }

        restored = CheckpointSpec.model_validate(legacy)

        assert restored.polarity == "expected"
        assert restored.defect_kind is None


class TestBuilderMapping:
    def _trace(self, *, expects_defect: bool) -> RunTrace:
        return RunTrace(
            run_id="run-1",
            target_app_id="netease",
            task="点击首页海报并查看是否会无反应",
            device_id="SN1",
            plan=[_step(expects_defect=expects_defect)],
            actions=[_result(expects_defect=expects_defect)],
        )

    def _profile(self) -> TargetAppProfile:
        return TargetAppProfile(
            target_app_id="netease",
            display_name="网易云音乐",
            bundle_name=BUNDLE,
            main_ability="EntryAbility",
        )

    def test_expects_defect_becomes_unexpected_polarity(self) -> None:
        built = CaseBuilder().from_trace(self._trace(expects_defect=True), self._profile())
        checkpoints = [item for step in built.spec.steps for item in step.checkpoints]

        assert checkpoints and all(item.polarity == "unexpected" for item in checkpoints)
        assert EXPECTED in checkpoints[0].message_zh

    def test_plain_assertion_stays_expected(self) -> None:
        built = CaseBuilder().from_trace(self._trace(expects_defect=False), self._profile())
        checkpoints = [item for step in built.spec.steps for item in step.checkpoints]

        assert checkpoints and all(item.polarity == "expected" for item in checkpoints)

    def test_script_semantics_are_unchanged(self) -> None:
        """只有注释不同：选择器与 ``expect_exist`` 字面量逐字一致。"""
        built = CaseBuilder().from_trace(self._trace(expects_defect=True), self._profile())
        checkpoint = built.spec.steps[0].checkpoints[0]

        rendered = render_standalone_checkpoint(checkpoint)

        assert rendered[0].strip() == UNEXPECTED_POLARITY_COMMENT
        assert "expect_exist=True" in rendered[1]
        assert rendered[1].strip().startswith("driver.check_component_exist(")

    def test_expected_polarity_gets_no_comment(self) -> None:
        checkpoint = CheckpointSpec(
            kind=CheckpointKind.ELEMENT_EXISTS,
            message_zh="首页应可见",
            locator={
                "kind": "text",
                "value": "首页",
                "match": "equals",
            },  # type: ignore[arg-type]
        )

        assert UNEXPECTED_POLARITY_COMMENT not in "".join(render_standalone_checkpoint(checkpoint))
        assert UNEXPECTED_POLARITY_COMMENT not in "".join(render_devicetest_checkpoint(checkpoint))
