"""DC 模式 LLM Provider：子类扩展 OpenAICompatibleProvider，不修改父类。

复用父类的 ``_model()`` 和 ``_model_settings()`` 构造视觉模型与兼容参数，
通过 pydantic-ai ``Agent`` + ``Tool`` 机制实现多轮工具对话。
"""

from __future__ import annotations

from typing import Any

from pydantic_ai import Agent, BinaryContent, UsageLimits

from ..agents.providers import MockAgentProvider, OpenAICompatibleProvider
from ..config import Settings
from .models import DcChatRequest, DcChatResponse

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
3. **无断言**：DC 模式不引入任何断言校验。不要尝试使用 assert_visible/assert_text 等工具。
4. **免 Profile**：不需要验证 Target App Profile。直接操作用户指定的应用。
5. **Shell 安全**：execute_shell 受安全策略约束，破坏性命令（reboot/rm -rf / /mkfs/dd/param set 等）\
会被拒绝。如果被拒绝，向用户报告并尝试替代方案。
6. **完成任务后**：返回结构化总结，说明完成了什么、遇到了什么问题、建议用户下一步做什么。\
然后等待用户的下一条消息。
7. **禁止编造**：不要在回复中编造工具结果。所有设备状态必须通过工具调用获取。

## 回复格式
- 调用工具时：简要说明你要做什么，然后调用工具。
- 任务完成时：以"任务完成"开头，总结执行的操作和结果。
- 遇到阻塞时：以"需要帮助"开头，说明遇到的具体问题和建议。
"""

DC_TURN_TEMPLATE = """\
当前 UI 控件树摘要：
{ui_tree}

用户消息：{prompt}
"""


# ---------------------------------------------------------------------------
# DcChatProvider — OpenAICompatibleProvider 子类
# ---------------------------------------------------------------------------


class DcChatProvider(OpenAICompatibleProvider):
    """DC 模式对话提供方：子类扩展，不修改父类。

    复用 ``_model(vision=True)`` 构造视觉模型，``_model_settings()`` 构造
    兼容参数（如 ``AGENT_DISABLE_THINKING``）。
    """

    async def chat(self, request: DcChatRequest) -> DcChatResponse:
        """执行一轮多步工具对话，并实时 emit 模型侧的思考/叙述事件。

        与 ``agent.run()`` 不同，这里用 ``agent.iter()`` 逐节点遍历：
        每次被动遍历推进一个节点后，从 ``run.all_messages()`` 差分出新产生的
        ``ModelResponse``，把其中的 ``thinking`` / ``text`` part 作为
        ``THINKING`` / ``AGENT_TEXT`` 事件实时发出。工具调用事件由
        ``DcActionRecorder`` 实时发出，两条流按时间序交错。

        pydantic-ai 的历史管理已保证 ``ThinkingPart`` 正确进出模型请求，
        这里只读不改（思考内容绝不回喂模型）。
        """
        agent: Agent[Any, str] = Agent(
            self._model(vision=True),
            system_prompt=DC_SYSTEM_PROMPT,
            tools=request.tools,
            retries=1,
        )

        # 构造用户 prompt：UI 树摘要 + 用户消息 + 截图
        user_prompt = DC_TURN_TEMPLATE.format(
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

        async with agent.iter(
            user_content,
            message_history=history or None,
            model_settings=self._model_settings(),
            deps=request.tool_context,
            usage_limits=UsageLimits(
                request_limit=30,
                tool_calls_limit=50,
            ),
        ) as run:
            async for _node in run:
                messages = run.all_messages()
                if len(messages) <= seen:
                    continue
                for message in messages[seen:]:
                    step = _emit_message_parts(message, step, emit)
                seen = len(messages)

        messages = run.all_messages()
        result = run.result
        return DcChatResponse(
            output_text=result.output if result else "",
            history=messages,
            tool_call_count=_count_tool_calls_from_messages(messages),
        )


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
        「思考/叙述/最终回复」三步渲染链路。
        """
        from .models import DcEventType

        emit = request.emit
        if emit:
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
        return DcChatResponse(
            output_text=(
                "任务完成（Mock）：已收到用户消息。"
                "当前为 Mock 模式，未实际调用工具或操作设备。"
                "请配置 OPENAI_API_KEY 和 AGENT_MODEL 以启用真实模型。"
            ),
            history=list(request.history or []),
            tool_call_count=0,
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


def _part_field(part: Any, name: str, default: Any = None) -> Any:
    """兼容 dict / dataclass 两种 part 表示读取字段。"""
    if isinstance(part, dict):
        return part.get(name, default)
    return getattr(part, name, default)


def _emit_message_parts(
    message: Any,
    step: int,
    emit: Any,
) -> int:
    """把一条 ModelMessage 中的 thinking/text part 作为事件 emit。

    Returns:
        更新后的 step 计数（每个非空 part 递增一次）。
    """
    from .models import DcEventType

    if not emit:
        return step
    parts = getattr(message, "parts", None) or []
    for part in parts:
        kind = _part_field(part, "part_kind")
        if kind not in ("thinking", "text"):
            continue
        content = _part_field(part, "content", "") or ""
        if not isinstance(content, str) or not content.strip():
            continue
        step += 1
        event_type = DcEventType.THINKING if kind == "thinking" else DcEventType.AGENT_TEXT
        emit(event_type.value, content[:200], {"step": step, "text": content})
    return step
