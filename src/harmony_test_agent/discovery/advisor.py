"""LLM 视觉探索顾问：单次探索维持一段连续对话，逐页约束点击范围与次数。

顾问只对确定性候选列表做重排/过滤，风险分类在它之后照常执行，因此模型永远
无法放行被安全策略拦截的动作。任何一次调用失败都回退为启发式排序，探索绝不
因顾问而中断。
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, Field

from ..models import ExplorationPolicy, ScreenSnapshot


class AdvisorVerdict(BaseModel):
    """单页探索建议：recommended/avoid 为候选列表中的下标。"""

    page_summary: str = ""
    recommended: list[int] = Field(default_factory=list)
    avoid: list[int] = Field(default_factory=list)
    reason: str = ""


class AdvisorTurnResult(BaseModel):
    """一次顾问对话轮的结构化输出与累计消息历史。"""

    verdict: AdvisorVerdict
    history: list[Any] = Field(default_factory=list)


ADVISOR_PROMPT = """你是 OpenHarmony 自动探索顾问，帮助测试 agent 用尽量少的高价值交互摸清应用的主要页面结构。
每个回合你会看到当前页面截图、推荐上限和带编号的候选动作列表。请：
1. 用一句话概括当前页面（page_summary）。
2. 从候选中挑选不超过推荐上限、最能揭示应用结构的动作编号（recommended）：优先 Tab、底部/侧边导航、
   标题栏入口、菜单、"我的/设置/搜索"等结构性入口，其次输入框与可滚动列表；不要推荐进入具体内容的动作
   （信息流卡片、文章、视频、热搜条目等），也不要重复推荐历史摘要里已经走过的入口。
3. 标出明确不应点击的内容型候选编号（avoid）。
4. 用一句话说明理由（reason）。
recommended/avoid 必须是候选列表中的编号；候选全部是内容型时 recommended 返回空数组。"""


class ExplorationAdvisor:
    """跨页连续对话的探索顾问。

    整个探索 run 只有一段对话：首个逻辑页开启会话（system prompt + 截图 + 候选），
    之后每个新逻辑页把上一轮的结构化回答留在消息历史里继续追问，模型因此记得
    自己推荐过什么、页面之间如何衔接。历史超过 `advisor_history_turns` 轮时保留
    system 头并裁掉最旧的交换，被裁掉页面的摘要由 payload 的上下文摘要继续携带。
    """

    def __init__(self, provider: Any, policy: ExplorationPolicy) -> None:
        self.provider = provider
        self.max_actions = policy.advisor_max_actions
        self.history_turns = policy.advisor_history_turns
        self.turn_count = 0
        self._history: list[Any] = []

    def advise(
        self,
        snapshot: ScreenSnapshot,
        candidates: list[Any],
        context_note: str = "",
    ) -> tuple[AdvisorVerdict | None, str]:
        """同步入口（explorer 运行在工作线程）。返回 (verdict | None, source)。"""
        try:
            return asyncio.run(self._advise_async(snapshot, candidates, context_note))
        except Exception:
            return None, "heuristic-fallback"

    async def _advise_async(
        self,
        snapshot: ScreenSnapshot,
        candidates: list[Any],
        context_note: str,
    ) -> tuple[AdvisorVerdict | None, str]:
        payload = self._page_payload(snapshot, candidates, context_note)
        turn = await self.provider.advise_turn(list(self._history), snapshot.image_path.read_bytes(), payload)
        if turn is None:
            return None, "heuristic-fallback"
        self._history = self._prune(turn.history)
        self.turn_count += 1
        return self._validated(turn.verdict, len(candidates)), "model"

    def _prune(self, history: list[Any]) -> list[Any]:
        head = 1 if len(history) % 2 == 1 else 0
        body = history[head:]
        if len(body) > 2 * self.history_turns:
            body = body[-2 * self.history_turns :]
        return ([history[0]] if head else []) + body

    def _validated(self, verdict: AdvisorVerdict, count: int) -> AdvisorVerdict:
        recommended = [index for index in verdict.recommended if isinstance(index, int) and 0 <= index < count]
        avoid = [index for index in verdict.avoid if isinstance(index, int) and 0 <= index < count]
        return verdict.model_copy(update={"recommended": recommended[: self.max_actions], "avoid": avoid})

    def _page_payload(
        self,
        snapshot: ScreenSnapshot,
        candidates: list[Any],
        context_note: str,
    ) -> str:
        lines = []
        for index, action in enumerate(candidates):
            locator = action.locator_value if action.locator_kind in {"key", "id"} else action.target_text
            lines.append(f"{index}. [{action.kind}] {str(locator)[:60]} coordinate={action.coordinate}")
        digest = f"\n{context_note}\n" if context_note else ""
        return (
            f"当前页面 page_path={snapshot.page_path}，元素数={len(snapshot.elements)}。"
            f"本页最多推荐 {self.max_actions} 个动作。"
            f"{digest}\n候选动作（编号即 recommended/avoid 的索引）：\n" + "\n".join(lines)
        )
