"""``CaseBuilder.from_dc_invocations`` / ``DcHypiumGenerator.build`` 的 DC 映射契约测试。

覆盖计划 A3 的 DC 行：``resolved_element`` → KEY/ID 结构化定位器（带
``dc_resolved_element`` 证据）、裸 ``click`` → 坐标 + ``coordinate_fallbacks``、swipe 起止
坐标推断与固定顺序的聚合警告、非 Back 的 ``KEY_EVENT`` 省略文案、non-replayable 工具的
``# skipped: ...`` 注释、DC 从不注入兜底断言、``replay_eligible`` 三条件规则，以及
「无可回放操作」时的警告与 ``NO_REPLAYABLE_COMMENT`` 步骤。

测试全程离线：只构造 ``DcToolInvocation`` 并调用纯函数（``build`` 不落盘）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from harmony_test_agent.cases.builder import (
    PLACEHOLDER_ABILITY,
    PLACEHOLDER_BUNDLE,
    UNGROUNDED_ABSENT_ASSERTION_OMIT_REASON,
    CaseBuilder,
    CaseBuildResult,
)
from harmony_test_agent.cases.spec import (
    NO_REPLAYABLE_COMMENT,
    CheckpointKind,
    CheckpointSpec,
    StepAction,
)
from harmony_test_agent.dc.generator import DcHypiumGenerator
from harmony_test_agent.dc.models import DcToolInvocation, DcToolName, DcToolTier, utc_now
from harmony_test_agent.generation.standalone import StandaloneEmitter
from harmony_test_agent.models import BoundingBox, LocatorCandidate, LocatorKind, ScreenSnapshot, UIElement
from harmony_test_agent.storage import ArtifactStore

SESSION_ID = "dc-session-001"
DEVICE_ID = "127.0.0.1:5555"

# ---------------------------------------------------------------------------
# fixture 构造
# ---------------------------------------------------------------------------


@pytest.fixture
def artifacts(tmp_path: Path) -> ArtifactStore:
    """隔离的产物根：``DcHypiumGenerator`` 需要 store，但 ``build`` 不落盘。"""
    return ArtifactStore(tmp_path / "runs")


def invocation(
    tool: DcToolName,
    args: dict | None = None,
    *,
    success: bool = True,
    invocation_id: str = "inv-001",
    resolved_element: UIElement | None = None,
) -> DcToolInvocation:
    return DcToolInvocation(
        invocation_id=invocation_id,
        turn_id="turn-001",
        tool=tool,
        tier=DcToolTier.L1,
        args=args or {},
        success=success,
        started_at=utc_now(),
        ended_at=utc_now(),
        duration_ms=100,
        resolved_element=resolved_element,
    )


def element(*, key: str = "", element_id: str = "", content: str = "搜索") -> UIElement:
    return UIElement(
        element_id=key or element_id,
        key=key,
        id=element_id,
        content=content,
        clickable=True,
        bbox=BoundingBox(left=0, top=0, right=100, bottom=50),
    )


def snapshot() -> ScreenSnapshot:
    return ScreenSnapshot(
        snapshot_id="snap-1",
        run_id="dc-session-001",
        image_path=Path("screens/snap-1.png"),
        image_sha256="abc",
        width=1080,
        height=2340,
        elements=[UIElement(element_id="stable-title", key="stable_title", content="首页")],
    )


def build_dc(
    artifacts: ArtifactStore,
    invocations: list[DcToolInvocation],
    *,
    snapshots: list[ScreenSnapshot] | None = None,
    bundle_name: str = "com.demo.app",
    main_ability: str = "MainAbility",
) -> CaseBuildResult:
    return DcHypiumGenerator(artifacts).build(
        SESSION_ID,
        DEVICE_ID,
        invocations,
        snapshots=snapshots,
        bundle_name=bundle_name,
        main_ability=main_ability,
    )


def step_by_id(result: CaseBuildResult, step_id: str) -> Any:
    return next(step for step in result.spec.steps if step.step_id == step_id)


def checkpoints_of(result: CaseBuildResult) -> list[CheckpointSpec]:
    return [checkpoint for step in result.spec.steps for checkpoint in step.checkpoints]


def inferred_swipe_warnings(result: CaseBuildResult) -> list[str]:
    return [warning for warning in result.warnings if "swipe direction inferred" in warning]


def asserted_replay_rule(result: CaseBuildResult, *, bundle_name: str, main_ability: str) -> bool:
    """把计划的 G3 两条物理必要条件原样写一遍，用于逐条对照实测结果。"""
    return bool(
        result.counts["generated_actions"] > 0 and bundle_name and bundle_name != PLACEHOLDER_BUNDLE and main_ability
    )


# ---------------------------------------------------------------------------
# resolved_element → 结构化定位器
# ---------------------------------------------------------------------------


def test_click_with_resolved_element_key_uses_key_locator_with_dc_evidence(artifacts: ArtifactStore) -> None:
    result = build_dc(
        artifacts,
        [invocation(DcToolName.CLICK, {"x": 10, "y": 20}, resolved_element=element(key="home_search"))],
    )

    locator = step_by_id(result, "inv-001").locator
    assert locator is not None
    assert locator.kind == LocatorKind.KEY
    assert locator.value == "home_search"
    assert locator.evidence is not None
    assert locator.evidence.source == "dc_resolved_element"
    assert result.counts["coordinate_fallbacks"] == 0


def test_click_with_resolved_element_id_only_uses_id_locator(artifacts: ArtifactStore) -> None:
    result = build_dc(
        artifacts,
        [invocation(DcToolName.CLICK, {"x": 10, "y": 20}, resolved_element=element(element_id="submit_button"))],
    )

    locator = step_by_id(result, "inv-001").locator
    assert locator is not None
    assert locator.kind == LocatorKind.ID
    assert locator.value == "submit_button"
    assert locator.evidence is not None
    assert locator.evidence.source == "dc_resolved_element"


def test_input_text_with_resolved_element_uses_structured_locator(artifacts: ArtifactStore) -> None:
    result = build_dc(
        artifacts,
        [
            invocation(
                DcToolName.INPUT_TEXT,
                {"text": "hello"},
                resolved_element=element(key="search_input"),
            )
        ],
    )

    step = step_by_id(result, "inv-001")
    assert step.action == StepAction.INPUT_TEXT
    assert step.text == "hello"
    assert step.locator is not None
    assert step.locator.kind == LocatorKind.KEY
    assert step.locator.value == "search_input"


# ---------------------------------------------------------------------------
# 裸 click → 坐标兜底
# ---------------------------------------------------------------------------


def test_click_without_resolved_element_falls_back_to_coordinate(artifacts: ArtifactStore) -> None:
    result = build_dc(artifacts, [invocation(DcToolName.CLICK, {"x": 10, "y": 20})])

    step = step_by_id(result, "inv-001")
    assert step.action == StepAction.CLICK
    assert step.coordinate == (10, 20)
    assert step.locator is not None
    assert step.locator.kind == LocatorKind.COORDINATE
    assert step.locator.coordinate == (10, 20)
    # 没有快照时解析边界未知，但坐标定位器仍必须带边界与警告（IR 不变式）。
    assert step.locator.resolution_bound == (0, 0)
    assert step.locator.warning is not None
    assert step.locator.warning.startswith("inv-001: click fell back to coordinate (10, 20)")
    assert "resolution bound unknown" in step.locator.warning
    assert result.counts["coordinate_fallbacks"] == 1


def test_click_without_resolved_element_records_snapshot_resolution_bound(artifacts: ArtifactStore) -> None:
    result = build_dc(
        artifacts,
        [invocation(DcToolName.CLICK, {"coordinate": [540, 1200]})],
        snapshots=[snapshot()],
    )

    locator = step_by_id(result, "inv-001").locator
    assert locator is not None
    assert locator.kind == LocatorKind.COORDINATE
    assert locator.resolution_bound == (1080, 2340)
    assert result.counts["coordinate_fallbacks"] == 1


def test_coordinate_fallback_count_accumulates_per_invocation(artifacts: ArtifactStore) -> None:
    result = build_dc(
        artifacts,
        [
            invocation(DcToolName.CLICK, {"x": 1, "y": 2}, invocation_id="inv-1"),
            invocation(DcToolName.CLICK, {"x": 3, "y": 4}, invocation_id="inv-2"),
            invocation(DcToolName.CLICK, {"x": 5, "y": 6}, resolved_element=element(key="k"), invocation_id="inv-3"),
        ],
    )

    assert result.counts["coordinate_fallbacks"] == 2


# ---------------------------------------------------------------------------
# swipe 方向推断与警告聚合
# ---------------------------------------------------------------------------


def test_swipe_directions_are_inferred_and_aggregated_in_fixed_order(artifacts: ArtifactStore) -> None:
    result = build_dc(
        artifacts,
        [
            invocation(DcToolName.SWIPE, {"start": [500, 100], "end": [100, 100]}, invocation_id="inv-left"),
            invocation(DcToolName.SWIPE, {"start": [100, 200], "end": [100, 500]}, invocation_id="inv-down"),
            invocation(DcToolName.SWIPE, {"start": [100, 500], "end": [100, 200]}, invocation_id="inv-up-1"),
            invocation(DcToolName.SWIPE, {"start": [100, 600], "end": [100, 300]}, invocation_id="inv-up-2"),
            invocation(
                DcToolName.SWIPE,
                {"direction": "DOWN", "start": [100, 200], "end": [100, 500]},
                invocation_id="inv-explicit",
            ),
        ],
    )

    # 固定顺序 UP → DOWN → LEFT → RIGHT（与调用顺序无关），显式 direction 不计入推断。
    assert inferred_swipe_warnings(result) == [
        "swipe direction inferred as UP x2 (from start/end coordinates)",
        "swipe direction inferred as DOWN x1 (from start/end coordinates)",
        "swipe direction inferred as LEFT x1 (from start/end coordinates)",
    ]
    assert not any(warning.startswith("inv-") for warning in result.warnings)
    assert step_by_id(result, "inv-left").direction == "left"
    assert step_by_id(result, "inv-down").direction == "down"
    assert step_by_id(result, "inv-up-1").direction == "up"
    assert step_by_id(result, "inv-explicit").direction == "down"


def test_swipe_with_unusable_coordinates_falls_back_to_up(artifacts: ArtifactStore) -> None:
    result = build_dc(artifacts, [invocation(DcToolName.SWIPE, {"start": "bad", "end": "worse"})])

    assert step_by_id(result, "inv-001").direction == "up"
    assert inferred_swipe_warnings(result) == ["swipe direction inferred as UP x1 (from start/end coordinates)"]


# ---------------------------------------------------------------------------
# KEY_EVENT
# ---------------------------------------------------------------------------


def test_non_back_key_event_is_omitted_with_exact_reason_and_skipped_comment(artifacts: ArtifactStore) -> None:
    result = build_dc(
        artifacts,
        [invocation(DcToolName.KEY_EVENT, {"key": "power"}, invocation_id="inv-power")],
    )

    assert result.omitted_actions == [
        {
            "invocation_id": "inv-power",
            "tool": "key_event",
            # 有意变更的 omit 文案：Enter 等按键现在也能映射了，旧文案
            # 「only Back maps to go_back」已不再成立。
            "reason": "key_event('power') is not replayable (unsupported key)",
        }
    ]
    step = step_by_id(result, "inv-power")
    assert step.action == StepAction.NOOP_COMMENT
    assert step.comment == "skipped: key_event('power')"
    assert result.counts["generated_actions"] == 0


@pytest.mark.parametrize("raw", ["Enter", "enter", "ENTER", " Enter "])
def test_enter_key_event_maps_to_a_key_event_step(artifacts: ArtifactStore, raw: str) -> None:
    """本案第二个必然失败点：提交搜索的 Enter 此前被无谓丢弃，导致脚本走到搜索页也不提交。"""
    result = build_dc(
        artifacts,
        [invocation(DcToolName.KEY_EVENT, {"key": raw}, invocation_id="inv-enter")],
    )

    step = step_by_id(result, "inv-enter")
    assert step.action == StepAction.KEY_EVENT
    assert step.key == "Enter"
    assert result.omitted_actions == []
    assert result.counts["generated_actions"] == 1


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("Home", "Home"),
        ("home", "Home"),
        ("Volume Up", "VolumeUp"),
        ("volume-up", "VolumeUp"),
        ("volumeUp", "VolumeUp"),
        ("volume_up", "VolumeUp"),
        ("VolumeDown", "VolumeDown"),
        ("volume_down", "VolumeDown"),
    ],
)
def test_whitelisted_key_events_use_the_canonical_spelling(artifacts: ArtifactStore, raw: str, canonical: str) -> None:
    result = build_dc(
        artifacts,
        [invocation(DcToolName.KEY_EVENT, {"key": raw}, invocation_id="inv-key")],
    )

    step = step_by_id(result, "inv-key")
    assert step.action == StepAction.KEY_EVENT
    assert step.key == canonical


def test_canonical_key_event_output_passes_the_case_safety_gate(artifacts: ArtifactStore) -> None:
    """规范拼写必须同时通过 IR 校验（否则用例会被静默降级为 draft）。"""
    from harmony_test_agent.cases.safety import validate_case_spec
    from harmony_test_agent.config import Settings

    result = build_dc(
        artifacts,
        [
            invocation(DcToolName.KEY_EVENT, {"key": "enter"}, invocation_id="inv-enter"),
            invocation(DcToolName.KEY_EVENT, {"key": "Volume Up"}, invocation_id="inv-volume"),
        ],
    )

    assert validate_case_spec(result.spec, max_iterations=Settings().stress_max_iterations) == []


@pytest.mark.parametrize("raw", ["Power", "power", "Menu", "KEYCODE_ENTER", "return", ""])
def test_non_whitelisted_key_events_stay_omitted(artifacts: ArtifactStore, raw: str) -> None:
    result = build_dc(
        artifacts,
        [invocation(DcToolName.KEY_EVENT, {"key": raw}, invocation_id="inv-key")],
    )

    assert step_by_id(result, "inv-key").action == StepAction.NOOP_COMMENT
    assert result.omitted_actions[0]["reason"] == f"key_event({raw!r}) is not replayable (unsupported key)"


def test_backspace_key_event_maps_to_a_back_step(artifacts: ArtifactStore) -> None:
    """``backspace`` 与 emitter 层既有归一化一致（``standalone.py`` 也把两者都当 Back）。

    注：这是本次的**可观测行为变更** —— 改前 ``backspace`` 在预过滤里就被当成
    「非 Back 按键」丢弃；语义上「退格 ≠ 返回」仍是已知遗留问题，见 builder 内注释。
    """
    result = build_dc(
        artifacts,
        [invocation(DcToolName.KEY_EVENT, {"key": "backspace"}, invocation_id="inv-backspace")],
    )

    assert step_by_id(result, "inv-backspace").action == StepAction.BACK
    assert result.omitted_actions == []


def test_back_key_event_maps_to_a_back_step(artifacts: ArtifactStore) -> None:
    """计划 A3：``KEY_EVENT`` 且 key 为 ``back`` → ``TestStepSpec(BACK)``（不是省略）。"""
    result = build_dc(
        artifacts,
        [invocation(DcToolName.KEY_EVENT, {"key": "back"}, invocation_id="inv-back")],
    )

    assert [step.action for step in result.spec.steps] == [StepAction.BACK]
    assert step_by_id(result, "inv-back").action == StepAction.BACK
    assert result.omitted_actions == []
    assert result.counts["generated_actions"] == 1
    assert "no replayable operations were recorded" not in result.warnings
    assert all(step.comment != NO_REPLAYABLE_COMMENT for step in result.spec.steps)
    # emitter 后果：BACK 步骤渲染为历史 DC 生成器的 driver.go_back()。
    rendered = StandaloneEmitter().render(result.spec, run_id=SESSION_ID, device_id=DEVICE_ID)
    assert "driver.go_back()" in rendered.python_text


def test_back_key_event_is_case_insensitive(artifacts: ArtifactStore) -> None:
    result = build_dc(
        artifacts,
        [invocation(DcToolName.KEY_EVENT, {"key": "Back"}, invocation_id="inv-back")],
    )

    assert step_by_id(result, "inv-back").action == StepAction.BACK


# ---------------------------------------------------------------------------
# non-replayable 工具
# ---------------------------------------------------------------------------


def test_non_replayable_tool_becomes_skipped_comment_with_exact_reason(artifacts: ArtifactStore) -> None:
    result = build_dc(
        artifacts,
        [invocation(DcToolName.COLLECT_LOGS, {"lines": 100}, invocation_id="inv-logs")],
    )

    assert result.omitted_actions == [
        {
            "invocation_id": "inv-logs",
            "tool": "collect_logs",
            "reason": "not replayable in Hypium (observation/management/shell)",
        }
    ]
    step = step_by_id(result, "inv-logs")
    assert step.action == StepAction.NOOP_COMMENT
    assert step.comment.startswith("skipped: collect_logs")


def test_non_replayable_tool_comment_payload_starts_with_skipped_tool_value(artifacts: ArtifactStore) -> None:
    tools = [
        DcToolName.SCREENSHOT,
        DcToolName.DUMP_UI_HIERARCHY,
        DcToolName.EXECUTE_SHELL,
        DcToolName.CLEAR_APP_DATA,
    ]
    invocations = [invocation(tool, {"argv": ["ls"]}, invocation_id=f"inv-{index}") for index, tool in enumerate(tools)]

    result = build_dc(artifacts, invocations)

    for index, tool in enumerate(tools):
        step = step_by_id(result, f"inv-{index}")
        assert step.action == StepAction.NOOP_COMMENT
        assert step.comment.startswith(f"skipped: {tool.value}")
        assert result.omitted_actions[index]["reason"] == "not replayable in Hypium (observation/management/shell)"
    assert result.counts["generated_actions"] == 0


# ---------------------------------------------------------------------------
# 断言映射与「DC 从不注入兜底断言」
# ---------------------------------------------------------------------------


def test_dc_assert_visible_with_resolved_element_keeps_key_locator(artifacts: ArtifactStore) -> None:
    result = build_dc(
        artifacts,
        [
            invocation(
                DcToolName.ASSERT_VISIBLE,
                {"target": "搜索"},
                resolved_element=element(key="home_search"),
            )
        ],
    )

    checkpoint = checkpoints_of(result)[0]
    assert checkpoint.kind == CheckpointKind.ELEMENT_EXISTS
    assert checkpoint.locator is not None
    assert checkpoint.locator.kind == LocatorKind.KEY
    assert checkpoint.locator.evidence is not None
    assert checkpoint.locator.evidence.source == "dc_resolved_element"
    assert result.explicit_assertions == 1


def test_dc_assert_text_uses_text_equals_with_element_and_text_contains_without(artifacts: ArtifactStore) -> None:
    """与 Live 侧同规则：KEY/ID → 精确 ``text=``；无结构化定位器 → 模糊包含匹配。"""
    with_element = build_dc(
        artifacts,
        [
            invocation(
                DcToolName.ASSERT_TEXT,
                {"target": "OpenHarmony"},
                resolved_element=element(key="search_input"),
            )
        ],
    )
    without_element = build_dc(
        artifacts,
        [invocation(DcToolName.ASSERT_TEXT, {"target": "OpenHarmony"})],
    )

    exact = checkpoints_of(with_element)[0]
    assert exact.kind == CheckpointKind.TEXT_EQUALS
    assert exact.expected == "OpenHarmony"
    assert exact.locator is not None
    assert exact.locator.kind == LocatorKind.KEY

    fuzzy = checkpoints_of(without_element)[0]
    assert fuzzy.kind == CheckpointKind.TEXT_CONTAINS
    assert fuzzy.expected == "OpenHarmony"
    assert fuzzy.locator is None


def test_dc_never_injects_a_fallback_assertion(artifacts: ArtifactStore) -> None:
    """DC 生成器用 ``inject_fallback_assertion=False``：即使有稳定元素也不补断言。"""
    invocations = [invocation(DcToolName.CLICK, {"x": 10, "y": 20}, invocation_id="inv-click")]
    snapshots = [snapshot()]

    result = build_dc(artifacts, invocations, snapshots=snapshots)

    assert result.explicit_assertions == 0
    assert not any(step.action == StepAction.CHECK for step in result.spec.steps)
    assert not any("fallback assertion" in warning for warning in result.warnings)
    assert "source trace has no successful explicit assertion" not in result.warnings
    assert result.counts["generated_assertions"] == 0

    direct = CaseBuilder(inject_fallback_assertion=False).from_dc_invocations(
        SESSION_ID,
        DEVICE_ID,
        invocations,
        bundle_name="com.demo.app",
        main_ability="MainAbility",
        snapshots=snapshots,
    )
    assert [step.action for step in direct.spec.steps] == [step.action for step in result.spec.steps]
    assert direct.warnings == result.warnings
    assert direct.counts == result.counts


def test_dc_generator_builds_the_case_ir_with_fallback_injection_disabled(
    artifacts: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """直接断言「``inject_fallback_assertion=False`` 被使用」这一构造契约。"""
    recorded: dict[str, Any] = {}
    original_init = CaseBuilder.__init__

    def spy(self: CaseBuilder, *, min_observed_rounds: int = 3, inject_fallback_assertion: bool = True) -> None:
        recorded["inject_fallback_assertion"] = inject_fallback_assertion
        original_init(
            self, min_observed_rounds=min_observed_rounds, inject_fallback_assertion=inject_fallback_assertion
        )

    monkeypatch.setattr(CaseBuilder, "__init__", spy)

    build_dc(artifacts, eligible_invocations())

    assert recorded["inject_fallback_assertion"] is False


# ---------------------------------------------------------------------------
# replay_eligible 三条件规则
# ---------------------------------------------------------------------------


def eligible_invocations() -> list[DcToolInvocation]:
    return [
        invocation(
            DcToolName.ASSERT_VISIBLE,
            {"target": "搜索"},
            invocation_id="inv-assert",
            resolved_element=element(key="home_search"),
        ),
        invocation(DcToolName.CLICK, {"x": 10, "y": 20}, invocation_id="inv-click"),
    ]


def test_replay_eligible_baseline_is_true(artifacts: ArtifactStore) -> None:
    result = build_dc(artifacts, eligible_invocations())

    assert asserted_replay_rule(result, bundle_name="com.demo.app", main_ability="MainAbility") is True
    assert result.replay_eligible is True
    assert result.purpose == "acceptance"


def test_no_assertion_only_lowers_confidence_to_medium(artifacts: ArtifactStore) -> None:
    """缺显式断言从阻断条件降为质量因素：脚本照样可执行，只是没有检查点。"""
    result = build_dc(artifacts, [invocation(DcToolName.CLICK, {"x": 10, "y": 20}, invocation_id="inv-click")])

    assert asserted_replay_rule(result, bundle_name="com.demo.app", main_ability="MainAbility") is True
    assert result.replay_eligible is True
    assert result.purpose == "acceptance"
    assert result.explicit_assertions == 0
    assert result.confidence == "medium"
    assert result.confidence_factors == ["no explicit assert_* tool call was recorded"]


def test_replay_eligible_fails_with_a_failed_assert_tool(artifacts: ArtifactStore) -> None:
    result = build_dc(
        artifacts,
        [
            invocation(
                DcToolName.ASSERT_VISIBLE,
                {"target": "不存在的元素"},
                success=False,
                invocation_id="inv-assert",
            )
        ],
    )

    assert result.explicit_assertions == 0
    # 唯一一次调用失败 ⇒ 没有任何可回放动作 ⇒ 命中 G3 第一条。
    assert result.replay_eligible is False
    assert result.runnable_blockers == ["script has no replayable action"]


def test_replay_eligible_fails_with_a_placeholder_bundle(artifacts: ArtifactStore) -> None:
    result = build_dc(artifacts, eligible_invocations(), bundle_name=PLACEHOLDER_BUNDLE)

    assert asserted_replay_rule(result, bundle_name=PLACEHOLDER_BUNDLE, main_ability="MainAbility") is False
    assert result.replay_eligible is False
    assert result.runnable_blockers == [f"app identity is a placeholder ({PLACEHOLDER_BUNDLE}/{PLACEHOLDER_ABILITY})"]


def test_entry_ability_is_a_real_identity_and_stays_runnable(artifacts: ArtifactStore) -> None:
    """``EntryAbility`` 是鸿蒙工程的默认且常见的**真实** ability 名。

    计划的 G3 原文把它写成占位哨兵，但那会让本案真实运行
    （``com.github.zhuoyi233.zhplus / EntryAbility``）永远不可执行，直接违背 G1。
    因此占位判定只认 bundle 哨兵；这里把该决策钉成回归。
    """
    result = build_dc(artifacts, eligible_invocations(), main_ability=PLACEHOLDER_ABILITY)

    assert asserted_replay_rule(result, bundle_name="com.demo.app", main_ability=PLACEHOLDER_ABILITY) is True
    assert result.replay_eligible is True
    assert result.runnable_blockers == []


def test_replay_eligible_fails_without_included_operations(artifacts: ArtifactStore) -> None:
    """``included_count > 0`` 条件：只有不可回放工具时不合格。

    注：成功的 ``assert_*`` 调用本身也计入 ``included_count``（``_dc_step`` 对断言返回
    ``counted=True``），因此这一条件无法脱离「至少一条成功断言」单独失败；这里覆盖
    ``included_count == 0`` 的边界。
    """
    result = build_dc(artifacts, [invocation(DcToolName.COLLECT_LOGS, {}, invocation_id="inv-logs")])

    assert result.counts["generated_actions"] == 0
    assert asserted_replay_rule(result, bundle_name="com.demo.app", main_ability="MainAbility") is False
    assert result.replay_eligible is False


# ---------------------------------------------------------------------------
# 无可回放操作
# ---------------------------------------------------------------------------


def test_no_replayable_operations_warning_and_comment_step(artifacts: ArtifactStore) -> None:
    result = build_dc(artifacts, [invocation(DcToolName.COLLECT_LOGS, {}, invocation_id="inv-logs")])

    assert "no replayable operations were recorded" in result.warnings
    comments = [step.comment for step in result.spec.steps if step.action == StepAction.NOOP_COMMENT]
    # non-replayable 的 # skipped 注释在前，末尾再补一条「无可回放操作」。
    assert comments == ["skipped: collect_logs", NO_REPLAYABLE_COMMENT]
    assert result.spec.steps[-1].action == StepAction.NOOP_COMMENT


def test_empty_recording_reports_no_replayable_operations(artifacts: ArtifactStore) -> None:
    result = build_dc(artifacts, [])

    assert result.counts["source_actions"] == 0
    assert result.counts["generated_actions"] == 0
    assert "no replayable operations were recorded" in result.warnings
    assert [step.comment for step in result.spec.steps] == [NO_REPLAYABLE_COMMENT]
    assert result.replay_eligible is False


def test_replayable_recording_has_no_no_replayable_warning(artifacts: ArtifactStore) -> None:
    result = build_dc(artifacts, eligible_invocations())

    assert "no replayable operations were recorded" not in result.warnings
    assert all(step.comment != NO_REPLAYABLE_COMMENT for step in result.spec.steps)


# ---------------------------------------------------------------------------
# 断言证据门禁（DC 侧）
# ---------------------------------------------------------------------------

DC_IDENTIFIER_TARGET = "p2_channel_content_question_2085141629112009975"


def test_dc_assert_visible_without_evidence_becomes_a_soft_key_checkpoint(artifacts: ArtifactStore) -> None:
    """无据标识符 target：不得渲染成恒假的 ``BY.text(标识符)``，改用 soft 的 ``BY.key``。"""
    result = build_dc(
        artifacts,
        [invocation(DcToolName.ASSERT_VISIBLE, {"target": DC_IDENTIFIER_TARGET}, invocation_id="inv-assert")],
    )

    checkpoint = checkpoints_of(result)[0]
    assert checkpoint.kind == CheckpointKind.ELEMENT_EXISTS
    assert checkpoint.soft is True
    assert checkpoint.locator is not None
    assert checkpoint.locator.kind == LocatorKind.KEY
    assert checkpoint.locator.value == DC_IDENTIFIER_TARGET
    assert any("has no component evidence" in warning for warning in result.warnings)
    # soft 仍然产出了一个检查点 ⇒ 计入 included_count（与历史口径一致）。
    assert result.counts["generated_actions"] == 1
    assert result.explicit_assertions == 1
    assert result.runnable_blockers == []

    rendered = StandaloneEmitter().render(result.spec, run_id=SESSION_ID, device_id=DEVICE_ID)
    assert f"BY.key({DC_IDENTIFIER_TARGET!r})" in rendered.python_text
    assert f"BY.text({DC_IDENTIFIER_TARGET!r})" not in rendered.python_text
    assert "result['soft_failures'].append(" in rendered.python_text


def test_dc_assert_visible_without_element_keeps_human_readable_text_locator(artifacts: ArtifactStore) -> None:
    """CJK target 不触发门禁：仍是硬的 ``BY.text`` 锚点（边界行为不变）。"""
    result = build_dc(
        artifacts,
        [invocation(DcToolName.ASSERT_VISIBLE, {"target": "搜索"}, invocation_id="inv-assert")],
    )

    checkpoint = checkpoints_of(result)[0]
    assert checkpoint.soft is False
    assert checkpoint.locator is not None
    assert checkpoint.locator.kind == LocatorKind.TEXT
    assert checkpoint.locator.value == "搜索"


def test_dc_assert_not_visible_without_evidence_is_omitted_not_softened(artifacts: ArtifactStore) -> None:
    """无据的 ``expect_exist=False`` 恒真：soft 化等于静默放行，因此省略。"""
    result = build_dc(
        artifacts,
        [invocation(DcToolName.ASSERT_NOT_VISIBLE, {"target": DC_IDENTIFIER_TARGET}, invocation_id="inv-absent")],
    )

    assert result.omitted_actions == [
        {
            "invocation_id": "inv-absent",
            "tool": "assert_not_visible",
            "reason": UNGROUNDED_ABSENT_ASSERTION_OMIT_REASON,
        }
    ]
    assert checkpoints_of(result) == []
    # DC 侧的 ``explicit_assertions`` 计的是**录制到几次断言调用**（``builder.py:967``），
    # 与被省略与否无关；这里如实钉住该既有口径，不改动它。
    assert result.counts["source_assertions"] == 1
    assert result.explicit_assertions == 1
    # 「什么都没产出」⇒ 不虚增 included_count ⇒ 命中「无可回放动作」这条物理必要条件。
    assert result.counts["generated_actions"] == 0
    assert result.runnable_blockers == ["script has no replayable action"]
    assert result.replay_eligible is False
    assert any("vacuously true" in warning for warning in result.warnings)


def test_dc_assert_not_visible_with_human_readable_target_stays_a_hard_absent_checkpoint(
    artifacts: ArtifactStore,
) -> None:
    """CJK 缺席断言保持原状：硬 ``ELEMENT_ABSENT`` + ``BY.text``。"""
    result = build_dc(
        artifacts,
        [invocation(DcToolName.ASSERT_NOT_VISIBLE, {"target": "加载中"}, invocation_id="inv-absent")],
    )

    checkpoint = checkpoints_of(result)[0]
    assert checkpoint.kind == CheckpointKind.ELEMENT_ABSENT
    assert checkpoint.soft is False
    assert checkpoint.locator is not None
    assert checkpoint.locator.kind == LocatorKind.TEXT
    assert result.omitted_actions == []


def test_dc_assert_text_with_identifier_expected_becomes_soft(artifacts: ArtifactStore) -> None:
    """``expected`` 本身是无据标识符时，``TEXT_CONTAINS`` 也恒假 ⇒ 降级为 soft。"""
    result = build_dc(
        artifacts,
        [invocation(DcToolName.ASSERT_TEXT, {"target": DC_IDENTIFIER_TARGET}, invocation_id="inv-text")],
    )

    checkpoint = checkpoints_of(result)[0]
    assert checkpoint.kind == CheckpointKind.TEXT_CONTAINS
    assert checkpoint.soft is True
    assert checkpoint.locator is None
    assert checkpoint.message_zh
    assert any("is identifier-shaped and appears in no captured frame" in warning for warning in result.warnings)


def test_dc_assert_text_with_human_readable_expected_stays_hard(artifacts: ArtifactStore) -> None:
    result = build_dc(
        artifacts,
        [invocation(DcToolName.ASSERT_TEXT, {"target": "OpenHarmony"}, invocation_id="inv-text")],
    )

    checkpoint = checkpoints_of(result)[0]
    assert checkpoint.kind == CheckpointKind.TEXT_CONTAINS
    assert checkpoint.soft is False


def test_dc_identifier_target_present_as_literal_text_in_a_frame_is_not_gated(artifacts: ArtifactStore) -> None:
    """帧里确有该字面文本 ⇒ 有证据 ⇒ 保持硬检查点。

    证据必须是**真实文本**（TEXT 定位候选，``perception/normalizer.py`` 只在原始 ``text``
    非空时才产出它），不能只写合成的 ``content`` —— 后者在 text 为空时会退化成 key，
    拿它当证据就会放行一个设备上永不命中的 ``BY.text(标识符)``。
    """
    frame = ScreenSnapshot(
        snapshot_id="snap-login",
        run_id=SESSION_ID,
        image_path=Path("screens/snap-login.png"),
        image_sha256="abc",
        width=1080,
        height=2340,
        elements=[
            UIElement(
                element_id="ui-1",
                key="some_key",
                content="log-in",
                locator_candidates=[LocatorCandidate(kind=LocatorKind.TEXT, value="log-in", score=0.9)],
            )
        ],
    )

    result = build_dc(
        artifacts,
        [invocation(DcToolName.ASSERT_VISIBLE, {"target": "log-in"}, invocation_id="inv-assert")],
        snapshots=[frame],
    )

    checkpoint = checkpoints_of(result)[0]
    assert checkpoint.soft is False
    assert checkpoint.locator is not None
    assert checkpoint.locator.kind == LocatorKind.TEXT
    assert checkpoint.locator.value == "log-in"


def test_dc_identifier_target_held_only_as_a_key_is_recovered_as_by_key(artifacts: ArtifactStore) -> None:
    """真机复盘 dc-20260923T180535Z-1bed642e：target 是帧里真实存在的 key、text 为空串。

    ``content`` 被 normalizer 合成成 key 本身，旧实现据此误判「屏幕上有这段文字」并渲染
    ``BY.text('p2_answer_detail_page')``；正确结果是恢复成 ``BY.key(...)`` 的**硬**检查点。
    """
    target = "p2_answer_detail_page"
    frame = ScreenSnapshot(
        snapshot_id="snap-detail",
        run_id=SESSION_ID,
        image_path=Path("screens/snap-detail.png"),
        image_sha256="abc",
        width=1320,
        height=2232,
        elements=[
            UIElement(
                element_id="ui-detail",
                key=target,
                id=target,
                content=target,
                locator_candidates=[LocatorCandidate(kind=LocatorKind.KEY, value=target, score=1)],
            )
        ],
    )

    result = build_dc(
        artifacts,
        [invocation(DcToolName.ASSERT_VISIBLE, {"target": target}, invocation_id="inv-assert")],
        snapshots=[frame],
        bundle_name="com.github.zhuoyi233.zhplus",
    )

    checkpoint = checkpoints_of(result)[0]
    assert checkpoint.kind == CheckpointKind.ELEMENT_EXISTS
    assert checkpoint.soft is False
    assert checkpoint.locator is not None
    assert checkpoint.locator.kind == LocatorKind.KEY
    assert checkpoint.locator.value == target
    assert f"semantic target {target!r} resolved to key:{target!r} from the captured frames" in result.warnings


# ---------------------------------------------------------------------------
# 外来窗口（输入法 / 桌面 / 系统 UI）的点击不得变成硬回放步骤
# 真机复盘 dc-20260923T180535Z-1bed642e：模型按 Enter 提交搜索失败后改点软键盘上的搜索键，
# 命中 com.huawei.hmos.inputmethod 窗口下的 index_keyMenu_container。键盘是瞬时浮层，
# 回放时早已收起 ⇒ BY.key(...) 必然 Can't find component，坐标兜底也一样会点到空白处。
# ---------------------------------------------------------------------------

APP_BUNDLE = "com.github.zhuoyi233.zhplus"
IME_BUNDLE = "com.huawei.hmos.inputmethod"
IME_KEY = "index_keyMenu_container"


def owned_element(key: str, bundle: str) -> UIElement:
    """带归属窗口信息的元素（``normalize_layout`` 从窗口 root 继承 bundleName 后写入 metadata）。"""
    return UIElement(
        element_id=f"ui-{key}",
        key=key,
        id=key,
        content=key,
        clickable=True,
        bbox=BoundingBox(left=0, top=1443, right=1320, bottom=2097),
        metadata={"bundle_name": bundle},
    )


def test_click_on_an_input_method_element_is_omitted(artifacts: ArtifactStore) -> None:
    result = build_dc(
        artifacts,
        [invocation(DcToolName.CLICK, {"x": 1225, "y": 1939}, resolved_element=owned_element(IME_KEY, IME_BUNDLE))],
        bundle_name=APP_BUNDLE,
    )

    assert IME_KEY not in StandaloneEmitter().render(result.spec, run_id=SESSION_ID, device_id=DEVICE_ID).python_text
    assert not [step for step in result.spec.steps if step.action == StepAction.CLICK]
    assert result.omitted_actions[0]["reason"] == f"click landed on {IME_BUNDLE}, which is not the app under test"
    assert any("input method / system overlay" in warning for warning in result.warnings)


def test_click_on_a_foreign_element_never_falls_back_to_coordinate(artifacts: ArtifactStore) -> None:
    """坐标兜底同样危险：那一下只会点到键盘原本所在的空白处，因此外来元素必须整条省略。"""
    result = build_dc(
        artifacts,
        [invocation(DcToolName.CLICK, {"x": 1225, "y": 1939}, resolved_element=owned_element(IME_KEY, IME_BUNDLE))],
        bundle_name=APP_BUNDLE,
    )

    assert not [step for step in result.spec.steps if step.locator is not None]
    assert result.counts["coordinate_fallbacks"] == 0


def test_input_text_on_a_foreign_element_is_omitted(artifacts: ArtifactStore) -> None:
    result = build_dc(
        artifacts,
        [
            invocation(
                DcToolName.INPUT_TEXT,
                {"text": "zcode", "coordinate": [660, 1900]},
                resolved_element=owned_element("ime_search_box", IME_BUNDLE),
            )
        ],
        bundle_name=APP_BUNDLE,
    )

    assert not [step for step in result.spec.steps if step.action == StepAction.INPUT_TEXT]
    assert result.omitted_actions[0]["tool"] == "input_text"


def test_click_on_the_app_under_test_is_kept(artifacts: ArtifactStore) -> None:
    result = build_dc(
        artifacts,
        [
            invocation(
                DcToolName.CLICK,
                {"x": 585, "y": 202},
                resolved_element=owned_element("p2_home_titlebar_search", APP_BUNDLE),
            )
        ],
        bundle_name=APP_BUNDLE,
    )

    steps = [step for step in result.spec.steps if step.action == StepAction.CLICK]
    assert len(steps) == 1
    assert steps[0].locator is not None
    assert steps[0].locator.value == "p2_home_titlebar_search"
    assert result.omitted_actions == []


def test_element_without_ownership_metadata_is_kept(artifacts: ArtifactStore) -> None:
    """判不了就不判：历史快照的 metadata 里没有 bundle_name，此时保持既有行为。"""
    result = build_dc(
        artifacts,
        [invocation(DcToolName.CLICK, {"x": 10, "y": 20}, resolved_element=element(key="some_control"))],
        bundle_name=APP_BUNDLE,
    )

    assert len([step for step in result.spec.steps if step.action == StepAction.CLICK]) == 1
    assert result.omitted_actions == []


def test_ownership_is_recovered_from_the_captured_frames(artifacts: ArtifactStore) -> None:
    """``resolved_element`` 是录制当时拷的副本，早于 bundle_name 字段的会话要靠回查帧补齐。"""
    stale = UIElement(
        element_id="ui-stale",
        key=IME_KEY,
        id=IME_KEY,
        content=IME_KEY,
        clickable=True,
        bbox=BoundingBox(left=0, top=1443, right=1320, bottom=2097),
        metadata={"page_path": "", "hierarchy": "ROOT41,0,0,1"},
    )
    frame = ScreenSnapshot(
        snapshot_id="snap-ime",
        run_id=SESSION_ID,
        image_path=Path("screens/snap-ime.png"),
        image_sha256="abc",
        width=1320,
        height=2232,
        elements=[owned_element(IME_KEY, IME_BUNDLE).model_copy(update={"element_id": "ui-stale"})],
    )

    result = build_dc(
        artifacts,
        [invocation(DcToolName.CLICK, {"x": 1225, "y": 1939}, resolved_element=stale)],
        snapshots=[frame],
        bundle_name=APP_BUNDLE,
    )

    assert not [step for step in result.spec.steps if step.action == StepAction.CLICK]
    assert any(IME_BUNDLE in warning for warning in result.warnings)


def test_placeholder_bundle_disables_the_foreign_filter(artifacts: ArtifactStore) -> None:
    """身份还是占位值时**不得**过滤：否则「什么都不等于占位 bundle」会把整份脚本清空。"""
    result = build_dc(
        artifacts,
        [invocation(DcToolName.CLICK, {"x": 1225, "y": 1939}, resolved_element=owned_element(IME_KEY, IME_BUNDLE))],
        bundle_name=PLACEHOLDER_BUNDLE,
    )

    assert len([step for step in result.spec.steps if step.action == StepAction.CLICK]) == 1
    assert result.omitted_actions == []
