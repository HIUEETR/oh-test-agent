"""Phase 4.1 回归（R15）：门禁只有一份实现，5 个调用点必须给出同一结论。

门禁原先在 5 处各自实现（registry / models / verification / orchestrator ×2），
改任意一处而不改其余只会把失败点搬个位置。这里用同一份不达标 Profile 喂给 5 个调用点，
断言共同维度上的失败集合完全一致，并钉住历史文案字面量。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from harmony_test_agent.config import Settings
from harmony_test_agent.models import (
    AssertionDefinition,
    ProfileStatus,
    StableLocator,
    TargetAppProfile,
)
from harmony_test_agent.profiles.admission import (
    MODEL_GATES,
    REGISTRY_GATES,
    AdmissionEvidence,
    AdmissionGate,
    AdmissionThresholds,
    describe_admission_failure,
    evaluate_admission,
    order_admission_failures,
    pick_first_failure,
)
from harmony_test_agent.profiles.registry import ProfileRegistry, ProfileTransitionError

COMMON_GATES = frozenset({AdmissionGate.LOCATORS, AdmissionGate.PAGE_STATES, AdmissionGate.ASSERTIONS})


def _under_qualified_profile() -> TargetAppProfile:
    """1 个定位器 / 1 个页面状态 / 1 条断言：三个资产门禁全部不达标。

    其余维度（验证证据 / 回放 ID / verified_at）刻意全部满足，以便比较各调用点在
    「资产不足」上的结论差异；模型不变式只在 VERIFIED 时触发，故这里保持 DRAFT。
    """
    return TargetAppProfile(
        target_app_id="com-example-under",
        display_name="Under",
        bundle_name="com.example.under",
        main_ability="MainAbility",
        stable_locator_inventory=[
            StableLocator(
                name="only",
                page_signature="page-1",
                key="only_key",
                observed_rounds=1,
                unique_match_rounds=1,
                evidence_snapshot_ids=["snap-1"],
            )
        ],
        assertion_inventory=[
            AssertionDefinition(
                name="a1",
                kind="visible",
                target="only_key",
                page_signature="page-1",
                observed_rounds=1,
                evidence_snapshot_ids=["snap-1"],
            )
        ],
        core_flows=[{"pages": ["page-1"], "steps": [], "interaction_types": ["click", "back"]}],
        provenance={
            "verified_at": "2026-09-21T00:00:00Z",
            "hypium_replay_run_ids": ["run-1:profile-attempt-1"],
            "evidence": {"verification_passed": True},
        },
    )


def test_common_gates_are_reported_identically_by_every_call_site() -> None:
    thresholds = AdmissionThresholds()
    profile = _under_qualified_profile()

    #: 各调用点历史上真正参与判定的门禁维度（由原来的 5 处独立实现决定）。
    cared: dict[str, frozenset[AdmissionGate]] = {
        "registry": COMMON_GATES,
        "candidate": COMMON_GATES,
        "verification": COMMON_GATES,
        "model": frozenset({AdmissionGate.PAGE_STATES, AdmissionGate.ASSERTIONS}),
        "admission_replay": frozenset({AdmissionGate.PAGE_STATES, AdmissionGate.ASSERTIONS}),
    }

    results: dict[str, set[AdmissionGate]] = {}
    for style, include in (
        ("registry", REGISTRY_GATES),
        ("model", MODEL_GATES),
        ("candidate", frozenset({AdmissionGate.LOCATORS, AdmissionGate.PAGE_STATES, AdmissionGate.ASSERTIONS})),
    ):
        evidence = AdmissionEvidence.from_profile(profile, thresholds, include=include)
        results[style] = set(evaluate_admission(evidence, thresholds=thresholds))

    # verification 风格从稳定性报告取值（同一份证据的另一种投影）。
    results["verification"] = set(
        evaluate_admission(
            AdmissionEvidence(stable_locator_count=1, distinct_page_state_count=1, app_assertion_count=1),
            thresholds=thresholds,
        )
    )
    results["admission_replay"] = set(
        evaluate_admission(AdmissionEvidence(distinct_page_state_count=1, app_assertion_count=1), thresholds=thresholds)
    )

    assert set(results) == set(cared)
    for style, gates in results.items():
        # 每个调用点在自己关心的维度上给出与其它调用点完全一致的结论。
        assert gates & cared[style] == COMMON_GATES & cared[style], f"{style} 门禁结论不一致: {gates}"


def test_no_call_site_reports_a_failing_gate_as_passed() -> None:
    """反向保险：调用点但凡评估了某个维度，就必须与规范结论一致（不得漏报失败）。"""
    thresholds = AdmissionThresholds()
    profile = _under_qualified_profile()
    canonical = set(
        evaluate_admission(
            AdmissionEvidence.from_profile(profile, thresholds, include=REGISTRY_GATES), thresholds=thresholds
        )
    )
    cared = {
        "registry": COMMON_GATES,
        "model": frozenset({AdmissionGate.PAGE_STATES, AdmissionGate.ASSERTIONS}),
    }

    for style, include in (("registry", REGISTRY_GATES), ("model", MODEL_GATES)):
        evaluated = set(
            evaluate_admission(
                AdmissionEvidence.from_profile(profile, thresholds, include=include), thresholds=thresholds
            )
        )
        assert canonical & cared[style] <= evaluated, f"{style} 漏掉了他自己评估维度上的失败门禁"


def test_registry_call_site_raises_first_gate_message(tmp_path) -> None:
    registry = ProfileRegistry(tmp_path / "profiles")
    with pytest.raises(ProfileTransitionError, match="candidate requires three stable locators"):
        registry.save_candidate(_under_qualified_profile())


def test_model_call_site_keeps_historical_literal() -> None:
    payload = _under_qualified_profile().model_dump(mode="python")
    payload["status"] = ProfileStatus.VERIFIED
    with pytest.raises(ValidationError, match="verified Profile requires locators on three pages"):
        TargetAppProfile.model_validate(payload)


def test_verification_and_candidate_messages_are_exact() -> None:
    thresholds = AdmissionThresholds()
    assert (
        describe_admission_failure(AdmissionGate.PAGE_STATES, style="verification", observed_count=1)
        == "fewer than 3 distinct page states repeated in every verification round: 1 observed"
    )
    assert (
        describe_admission_failure(AdmissionGate.LOCATORS, style="verification")
        == "fewer than 3 stable high/medium locators"
    )
    assert (
        describe_admission_failure(AdmissionGate.LOCATORS, style="candidate")
        == "Profile candidate does not contain three stable locators"
    )
    assert (
        describe_admission_failure(AdmissionGate.ASSERTIONS, style="admission_replay")
        == "Profile admission replay requires 3 page checks and 2 application assertions"
    )
    assert (
        describe_admission_failure(
            AdmissionGate.CORE_FLOW, style="registry", thresholds=thresholds, prefix="candidate "
        )
        == "candidate requires a replayable three-page core flow"
    )
    assert (
        describe_admission_failure(
            AdmissionGate.ASSERTION_ROUNDS, style="registry", thresholds=thresholds, prefix="promotion "
        )
        == "promotion requires 1-round assertion evidence"
    )


def test_first_failure_order_matches_each_call_site() -> None:
    failures = [
        AdmissionGate.LOCATORS,
        AdmissionGate.PAGE_STATES,
        AdmissionGate.ASSERTIONS,
        AdmissionGate.VERIFICATION,
    ]
    assert pick_first_failure(failures, style="registry") == AdmissionGate.LOCATORS
    assert pick_first_failure(failures, style="model") == AdmissionGate.VERIFICATION
    assert pick_first_failure(failures, style="candidate") == AdmissionGate.LOCATORS
    assert pick_first_failure([], style="registry") is None
    assert order_admission_failures(failures, style="model")[0] == AdmissionGate.VERIFICATION


def test_thresholds_come_from_settings() -> None:
    settings = Settings(
        _env_file=None,
        profile_min_stable_locators=2,
        profile_min_page_states=1,
        profile_min_assertions=1,
        profile_min_interaction_kinds=1,
        profile_verification_rounds=2,
        hypium_replay_attempts=2,
    )

    thresholds = AdmissionThresholds.from_settings(settings)

    assert thresholds.min_stable_locators == 2
    assert thresholds.min_distinct_page_states == 1
    assert thresholds.min_app_assertions == 1
    assert thresholds.min_interaction_kinds == 1
    assert thresholds.min_evidence_rounds == 2
    assert thresholds.min_replay_ids == 2


def test_single_page_app_thresholds_are_configurable(tmp_path) -> None:
    """单页应用调试：把页面状态门槛降到 1 后，同一份 Profile 可以通过准入。"""
    settings = Settings(
        _env_file=None,
        profile_min_stable_locators=1,
        profile_min_page_states=1,
        profile_min_assertions=1,
    )
    thresholds = AdmissionThresholds.from_settings(settings)
    evidence = AdmissionEvidence.from_profile(_under_qualified_profile(), thresholds, include=REGISTRY_GATES)

    assert evaluate_admission(evidence, thresholds=thresholds) == []

    strict = AdmissionThresholds()
    strict_evidence = AdmissionEvidence.from_profile(_under_qualified_profile(), strict, include=REGISTRY_GATES)
    assert evaluate_admission(strict_evidence, thresholds=strict)
