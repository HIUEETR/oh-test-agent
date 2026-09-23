"""G2 的机器证明：日期格 / 时钟读数 / 列表实例 key 不再进脚本（计划 Phase 3）。

真机事故 dc-20260922T115708Z-f2acffa4 的脚本第 51 行是
``driver.touch(BY.key('1_国庆节__廿一_休'))``——该 key 把「1 号 + 国庆节 + 农历廿一 + 休」
编码在一起，``_DYNAMIC_LOCATOR`` 抓不到（后缀不是数字），``is_volatile_evidence_key``
能抓但没人问它。**即使 Phase 1/2 修好，脚本换一天/换一年跑还是在这里挂。**

同时本文件必须守住 Phase 3 的**最高风险**：误杀正在工作的稳定骨架 key。
真机失败脚本里的 ``add_agenda_start_time``（第 60 行）与 ``main_page_date_info``（第 39 行）
回放成功，任何一条被判为易变都意味着「越修越差」。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from harmony_test_agent.cases.builder import (
    VOLATILE_INPUT_OMIT_REASON,
    VOLATILE_LOCATOR_OMIT_REASON,
    CaseBuilder,
    CaseBuildResult,
)
from harmony_test_agent.cases.spec import CheckpointKind, StepAction
from harmony_test_agent.dc.generator import DcHypiumGenerator
from harmony_test_agent.dc.models import DcToolInvocation, DcToolName
from harmony_test_agent.models import (
    BoundingBox,
    LocatorCandidate,
    LocatorKind,
    ScreenSnapshot,
    TargetAppProfile,
    UIElement,
)
from harmony_test_agent.perception.normalizer import normalize_layout
from harmony_test_agent.storage import ArtifactStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "calendar"
SESSION = "dc-20260922T115708Z-f2acffa4"
BUNDLE = "com.huawei.hmos.calendar"
DATE_CELL = "1_国庆节__廿一_休"

#: 真机脚本里正在工作的 6 个稳定骨架 key（``is_volatile_evidence_key`` 会误杀它们）。
STABLE_SKELETON_KEYS = (
    "add_agenda_start_time",
    "add_agenda_end_time",
    "add_agenda_start_time_text",
    "main_page_date_info",
    "month_view_date",
    "month_view_date_banner",
)


def real_invocations() -> list[DcToolInvocation]:
    payload = json.loads((FIXTURES / "invocations.json").read_text(encoding="utf-8"))
    return [DcToolInvocation.model_validate(item) for item in payload]


def real_frames() -> list[ScreenSnapshot]:
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


def invocation(invocation_id: str) -> DcToolInvocation:
    return next(item for item in real_invocations() if item.invocation_id == invocation_id)


def build_dc(invocations: list[Any], **kwargs: Any) -> CaseBuildResult:
    return CaseBuilder().from_dc_invocations(
        SESSION,
        "127.0.0.1:5555",
        invocations,
        bundle_name=BUNDLE,
        main_ability="MainAbility",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 端到端：生成路径
# ---------------------------------------------------------------------------


@pytest.fixture
def date_cell_script(tmp_path: Path):
    """真机日期格点击 + 稳定 key 点击的完整脚本。"""
    return DcHypiumGenerator(ArtifactStore(tmp_path / "runs")).generate(
        session_id=SESSION,
        device_id="127.0.0.1:5555",
        invocations=[invocation("inv-5a71f663ff")],
        snapshots=real_frames(),
        bundle_name=BUNDLE,
        main_ability="MainAbility",
    )


def test_date_cell_key_is_not_rendered_and_falls_back_to_coordinates(date_cell_script) -> None:
    """G2：全文不含日期格 key；该步骤退回坐标兜底（录制坐标 835,657）。"""
    assert "国庆节" not in date_cell_script.python_text
    assert DATE_CELL not in date_cell_script.python_text
    assert "driver.touch((835, 657))  # coordinate fallback" in date_cell_script.python_text
    assert (
        f"volatile key {DATE_CELL!r} encodes a date/clock/list-instance and cannot be used as a replay locator"
        in date_cell_script.warnings
    )


def test_date_cell_key_lowers_confidence_to_low(date_cell_script) -> None:
    """换一天必挂的定位器：报 high/medium 都是说谎。"""
    assert date_cell_script.confidence == "low"
    assert "script contains a date/clock/list-instance locator that will not match on another day" in (
        date_cell_script.confidence_factors
    )


def test_stable_skeleton_keys_still_render_as_key_selectors(tmp_path: Path) -> None:
    """防误杀的端到端证明：稳定 key 照样渲染成 ``BY.key(...)``，且不产生坐标兜底。"""
    result = build_dc(
        [
            invocation("inv-812b833c6f"),
            # 用真实帧里的稳定骨架 key 另造一条 input_text：证明它在 DC 路径上不被拒。
            invocation("inv-5c2eaa6bd8").model_copy(
                update={
                    "resolved_element": UIElement(
                        element_id="ui-start-time",
                        key="add_agenda_start_time",
                        content="开始时间",
                        clickable=True,
                        bbox=BoundingBox(left=60, top=948, right=1260, bottom=1092),
                    )
                }
            ),
        ],
        snapshots=real_frames(),
    )

    assert result.spec.steps[0].locator is not None
    assert result.spec.steps[0].locator.value == "add_agenda_start_time"
    assert result.spec.steps[1].locator is not None
    assert result.spec.steps[1].locator.value == "add_agenda_start_time"
    assert result.counts["coordinate_fallbacks"] == 0
    assert not any(item.startswith(("volatile key ", "volatile id ")) for item in result.warnings)


@pytest.mark.parametrize("key", STABLE_SKELETON_KEYS)
def test_every_stable_skeleton_key_survives_the_selector_path(key: str) -> None:
    """逐条对照真机的 6 个稳定 key：全部必须原样返回 KEY 定位器。"""
    warnings: list[str] = []

    locator = CaseBuilder().locator_from_candidate(
        LocatorCandidate(kind=LocatorKind.KEY, value=key), key, None, warnings
    )

    assert locator is not None
    assert locator.kind == LocatorKind.KEY
    assert locator.value == key
    assert warnings == []


# ---------------------------------------------------------------------------
# 拒绝对应的三种调用点
# ---------------------------------------------------------------------------


def test_rejected_click_always_has_a_coordinate_to_fall_back_to() -> None:
    """DC 的 ``click`` 参数必带 ``x``/``y``，因此被拒的点击总能退到坐标而不必省略动作。

    这也是「点击走坐标、输入走省略」这条不对称规则的由来：输入框没有坐标可退。
    """
    result = build_dc(
        [
            DcToolInvocation(
                invocation_id="inv-no-coordinate",
                turn_id="turn-1",
                tool=invocation("inv-5a71f663ff").tool,
                tier=invocation("inv-5a71f663ff").tier,
                args={},
                success=True,
                resolved_element=UIElement(element_id="ui-x", key=DATE_CELL, content=DATE_CELL),
            )
        ]
    )

    assert result.omitted_actions == []
    step = result.spec.steps[0]
    assert step.action == StepAction.CLICK
    assert step.coordinate == (0, 0)
    assert result.counts["coordinate_fallbacks"] == 1


def test_rejected_input_target_is_omitted_instead_of_typing_into_a_guessed_widget() -> None:
    """输入框没有坐标兜底：省略动作并显式记录「这条输入没了」，不静默写进错的控件。"""
    result = build_dc(
        [
            DcToolInvocation(
                invocation_id="inv-volatile-input",
                turn_id="turn-1",
                tool=invocation("inv-5c2eaa6bd8").tool,
                tier=invocation("inv-5c2eaa6bd8").tier,
                args={"text": "生日"},
                success=True,
                resolved_element=UIElement(element_id="ui-x", key="TimeView_Text_timeText", content="20:47"),
            )
        ]
    )

    assert result.omitted_actions == [
        {"invocation_id": "inv-volatile-input", "tool": "input_text", "reason": VOLATILE_INPUT_OMIT_REASON}
    ]
    assert result.counts["generated_actions"] == 0
    assert "no replayable operations were recorded" in result.warnings


def test_rejected_assertion_falls_back_to_a_semantic_text_anchor() -> None:
    """断言不省略：退回语义文本锚点（与「完全没有定位器」同一分支）。"""
    template = invocation("inv-5a71f663ff")
    result = build_dc(
        [
            DcToolInvocation(
                invocation_id="inv-volatile-assert",
                turn_id="turn-1",
                tool=DcToolName.ASSERT_TEXT,
                tier=template.tier,
                args={"target": "生日"},
                success=True,
                resolved_element=UIElement(element_id="ui-x", key=DATE_CELL, content=DATE_CELL),
            )
        ]
    )

    checkpoint = result.spec.steps[0].checkpoints[0]
    assert checkpoint.kind == CheckpointKind.TEXT_CONTAINS
    assert checkpoint.expected == "生日"
    assert checkpoint.locator is None


def test_timestamp_instance_key_is_not_rejected_by_the_volatile_branch() -> None:
    """``add_agenda_title-<epoch_ms>`` 必须走 Phase 2 的泛化，而不是被这条拒掉。"""
    warnings: list[str] = []

    locator = CaseBuilder().locator_from_candidate(
        LocatorCandidate(kind=LocatorKind.KEY, value="add_agenda_title-1790078405913"), "标题", None, warnings
    )

    assert locator is not None
    assert locator.value == "add_agenda_title-1790078405913"
    assert not any(item.startswith("volatile key ") for item in warnings)


def test_click_on_a_rejected_key_keeps_the_action_as_a_coordinate() -> None:
    """点击有坐标可退：保留动作、坐标兜底，并计入 ``coordinate_fallbacks``。"""
    result = build_dc([invocation("inv-5a71f663ff")], snapshots=real_frames())

    step = result.spec.steps[0]
    assert step.action == StepAction.CLICK
    assert step.coordinate == (835, 657)
    assert step.locator is not None
    assert step.locator.kind == LocatorKind.COORDINATE
    assert result.counts["coordinate_fallbacks"] == 1
    assert result.replay_eligible is True


# ---------------------------------------------------------------------------
# Live 路径的 None 处理（Phase 3.2 的另一半：Live 六处调用点）
# ---------------------------------------------------------------------------


def live_trace(actions: list[Any], snapshots: list[ScreenSnapshot] | None = None) -> Any:
    from harmony_test_agent.models import RunState, RunTrace

    return RunTrace(
        run_id="run-volatile",
        target_app_id="com-huawei-hmos-calendar",
        task="Live 易变定位器回归",
        device_id="device-1",
        state=RunState.COMPLETED,
        agent_outcome="completed",
        actions=actions,
        snapshots=snapshots or [],
    )


def live_profile() -> TargetAppProfile:
    return TargetAppProfile(
        target_app_id="com-huawei-hmos-calendar",
        display_name="日历",
        bundle_name=BUNDLE,
        main_ability="MainAbility",
        launch_strategy={"wait_seconds": 2},
    )


def live_action(tool: Any, *, step_id: str, locator: Any, before_snapshot_id: str | None = None, **params: Any) -> Any:
    from harmony_test_agent.models import ActionResult

    return ActionResult(
        step_id=step_id,
        tool=tool,
        success=True,
        params=params,
        locator=locator,
        before_snapshot_id=before_snapshot_id,
    )


def test_live_click_without_a_runtime_coordinate_is_omitted() -> None:
    """Live 点击被拒且**没有**坐标可退：省略动作并记精确 reason（不是笼统的「无法映射」）。"""
    from harmony_test_agent.models import ToolName

    action = live_action(
        ToolName.CLICK_ELEMENT,
        step_id="click-date-cell",
        locator=LocatorCandidate(kind=LocatorKind.KEY, value=DATE_CELL),
        target="1 号",
    )

    result = CaseBuilder().from_trace(live_trace([action]), live_profile())

    assert result.omitted_actions == [
        {"step_id": "click-date-cell", "tool": str(ToolName.CLICK_ELEMENT), "reason": VOLATILE_LOCATOR_OMIT_REASON}
    ]
    assert result.counts["generated_actions"] == 0
    assert result.counts["omitted_actions"] == 1


def test_live_click_with_a_runtime_coordinate_falls_back_to_it() -> None:
    """Live 点击被拒但有坐标：保留动作并改成坐标兜底。"""
    from pathlib import Path

    from harmony_test_agent.models import RunState, ToolName

    snapshot = ScreenSnapshot(
        snapshot_id="before-click",
        run_id="run-volatile",
        image_path=Path("before.png"),
        image_sha256="x",
        width=1320,
        height=2232,
        elements=[
            UIElement(
                element_id="ui-date-cell",
                key=DATE_CELL,
                content=DATE_CELL,
                bbox=BoundingBox(left=748, top=573, right=923, bottom=741),
            )
        ],
    )
    action = live_action(
        ToolName.CLICK_ELEMENT,
        step_id="click-date-cell",
        locator=LocatorCandidate(kind=LocatorKind.KEY, value=DATE_CELL),
        before_snapshot_id="before-click",
        target="ui-date-cell",
    )
    trace = live_trace([action], [snapshot])
    trace.state = RunState.COMPLETED

    result = CaseBuilder().from_trace(trace, live_profile())

    step = result.spec.steps[0]
    assert step.coordinate is not None
    assert step.locator is not None
    assert step.locator.kind == LocatorKind.COORDINATE
    assert result.counts["coordinate_fallbacks"] == 1


def test_live_input_text_on_a_rejected_key_is_omitted() -> None:
    """Live 输入框同样不能静默写进错的控件。"""
    from harmony_test_agent.models import ToolName

    action = live_action(
        ToolName.INPUT_TEXT,
        step_id="type-into-clock",
        locator=LocatorCandidate(kind=LocatorKind.KEY, value="TimeView_Text_timeText"),
        target="输入框",
        text="生日",
    )

    result = CaseBuilder().from_trace(live_trace([action]), live_profile())

    assert result.omitted_actions == [
        {
            "step_id": "type-into-clock",
            "tool": str(ToolName.INPUT_TEXT),
            "reason": VOLATILE_INPUT_OMIT_REASON,
        }
    ]
    assert result.counts["generated_actions"] == 0


def test_live_assert_on_a_rejected_key_degrades_to_a_semantic_anchor() -> None:
    """断言不省略：退回语义文本锚点，而不是把易变 key 写进断言。"""
    from harmony_test_agent.models import ToolName

    action = live_action(
        ToolName.ASSERT_VISIBLE,
        step_id="assert-date-cell",
        locator=LocatorCandidate(kind=LocatorKind.KEY, value=DATE_CELL),
        target="1 号",
    )

    result = CaseBuilder().from_trace(live_trace([action]), live_profile())

    checkpoint = result.spec.steps[0].checkpoints[0]
    assert checkpoint.locator is not None
    assert checkpoint.locator.kind == LocatorKind.TEXT
    assert checkpoint.locator.value == "1 号"
    assert result.counts["generated_assertions"] == 1


def test_live_volatile_rejection_feeds_confidence_as_a_low_factor() -> None:
    """Live 侧同样降级到 low：换一天必挂的定位器不能报 high/medium。"""
    from harmony_test_agent.models import ToolName

    action = live_action(
        ToolName.CLICK_ELEMENT,
        step_id="click-date-cell",
        locator=LocatorCandidate(kind=LocatorKind.KEY, value=DATE_CELL),
        target="1 号",
    )

    result = CaseBuilder().from_trace(live_trace([action]), live_profile())

    assert "script contains a date/clock/list-instance locator that will not match on another day" in (
        result.confidence_factors
    )
    assert result.confidence == "low"
