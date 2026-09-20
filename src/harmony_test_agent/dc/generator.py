"""DC 模式 Hypium 脚本生成器。

通过**组合**（非继承、非修改）复用 ``HypiumGenerator`` 的静态方法
``_selector`` 和 ``_render_script``，生成无断言、无 Profile 依赖的
Hypium Python 回放脚本。

关键差异（对比 ``generation/hypium.py:191-199``）：
当操作中没有断言时**不注入兜底断言**，只输出 ``driver.capture_screen(...)``
作为脚本收尾。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..generation.hypium import HypiumGenerator
from ..models import (
    ActionResult,
    LocatorCandidate,
    LocatorKind,
    RunMode,
    RunState,
    RunTrace,
    ScreenSnapshot,
    TargetAppProfile,
    ToolName,
    utc_now,
)
from ..storage.artifacts import ArtifactStore
from .models import DcScriptArtifact, DcToolInvocation, DcToolName

# DC 工具 → 既有 ToolName 的映射（可回放操作）
_REPLAYABLE_MAP: dict[DcToolName, ToolName] = {
    DcToolName.CLICK: ToolName.CLICK_COORDINATE,  # 默认坐标；有元素时升级为 CLICK_ELEMENT
    DcToolName.SWIPE: ToolName.SWIPE,
    DcToolName.INPUT_TEXT: ToolName.INPUT_TEXT,
    DcToolName.BACK: ToolName.BACK,
    DcToolName.KEY_EVENT: ToolName.BACK,  # 仅 Back 键可映射；其他键省略
    DcToolName.WAIT: ToolName.WAIT,
    DcToolName.START_APP: ToolName.OPEN_APP,
    # 2026-09-17 新增：断言工具渲染为 driver.check_component_exist，使 DC 脚本含检查点。
    DcToolName.ASSERT_VISIBLE: ToolName.ASSERT_VISIBLE,
    DcToolName.ASSERT_NOT_VISIBLE: ToolName.ASSERT_NOT_VISIBLE,
    DcToolName.ASSERT_TEXT: ToolName.ASSERT_TEXT,
}

# 断言工具集合：replay_eligible 判定与 generator 统计共用同一数据源。
_ASSERT_TOOLS: frozenset[DcToolName] = frozenset(
    {DcToolName.ASSERT_VISIBLE, DcToolName.ASSERT_NOT_VISIBLE, DcToolName.ASSERT_TEXT}
)

# 占位应用身份：未显式提供 bundle/ability 时生成的脚本不得作为验收脚本。
_PLACEHOLDER_BUNDLE = "com.example.app"
_PLACEHOLDER_ABILITY = "EntryAbility"

# 不可回放工具（生成 # skipped 注释）
# 注意：断言工具**不在**此集合（它们在 _REPLAYABLE_MAP 中），因为它们可渲染为
# driver.check_component_exist 检查点。
_NON_REPLAYABLE: frozenset[DcToolName] = frozenset(
    {
        DcToolName.SCREENSHOT,
        DcToolName.INSPECT_SCREEN,
        DcToolName.DUMP_UI_HIERARCHY,
        DcToolName.COLLECT_LOGS,
        DcToolName.FOREGROUND_APP,
        DcToolName.LIST_APPS,
        DcToolName.INSPECT_APP,
        DcToolName.MEMORY_DUMP,
        DcToolName.FORCE_STOP_APP,
        DcToolName.INSTALL_APP,
        DcToolName.UNINSTALL_APP,
        DcToolName.CLEAR_APP_DATA,
        DcToolName.FILE_SEND,
        DcToolName.FILE_RECV,
        DcToolName.FILE_LIST,
        DcToolName.EXECUTE_SHELL,
    }
)

_SWIPE_DIRECTIONS = frozenset({"UP", "DOWN", "LEFT", "RIGHT"})

# 聚合警告的固定方向顺序：保证同一会话的警告列表稳定可断言（集合本身无序）。
_SWIPE_DIRECTION_ORDER = ("UP", "DOWN", "LEFT", "RIGHT")


class DcHypiumGenerator:
    """从 DC 会话录制的操作生成 Hypium Python 脚本。"""

    def __init__(self, artifacts: ArtifactStore, min_observed_rounds: int = 3):
        self.artifacts = artifacts
        # 动态 key 前缀泛化所需的跨轮观察门槛；由调用方注入
        # settings.profile_verification_rounds（精简后默认 1 轮）。
        self.min_observed_rounds = max(int(min_observed_rounds), 1)

    def generate(
        self,
        session_id: str,
        device_id: str,
        invocations: list[DcToolInvocation],
        snapshots: list[ScreenSnapshot] | None = None,
        bundle_name: str = "com.example.app",
        main_ability: str = "EntryAbility",
    ) -> DcScriptArtifact:
        """从录制的工具调用生成 Hypium 脚本。

        Args:
            session_id: DC 会话 ID（``dc-`` 前缀）。
            device_id: 设备序列号。
            invocations: 录制的工具调用列表。
            snapshots: 可选的截图列表（用于元素解析）。
            bundle_name: 目标应用 bundle name（用于脚本头部）。
            main_ability: 目标应用 ability name。

        Returns:
            DcScriptArtifact 包含脚本路径、源码、警告和省略操作。
        """
        warnings: list[str] = []
        # swipe 方向只能靠起止坐标推断，逐条警告会刷成警告墙：先计数、循环后聚合（改动 C2）。
        swipe_inferred: dict[str, int] = {}
        omitted: list[dict[str, str]] = []
        body_lines: list[str] = []
        included_count = 0

        # 构造合成最小 TargetAppProfile（in-memory，仅用于 _selector 和 _render_script）
        profile = TargetAppProfile(
            target_app_id=f"dc-{session_id}",
            display_name="DC Mode Recording",
            bundle_name=bundle_name,
            main_ability=main_ability,
            stable_locator_inventory=[],
            assertion_inventory=[],
        )

        # 构造合成最小 RunTrace（in-memory，仅用于 _render_script 模板）
        trace = RunTrace(
            run_id=session_id,
            target_app_id=profile.target_app_id,
            task="DC Mode recording",
            mode=RunMode.REGRESSION,
            device_id=device_id,
            state=RunState.COMPLETED,
            started_at=utc_now(),
            profile_snapshot=profile,
            snapshots=snapshots or [],
            actions=[],
        )

        # 映射并渲染每个操作
        for inv in invocations:
            if not inv.success:
                omitted.append(
                    {"invocation_id": inv.invocation_id, "tool": inv.tool.value, "reason": "invocation failed"}
                )
                continue

            if inv.tool in _NON_REPLAYABLE:
                omitted.append(
                    {
                        "invocation_id": inv.invocation_id,
                        "tool": inv.tool.value,
                        "reason": "not replayable in Hypium (observation/management/shell)",
                    }
                )
                body_lines.append(f"        # skipped: {inv.tool.value} {_format_args(inv.args)}")
                continue

            if inv.tool == DcToolName.KEY_EVENT:
                key = str(inv.args.get("key", ""))
                if key.lower() != "back":
                    omitted.append(
                        {
                            "invocation_id": inv.invocation_id,
                            "tool": inv.tool.value,
                            "reason": f"key_event({key!r}) is not replayable (only Back maps to go_back)",
                        }
                    )
                    body_lines.append(f"        # skipped: key_event({key!r})")
                    continue

            action_result = self._to_action_result(inv, trace, warnings, swipe_inferred)
            if action_result is None:
                omitted.append(
                    {"invocation_id": inv.invocation_id, "tool": inv.tool.value, "reason": "could not map to action"}
                )
                continue

            line = self._render_action_line(action_result, inv, profile, warnings)
            if line:
                body_lines.append(line)
                included_count += 1
            else:
                omitted.append(
                    {"invocation_id": inv.invocation_id, "tool": inv.tool.value, "reason": "no renderable output"}
                )

        # swipe 推断警告按方向聚合为一条（固定方向顺序，输出稳定）
        for direction in _SWIPE_DIRECTION_ORDER:
            count = swipe_inferred.get(direction, 0)
            if count:
                warnings.append(f"swipe direction inferred as {direction} x{count} (from start/end coordinates)")

        # DC 会话使用显式 assert_* 工具，因此**不注入** hypium.py:191-199 的兜底断言，
        # 以免掩盖缺失的断言。若模型未调用任何 assert_* 工具（explicit_assertions == 0），
        # 下面的 replay_eligible 判定会把脚本标记为不可回放。
        if included_count == 0:
            warnings.append("no replayable operations were recorded")
            body_lines.append("        pass  # no replayable operations")

        # 复用 HypiumGenerator._render_script 模板
        script_text = HypiumGenerator._render_script(trace, profile, body_lines)

        # replay_eligible 条件判定（2026-09-17 重构：原来是硬编码 False）：
        # 必须含显式断言 + 非占位应用身份 + 至少一条可回放操作。
        explicit_assertions = sum(1 for inv in invocations if inv.success and inv.tool in _ASSERT_TOOLS)
        replay_eligible = bool(
            explicit_assertions >= 1
            and bundle_name != _PLACEHOLDER_BUNDLE
            and main_ability != _PLACEHOLDER_ABILITY
            and included_count > 0
        )
        if not replay_eligible:
            if explicit_assertions == 0:
                warnings.append("no explicit assert_* tool call was recorded; script is diagnostic only")
            if bundle_name == _PLACEHOLDER_BUNDLE or main_ability == _PLACEHOLDER_ABILITY:
                warnings.append("placeholder bundle/ability supplied; script is diagnostic only")

        # 落盘
        output_dir = self.artifacts.run_dir(session_id) / "generated"
        output_dir.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r"[^a-zA-Z0-9_]", "_", session_id)
        python_path = output_dir / f"dc_test_{safe_id}.py"
        python_path.write_text(script_text, encoding="utf-8")

        config_path = output_dir / f"dc_test_{safe_id}.json"
        config = {
            "schema_version": 2,
            "runner_mode": "driver",
            "case_id": safe_id,
            "device_id": device_id,
            "target_app_id": profile.target_app_id,
            "bundle_name": bundle_name,
            "main_ability": main_ability,
            "purpose": "acceptance" if replay_eligible else "dc_recording",
            "replay_eligible": replay_eligible,
            "explicit_assertions": explicit_assertions,
            "generated_from_session_id": session_id,
            "included_operations": included_count,
            "omitted_operations": len(omitted),
            "warnings": warnings,
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        return DcScriptArtifact(
            python_path=str(python_path.resolve()),
            python_text=script_text,
            warnings=warnings,
            generated_at=utc_now(),
            included_operations=included_count,
            omitted_operations=omitted,
            replay_eligible=replay_eligible,
            explicit_assertions=explicit_assertions,
        )

    # ------------------------------------------------------------------
    # 内部：DC 工具调用 → ActionResult 映射
    # ------------------------------------------------------------------

    def _to_action_result(
        self,
        inv: DcToolInvocation,
        trace: RunTrace,
        warnings: list[str],
        swipe_inferred: dict[str, int],
    ) -> ActionResult | None:
        """把 DcToolInvocation 映射为既有 ActionResult（不修改 ToolName 枚举）。

        ``swipe_inferred`` 由 :meth:`generate` 创建并持有：方向靠坐标推断的 swipe 只累加计数，
        聚合警告由调用方在循环结束后统一追加（避免逐条刷屏）。
        """
        tool_name = _REPLAYABLE_MAP.get(inv.tool)
        if tool_name is None:
            return None

        params: dict[str, Any] = dict(inv.args)
        locator: LocatorCandidate | None = None

        # click：如果有 resolved_element 且带 key/id，升级为 CLICK_ELEMENT
        if inv.tool == DcToolName.CLICK:
            element = inv.resolved_element
            if element and (element.key or element.id):
                tool_name = ToolName.CLICK_ELEMENT
                kind = LocatorKind.KEY if element.key else LocatorKind.ID
                value = element.key or element.id
                locator = LocatorCandidate(kind=kind, value=value)
                params["target"] = value
            else:
                tool_name = ToolName.CLICK_COORDINATE
                coord = params.get("coordinate") or [params.get("x", 0), params.get("y", 0)]
                params["coordinate"] = coord

        # 断言：resolved_element 带 key/id 时生成结构化定位器（与 click 同一优先级），
        # 否则退化为 BY.text(target) 诊断选择器。
        if inv.tool in _ASSERT_TOOLS:
            element = inv.resolved_element
            if element and (element.key or element.id):
                kind = LocatorKind.KEY if element.key else LocatorKind.ID
                value = element.key or element.id
                locator = LocatorCandidate(kind=kind, value=value)

        # input_text：resolved_element 带 key/id 时同样升级为结构化定位器。
        # 录制补录了坐标命中元素（tools.py::_resolve_element）后，输入框才能写成
        # BY.key/BY.id；否则渲染只能退化为 BY.text("输入框") 并留下
        # "semantic target ... fell back to exact text" 警告。
        if inv.tool == DcToolName.INPUT_TEXT:
            element = inv.resolved_element
            if element and (element.key or element.id):
                kind = LocatorKind.KEY if element.key else LocatorKind.ID
                value = element.key or element.id
                locator = LocatorCandidate(kind=kind, value=value)

        # swipe：确保 direction 大写
        if inv.tool == DcToolName.SWIPE:
            direction = str(params.get("direction", "")).upper()
            if direction not in _SWIPE_DIRECTIONS:
                # 从起止坐标推断方向
                start = params.get("start", [0, 0])
                end = params.get("end", [0, 0])
                if isinstance(start, list) and isinstance(end, list) and len(start) == 2 and len(end) == 2:
                    dx = end[0] - start[0]
                    dy = end[1] - start[1]
                    if dx > 0 and abs(dx) > abs(dy):
                        direction = "RIGHT"
                    elif dx < 0 and abs(dx) > abs(dy):
                        direction = "LEFT"
                    elif dy > 0:
                        direction = "DOWN"
                    else:
                        direction = "UP"
                else:
                    direction = "UP"
                swipe_inferred[direction] = swipe_inferred.get(direction, 0) + 1
            params["direction"] = direction

        # input_text：确保有 text 参数
        if inv.tool == DcToolName.INPUT_TEXT:
            if "text" not in params:
                params["text"] = ""

        # 断言：target 是唯一目标载体；ASSERT_TEXT 的期望文案同样落在 target。
        if inv.tool in _ASSERT_TOOLS:
            target = str(params.get("target") or params.get("text") or "")
            params["target"] = target
            params["text"] = target

        # wait：确保有 wait_seconds
        if inv.tool == DcToolName.WAIT:
            if "wait_seconds" not in params:
                params["wait_seconds"] = params.get("seconds", 1)

        return ActionResult(
            step_id=inv.invocation_id,
            tool=tool_name,
            params=params,
            success=True,
            started_at=inv.started_at,
            ended_at=inv.ended_at,
            duration_ms=inv.duration_ms,
            locator=locator,
        )

    # ------------------------------------------------------------------
    # 内部：ActionResult → Hypium 脚本行
    # ------------------------------------------------------------------

    def _render_action_line(
        self,
        action: ActionResult,
        inv: DcToolInvocation,
        profile: TargetAppProfile,
        warnings: list[str],
    ) -> str | None:
        """渲染单个动作为 Hypium 脚本行。

        复用 ``HypiumGenerator._selector`` 生成 BY.xxx 选择器。
        """
        tool = action.tool

        if tool == ToolName.OPEN_APP:
            # start_app 由脚本头部的 stop/start/wait 处理，这里省略
            return f"        # start_app: {inv.args.get('bundle_name', '?')} (handled by script setup)"

        if tool == ToolName.CLICK_ELEMENT:
            selector = self._selector(action.locator, action.params.get("target"), profile, warnings)
            return f"        driver.touch({selector})"

        if tool == ToolName.CLICK_COORDINATE:
            coordinate = action.params.get("coordinate")
            point = tuple(coordinate) if coordinate else (0, 0)
            return f"        driver.touch({point})  # coordinate fallback"

        if tool == ToolName.INPUT_TEXT:
            selector = self._selector(action.locator, action.params.get("target") or "输入框", profile, warnings)
            return f"        driver.input_text({selector}, {action.params.get('text', '')!r})"

        if tool == ToolName.SWIPE:
            direction = str(action.params.get("direction", "UP")).upper()
            if direction not in _SWIPE_DIRECTIONS:
                direction = "UP"
            return f"        driver.swipe({direction!r})"

        if tool == ToolName.BACK:
            return "        driver.go_back()"

        if tool == ToolName.WAIT:
            seconds = float(action.params.get("wait_seconds") or action.params.get("seconds") or 1)
            return f"        driver.wait({seconds!r})"

        # 断言渲染：与 generation/hypium.py:176-188 的模板一致，
        # 复用 driver.check_component_exist(selector, expect_exist=...)。
        if tool == ToolName.ASSERT_VISIBLE:
            selector = self._selector(action.locator, action.params.get("target"), profile, warnings)
            return f"        driver.check_component_exist({selector}, expect_exist=True)"

        if tool == ToolName.ASSERT_NOT_VISIBLE:
            selector = self._selector(action.locator, action.params.get("target"), profile, warnings)
            return f"        driver.check_component_exist({selector}, expect_exist=False)"

        if tool == ToolName.ASSERT_TEXT:
            target = action.params.get("target") or action.params.get("text")
            selector = self._selector(action.locator, target, profile, warnings)
            return f"        driver.check_component_exist({selector}, expect_exist=True)"

        return None

    def _selector(
        self,
        locator: LocatorCandidate | None,
        target: str | None,
        profile: TargetAppProfile,
        warnings: list[str],
    ) -> str:
        """复用 ``HypiumGenerator._selector``，并注入本会话的跨轮观察门槛。"""
        return HypiumGenerator._selector(locator, target, warnings, profile, self.min_observed_rounds)


def _format_args(args: dict[str, Any]) -> str:
    """把工具参数格式化为短字符串，用于注释。"""
    if not args:
        return ""
    parts = [f"{k}={v!r}" for k, v in args.items() if v is not None]
    text = ", ".join(parts)
    return text[:120] + "..." if len(text) > 120 else text
