"""``CaseBuilder.from_trace`` 的历史兼容契约测试（计划 A2/A3 + 必测断言表）。

覆盖：omit reason 与 ``generation/hypium.py`` 逐字节一致、``OPEN_APP`` 落到
``SetupSpec`` 的确定性 stop/start/wait、SPATIAL/VLM 运行时坐标 → ``COORDINATE``
定位器 + 警告 + ``coordinate_fallbacks``、动态 key 的 3 轮 Profile 证据通道、
``ASSERT_TEXT`` → ``TEXT_EQUALS`` 的 bug 修复、兜底断言注入的两条警告、
``counts`` / ``replay_eligible`` 与 ``HypiumGenerator`` 同源一致，以及
``incomplete_reasons`` 的文案。

测试全程离线：只构造 ``RunTrace`` / ``TargetAppProfile`` 并调用纯函数。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from harmony_test_agent.cases.builder import (
    FALLBACK_CHECKPOINT_MESSAGE,
    CaseBuilder,
    CaseBuildResult,
)
from harmony_test_agent.cases.spec import (
    CheckpointKind,
    MatchMode,
    StepAction,
)
from harmony_test_agent.generation import HypiumGenerator
from harmony_test_agent.models import (
    ActionResult,
    AssertionResult,
    BoundingBox,
    LocatorCandidate,
    LocatorKind,
    RunState,
    RunTrace,
    ScreenSnapshot,
    StableLocator,
    TargetAppProfile,
    ToolName,
    UIElement,
)
from harmony_test_agent.storage import ArtifactStore

# ---------------------------------------------------------------------------
# fixture 构造
# ---------------------------------------------------------------------------


def profile(*, inventory: list[StableLocator] | None = None) -> TargetAppProfile:
    """被测应用 Profile；``launch_strategy.wait_seconds=3`` 对应 ``STARTUP_WAIT_SECONDS = 3.0``。"""
    return TargetAppProfile(
        target_app_id="zhihu-plus",
        display_name="知乎++",
        bundle_name="com.example",
        main_ability="EntryAbility",
        launch_strategy={"wait_seconds": 3},
        stable_locator_inventory=inventory or [],
    )


def finish_action() -> ActionResult:
    return ActionResult(step_id="finish", tool=ToolName.FINISH, success=True)


def click_action(
    *,
    step_id: str = "click",
    target: str = "搜索",
    locator: LocatorCandidate | None = None,
    before_snapshot_id: str | None = None,
) -> ActionResult:
    return ActionResult(
        step_id=step_id,
        tool=ToolName.CLICK_ELEMENT,
        success=True,
        params={"target": target},
        locator=locator,
        before_snapshot_id=before_snapshot_id,
    )


def assert_visible_action(*, step_id: str = "assert", target: str = "搜索") -> ActionResult:
    return ActionResult(
        step_id=step_id,
        tool=ToolName.ASSERT_VISIBLE,
        success=True,
        params={"target": target},
        locator=LocatorCandidate(kind=LocatorKind.KEY, value="search_key"),
    )


def core_flow_trace() -> RunTrace:
    """完整合格的核心流：点击 → 显式断言 → FINISH（``replay_eligible=True``）。"""
    return RunTrace(
        run_id="run-eligible",
        target_app_id="zhihu-plus",
        task="测试搜索",
        device_id="device-1",
        state=RunState.COMPLETED,
        agent_outcome="completed",
        actions=[
            click_action(locator=LocatorCandidate(kind=LocatorKind.KEY, value="search_key")),
            assert_visible_action(),
            finish_action(),
        ],
    )


def spatial_trace() -> RunTrace:
    """SPATIAL/VLM 运行时元素：回放时必须退化为坐标点击（与 test_generation 同 fixture）。"""
    snapshot = ScreenSnapshot(
        snapshot_id="before-back",
        run_id="run-spatial",
        image_path=Path("screens/before-back.png"),
        image_sha256="abc",
        width=1320,
        height=2232,
        elements=[
            UIElement(
                element_id="ui-back",
                content="返回按钮",
                type="Button",
                bbox=BoundingBox(left=48, top=141, right=168, bottom=261),
                clickable=True,
            )
        ],
    )
    return RunTrace(
        run_id="run-spatial",
        target_app_id="zhihu-plus",
        task="返回首页",
        device_id="device-1",
        state=RunState.COMPLETED,
        agent_outcome="completed",
        snapshots=[snapshot],
        actions=[
            click_action(
                step_id="back",
                target="ui-back",
                before_snapshot_id="before-back",
                locator=LocatorCandidate(kind=LocatorKind.VLM_BBOX, value="ui-back"),
            ),
            assert_visible_action(target="返回按钮"),
            finish_action(),
        ],
    )


def diagnostic_trace() -> RunTrace:
    """桌面图标启动点击 + 失败动作：两条 omitted 记录且不可回放。"""
    snapshot = ScreenSnapshot(
        snapshot_id="desktop",
        run_id="run-diagnostic",
        image_path=Path("screens/desktop.png"),
        image_sha256="abc",
        width=100,
        height=200,
        elements=[UIElement(element_id="target-icon", type="AppIcon", clickable=True)],
    )
    return RunTrace(
        run_id="run-diagnostic",
        target_app_id="zhihu-plus",
        task="启动后失败",
        device_id="device-1",
        agent_outcome="failed",
        snapshots=[snapshot],
        actions=[
            click_action(step_id="desktop-launch", target="target-icon", before_snapshot_id="desktop"),
            ActionResult(step_id="failed", tool=ToolName.BACK, success=False, error="device disconnected"),
        ],
        error="device disconnected",
    )


def dynamic_key_trace() -> RunTrace:
    """动态 key（``feed_card_<8+ 位数字>``）的点击 + 稳定断言 + FINISH。"""
    return RunTrace(
        run_id="run-dynamic",
        target_app_id="zhihu-plus",
        task="动态 key 泛化",
        device_id="device-1",
        state=RunState.COMPLETED,
        agent_outcome="completed",
        actions=[
            click_action(
                step_id="click-dynamic",
                target="卡片",
                locator=LocatorCandidate(kind=LocatorKind.KEY, value="feed_card_20240101"),
            ),
            assert_visible_action(),
            finish_action(),
        ],
    )


def stable_locator(**overrides: Any) -> StableLocator:
    data: dict[str, Any] = {
        "name": "feed-card",
        "key": "feed_card_20240101",
        "dynamic_pattern": "feed_card_",
        "page_signature": "home",
        "observed_rounds": 3,
        "unique_match_rounds": 3,
    }
    data.update(overrides)
    return StableLocator(**data)


def assert_text_trace(
    *,
    locator: LocatorCandidate | None,
    params: dict[str, Any],
) -> RunTrace:
    return RunTrace(
        run_id="run-assert-text",
        target_app_id="zhihu-plus",
        task="断言文本",
        device_id="device-1",
        state=RunState.COMPLETED,
        agent_outcome="completed",
        actions=[
            click_action(locator=LocatorCandidate(kind=LocatorKind.KEY, value="search_input")),
            ActionResult(
                step_id="assert-text", tool=ToolName.ASSERT_TEXT, success=True, params=params, locator=locator
            ),
            finish_action(),
        ],
    )


def fallback_trace() -> RunTrace:
    """终态完成但全程没有显式断言：必须注入兜底断言。"""
    snapshot = ScreenSnapshot(
        snapshot_id="stable",
        run_id="run-no-assertion",
        image_path=Path("screens/stable.png"),
        image_sha256="abc",
        width=100,
        height=200,
        elements=[UIElement(element_id="stable-title", key="stable_title", content="首页")],
    )
    return RunTrace(
        run_id="run-no-assertion",
        target_app_id="zhihu-plus",
        task="没有显式断言",
        device_id="device-1",
        state=RunState.COMPLETED,
        agent_outcome="completed",
        snapshots=[snapshot],
        actions=[finish_action()],
    )


def unknown_tool_action() -> SimpleNamespace:
    """伪造一个 ``ToolName`` 尚未收录的工具动作（历史 trace 里可能出现的未来工具）。

    ``ToolName`` 已覆盖全部现行工具，因此「unsupported replay tool」分支只能用
    非枚举值的轻量对象触发；``RunTrace`` 未开启 ``validate_assignment``，可直接替换。
    """
    return SimpleNamespace(
        step_id="legacy",
        tool="future_tool",
        success=True,
        params={},
        locator=None,
        before_snapshot_id=None,
        error=None,
    )


def omit_reason(result: CaseBuildResult, step_id: str) -> str:
    return next(item["reason"] for item in result.omitted_actions if item["step_id"] == step_id)


def only_checkpoint(result: CaseBuildResult, step_index: int = 0) -> Any:
    return result.spec.steps[step_index].checkpoints[0]


# ---------------------------------------------------------------------------
# omit reason：与历史实现逐字节一致
# ---------------------------------------------------------------------------


def test_open_app_omit_reason_is_byte_identical_and_lands_in_setup() -> None:
    """OPEN_APP 被省略，但确定性 stop/start/wait 必须落到 ``SetupSpec``。"""
    trace = core_flow_trace()
    trace.actions.insert(0, ActionResult(step_id="open", tool=ToolName.OPEN_APP, success=True))

    result = CaseBuilder().from_trace(trace, profile())

    assert result.omitted_actions[0] == {
        "step_id": "open",
        "tool": "open_app",
        "reason": "OPEN_APP is replaced by deterministic stop/start/wait setup",
    }
    # 确定性 stop/start/wait 由 SetupSpec 承载，而不是脚本体里的动作步骤。
    assert result.spec.setup.stop_app_first is True
    assert result.spec.setup.start_app is True
    assert result.spec.setup.startup_wait_seconds == 3.0
    assert all(step.action != StepAction.START_APP for step in result.spec.steps)


def test_agent_control_action_omit_reasons_are_byte_identical() -> None:
    """INSPECT_SCREEN / FINISH 是 Agent 控制动作，不进入回放脚本体。"""
    trace = core_flow_trace()
    trace.actions.insert(0, ActionResult(step_id="inspect", tool=ToolName.INSPECT_SCREEN, success=True))

    result = CaseBuilder().from_trace(trace, profile())

    assert {item["step_id"]: item["reason"] for item in result.omitted_actions} == {
        "inspect": "inspect_screen is an agent-control action",
        "finish": "finish is an agent-control action",
    }


def test_desktop_appicon_click_is_omitted() -> None:
    """桌面图标启动点击不进脚本体，且以 ``click_element`` 记入 omitted。"""
    result = CaseBuilder().from_trace(diagnostic_trace(), profile())

    assert result.omitted_actions[0]["step_id"] == "desktop-launch"
    assert result.omitted_actions[0]["tool"] == "click_element"
    assert result.omitted_actions[0]["reason"].startswith("desktop AppIcon")
    assert not any("target-icon" in (step.locator.value if step.locator else "") for step in result.spec.steps)


def test_desktop_appicon_click_omit_reason_is_byte_identical() -> None:
    """``hypium.py`` 历史文案逐字节一致（含 "launch"）。"""
    result = CaseBuilder().from_trace(diagnostic_trace(), profile())

    assert result.omitted_actions[0] == {
        "step_id": "desktop-launch",
        "tool": "click_element",
        "reason": "desktop AppIcon launch click is replaced by deterministic app setup",
    }


def test_source_action_failure_omit_reason_prefers_action_error() -> None:
    """``action.error or "source action failed"``：有错误用错误，没有则用历史兜底文案。"""
    trace = core_flow_trace()
    trace.actions.insert(
        1,
        ActionResult(step_id="failed", tool=ToolName.BACK, success=False, error="device disconnected"),
    )
    trace.actions.insert(2, ActionResult(step_id="failed-silent", tool=ToolName.BACK, success=False))

    result = CaseBuilder().from_trace(trace, profile())

    assert omit_reason(result, "failed") == "device disconnected"
    assert omit_reason(result, "failed-silent") == "source action failed"


def test_unsupported_replay_tool_omit_reason_is_byte_identical() -> None:
    trace = core_flow_trace()
    trace.actions = [trace.actions[0], unknown_tool_action(), trace.actions[1], trace.actions[2]]

    result = CaseBuilder().from_trace(trace, profile())

    assert omit_reason(result, "legacy") == "unsupported replay tool: future_tool"
    assert "source trace contains unsupported replay actions" in result.confidence_factors
    # 不支持的动作只降置信度（medium），不再阻断执行：脚本里的其余动作照样可回放。
    assert result.replay_eligible is True
    assert result.confidence == "medium"


# ---------------------------------------------------------------------------
# SPATIAL/VLM → 坐标兜底
# ---------------------------------------------------------------------------


def test_spatial_runtime_element_becomes_coordinate_locator_with_bound_and_warning() -> None:
    result = CaseBuilder().from_trace(spatial_trace(), profile())

    step = result.spec.steps[0]
    expected_warning = "back: runtime element 'ui-back' uses coordinate (108, 201)"

    assert step.action == StepAction.CLICK
    assert step.coordinate == (108, 201)
    assert step.locator is not None
    assert step.locator.kind == LocatorKind.COORDINATE
    assert step.locator.coordinate == (108, 201)
    assert step.locator.resolution_bound == (1320, 2232)
    assert step.locator.warning == expected_warning
    assert step.locator.target_label == "ui-back"
    assert expected_warning in result.warnings
    assert result.counts["coordinate_fallbacks"] == 1


# ---------------------------------------------------------------------------
# 动态 key：Profile 证据通道
# ---------------------------------------------------------------------------


def test_dynamic_key_with_three_round_profile_evidence_generalizes_to_starts_with() -> None:
    result = CaseBuilder().from_trace(
        dynamic_key_trace(),
        profile(inventory=[stable_locator()]),
    )

    locator = result.spec.steps[0].locator
    assert locator is not None
    assert locator.kind == LocatorKind.KEY
    assert locator.value == "feed_card_"
    assert locator.match == MatchMode.STARTS_WITH
    assert locator.evidence is not None
    assert locator.evidence.source == "profile_stable_locator"
    assert locator.evidence.observed_rounds == 3
    assert locator.evidence.unique_match_rounds == 3
    assert locator.evidence.dynamic_pattern == "feed_card_"
    assert "validated dynamic key 'feed_card_20240101' generalized to unique prefix 'feed_card_'" in result.warnings
    assert "source trace contains a dynamic locator without stable unique-prefix evidence" not in (
        result.incomplete_reasons
    )


@pytest.mark.parametrize(
    "inventory",
    [
        pytest.param([], id="no-inventory"),
        pytest.param([stable_locator(observed_rounds=1, unique_match_rounds=1)], id="only-one-round"),
        pytest.param([stable_locator(observed_rounds=3, unique_match_rounds=2)], id="not-unique-enough"),
    ],
)
def test_dynamic_key_without_stable_evidence_keeps_exact_value_and_feeds_incomplete_reasons(
    inventory: list[StableLocator],
) -> None:
    result = CaseBuilder().from_trace(dynamic_key_trace(), profile(inventory=inventory))

    locator = result.spec.steps[0].locator
    assert locator is not None
    assert locator.kind == LocatorKind.KEY
    # 无稳定前缀证据时保留精确值；此时即使精确值恰好命中某个 StableLocator，
    # 也只是 ``_exact_locator_evidence`` 的证据，不构成前缀泛化许可。
    assert locator.value == "feed_card_20240101"
    assert locator.match == MatchMode.EQUALS
    assert "unvalidated dynamic key 'feed_card_20240101' retained as an exact diagnostic selector" in result.warnings
    assert "source trace contains a dynamic locator without stable unique-prefix evidence" in (
        result.confidence_factors
    )
    # 未验证动态定位器只把置信度降到 medium：脚本立即可执行，风险以质量提示的形式保留。
    assert result.replay_eligible is True
    assert result.purpose == "acceptance"
    assert result.confidence == "medium"


# ---------------------------------------------------------------------------
# 连字符 + 毫秒时间戳型动态 key（真机回放失败复盘）
# ---------------------------------------------------------------------------


def hyphen_dynamic_key_trace() -> RunTrace:
    """真机实例 key：``add_agenda_title-<13 位毫秒>``（见 run-20260921T053514Z-8418044b）。"""
    return RunTrace(
        run_id="run-hyphen-dynamic",
        target_app_id="com-huawei-hmos-calendar",
        task="新建日程并填写标题",
        device_id="device-1",
        state=RunState.COMPLETED,
        agent_outcome="completed",
        actions=[
            click_action(
                step_id="click-title",
                target="标题",
                locator=LocatorCandidate(kind=LocatorKind.KEY, value="add_agenda_title-1789969034729"),
            ),
            assert_visible_action(),
            finish_action(),
        ],
    )


def test_hyphen_dynamic_key_generalizes_with_harvested_prefix_evidence() -> None:
    """任务期回收把毫秒 key 记成 ``name-#`` 前缀模式时，脚本必须用 starts_with 前缀。"""
    harvested = StableLocator(
        name="add_agenda_title",
        key="add_agenda_title-1789969034729",
        dynamic_pattern="add_agenda_title-#",
        page_signature="editor",
        observed_rounds=1,
        unique_match_rounds=1,
        source="live_task_harvest",
    )

    result = CaseBuilder(min_observed_rounds=1).from_trace(
        hyphen_dynamic_key_trace(),
        profile(inventory=[harvested]),
    )

    locator = result.spec.steps[0].locator
    assert locator is not None
    assert locator.value == "add_agenda_title-"
    assert locator.match == MatchMode.STARTS_WITH
    assert locator.evidence is not None and locator.evidence.source == "profile_stable_locator"
    assert "add_agenda_title-1789969034729" in " ".join(result.warnings)


