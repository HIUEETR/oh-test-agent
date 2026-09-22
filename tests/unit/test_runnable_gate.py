"""G3 可执行门禁：``evaluate_runnable`` 只有两条**物理**必要条件。

本文件同时钉住一条有意偏离计划原文的决策：``EntryAbility`` **不是**占位 ability。
鸿蒙工程的默认且常见 ability 名就是 ``EntryAbility``（本案真实运行
``com.github.zhuoyi233.zhplus / EntryAbility`` 即是），把它当占位会让默认路径下的真实脚本
永远不可执行，直接违背计划 G1；因此占位判定只认 bundle 哨兵与空身份。
"""

from __future__ import annotations

from harmony_test_agent.cases.builder import (
    PLACEHOLDER_ABILITY,
    PLACEHOLDER_BUNDLE,
    CaseBuilder,
    CaseBuildResult,
    evaluate_runnable,
)
from harmony_test_agent.models import (
    ActionResult,
    LocatorCandidate,
    LocatorKind,
    RunState,
    RunTrace,
    TargetAppProfile,
    ToolName,
)

REAL_BUNDLE = "com.github.zhuoyi233.zhplus"
REAL_ABILITY = "EntryAbility"
PLACEHOLDER_IDENTITY_BLOCKER = f"app identity is a placeholder ({PLACEHOLDER_BUNDLE}/{PLACEHOLDER_ABILITY})"
NO_ACTION_BLOCKER = "script has no replayable action"


def test_no_replayable_action_is_not_runnable() -> None:
    runnable, blockers = evaluate_runnable(included_actions=0, bundle_name=REAL_BUNDLE, main_ability=REAL_ABILITY)

    assert runnable is False
    assert blockers == [NO_ACTION_BLOCKER]


def test_placeholder_bundle_is_not_runnable() -> None:
    runnable, blockers = evaluate_runnable(
        included_actions=3, bundle_name=PLACEHOLDER_BUNDLE, main_ability=REAL_ABILITY
    )

    assert runnable is False
    assert blockers == [PLACEHOLDER_IDENTITY_BLOCKER]


def test_empty_identity_is_not_runnable() -> None:
    runnable, blockers = evaluate_runnable(included_actions=3, bundle_name="", main_ability="")

    assert runnable is False
    assert blockers == [PLACEHOLDER_IDENTITY_BLOCKER]


def test_placeholder_ability_with_a_real_bundle_is_runnable() -> None:
    """``EntryAbility`` 是真实 ability 名：与真实 bundle 组合时必须可执行。"""
    runnable, blockers = evaluate_runnable(
        included_actions=3, bundle_name=REAL_BUNDLE, main_ability=PLACEHOLDER_ABILITY
    )

    assert runnable is True
    assert blockers == []


def test_both_conditions_missing_reports_both_blockers() -> None:
    runnable, blockers = evaluate_runnable(
        included_actions=0, bundle_name=PLACEHOLDER_BUNDLE, main_ability=REAL_ABILITY
    )

    assert runnable is False
    assert blockers == [NO_ACTION_BLOCKER, PLACEHOLDER_IDENTITY_BLOCKER]


def test_real_identity_with_actions_is_runnable() -> None:
    runnable, blockers = evaluate_runnable(included_actions=8, bundle_name=REAL_BUNDLE, main_ability=REAL_ABILITY)

    assert runnable is True
    assert blockers == []


# ---------------------------------------------------------------------------
# 质量/验证/Profile 状态**不得**影响可执行性
# ---------------------------------------------------------------------------


def _profile() -> TargetAppProfile:
    return TargetAppProfile(
        target_app_id="com-github-zhuoyi233-zhplus",
        display_name="知乎++",
        bundle_name=REAL_BUNDLE,
        main_ability=REAL_ABILITY,
    )


def _trace(**overrides) -> RunTrace:
    defaults: dict = {
        "run_id": "run-live-mode-quality",
        "target_app_id": REAL_BUNDLE,
        "task": "搜索 OpenHarmony",
        "device_id": "device-1",
        "state": RunState.COMPLETED,
        "agent_outcome": "completed",
        "live_mode": True,
        "provisional": True,
        "actions": [
            ActionResult(
                step_id="click-search",
                tool=ToolName.CLICK_ELEMENT,
                success=True,
                params={"target": "搜索"},
                locator=LocatorCandidate(kind=LocatorKind.KEY, value="p2_home_titlebar_search"),
            ),
            ActionResult(step_id="finish", tool=ToolName.FINISH, success=True),
        ],
    }
    defaults.update(overrides)
    return RunTrace(**defaults)


def _build(**overrides) -> CaseBuildResult:
    return CaseBuilder().from_trace(_trace(**overrides), _profile())


def test_live_mode_and_provisional_do_not_block_execution() -> None:
    result = _build()

    assert result.replay_eligible is True
    assert result.runnable_blockers == []
    assert result.purpose == "acceptance"


def test_failed_actions_and_missing_finish_do_not_block_execution() -> None:
    result = _build(
        agent_outcome="failed",
        actions=[
            ActionResult(
                step_id="click-search",
                tool=ToolName.CLICK_ELEMENT,
                success=True,
                params={"target": "搜索"},
                locator=LocatorCandidate(kind=LocatorKind.KEY, value="p2_home_titlebar_search"),
            ),
            ActionResult(step_id="failed", tool=ToolName.BACK, success=False, error="device disconnected"),
        ],
    )

    assert result.replay_eligible is True
    assert result.runnable_blockers == []
    # 质量层如实记下失败：置信度降到 low。
    assert result.confidence == "low"


def test_missing_explicit_assertion_does_not_block_execution() -> None:
    result = _build()

    assert result.explicit_assertions == 0
    assert result.replay_eligible is True
    assert result.confidence == "medium"
    assert "source trace has no successful explicit assertion" in result.confidence_factors


def test_unvalidated_dynamic_locator_does_not_block_execution() -> None:
    result = _build(
        actions=[
            ActionResult(
                step_id="click-dynamic",
                tool=ToolName.CLICK_ELEMENT,
                success=True,
                params={"target": "动态卡片"},
                locator=LocatorCandidate(kind=LocatorKind.KEY, value="feed_card_20240101"),
            ),
            ActionResult(
                step_id="assert",
                tool=ToolName.ASSERT_VISIBLE,
                success=True,
                params={"target": "动态卡片"},
                locator=LocatorCandidate(kind=LocatorKind.KEY, value="feed_card_20240101"),
            ),
            ActionResult(step_id="finish", tool=ToolName.FINISH, success=True),
        ]
    )

    assert result.replay_eligible is True
    assert result.confidence == "medium"
    assert "source trace contains a dynamic locator without stable unique-prefix evidence" in (
        result.confidence_factors
    )
