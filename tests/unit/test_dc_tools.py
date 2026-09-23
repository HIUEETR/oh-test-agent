"""DC 工具注册与 build_tools 测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.usage import RunUsage

from harmony_test_agent.dc.models import (
    SIDE_EFFECT_TOOLS,
    TIER_TOOLS,
    TOOL_TIER,
    DcToolName,
    DcToolStatus,
    DcToolTier,
    tools_up_to,
)
from harmony_test_agent.dc.tools import (
    _TOOL_REGISTRY,
    DcActionRecorder,
    DcSnapshotHolder,
    DcToolContext,
    _resolve_element,
    build_tools,
    tool_assert_not_visible,
    tool_assert_text,
    tool_assert_visible,
    tool_click,
    tool_input_text,
    tool_start_app,
)
from harmony_test_agent.models import BoundingBox, CommandResult, ScreenSnapshot, UIElement

ASSERT_TOOLS = (DcToolName.ASSERT_VISIBLE, DcToolName.ASSERT_NOT_VISIBLE, DcToolName.ASSERT_TEXT)
BUNDLE = "com.github.zhuoyi233.zhplus"


class TestTierTools:
    """TIER_TOOLS 字典是 tier→tool 映射的单一数据源。"""

    def test_l1_has_11_tools(self) -> None:
        assert len(TIER_TOOLS[DcToolTier.L1]) == 11

    def test_l2_has_7_tools(self) -> None:
        assert len(TIER_TOOLS[DcToolTier.L2]) == 7

    def test_l3_has_4_tools(self) -> None:
        assert len(TIER_TOOLS[DcToolTier.L3]) == 4

    def test_start_app_is_available_at_l2(self) -> None:
        """start_app 必须在默认层可用：会话身份（bundle/ability）只能由显式启动留下。

        回归背景（30e 复盘）：默认 L2 下没有 start_app，模型只能靠 foreground_app
        记录身份，而设备对前台应用的 ability 常报 unknown，导致脚本生成与蒸馏都拿不到
        真实 bundle/ability。
        """
        assert DcToolName.START_APP in TIER_TOOLS[DcToolTier.L2]
        assert DcToolName.START_APP in tools_up_to(DcToolTier.L2)
        assert DcToolName.START_APP not in tools_up_to(DcToolTier.L1)
        assert DcToolName.FORCE_STOP_APP not in tools_up_to(DcToolTier.L2)

    def test_l4_has_3_tools(self) -> None:
        assert len(TIER_TOOLS[DcToolTier.L4]) == 3

    def test_l5_has_1_tool(self) -> None:
        assert len(TIER_TOOLS[DcToolTier.L5]) == 1

    def test_total_26_tools(self) -> None:
        total = sum(len(tools) for tools in TIER_TOOLS.values())
        assert total == 26

    def test_all_tool_names_unique(self) -> None:
        all_tools = [tool for tools in TIER_TOOLS.values() for tool in tools]
        assert len(all_tools) == len(set(all_tools))

    def test_tool_tier_reverse_mapping(self) -> None:
        for tier, tools in TIER_TOOLS.items():
            for tool in tools:
                assert TOOL_TIER[tool] == tier


class TestToolsUpTo:
    """tools_up_to 返回 level ≤ tier 的全部工具。"""

    def test_l1_returns_11(self) -> None:
        assert len(tools_up_to(DcToolTier.L1)) == 11

    def test_l2_returns_18(self) -> None:
        assert len(tools_up_to(DcToolTier.L2)) == 18

    def test_l3_returns_22(self) -> None:
        assert len(tools_up_to(DcToolTier.L3)) == 22

    def test_l4_returns_25(self) -> None:
        assert len(tools_up_to(DcToolTier.L4)) == 25

    def test_l5_returns_26(self) -> None:
        assert len(tools_up_to(DcToolTier.L5)) == 26

    def test_l1_contains_click(self) -> None:
        assert DcToolName.CLICK in tools_up_to(DcToolTier.L1)

    def test_l5_contains_execute_shell(self) -> None:
        assert DcToolName.EXECUTE_SHELL in tools_up_to(DcToolTier.L5)

    def test_l1_does_not_contain_execute_shell(self) -> None:
        assert DcToolName.EXECUTE_SHELL not in tools_up_to(DcToolTier.L1)


class TestToolRegistry:
    """_TOOL_REGISTRY 必须包含全部 26 个工具。"""

    def test_registry_has_26_entries(self) -> None:
        assert len(_TOOL_REGISTRY) == 26

    def test_every_tool_name_in_registry(self) -> None:
        for tool_name in DcToolName:
            assert tool_name in _TOOL_REGISTRY, f"{tool_name} missing from registry"

    def test_registry_entries_are_callable(self) -> None:
        for tool_name, (func, desc) in _TOOL_REGISTRY.items():
            assert callable(func), f"{tool_name} function is not callable"
            assert isinstance(desc, str) and len(desc) > 0, f"{tool_name} description is empty"


class TestBuildTools:
    """build_tools 返回 pydantic-ai Tool 对象列表。"""

    def test_l1_returns_11_tools(self) -> None:
        tools = build_tools(DcToolTier.L1)
        assert len(tools) == 11

    def test_l5_returns_26_tools(self) -> None:
        tools = build_tools(DcToolTier.L5)
        assert len(tools) == 26

    def test_tool_names_match_tier(self) -> None:
        tools = build_tools(DcToolTier.L2)
        tool_names = {t.name for t in tools}
        expected = {name.value for name in tools_up_to(DcToolTier.L2)}
        assert tool_names == expected

    def test_l1_does_not_include_shell(self) -> None:
        tools = build_tools(DcToolTier.L1)
        tool_names = {t.name for t in tools}
        assert "execute_shell" not in tool_names

    def test_l5_includes_shell(self) -> None:
        tools = build_tools(DcToolTier.L5)
        tool_names = {t.name for t in tools}
        assert "execute_shell" in tool_names


class TestAssertionTools:
    """Phase 3（2026-09-17）：3 个 DC 断言工具。"""

    def test_assertion_tools_are_registered_at_l1(self) -> None:
        l1 = {tool.value for tool in tools_up_to(DcToolTier.L1)}
        assert {tool.value for tool in ASSERT_TOOLS} <= l1

    def test_assertion_tools_have_no_device_side_effect(self) -> None:
        """断言是只读 UI 检查：不得进入 SIDE_EFFECT_TOOLS（否则超时后会被判为副作用未知）。"""
        assert not (set(ASSERT_TOOLS) & SIDE_EFFECT_TOOLS)

    def test_assertion_tool_schemas_expose_target(self) -> None:
        tools = {tool.name: tool for tool in build_tools(DcToolTier.L1)}
        for name in ASSERT_TOOLS:
            schema = tools[name.value].function_schema
            assert "target" in schema.json_schema["properties"]


def _snapshot(elements: list[UIElement], *, title: str = "", summary: str = "") -> ScreenSnapshot:
    return ScreenSnapshot(
        snapshot_id="snap-assert",
        run_id="dc-test",
        image_path=Path("snap.png"),
        image_sha256="sha",
        width=1080,
        height=1920,
        page_path="pages/Home",
        page_title=title,
        summary=summary,
        elements=elements,
    )


def _element(key: str, content: str) -> UIElement:
    return UIElement(
        element_id=key,
        key=key,
        content=content,
        clickable=True,
        bbox=BoundingBox(left=0, top=0, right=100, bottom=50),
    )


class _FakeHdc:
    """最小 HDC 替身：只为 tool_screenshot 提供一帧 JPEG。"""

    def __init__(self) -> None:
        self.capture_calls = 0

    def screenshot_jpeg(self, screens_dir: Path, name: str, on_phase=None):  # type: ignore[no-untyped-def]
        del on_phase
        self.capture_calls += 1
        screens_dir.mkdir(parents=True, exist_ok=True)
        path = screens_dir / f"{name}.jpeg"
        path.write_bytes(b"fake-jpeg")
        return path, b"fake-jpeg", 1080, 1920


class _FakeDevice:
    """最小设备替身：screenshot 返回预先准备的帧，click/input_text 只记账不产生副作用。"""

    def __init__(self, snapshot: ScreenSnapshot) -> None:
        self.snapshot = snapshot
        self.clicks: list[tuple[int, int]] = []
        self.inputs: list[tuple[str, int | None, int | None]] = []

    def screenshot(self, output_dir: Path, run_id: str, label: str = "screen") -> ScreenSnapshot:
        del output_dir, run_id, label
        return self.snapshot

    def click(self, x: int, y: int) -> None:
        self.clicks.append((x, y))

    def input_text(self, text: str, x: int | None = None, y: int | None = None) -> None:
        self.inputs.append((text, x, y))


def _context(
    tmp_path: Path,
    snapshot: ScreenSnapshot | None,
    *,
    captured: ScreenSnapshot | None = None,
) -> RunContext[DcToolContext]:
    """构造只含断言工具所需依赖的 RunContext。

    ``session_dir`` 必须指向临时目录：自动采集帧会在 ``session_dir/screens`` 下落盘，
    用仓库根目录会留下测试残渣。
    """
    holder = DcSnapshotHolder()
    holder.latest = snapshot
    deps = DcToolContext(
        session_id="dc-test",
        device=_FakeDevice(captured or snapshot or _snapshot([])),  # type: ignore[arg-type]
        hdc=_FakeHdc(),  # type: ignore[arg-type]
        safety=Any,  # type: ignore[arg-type]
        recorder=DcActionRecorder(),
        artifacts=Any,  # type: ignore[arg-type]
        session_dir=tmp_path,
        snapshot_holder=holder,
        tier=DcToolTier.L1,
    )
    # 断言工具只读取 deps；model/usage 是 RunContext 的形式参数，用占位值即可。
    return RunContext(deps=deps, model=None, usage=RunUsage())  # type: ignore[arg-type]


def test_assert_visible_success(tmp_path: Path) -> None:
    """命中 UI 元素 → 返回断言消息，账本记为 succeeded。"""
    ctx = _context(tmp_path, _snapshot([_element("home_search", "搜索")]))

    message = asyncio.run(tool_assert_visible(ctx, "搜索"))

    assert "assertion passed" in message
    invocation = ctx.deps.recorder.invocations[-1]
    assert invocation.tool == DcToolName.ASSERT_VISIBLE
    assert invocation.status == DcToolStatus.SUCCEEDED
    assert invocation.success is True


def test_assert_visible_failure_records_failed_invocation(tmp_path: Path) -> None:
    """目标不存在 → 工具返回失败摘要，账本记为 failed（不静默通过）。"""
    ctx = _context(tmp_path, _snapshot([_element("home_search", "搜索")]))

    message = asyncio.run(tool_assert_visible(ctx, "不存在的元素"))

    invocation = ctx.deps.recorder.invocations[-1]
    assert invocation.tool == DcToolName.ASSERT_VISIBLE
    assert invocation.status == DcToolStatus.FAILED
    assert invocation.success is False
    assert "failed" in message


def test_assert_not_visible_success(tmp_path: Path) -> None:
    ctx = _context(tmp_path, _snapshot([_element("home_search", "搜索")]))

    message = asyncio.run(tool_assert_not_visible(ctx, "加载中"))

    assert "assertion passed" in message
    assert ctx.deps.recorder.invocations[-1].success is True


def test_assert_text_matches_explicit_element(tmp_path: Path) -> None:
    """严格断言：元素命中即通过。"""
    ctx = _context(tmp_path, _snapshot([_element("result_title", "OpenHarmony")]))

    message = asyncio.run(tool_assert_text(ctx, "OpenHarmony"))

    assert "UI element" in message
    assert ctx.deps.recorder.invocations[-1].success is True


def test_assert_text_ignores_page_summary_fallback(tmp_path: Path) -> None:
    """页面摘要含目标但元素不含 → ASSERT_TEXT 仍失败（严格语义，与 Live Mode 一致）。"""
    ctx = _context(tmp_path, _snapshot([_element("home_search", "搜索")], summary="OpenHarmony 首页"))

    message = asyncio.run(tool_assert_text(ctx, "OpenHarmony"))

    assert ctx.deps.recorder.invocations[-1].success is False
    assert "failed" in message


def test_assert_visible_falls_back_to_page_summary(tmp_path: Path) -> None:
    """可见性断言允许页面摘要兜底（与 Live Mode 一致）。"""
    ctx = _context(tmp_path, _snapshot([_element("home_search", "搜索")], summary="OpenHarmony 首页"))

    message = asyncio.run(tool_assert_visible(ctx, "OpenHarmony"))

    assert "page summary" in message
    assert ctx.deps.recorder.invocations[-1].success is True


def test_assertion_without_snapshot_captures_one(tmp_path: Path) -> None:
    """无帧可判时自动采集一帧（并经 recorder 落账），而不是直接判失败。"""
    ctx = _context(tmp_path, None, captured=_snapshot([_element("home_search", "搜索")]))

    message = asyncio.run(tool_assert_visible(ctx, "搜索"))

    assert ctx.deps.hdc.capture_calls == 1
    assert ctx.deps.snapshot_holder.latest is not None
    assert "assertion passed" in message
    # 采集帧与断言各自落账（SCREENSHOT + ASSERT_VISIBLE）
    assert [inv.tool for inv in ctx.deps.recorder.invocations] == [
        DcToolName.SCREENSHOT,
        DcToolName.ASSERT_VISIBLE,
    ]
    assert ctx.deps.recorder.invocations[-1].page_path == "pages/Home"


def test_page_path_is_recorded_from_latest_snapshot(tmp_path: Path) -> None:
    """工具调用落账时补录 page_path（DC 蒸馏的页面覆盖校验依赖它）。"""
    ctx = _context(tmp_path, _snapshot([_element("home_search", "搜索")]))

    asyncio.run(tool_assert_visible(ctx, "搜索"))

    assert ctx.deps.recorder.invocations[-1].page_path == "pages/Home"


# ---------------------------------------------------------------------------
# 改动 A：坐标 → 元素命中，录制时补录 resolved_element
# ---------------------------------------------------------------------------


def _boxed(
    element_id: str,
    box: tuple[int, int, int, int],
    *,
    key: str = "",
    element_key_id: str = "",
    editable: bool = False,
) -> UIElement:
    """带 bbox 的元素替身（现有 ``_element`` 的 bbox 固定，无法构造嵌套命中）。"""
    left, top, right, bottom = box
    return UIElement(
        element_id=element_id,
        key=key,
        id=element_key_id,
        editable=editable,
        clickable=True,
        bbox=BoundingBox(left=left, top=top, right=right, bottom=bottom),
    )


def _layered_snapshot() -> ScreenSnapshot:
    """外层容器 + 内层按钮 + 一块可编辑输入框（互相重叠）。"""
    return _snapshot(
        [
            _boxed("container", (0, 0, 400, 400)),
            _boxed("inner_button", (10, 10, 60, 60), key="inner_button"),
            _boxed("search_field", (100, 100, 300, 160), key="search_input", editable=True),
        ]
    )


class TestResolvedElementHitTest:
    """``_resolve_element`` 纯函数：最内层优先、editable 优先、命中失败返回 None。"""

    def test_none_snapshot_returns_none(self) -> None:
        assert _resolve_element(None, 10, 10) is None

    def test_picks_innermost_element(self) -> None:
        element = _resolve_element(_layered_snapshot(), 20, 20)

        assert element is not None
        assert element.element_id == "inner_button"

    def test_prefers_editable_over_smaller_child(self) -> None:
        snapshot = _snapshot(
            [
                _boxed("label", (100, 100, 140, 140)),
                _boxed("search_field", (100, 100, 300, 160), key="search_input", editable=True),
            ]
        )

        element = _resolve_element(snapshot, 120, 120)

        assert element is not None
        assert element.element_id == "search_field"

    def test_point_outside_every_bbox_returns_none(self) -> None:
        assert _resolve_element(_layered_snapshot(), 900, 900) is None

    def test_right_and_bottom_edges_are_exclusive(self) -> None:
        """bbox 是半开区间 [left, right) × [top, bottom)：右/下边界上的点不算命中。"""
        snapshot = _snapshot([_boxed("inner_button", (10, 10, 60, 60))])

        assert _resolve_element(snapshot, 59, 59) is not None
        assert _resolve_element(snapshot, 60, 30) is None
        assert _resolve_element(snapshot, 30, 60) is None

    def test_element_without_bbox_is_ignored(self) -> None:
        snapshot = _snapshot([UIElement(element_id="no_bbox", content="无定位框")])

        assert _resolve_element(snapshot, 10, 10) is None


class TestRecorderResolvedElement:
    """录制器补录：CLICK 用 x/y，INPUT_TEXT 用二元 coordinate，命中失败静默为 None。"""

    def test_click_records_innermost_element(self, tmp_path: Path) -> None:
        ctx = _context(tmp_path, _layered_snapshot())

        asyncio.run(tool_click(ctx, 20, 20))

        invocation = ctx.deps.recorder.invocations[-1]
        assert invocation.tool == DcToolName.CLICK
        assert invocation.status == DcToolStatus.SUCCEEDED
        assert invocation.resolved_element is not None
        assert invocation.resolved_element.element_id == "inner_button"
        assert ctx.deps.device.clicks == [(20, 20)]  # type: ignore[attr-defined]

    def test_click_on_editable_field_prefers_editable(self, tmp_path: Path) -> None:
        ctx = _context(tmp_path, _layered_snapshot())

        asyncio.run(tool_click(ctx, 120, 120))

        element = ctx.deps.recorder.invocations[-1].resolved_element
        assert element is not None
        assert element.element_id == "search_field"
        assert element.editable is True

    def test_click_hit_failure_records_none(self, tmp_path: Path) -> None:
        ctx = _context(tmp_path, _layered_snapshot())

        asyncio.run(tool_click(ctx, 900, 900))

        assert ctx.deps.recorder.invocations[-1].resolved_element is None

    def test_click_without_snapshot_records_none(self, tmp_path: Path) -> None:
        """无帧可判时不抛异常，只保持 None（脚本回退坐标写法）。"""
        ctx = _context(tmp_path, None)

        asyncio.run(tool_click(ctx, 20, 20))

        invocation = ctx.deps.recorder.invocations[-1]
        assert invocation.status == DcToolStatus.SUCCEEDED
        assert invocation.resolved_element is None

    def test_input_text_records_element_from_coordinate(self, tmp_path: Path) -> None:
        ctx = _context(tmp_path, _layered_snapshot())

        asyncio.run(tool_input_text(ctx, "hello", 120, 120))

        invocation = ctx.deps.recorder.invocations[-1]
        assert invocation.tool == DcToolName.INPUT_TEXT
        assert invocation.resolved_element is not None
        assert invocation.resolved_element.element_id == "search_field"

    def test_input_text_without_coordinate_records_none(self, tmp_path: Path) -> None:
        """coordinate 缺省（仅输入、不先点按）时不猜元素。"""
        ctx = _context(tmp_path, _layered_snapshot())

        asyncio.run(tool_input_text(ctx, "hello"))

        invocation = ctx.deps.recorder.invocations[-1]
        assert invocation.args.get("coordinate") is None
        assert invocation.resolved_element is None

    def test_malformed_coordinates_are_skipped_silently(self, tmp_path: Path) -> None:
        """coordinate 长度不足/非序列、x/y 缺失或非数值 → 全部静默跳过，不抛异常。"""
        ctx = _context(tmp_path, _layered_snapshot())
        recorder = ctx.deps.recorder

        malformed: list[tuple[DcToolName, dict[str, Any]]] = [
            (DcToolName.INPUT_TEXT, {"text": "a", "coordinate": [120]}),
            (DcToolName.INPUT_TEXT, {"text": "a", "coordinate": "120,120"}),
            (DcToolName.CLICK, {"x": 120}),
            (DcToolName.CLICK, {"x": "120", "y": 120}),
        ]
        for tool, args in malformed:
            asyncio.run(recorder.run(ctx, tool, args, lambda: None))

        assert len(recorder.invocations) == len(malformed)
        assert all(invocation.resolved_element is None for invocation in recorder.invocations)


# ---------------------------------------------------------------------------
# start_app 冷重置：录制起点必须与生成脚本的冷启动锚点对齐
# ---------------------------------------------------------------------------


class _LaunchDevice(_FakeDevice):
    """在 ``_FakeDevice`` 之上补 start_app / stop_app 记账。"""

    def __init__(self, snapshot: ScreenSnapshot, *, stop_ok: bool = True) -> None:
        super().__init__(snapshot)
        self.stop_ok = stop_ok
        self.stops: list[str] = []
        self.starts: list[tuple[str, str, str | None]] = []

    def stop_app(self, bundle_name: str) -> CommandResult:
        self.stops.append(bundle_name)
        return CommandResult(
            command=f"aa force-stop {bundle_name}",
            returncode=0 if self.stop_ok else 1,
            stderr="" if self.stop_ok else "error: not running",
        )

    def start_app(self, bundle_name: str, ability_name: str, module_name: str | None = None) -> CommandResult:
        self.starts.append((bundle_name, ability_name, module_name))
        return CommandResult(command=f"aa start -b {bundle_name} -a {ability_name}", returncode=0)


def _launch_context(tmp_path: Path, *, stop_ok: bool = True) -> RunContext[DcToolContext]:
    ctx = _context(tmp_path, _layered_snapshot())
    ctx.deps.device = _LaunchDevice(_layered_snapshot(), stop_ok=stop_ok)  # type: ignore[assignment]
    return ctx


def test_first_start_app_in_the_session_cold_resets(tmp_path: Path) -> None:
    """会话内该 bundle 的首次 start_app：先 force-stop，再 start，``args.reset=true``。"""
    ctx = _launch_context(tmp_path)
    device = ctx.deps.device

    asyncio.run(tool_start_app(ctx, BUNDLE, "EntryAbility"))

    assert device.stops == [BUNDLE]  # type: ignore[attr-defined]
    assert device.starts == [(BUNDLE, "EntryAbility", None)]  # type: ignore[attr-defined]
    invocation = ctx.deps.recorder.invocations[-1]
    assert invocation.tool == DcToolName.START_APP
    assert invocation.args["reset"] is True
    assert invocation.args["bundle_name"] == BUNDLE
    assert invocation.args["ability_name"] == "EntryAbility"
    assert "module_name" not in invocation.args
    assert invocation.status == DcToolStatus.SUCCEEDED


def test_second_start_app_of_the_same_bundle_stays_warm(tmp_path: Path) -> None:
    """同一 bundle 的第二次调用不再 force-stop：不打断用户当前屏幕。"""
    ctx = _launch_context(tmp_path)
    device = ctx.deps.device

    asyncio.run(tool_start_app(ctx, BUNDLE, "EntryAbility"))
    asyncio.run(tool_start_app(ctx, BUNDLE, "EntryAbility"))

    assert device.stops == [BUNDLE]  # type: ignore[attr-defined]
    assert len(device.starts) == 2  # type: ignore[attr-defined]
    assert ctx.deps.recorder.invocations[0].args["reset"] is True
    assert ctx.deps.recorder.invocations[1].args["reset"] is False


def test_each_bundle_gets_its_own_cold_reset(tmp_path: Path) -> None:
    """first-**per-bundle**：不同 bundle 各自冷重置一次。"""
    ctx = _launch_context(tmp_path)
    device = ctx.deps.device

    asyncio.run(tool_start_app(ctx, BUNDLE, "EntryAbility"))
    asyncio.run(tool_start_app(ctx, "com.other.app", "EntryAbility"))
    asyncio.run(tool_start_app(ctx, BUNDLE, "EntryAbility"))

    assert device.stops == [BUNDLE, "com.other.app"]  # type: ignore[attr-defined]
    assert [item.args["reset"] for item in ctx.deps.recorder.invocations] == [True, True, False]


def test_failed_force_stop_still_starts_the_app_and_succeeds(tmp_path: Path) -> None:
    """``aa force-stop`` 对未运行的应用可能返回非 0，此时状态本来就是干净的 —— 不得判失败。"""
    ctx = _launch_context(tmp_path, stop_ok=False)
    device = ctx.deps.device

    message = asyncio.run(tool_start_app(ctx, BUNDLE, "EntryAbility"))

    assert device.stops == [BUNDLE]  # type: ignore[attr-defined]
    assert device.starts == [(BUNDLE, "EntryAbility", None)]  # type: ignore[attr-defined]
    invocation = ctx.deps.recorder.invocations[-1]
    assert invocation.status == DcToolStatus.SUCCEEDED
    assert invocation.args["reset"] is True
    # 返回值仍是 start_app 的 CommandResult 摘要，不含 force-stop 的错误。
    assert "not running" not in message
    assert "error" not in invocation.args


def test_module_name_is_recorded_only_when_provided(tmp_path: Path) -> None:
    ctx = _launch_context(tmp_path)

    asyncio.run(tool_start_app(ctx, BUNDLE, "EntryAbility", "entry"))

    assert ctx.deps.recorder.invocations[-1].args["module_name"] == "entry"


def test_running_invocation_already_in_the_ledger_counts_as_started(tmp_path: Path) -> None:
    """账本里已有（含 RUNNING 残留）该 bundle 的 start_app ⇒ 不再重复 force-stop。"""
    ctx = _launch_context(tmp_path)
    device = ctx.deps.device
    asyncio.run(ctx.deps.recorder.run(ctx, DcToolName.START_APP, {"bundle_name": BUNDLE}, lambda: None))

    asyncio.run(tool_start_app(ctx, BUNDLE, "EntryAbility"))

    assert device.stops == []  # type: ignore[attr-defined]
    assert ctx.deps.recorder.invocations[-1].args["reset"] is False
