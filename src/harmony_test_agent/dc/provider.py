"""DC 模式 LLM Provider：子类扩展 OpenAICompatibleProvider，不修改父类。

复用父类的 ``_model()`` 和 ``_model_settings()`` 构造视觉模型与兼容参数，
通过 pydantic-ai ``Agent`` + ``Tool`` 机制实现多轮工具对话。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import timedelta
from typing import Any

from pydantic_ai import Agent, BinaryContent, ModelRequestNode, UsageLimits
from pydantic_ai.exceptions import UsageLimitExceeded

from ..agents.providers import MockAgentProvider, OpenAICompatibleProvider
from ..config import Settings
from ..models import utc_now
from .models import (
    DcChatRequest,
    DcChatResponse,
    DcEventType,
    DcModelTimeout,
    DcTokenUsage,
    DcTurnTimeout,
    DcUsageLimitReached,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 系统提示词
# ---------------------------------------------------------------------------

DC_SYSTEM_PROMPT = """\
你是 OpenHarmony 设备操作助手，运行在"直流模式（DC Mode）"下。

## 你的能力
- 你会看到当前设备截图（JPEG）和 UI 控件树摘要（top-K 可交互元素）。
- 你可以调用已注册的工具来操作设备：点击、滑动、输入文本、按键、截图、查看 UI 层级、\
管理应用、传输文件、执行受控 shell 命令等。
- 工具调用后你会收到结果字符串；截图工具会返回最新的 UI 元素摘要。

## 执行规则
1. **自主执行**：收到用户消息后，自主多步调用工具完成任务，直到任务完成或遇到无法逾越的阻塞。\
不要每步都询问用户下一步该做什么。
2. **每轮一个工具**：每次回复只调用一个工具，等待结果后再决定下一步。
3. **断言 checkpoint**：当用户任务包含可观测的完成条件（某文案出现/某元素可见）时，在达成后调用\
assert_text/assert_visible 留证（整个任务 1-2 次即可，不要每步都断言）；这些断言会进入回放脚本与\
Profile 蒸馏证据。中间步骤不需要断言。
4. **免 Profile**：不需要验证 Target App Profile。直接操作用户指定的应用。
5. **Shell 安全**：execute_shell 受安全策略约束，破坏性命令（reboot/rm -rf / /mkfs/dd/param set 等）\
会被拒绝。如果被拒绝，向用户报告并尝试替代方案。
6. **完成任务后**：返回结构化总结，说明完成了什么、遇到了什么问题、建议用户下一步做什么。\
然后等待用户的下一条消息。
7. **禁止编造**：不要在回复中编造工具结果。所有设备状态必须通过工具调用获取。
8. **先看清再动手**：只能在 UI 控件树摘要里真实出现过的控件上操作。如果目标控件不在摘要中\
（例如底部弹窗的表单元素被 top-K 截断），先降低不确定性：用 dump_ui_hierarchy 取完整层级，\
或先滚动/收起遮挡层让目标控件进入摘要；**不要连续对同一界面反复 screenshot，也不要盲点推测出来的坐标**。\
若多步观测后仍无法确认控件位置，以"需要帮助"停下来说明缺什么，而不是继续盲试烧掉预算。
9. **身份记录**：会话录制必须包含**目标应用**身份，否则生成的脚本与蒸馏的 Profile 无法使用。执行：\
(a) **目标应用进入前台之后**再调用 foreground_app 记录当前前台 bundle/ability——不要在还没打开目标\
应用时就记录，那一刻前台通常是桌面，记下来的桌面身份没有用处；若返回的是桌面（例如 \
com.ohos.sceneboard）或 ability=unknown，先启动/切到目标应用，然后再记录一次；\
(b) 打开或重启目标应用时用 start_app 并显式传 bundle_name 与 ability_name\
（bundle 未知时先用 list_apps 或 inspect_app 查询；ability 未知时可用 MainAbility，\
但只有 start_app 成功的记录才算数）；禁止猜测占位值（com.example.app / EntryAbility）；\
(c) 若用户消息已给出应用名/bundle，直接用它；整个会话至少要有一条**成功**的身份记录：\
bundle 非桌面、ability 非 unknown 的 foreground_app，或带显式 bundle_name/ability_name 且成功的 start_app。
10. **留意应用异常（这是测试的核心产出）**：出现以下任一现象时，先用 foreground_app 与 collect_logs 取证，\
然后在回复中明确报告「疑似缺陷」并描述现象，**不要静默绕过**：点击后页面毫无反应、页面空白/全白、\
应用突然退回桌面、界面元素重叠或越界、长时间无响应。collect_logs 的返回值里若出现 `SUSPECTED CRASH`，\
说明刚刚采集的日志里命中了崩溃/冻屏模式，必须把它写进你的结论。这类发现比"任务是否跑通"更重要。