def test_hyphen_dynamic_key_without_evidence_is_flagged_as_dynamic() -> None:
    """没有前缀证据时也必须识别为动态 key（回归：旧正则漏判导致脚本写死毫秒 key）。"""
    result = CaseBuilder(min_observed_rounds=1).from_trace(hyphen_dynamic_key_trace(), profile(inventory=[]))

    locator = result.spec.steps[0].locator
    assert locator is not None
    assert locator.match == MatchMode.EQUALS
    assert any("unvalidated dynamic key" in item for item in result.warnings)
    assert "source trace contains a dynamic locator without stable unique-prefix evidence" in (
        result.incomplete_reasons
    )


# ---------------------------------------------------------------------------
# 无 key/id 元素的空间回退：按宿主容器恢复 key
# ---------------------------------------------------------------------------


def test_spatial_fallback_recovers_key_from_host_container() -> None:
    """真机复盘：确认按钮自身无 key，宿主容器 ``add_agenda_comfrim`` 有 key → 用 key 选择器。"""
    snapshot = ScreenSnapshot(
        snapshot_id="editor",
        run_id="run-host-key",
        image_path=Path("editor.png"),
        image_sha256="hash",
        width=1320,
        height=2232,
        elements=[
            UIElement(
                element_id="ui-confirm-host",
                key="add_agenda_comfrim",
                type="__Common__",
                bbox=BoundingBox(left=1152, top=177, right=1272, bottom=297),
            ),
            UIElement(
                element_id="ui-confirm-button",
                type="Button",
                clickable=True,
                bbox=BoundingBox(left=1152, top=177, right=1272, bottom=297),
            ),
        ],
    )
    trace = RunTrace(
        run_id="run-host-key",
        target_app_id="com-huawei-hmos-calendar",
        task="保存日程",
        device_id="device-1",
        snapshots=[snapshot],
        actions=[
            ActionResult(
                step_id="confirm",
                tool=ToolName.CLICK_ELEMENT,
                success=True,
                params={"target": "ui-confirm-button"},
                before_snapshot_id="editor",
                locator=LocatorCandidate(kind=LocatorKind.SPATIAL, value="ui-confirm-button"),
            ),
            assert_visible_action(),
            finish_action(),
        ],
    )

    result = CaseBuilder(min_observed_rounds=1).from_trace(trace, profile(inventory=[]))

    locator = result.spec.steps[0].locator
    assert locator is not None
    assert locator.kind == LocatorKind.KEY
    assert locator.value == "add_agenda_comfrim"
    assert result.counts["coordinate_fallbacks"] == 0
    assert any("runtime locator recovered as" in item for item in result.warnings)


