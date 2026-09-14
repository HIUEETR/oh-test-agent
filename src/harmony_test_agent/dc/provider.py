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
        """执行一轮多步工具对话。

        pydantic-ai Agent 会自动循环调用 tools 直到模型给出终态回答或超出
        ``UsageLimits``。每轮构造新 Agent 实例（匹配 ``providers.py`` 风格），
        但 ``message_history`` 跨轮累积。
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

        result = await agent.run(
            user_content,
            message_history=request.history or None,
            model_settings=self._model_settings(),
            deps=request.tool_context,
            usage_limits=UsageLimits(
                request_limit=30,
                tool_calls_limit=50,
            ),
        )

        # 统计工具调用次数
        tool_call_count = _count_tool_calls(result)

        return DcChatResponse(
            output_text=result.output,
            history=result.all_messages(),
            tool_call_count=tool_call_count,
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
        """返回固定 Mock 响应；history 原样回传以便测试累积。"""
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
    count = 0
    for message in result.all_messages():
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
