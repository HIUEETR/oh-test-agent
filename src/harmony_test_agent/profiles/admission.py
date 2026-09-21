"""Profile 准入/晋级门禁的唯一实现（计划 4.1 / R15）。

背景：同一套「3 定位器 / 3 页 / 2 断言 / 验证证据」门禁此前在 5 处各自实现：

- ``profiles/registry.py::_validate_admission_assets``（candidate / promotion 晋级）
- ``models.py::TargetAppProfile.validate_profile_invariants``（VERIFIED 不变式）
- ``discovery/verification.py::ProfileVerifier.verify``（每轮与结果级门禁）
- ``agents/orchestrator.py::_bootstrap_profile``（本地 candidate 门禁）
- ``agents/orchestrator.py::_profile_validation_trace``（准入回放轨迹门禁）

阈值各写各的、文案各写各的：改任何一处而不改其余，只会把失败点搬个位置。本模块把
**计数与判定**收敛为 :func:`evaluate_admission`，把**文案**收敛为
:func:`describe_admission_failure`（按调用点风格渲染，逐字保持历史字面量，兼容既有测试）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

__all__ = [
    "ADMISSION_ORDER",
    "MODEL_GATES",
    "PRIORITY_BY_STYLE",
    "REGISTRY_GATES",
    "AdmissionEvidence",
    "AdmissionGate",
    "AdmissionThresholds",
    "describe_admission_failure",
    "evaluate_admission",
    "order_admission_failures",
    "pick_first_failure",
]


class AdmissionGate(StrEnum):
    """门禁维度；每个维度在一个调用点最多产生一条失败。"""

    VERIFIED_AT = "verified_at"
    REPLAY_IDS = "replay_ids"
    LOCATORS = "stable_locators"
    PAGE_STATES = "distinct_page_states"
    LOCATOR_ROUNDS = "locator_rounds"
    ASSERTIONS = "app_assertions"
    ASSERTION_ROUNDS = "assertion_rounds"
    CORE_FLOW = "core_flow"
    INTERACTION_KINDS = "interaction_kinds"
    VERIFICATION = "verification"
    CROSS_BUNDLE = "cross_bundle"


@dataclass(frozen=True, slots=True)
class AdmissionThresholds:
    """门禁阈值；全部可由 ``Settings`` 注入（计划 4.2）。"""

    min_stable_locators: int = 3
    min_distinct_page_states: int = 3
    min_app_assertions: int = 2
    min_interaction_kinds: int = 2
    min_replay_ids: int = 1
    min_evidence_rounds: int = 1

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        min_interaction_kinds: int | None = None,
        min_evidence_rounds: int | None = None,
    ) -> AdmissionThresholds:
        """从 ``Settings`` 读取阈值；调用点已有的运行态覆盖项优先。"""
        return cls(
            min_stable_locators=int(getattr(settings, "profile_min_stable_locators", 3)),
            min_distinct_page_states=int(getattr(settings, "profile_min_page_states", 3)),
            min_app_assertions=int(getattr(settings, "profile_min_assertions", 2)),
            min_interaction_kinds=int(
                min_interaction_kinds
                if min_interaction_kinds is not None
                else getattr(settings, "profile_min_interaction_kinds", 2)
            ),
            min_replay_ids=int(getattr(settings, "hypium_replay_attempts", 1)),
            min_evidence_rounds=int(
                min_evidence_rounds
                if min_evidence_rounds is not None
                else getattr(settings, "profile_verification_rounds", 1)
            ),
        )


@dataclass(frozen=True, slots=True)
class AdmissionEvidence:
    """门禁计数输入；``None`` 表示该维度不参与判定（调用点只看自己关心的门禁）。"""

    stable_locator_count: int | None = None
    distinct_page_state_count: int | None = None
    round_qualified_locator_count: int | None = None
    app_assertion_count: int | None = None
    assertion_rounds_ok: bool | None = None
    core_flow_page_count: int | None = None
    interaction_kind_count: int | None = None
    verification_passed: bool | None = None
    cross_bundle_recovery_failed: bool | None = None
    replay_ids: tuple[str, ...] | None = None
    verified_at_present: bool | None = None

    @classmethod
    def from_profile(
        cls,
        profile: Any,
        thresholds: AdmissionThresholds | None = None,
        *,
        include: frozenset[AdmissionGate] | None = None,
    ) -> AdmissionEvidence:
        """按 ``registry._validate_admission_assets`` 的口径把 Profile 折算为计数。

        ``include`` 限定本调用点真正关心的门禁维度；未包含的维度置为 ``None``（不参与判定），
        从而让同一份计数实现服务历史门禁集合不同的调用点。
        """
        limits = thresholds or AdmissionThresholds()

        def want(gate: AdmissionGate) -> bool:
            return include is None or gate in include

        locators = list(profile.stable_locator_inventory)
        assertions = list(profile.assertion_inventory)
        flow = profile.core_flows[0] if profile.core_flows else {}
        pages = list(flow.get("pages") or [])
        kinds = list(flow.get("interaction_types") or [])
        provenance = profile.provenance
        evidence = dict(provenance.evidence or {})
        admitted = [
            item
            for item in locators
            if item.observed_rounds >= limits.min_evidence_rounds
            and item.unique_match_rounds >= limits.min_evidence_rounds
            and item.evidence_snapshot_ids
        ]
        return cls(
            stable_locator_count=len(locators) if want(AdmissionGate.LOCATORS) else None,
            distinct_page_state_count=(
                len({item.page_signature for item in locators}) if want(AdmissionGate.PAGE_STATES) else None
            ),
            round_qualified_locator_count=len(admitted) if want(AdmissionGate.LOCATOR_ROUNDS) else None,
            app_assertion_count=len(assertions) if want(AdmissionGate.ASSERTIONS) else None,
            assertion_rounds_ok=(
                not any(
                    item.observed_rounds < limits.min_evidence_rounds or not item.evidence_snapshot_ids
                    for item in assertions
                )
                if want(AdmissionGate.ASSERTION_ROUNDS)
                else None
            ),
            core_flow_page_count=len(pages) if want(AdmissionGate.CORE_FLOW) else None,
            interaction_kind_count=len(set(kinds)) if want(AdmissionGate.INTERACTION_KINDS) else None,
            verification_passed=(
                bool(evidence.get("verification_passed")) if want(AdmissionGate.VERIFICATION) else None
            ),
            cross_bundle_recovery_failed=(
                bool(evidence.get("cross_bundle_recovery_failed", False)) if want(AdmissionGate.CROSS_BUNDLE) else None
            ),
            replay_ids=(tuple(provenance.hypium_replay_run_ids) if want(AdmissionGate.REPLAY_IDS) else None),
            verified_at_present=((provenance.verified_at is not None) if want(AdmissionGate.VERIFIED_AT) else None),
        )


#: ``registry._validate_admission_assets`` 关心的门禁集合（candidate / promotion 晋级）。
REGISTRY_GATES: frozenset[AdmissionGate] = frozenset(
    {
        AdmissionGate.LOCATORS,
        AdmissionGate.PAGE_STATES,
        AdmissionGate.LOCATOR_ROUNDS,
        AdmissionGate.ASSERTIONS,
        AdmissionGate.ASSERTION_ROUNDS,
        AdmissionGate.CORE_FLOW,
        AdmissionGate.INTERACTION_KINDS,
        AdmissionGate.VERIFICATION,
        AdmissionGate.CROSS_BUNDLE,
    }
)

#: ``models.validate_profile_invariants`` 关心的门禁集合（历史不变式只查这 5 项）。
MODEL_GATES: frozenset[AdmissionGate] = frozenset(
    {
        AdmissionGate.VERIFIED_AT,
        AdmissionGate.REPLAY_IDS,
        AdmissionGate.VERIFICATION,
        AdmissionGate.PAGE_STATES,
        AdmissionGate.ASSERTIONS,
    }
)


#: 规范顺序：与 ``registry._validate_admission_assets`` 的历史顺序一致。
ADMISSION_ORDER: tuple[AdmissionGate, ...] = (
    AdmissionGate.LOCATORS,
    AdmissionGate.PAGE_STATES,
    AdmissionGate.LOCATOR_ROUNDS,
    AdmissionGate.ASSERTIONS,
    AdmissionGate.ASSERTION_ROUNDS,
    AdmissionGate.CORE_FLOW,
    AdmissionGate.INTERACTION_KINDS,
    AdmissionGate.VERIFICATION,
    AdmissionGate.CROSS_BUNDLE,
    AdmissionGate.VERIFIED_AT,
    AdmissionGate.REPLAY_IDS,
)

#: 各调用点的历史优先顺序（决定「第一条失败」是哪一条，从而决定历史文案）。
PRIORITY_BY_STYLE: dict[str, tuple[AdmissionGate, ...]] = {
    "registry": ADMISSION_ORDER,
    "model": (
        AdmissionGate.VERIFIED_AT,
        AdmissionGate.REPLAY_IDS,
        AdmissionGate.VERIFICATION,
        AdmissionGate.PAGE_STATES,
        AdmissionGate.ASSERTIONS,
    ),
    "verification": (
        AdmissionGate.INTERACTION_KINDS,
        AdmissionGate.PAGE_STATES,
        AdmissionGate.LOCATORS,
        AdmissionGate.ASSERTIONS,
    ),
    "candidate": (AdmissionGate.LOCATORS, AdmissionGate.PAGE_STATES, AdmissionGate.ASSERTIONS),
    "admission_replay": (AdmissionGate.PAGE_STATES, AdmissionGate.ASSERTIONS),
}

AdmissionStyle = Literal["registry", "model", "verification", "candidate", "admission_replay"]


def evaluate_admission(
    evidence: AdmissionEvidence,
    *,
    thresholds: AdmissionThresholds | None = None,
) -> list[AdmissionGate]:
    """返回未满足的门禁维度（空列表 = 通过）。唯一实现，多处调用。

    ``evidence`` 里为 ``None`` 的维度不参与判定，使同一函数可以服务 5 个只看部分门禁的调用点。
    """
    limits = thresholds or AdmissionThresholds()
    failures: list[AdmissionGate] = []
    if evidence.stable_locator_count is not None and evidence.stable_locator_count < limits.min_stable_locators:
        failures.append(AdmissionGate.LOCATORS)
    if (
        evidence.distinct_page_state_count is not None
        and evidence.distinct_page_state_count < limits.min_distinct_page_states
    ):
        failures.append(AdmissionGate.PAGE_STATES)
    if (
        evidence.round_qualified_locator_count is not None
        and evidence.round_qualified_locator_count < limits.min_stable_locators
    ):
        failures.append(AdmissionGate.LOCATOR_ROUNDS)
    if evidence.app_assertion_count is not None and evidence.app_assertion_count < limits.min_app_assertions:
        failures.append(AdmissionGate.ASSERTIONS)
    if evidence.assertion_rounds_ok is False:
        failures.append(AdmissionGate.ASSERTION_ROUNDS)
    if evidence.core_flow_page_count is not None and evidence.core_flow_page_count < limits.min_distinct_page_states:
        failures.append(AdmissionGate.CORE_FLOW)
    if evidence.interaction_kind_count is not None and evidence.interaction_kind_count < limits.min_interaction_kinds:
        failures.append(AdmissionGate.INTERACTION_KINDS)
    if evidence.verification_passed is False:
        failures.append(AdmissionGate.VERIFICATION)
    if evidence.cross_bundle_recovery_failed is True:
        failures.append(AdmissionGate.CROSS_BUNDLE)
    if evidence.verified_at_present is False:
        failures.append(AdmissionGate.VERIFIED_AT)
    if evidence.replay_ids is not None:
        replay_ids = list(evidence.replay_ids)
        if len(replay_ids) < limits.min_replay_ids or len(set(replay_ids)) != len(replay_ids):
            failures.append(AdmissionGate.REPLAY_IDS)
    return [gate for gate in ADMISSION_ORDER if gate in set(failures)]


def pick_first_failure(
    failures: list[AdmissionGate],
    *,
    style: AdmissionStyle = "registry",
) -> AdmissionGate | None:
    """按调用点的历史优先顺序取第一条失败（保证历史文案与语义不变）。"""
    if not failures:
        return None
    present = set(failures)
    for gate in PRIORITY_BY_STYLE.get(style, ADMISSION_ORDER):
        if gate in present:
            return gate
    return failures[0]


def order_admission_failures(
    failures: list[AdmissionGate],
    *,
    style: AdmissionStyle = "registry",
) -> list[AdmissionGate]:
    """按调用点的历史优先顺序重排全部失败（用于逐条追加失败文案的调用点）。"""
    if not failures:
        return []
    present = set(failures)
    ordered = [gate for gate in PRIORITY_BY_STYLE.get(style, ADMISSION_ORDER) if gate in present]
    return [*ordered, *(gate for gate in failures if gate not in set(ordered))]


def describe_admission_failure(
    gate: AdmissionGate,
    *,
    style: AdmissionStyle = "registry",
    thresholds: AdmissionThresholds | None = None,
    prefix: str = "",
    observed_count: int | None = None,
) -> str:
    """按调用点风格渲染门禁失败文案（历史字面量逐字保留）。"""
    limits = thresholds or AdmissionThresholds()
    rounds = limits.min_evidence_rounds
    if style == "registry":
        return {
            AdmissionGate.LOCATORS: f"{prefix}requires three stable locators",
            AdmissionGate.PAGE_STATES: f"{prefix}requires stable locators on three pages",
            AdmissionGate.LOCATOR_ROUNDS: f"{prefix}requires {rounds}-round unique locator evidence",
            AdmissionGate.ASSERTIONS: f"{prefix}requires two application-level assertions",
            AdmissionGate.ASSERTION_ROUNDS: f"{prefix}requires {rounds}-round assertion evidence",
            AdmissionGate.CORE_FLOW: f"{prefix}requires a replayable three-page core flow",
            AdmissionGate.INTERACTION_KINDS: f"{prefix}requires {limits.min_interaction_kinds} interaction types",
            AdmissionGate.VERIFICATION: f"{prefix}requires passed device verification evidence",
            AdmissionGate.CROSS_BUNDLE: f"{prefix}cannot contain unrecovered cross-bundle violations",
        }.get(gate, f"{prefix}does not satisfy the {gate} admission gate")
    if style == "model":
        return {
            AdmissionGate.VERIFIED_AT: "verified Profile requires provenance.verified_at",
            AdmissionGate.REPLAY_IDS: "verified Profile requires at least one unique Hypium replay run ID",
            AdmissionGate.VERIFICATION: "verified Profile requires passed device verification evidence",
            AdmissionGate.PAGE_STATES: "verified Profile requires locators on three pages",
            AdmissionGate.ASSERTIONS: "verified Profile requires two application assertions",
        }.get(gate, f"verified Profile does not satisfy the {gate} admission gate")
    if style == "verification":
        if gate == AdmissionGate.PAGE_STATES:
            suffix = f": {observed_count} observed" if observed_count is not None else ""
            return f"fewer than 3 distinct page states repeated in every verification round{suffix}"
        return {
            AdmissionGate.INTERACTION_KINDS: f"fewer than {limits.min_interaction_kinds} interaction types",
            AdmissionGate.LOCATORS: "fewer than 3 stable high/medium locators",
            AdmissionGate.ASSERTIONS: "fewer than 2 stable application-level assertions",
        }.get(gate, f"verification gate failed: {gate}")
    if style == "candidate":
        return {
            AdmissionGate.LOCATORS: "Profile candidate does not contain three stable locators",
            AdmissionGate.PAGE_STATES: "Profile candidate does not contain stable locators on three pages",
            AdmissionGate.ASSERTIONS: "Profile candidate does not contain two application-level assertions",
        }.get(gate, f"Profile candidate does not satisfy the {gate} admission gate")
    if style == "admission_replay":
        return "Profile admission replay requires 3 page checks and 2 application assertions"
    raise ValueError(f"unknown admission message style: {style}")