def test_host_container_recovery_prefers_the_topmost_owner() -> None:
    """弹层确认按钮与背景页 more_menu 的 bbox 完全一致：取层级更靠后的那个（弹层在上面）。"""
    snapshot = ScreenSnapshot(
        snapshot_id="sheet",
        run_id="run-topmost",
        image_path=Path("sheet.png"),
        image_sha256="hash",
        width=1320,
        height=2232,
        elements=[
            UIElement(
                element_id="ui-more-menu",
                key="more_menu",
                type="Button",
                bbox=BoundingBox(left=1152, top=177, right=1272, bottom=297),
            ),
            UIElement(
                element_id="ui-bare-icon",
                type="Button",
                clickable=True,
                bbox=BoundingBox(left=1152, top=177, right=1272, bottom=297),
            ),
            UIElement(
                element_id="ui-confirm-host",
                key="add_agenda_comfrim",
                type="__Common__",
                bbox=BoundingBox(left=1152, top=177, right=1272, bottom=297),
            ),
        ],
    )
    trace = RunTrace(
        run_id="run-topmost",
        target_app_id="com-huawei-hmos-calendar",
        task="保存日程",
        device_id="device-1",
        snapshots=[snapshot],
        actions=[
            ActionResult(
                step_id="confirm",
                tool=ToolName.CLICK_ELEMENT,
                success=True,
                params={"target": "ui-bare-icon"},
                before_snapshot_id="sheet",
                locator=LocatorCandidate(kind=LocatorKind.SPATIAL, value="ui-bare-icon"),
            ),
            assert_visible_action(),
            finish_action(),
        ],
    )

    result = CaseBuilder(min_observed_rounds=1).from_trace(trace, profile(inventory=[]))

    locator = result.spec.steps[0].locator
    assert locator is not None
    assert locator.value == "add_agenda_comfrim"


