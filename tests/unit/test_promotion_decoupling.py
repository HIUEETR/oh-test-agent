"""晋级层与可执行层的解耦证明。

计划核心论证：Profile 晋级**不使用任务脚本**（晋级回放读
``trace.profile_validation_generated`` 与 ``provenance.generated_script_path``），因此放开任务
脚本的执行门禁对晋级链没有影响。这里的断言把两层的关系钉成不变式：

    promotion_eligible == replay_eligible（runnable） and not provisional and not live_mode
"""

from __future__ import annotations

from harmony_test_agent.cases.builder import CaseBuilder, evaluate_runnable
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


def _profile() -> TargetAppProfile:
    return TargetAppProfile(
        target_app_id="com-github-zhuoyi233-zhplus",
        display_name="知乎++",
        bundle_name=REAL_BUNDLE,
        main_ability="EntryAbility",
    )


def _trace(*, provisional: bool = False, live_mode: bool = False, with_action: bool = True) -> RunTrace:
    actions = []
    if with_action:
        actions.append(
            ActionResult(
                step_id="click",
                tool=ToolName.CLICK_ELEMENT,
                success=True,
                params={"target": "搜索"},
                locator=LocatorCandidate(kind=LocatorKind.KEY, value="p2_home_titlebar_search"),
            )
        )
    actions.append(ActionResult(step_id="finish", tool=ToolName.FINISH, success=True))
    return RunTrace(
        run_id="run-promotion",
        target_app_id=REAL_BUNDLE,
        task="搜索 OpenHarmony",
        device_id="device-1",
        state=RunState.COMPLETED,
        agent_outcome="completed",
        provisional=provisional,
        live_mode=live_mode,
        actions=actions,
    )


def test_clean_run_is_both_runnable_and_promotion_eligible() -> None:
    result = CaseBuilder().from_trace(_trace(), _profile())

    assert result.replay_eligible is True
    assert result.promotion_eligible is True
    assert result.promotion_blockers == []


def test_provisional_run_is_runnable_but_not_promotion_evidence() -> None:
    result = CaseBuilder().from_trace(_trace(provisional=True), _profile())

    assert result.replay_eligible is True
    assert result.promotion_eligible is False
    assert result.promotion_blockers == ["provisional trace is not Profile-promotion evidence"]


def test_live_mode_run_is_runnable_but_not_promotion_evidence() -> None:
    result = CaseBuilder().from_trace(_trace(live_mode=True), _profile())

    assert result.replay_eligible is True
    assert result.promotion_eligible is False
    assert result.promotion_blockers == ["live-mode trace is not Profile-promotion evidence"]


def test_provisional_live_mode_lists_both_blockers() -> None:
    result = CaseBuilder().from_trace(_trace(provisional=True, live_mode=True), _profile())

    assert result.promotion_eligible is False
    assert result.promotion_blockers == [
        "provisional trace is not Profile-promotion evidence",
        "live-mode trace is not Profile-promotion evidence",
    ]


def test_runnable_and_promotion_are_decoupled() -> None:
    """核心解耦证明：runnable 为 True 时 promotion 仍可为 False。"""
    result = CaseBuilder().from_trace(_trace(live_mode=True), _profile())

    assert result.replay_eligible is True
    assert result.runnable_blockers == []
    assert result.promotion_eligible is False


def test_not_runnable_implies_not_promotion_eligible() -> None:
    """不可执行的脚本也拿不到晋级资格（物理必要条件不可绕过）。"""
    result = CaseBuilder().from_trace(_trace(with_action=False), _profile())

    assert result.replay_eligible is False
    assert result.promotion_eligible is False


def test_promotion_rule_matches_the_documented_formula() -> None:
    for provisional in (False, True):
        for live_mode in (False, True):
            for with_action in (False, True):
                result = CaseBuilder().from_trace(
                    _trace(provisional=provisional, live_mode=live_mode, with_action=with_action), _profile()
                )
                runnable, _ = evaluate_runnable(
                    included_actions=result.counts["generated_actions"] + result.counts["generated_assertions"],
                    bundle_name=REAL_BUNDLE,
                    main_ability="EntryAbility",
                )
                assert result.replay_eligible is runnable
                assert result.promotion_eligible is (runnable and not provisional and not live_mode)
