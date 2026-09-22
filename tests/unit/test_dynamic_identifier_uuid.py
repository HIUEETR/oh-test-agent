"""改动 3 回归：UUID 形式的假「稳定定位器」必须被识别并拒绝入库。

真机复盘（``profiles/draft/com-example-neteasymusic.json``）：一条
``key = b3911500-f93c-4ab0-8c02-44751c2ca862``（UUID4，每次渲染都变）以
``confidence: high`` / ``source: live_task_harvest`` 进了 Profile，随后经
``PlanningContext.stable_locator_names`` 喂给 planner，planner 写出「点击首页的第一个海报
（稳定定位器 b3911500-...）」——执行时找不到该元素，退化成 spatial 点击。

漏网原因：UUID 被连字符切成 8-4-4-4-12 段，``\\d{6,}`` 与 ``[0-9a-f]{16,}`` 两条规则
**没有一段够长**，因此 ``is_dynamic_identifier`` 对它返回 False。

同时必须**不误杀**真正可复用的 key，也不影响「时间戳后缀 key 折叠为可复用前缀」的既有
设计（``add_agenda_title-1789951623657`` → ``add_agenda_title-#``）。
"""

from __future__ import annotations

import uuid

import pytest
from fakes import CALENDAR_FIXTURES, snapshot_from_layout

from harmony_test_agent.agents import AgentOrchestrator
from harmony_test_agent.config import Settings
from harmony_test_agent.discovery import (
    dynamic_identifier_pattern,
    is_dynamic_identifier,
    is_unreusable_dynamic_identifier,
)
from harmony_test_agent.models import (
    ActionResult,
    LocatorCandidate,
    LocatorKind,
    RunTrace,
    TargetAppProfile,
    ToolName,
)
from harmony_test_agent.storage import ArtifactStore, RunRepository

NETEASE_UUID = "b3911500-f93c-4ab0-8c02-44751c2ca862"
#: 真机里那条污染条目的原始形状（``name`` 与 ``key`` 都是 UUID）。
UUID_SAMPLES = [
    NETEASE_UUID,
    str(uuid.uuid4()),
    str(uuid.uuid1()),
    f"row_{uuid.uuid4()}",
]
#: 可复用的真实 key（防误杀）。前两条另受既有 ``is_volatile_evidence_key``（含 time / date
#: 字样）拦截，因此只参与「不是动态标识」的断言，不参与收割门断言。
STABLE_KEYS = [
    "add_agenda_start_time",
    "main_page_date_info",
    "phone_add_agenda",
    "p2_search_input",
]
#: 能真正通过收割门（易变检查 + 动态标识检查）的 key。
HARVESTABLE_KEYS = ["tabs_month", "phone_add_agenda", "p2_search_input"]


class TestDynamicIdentifierRules:
    @pytest.mark.parametrize("value", UUID_SAMPLES)
    def test_uuid_is_a_dynamic_identifier(self, value: str) -> None:
        assert is_dynamic_identifier(value) is True

    def test_netease_uuid_is_unreusable_even_though_it_contains_a_digit_run(self) -> None:
        """UUID 里「3911500」这段 7 位数字会被 ``\\d{6,}`` 折叠，折出来的前缀依旧带 UUID 残段。

        因此 UUID 不能靠「折叠是否为空操作」识别，必须由 UUID 规则直接命中 —— 这条断言
        锁住的就是这个顺序。
        """
        assert dynamic_identifier_pattern(NETEASE_UUID) != NETEASE_UUID
        assert "44751c2ca862" in dynamic_identifier_pattern(NETEASE_UUID)
        assert is_unreusable_dynamic_identifier(NETEASE_UUID) is True

    @pytest.mark.parametrize("value", STABLE_KEYS)
    def test_stable_keys_are_not_dynamic(self, value: str) -> None:
        assert is_dynamic_identifier(value) is False
        assert is_unreusable_dynamic_identifier(value) is False

    @pytest.mark.parametrize(
        "value",
        ["add_agenda_title-1790078405913", "add_agenda_title-1789951623657"],
    )
    def test_timestamp_suffix_keys_stay_unaffected(self, value: str) -> None:
        """回归：时间戳后缀 key 仍是动态标识，但**能**折叠出可复用前缀，因此不算不可复用。"""
        assert is_dynamic_identifier(value) is True
        assert is_unreusable_dynamic_identifier(value) is False
        assert dynamic_identifier_pattern(value).endswith("-#")

    def test_long_hex_content_id_is_unreusable(self) -> None:
        """长 hex 内容 ID 同样泛化不出前缀（``[0-9a-f]{16,}`` 规则已经把它标为动态）。"""
        value = "content_id_a3f5c9d17b2e4806"
        assert is_dynamic_identifier(value) is True
        assert is_unreusable_dynamic_identifier(value) is True