def test_spatial_fallback_without_host_key_keeps_coordinate() -> None:
    """没有可归属的带 key 宿主时保持坐标兜底语义不变。"""
    snapshot = ScreenSnapshot(
        snapshot_id="editor",
        run_id="run-no-host",
        image_path=Path("editor.png"),
        image_sha256="hash",
        width=1320,
        height=2232,
        elements=[
            UIElement(
                element_id="ui-bare-button",
                type="Button",
                clickable=True,
                bbox=BoundingBox(left=1152, top=177, right=1272, bottom=297),
            )
        ],
    )
    trace = RunTrace(
        run_id="run-no-host",
        target_app_id="com-huawei-hmos-calendar",
        task="保存日程",
        device_id="device-1",
        snapshots=[snapshot],
        actions=[
            ActionResult(
                step_id="confirm",
                tool=ToolName.CLICK_ELEMENT,
                success=True,
                params={"target": "ui-bare-button"},
                before_snapshot_id="editor",
                locator=LocatorCandidate(kind=LocatorKind.SPATIAL, value="ui-bare-button"),
            ),
            assert_visible_action(),
            finish_action(),
        ],
    )

    result = CaseBuilder(min_observed_rounds=1).from_trace(trace, profile(inventory=[]))

    locator = result.spec.steps[0].locator
    assert locator is not None
    assert locator.kind == LocatorKind.COORDINATE
    assert result.counts["coordinate_fallbacks"] == 1


