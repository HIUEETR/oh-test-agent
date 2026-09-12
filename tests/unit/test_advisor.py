"""探索顾问（连续会话）的单元测试：历史延续、裁剪、校验与应用兜底。"""

from __future__ import annotations

from pathlib import Path

from harmony_test_agent.discovery import ExplorationAction, ExplorationPolicy
from harmony_test_agent.discovery.advisor import ADVISOR_PROMPT, AdvisorTurnResult, AdvisorVerdict, ExplorationAdvisor
from harmony_test_agent.models import ResolvedTarget, ScreenSnapshot


def _target() -> ResolvedTarget:
    return ResolvedTarget(
        target_app_id="demo",
        display_name="Demo",
        bundle_name="com.example.demo",
        main_ability="EntryAbility",
        device_id="fake-device",
        source="installed_app",
    )


def _snapshot(tmp_path: Path, page_path: str = "/page/0") -> ScreenSnapshot:
    image_path = tmp_path / "screen.png"
    if not image_path.exists():
        image_path.write_bytes(b"\x89PNG-fake-image-bytes")
    return ScreenSnapshot(
        snapshot_id=f"snap-{page_path}",
        run_id="run-advisor",
        image_path=image_path,
        image_sha256="hash",
        width=100,
        height=200,
        page_path=page_path,
        elements=[],
    )


def _action(action_id: str, kind: str = "click") -> ExplorationAction:
    return ExplorationAction(action_id=action_id, kind=kind, locator_kind="key", locator_value=action_id)


class RecordingProvider:
    """记录每轮收到的 history，并返回自增编号的建议。"""

    def __init__(self, *, fail: bool = False, return_none: bool = False) -> None:
        self.fail = fail
        self.return_none = return_none
        self.received_histories: list[list[object]] = []
        self.calls = 0

    async def advise_turn(self, history: list[object], screenshot: bytes, payload: str) -> AdvisorTurnResult | None:
        del screenshot
        if self.fail:
            raise RuntimeError("advisor backend down")
        if self.return_none:
            return None
        self.calls += 1
        self.received_histories.append(list(history))
        verdict = AdvisorVerdict(page_summary=f"page {self.calls}", recommended=[0], avoid=[], reason="ok")
        base = list(history) or ["system-prompt"]  # 首轮携带 system 头，模拟 pydantic-ai 消息布局
        return AdvisorTurnResult(
            verdict=verdict,
            history=[*base, f"request-{self.calls}", f"response-{self.calls}"],
        )


def test_advisor_keeps_single_conversation_across_pages(tmp_path: Path) -> None:
    """第二次调用必须携带第一轮的完整对话历史，而不是全新会话。"""
    provider = RecordingProvider()
    advisor = ExplorationAdvisor(provider, ExplorationPolicy())

    first, source = advisor.advise(_snapshot(tmp_path, "/page/0"), [_action("a")])
    assert source == "model"
    assert first is not None and first.page_summary == "page 1"
    assert provider.received_histories[0] == []

    second, _ = advisor.advise(_snapshot(tmp_path, "/page/1"), [_action("b")])
    assert second is not None
    assert provider.received_histories[1] == ["system-prompt", "request-1", "response-1"]
    assert advisor.turn_count == 2


def test_advisor_prunes_history_but_keeps_system_head(tmp_path: Path) -> None:
    provider = RecordingProvider()
    advisor = ExplorationAdvisor(provider, ExplorationPolicy(advisor_history_turns=2))

    for index in range(4):
        advisor.advise(_snapshot(tmp_path, f"/page/{index}"), [_action(f"a{index}")])

    assert len(advisor._history) == 1 + 2 * 2


def test_advisor_validates_and_caps_recommendations(tmp_path: Path) -> None:
    provider = RecordingProvider()
    advisor = ExplorationAdvisor(provider, ExplorationPolicy(advisor_max_actions=2))

    verdict, _ = advisor.advise(
        _snapshot(tmp_path),
        [_action("a"), _action("b")],
    )

    assert verdict is not None
    assert verdict.recommended == [0]


