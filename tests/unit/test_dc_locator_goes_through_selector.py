"""DC 录制定位器必须经过 ``locator_from_candidate``（计划 Phase 1 / R1）。

历史 bug：``builder.py`` 的 DC 路径三处**手工构造** ``LocatorSpec``，因此绕过了动态 key
泛化（``_DYNAMIC_LOCATOR``）与易变 key 拒绝（``is_unreplayable_locator_key``）。
真机事故 dc-20260922T115708Z-f2acffa4 的 ``add_agenda_title-1790078405913`` 就是这样
原样写进脚本的，回放必然 ``Can't find component with [BY.key(...)]``。

本文件锁的是「接上了」这一层：三处都发得出选择器层警告、``evidence.source`` 仍是
``dc_resolved_element``、置信度不再是无脑 high。**能否回放**由 Phase 2/3 决定。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from harmony_test_agent.cases.builder import CaseBuilder, CaseBuildResult
from harmony_test_agent.cases.spec import StepAction
from harmony_test_agent.dc.models import DcToolInvocation
from harmony_test_agent.models import BoundingBox, LocatorKind, UIElement

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "calendar"
BUNDLE = "com.huawei.hmos.calendar"


@pytest.fixture(scope="module")
def real_invocations() -> list[DcToolInvocation]:
    """真实录制调用：input_text（动态 key）、click（日期格）、click（稳定 key）。"""
    payload = json.loads((FIXTURES / "invocations.json").read_text(encoding="utf-8"))
    return [DcToolInvocation.model_validate(item) for item in payload]


def invocation_by_id(invocations: list[DcToolInvocation], invocation_id: str) -> DcToolInvocation:
    return next(item for item in invocations if item.invocation_id == invocation_id)


def build(invocations: list[DcToolInvocation], **kwargs: Any) -> CaseBuildResult:
    """**不传 snapshots**：模拟「一帧都没采到」——此时不泛化，只能保留精确值 + 警告。"""
    return CaseBuilder().from_dc_invocations(
        "dc-session-001",
        "127.0.0.1:5555",
        invocations,
        bundle_name=BUNDLE,
        main_ability="MainAbility",
        **kwargs,
    )


def test_timestamp_key_without_frames_is_kept_as_an_exact_diagnostic_selector(
    real_invocations: list[DcToolInvocation],
) -> None:
    """没有帧证据时不冒险泛化：保留精确值，但必须发出选择器层警告（历史行为是零警告）。"""
    target = invocation_by_id(real_invocations, "inv-5c2eaa6bd8")

    result = build([target])

    assert (
        "unvalidated dynamic key 'add_agenda_title-1790078405913' retained as an exact diagnostic selector"
        in result.warnings
    )
    step = result.spec.steps[0]
    assert step.action == StepAction.INPUT_TEXT
    assert step.locator is not None
    assert step.locator.kind == LocatorKind.KEY
    assert step.locator.value == "add_agenda_title-1790078405913"
    assert step.text == "生日"


def test_dc_dynamic_key_lowers_confidence_below_high(real_invocations: list[DcToolInvocation]) -> None:
    """未验证动态定位器把置信度从 high 拉下来：过去这里报 high 是假的。"""
    result = build([invocation_by_id(real_invocations, "inv-5c2eaa6bd8")])

    assert result.confidence != "high"
    assert "source trace contains a dynamic locator without stable unique-prefix evidence" in result.confidence_factors


def test_dc_locator_keeps_the_dc_evidence_source(real_invocations: list[DcToolInvocation]) -> None:
    """``evidence.source == "dc_resolved_element"`` 是既有契约（三处调用点都依赖）。"""
    for invocation_id in ("inv-5c2eaa6bd8", "inv-812b833c6f"):
        result = build([invocation_by_id(real_invocations, invocation_id)])
        locator = result.spec.steps[0].locator
        assert locator is not None
        assert locator.evidence is not None
        assert locator.evidence.source == "dc_resolved_element"


def test_dc_stable_key_gets_no_locator_warning(real_invocations: list[DcToolInvocation]) -> None:
    """稳定 key 不得被新接入的选择器逻辑惊扰：零选择器警告、零定位器质量因素。"""
    result = build([invocation_by_id(real_invocations, "inv-812b833c6f")])

    locator = result.spec.steps[0].locator
    assert locator is not None
    assert locator.value == "add_agenda_start_time"
    assert [item for item in result.warnings if "locator" in item or "dynamic" in item] == []
    # 唯一的质量因素来自「这条录制没有断言」——与定位器无关（这也正是 Phase 7 (i) 的护栏）。
    assert result.confidence_factors == ["no explicit assert_* tool call was recorded"]
    assert result.confidence == "medium"


def test_dc_locator_helper_returns_none_for_an_element_without_key_or_id() -> None:
    element = UIElement(element_id="ui-x", content="无 key", bbox=BoundingBox(left=0, top=0, right=10, bottom=10))

    assert CaseBuilder()._dc_locator(element, None, None, []) is None
    assert CaseBuilder()._dc_locator(None, None, None, []) is None


@pytest.mark.parametrize("target", ["", None])
def test_dc_locator_falls_back_to_the_key_as_target_label(target: str | None) -> None:
    """DC 的 click 参数里没有 ``target``：标签退回 key 本身（历史行为）。"""
    element = UIElement(element_id="ui-1", key="phone_add_agenda", content="新建")

    locator = CaseBuilder()._dc_locator(element, target, None, [])

    assert locator is not None
    assert locator.target_label == "phone_add_agenda"