# ---------------------------------------------------------------------------
# assert_text：检查点必须落在真正持有文本的元素上
# ---------------------------------------------------------------------------


def test_text_assertion_retargets_from_container_to_text_holder() -> None:
    """真机复盘：``assert_text`` 的运行时定位器是容器 key，容器 text 为空 → 必须改用文本持有者。"""
    snapshot = ScreenSnapshot(
        snapshot_id="search",
        run_id="run-text-holder",
        image_path=Path("search.png"),
        image_sha256="hash",
        width=1222,
        height=2670,
        elements=[
            UIElement(
                element_id="ui-container",
                key="p2_search_input_container",
                type="Row",
                bbox=BoundingBox(left=60, top=160, right=1160, bottom=250),
            ),
            UIElement(
                element_id="ui-input",
                key="p2_search_input",
                content="OpenHarmony",
                type="TextInput",
                editable=True,
                bbox=BoundingBox(left=80, top=170, right=1100, bottom=240),
            ),
        ],
    )
    trace = RunTrace(
        run_id="run-text-holder",
        target_app_id="zhihu-plus",
        task="搜索 OpenHarmony",
        device_id="device-1",
        snapshots=[snapshot],
        actions=[
            ActionResult(
                step_id="assert-search-text",
                tool=ToolName.ASSERT_TEXT,
                success=True,
                params={"target": "p2_search_input", "text": "OpenHarmony"},
                before_snapshot_id="search",
                after_snapshot_id="search",
                locator=LocatorCandidate(kind=LocatorKind.KEY, value="p2_search_input_container"),
                assertion=AssertionResult(
                    kind=ToolName.ASSERT_TEXT, target="p2_search_input", passed=True, message="ok"
                ),
            ),
            finish_action(),
        ],
    )

    result = CaseBuilder(min_observed_rounds=1).from_trace(trace, profile(inventory=[]))

    checkpoint = only_checkpoint(result)
    assert checkpoint.kind == CheckpointKind.TEXT_EQUALS
    assert checkpoint.expected == "OpenHarmony"
    assert checkpoint.locator is not None
    assert checkpoint.locator.value == "p2_search_input"
    assert any("text assertion re-targeted" in item for item in result.warnings)


