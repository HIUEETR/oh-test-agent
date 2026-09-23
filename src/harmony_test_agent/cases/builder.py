"""把四种来源归一化为用例 IR。

来源：
* Live 运行轨迹 ``RunTrace.actions``（复刻 ``generation/hypium.py`` 的逐分支语义）
* DC 会话录制 ``DcToolInvocation``（复刻 ``dc/generator.py`` 的逐分支语义）
* 压测请求 ``StressRequest``（见 ``cases/stress.py`` 的调用方）
* 缺陷报告 ``BugReproPlan``（见 ``cases/bug_repro.py``）

**兼容性契约**：omit reason 字符串与 ``counts`` 的 key 必须与历史实现一字不差
（``tests/unit/test_generation.py`` / ``tests/unit/test_dc_generator.py`` 做精确子串断言）。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from ..models import (
    ActionResult,
    LocatorCandidate,
    LocatorKind,
    ProfileStatus,
    RunState,
    RunTrace,
    ScenarioKind,
    ScreenSnapshot,
    TargetAppProfile,
    ToolName,
    UIElement,
    utc_now,
)
from ..perception.volatility import is_unreplayable_locator_key

# 「脚本定位器不可回放」的判定必须用 ``perception.volatility`` 里**最窄**的那个函数：
# ``is_volatile_evidence_key`` 会把正在工作的稳定骨架 key 一并判为易变（真机实测误杀 6 个），
# 详见该模块 docstring 的反例清单。``cases → perception`` 只依赖标准库，无环。
# ``cases.safety`` 只依赖 ``..models`` / ``..runtime.safety`` / ``.spec``，已实测与
# 本模块无环（I4）。按键名归一化的唯一真源在那边，避免三份表各写一份归一化规则。
from .safety import canonical_key_event
from .spec import (
    NO_REPLAYABLE_COMMENT,
    CaseProvenance,
    CheckpointKind,
    CheckpointSpec,
    LocatorEvidence,
    LocatorSpec,
    MatchMode,
    SetupSpec,
    StepAction,
    TeardownSpec,
    TestCaseSpec,
    TestStepSpec,
)
from .titles import checkpoint_title_zh

# Hypium Driver 的 swipe 只接受大写方向枚举（RIGHT/LEFT/UP/DOWN）。
SWIPE_DIRECTIONS = frozenset({"UP", "DOWN", "LEFT", "RIGHT"})

#: ``SWIPE`` 推断警告的方向顺序：保证同一输入的警告列表稳定可断言。
SWIPE_DIRECTION_ORDER = ("UP", "DOWN", "LEFT", "RIGHT")

#: DC 侧「不可回放」工具（观测/管理/shell）；用字符串值表达，避免在模块导入期
#: 触发 ``harmony_test_agent.dc`` 包初始化（``dc/__init__`` 会 import 本模块）。
#: ``dc/generator.py`` 把同一集合投影回 ``DcToolName`` 供既有契约测试使用。
NON_REPLAYABLE_DC_TOOLS: frozenset[str] = frozenset(
    {
        "screenshot",
        "inspect_screen",
        "dump_ui_hierarchy",
        "collect_logs",
        "foreground_app",
        "list_apps",
        "inspect_app",
        "memory_dump",
        "force_stop_app",
        "install_app",
        "uninstall_app",
        "clear_app_data",
        "file_send",
        "file_recv",
        "file_list",
        "execute_shell",
    }
)

#: 动态 key 前缀泛化的历史正则（``generation/hypium.py``）。
#:
#: 真机复盘（run-20260921T053514Z-8418044b 的回放失败）：鸿蒙的实例 key 既可能是
#: ``feed_card_20240101``（下划线分隔），也可能是 ``add_agenda_title-1789969034729``
#: （连字符 + 毫秒时间戳）。旧正则只认下划线，后者会被当成稳定 key 原样写进脚本，
#: 回放时必然 ``Can't find component with [BY.key(...)]``。
_DYNAMIC_LOCATOR = re.compile(r"(.+?[_-])\d{8,}")

#: 「epoch 时间戳实例 ID」形态：``add_agenda_title-1790078405913``（13 位毫秒）
#: 与 ``add_agenda_title_1790078405``（10 位秒）。只认这两种长度，19 位内容 ID
#: （``p2_channel_content_question_2085141629112009975``）必须保持精确值：
#: 内容 ID 的**前缀**（``p2_channel_content_question_``）会同时匹配多个不同内容项，
#: 盲目 STARTS_WITH 会点到错误元素。
_TIMESTAMP_SUFFIX_MS = re.compile(r"^(?P<prefix>.+?[_-])(?P<ts>\d{13})$")
_TIMESTAMP_SUFFIX_S = re.compile(r"^(?P<prefix>.+?[_-])(?P<ts>\d{10})$")

#: 会话帧时间窗的两侧余量（秒）。DC 录制里 key 的时间戳与帧采集时间之间存在工具调用
#: 延迟，但仍必须落在同一次会话附近，才能把「时间戳实例 ID」与「内容 ID」区分开。
SNAPSHOT_WINDOW_MARGIN_SECONDS = 3600.0


def _timestamp_suffix(value: str, window: tuple[float, float] | None) -> str | None:
    """识别「epoch 时间戳实例 ID」形态的 key/id 后缀，返回可用于 STARTS_WITH 的前缀。

    只认 13 位（毫秒）与 10 位（秒），且解释为 epoch 后必须落在会话时间窗内。
    窗口约束把时间戳实例 ID 与内容 ID 区分开：
    ``add_agenda_title-1790078405913``（13 位，落在会话帧时间戳 1790078264–1790078623 之间）
    → 可泛化；``p2_channel_content_question_2085141629112009975``（19 位内容 ID）→ 不泛化。

    ``window is None``（会话一帧都没采到）时**不泛化**：无证据不冒险，退回
    ``unvalidated dynamic`` 的「保留精确值 + 响亮警告」行为。
    """
    if not value or window is None:
        return None
    for pattern, scale in ((_TIMESTAMP_SUFFIX_MS, 1000.0), (_TIMESTAMP_SUFFIX_S, 1.0)):
        match = pattern.fullmatch(value)
        if match is None:
            continue
        try:
            stamp = int(match.group("ts")) / scale
        except TypeError, ValueError:
            return None
        return match.group("prefix") if window[0] <= stamp <= window[1] else None
    return None


def _snapshot_window(snapshots: list[ScreenSnapshot] | None) -> tuple[float, float] | None:
    """从已采集帧的 ``captured_at`` 推出会话时间窗（两侧各留一小时）；无帧返回 ``None``。"""
    if not snapshots:
        return None
    stamps = [
        snapshot.captured_at.timestamp() for snapshot in snapshots if getattr(snapshot, "captured_at", None) is not None
    ]
    if not stamps:
        return None
    return (min(stamps) - SNAPSHOT_WINDOW_MARGIN_SECONDS, max(stamps) + SNAPSHOT_WINDOW_MARGIN_SECONDS)


def _dynamic_pattern_variants(prefix: str, value: str) -> set[str]:
    """前缀泛化的等价写法：``foo-``/``foo_`` 与收割侧记录的 ``foo-#`` 必须互相认得。

    任务期证据回收（``orchestrator._stable_locator_from_candidate``）把动态标识折叠为
    ``<prefix>#`` 存进 ``dynamic_pattern``；而选择器渲染需要的是可直接用于 starts_with 的
    裸前缀，因此比较时同时接受这两种写法。
    """
    return {prefix, re.sub(r"\d+", "#", value)}


#: DC 占位应用身份。
PLACEHOLDER_BUNDLE = "com.example.app"
PLACEHOLDER_ABILITY = "EntryAbility"

#: 坐标兜底在未知分辨率时的占位边界（仍带警告，绝不静默）。
UNKNOWN_RESOLUTION_BOUND = (0, 0)

#: 兜底检查点的中文说明。
FALLBACK_CHECKPOINT_MESSAGE = "检查点：应可见一个已观察到的稳定元素"

#: 易变定位器（日期格 / 时钟读数 / 列表实例序号）被拒绝、且该动作**无坐标可退**时的省略原因。
#: 必须与 ``counts["omitted_actions"]`` 一起出现在产物里：宁可脚本少一步，也不要把注定
#: 失效的选择器写进脚本，更不要静默地把文本输进一个猜出来的控件。
VOLATILE_LOCATOR_OMIT_REASON = "volatile locator cannot be replayed"
#: 输入框专用：没有坐标兜底，单独区分原因，便于诊断「少了输入」而不是「少了点击」。
VOLATILE_INPUT_OMIT_REASON = "volatile locator on an input target cannot be replayed"

#: 无据缺席断言的省略原因。
#:
#: ``ASSERT_NOT_VISIBLE`` 的语义是**反的**：``check_component_exist(BY.text(<无据标识符>),
#: expect_exist=False)`` 是**恒真**的 —— 把它 soft 化会静默通过并制造假绿，比恒假更危险
#: （恒假至少会被看见）。因此无据时直接 omit。
UNGROUNDED_ABSENT_ASSERTION_OMIT_REASON = "absent-assertion target has no component evidence (vacuously true)"

#: **状态条件存在**、且「不存在时跳过」等价于无操作的控件 token。
#: 只收这类控件，是因为把它们降级为条件步骤不会掩盖真实失败：
#: ``clear``（搜索框清空按钮只在有输入时渲染）、``dismiss``（可关闭的提示卡）、
#: ``back_to_top``（滚动后才出现）、``skip``（引导页跳过）。
#: 刻意**不含** ``close`` / ``cancel``：对话框上的关闭/取消在弹窗存在时必然可见，
#: 静默跳过只会把「弹窗没出现」这种真实失败藏起来。
_OPTIONAL_CONTROL_TOKENS: tuple[str, ...] = ("clear", "dismiss", "back_to_top", "skip")
_OPTIONAL_CONTROL_TEXT: tuple[str, ...] = ("清空", "清除", "不再提示", "忽略", "跳过")
_OPTIONAL_CONTROL_RE = re.compile(r"(?<![a-z0-9])(?:" + "|".join(_OPTIONAL_CONTROL_TOKENS) + r")(?![a-z0-9])", re.I)


def is_optional_control(*values: str) -> bool:
    """判断一个控件是否是「状态条件存在」的可跳过控件。

    真机复盘 dc-20260922T171655Z-6fff3547：录制的第 2 步点击搜索框的 ``p2_search_clear``，
    那一刻搜索框里还留着上一轮的查询词；而生成脚本的 setup 是 ``stop_app`` + ``start_app``
    冷启动 ⇒ 输入框为空 ⇒ 该按钮根本不渲染 ⇒ 回放第一步就
    ``Can't find component with [BY.key('p2_search_clear')]``。

    这类控件的正确语义是「有就清掉、没有就已经是干净状态」，因此按条件步骤渲染
    （见 ``generation/standalone.py``）：找不到就跳过并在产物里留痕，而不是判失败。
    判定只看 key/id/content 里的词元，是**保守的启发式** —— 宁可少降级，不可把真失败藏起来。
    """
    for value in values:
        text = (value or "").strip()
        if not text:
            continue
        if _OPTIONAL_CONTROL_RE.search(text):
            return True
        if any(token in text for token in _OPTIONAL_CONTROL_TEXT):
            return True
    return False


#: 反向断言（``PlannedStep.expects_defect``）的检查点说明前缀：通过 = 观测到异常现象。
UNEXPECTED_CHECKPOINT_MESSAGE = "反向断言：通过即代表观测到异常现象"

#: 「标识符形态的 target」正则：至少一个 ``_``/``-`` 分隔符的多 token 名字。
#:
#: 真机复盘 run-20260923T065210Z-23434a78：``assert_visible`` 的 target
#: ``p2_channel_content_question_2085141629112009975`` 没有任何 ``locator``，被终端兜底
#: 渲染成 ``BY.text(该标识符)`` —— 设备上永远不会有一个**文本**等于这串 key 的控件，
#: 硬检查点恒假，脚本 3/3 attempt 全红（``[Script-0203003]``）。
#:
#: 要求至少一个 ``_``/``-`` 分隔符，是为了让 ``OK`` / ``确定`` / ``Sign In`` 这类
#: 真实按钮文案（单 token 或含空格）不被误判成标识符。
_IDENTIFIER_SHAPED_TARGET = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:[_-][A-Za-z0-9]+)+$")


def is_identifier_shaped_target(value: str) -> bool:
    """target 长得像控件 key/id 标识符，而不是屏幕上的人类可读文本。

    正则要求至少一个 ``_``/``-`` 分隔符，因此 ``OK`` / ``确定`` 这类单 token 按钮文案
    **不算**标识符（它们确实可能是真实文本）。
    """
    return bool(_IDENTIFIER_SHAPED_TARGET.fullmatch((value or "").strip()))


#: 脚本里有断言的目标**没有任何控件证据**（被降级为 soft 检查点）。生成器明知该选择器
#: 命不中，就不能再对外承诺 ``confidence: high`` / ``promotion_eligible: true``。
UNGROUNDED_ASSERTION_FACTOR = "script contains an assertion whose target has no component evidence"

#: 录制从**热启动**开始，而生成脚本的 setup 是冷启动（``stop_app`` + ``start_app``）。
WARM_START_RECORDING_BLOCKER = (
    "dc recording started from a warm app launch; replay cold-starts and may begin on a different page"
)
WARM_START_FACTOR = "script replays from a cold start but the recording began on a warm app launch"

#: ``confidence`` 三档中判为 low 的质量因素前缀。
#:
#: 后四条是「注定跑不起来 / 结论不可信」的因素：日期格 / 时钟读数 / 列表实例 key 换一天必挂，
#: 时间戳前缀在同帧匹配到多个控件时不泛化也会挂，无据断言与热启动录制则会落到不同页面上。
#: 这类脚本报 high/medium 都是说谎。
LOW_CONFIDENCE_PREFIXES: tuple[str, ...] = (
    "source agent outcome is failed",
    "source trace contains failed actions",
    "source trace does not end with a successful FINISH action",
    "script contains a locator that will not match on replay",
    "script contains a date/clock/list-instance locator that will not match on another day",
    UNGROUNDED_ASSERTION_FACTOR,
    WARM_START_FACTOR,
)

ConfidenceLevel = Literal["high", "medium", "low"]


def confidence_factors_from_warnings(warnings: list[str]) -> list[str]:
    """把定位器类警告翻译成质量因素（Live 与 DC 两条构建路径共用）。

    ``locator_from_candidate`` 只负责发出**带上下文数字**的警告（帧数、同帧命中数、
    具体 key），分档所需的固定文案在这里集中映射，避免两条路径各写一份而漂移。
    返回的因素按固定顺序排列，且全部是「非阻断」的质量顾虑。
    """
    factors: list[str] = []
    if any(item.startswith("unvalidated dynamic ") for item in warnings):
        factors.append("source trace contains a dynamic locator without stable unique-prefix evidence")
    if any(item.startswith("timestamp-suffixed ") and "generalized to prefix" in item for item in warnings):
        factors.append("dynamic locator generalized from a single session without cross-round evidence")
    if any(item.startswith("timestamp-suffixed ") and "prefix is not unique" in item for item in warnings):
        factors.append("script contains a locator that will not match on replay")
    if any(item.startswith(("volatile key ", "volatile id ")) for item in warnings):
        factors.append("script contains a date/clock/list-instance locator that will not match on another day")
    # 无据断言：`locator_from_candidate` 的终端兜底文案（Live/DC 共用），以及
    # assert_text / DC 侧降级时的「has no component evidence」文案（不以 ungrounded 开头）。
    if any(item.startswith("ungrounded target ") for item in warnings):
        factors.append(UNGROUNDED_ASSERTION_FACTOR)
    if any("has no component evidence" in item for item in warnings):
        factors.append(UNGROUNDED_ASSERTION_FACTOR)
    if any(item.startswith("the recording began with a warm app launch") for item in warnings):
        factors.append(WARM_START_FACTOR)
    return factors


def evaluate_runnable(
    *,
    included_actions: int,
    bundle_name: str,
    main_ability: str,
) -> tuple[bool, list[str]]:
    """可执行性判定：**只有 2 条物理必要条件**。

    任何质量、验证、Profile 状态相关的顾虑都不得进入这里——它们属于 confidence 层
    （``evaluate_confidence``）或晋级层（``_promotion_blockers``）。缺任一条的脚本在物理上
    根本无法运行：没有可回放动作，或应用身份还是占位值。

    **身份占位只认 ``bundle_name``**：``EntryAbility`` 是鸿蒙工程的默认且常见的**真实**
    ability 名——本案 ``com.github.zhuoyi233.zhplus / EntryAbility`` 就是真实身份，把它当成
    占位会让默认路径下的真实脚本永远不可执行（计划的 G1 与 Phase 4 验收都要求它可执行）。
    因此这里只在 bundle 缺失/等于哨兵 ``com.example.app``，或 ability 为空（脚本连启动参数都
    没有）时判为不可执行。
    """
    blockers: list[str] = []
    if included_actions <= 0:
        blockers.append("script has no replayable action")
    if not bundle_name or bundle_name == PLACEHOLDER_BUNDLE or not main_ability:
        blockers.append(f"app identity is a placeholder ({PLACEHOLDER_BUNDLE}/{PLACEHOLDER_ABILITY})")
    return not blockers, blockers


def evaluate_confidence(factors: list[str], *, outcome: str) -> ConfidenceLevel:
    """按质量因素把脚本分到 high/medium/low 三档（**非阻断**，只影响徽章与排序）。"""
    if outcome == "failed" or any(factor.startswith(LOW_CONFIDENCE_PREFIXES) for factor in factors):
        return "low"
    return "medium" if factors else "high"


@dataclass
class CaseBuildResult:
    """一次 IR 构建的产物与全部审计信息。"""

    spec: TestCaseSpec
    omitted_actions: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    incomplete_reasons: list[str] = field(default_factory=list)
    """.. deprecated:: 与 ``confidence_factors`` 同值的兼容别名（旧 JSON 产物 / 旧 API 消费方）。"""
    replay_eligible: bool = False
    """脚本是否可执行（runnable）；质量看 ``confidence``，晋级看 ``promotion_eligible``。"""
    purpose: Literal["acceptance", "diagnostic"] = "diagnostic"
    explicit_assertions: int = 0
    source_agent_outcome: Literal["completed", "failed", "stopped", "unknown"] = "unknown"
    confidence: ConfidenceLevel = "low"
    confidence_factors: list[str] = field(default_factory=list)
    promotion_eligible: bool = False
    promotion_blockers: list[str] = field(default_factory=list)
    runnable_blockers: list[str] = field(default_factory=list)
    source_failures: list[dict[str, str]] = field(default_factory=list)
    """录制期间失败的 invocation（缺口 5 末条）。

    失败的 invocation 仍然**不进脚本**（脚本里确实不该有失败动作），但「这个用例的录制
    过程中有过失败」必须可追溯，因此单独留痕并写进 case 的 config JSON。"""


def new_case_id() -> str:
    """生成 ``case-<UTC 时间戳>-<6 位十六进制>`` 形式的用例 ID。"""
    return f"case-{utc_now():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def _swipe_points_from_params(params: dict[str, Any]) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    """从动作参数里读出显式滑动起止坐标（计划 5.5），非法输入一律忽略。"""

    def point(value: Any) -> tuple[int, int] | None:
        if isinstance(value, (list, tuple)) and len(value) == 2:
            try:
                return (int(value[0]), int(value[1]))
            except TypeError, ValueError:
                return None
        return None

    return point(params.get("start")), point(params.get("end"))


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str, *, fallback: str = "case") -> str:
    """把中文/任意文本压成 ``[a-z0-9-]`` 的 slug；纯中文会退化为 fallback + 短哈希。"""
    ascii_part = _SLUG_STRIP.sub("-", text.lower()).strip("-")
    ascii_part = ascii_part[:48].strip("-")
    if not ascii_part:
        ascii_part = f"{fallback}-{uuid.uuid4().hex[:6]}"
    return ascii_part[:64].strip("-") or "case"


def format_args(args: dict[str, Any]) -> str:
    """把工具参数格式化为短字符串（``dc/generator.py::_format_args`` 的等价实现）。"""
    if not args:
        return ""
    parts = [f"{key}={value!r}" for key, value in args.items() if value is not None]
    text = ", ".join(parts)
    return text[:120] + "..." if len(text) > 120 else text


class CaseBuilder:
    """把轨迹 / DC 录制 / 压测 / 缺陷报告构建为 ``TestCaseSpec``。"""

    def __init__(self, *, min_observed_rounds: int = 3, inject_fallback_assertion: bool = True):
        self.min_observed_rounds = max(int(min_observed_rounds), 1)
        self.inject_fallback_assertion = inject_fallback_assertion
        # 本次构建可用的已采集帧，以及由它们推出的会话时间窗。时间戳实例 key 的泛化
        # （Phase 2）与「前缀在帧里是否唯一」的离线验证都只读这两个值：零设备零磁盘成本。
        # 由 ``from_trace`` / ``from_dc_invocations`` 在入口处赋值，因此 ``CaseBuilder``
        # 的构造签名不变，``dc/generator.py`` 与 ``generation/hypium.py`` 的调用点都不用改。
        self._snapshots: list[ScreenSnapshot] = []
        self._snapshot_window: tuple[float, float] | None = None

    # ------------------------------------------------------------------
    # Live 运行轨迹
    # ------------------------------------------------------------------

    def from_trace(
        self,
        trace: RunTrace,
        profile: TargetAppProfile,
        *,
        case_id: str | None = None,
        slug: str | None = None,
        scenario: ScenarioKind = ScenarioKind.CORE_FLOW,
    ) -> CaseBuildResult:
        """把 Live 轨迹构建为用例 IR（逐分支复刻 ``HypiumGenerator._render_actions``）。"""
        # 已采集帧是「前缀是否唯一」与「时间戳是否落在会话窗口内」的唯一证据来源。
        self._snapshots = list(trace.snapshots)
        self._snapshot_window = _snapshot_window(self._snapshots)
        warnings: list[str] = []
        omitted: list[dict[str, str]] = []
        source_failures: list[dict[str, str]] = []
        steps: list[TestStepSpec] = []
        generated_actions = 0
        generated_assertions = 0
        explicit_assertions = 0
        coordinate_fallbacks = 0

        def omit(action: ActionResult, reason: str) -> None:
            omitted.append({"step_id": action.step_id, "tool": str(action.tool), "reason": reason})

        def add_step(action: StepAction, **kwargs: Any) -> TestStepSpec:
            step = TestStepSpec(
                step_id=kwargs.pop("step_id", f"step-{len(steps) + 1}"),
                index=len(steps) + 1,
                action=action,
                title_zh="",
                **kwargs,
            )
            steps.append(step)
            return step

        def attach(checkpoint: CheckpointSpec) -> None:
            """断言挂到前一个步骤；无前置步骤时新建一个纯 CHECK 步骤。"""
            checkpoint.message_zh = checkpoint.message_zh or checkpoint_title_zh(checkpoint)
            if steps:
                steps[-1].checkpoints.append(checkpoint)
            else:
                add_step(StepAction.CHECK, checkpoints=[checkpoint], locator=checkpoint.locator)

        for action in trace.actions:
            if not action.success:
                omit(action, action.error or "source action failed")
                # 失败动作不进脚本，但必须留痕（缺口 5 末条）。
                source_failures.append(
                    {
                        "step_id": action.step_id,
                        "tool": str(action.tool),
                        "status": "failed",
                        "page_path": str(action.params.get("page_path") or ""),
                        "args": format_args(action.params),
                        "error": (action.error or "")[:500],
                    }
                )
                continue
            tool = action.tool
            if tool == ToolName.OPEN_APP:
                omit(action, "OPEN_APP is replaced by deterministic stop/start/wait setup")
            elif tool in {ToolName.INSPECT_SCREEN, ToolName.FINISH}:
                omit(action, f"{tool} is an agent-control action")
            elif tool == ToolName.CLICK_ELEMENT and self._is_nondeterministic_system_click(trace, action):
                omit(action, "desktop AppIcon launch click is replaced by deterministic app setup")
            elif tool == ToolName.CLICK_ELEMENT:
                target = action.params.get("target")
                coordinate = self._runtime_element_coordinate(trace, action)
                recovered = self._recorded_key_locator(trace, action, action.locator)
                if recovered is not None:
                    locator = self.locator_from_candidate(recovered, target, profile, warnings)
                    warnings.append(
                        f"{action.step_id}: runtime locator recovered as "
                        f"{recovered.kind}:{recovered.value!r} from the recorded frame"
                    )
                    if locator is not None:
                        optional = self._optional_fields(locator, str(target or ""))
                        if optional:
                            warnings.append(
                                f"{action.step_id}: conditional control {target!r} "
                                "is rendered as an optional step (skipped when absent)"
                            )
                        add_step(StepAction.CLICK, step_id=action.step_id, locator=locator, **optional)
                    elif coordinate is not None:
                        # 恢复出来的 key 本身不可回放（日期格 / 列表实例）：退回坐标。
                        add_step(
                            StepAction.CLICK,
                            step_id=action.step_id,
                            coordinate=coordinate,
                            locator=self.coordinate_locator(
                                coordinate,
                                bound=self._snapshot_bound(trace, action.before_snapshot_id)
                                or self._trace_bound(trace),
                                label=str(target or ""),
                                warning=f"{action.step_id}: volatile key {recovered.value!r} "
                                f"fell back to coordinate {coordinate}",
                            ),
                        )
                        warnings.append(
                            f"{action.step_id}: volatile key {recovered.value!r} fell back to coordinate {coordinate}"
                        )
                        coordinate_fallbacks += 1
                    else:
                        omit(action, VOLATILE_LOCATOR_OMIT_REASON)
                        continue
                elif (
                    action.locator and action.locator.kind in {LocatorKind.SPATIAL, LocatorKind.VLM_BBOX} and coordinate
                ):
                    add_step(
                        StepAction.CLICK,
                        step_id=action.step_id,
                        coordinate=coordinate,
                        locator=self.coordinate_locator(
                            coordinate,
                            bound=self._snapshot_bound(trace, action.before_snapshot_id) or self._trace_bound(trace),
                            label=str(target or ""),
                            warning=f"{action.step_id}: runtime element {target!r} uses coordinate {coordinate}",
                        ),
                    )
                    warnings.append(f"{action.step_id}: runtime element {target!r} uses coordinate {coordinate}")
                    coordinate_fallbacks += 1
                else:
                    locator = self.locator_from_candidate(action.locator, target, profile, warnings)
                    if locator is None and coordinate is not None:
                        # 运行时候选就是不可回放的易变 key：保留动作，改成坐标兜底。
                        add_step(
                            StepAction.CLICK,
                            step_id=action.step_id,
                            coordinate=coordinate,
                            locator=self.coordinate_locator(
                                coordinate,
                                bound=self._snapshot_bound(trace, action.before_snapshot_id)
                                or self._trace_bound(trace),
                                label=str(target or ""),
                                warning=f"{action.step_id}: volatile locator fell back to coordinate {coordinate}",
                            ),
                        )
                        warnings.append(f"{action.step_id}: volatile locator fell back to coordinate {coordinate}")
                        coordinate_fallbacks += 1
                    elif locator is None:
                        omit(action, VOLATILE_LOCATOR_OMIT_REASON)
                        continue
                    else:
                        optional = self._optional_fields(locator, str(target or ""))
                        if optional:
                            warnings.append(
                                f"{action.step_id}: conditional control {target!r} "
                                "is rendered as an optional step (skipped when absent)"
                            )
                        add_step(StepAction.CLICK, step_id=action.step_id, locator=locator, **optional)
                generated_actions += 1
            elif tool == ToolName.CLICK_COORDINATE:
                raw = action.params.get("coordinate")
                point = tuple(raw) if raw else (0, 0)
                add_step(
                    StepAction.CLICK,
                    step_id=action.step_id,
                    coordinate=(int(point[0]), int(point[1])),
                    locator=self.coordinate_locator(
                        (int(point[0]), int(point[1])),
                        bound=self._snapshot_bound(trace, action.before_snapshot_id) or self._trace_bound(trace),
                        label="",
                        warning=f"{action.step_id}: click uses a coordinate fallback",
                    ),
                )
                warnings.append(f"{action.step_id}: click uses a coordinate fallback")
                coordinate_fallbacks += 1
                generated_actions += 1
            elif tool == ToolName.INPUT_TEXT:
                target = action.params.get("target") or "输入框"
                locator = self.locator_from_candidate(action.locator, target, profile, warnings)
                if locator is None:
                    # 输入框没有坐标兜底：定位器不可回放时省略动作并留痕，
                    # 也不要把文本静默输进一个猜出来的控件。
                    omit(action, VOLATILE_INPUT_OMIT_REASON)
                    continue
                add_step(
                    StepAction.INPUT_TEXT,
                    step_id=action.step_id,
                    locator=locator,
                    text=str(action.params.get("text", "")),
                )
                generated_actions += 1
            elif tool == ToolName.SWIPE:
                direction = str(action.params.get("direction") or "up").upper()
                if direction not in SWIPE_DIRECTIONS:
                    warnings.append(
                        f"{action.step_id}: unknown swipe direction {action.params.get('direction')!r}, fallback to UP"
                    )
                    direction = "UP"
                start_point, end_point = _swipe_points_from_params(action.params)
                add_step(
                    StepAction.SWIPE,
                    step_id=action.step_id,
                    direction=direction.lower(),  # type: ignore[arg-type]
                    start=start_point,
                    end=end_point,
                )
                generated_actions += 1
            elif tool == ToolName.BACK:
                add_step(StepAction.BACK, step_id=action.step_id)
                generated_actions += 1
            elif tool == ToolName.WAIT:
                add_step(
                    StepAction.WAIT,
                    step_id=action.step_id,
                    wait_seconds=float(action.params.get("wait_seconds") or 1),
                )
                generated_actions += 1
            elif tool == ToolName.ASSERT_VISIBLE:
                target = action.params.get("target") or action.params.get("text")
                locator = self.locator_from_candidate(action.locator, target, profile, warnings)
                if locator is None:
                    # ① 已判定「无据标识符」或「易变 key」：先按录制帧精确回查 key/id
                    # （断言不使用宿主容器兜底，否则容器恒存在会把真失败洗成假绿）。
                    recovered = self._recorded_key_locator(trace, action, None, allow_owner_fallback=False)
                    if recovered is not None:
                        locator = self.locator_from_candidate(recovered, target, profile, warnings)
                        warnings.append(
                            f"{action.step_id}: assertion locator recovered as "
                            f"{recovered.kind}:{recovered.value!r} from the recorded frame"
                        )
                if locator is None:
                    # 易变 key 被拒后回退到语义文本锚点（与「完全没有定位器」同一分支）。
                    # CJK / 单 token / 含空格的人读 target 在这里正常拿到 BY.text 锚点；
                    # 只有**无据的标识符形态** target 会让这一步也返回 None。
                    locator = self.locator_from_candidate(None, target, profile, warnings)
                if locator is None:
                    # 仍然没有可信选择器：用 KEY 而非 TEXT —— 标识符形态的 target 本来就是
                    # key 命名空间的成员，且 checkpoints.py 在 locator 为 None 时会 raise
                    # CheckpointRenderError。降级为 soft：不再让脚本变红，同时保留真信号。
                    text = str(target or "")
                    fields = self._polarity_fields(trace, action)
                    # 反向断言（expects_defect）的极性必须保留：它决定脚本里的
                    # 「通过即代表观测到异常现象」注释与下游 polarity 字段。
                    fields["message_zh"] = (
                        f"{UNEXPECTED_CHECKPOINT_MESSAGE}：断言目标 {text!r} 缺少控件证据，已降级为软检查点"
                        if fields.get("polarity")
                        else f"断言目标 {text!r} 缺少控件证据，已降级为软检查点"
                    )
                    attach(
                        CheckpointSpec(
                            kind=CheckpointKind.ELEMENT_EXISTS,
                            locator=LocatorSpec(kind=LocatorKind.KEY, value=text, target_label=text),
                            soft=True,
                            **fields,
                        )
                    )
                    warnings.append(
                        f"{action.step_id}: assertion target {target!r} has no component evidence; "
                        "rendered as a soft checkpoint instead of a hard BY.text() that can never match"
                    )
                else:
                    attach(
                        CheckpointSpec(
                            kind=CheckpointKind.ELEMENT_EXISTS,
                            locator=locator,
                            **self._polarity_fields(trace, action),
                        )
                    )
                generated_assertions += 1
                explicit_assertions += 1
            elif tool == ToolName.ASSERT_TEXT:
                # 修正历史 bug：ASSERT_TEXT 过去渲染为 check_component_exist，根本没有校验文本。
                expected = action.params.get("text") or action.params.get("target") or ""
                locator = self.locator_from_candidate(action.locator, action.params.get("target"), profile, warnings)
                if locator is not None and locator.kind not in {LocatorKind.KEY, LocatorKind.ID}:
                    locator = None
                if locator is not None:
                    locator = self._align_text_assertion_locator(
                        trace, action, locator, str(expected), profile, warnings
                    )
                # 只有 KEY/ID 钉死了具体控件时才用精确 ``text=``；否则用包含匹配，
                # 与运行时 ``evaluate_assertion`` 的 target_variants 模糊匹配保持一致，
                # 避免生成脚本比录制时更严格而在回放中抖动失败。
                # ``expected`` 本身也可能是无据标识符：那时 TEXT_CONTAINS 渲染出的
                # BY.text(标识符) 同样恒假，降级为 soft。
                soft = (
                    locator is None
                    and is_identifier_shaped_target(str(expected))
                    and not self._literal_text_in_snapshots(str(expected))
                )
                if soft:
                    warnings.append(
                        f"{action.step_id}: assert_text expected {expected!r} is identifier-shaped and appears "
                        "in no captured frame; rendered as a soft checkpoint"
                    )
                fields = self._polarity_fields(trace, action)
                if soft:
                    soft_message = f"断言文本 {str(expected)!r} 缺少控件证据，已降级为软检查点"
                    # 反向断言的极性（``polarity="unexpected"``）必须原样保留。
                    fields["message_zh"] = (
                        f"{UNEXPECTED_CHECKPOINT_MESSAGE}：{soft_message}" if fields.get("polarity") else soft_message
                    )
                attach(
                    CheckpointSpec(
                        kind=CheckpointKind.TEXT_EQUALS if locator is not None else CheckpointKind.TEXT_CONTAINS,
                        locator=locator,
                        expected=str(expected),
                        soft=soft,
                        **fields,
                    )
                )
                generated_assertions += 1
                explicit_assertions += 1
            elif tool == ToolName.ASSERT_NOT_VISIBLE:
                target = action.params.get("target")
                locator = self.locator_from_candidate(action.locator, target, profile, warnings)
                if locator is None:
                    # 断言分支不使用宿主容器兜底（容器恒存在 ⇒ 假绿）。
                    recovered = self._recorded_key_locator(trace, action, None, allow_owner_fallback=False)
                    if recovered is not None:
                        locator = self.locator_from_candidate(recovered, target, profile, warnings)
                        warnings.append(
                            f"{action.step_id}: assertion locator recovered as "
                            f"{recovered.kind}:{recovered.value!r} from the recorded frame"
                        )
                if locator is None:
                    # 同 ASSERT_VISIBLE：拒绝易变 key 后退回语义文本锚点（CJK 等仍走 BY.text）。
                    locator = self.locator_from_candidate(None, target, profile, warnings)
                if locator is None:
                    # 只有**无据的标识符形态** target 才会走到这里。无据的
                    # expect_exist=False 恒真：soft 化等于静默放行，因此直接省略
                    # （该断言没有进脚本，计数器不递增，与既有 omit 语义一致）。
                    omit(action, UNGROUNDED_ABSENT_ASSERTION_OMIT_REASON)
                    warnings.append(
                        f"{action.step_id}: absent-assertion target {target!r} has no component "
                        "evidence; an ungrounded expect_exist=False check is vacuously true, so it was omitted"
                    )
                    continue
                attach(
                    CheckpointSpec(
                        kind=CheckpointKind.ELEMENT_ABSENT,
                        locator=locator,
                        **self._polarity_fields(trace, action),
                    )
                )
                generated_assertions += 1
                explicit_assertions += 1
            else:
                omit(action, f"unsupported replay tool: {tool}")

        if explicit_assertions == 0 and self.inject_fallback_assertion:
            warnings.append("source trace has no successful explicit assertion")
            stable = self._first_stable_locator(trace)
            if stable is not None:
                locator = stable
                add_step(
                    StepAction.CHECK,
                    step_id="fallback-assertion",
                    locator=locator,
                    checkpoints=[
                        CheckpointSpec(
                            kind=CheckpointKind.ELEMENT_EXISTS,
                            message_zh=FALLBACK_CHECKPOINT_MESSAGE,
                            locator=locator.model_copy(deep=True),
                        )
                    ],
                )
                warnings.append("generated a fallback assertion from an observed stable locator")
                generated_assertions += 1
            else:
                warnings.append("no stable UI assertion was available")

        if not steps:
            # 历史模板在脚本体为空时发射裸 ``pass``；IR 的「非压测至少一步」不变式
            # 用一条空注释步骤承载它（emitter 对空注释渲染为 ``pass``）。
            add_step(StepAction.NOOP_COMMENT, step_id="empty-body", comment="")

        counts = {
            "source_actions": len(trace.actions),
            "source_assertions": len(trace.assertions),
            "generated_actions": generated_actions,
            "generated_assertions": generated_assertions,
            "omitted_actions": len(omitted),
            "failed_actions": sum(not action.success for action in trace.actions),
            "coordinate_fallbacks": coordinate_fallbacks,
        }
        outcome = self._agent_outcome(trace)
        confidence_factors = self._confidence_factors(
            trace,
            outcome,
            explicit_assertions=explicit_assertions,
            omitted=omitted,
            warnings=warnings,
        )
        promotion_blockers = self._promotion_blockers(trace)
        # 无据断言让脚本在物理上仍可执行，但结论不可信 ⇒ 归晋级层（**不进**
        # runnable_blockers，见 evaluate_runnable 的「只有 2 条物理必要条件」契约）。
        if UNGROUNDED_ASSERTION_FACTOR in confidence_factors:
            promotion_blockers.append(UNGROUNDED_ASSERTION_FACTOR)
        included_actions = counts["generated_actions"] + counts["generated_assertions"]
        replay_eligible, runnable_blockers = evaluate_runnable(
            included_actions=included_actions,
            bundle_name=profile.bundle_name,
            main_ability=profile.main_ability,
        )
        confidence = evaluate_confidence(confidence_factors, outcome=outcome)
        spec = self._assemble(
            steps=steps,
            profile=profile,
            trace=trace,
            case_id=case_id,
            slug=slug,
            scenario=scenario,
            bundle_name=profile.bundle_name,
            main_ability=profile.main_ability,
            device_sn=trace.device_id,
            startup_wait=float(profile.launch_strategy.get("wait_seconds", 2)),
            source_kind="live_run",
            source_id=trace.run_id,
        )
        return CaseBuildResult(
            spec=spec,
            omitted_actions=omitted,
            warnings=warnings,
            counts=counts,
            incomplete_reasons=confidence_factors,  # 兼容别名：与 confidence_factors 同值
            confidence_factors=confidence_factors,
            confidence=confidence,
            replay_eligible=replay_eligible,
            runnable_blockers=runnable_blockers,
            promotion_eligible=replay_eligible and not promotion_blockers,
            promotion_blockers=promotion_blockers,
            purpose="acceptance" if replay_eligible else "diagnostic",
            explicit_assertions=explicit_assertions,
            source_agent_outcome=outcome,  # type: ignore[arg-type]
            source_failures=source_failures,
        )

    # ------------------------------------------------------------------
    # DC 会话录制
    # ------------------------------------------------------------------

    def from_dc_invocations(
        self,
        session_id: str,
        device_id: str,
        invocations: list[Any],
        *,
        bundle_name: str,
        main_ability: str,
        snapshots: list[ScreenSnapshot] | None = None,
        case_id: str | None = None,
        slug: str | None = None,
        profile: TargetAppProfile | None = None,
    ) -> CaseBuildResult:
        """把 DC 会话录制构建为用例 IR（复刻 ``dc/generator.py`` 的逐分支语义）。

        ``invocations`` 的元素类型是 ``dc.models.DcToolInvocation``；此处故意用 ``Any``
        并在函数体内延迟 import，避免 ``cases`` ↔ ``dc`` 的包级循环依赖。

        ``profile`` 可选：DC 会话没有「轮」的概念，但**已蒸馏过 Profile 的应用**可以在
        磁盘上查到稳定定位器证据（``registry.get_any(bundle_name=...)``），传进来就能
        让内容 key 的泛化走 ``validated dynamic`` 分支；``None`` 时退回「时间戳前缀 +
        帧内唯一性」的离线证据（见 :meth:`locator_from_candidate`）。
        ``snapshots`` 是本会话已采集的帧，同时用于坐标解析边界、时间戳会话窗口与
        前缀唯一性验证——三者都零设备零磁盘成本。
        """
        from ..dc.models import DcToolName  # 延迟 import：避免 harmony_test_agent.dc 包初始化成环

        non_replayable = NON_REPLAYABLE_DC_TOOLS
        assert_tools = {DcToolName.ASSERT_VISIBLE, DcToolName.ASSERT_NOT_VISIBLE, DcToolName.ASSERT_TEXT}

        # 帧证据只在这里赋值一次：``locator_from_candidate`` 与坐标边界都读实例属性。
        self._snapshots = list(snapshots or [])
        self._snapshot_window = _snapshot_window(self._snapshots)

        warnings: list[str] = []
        omitted: list[dict[str, str]] = []
        source_failures: list[dict[str, str]] = []
        steps: list[TestStepSpec] = []
        swipe_inferred: dict[str, int] = {}
        included_count = 0
        bundle_for_bounds = None if not snapshots else (snapshots[0].width, snapshots[0].height)

        def add_step(action: StepAction, **kwargs: Any) -> TestStepSpec:
            step = TestStepSpec(
                step_id=kwargs.pop("step_id", f"step-{len(steps) + 1}"),
                index=len(steps) + 1,
                action=action,
                title_zh="",
                **kwargs,
            )
            steps.append(step)
            return step

        def attach(checkpoint: CheckpointSpec) -> None:
            checkpoint.message_zh = checkpoint.message_zh or checkpoint_title_zh(checkpoint)
            if steps:
                steps[-1].checkpoints.append(checkpoint)
            else:
                add_step(StepAction.CHECK, checkpoints=[checkpoint], locator=checkpoint.locator)

        for invocation in invocations:
            if not invocation.success:
                omitted.append(
                    {
                        "invocation_id": invocation.invocation_id,
                        "tool": invocation.tool.value,
                        "reason": "invocation failed",
                    }
                )
                # 失败动作不进脚本，但必须留痕：否则「录制期间有过失败」在产物层面被抹掉。
                source_failures.append(
                    {
                        "invocation_id": invocation.invocation_id,
                        "tool": invocation.tool.value,
                        "status": invocation.status.value
                        if hasattr(invocation.status, "value")
                        else str(invocation.status),
                        "page_path": invocation.page_path,
                        "args": format_args(invocation.args),
                        "error": (invocation.error or invocation.result_summary or "")[:500],
                    }
                )
                continue

            if invocation.tool in non_replayable:
                omitted.append(
                    {
                        "invocation_id": invocation.invocation_id,
                        "tool": invocation.tool.value,
                        "reason": "not replayable in Hypium (observation/management/shell)",
                    }
                )
                add_step(
                    StepAction.NOOP_COMMENT,
                    step_id=invocation.invocation_id,
                    comment=f"skipped: {invocation.tool.value} {format_args(invocation.args)}".rstrip(),
                )
                continue

            if invocation.tool == DcToolName.KEY_EVENT:
                key = str(invocation.args.get("key", ""))
                # Back/backspace 走 StepAction.BACK（渲染 driver.go_back()）；其余按键只要
                # 能归一到 ALLOWED_KEY_EVENTS 的规范拼写就映射成 KEY_EVENT（Enter/Home/
                # 音量键），两个 emitter 已完整支持，此前却在这里被无谓丢弃。
                if key.strip().lower() not in {"back", "backspace"} and canonical_key_event(key) is None:
                    omitted.append(
                        {
                            "invocation_id": invocation.invocation_id,
                            "tool": invocation.tool.value,
                            "reason": f"key_event({key!r}) is not replayable (unsupported key)",
                        }
                    )
                    add_step(
                        StepAction.NOOP_COMMENT,
                        step_id=invocation.invocation_id,
                        comment=f"skipped: key_event({key!r})",
                    )
                    continue

            produced, counted = self._dc_step(
                invocation,
                DcToolName,
                add_step=add_step,
                attach=attach,
                warnings=warnings,
                swipe_inferred=swipe_inferred,
                bound=bundle_for_bounds,
                omitted=omitted,
                profile=profile,
            )
            if not produced:
                # ``_dc_step`` 自己会为「有明确语义的省略」写 reason（例如易变定位器），
                # 只有它完全无法归类的调用才落到这条兜底文案。
                if not any(item["invocation_id"] == invocation.invocation_id for item in omitted):
                    omitted.append(
                        {
                            "invocation_id": invocation.invocation_id,
                            "tool": invocation.tool.value,
                            "reason": "could not map to action",
                        }
                    )
                continue
            if counted:
                included_count += 1

        for direction in SWIPE_DIRECTION_ORDER:
            count = swipe_inferred.get(direction, 0)
            if count:
                warnings.append(f"swipe direction inferred as {direction} x{count} (from start/end coordinates)")

        if included_count == 0:
            warnings.append("no replayable operations were recorded")
            add_step(StepAction.NOOP_COMMENT, step_id="no-replayable-operations", comment=NO_REPLAYABLE_COMMENT)

        explicit_assertions = sum(1 for item in invocations if item.success and item.tool in assert_tools)
        replay_eligible, runnable_blockers = evaluate_runnable(
            included_actions=included_count,
            bundle_name=bundle_name,
            main_ability=main_ability,
        )
        # 热启动录制：生成脚本的 setup 是 stop_app + start_app（冷启动），而录制里首个
        # start_app 没有 force-stop ⇒ 回放可能落在与录制不同的页面上。
        # 只在录制里**有** start_app 时判定：没有启动锚点就没有可比对象（这同时守住了
        # test_dc_promotion_blockers.py 里那些纯 CLICK 夹具的列表全等断言）。
        # 判定基于**整个会话**的 invocations，且只看首个 start_app —— 脚本 setup 也只有一个
        # 冷启动锚点。此警告必须在这里发出：下面 confidence_factors_from_warnings 要读它。
        start_apps = [item for item in invocations if item.tool == DcToolName.START_APP]
        warm_start_recording = bool(start_apps) and not (start_apps[0].args or {}).get("reset")
        if warm_start_recording:
            warnings.append(
                "the recording began with a warm app launch (no force-stop before start_app); "
                "the generated script cold-starts, so the replay may begin on a different page"
            )
        confidence_factors: list[str] = []
        if explicit_assertions == 0:
            # 「无显式断言」从阻断条件降为 medium 置信度：脚本照样能跑，只是没有检查点。
            confidence_factors.append("no explicit assert_* tool call was recorded")
        # 定位器质量顾虑（未验证动态 key / 时间戳泛化依据 / 易变 key）与 Live 同一映射。
        confidence_factors.extend(confidence_factors_from_warnings(warnings))
        confidence = evaluate_confidence(confidence_factors, outcome="completed")

        counts = {
            "source_actions": len(invocations),
            "source_assertions": explicit_assertions,
            "generated_actions": included_count,
            "generated_assertions": sum(len(step.checkpoints) for step in steps),
            "omitted_actions": len(omitted),
            "failed_actions": sum(not item.success for item in invocations),
            "coordinate_fallbacks": sum(
                1 for step in steps if step.locator is not None and step.locator.kind == LocatorKind.COORDINATE
            ),
        }
        spec = self._assemble(
            steps=steps,
            profile=profile,
            trace=None,
            case_id=case_id,
            slug=slug,
            scenario=ScenarioKind.CORE_FLOW,
            bundle_name=bundle_name,
            main_ability=main_ability,
            device_sn=device_id,
            startup_wait=2.0,
            source_kind="dc_session",
            source_id=session_id,
        )
        # 晋级资格与「能否执行」解耦：DC 录制既没有跨轮 Profile 验证，也没有跨会话证据，
        # 因此默认不能作为 Profile 晋级证据（历史实现无条件给了 True，是虚假承诺）。
        promotion_blockers: list[str] = []
        if profile is None or profile.status not in {ProfileStatus.CANDIDATE, ProfileStatus.VERIFIED}:
            promotion_blockers.append("dc recording has no cross-round locator evidence")
        # **必须追加在既有 profile blocker 之后**：promotion_blockers 的列表全等断言依赖顺序稳定。
        if UNGROUNDED_ASSERTION_FACTOR in confidence_factors:
            promotion_blockers.append(UNGROUNDED_ASSERTION_FACTOR)
        if warm_start_recording:
            promotion_blockers.append(WARM_START_RECORDING_BLOCKER)
        return CaseBuildResult(
            spec=spec,
            omitted_actions=omitted,
            warnings=warnings,
            counts=counts,
            incomplete_reasons=confidence_factors,  # 兼容别名：与 confidence_factors 同值
            confidence_factors=confidence_factors,
            confidence=confidence,
            replay_eligible=replay_eligible,
            runnable_blockers=runnable_blockers,
            promotion_eligible=replay_eligible and not promotion_blockers,
            promotion_blockers=promotion_blockers,
            purpose="acceptance" if replay_eligible else "diagnostic",
            explicit_assertions=explicit_assertions,
            source_failures=source_failures,
        )

    @staticmethod
    def _optional_fields(locator: Any, *values: str) -> dict[str, Any]:
        """条件存在控件 → ``TestStepSpec.optional`` 的构造字段（见 :func:`is_optional_control`）。

        只对**选择器型**定位器生效：坐标点击没有可探测的控件，降级没有意义。
        """
        if locator is None or getattr(locator, "kind", None) == LocatorKind.COORDINATE:
            return {}
        candidates = [str(getattr(locator, "value", "") or ""), *(value or "" for value in values)]
        return {"optional": True} if is_optional_control(*candidates) else {}

    def _dc_step(
        self,
        invocation: Any,
        dc_tool_name: Any,
        *,
        add_step: Any,
        attach: Any,
        warnings: list[str],
        swipe_inferred: dict[str, int],
        bound: tuple[int, int] | None,
        omitted: list[dict[str, str]],
        profile: TargetAppProfile | None = None,
    ) -> tuple[bool, bool]:
        """把一个可回放的 DC 调用转为 IR 步骤；返回 ``(是否产出内容, 是否计入 included_count)``。"""
        tool = invocation.tool
        args = dict(invocation.args or {})
        element: UIElement | None = invocation.resolved_element
        locator: LocatorSpec | None = None

        def omit(reason: str) -> None:
            """在 ``_dc_step`` 内部直接写省略记录（reason 比调用方的兜底文案精确）。"""
            omitted.append(
                {
                    "invocation_id": invocation.invocation_id,
                    "tool": invocation.tool.value,
                    "reason": reason,
                }
            )

        if tool == dc_tool_name.START_APP:
            add_step(
                StepAction.NOOP_COMMENT,
                step_id=invocation.invocation_id,
                comment=f"start_app: {args.get('bundle_name', '?')} (handled by script setup)",
            )
            return True, True

        if tool == dc_tool_name.CLICK:
            raw_value = (element.key or element.id) if element is not None else ""
            locator = self._dc_locator(element, str(args.get("target") or "") or None, profile, warnings)
            if locator is not None:
                optional = self._optional_fields(locator, raw_value, element.content if element is not None else "")
                if optional:
                    warnings.append(
                        f"{invocation.invocation_id}: conditional control {raw_value!r} "
                        "is rendered as an optional step (skipped when absent)"
                    )
                add_step(
                    StepAction.CLICK,
                    step_id=invocation.invocation_id,
                    locator=locator,
                    # 步骤标签保留**录制时的原始 key**（历史行为），前缀泛化只影响选择器。
                    text=raw_value,
                    **optional,
                )
            else:
                # 没有可回放的稳定定位器（无命中 / 易变 key 被拒）→ 坐标兜底。
                coordinate = args.get("coordinate") or [args.get("x", 0), args.get("y", 0)]
                point = (int(coordinate[0]), int(coordinate[1]))
                add_step(
                    StepAction.CLICK,
                    step_id=invocation.invocation_id,
                    coordinate=point,
                    locator=self.coordinate_locator(
                        point,
                        bound=bound,
                        label="",
                        warning=f"{invocation.invocation_id}: click fell back to coordinate {point}",
                    ),
                )
            return True, True

        if tool == dc_tool_name.SWIPE:
            direction = str(args.get("direction", "")).upper()
            if direction not in SWIPE_DIRECTIONS:
                start = args.get("start", [0, 0])
                end = args.get("end", [0, 0])
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
            add_step(
                StepAction.SWIPE,
                step_id=invocation.invocation_id,
                direction=direction.lower(),  # type: ignore[arg-type]
            )
            return True, True

        if tool == dc_tool_name.INPUT_TEXT:
            locator = self._dc_locator(element, "输入框", profile, warnings)
            if locator is None:
                if element is not None and (element.key or element.id):
                    # 输入框没有坐标兜底，也不能把文本输进猜出来的控件：省略并留痕。
                    omit(VOLATILE_INPUT_OMIT_REASON)
                    return False, False
                locator = self.locator_from_candidate(None, "输入框", profile, warnings)
            add_step(
                StepAction.INPUT_TEXT,
                step_id=invocation.invocation_id,
                locator=locator,
                text=str(args.get("text", "")),
            )
            return True, True

        if tool == dc_tool_name.BACK:
            add_step(StepAction.BACK, step_id=invocation.invocation_id)
            return True, True

        if tool == dc_tool_name.WAIT:
            seconds = args.get("wait_seconds", args.get("seconds", 1))
            add_step(
                StepAction.WAIT,
                step_id=invocation.invocation_id,
                wait_seconds=float(seconds or 1),
            )
            return True, True

        if tool in {dc_tool_name.ASSERT_VISIBLE, dc_tool_name.ASSERT_NOT_VISIBLE, dc_tool_name.ASSERT_TEXT}:
            target = str(args.get("target") or args.get("text") or "")
            locator = self._dc_locator(element, target, profile, warnings)
            # DC 侧**不另造帧恢复路径**：``_dc_locator`` 用的 ``invocation.resolved_element``
            # 已经是帧命中的元素，``resolved_element`` 为 None 时没有别的帧可查
            # （``_recorded_key_locator`` 依赖 trace/action，DC 侧不存在）。
            if tool == dc_tool_name.ASSERT_VISIBLE:
                if locator is None:
                    locator = self.locator_from_candidate(None, target, profile, warnings)
                if locator is None:
                    # 无据标识符：BY.text() 恒假。用 KEY 而非 TEXT —— 标识符本来就是 key
                    # 命名空间的成员，且 checkpoints.py 在 locator 为 None 时会 raise。
                    attach(
                        CheckpointSpec(
                            kind=CheckpointKind.ELEMENT_EXISTS,
                            message_zh=f"断言目标 {target!r} 缺少控件证据，已降级为软检查点",
                            locator=LocatorSpec(kind=LocatorKind.KEY, value=target, target_label=target),
                            soft=True,
                        )
                    )
                    warnings.append(
                        f"{invocation.invocation_id}: assertion target {target!r} has no component evidence; "
                        "rendered as a soft checkpoint instead of a hard BY.text() that can never match"
                    )
                else:
                    attach(CheckpointSpec(kind=CheckpointKind.ELEMENT_EXISTS, message_zh="", locator=locator))
            elif tool == dc_tool_name.ASSERT_NOT_VISIBLE:
                if locator is None:
                    locator = self.locator_from_candidate(None, target, profile, warnings)
                if locator is None:
                    # 无据的 expect_exist=False 恒真：soft 化等于静默放行，直接省略。
                    # 该调用没有产出任何内容 ⇒ ``(False, False)``，不把 included_count 虚增。
                    omit(UNGROUNDED_ABSENT_ASSERTION_OMIT_REASON)
                    warnings.append(
                        f"{invocation.invocation_id}: absent-assertion target {target!r} has no component "
                        "evidence; an ungrounded expect_exist=False check is vacuously true, so it was omitted"
                    )
                    return False, False
                attach(CheckpointSpec(kind=CheckpointKind.ELEMENT_ABSENT, message_zh="", locator=locator))
            else:
                soft = (
                    locator is None
                    and is_identifier_shaped_target(target)
                    and not self._literal_text_in_snapshots(target)
                )
                if soft:
                    warnings.append(
                        f"{invocation.invocation_id}: assert_text expected {target!r} is identifier-shaped and "
                        "appears in no captured frame; rendered as a soft checkpoint"
                    )
                attach(
                    CheckpointSpec(
                        # 与 Live 侧同一规则：无结构化定位器时用包含匹配（对齐运行时模糊匹配）。
                        kind=CheckpointKind.TEXT_EQUALS if locator is not None else CheckpointKind.TEXT_CONTAINS,
                        message_zh=(f"断言文本 {target!r} 缺少控件证据，已降级为软检查点" if soft else ""),
                        locator=locator,
                        expected=target,
                        soft=soft,
                    )
                )
            # 历史实现里断言也会渲染成一行脚本体，因此同样计入 included_count
            # （``evaluate_runnable`` 的「至少 1 个可回放动作」依赖这个口径）。
            return True, True

        if tool == dc_tool_name.KEY_EVENT:
            key = str(args.get("key", ""))
            # 已知遗留（本次不动）：``backspace`` → ``driver.go_back()`` 在语义上是错的
            # （退格 ≠ 返回）。``generation/standalone.py`` 的归一化表同样这么写，
            # 改它会牵动既有行为，且与本次两条确定性回放失败无关。
            if key.strip().lower() in {"back", "backspace"}:
                add_step(StepAction.BACK, step_id=invocation.invocation_id)
                return True, True
            canonical = canonical_key_event(key)
            if canonical is not None:
                add_step(StepAction.KEY_EVENT, step_id=invocation.invocation_id, key=canonical)
                return True, True
            return False, False

        return False, False

    # ------------------------------------------------------------------
    # 压测 / 缺陷复现（实现在各自的模块里，这里只做统一入口）
    # ------------------------------------------------------------------

    def from_stress_request(
        self,
        request: Any,
        profile: TargetAppProfile | None = None,
        base_spec: TestCaseSpec | None = None,
    ) -> CaseBuildResult:
        """把压测请求构建为用例 IR（委托 ``cases/stress.py``，避免模块成环）。"""
        from .stress import build_stress_case

        return build_stress_case(self, request, profile, base_spec=base_spec)

    def from_bug_repro(
        self,
        plan: Any,
        request: Any,
        profile: TargetAppProfile | None = None,
    ) -> CaseBuildResult:
        """把缺陷复现计划构建为用例 IR（委托 ``cases/bug_repro.py``）。"""
        from .bug_repro import build_bug_repro_case

        return build_bug_repro_case(self, plan, request, profile)

    # ------------------------------------------------------------------
    # 定位器策略
    # ------------------------------------------------------------------

    def _align_text_assertion_locator(
        self,
        trace: RunTrace,
        action: ActionResult,
        locator: LocatorSpec,
        expected: str,
        profile: TargetAppProfile | None,
        warnings: list[str],
    ) -> LocatorSpec | None:
        """把 TEXT_EQUALS 检查点对齐到「真正持有该文本的元素」。

        真机复盘（run-20260921T063745Z-ba36e30e 的知乎++回放）：``assert_text 'OpenHarmony'`` 的
        运行时定位器是**容器** key ``p2_search_input_container``，脚本因此断言「容器的 text 等于
        OpenHarmony」——容器自身 text 为空，回放必然失败。断言帧里真正的文本持有者是输入框本身，
        这里改用它的 key/id，并保留精确 ``text=`` 语义。
        """
        if not expected:
            return locator
        snapshot = self._assertion_frame(trace, action)
        if snapshot is None:
            return locator
        recorded = self._element_for_locator(snapshot, locator)
        if recorded is not None and self._holds_text(recorded, expected):
            return locator
        holder = next(
            (item for item in snapshot.elements if self._holds_text(item, expected)),
            None,
        )
        if holder is None:
            return locator
        candidate = self._key_id_candidate(holder)
        if candidate is None or candidate.value == locator.value:
            return locator
        warnings.append(
            f"{action.step_id}: text assertion re-targeted from {locator.value!r} to "
            f"{candidate.value!r} (the element that actually holds {expected!r})"
        )
        return self.locator_from_candidate(candidate, expected, profile, warnings)

    @staticmethod
    def _expects_defect(trace: RunTrace, action: ActionResult) -> bool:
        """该动作的断言是否为**反向断言**（通过即代表观测到缺陷）。

        两个来源都认：断言结果上由运行期透传的 ``expects_defect``（决定即时 finding），
        以及计划步骤 ``PlannedStep.expects_defect``（决定 IR 的 ``polarity``）。
        """
        if action.assertion is not None and action.assertion.expects_defect:
            return True
        return any(step.step_id == action.step_id and step.expects_defect for step in trace.plan)

    @staticmethod
    def _polarity_fields(trace: RunTrace, action: ActionResult) -> dict[str, Any]:
        """``expects_defect`` → ``CheckpointSpec`` 的构造字段。

        非反向断言返回的 ``message_zh=""`` 与历史行为逐字一致（由 ``attach`` 兜底填标题）。
        """
        if not CaseBuilder._expects_defect(trace, action):
            return {"message_zh": ""}
        expected = next(
            (step.expected for step in trace.plan if step.step_id == action.step_id and step.expected),
            "",
        )
        return {
            "message_zh": f"{UNEXPECTED_CHECKPOINT_MESSAGE}：{expected}" if expected else UNEXPECTED_CHECKPOINT_MESSAGE,
            "polarity": "unexpected",
        }

    @staticmethod
    def _assertion_frame(trace: RunTrace, action: ActionResult) -> ScreenSnapshot | None:
        """断言求值所用帧：优先 after 帧，其次 before 帧。"""
        for snapshot_id in (action.after_snapshot_id, action.before_snapshot_id):
            if not snapshot_id:
                continue
            snapshot = next((item for item in trace.snapshots if item.snapshot_id == snapshot_id), None)
            if snapshot is not None:
                return snapshot
        return None

    @staticmethod
    def _element_for_locator(snapshot: ScreenSnapshot, locator: LocatorSpec) -> UIElement | None:
        for item in snapshot.elements:
            if (locator.kind == LocatorKind.KEY and item.key == locator.value) or (
                locator.kind == LocatorKind.ID and item.id == locator.value
            ):
                return item
        return None

    @staticmethod
    def _holds_text(element: UIElement, expected: str) -> bool:
        wanted = expected.strip()
        if not wanted:
            return False
        return wanted in {element.content.strip(), element.description.strip()}

    def _literal_text_in_snapshots(self, value: str) -> bool:
        """该字面值是否真的作为某个元素的 content/description 出现在已采集帧里。"""
        wanted = (value or "").strip()
        if not wanted:
            return False
        return any(self._holds_text(element, wanted) for snapshot in self._snapshots for element in snapshot.elements)

    @staticmethod
    def _key_id_candidate(element: UIElement) -> LocatorCandidate | None:
        for candidate in element.locator_candidates:
            if candidate.kind in {LocatorKind.KEY, LocatorKind.ID} and candidate.value:
                return candidate
        if element.key:
            return LocatorCandidate(kind=LocatorKind.KEY, value=element.key, score=1)
        if element.id:
            return LocatorCandidate(kind=LocatorKind.ID, value=element.id, score=1)
        return None

    @staticmethod
    def _recorded_key_locator(
        trace: RunTrace,
        action: ActionResult,
        runtime_locator: LocatorCandidate | None,
        *,
        allow_owner_fallback: bool = True,
    ) -> LocatorCandidate | None:
        """运行时只拿到空间/坐标回退时，按 ``params['target']`` 回查原始帧取 key/id 定位器。

        真机复盘（run-20260921T053514Z-8418044b 的回放）：第 13 步「确认保存」在运行时是
        SPATIAL 回退，生成脚本因此写成 ``driver.touch((1212, 237))``；回放时那一下没有落到
        保存按钮上，编辑页一直开着，随后的 ``agenda_item_title`` 断言必然失败。原始帧里该
        元素其实带 ``add_agenda_comfrim`` key——恢复出来就能渲染成稳定的 key 选择器。

        只恢复 key/id：内容文本选择器对坐标回退不是稳定替代，保持坐标兜底语义不变。

        ``allow_owner_fallback=False``（断言分支）时不做宿主容器兜底，见下方注释。
        """
        if runtime_locator is not None and runtime_locator.kind in {
            LocatorKind.KEY,
            LocatorKind.ID,
            LocatorKind.TEXT,
            LocatorKind.TYPE_TEXT,
        }:
            return None
        target = str(action.params.get("target") or "")
        if not target or not action.before_snapshot_id:
            return None
        snapshot = next(
            (item for item in trace.snapshots if item.snapshot_id == action.before_snapshot_id),
            None,
        )
        if snapshot is None:
            return None
        element = next((item for item in snapshot.elements if item.element_id == target), None)
        if element is None:
            element = next((item for item in snapshot.elements if target in {item.key, item.id}), None)
        if element is None:
            return None
        for candidate in element.locator_candidates:
            if candidate.kind in {LocatorKind.KEY, LocatorKind.ID} and candidate.value:
                return candidate
        if element.key:
            return LocatorCandidate(kind=LocatorKind.KEY, value=element.key, score=1)
        if element.id:
            return LocatorCandidate(kind=LocatorKind.ID, value=element.id, score=1)
        # 断言**不得**使用宿主容器兜底：对点击，「包含该元素中心的最小带 key 元素」是合理的
        # 空间归属推断；但对断言，容器往往**恒存在**，用它会把「内容不存在」这个真失败
        # 洗成假绿。断言只接受上面精确的 element_id / key / id 命中。
        if not allow_owner_fallback:
            return None
        # 自绘/图标按钮常常既无 key 也无 id，但它的宿主容器带 key（``add_agenda_comfrim`` 包裹
        # 一个无 key 的 Button）——取「包含该元素中心的最小带 key 元素」作为归属控件。
        # 面积相同时取层级更靠后（更靠上层）的那个：弹层里的确认按钮与背景页的 more_menu
        # 在 dump 里 bbox 完全一致，只有顺序能区分。
        if element.bbox is None:
            return None
        center_x, center_y = element.bbox.center
        owners = [
            (index, item)
            for index, item in enumerate(snapshot.elements)
            if item.bbox is not None
            and (item.key or item.id)
            and item.bbox.left <= center_x <= item.bbox.right
            and item.bbox.top <= center_y <= item.bbox.bottom
        ]
        if not owners:
            return None
        _, owner = min(owners, key=lambda pair: (pair[1].bbox.area, -pair[0]))
        if owner.key:
            return LocatorCandidate(kind=LocatorKind.KEY, value=owner.key, score=0.9)
        return LocatorCandidate(kind=LocatorKind.ID, value=owner.id, score=0.9)

    def _dc_locator(
        self,
        element: Any | None,
        target: str | None,
        profile: TargetAppProfile | None,
        warnings: list[str],
    ) -> LocatorSpec | None:
        """DC ``resolved_element`` → IR 定位器：**必须**经 :meth:`locator_from_candidate`。

        直接手工构造 ``LocatorSpec`` 会同时跳过动态 key 泛化（``_DYNAMIC_LOCATOR``）与
        易变 key 拒绝（``is_unreplayable_locator_key``），导致
        ``add_agenda_title-<epoch_ms>`` 这类实例 key 被原样写进脚本，回放必然
        ``Can't find component with [BY.key(...)]``（真机复盘 dc-20260922T115708Z-f2acffa4）。

        返回 ``None`` 表示「这个元素给不出可回放的稳定定位器」：调用方负责坐标兜底
        （点击）或省略动作（输入框）。``evidence.source`` 保持 ``dc_resolved_element``，
        DC 录制链路的既有断言依赖它。
        """
        if element is None or not (element.key or element.id):
            return None
        kind = LocatorKind.KEY if element.key else LocatorKind.ID
        candidate = LocatorCandidate(kind=kind, value=element.key or element.id)
        spec = self.locator_from_candidate(candidate, target or candidate.value, profile, warnings)
        if spec is None:
            return None
        # 保留 DC 的证据来源标记：既有测试断言 evidence.source == "dc_resolved_element"。
        evidence = spec.evidence or LocatorEvidence()
        return spec.model_copy(
            update={"evidence": evidence.model_copy(update={"source": "dc_resolved_element"})},
            deep=True,
        )

    def _prefix_unique_in_snapshots(
        self,
        prefix: str,
        kind: LocatorKind,
        snapshots: list[ScreenSnapshot] | None,
    ) -> tuple[bool, int, int]:
        """在已采集帧里验证前缀唯一性；返回 ``(是否唯一, 含该前缀的帧数, 最大同帧命中数)``。

        零设备零磁盘成本：``from_dc_invocations`` 已收到 snapshots，``from_trace`` 直接读
        ``trace.snapshots``。判定标准是「至少出现过一次，且没有任何一帧里同前缀匹配 >1 个
        控件」——``BY.key(prefix, STARTS_WITH)`` 在同帧匹配多个控件时 hypium 取第一个，
        可能操作到错误元素，因此不唯一就**不泛化**，改为响亮警告 + 低置信度。
        """
        if not prefix or not snapshots:
            return False, 0, 0
        frames = 0
        max_hits = 0
        for snapshot in snapshots:
            hits = 0
            for item in snapshot.elements:
                value = item.key if kind == LocatorKind.KEY else item.id
                if value and value.startswith(prefix):
                    hits += 1
            if hits:
                frames += 1
                max_hits = max(max_hits, hits)
        return frames >= 1 and max_hits <= 1, frames, max_hits

    def locator_from_candidate(
        self,
        locator: LocatorCandidate | None,
        target: str | None,
        profile: TargetAppProfile | None,
        warnings: list[str],
    ) -> LocatorSpec | None:
        """把运行时候选定位器提升为带 Profile 证据的 IR 定位器。

        逐条复刻 ``HypiumGenerator._selector`` 的语义与**警告文案**：
        动态 key 有跨轮证据 → 前缀泛化 + ``validated dynamic ...`` 警告；
        无跨轮证据但时间戳前缀在已采集帧里唯一 → 离线泛化 + ``timestamp-suffixed ...`` 警告；
        两者都不成立 → 保留精确值 + ``unvalidated dynamic ...`` 警告（该警告仍会喂给
        ``incomplete_reasons``）；易变 key（日期格 / 时钟读数 / 列表实例）→ **返回 ``None``**，
        由调用方坐标兜底或省略动作；完全无定位器 → ``BY.text(target)`` 兜底 + 语义回退警告。
        """
        if (
            locator is not None
            and locator.kind in {LocatorKind.KEY, LocatorKind.ID}
            # 可折叠的实例 ID（``add_agenda_title-<epoch_ms>``）走泛化，不在这条拒绝。
            and _DYNAMIC_LOCATOR.fullmatch(locator.value) is None
            and is_unreplayable_locator_key(locator.value)
        ):
            method = "key" if locator.kind == LocatorKind.KEY else "id"
            warnings.append(
                f"volatile {method} {locator.value!r} encodes a date/clock/list-instance and cannot "
                "be used as a replay locator"
            )
            return None
        if locator is not None and locator.kind in {LocatorKind.KEY, LocatorKind.ID}:
            method = "key" if locator.kind == LocatorKind.KEY else "id"
            dynamic = _DYNAMIC_LOCATOR.fullmatch(locator.value)
            if dynamic:
                prefix = dynamic.group(1)
                evidence = self._stable_locator_evidence(locator, prefix, profile)
                if evidence is not None:
                    # 前缀可能是 ``foo-``（连字符 + 毫秒时间戳）或 ``foo_``：Hypium 的
                    # starts_with 对二者同样有效，用前缀本身作为选择器值。
                    warnings.append(
                        f"validated dynamic {method} {locator.value!r} generalized to unique prefix {prefix!r}"
                    )
                    return LocatorSpec(
                        kind=locator.kind,
                        value=prefix,
                        match=MatchMode.STARTS_WITH,
                        target_label=target or prefix,
                        evidence=evidence,
                    )
                # DC 会话没有「轮」，profile 恒为 None；但时间戳实例 key 的前缀唯一性
                # 可以用本会话已采集的帧离线确认，零设备零磁盘成本。
                ts_prefix = _timestamp_suffix(locator.value, self._snapshot_window)
                if ts_prefix is not None:
                    unique, frames, max_hits = self._prefix_unique_in_snapshots(
                        ts_prefix, locator.kind, self._snapshots
                    )
                    if unique:
                        warnings.append(
                            f"timestamp-suffixed {method} {locator.value!r} generalized to prefix "
                            f"{ts_prefix!r} (unique in {frames} captured frame(s); "
                            "no cross-round Profile evidence)"
                        )
                        return LocatorSpec(
                            kind=locator.kind,
                            value=ts_prefix,
                            match=MatchMode.STARTS_WITH,
                            target_label=target or ts_prefix,
                        )
                    warnings.append(
                        f"timestamp-suffixed {method} {locator.value!r} matched {max_hits} components "
                        f"in {frames} frame(s); prefix is not unique, retained as an exact selector "
                        "that will fail on replay"
                    )
                else:
                    warnings.append(
                        f"unvalidated dynamic {method} {locator.value!r} retained as an exact diagnostic selector"
                    )
            return LocatorSpec(
                kind=locator.kind,
                value=locator.value,
                target_label=target or locator.value,
                evidence=self._exact_locator_evidence(locator, profile),
            )
        if locator is not None and locator.kind == LocatorKind.TEXT:
            return LocatorSpec(
                kind=LocatorKind.TEXT,
                value=locator.value,
                target_label=target or locator.value,
                evidence=self._exact_locator_evidence(locator, profile),
            )
        if locator is not None and locator.kind == LocatorKind.TYPE_TEXT:
            type_name, _, text = locator.value.partition("|")
            return LocatorSpec(
                kind=LocatorKind.TYPE_TEXT,
                value=f"{type_name}|{text}",
                target_label=target or text or type_name,
            )
        target_text = (target or "").strip()
        if is_identifier_shaped_target(target_text) and not self._literal_text_in_snapshots(target_text):
            warnings.append(
                f"ungrounded target {target!r} is identifier-shaped and appears in no captured frame; "
                "BY.text() on it can never match"
            )
            return None
        warnings.append(f"semantic target {target!r} fell back to exact text")
        return LocatorSpec(kind=LocatorKind.TEXT, value=target or "", target_label=target or "")

    def coordinate_locator(
        self,
        point: tuple[int, int],
        *,
        bound: tuple[int, int] | None,
        label: str,
        warning: str,
    ) -> LocatorSpec:
        """构造坐标定位器；IR 不变式要求坐标定位器必须带解析边界与警告。"""
        resolution = bound or UNKNOWN_RESOLUTION_BOUND
        text = warning if bound else f"{warning} (resolution bound unknown, recorded as {resolution})"
        return LocatorSpec(
            kind=LocatorKind.COORDINATE,
            coordinate=(int(point[0]), int(point[1])),
            resolution_bound=(int(resolution[0]), int(resolution[1])),
            target_label=label,
            warning=text,
        )

    def _stable_locator_evidence(
        self, locator: LocatorCandidate, prefix: str, profile: TargetAppProfile | None
    ) -> LocatorEvidence | None:
        if profile is None:
            return None
        method = "key" if locator.kind == LocatorKind.KEY else "id"
        threshold = self.min_observed_rounds
        accepted_patterns = _dynamic_pattern_variants(prefix, locator.value)
        for item in profile.stable_locator_inventory:
            if (
                getattr(item, method) == locator.value
                and item.dynamic_pattern in accepted_patterns
                and item.observed_rounds >= threshold
                and item.unique_match_rounds >= threshold
            ):
                return LocatorEvidence(
                    observed_rounds=item.observed_rounds,
                    unique_match_rounds=item.unique_match_rounds,
                    dynamic_pattern=item.dynamic_pattern,
                    page_signature=item.page_signature,
                    confidence=item.confidence,
                    source="profile_stable_locator",
                )
        return None

    def _exact_locator_evidence(
        self, locator: LocatorCandidate, profile: TargetAppProfile | None
    ) -> LocatorEvidence | None:
        """精确值命中某个 StableLocator 时，把 Profile 证据带进用例 IR。"""
        if profile is None:
            return None
        method = "key" if locator.kind == LocatorKind.KEY else "id" if locator.kind == LocatorKind.ID else None
        for item in profile.stable_locator_inventory:
            matched = (
                (method == "key" and item.key == locator.value)
                or (method == "id" and item.id == locator.value)
                or (method is None and item.text == locator.value)
            )
            if matched:
                return LocatorEvidence(
                    observed_rounds=item.observed_rounds,
                    unique_match_rounds=item.unique_match_rounds,
                    dynamic_pattern=item.dynamic_pattern,
                    page_signature=item.page_signature,
                    confidence=item.confidence,
                    source="profile_stable_locator",
                )
        return None

    # ------------------------------------------------------------------
    # 兼容性辅助（从 generation/hypium.py 原样搬入）
    # ------------------------------------------------------------------

    @staticmethod
    def _agent_outcome(trace: RunTrace) -> str:
        if trace.agent_outcome != "unknown":
            return trace.agent_outcome
        if trace.state == RunState.COMPLETED:
            return "completed"
        if trace.state == RunState.STOPPED_BY_USER:
            return "stopped"
        if trace.error or any(not action.success for action in trace.actions):
            return "failed"
        if trace.actions and trace.actions[-1].tool == ToolName.FINISH and trace.actions[-1].success:
            return "completed"
        return "unknown"

    @staticmethod
    def _confidence_factors(
        trace: RunTrace,
        outcome: str,
        *,
        explicit_assertions: int,
        omitted: list[dict[str, str]],
        warnings: list[str],
    ) -> list[str]:
        """质量顾虑清单：**不阻断执行**，只决定 ``confidence`` 分档与前端提示。

        文案与历史 ``incomplete_reasons`` 逐字一致（``tests/unit/test_case_builder_trace.py``
        钉住这些字符串），只是**去掉** ``provisional`` / ``live_mode`` 两条——它们属于
        Profile 晋级资格（``_promotion_blockers``），与「这脚本能不能跑」无关。
        """
        factors: list[str] = []
        if outcome != "completed":
            factors.append(f"source agent outcome is {outcome}")
        if trace.agent_error:
            factors.append(f"source agent error: {trace.agent_error}")
        if any(not action.success for action in trace.actions):
            factors.append("source trace contains failed actions")
        if not trace.actions or trace.actions[-1].tool != ToolName.FINISH or not trace.actions[-1].success:
            factors.append("source trace does not end with a successful FINISH action")
        if explicit_assertions == 0:
            factors.append("source trace has no successful explicit assertion")
        if any(item["reason"].startswith("unsupported replay tool") for item in omitted):
            factors.append("source trace contains unsupported replay actions")
        # 定位器类顾虑（未验证动态 key / 时间戳泛化依据 / 易变 key）统一由映射函数给出，
        # 避免 Live 与 DC 两条路径各写一份而漂移。
        factors.extend(confidence_factors_from_warnings(warnings))
        return list(dict.fromkeys(factors))

    @staticmethod
    def _promotion_blockers(trace: RunTrace) -> list[str]:
        """Profile 晋级证据资格：与「能否执行」完全解耦的防御性标注。

        晋级实际使用 ``trace.profile_validation_generated`` 与
        ``provenance.generated_script_path``，任务脚本本就不参与晋级；这两个字段只供 API
        在误用时给出明确拒绝理由。
        """
        blockers: list[str] = []
        if trace.provisional:
            blockers.append("provisional trace is not Profile-promotion evidence")
        if trace.live_mode:
            blockers.append("live-mode trace is not Profile-promotion evidence")
        return blockers

    @staticmethod
    def _is_nondeterministic_system_click(trace: RunTrace, action: ActionResult) -> bool:
        target = str(action.params.get("target") or "")
        if "appicon" in target.lower():
            return True
        if not action.before_snapshot_id:
            return False
        snapshot = next((item for item in trace.snapshots if item.snapshot_id == action.before_snapshot_id), None)
        if snapshot is None:
            return False
        element = next((item for item in snapshot.elements if item.element_id == target), None)
        if element is None:
            return False
        launch_markers = " ".join(
            value or "" for value in (element.type, element.source, element.key, element.id, element.content)
        ).lower()
        return (
            "appicon" in launch_markers
            or "keyhidekbd" in launch_markers
            or (
                "launcher" in snapshot.page_path.lower()
                and trace.target_app_id.replace("-", "") in launch_markers.replace("-", "")
            )
        )

    @staticmethod
    def _runtime_element_coordinate(trace: RunTrace, action: ActionResult) -> tuple[int, int] | None:
        target = action.params.get("target")
        if not target or not action.before_snapshot_id:
            return None
        snapshot = next((item for item in trace.snapshots if item.snapshot_id == action.before_snapshot_id), None)
        if snapshot is None:
            return None
        element = next((item for item in snapshot.elements if item.element_id == target), None)
        return element.bbox.center if element and element.bbox else None

    @staticmethod
    def _snapshot_bound(trace: RunTrace, snapshot_id: str | None) -> tuple[int, int] | None:
        if not snapshot_id:
            return None
        snapshot = next((item for item in trace.snapshots if item.snapshot_id == snapshot_id), None)
        if snapshot is None:
            return None
        return (snapshot.width, snapshot.height)

    @staticmethod
    def _trace_bound(trace: RunTrace) -> tuple[int, int] | None:
        for snapshot in reversed(trace.snapshots):
            return (snapshot.width, snapshot.height)
        return None

    @staticmethod
    def _first_stable_locator(trace: RunTrace) -> LocatorSpec | None:
        """复刻 ``HypiumGenerator._first_stable_locator``，返回 IR 定位器。"""
        for snapshot in reversed(trace.snapshots):
            for element in snapshot.elements:
                if element.key and element.enabled and not re.search(r"_\d{8,}$", element.key):
                    return LocatorSpec(kind=LocatorKind.KEY, value=element.key, target_label=element.key)
                if element.content and element.enabled:
                    return LocatorSpec(kind=LocatorKind.TEXT, value=element.content, target_label=element.content)
        return None

    # ------------------------------------------------------------------
    # 组装
    # ------------------------------------------------------------------

    @staticmethod
    def _assemble(
        *,
        steps: list[TestStepSpec],
        profile: TargetAppProfile | None,
        trace: RunTrace | None,
        case_id: str | None,
        slug: str | None,
        scenario: ScenarioKind,
        bundle_name: str,
        main_ability: str,
        device_sn: str | None,
        startup_wait: float,
        source_kind: str,
        source_id: str,
    ) -> TestCaseSpec:
        for position, step in enumerate(steps, start=1):
            step.index = position
            step.title_zh = step.title_zh or _derive_step_title(step)
        hard = sum(1 for step in steps for checkpoint in step.checkpoints if not checkpoint.soft)
        title = _case_title(trace=trace, source_kind=source_kind, steps=steps)
        return TestCaseSpec(
            case_id=case_id or new_case_id(),
            slug=slug or slugify(title),
            title_zh=title,
            scenario=scenario,
            status="active" if hard else "draft",
            bundle_name=bundle_name,
            main_ability=main_ability,
            device_sn=device_sn,
            setup=SetupSpec(
                stop_app_first=True,
                start_app=True,
                startup_wait_seconds=startup_wait,
                listen_toast=any(
                    checkpoint.kind == CheckpointKind.TOAST for step in steps for checkpoint in step.checkpoints
                ),
            ),
            steps=steps,
            teardown=TeardownSpec(capture_final_screenshot=True, stop_app=False),
            provenance=CaseProvenance(
                source_kind=source_kind,  # type: ignore[arg-type]
                source_id=source_id,
                profile_target_app_id=profile.target_app_id if profile is not None else None,
            ),
        )


def _derive_step_title(step: TestStepSpec) -> str:
    from .titles import step_title_zh

    return step_title_zh(step)


def _case_title(*, trace: RunTrace | None, source_kind: str, steps: list[TestStepSpec]) -> str:
    if trace is not None and trace.task:
        return trace.task.strip()[:200] or "未命名用例"
    first = next((step for step in steps if step.action != StepAction.NOOP_COMMENT), None)
    if first is not None:
        return first.title_zh[:200] or "未命名用例"
    return f"未命名用例（{source_kind}）"


__all__ = [
    "FALLBACK_CHECKPOINT_MESSAGE",
    "LOW_CONFIDENCE_PREFIXES",
    "PLACEHOLDER_ABILITY",
    "PLACEHOLDER_BUNDLE",
    "SWIPE_DIRECTIONS",
    "SWIPE_DIRECTION_ORDER",
    "UNKNOWN_RESOLUTION_BOUND",
    "CaseBuildResult",
    "CaseBuilder",
    "evaluate_confidence",
    "evaluate_runnable",
    "format_args",
    "new_case_id",
    "slugify",
]
