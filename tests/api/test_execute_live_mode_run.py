"""首次实时模式运行（live_mode=True）生成的脚本必须立即可执行。

回归来源：``run-20260921T104829Z-00d18d8a``（知乎++ 搜索流程）端到端成功、10 个动作全部
成功、1 条真实通过的断言，却因为唯一的 ``live-mode trace cannot qualify for acceptance
replay`` 被判为诊断产物：前端按钮禁用、``POST /api/runs/{id}/execute`` 返回 409。
本文件用同一形态的精简 trace 钉住改造后的行为（计划 G1/G2）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from harmony_test_agent.api import create_app
from harmony_test_agent.config import Settings
from harmony_test_agent.generation import HypiumGenerator
from harmony_test_agent.models import (
    ActionResult,
    LocatorCandidate,
    LocatorKind,
    RunState,
    RunTrace,
    TargetAppProfile,
    ToolName,
)

BUNDLE_NAME = "com.github.zhuoyi233.zhplus"
ABILITY_NAME = "EntryAbility"


def _click(step_id: str, key: str, target: str) -> ActionResult:
    return ActionResult(
        step_id=step_id,
        tool=ToolName.CLICK_ELEMENT,
        success=True,
        params={"target": target},
        locator=LocatorCandidate(kind=LocatorKind.KEY, value=key),
    )


def _live_mode_trace() -> RunTrace:
    """精简复刻 run-20260921T104829Z-00d18d8a：10 个动作全成功、1 条断言、全结构化 key。"""
    return RunTrace(
        run_id="run-20260921T104829Z-00d18d8a",
        target_app_id="com-github-zhuoyi233-zhplus",
        task="打开知乎++，搜索 OpenHarmony",
        device_id="device-1",
        state=RunState.COMPLETED,
        agent_outcome="completed",
        live_mode=True,
        actions=[
            ActionResult(step_id="1", tool=ToolName.OPEN_APP, success=True),
            ActionResult(step_id="2", tool=ToolName.INSPECT_SCREEN, success=True),
            _click("3", "p2_home_titlebar_search", "搜索"),
            _click("4", "p2_search_input", "搜索输入框"),
            _click("5", "p2_search_history_zcode", "历史记录"),
            _click("6", "p2_search_hot_0", "热搜"),
            _click("7", "p2_search_hot_0", "热搜"),
            ActionResult(
                step_id="8",
                tool=ToolName.ASSERT_VISIBLE,
                success=True,
                params={"target": "搜索结果"},
                locator=LocatorCandidate(kind=LocatorKind.KEY, value="p2_search_hot_0"),
            ),
            _click("9", "p2_search_input", "搜索输入框"),
            ActionResult(step_id="10", tool=ToolName.FINISH, success=True),
        ],
    )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "runtime-home",
        cases_dir=tmp_path / "cases",
        agent_provider="mock",
    )


def test_live_mode_run_generates_an_immediately_runnable_script(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    manager = app.state.manager
    profile = TargetAppProfile(
        target_app_id="com-github-zhuoyi233-zhplus",
        display_name="知乎++",
        bundle_name=BUNDLE_NAME,
        main_ability=ABILITY_NAME,
    )
    trace = _live_mode_trace()
    trace.profile_snapshot = profile
    trace.generated = HypiumGenerator(manager.artifacts).generate(trace, profile)
    manager.repository.save_trace(trace)
    manager.artifacts.save_trace(trace)

    blocked = asyncio.Event()

    async def blocked_run(run_id: str) -> None:
        await blocked.wait()

    monkeypatch.setattr(manager, "_run_replay", blocked_run)

    with TestClient(app) as client:
        detail = client.get("/api/runs/run-20260921T104829Z-00d18d8a/script")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        # G1：purpose=acceptance、replay_eligible=true、置信度 high（10 个动作全成功 + 1 条断言）。
        assert body["purpose"] == "acceptance"
        assert body["acceptance_replay_enabled"] is True
        assert body["confidence"] == "high"
        assert body["confidence_factors"] == []
        assert body["incomplete_reasons"] == []
        assert body["runnable_blockers"] == []
        # G2/G6：live_mode 只影响晋级资格，不再阻断执行。
        assert body["promotion_eligible"] is False
        assert body["promotion_blockers"] == ["live-mode trace is not Profile-promotion evidence"]

        # 旧行为是 409「provisional or live-mode runs cannot execute …」；现在必须是 202。
        accepted = client.post("/api/runs/run-20260921T104829Z-00d18d8a/execute?attempts=1")
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["status"] == "pending"
        blocked.set()


def test_live_mode_run_is_persisted_as_a_reusable_case(tmp_path: Path) -> None:
    """G5：实时模式轨迹只要物理上可执行就会自动入库，不再被质量门禁拦下。"""
    settings = _settings(tmp_path)
    app = create_app(settings)
    manager = app.state.manager
    profile = TargetAppProfile(
        target_app_id="com-github-zhuoyi233-zhplus",
        display_name="知乎++",
        bundle_name=BUNDLE_NAME,
        main_ability=ABILITY_NAME,
    )
    trace = _live_mode_trace()
    trace.profile_snapshot = profile
    trace.generated = HypiumGenerator(manager.artifacts).generate(trace, profile)

    record = manager.case_library.build_from_run(trace)

    assert record is not None
    assert record.confidence == "high"
    assert record.promotion_eligible is False
    assert record.promotion_blockers == ["live-mode trace is not Profile-promotion evidence"]
    assert manager.case_library.list()