def test_text_assertion_keeps_locator_when_it_already_holds_the_text() -> None:
    """定位器本来就指向文本持有者时不动它（不得引入额外改写）。"""
    snapshot = ScreenSnapshot(
        snapshot_id="search",
        run_id="run-text-holder-2",
        image_path=Path("search.png"),
        image_sha256="hash",
        width=1222,
        height=2670,
        elements=[
            UIElement(
                element_id="ui-input",
                key="p2_search_input",
                content="OpenHarmony",
                type="TextInput",
                editable=True,
                bbox=BoundingBox(left=80, top=170, right=1100, bottom=240),
            )
        ],
    )
    trace = RunTrace(
        run_id="run-text-holder-2",
        target_app_id="zhihu-plus",
        task="搜索 OpenHarmony",
        device_id="device-1",
        snapshots=[snapshot],
        actions=[
            ActionResult(
                step_id="assert-search-text",
                tool=ToolName.ASSERT_TEXT,
                success=True,
                params={"target": "p2_search_input", "text": "OpenHarmony"},
                before_snapshot_id="search",
                after_snapshot_id="search",
                locator=LocatorCandidate(kind=LocatorKind.KEY, value="p2_search_input"),
                assertion=AssertionResult(
                    kind=ToolName.ASSERT_TEXT, target="p2_search_input", passed=True, message="ok"
                ),
            ),
            finish_action(),
        ],
    )

    result = CaseBuilder(min_observed_rounds=1).from_trace(trace, profile(inventory=[]))

    checkpoint = only_checkpoint(result)
    assert checkpoint.locator is not None
    assert checkpoint.locator.value == "p2_search_input"
    assert not any("text assertion re-targeted" in item for item in result.warnings)


