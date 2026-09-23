"""confidence 三档分档规则与历史文案兼容性。

``confidence`` 是**非阻断**质量层：只决定徽章与排序，绝不决定脚本能不能跑
（那是 ``evaluate_runnable``）或能不能作为 Profile 晋级证据（``promotion_eligible``）。
"""

from __future__ import annotations

import pytest

from harmony_test_agent.cases.builder import (
    LOW_CONFIDENCE_PREFIXES,
    CaseBuilder,
    confidence_factors_from_warnings,
    evaluate_confidence,
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


@pytest.mark.parametrize(
    ("factors", "outcome", "expected"),
    [
        pytest.param([], "completed", "high", id="no-factors"),
        pytest.param([], "unknown", "high", id="no-factors-unknown-outcome"),
        pytest.param(["source trace has no successful explicit assertion"], "completed", "medium", id="no-assertion"),
        pytest.param(["source trace contains unsupported replay actions"], "completed", "medium", id="unsupported"),
        pytest.param(
            ["source trace contains a dynamic locator without stable unique-prefix evidence"],
            "completed",
            "medium",
            id="dynamic-locator",
        ),
        pytest.param(["source agent outcome is stopped"], "stopped", "medium", id="stopped-outcome"),
        pytest.param(["source agent outcome is failed"], "failed", "low", id="failed-outcome"),
        pytest.param(["source trace contains failed actions"], "completed", "low", id="failed-actions-factor"),
        pytest.param(
            ["source trace does not end with a successful FINISH action"],
            "completed",
            "low",
            id="missing-finish-factor",
        ),
        pytest.param(
            ["source agent outcome is failed", "source trace contains failed actions"],
            "failed",
            "low",
            id="multiple-low-factors",
        ),
    ],
)
def test_confidence_levels(factors: list[str], outcome: str, expected: str) -> None:
    assert evaluate_confidence(factors, outcome=outcome) == expected


def test_low_confidence_prefixes_are_the_blocking_quality_signals() -> None:
    """把置信度压到 ``low`` 的因素前缀**精确**钉住。

    前三条是历史契约（源运行结局 / 失败动作 / 未以 FINISH 结束）。
    后两条是定位器修复新增的（计划 Phase 2.4 / 3.3）：日期格、时钟读数、列表实例 key
    换一天必挂；时间戳前缀在同帧匹配到多个控件时不泛化也会挂——这类脚本报 high/medium
    都是说谎，因此必须进 ``low``。
    """
    assert LOW_CONFIDENCE_PREFIXES == (
        "source agent outcome is failed",
        "source trace contains failed actions",
        "source trace does not end with a successful FINISH action",
        "script contains a locator that will not match on replay",
        "script contains a date/clock/list-instance locator that will not match on another day",
    )


def test_locator_warnings_translate_into_blocking_confidence_factors() -> None:
    """警告 → 因素的映射必须真能压到 ``low``（两条新前缀各自独立生效）。"""
    not_unique = confidence_factors_from_warnings(
        [
            "timestamp-suffixed key 'x_1790078405913' matched 2 components in 1 frame(s); "
            "prefix is not unique, retained as an exact selector that will fail on replay",
        ]
    )
    volatile = confidence_factors_from_warnings(["volatile key '1_国庆节__廿一_休' encodes a date/clock/list-instance"])

    assert not_unique == ["script contains a locator that will not match on replay"]
    assert volatile == ["script contains a date/clock/list-instance locator that will not match on another day"]
    assert evaluate_confidence(not_unique, outcome="completed") == "low"
    assert evaluate_confidence(volatile, outcome="completed") == "low"
    # 单会话泛化（前缀唯一）只是 medium：能跑，只是没有跨轮证据。
    assert (
        evaluate_confidence(
            confidence_factors_from_warnings(
                ["timestamp-suffixed key 'x_1790078405913' generalized to prefix 'x_' (unique in 1 captured frame(s))"]
            ),
            outcome="completed",
        )
        == "medium"
    )


# ---------------------------------------------------------------------------
# 文案与历史 incomplete_reasons 逐字一致
# ---------------------------------------------------------------------------

HISTORICAL_STRINGS = (
    "source agent outcome is failed",
    "source agent error: model request failed",
    "source trace contains failed actions",
    "source trace does not end with a successful FINISH action",
    "source trace has no successful explicit assertion",
    "source trace contains unsupported replay actions",
    "source trace contains a dynamic locator without stable unique-prefix evidence",
)


def _diagnostic_trace() -> RunTrace:
    """一次失败运行：命中 4 条历史质量因素。"""
    return RunTrace(
        run_id="run-diagnostic",
        target_app_id=REAL_BUNDLE,
        task="失败的搜索",
        device_id="device-1",
        state=RunState.FAILED_ACTION,
        agent_outcome="failed",
        agent_error="model request failed",
        actions=[
            ActionResult(
                step_id="click",
                tool=ToolName.CLICK_ELEMENT,
                success=True,
                params={"target": "搜索"},
                locator=LocatorCandidate(kind=LocatorKind.KEY, value="search_input"),
            ),
            ActionResult(step_id="failed", tool=ToolName.BACK, success=False, error="device disconnected"),
        ],
    )


def _profile() -> TargetAppProfile:
    return TargetAppProfile(
        target_app_id="com-github-zhuoyi233-zhplus",
        display_name="知乎++",
        bundle_name=REAL_BUNDLE,
        main_ability="EntryAbility",
    )


def test_confidence_factors_keep_the_historical_wording() -> None:
    result = CaseBuilder().from_trace(_diagnostic_trace(), _profile())

    for text in (
        "source agent outcome is failed",
        "source agent error: model request failed",
        "source trace contains failed actions",
        "source trace does not end with a successful FINISH action",
    ):
        assert text in result.confidence_factors
    # 兼容别名与 confidence_factors 完全同值。
    assert result.incomplete_reasons == result.confidence_factors
    assert result.confidence == "low"


def test_historical_strings_are_not_rewritten_anywhere() -> None:
    """``HISTORICAL_STRINGS`` 只是文案台账：逐条来自 builder 的 factors 集合。"""
    assert set(HISTORICAL_STRINGS) <= {
        "source agent outcome is failed",
        "source agent error: model request failed",
        "source trace contains failed actions",
        "source trace does not end with a successful FINISH action",
        "source trace has no successful explicit assertion",
        "source trace contains unsupported replay actions",
        "source trace contains a dynamic locator without stable unique-prefix evidence",
    }
    # provisional / live_mode 两条历史 reason 已移出质量层，见 test_promotion_decoupling.py。
    assert "provisional trace cannot qualify for acceptance replay" not in HISTORICAL_STRINGS
    assert "live-mode trace cannot qualify for acceptance replay" not in HISTORICAL_STRINGS