## 回复格式
- 调用工具时：简要说明你要做什么，然后调用工具。
- 任务完成时：以"任务完成"开头，总结执行的操作和结果。
- 遇到阻塞时：以"需要帮助"开头，说明遇到的具体问题和建议。
"""

DC_TURN_TEMPLATE = """\
{continuation}
当前 UI 控件树摘要：
{ui_tree}

用户消息：{prompt}
"""

# 视为「工具调用」的 part_kind（含 pydantic-ai 内建工具）
_TOOL_CALL_PART_KINDS = frozenset({"tool-call", "builtin-tool-call"})

# 预算耗尽时的用户可读文案（前端 ERROR 事件与 turn.error 共用）
_USAGE_LIMIT_MESSAGE = "本轮模型请求预算已用尽（request_limit={request_limit}，tool_calls={tool_calls_limit}）"


def _usage_limits(settings: Settings) -> UsageLimits:
    """按配置构造 pydantic-ai 用量上限。

    两个上限都必须可配置：此前 ``request_limit=30`` 被硬编码，而 DC 每轮只调一个工具，
    30 次请求 ≈ 30 次工具往返，真实多步任务（例如在日历底部弹窗里填表单）会先把预算
    烧在观测上，第 31 次请求直接抛 ``UsageLimitExceeded``。
    """
    return UsageLimits(
        request_limit=settings.dc_model_request_limit,
        tool_calls_limit=settings.dc_model_tool_calls_limit,
    )


def _usage_limit_exceeded(
    exc: UsageLimitExceeded,
    settings: Settings,
    *,
    turn_id: str,
    attempt: int,
) -> DcUsageLimitReached:
    """把 pydantic-ai 的裸用量异常转成携带上限值的 DC 领域异常。"""
    message = _USAGE_LIMIT_MESSAGE.format(
        request_limit=settings.dc_model_request_limit,
        tool_calls_limit=settings.dc_model_tool_calls_limit,
    )
    return DcUsageLimitReached(
        f"[turn={turn_id}] {message}；在第 {attempt} 次模型请求前被拒绝；命中原因：{exc}",
        request_limit=settings.dc_model_request_limit,
        tool_calls_limit=settings.dc_model_tool_calls_limit,
    )


# ---------------------------------------------------------------------------
# DcChatProvider — OpenAICompatibleProvider 子类
# ---------------------------------------------------------------------------


class DcChatProvider(OpenAICompatibleProvider):
    """DC 模式对话提供方：子类扩展，不修改父类。

    复用 ``_model(vision=True)`` 构造视觉模型，``_model_settings()`` 构造
    兼容参数（如 ``AGENT_DISABLE_THINKING``）。
    """

    def __init__(self, settings: Settings):
        super().__init__(settings)
        # 用量是会话级状态：DcChatProvider 每个会话一个实例（见 DcSessionManager），
        # 而 pydantic-ai 的 run 会带上历史消息，故 RunUsage 累计值跨轮次单调递增，
        # 相邻两轮的差值就是本轮真实消耗。
        self._last_run_usage = DcTokenUsage()
        self._last_usage_delta = DcTokenUsage()
        # 本轮已发生的用量：超时/取消/预算耗尽时用它兜底入账
        self._progress_usage = DcTokenUsage()
        # 增量归属的轮次：会话层据此避免把上一轮的增量重复入账
        self._usage_delta_turn_id: str | None = None

    @property
    def last_usage(self) -> DcTokenUsage:
        """最近一次 run 的累计用量（会话内累计）。"""
        return self._last_run_usage

    @property
    def last_usage_delta(self) -> DcTokenUsage:
        """相对上一次 run 的增量。"""
        return self._last_usage_delta

    @property
    def usage_delta_turn_id(self) -> str | None:
        """``last_usage_delta`` 归属的轮次；None 表示尚未产生任何增量。"""
        return self._usage_delta_turn_id

    async def chat(self, request: DcChatRequest) -> DcChatResponse:
        """执行一轮多步工具对话，并实时 emit 模型侧的思考/叙述事件。

        与 ``agent.run()`` 不同，这里用 ``agent.iter()`` 逐节点遍历：
        每次被动遍历推进一个节点后，从 ``run.all_messages()`` 差分出新产生的
        ``ModelResponse``，把其中的 ``thinking`` / ``text`` part 作为
        ``THINKING`` / ``AGENT_TEXT`` 事件实时发出。工具调用事件由
        ``DcActionRecorder`` 实时发出，两条流按时间序交错。
        **终态回答的文本不发 ``AGENT_TEXT``**（避免与 ``ASSISTANT_MESSAGE`` 重复），
        详见 ``_emit_message_parts``。

        Phase 2 关键变化：**每一次迭代推进都包在剩余 deadline 内**，并发出
        ``model_call_started`` / ``model_call_progress`` / ``model_call_finished``
        / ``model_call_failed``。没有 token streaming 也不再等于「没有任何进度」：
        等待期间按 ``progress_interval`` 发心跳，超时后抛出 ``DcModelTimeout``
        或 ``DcTurnTimeout``，绝不无限等待。

        Phase 3 关键变化：``dc_token_streaming`` 开启时，遇到 ``ModelRequestNode``
        就地用 ``node.stream(ctx)`` 消费模型流，按增量发 ``MESSAGE_DELTA``
        （前端逐字显示草稿），随后仍发全量 ``THINKING`` / ``AGENT_TEXT`` 供收口；
        模型不支持流式时记录 warning 并回退整块推进，行为与关闭开关一致。

        pydantic-ai 的历史管理已保证 ``ThinkingPart`` 正确进出模型请求，
        这里只读不改（思考内容绝不回喂模型）。
        """
        agent: Agent[Any, str] = Agent(
            self._model(vision=True),
            system_prompt=DC_SYSTEM_PROMPT,
            tools=request.tools,
            retries=1,
        )

        # 构造用户 prompt：公开连续性摘要 + UI 树摘要 + 用户消息 + 截图
        user_prompt = DC_TURN_TEMPLATE.format(
            continuation=_render_continuation(request.continuation_prompt),
            ui_tree=request.ui_tree_digest or "(no UI tree available)",
            prompt=request.user_prompt,
        )
        user_content: list[Any] = [user_prompt]
        if request.screenshot_jpeg:
            user_content.append(BinaryContent(data=request.screenshot_jpeg, media_type="image/jpeg"))

        emit = request.emit
        history = list(request.history or [])
        # all_messages() 为「历史 + 本次新增」，故以历史长度为差分起点，
        # 避免把上一轮的 text/thinking 重复 emit。
        seen = len(history)
        step = 0
        # retries/重放可能产生完全相同的 part，按 (kind, text) 去重
        emitted: set[tuple[str, str]] = set()

        attempt = 0
        # 上一次推进是否只是「刚被流式消费过的节点」的缓存命中：
        # 命中时不再重复发 model_call_started/progress/finished（真实模型调用已经发过）
        streamed_advance = False
        try:
            async with agent.iter(
                user_content,
                message_history=history or None,
                model_settings=self._model_settings(),
                deps=request.tool_context,
                usage_limits=_usage_limits(self.settings),
            ) as run:
                iterator = run.__aiter__()
                while True:
                    attempt += 1
                    timeout = _advance_timeout(request)
                    if timeout <= 0:
                        raise DcTurnTimeout(
                            f"DC turn budget exhausted before model call {attempt} (dc_turn_timeout reached)"
                        )
                    node = await _advance_with_progress(
                        iterator, request, attempt, timeout, emit, emit_progress=not streamed_advance
                    )
                    streamed_advance = False
                    # 每个节点后刷新一次「已发生用量」，使超时/取消路径也有数字可入账
                    self._progress_usage = DcTokenUsage.from_run_usage(run.usage())
                    if node is _EXHAUSTED:
                        break
                    messages = run.all_messages()
                    if len(messages) > seen:
                        for index, message in enumerate(messages[seen:], start=seen):
                            step = _emit_message_parts(
                                message, step, emit, emitted, stream_key_base=f"{request.turn_id}:m{index}"
                            )
                        seen = len(messages)
                    # token 级流式：就地消费本次模型请求节点。节点被 stream 后
                    # 下一次 __anext__ 走 node.run() 的缓存 _result，不会重复请求模型。
                    if isinstance(node, ModelRequestNode) and self.settings.dc_token_streaming:
                        # 流式消费发生在节点执行之前：本次的 ModelRequest 还没写进
                        # all_messages()，故本次响应消息的下标 = 当前条数 + 1（请求占一位），
                        # 与随后差分 emit 全量事件时用的下标一致（DC 不走无用户消息恢复路径）。
                        streamed_advance = await _stream_model_node(
                            node, run, request, attempt, emit, message_index=seen + 1
                        )
                        messages = run.all_messages()
                        for index, message in enumerate(messages[seen:], start=seen):
                            step = _emit_message_parts(
                                message, step, emit, emitted, stream_key_base=f"{request.turn_id}:m{index}"
                            )
                        seen = len(messages)
                messages = run.all_messages()
                result = run.result
                # 必须在 iter 上下文内读取：块外 run 的累计用量不再可用
                cumulative = DcTokenUsage.from_run_usage(run.usage())
        except UsageLimitExceeded as exc:
            # pydantic-ai 在进入 ModelRequestNode 前就拒绝请求，此时
            # ``_advance_with_progress`` 尚未发出 started/finished，必须在这里补一条
            # MODEL_CALL_FAILED，否则前端「当前活动区」看不到任何失败迹象。
            # 预算耗尽前已经发生的请求同样要计入用量，否则最需要看数字的场景反而没有数据。
            cumulative = self._progress_usage
            self._record_usage(cumulative, request.turn_id)
            _emit_model_event(
                emit,
                DcEventType.MODEL_CALL_FAILED,
                "模型请求预算已用尽",
                {
                    "turn_id": request.turn_id,
                    "attempt": attempt,
                    "phase": "waiting_model",
                    "error_code": "usage_limit",
                    "request_limit": self.settings.dc_model_request_limit,
                    "tool_calls_limit": self.settings.dc_model_tool_calls_limit,
                    "error": str(exc),
                },
            )
            raise _usage_limit_exceeded(exc, self.settings, turn_id=request.turn_id, attempt=attempt) from exc
        except DcModelTimeout, DcTurnTimeout, asyncio.CancelledError:
            # 超时/取消同样保留已发生的用量，避免统计面板在一轮失败后归零
            cumulative = self._progress_usage
            self._record_usage(cumulative, request.turn_id)
            raise

        self._record_usage(cumulative, request.turn_id)
        return DcChatResponse(
            output_text=result.output if result else "",
            history=messages,
            tool_call_count=_count_tool_calls_from_messages(messages),
            usage=self._last_run_usage,
            usage_delta=self._last_usage_delta,
        )

    def _record_usage(self, cumulative: DcTokenUsage, turn_id: str) -> None:
        """把一次 run 的累计用量换算成会话累计与「本轮增量」。

        pydantic-ai 的 ``RunUsage`` 在带历史消息时是**跨轮次累计**的，因此
        本轮真实消耗 = 本次累计 − 上一次累计。同一轮内多次调用本方法是幂等的
        （增量由差值定义，不会被重复累加）。
        """
        self._last_usage_delta = cumulative.minus(self._last_run_usage)
        self._last_run_usage = cumulative
        self._progress_usage = cumulative
        self._usage_delta_turn_id = turn_id


# ---------------------------------------------------------------------------
# MockDcChatProvider — 离线测试用
# ---------------------------------------------------------------------------


class MockDcChatProvider(MockAgentProvider):
    """DC 模式 Mock 提供方：返回固定响应，不调用工具。

    继承 ``MockAgentProvider`` 以获得 ``plan``/``analyze``/``decide`` 的
    确定性实现，并覆盖 ``chat`` 返回 DC 专用 Mock 响应。
    """

    async def chat(self, request: DcChatRequest) -> DcChatResponse:
        """返回固定 Mock 响应；history 原样回传以便测试累积。

        同时 emit 一条 THINKING + AGENT_TEXT，让无模型配置时前端也能验证
        「思考/叙述/最终回复」三步渲染链路；并发出 model_call_started/finished，
        使 Mock 模式也具备「当前活动区」进度事件。
        """
        from .models import DcEventType

        emit = request.emit
        if emit:
            model_call_id = f"model-mock-{uuid.uuid4().hex[:6]}"
            emit(
                DcEventType.MODEL_CALL_STARTED.value,
                "开始等待模型响应（Mock）",
                {
                    "model_call_id": model_call_id,
                    "turn_id": request.turn_id,
                    "attempt": 1,
                    "phase": "waiting_model",
                    "deadline_at": (utc_now() + timedelta(seconds=request.model_timeout)).isoformat(),
                    "remaining_budget_ms": round(request.model_timeout * 1000),
                },
            )
            emit(
                DcEventType.THINKING.value,
                "（Mock）分析用户意图与当前屏幕…",
                {"step": 1, "text": "（Mock 模式）未连接真实模型，跳过实际推理与工具调用。"},
            )
            emit(
                DcEventType.AGENT_TEXT.value,
                "（Mock）已收到消息",
                {"step": 2, "text": "当前为 Mock 模式，配置 OPENAI_API_KEY 与 AGENT_MODEL 后启用真实执行。"},
            )
            emit(
                DcEventType.MODEL_CALL_FINISHED.value,
                "模型调用完成（Mock）",
                {"model_call_id": model_call_id, "turn_id": request.turn_id, "attempt": 1, "duration_ms": 0},
            )
        return DcChatResponse(
            output_text=(
                "任务完成（Mock）：已收到用户消息。"
                "当前为 Mock 模式，未实际调用工具或操作设备。"
                "请配置 OPENAI_API_KEY 和 AGENT_MODEL 以启用真实模型。"
            ),
            history=list(request.history or []),
            tool_call_count=0,
            # Mock 不调用真实模型：显式返回空用量，前端据此显示「尚无用量数据」
            usage=DcTokenUsage(),
            usage_delta=DcTokenUsage(),
        )


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def create_dc_provider(settings: Settings) -> DcChatProvider | MockDcChatProvider:
    """根据配置选择 DC 提供方。

    - ``agent_provider == "mock"`` 或模型未配置 → ``MockDcChatProvider``
    - 否则 → ``DcChatProvider``
    """
    if settings.agent_provider == "mock" or not settings.model_configured:
        return MockDcChatProvider()
    return DcChatProvider(settings)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _count_tool_calls(result: Any) -> int:
    """从 pydantic-ai AgentRunResult 中统计工具调用次数。"""
    return _count_tool_calls_from_messages(result.all_messages())


def _count_tool_calls_from_messages(messages: list[Any]) -> int:
    """从 ModelMessage 列表中统计工具调用次数。"""
    count = 0
    for message in messages:
        # ModelRequest 中的 ToolReturnPart 表示一次工具调用完成
        parts = getattr(message, "parts", None)
        if parts is None:
            continue
        for part in parts:
            part_type = type(part).__name__
            if part_type in ("ToolReturnPart", "ToolCallPart"):
                count += 1
    # 每个工具调用产生一对 (ToolCallPart + ToolReturnPart)，除以 2
    return count // 2 if count >= 2 else count


# ---------------------------------------------------------------------------
# 模型调用进度与 deadline（Phase 2）
# ---------------------------------------------------------------------------

# ``agent.iter()`` 迭代结束的哨兵值
_EXHAUSTED = object()


def _render_continuation(text: str) -> str:
    """把公开连续性摘要渲染成 prompt 前缀（空摘要返回空串）。"""
    stripped = (text or "").strip()
    return f"{stripped}\n" if stripped else ""


def _advance_timeout(request: DcChatRequest) -> float:
    """单次模型调用可用秒数 = min(AGENT_MODEL_TIMEOUT, 轮次剩余预算)。"""
    remaining = float(request.model_timeout)
    if request.turn_deadline is not None:
        remaining = min(remaining, (request.turn_deadline - utc_now()).total_seconds())
    return remaining


def _emit_model_event(emit: Any, event_type: DcEventType, message: str, payload: dict[str, Any]) -> None:
    """发射模型侧事件（emit 为空时跳过，便于单测直接调用 chat()）。"""
    if emit:
        emit(event_type.value, message, payload)


async def _advance_with_progress(
    iterator: Any,
    request: DcChatRequest,
    attempt: int,
    timeout: float,
    emit: Any,
    *,
    emit_progress: bool = True,
) -> Any:
    """推进一次 ``agent.iter()``，全程发 started/progress，超时发 failed 并抛出。

    ``__anext__()`` 内部包含真实的模型 HTTP 请求，因此把 deadline 放在这一层
    才能约束「单次模型调用」，而不是只包住整个轮次。

    ``emit_progress=False`` 用于「刚被 ``_stream_model_node`` 流式消费过的节点」：
    这次推进只是缓存命中，真实模型调用的 started/finished 已由流式路径发出，
    再发一对会让前端看到重复的「开始等待模型响应」。超时/取消的失败事件照常发。
    """
    model_call_id = f"model-{uuid.uuid4().hex[:8]}"
    started_mono = time.monotonic()
    deadline_at = utc_now() + timedelta(seconds=timeout)
    base_payload: dict[str, Any] = {
        "model_call_id": model_call_id,
        "turn_id": request.turn_id,
        "attempt": attempt,
        "phase": "waiting_model",
    }
    if emit_progress:
        _emit_model_event(
            emit,
            DcEventType.MODEL_CALL_STARTED,
            f"开始等待模型响应（第 {attempt} 次）",
            {
                **base_payload,
                "started_at": utc_now().isoformat(),
                "deadline_at": deadline_at.isoformat(),
                "remaining_budget_ms": round(timeout * 1000),
            },
        )

    async def heartbeat() -> None:
        interval = max(0.2, float(request.progress_interval))
        while True:
            await asyncio.sleep(interval)
            elapsed = time.monotonic() - started_mono
            _emit_model_event(
                emit,
                DcEventType.MODEL_CALL_PROGRESS,
                "正在等待模型响应",
                {
                    **base_payload,
                    "elapsed_ms": round(elapsed * 1000),
                    "remaining_ms": round(max(0.0, timeout - elapsed) * 1000),
                    "last_progress_at": utc_now().isoformat(),
                },
            )

    task = asyncio.create_task(heartbeat(), name=f"dc-model-progress-{model_call_id}") if emit_progress else None
    # 用 shield + 独立推进任务：deadline 由我们判定，不被 pydantic-ai 内部的
    # CancelledError 改写影响；同时保证超时后底层推进任务被真正取消。
    advance_task = asyncio.ensure_future(iterator.__anext__())
    advance_task.add_done_callback(_consume_task_result)
    try:
        node = await asyncio.wait_for(asyncio.shield(advance_task), timeout=timeout)
    except StopAsyncIteration:
        return _EXHAUSTED
    except (TimeoutError, asyncio.CancelledError) as exc:
        current = asyncio.current_task()
        deadline_hit = isinstance(exc, TimeoutError) or not (current is not None and current.cancelling())
        duration_ms = round((time.monotonic() - started_mono) * 1000)
        if deadline_hit:
            _emit_model_event(
                emit,
                DcEventType.MODEL_CALL_FAILED,
                "模型调用超时",
                {
                    **base_payload,
                    "error_code": "model_timeout",
                    "error": f"model call exceeded {timeout:.1f}s",
                    "duration_ms": duration_ms,
                },
            )
            raise DcModelTimeout(f"model call exceeded {timeout:.1f}s") from exc
        _emit_model_event(
            emit,
            DcEventType.MODEL_CALL_FAILED,
            "模型调用已取消",
            {
                **base_payload,
                "error_code": "cancelled",
                "error": "model call cancelled",
                "duration_ms": duration_ms,
            },
        )
        raise
    finally:
        if task is not None:
            task.cancel()
        if not advance_task.done():
            advance_task.cancel()

    if not emit_progress:
        return node

    _emit_model_event(
        emit,
        DcEventType.MODEL_CALL_FINISHED,
        "模型调用完成",
        {**base_payload, "duration_ms": round((time.monotonic() - started_mono) * 1000)},
    )
    # 收到节点后进入解析/校验阶段：即使 Provider 不支持 token streaming，
    # 前端也能看到「正在校验结果」而不是直接跳到下一步。
    _emit_model_event(
        emit,
        DcEventType.MODEL_CALL_PROGRESS,
        "正在校验模型返回结果",
        {
            **base_payload,
            "phase": "validating",
            "elapsed_ms": round((time.monotonic() - started_mono) * 1000),
            "remaining_ms": round(max(0.0, timeout - (time.monotonic() - started_mono)) * 1000),
            "last_progress_at": utc_now().isoformat(),
        },
    )
    return node


def _consume_task_result(task: asyncio.Future[Any]) -> None:
    """消费被放弃的推进任务异常，避免 'exception was never retrieved' 噪音。"""
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError, Exception:  # noqa: BLE001 - 仅为回收异常
        pass


# ---------------------------------------------------------------------------
# token 级流式（Phase 3）
# ---------------------------------------------------------------------------

# 分块合并窗口：窗口内的增量合成一次 emit，避免每个 token 一条 SSE。
_DELTA_DEBOUNCE = 0.08

# 可流式的文本 part：part_kind → stream_key 后缀（思考与文本各一条独立草稿）。
_STREAM_PART_KINDS: tuple[tuple[str, str], ...] = (("thinking", "thinking"), ("text", "text"))


def _text_of(response: Any, kind: str) -> str:
    """把一条（可能仍在增长的）模型响应里某种 part 的文本拼起来。"""
    parts = getattr(response, "parts", None) or []
    chunks: list[str] = []
    for part in parts:
        if str(_part_field(part, "part_kind") or "") != kind:
            continue
        content = _part_field(part, "content", "")
        if isinstance(content, str):
            chunks.append(content)
    return "".join(chunks)


def _has_tool_call(response: Any) -> bool:
    """当前响应快照是否已包含工具调用（决定文本是「叙述」还是「终态回答」）。"""
    parts = getattr(response, "parts", None) or []
    return any(str(_part_field(part, "part_kind") or "") in _TOOL_CALL_PART_KINDS for part in parts)


def _suffix_delta(previous: str, current: str) -> str:
    """取 ``current`` 相对已发送内容的新增后缀。

    流式响应正常只追加内容；若出现与已发前缀不一致的重写（重试、role 改判），
    把整段当增量发出——宁可多显示一次，也不要静默吞掉用户可见的内容。
    """
    if current == previous:
        return ""
    if current.startswith(previous):
        return current[len(previous) :]
    return current


async def _stream_model_node(
    node: Any,
    run: Any,
    request: DcChatRequest,
    attempt: int,
    emit: Any,
    message_index: int,
) -> bool:
    """就地流式消费一个 ``ModelRequestNode``，按增量发 ``MESSAGE_DELTA``。

    可行性的依据（pydantic-ai 1.73 / pydantic_graph beta）：``agent.iter()`` 返回的是
    **尚未执行**的节点；``node.stream(ctx)`` 内部完成 ``_prepare_request`` +
    ``_finish_handling`` 并写入 ``node._result``，于是下一次 ``__anext__`` 走
    ``ModelRequestNode.run()`` 的缓存分支（``_agent_graph.py`` L485-491），
    不会重复请求模型，节点语义与整块推进完全一致。

    Returns:
        ``True``：本次模型调用已由本函数消费（节点已定稿，后续推进是缓存命中）；
        ``False``：流式入口就失败（例如模型不支持流式），已放弃流式，交回主循环整块推进。
    """
    model_call_id = f"model-{uuid.uuid4().hex[:8]}"
    started_mono = time.monotonic()
    timeout = _advance_timeout(request)
    deadline_at = utc_now() + timedelta(seconds=timeout)
    stream_key_base = f"{request.turn_id}:m{message_index}"
    base_payload: dict[str, Any] = {
        "model_call_id": model_call_id,
        "turn_id": request.turn_id,
        "attempt": attempt,
        "phase": "waiting_model",
    }
    _emit_model_event(
        emit,
        DcEventType.MODEL_CALL_STARTED,
        f"开始等待模型响应（第 {attempt} 次）",
        {
            **base_payload,
            "started_at": utc_now().isoformat(),
            "deadline_at": deadline_at.isoformat(),
            "remaining_budget_ms": round(timeout * 1000),
            "streaming": True,
        },
    )

    accumulated: dict[str, str] = {kind: "" for kind, _ in _STREAM_PART_KINDS}

    async def consume() -> None:
        async with node.stream(run.ctx) as agent_stream:
            async for response in agent_stream.stream_responses(debounce_by=_DELTA_DEBOUNCE):
                has_tool_call = _has_tool_call(response)
                for kind, suffix in _STREAM_PART_KINDS:
                    current = _text_of(response, kind)
                    delta = _suffix_delta(accumulated[kind], current)
                    if not delta:
                        continue
                    accumulated[kind] = current
                    role = "thinking" if kind == "thinking" else ("narration" if has_tool_call else "assistant")
                    _emit_model_event(
                        emit,
                        DcEventType.MESSAGE_DELTA,
                        delta[:200],
                        {
                            **base_payload,
                            "stream_key": f"{stream_key_base}:{suffix}",
                            "role": role,
                            "delta": delta,
                            "message_index": message_index,
                        },
                    )

    async def heartbeat() -> None:
        """流式等待期间的心跳：模型静默期也要让「最近进展」与剩余预算动起来。"""
        interval = max(0.2, float(request.progress_interval))
        while True:
            await asyncio.sleep(interval)
            elapsed = time.monotonic() - started_mono
            _emit_model_event(
                emit,
                DcEventType.MODEL_CALL_PROGRESS,
                "正在接收模型流式输出",
                {
                    **base_payload,
                    "elapsed_ms": round(elapsed * 1000),
                    "remaining_ms": round(max(0.0, timeout - elapsed) * 1000),
                    "last_progress_at": utc_now().isoformat(),
                    "streaming": True,
                },
            )

    consume_task = asyncio.ensure_future(consume())
    consume_task.add_done_callback(_consume_task_result)
    heartbeat_task = asyncio.create_task(heartbeat(), name=f"dc-model-stream-progress-{model_call_id}")
    try:
        await asyncio.wait_for(asyncio.shield(consume_task), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError) as exc:
        current = asyncio.current_task()
        deadline_hit = isinstance(exc, TimeoutError) or not (current is not None and current.cancelling())
        duration_ms = round((time.monotonic() - started_mono) * 1000)
        error_code = "model_timeout" if deadline_hit else "cancelled"
        _emit_model_event(
            emit,
            DcEventType.MODEL_CALL_FAILED,
            "模型调用超时" if deadline_hit else "模型调用已取消",
            {
                **base_payload,
                "error_code": error_code,
                "error": f"model call exceeded {timeout:.1f}s" if deadline_hit else "model call cancelled",
                "duration_ms": duration_ms,
            },
        )
        if deadline_hit:
            raise DcModelTimeout(f"model call exceeded {timeout:.1f}s") from exc
        raise
    except Exception as exc:
        # 节点已被 stream 标记（_did_stream=True）时不能再回退整块推进，
        # 只能按模型失败处理；入口就失败的情况见下面的 _did_stream 判定。
        if not getattr(node, "_did_stream", False):
            logger.warning("DC token streaming unavailable, falling back to block output: %s", exc)
            _emit_model_event(
                emit,
                DcEventType.MODEL_CALL_PROGRESS,
                "模型不支持流式输出，回退整块返回",
                {**base_payload, "phase": "streaming_unavailable"},
            )
            return False
        _emit_model_event(
            emit,
            DcEventType.MODEL_CALL_FAILED,
            "模型流式输出失败",
            {
                **base_payload,
                "error_code": "model_stream_error",
                "error": str(exc),
                "duration_ms": round((time.monotonic() - started_mono) * 1000),
            },
        )
        raise
    finally:
        heartbeat_task.cancel()
        if not consume_task.done():
            consume_task.cancel()

    _emit_model_event(
        emit,
        DcEventType.MODEL_CALL_FINISHED,
        "模型调用完成",
        {**base_payload, "duration_ms": round((time.monotonic() - started_mono) * 1000)},
    )
    return True


def _part_field(part: Any, name: str, default: Any = None) -> Any:
    """兼容 dict / dataclass 两种 part 表示读取字段。"""
    if isinstance(part, dict):
        return part.get(name, default)
    return getattr(part, name, default)


def _emit_message_parts(
    message: Any,
    step: int,
    emit: Any,
    emitted: set[tuple[str, str]] | None = None,
    stream_key_base: str | None = None,
) -> int:
    """把一条 ModelMessage 中的 thinking/text part 作为事件 emit。

    关键规则：**只有伴随工具调用的文本才是「中途叙述」**（``AGENT_TEXT``）。
    不含工具调用的文本属于本轮终态回答，会由 ``handle_user_message`` 以
    ``ASSISTANT_MESSAGE`` 呈现；若在此处也 emit，前端会同时渲染出叙述气泡与
    助手气泡，出现两条内容相同的「任务完成」。

    ``stream_key_base`` 非空时，payload 附带与 ``MESSAGE_DELTA`` 草稿一致的
    ``stream_key``：前端据此把逐字草稿就地收口为全量文本（id 不变）。

    Returns:
        更新后的 step 计数（每个实际发出的 part 递增一次）。
    """
    from .models import DcEventType

    if not emit:
        return step
    parts = getattr(message, "parts", None) or []
    has_tool_call = any(_part_field(part, "part_kind") in _TOOL_CALL_PART_KINDS for part in parts)
    for part in parts:
        kind = _part_field(part, "part_kind")
        if kind not in ("thinking", "text"):
            continue
        if kind == "text" and not has_tool_call:
            continue
        content = _part_field(part, "content", "") or ""
        if not isinstance(content, str) or not content.strip():
            continue
        if emitted is not None:
            key = (str(kind), content.strip())
            if key in emitted:
                continue
            emitted.add(key)
        step += 1
        event_type = DcEventType.THINKING if kind == "thinking" else DcEventType.AGENT_TEXT
        payload: dict[str, Any] = {"step": step, "text": content}
        if stream_key_base:
            payload["stream_key"] = f"{stream_key_base}:{kind}"
        emit(event_type.value, content[:200], payload)
    return step