# ---------------------------------------------------------------------------
# ASSERT_TEXT 修复：TEXT_EQUALS + 期望文本保留
# ---------------------------------------------------------------------------


def test_assert_text_maps_to_text_equals_and_keeps_key_locator() -> None:
    trace = assert_text_trace(
        locator=LocatorCandidate(kind=LocatorKind.KEY, value="search_input"),
        params={"target": "搜索框", "text": "OpenHarmony"},
    )

    result = CaseBuilder().from_trace(trace, profile())

    checkpoint = only_checkpoint(result)
    assert checkpoint.kind == CheckpointKind.TEXT_EQUALS
    assert checkpoint.expected == "OpenHarmony"
    assert checkpoint.locator is not None
    assert checkpoint.locator.kind == LocatorKind.KEY
    assert checkpoint.locator.value == "search_input"
    assert result.explicit_assertions == 1


def test_assert_text_prefers_text_param_over_target() -> None:
    """与运行时 ``runtime/tools.py`` 的优先级一致：``text`` 优先于 ``target``。"""
    trace = assert_text_trace(
        locator=LocatorCandidate(kind=LocatorKind.KEY, value="search_input"),
        params={"target": "回退文本", "text": "期望文本"},
    )

    checkpoint = only_checkpoint(CaseBuilder().from_trace(trace, profile()))

    assert checkpoint.kind == CheckpointKind.TEXT_EQUALS
    assert checkpoint.expected == "期望文本"


def test_assert_text_without_structured_locator_becomes_fuzzy_text_contains() -> None:
    """没有 KEY/ID 定位器时用包含匹配（对齐运行时 ``target_variants`` 模糊匹配）。"""
    trace = assert_text_trace(locator=None, params={"target": "OpenHarmony"})

    result = CaseBuilder().from_trace(trace, profile())

    checkpoint = only_checkpoint(result)
    assert checkpoint.kind == CheckpointKind.TEXT_CONTAINS
    assert checkpoint.expected == "OpenHarmony"
    assert checkpoint.locator is None


def test_assert_text_with_id_locator_keeps_id_locator() -> None:
    trace = assert_text_trace(
        locator=LocatorCandidate(kind=LocatorKind.ID, value="p2_search_input"),
        params={"text": "OpenHarmony"},
    )

    checkpoint = only_checkpoint(CaseBuilder().from_trace(trace, profile()))

    assert checkpoint.locator is not None
    assert checkpoint.locator.kind == LocatorKind.ID
    assert checkpoint.locator.value == "p2_search_input"


def test_assertion_without_preceding_step_creates_a_check_step() -> None:
    """断言没有前置步骤时新建纯 ``CHECK`` 步骤，而不是丢弃检查点。"""
    trace = RunTrace(
        run_id="run-leading-assert",
        target_app_id="zhihu-plus",
        task="只有断言",
        device_id="device-1",
        state=RunState.COMPLETED,
        agent_outcome="completed",
        actions=[assert_visible_action(), finish_action()],
    )

    result = CaseBuilder().from_trace(trace, profile())

    assert [step.action for step in result.spec.steps] == [StepAction.CHECK]
    assert only_checkpoint(result).kind == CheckpointKind.ELEMENT_EXISTS


# ---------------------------------------------------------------------------
# 兜底断言注入
# ---------------------------------------------------------------------------


def test_fallback_assertion_injection_adds_check_step_and_two_warnings() -> None:
    result = CaseBuilder().from_trace(fallback_trace(), profile())

    assert result.explicit_assertions == 0
    assert result.counts["generated_assertions"] == 1
    assert [step.action for step in result.spec.steps] == [StepAction.CHECK]
    fallback = result.spec.steps[-1]
    assert fallback.step_id == "fallback-assertion"
    assert fallback.locator is not None
    assert fallback.locator.kind == LocatorKind.KEY
    assert fallback.locator.value == "stable_title"
    assert fallback.checkpoints[0].kind == CheckpointKind.ELEMENT_EXISTS
    assert fallback.checkpoints[0].message_zh == FALLBACK_CHECKPOINT_MESSAGE
    assert "source trace has no successful explicit assertion" in result.warnings
    assert "generated a fallback assertion from an observed stable locator" in result.warnings
    # 兜底断言是生成物，不能把「无显式断言」的用例置信度洗成 high；脚本本身仍可执行。
    assert result.replay_eligible is True
    assert result.confidence == "medium"
    assert "source trace has no successful explicit assertion" in result.confidence_factors


def test_fallback_assertion_is_not_injected_when_an_explicit_assertion_exists() -> None:
    result = CaseBuilder().from_trace(core_flow_trace(), profile())

    assert "fallback-assertion" not in {step.step_id for step in result.spec.steps}
    assert "generated a fallback assertion from an observed stable locator" not in result.warnings


