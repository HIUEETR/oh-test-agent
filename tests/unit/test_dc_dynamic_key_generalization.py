"""G1 的机器证明：真机动态 key 在**无跨轮证据**时靠会话帧离线泛化（计划 Phase 2）。

真机事故 dc-20260922T115708Z-f2acffa4：脚本第 57 行写成
``driver.input_text(BY.key('add_agenda_title-1790078405913'), '生日')``，
回放报 ``Can't find component with [BY.key('add_agenda_title-1790078405913')]``。

DC 会话没有「轮」，``profile`` 恒为 None，因此不能走 ``validated dynamic`` 分支；
但 `add_agenda_title-<epoch_ms>` 的前缀唯一性可以用**本会话已采集的帧**离线确认
（零设备零磁盘成本）。本文件用真实 layout dump 与真实录制调用锁定该行为。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from harmony_test_agent.dc.generator import DcHypiumGenerator
from harmony_test_agent.dc.models import DcToolInvocation
from harmony_test_agent.models import ScreenSnapshot
from harmony_test_agent.perception.normalizer import normalize_layout
from harmony_test_agent.storage import ArtifactStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "calendar"
SESSION = "dc-20260922T115708Z-f2acffa4"
BUNDLE = "com.huawei.hmos.calendar"
DYNAMIC_KEY = "add_agenda_title-1790078405913"
PREFIX = "add_agenda_title-"


@pytest.fixture(scope="module")
def frames() -> list[ScreenSnapshot]:
    """真实帧：由真机 layout dump 裁掉无关节点后重建（key/bounds/type 原样）。"""
    payload = json.loads((FIXTURES / "snapshots.json").read_text(encoding="utf-8"))
    return [
        ScreenSnapshot(
            snapshot_id=item["snapshot_id"],
            run_id=SESSION,
            captured_at=datetime.fromtimestamp(item["captured_at_epoch"], tz=UTC),
            image_path=Path(item["source_layout"]),
            image_sha256="fixture",
            width=item["width"],
            height=item["height"],
            elements=normalize_layout(item["layout"], item["width"], item["height"]),
        )
        for item in payload
    ]


@pytest.fixture(scope="module")
def input_text_invocation() -> list[DcToolInvocation]:
    """只留真实录制里的那条 ``input_text``（真机脚本第 57 行对应的调用）。"""
    payload = json.loads((FIXTURES / "invocations.json").read_text(encoding="utf-8"))
    invocations = [DcToolInvocation.model_validate(item) for item in payload]
    return [item for item in invocations if item.tool.value == "input_text"]


@pytest.fixture
def generated(tmp_path: Path, frames: list[ScreenSnapshot], input_text_invocation: list[DcToolInvocation]):
    return DcHypiumGenerator(ArtifactStore(tmp_path / "runs")).generate(
        session_id=SESSION,
        device_id="127.0.0.1:5555",
        invocations=input_text_invocation,
        snapshots=frames,
        bundle_name=BUNDLE,
        main_ability="MainAbility",
    )


def test_generated_script_uses_a_starts_with_prefix_instead_of_the_instance_key(generated) -> None:
    """G1：渲染为 starts_with 前缀选择器，且全文不再出现那串毫秒实例 ID。"""
    assert "driver.input_text(BY.key('add_agenda_title-', MatchPattern.STARTS_WITH), '生日')" in generated.python_text
    assert DYNAMIC_KEY not in generated.python_text
    assert "1790078405913" not in generated.python_text


def test_generalization_is_reported_with_frame_evidence(generated) -> None:
    """警告必须写出「泛化依据」（帧数），而不是一句无从核对的「已泛化」。"""
    warning = next(item for item in generated.warnings if item.startswith("timestamp-suffixed key "))

    assert f"timestamp-suffixed key {DYNAMIC_KEY!r} generalized to prefix {PREFIX!r}" in warning
    # 3 帧 fixture 里有 2 帧含该前缀（第三帧是日期格页，面板已关闭）。
    assert "unique in 2 captured frame(s)" in warning
    assert "no cross-round Profile evidence" in warning


def test_single_session_generalization_lowers_confidence_to_medium(generated) -> None:
    """G4：含未验证动态定位器的脚本不得再报 high。"""
    assert generated.confidence == "medium"
    assert "dynamic locator generalized from a single session without cross-round evidence" in (
        generated.confidence_factors
    )


def test_prefix_is_not_generalized_when_the_same_frame_holds_two_matches(tmp_path: Path) -> None:
    """同帧匹配到多个控件时不泛化：宁可脚本不可回放，也不要点到错误元素。"""
    payload = json.loads((FIXTURES / "snapshots.json").read_text(encoding="utf-8"))
    frames = []
    for item in payload:
        elements = normalize_layout(item["layout"], item["width"], item["height"])
        frames.append(
            ScreenSnapshot(
                snapshot_id=item["snapshot_id"],
                run_id=SESSION,
                captured_at=datetime.fromtimestamp(item["captured_at_epoch"], tz=UTC),
                image_path=Path(item["source_layout"]),
                image_sha256="fixture",
                width=item["width"],
                height=item["height"],
                # 伪造第二个同前缀控件：`add_agenda_title_prompt`
                elements=[*elements, elements[0].model_copy(update={"key": f"{PREFIX}prompt", "id": ""})],
            )
        )
    invocations = [
        DcToolInvocation.model_validate(item)
        for item in json.loads((FIXTURES / "invocations.json").read_text(encoding="utf-8"))
    ]
    generated = DcHypiumGenerator(ArtifactStore(tmp_path / "runs")).generate(
        session_id=SESSION,
        device_id="127.0.0.1:5555",
        invocations=[item for item in invocations if item.tool.value == "input_text"],
        snapshots=frames,
        bundle_name=BUNDLE,
        main_ability="MainAbility",
    )

    assert DYNAMIC_KEY in generated.python_text
    assert "prefix is not unique, retained as an exact selector that will fail on replay" in " ".join(
        generated.warnings
    )
    assert generated.confidence == "low"
    assert "script contains a locator that will not match on replay" in generated.confidence_factors