def test_advisor_falls_back_on_provider_failure(tmp_path: Path) -> None:
    provider = RecordingProvider(fail=True)
    advisor = ExplorationAdvisor(provider, ExplorationPolicy())
    verdict, source = advisor.advise(_snapshot(tmp_path), [_action("a")])
    assert verdict is None and source == "heuristic-fallback"

    none_provider = RecordingProvider(return_none=True)
    advisor = ExplorationAdvisor(none_provider, ExplorationPolicy())
    verdict, source = advisor.advise(_snapshot(tmp_path), [_action("a")])
    assert verdict is None and source == "heuristic-fallback"


def test_advisor_records_input_output_per_turn(tmp_path: Path) -> None:
    """每次调用都应留痕输入 payload 与输出建议，供前端思考流与对话视图回放。"""
    provider = RecordingProvider()
    advisor = ExplorationAdvisor(provider, ExplorationPolicy())

    advisor.advise(_snapshot(tmp_path, "/page/0"), [_action("btn-home")], "历史上下文摘要")

    assert len(advisor.turns) == 1
    record = advisor.turns[0]
    assert record.turn == 1
    assert record.source == "model"
    assert record.page_path == "/page/0"
    assert record.snapshot_path is not None and record.snapshot_path.endswith("screen.png")
    assert "候选动作" in record.input
    assert "btn-home" in record.input
    assert "历史上下文摘要" in record.input
    assert record.output is not None
    assert record.output.page_summary == "page 1"
    assert record.output.recommended == [0]
    assert record.elapsed_ms is not None
    assert advisor.turn_count == 1


def test_advisor_records_failed_turns(tmp_path: Path) -> None:
    """失败调用同样留痕：异常来源标记 error，返回空建议标记 heuristic-fallback。"""
    fail_provider = RecordingProvider(fail=True)
    advisor = ExplorationAdvisor(fail_provider, ExplorationPolicy())
    advisor.advise(_snapshot(tmp_path), [_action("a")])

    assert len(advisor.turns) == 1
    failed = advisor.turns[0]
    assert failed.source == "error"
    assert failed.output is None
    assert "advisor backend down" in (failed.error or "")
    assert advisor.turn_count == 0  # 失败轮不计入成功对话轮数

    none_provider = RecordingProvider(return_none=True)
    advisor = ExplorationAdvisor(none_provider, ExplorationPolicy())
    advisor.advise(_snapshot(tmp_path), [_action("a")])
    none_turn = advisor.turns[0]
    assert none_turn.source == "heuristic-fallback"
    assert none_turn.error is None
    assert none_turn.output is None


def test_apply_advisor_reorders_and_avoids_content_clicks() -> None:
    from harmony_test_agent.discovery.explorer import BoundedExplorer

    explorer = BoundedExplorer.__new__(BoundedExplorer)
    explorer.policy = ExplorationPolicy(max_actions_per_page=8)
    candidates = [
        _action("content-card"),
        _action("nav-tab"),
        _action("search-input", kind="input"),
        _action("feed-swipe", kind="swipe"),
    ]
    verdict = AdvisorVerdict(recommended=[1, 3], avoid=[0], reason="avoid feed card")

    ordered = explorer._apply_advisor(candidates, verdict)

    ids = [item.action_id for item in ordered]
    assert ids[0] == "nav-tab"
    assert ids[1] == "feed-swipe"
    assert "content-card" not in ids  # avoid 剔除内容型 click
    assert "search-input" in ids  # input 不受 avoid 影响


def test_advisor_prompt_is_stable_without_dynamic_limits() -> None:
    """system prompt 不含每页变化的内容，保持 provider 前缀缓存友好。"""
    assert "{max_actions}" not in ADVISOR_PROMPT
    assert "推荐上限" in ADVISOR_PROMPT