def test_fallback_assertion_injection_can_be_disabled() -> None:
    """``inject_fallback_assertion=False``（DC 生成器使用）时既不注入步骤也不写两条警告。"""
    result = CaseBuilder(inject_fallback_assertion=False).from_trace(fallback_trace(), profile())

    assert result.explicit_assertions == 0
    assert "fallback-assertion" not in {step.step_id for step in result.spec.steps}
    assert "source trace has no successful explicit assertion" not in result.warnings
    assert "generated a fallback assertion from an observed stable locator" not in result.warnings
    assert result.counts["generated_assertions"] == 0
    # 没有兜底断言时脚本体为空，IR 不变式仍要求至少一步（空注释步骤 → emitter 发裸 pass）。
    assert [step.action for step in result.spec.steps] == [StepAction.NOOP_COMMENT]
    # 没有可回放动作 ⇒ 命中 G3 的第一条物理必要条件，不可执行。
    assert "source trace has no successful explicit assertion" in result.confidence_factors
    assert result.replay_eligible is False
    assert result.runnable_blockers == ["script has no replayable action"]


# ---------------------------------------------------------------------------
# 与 HypiumGenerator 同源一致
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(core_flow_trace, id="eligible-core-flow"),
        pytest.param(spatial_trace, id="coordinate-fallback"),
        pytest.param(diagnostic_trace, id="diagnostic"),
        pytest.param(fallback_trace, id="fallback-assertion"),
    ],
)
def test_counts_and_replay_eligible_match_hypium_generator(tmp_path: Path, factory: Any) -> None:
    """同一 fixture：``CaseBuilder().from_trace`` 与 ``HypiumGenerator.build`` 必须完全一致。"""
    trace = factory()
    store = ArtifactStore(tmp_path / "runs")

    built = CaseBuilder().from_trace(trace, profile())
    via_generator = HypiumGenerator(store).build(trace, profile())

    assert built.counts == via_generator.counts
    assert built.replay_eligible == via_generator.replay_eligible
    assert built.purpose == via_generator.purpose
    assert built.incomplete_reasons == via_generator.incomplete_reasons
    assert built.confidence == via_generator.confidence
    assert built.confidence_factors == via_generator.confidence_factors
    assert built.promotion_eligible == via_generator.promotion_eligible
    assert built.omitted_actions == via_generator.omitted_actions


def test_hypium_generator_build_goes_through_the_case_ir(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "runs")

    built = HypiumGenerator(store).build(core_flow_trace(), profile())

    assert built.spec.case_id.startswith("case-")
    assert built.counts["generated_actions"] == 1
    assert built.replay_eligible is True


# ---------------------------------------------------------------------------
# confidence / 晋级资格
# ---------------------------------------------------------------------------


def test_confidence_factors_keep_historical_strings() -> None:
    result = CaseBuilder().from_trace(diagnostic_trace(), profile())

    assert "source agent outcome is failed" in result.confidence_factors
    assert "source trace contains failed actions" in result.confidence_factors
    assert "source trace does not end with a successful FINISH action" in result.confidence_factors
    assert "source agent error: None" not in result.confidence_factors
    # 兼容别名与 confidence_factors 同值。
    assert result.incomplete_reasons == result.confidence_factors


def test_confidence_factors_report_agent_error_and_provisional_goes_to_promotion() -> None:
    trace = diagnostic_trace()
    trace.agent_error = "model request failed"
    trace.provisional = True

    result = CaseBuilder().from_trace(trace, profile())

    assert "source agent error: model request failed" in result.confidence_factors
    assert result.confidence == "low"
    # provisional 不再进质量层：它只决定能否作为 Profile 晋级证据。
    assert "provisional trace cannot qualify for acceptance replay" not in result.confidence_factors
    assert "provisional trace is not Profile-promotion evidence" in result.promotion_blockers
    assert result.promotion_eligible is False
    # 这条轨迹同时没有任何可回放动作（两条动作都被 omit），因此不可执行；
    # 晋级资格与可执行性是两个独立判定，见 test_promotion_decoupling.py。
    assert result.runnable_blockers == ["script has no replayable action"]


def test_fully_successful_trace_has_no_incomplete_reasons() -> None:
    result = CaseBuilder().from_trace(core_flow_trace(), profile())

    assert result.incomplete_reasons == []
    assert result.confidence_factors == []
    assert result.confidence == "high"
    assert result.replay_eligible is True
    assert result.runnable_blockers == []
    assert result.promotion_eligible is True
    assert result.purpose == "acceptance"
    assert result.source_agent_outcome == "completed"
