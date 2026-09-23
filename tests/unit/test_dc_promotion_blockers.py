"""DC 录制的 Profile 晋级资格（计划 Phase 6 / R6）。

历史行为：``from_dc_invocations`` 无条件给 ``promotion_eligible = replay_eligible``，
于是既无跨轮 Profile 验证、也无跨会话证据的临时录制脚本被标成
「可作 Profile 晋级证据」。这与「能不能跑」（``replay_eligible``）是**两层不同的判断**：
本文件把两层解耦钉死。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harmony_test_agent.cases.builder import UNGROUNDED_ASSERTION_FACTOR, CaseBuilder
from harmony_test_agent.dc.models import DcToolInvocation, DcToolName, DcToolTier
from harmony_test_agent.models import (
    AssertionDefinition,
    LocatorKind,
    ProfileStatus,
    StableLocator,
    TargetAppProfile,
    UIElement,
)

BUNDLE = "com.huawei.hmos.calendar"
BLOCKER = "dc recording has no cross-round locator evidence"


def invocation() -> DcToolInvocation:
    """可回放的一条点击（带稳定 key），足够让脚本 ``replay_eligible``。"""
    return DcToolInvocation(
        invocation_id="inv-1",
        turn_id="turn-1",
        tool=DcToolName.CLICK,
        tier=DcToolTier.L1,
        args={"x": 10, "y": 20},
        success=True,
        resolved_element=UIElement(element_id="ui-1", key="add_agenda_comfrim", content="确定"),
    )


def profile(status: ProfileStatus) -> TargetAppProfile:
    """被测 Profile。

    ``VERIFIED`` 不是随便贴的标签：``TargetAppProfile`` 的不变式要求「已验证时间 +
    回放 ID + 设备验证通过证据 + 三页定位器 + 两条应用断言」齐备，因此这里按真实
    晋级产物构造（与 ``tests/api/test_api_contract.py`` 的 verified Profile 同构）。
    """
    payload: dict = {
        "target_app_id": "com-huawei-hmos-calendar",
        "display_name": "日历",
        "bundle_name": BUNDLE,
        "main_ability": "MainAbility",
        "launch_strategy": {"wait_seconds": 2},
        "status": status,
    }
    if status is ProfileStatus.VERIFIED:
        rounds = [f"snapshot-round-{index}" for index in range(1, 4)]
        payload["stable_locator_inventory"] = [
            StableLocator(
                name=f"page-{index}",
                page_signature=f"page-{index}",
                key=f"page-key-{index}",
                observed_rounds=3,
                unique_match_rounds=3,
                evidence_snapshot_ids=rounds,
            )
            for index in range(1, 4)
        ]
        payload["assertion_inventory"] = [
            AssertionDefinition(
                name=f"assertion-{index}",
                kind="visible",
                target=f"page-key-{index}",
                page_signature=f"page-{index}",
                observed_rounds=3,
                evidence_snapshot_ids=rounds,
            )
            for index in range(1, 3)
        ]
        payload["provenance"] = {
            "verified_at": "2026-09-21T00:00:00Z",
            "hypium_replay_run_ids": ["run-verified-1"],
            "evidence": {"verification_passed": True},
        }
    return TargetAppProfile(**payload)


def build(**kwargs) -> object:
    return CaseBuilder().from_dc_invocations(
        "dc-session-001",
        "127.0.0.1:5555",
        [invocation()],
        bundle_name=BUNDLE,
        main_ability="MainAbility",
        **kwargs,
    )


def test_dc_recording_without_a_profile_is_not_promotion_evidence() -> None:
    result = build()

    assert result.replay_eligible is True  # 能跑
    assert result.promotion_eligible is False  # 但不能当晋级证据
    assert result.promotion_blockers == [BLOCKER]
    assert result.purpose == "acceptance"


@pytest.mark.parametrize("status", [ProfileStatus.VERIFIED, ProfileStatus.CANDIDATE])
def test_dc_recording_with_cross_round_evidence_is_promotion_eligible(status: ProfileStatus) -> None:
    result = build(profile=profile(status))

    assert result.promotion_eligible is True
    assert result.promotion_blockers == []


@pytest.mark.parametrize(
    "status",
    [ProfileStatus.DRAFT, ProfileStatus.SUPERSEDED, ProfileStatus.INVALID, ProfileStatus.ABSENT],
)
def test_dc_recording_with_a_non_qualifying_profile_is_not_promotion_evidence(status: ProfileStatus) -> None:
    result = build(profile=profile(status))

    assert result.promotion_eligible is False
    assert result.promotion_blockers == [BLOCKER]


def test_not_runnable_dc_recording_is_never_promotion_eligible() -> None:
    """两层是「与」关系：物理上跑不起来就一定不是晋级证据。"""
    result = CaseBuilder().from_dc_invocations(
        "dc-session-001",
        "127.0.0.1:5555",
        [invocation()],
        bundle_name="com.example.app",  # 占位身份 ⇒ runnable_blockers 非空
        main_ability="MainAbility",
        profile=profile(ProfileStatus.VERIFIED),
    )

    assert result.replay_eligible is False
    assert result.promotion_eligible is False
    assert result.runnable_blockers
    assert result.promotion_blockers == []


def test_live_path_promotion_rules_are_untouched() -> None:
    """Live 侧的晋级门禁不在本次改动范围：``_promotion_blockers`` 仍只看 provisional/live_mode。"""
    from harmony_test_agent.models import RunState, RunTrace

    trace = RunTrace(
        run_id="run-1",
        target_app_id="zhihu-plus",
        task="回归 Live 晋级门禁",
        device_id="device-1",
        state=RunState.COMPLETED,
        agent_outcome="completed",
        provisional=True,
    )

    built = CaseBuilder().from_trace(trace, profile(ProfileStatus.VERIFIED))

    assert built.promotion_blockers == ["provisional trace is not Profile-promotion evidence"]
    assert built.promotion_eligible is False
    # DC 的 blocker 文案绝不能出现在 Live 结果里。
    assert BLOCKER not in built.promotion_blockers


def test_dc_locator_kind_is_used_for_the_evidence_inventory_probe() -> None:
    """补充断言：``profile`` 只影响定位器证据，不改变 KEY 选择器的种类。"""
    result = build(profile=profile(ProfileStatus.VERIFIED))

    locator = result.spec.steps[0].locator
    assert locator is not None
    assert locator.kind == LocatorKind.KEY
    assert locator.value == "add_agenda_comfrim"
    assert locator.evidence is not None
    assert locator.evidence.source == "dc_resolved_element"


# ---------------------------------------------------------------------------
# 无据断言：进 promotion_blockers，但不进 runnable_blockers（I6）
# ---------------------------------------------------------------------------

UNGROUNDED_TARGET = "p2_channel_content_question_2085141629112009975"


def ungrounded_invocation() -> DcToolInvocation:
    """一条**目标没有任何控件证据**的可回放点击 + 一条无据断言。"""
    return DcToolInvocation(
        invocation_id="inv-1",
        turn_id="turn-1",
        tool=DcToolName.CLICK,
        tier=DcToolTier.L1,
        args={"x": 10, "y": 20},
        success=True,
        resolved_element=UIElement(element_id="ui-1", key="add_agenda_comfrim", content="确定"),
    )


def test_ungrounded_assertion_is_a_promotion_blocker_but_not_a_runnable_blocker() -> None:
    result = CaseBuilder().from_dc_invocations(
        "dc-session-001",
        "127.0.0.1:5555",
        [
            ungrounded_invocation(),
            DcToolInvocation(
                invocation_id="inv-2",
                turn_id="turn-1",
                tool=DcToolName.ASSERT_VISIBLE,
                tier=DcToolTier.L1,
                args={"target": UNGROUNDED_TARGET},
                success=True,
                resolved_element=None,
            ),
        ],
        bundle_name=BUNDLE,
        main_ability="MainAbility",
        profile=profile(ProfileStatus.VERIFIED),
    )

    # 物理上仍可执行（两个可回放动作都在脚本里），但结论不可信。
    assert result.replay_eligible is True
    assert result.runnable_blockers == []
    assert result.promotion_eligible is False
    assert result.promotion_blockers == [UNGROUNDED_ASSERTION_FACTOR]
    assert result.confidence == "low"


def test_ungrounded_assertion_blocker_follows_the_profile_blocker() -> None:
    """顺序稳定：无据因素**追加在**既有 profile blocker 之后。"""
    result = CaseBuilder().from_dc_invocations(
        "dc-session-001",
        "127.0.0.1:5555",
        [
            ungrounded_invocation(),
            DcToolInvocation(
                invocation_id="inv-2",
                turn_id="turn-1",
                tool=DcToolName.ASSERT_VISIBLE,
                tier=DcToolTier.L1,
                args={"target": UNGROUNDED_TARGET},
                success=True,
                resolved_element=None,
            ),
        ],
        bundle_name=BUNDLE,
        main_ability="MainAbility",
    )

    assert result.promotion_blockers == [BLOCKER, UNGROUNDED_ASSERTION_FACTOR]


# ---------------------------------------------------------------------------
# G7：config 里的用例身份必须诚实（悬空 case_id 的修复）
# ---------------------------------------------------------------------------

SESSION = "dc-20260922T115708Z-f2acffa4"


def generated_config(tmp_path, **kwargs) -> tuple[dict, object]:
    from harmony_test_agent.dc.generator import DcHypiumGenerator
    from harmony_test_agent.storage import ArtifactStore

    artifact = DcHypiumGenerator(ArtifactStore(tmp_path / "runs")).generate(
        session_id=SESSION,
        device_id="127.0.0.1:5555",
        invocations=[invocation()],
        bundle_name=BUNDLE,
        main_ability="MainAbility",
        **kwargs,
    )
    return json.loads(Path(artifact.python_path).with_suffix(".json").read_text(encoding="utf-8")), artifact


def test_unsaved_dc_recording_writes_no_dangling_case_id(tmp_path) -> None:
    """未入库的临时脚本：config 不写 ``case_id``/``ir_case_id``，如实写 ``case_persisted=false``。

    历史行为是写一个刚 mint 的用例 IR ID，用例库里查不到——UI 上那个用例身份必然 404。
    """
    config, artifact = generated_config(tmp_path)

    assert "case_id" not in config
    assert "ir_case_id" not in config
    assert config["case_persisted"] is False
    assert config["case_persist_hint"].startswith(f"POST /api/cases/from-dc/{SESSION}")
    # 内存里的 artifact 也不再冒充「可查询的用例」。
    assert artifact.case_id is None
    assert artifact.promotion_eligible is False
    assert artifact.promotion_blockers == [BLOCKER]


def test_saved_dc_recording_writes_the_real_case_id(tmp_path) -> None:
    """真的落库（调用方注入真实用例 ID）时才写 ``case_id`` 并置 ``case_persisted=true``。"""
    config, artifact = generated_config(tmp_path, persisted_case_id="case-dc-real-0001")

    assert config["case_id"] == "case-dc-real-0001"
    assert config["ir_case_id"]
    assert config["case_persisted"] is True
    assert "case_persist_hint" not in config
    assert artifact.case_id == "case-dc-real-0001"
    # 落库不改变可执行性/质量/晋级三层语义。
    assert config["replay_eligible"] is True
    assert config["promotion_eligible"] is False