def _profile() -> TargetAppProfile:
    return TargetAppProfile(
        target_app_id="com-huawei-hmos-calendar",
        display_name="日历",
        bundle_name="com.huawei.hmos.calendar",
        main_ability="EntryAbility",
        provenance={"discovery_run_id": "run-calendar"},
    )


def _trace(snapshot_id: str, keys: list[str]) -> RunTrace:
    snapshot = snapshot_from_layout(CALENDAR_FIXTURES / "calendar-home-t0.json", run_id="run-uuid-gate")
    actions = [
        ActionResult(
            step_id=f"step-{index:02d}",
            tool=ToolName.CLICK_ELEMENT,
            params={"target": f"element-{index}"},
            success=True,
            before_snapshot_id=snapshot_id,
            locator=LocatorCandidate(kind=LocatorKind.KEY, value=key),
        )
        for index, key in enumerate(keys, start=1)
    ]
    return RunTrace(
        run_id="run-uuid-gate",
        target_app_id="com-huawei-hmos-calendar",
        task="收割门：UUID 不得入库",
        device_id="fake-calendar",
        snapshots=[snapshot.model_copy(update={"snapshot_id": snapshot_id}, deep=True)],
        actions=actions,
    )


def _orchestrator(tmp_path) -> AgentOrchestrator:
    settings = Settings(
        agent_provider="mock",
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "home",
        target_profile_path=None,
    )
    return AgentOrchestrator(
        settings,
        repository=RunRepository(settings.resolved_database_path),
        artifacts=ArtifactStore(settings.resolved_runtime_dir),
    )


class TestHarvestGate:
    def test_uuid_locator_never_enters_the_inventory(self, tmp_path) -> None:
        """收割门集成：UUID 候选被丢弃，同一批里的可复用 key 照常入库。"""
        orchestrator = _orchestrator(tmp_path)
        harvested = orchestrator._collect_task_evidence(
            _trace("snap-uuid", [NETEASE_UUID, *HARVESTABLE_KEYS]), _profile()
        )

        values = [item.candidate.value for item in harvested]
        assert NETEASE_UUID not in values
        assert set(HARVESTABLE_KEYS) <= set(values)

    def test_uuid_never_becomes_a_high_confidence_stable_locator(self, tmp_path) -> None:
        """连「转成 StableLocator」这一步都不该发生（真机那次是 confidence: high）。"""
        orchestrator = _orchestrator(tmp_path)
        harvested = orchestrator._collect_task_evidence(_trace("snap-uuid", [NETEASE_UUID]), _profile())

        assert harvested == []

    def test_timestamp_key_is_still_harvested_with_a_warning(self, tmp_path) -> None:
        """回归：既有设计不变 —— 时间戳后缀 key 折叠为前缀模式，按 MEDIUM + 警告入库。

        若这里改成「动态标识一律丢弃」，会把日历 / 知乎那批已经能回放的定位器一起废掉。
        """
        orchestrator = _orchestrator(tmp_path)
        value = "add_agenda_title-1789951623657"
        harvested = orchestrator._collect_task_evidence(_trace("snap-uuid", [value]), _profile())

        assert [item.candidate.value for item in harvested] == [value]
        locator = AgentOrchestrator._stable_locator_from_candidate(harvested[0], "run-uuid-gate")
        assert locator.dynamic_pattern == "add_agenda_title-#"
        assert locator.warning
