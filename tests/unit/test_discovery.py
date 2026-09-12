from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from harmony_test_agent.devices import DeviceError
from harmony_test_agent.discovery import (
    ActionRisk,
    ActionRiskClassifier,
    BoundedExplorer,
    DiscoveryPage,
    DiscoveryResult,
    ExplorationAction,
    ExplorationPolicy,
)
from harmony_test_agent.discovery.advisor import AdvisorTurnResult, AdvisorVerdict, ExplorationAdvisor
from harmony_test_agent.models import CommandResult, ResolvedTarget, ScreenSnapshot
from harmony_test_agent.perception.normalizer import normalize_layout, page_path
from harmony_test_agent.runtime.safety import SafetyPolicy
from harmony_test_agent.targets import ForegroundApp


def _ok(command: str = "fake") -> CommandResult:
    return CommandResult(command=command, returncode=0)


def _target() -> ResolvedTarget:
    return ResolvedTarget(
        target_app_id="demo",
        display_name="Demo",
        bundle_name="com.example.demo",
        main_ability="EntryAbility",
        module_name="entry",
        device_id="fake-device",
        source="installed_app",
    )


class FakeDiscoveryDevice:
    """Small deterministic device whose click coordinates select a numbered page."""

    def __init__(self, tmp_path: Path, *, home_actions: int = 3) -> None:
        self.tmp_path = tmp_path
        self.home_actions = home_actions
        self.state = 0
        self.foreground_bundle = _target().bundle_name
        self.back_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.screenshot_calls = 0
        self.screenshot_labels: list[str] = []
        self.collect_calls = 0
        self.home_texts: list[str] = []
        self.home_cards: list[str] = []
        self.wrong_launch_states: list[int] = []
        self.foreign_start_after: int | None = None
        self.foreign_state = 9
        self.force_poll_state: int | None = None
        self.last_catalog_raw = "fake installed-app catalog"

    def list_installed_apps(self) -> list[object]:
        return []

    def stop_app(self, bundle_name: str) -> CommandResult:
        assert bundle_name == _target().bundle_name
        self.stop_calls += 1
        self.state = 0
        self.foreground_bundle = bundle_name
        return _ok("stop")

    def start_app(self, bundle_name: str, ability_name: str, module_name: str | None = None) -> CommandResult:
        assert (bundle_name, ability_name, module_name) == (
            _target().bundle_name,
            _target().main_ability,
            _target().module_name,
        )
        self.start_calls += 1
        if self.wrong_launch_states:
            self.state = self.wrong_launch_states.pop(0)
        elif self.foreign_start_after is not None and self.start_calls > self.foreign_start_after:
            self.state = self.foreign_state
        else:
            self.state = 0
        self.foreground_bundle = bundle_name
        return _ok("start")

    def current_foreground_app(self) -> ForegroundApp:
        ability = _target().main_ability if self.foreground_bundle == _target().bundle_name else "ForeignAbility"
        return ForegroundApp(bundle_name=self.foreground_bundle, ability_name=ability, window_type="main")

    def screenshot(self, output_dir: Path, run_id: str, label: str = "screen") -> ScreenSnapshot:
        self.screenshot_calls += 1
        self.screenshot_labels.append(label)
        return self.snapshot(self.state, run_id=run_id, label=f"{label}-{self.screenshot_calls}")

    def collect_ui_hierarchy(self) -> dict:
        self.collect_calls += 1
        if self.force_poll_state is not None:
            return self._hierarchy(self.force_poll_state)
        return self._hierarchy(self.state)

    def _hierarchy(self, state: int) -> dict:
        children: list[dict[str, object]] = []
        for page in range(state + 1, self.home_actions + 1):
            children.append(
                {
                    "attributes": {
                        "key": f"open_page_{page}",
                        "text": f"Page {page}",
                        "type": "Button",
                        "clickable": "true",
                        "visible": "true",
                        "enabled": "true",
                        "bounds": f"[{page * 100},10][{page * 100 + 20},30]",
                    },
                    "children": [],
                }
            )
        if state == 0:
            for text in self.home_texts:
                children.append(
                    {
                        "attributes": {
                            "text": text,
                            "type": "Text",
                            "visible": "true",
                            "enabled": "true",
                            "bounds": "[0,100][400,140]",
                        },
                        "children": [],
                    }
                )
            for index, card_key in enumerate(self.home_cards):
                children.append(
                    {
                        "attributes": {
                            "key": card_key,
                            "text": "Card",
                            "type": "Text",
                            "clickable": "true",
                            "visible": "true",
                            "enabled": "true",
                            "bounds": f"[0,{200 + index * 160}][1080,{320 + index * 160}]",
                        },
                        "children": [],
                    }
                )
        return {"attributes": {"pagePath": f"/page/{state}"}, "children": children}

    def snapshot(self, state: int, *, run_id: str = "discovery", label: str = "expected") -> ScreenSnapshot:
        hierarchy = self._hierarchy(state)
        return ScreenSnapshot(
            snapshot_id=label,
            run_id=run_id,
            image_path=self.tmp_path / f"page-{state}.png",
            image_sha256=f"hash-{state}",
            width=1080,
            height=1920,
            page_path=page_path(hierarchy),
            elements=normalize_layout(hierarchy, 1080, 1920),
        )

    def click(self, x: int, y: int) -> CommandResult:
        del y
        if x == 999:
            self.foreground_bundle = "com.example.external"
        else:
            self.state = x // 100
        return _ok("click")

    def input_text(self, text: str, x: int | None = None, y: int | None = None) -> CommandResult:
        del text, x, y
        return _ok("input")

    def swipe(self, start: tuple[int, int], end: tuple[int, int], duration: float = 0.5) -> CommandResult:
        del start, end, duration
        return _ok("swipe")

    def back(self) -> CommandResult:
        self.back_calls += 1
        self.foreground_bundle = _target().bundle_name
        self.state = max(0, self.state - 1)
        return _ok("back")

    def wait(self, seconds: float) -> CommandResult:
        del seconds
        return _ok("wait")

    def collect_logs(self, output_path: Path) -> CommandResult:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("fake log", encoding="utf-8")
        return _ok("hilog")


def _explorer(tmp_path: Path, device: FakeDiscoveryDevice, **policy: object) -> BoundedExplorer:
    return BoundedExplorer(
        device=device,  # type: ignore[arg-type]
        target=_target(),
        output_dir=tmp_path / "discovery",
        run_id="run-discovery",
        policy=ExplorationPolicy(**policy),
    )


class _StubAdvisorProvider:
    """始终返回固定建议的顾问后端替身，用于探索流程中的留痕验证。"""

    def __init__(self) -> None:
        self.calls = 0

    async def advise_turn(self, history: list[object], screenshot: bytes, payload: str) -> AdvisorTurnResult:
        del history, screenshot
        self.calls += 1
        verdict = AdvisorVerdict(page_summary=f"页面 {self.calls}", recommended=[0], avoid=[1], reason="结构优先")
        return AdvisorTurnResult(verdict=verdict, history=["system-prompt", payload, verdict.model_dump_json()])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_pages", 21),
        ("max_actions_per_page", 9),
        ("max_duration_seconds", 901),
        ("settle_timeout_seconds", 31),
        ("restore_retries", 4),
    ],
)
def test_exploration_policy_rejects_limits_above_hard_caps(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        ExplorationPolicy(**{field: value})


def test_explorer_enforces_page_and_per_page_action_limits_with_fake_device(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path, home_actions=4)

    result = _explorer(tmp_path, device, max_pages=2, max_actions_per_page=2).explore()

    assert len(result.pages) == 2
    home_transitions = [item for item in result.transitions if item.source_page_id == result.pages[0].page_id]
    assert len(home_transitions) == 2
    assert all(item.success for item in home_transitions)


def test_candidate_digest_maps_indices_to_readable_labels() -> None:
    """候选摘要必须保留编号语义：顾问 recommended/avoid 的下标即此列表下标。"""
    key_action = ExplorationAction(action_id="a", kind="click", locator_kind="key", locator_value="btn_home")
    text_action = ExplorationAction(action_id="b", kind="input", locator_kind="text", target_text="搜索框")
    coord_action = ExplorationAction(action_id="c", kind="swipe", coordinate=(10, 20), direction="up")

    digest = BoundedExplorer._candidate_digest([key_action, text_action, coord_action])

    assert digest[0] == {"index": 0, "kind": "click", "label": "btn_home", "coordinate": None}
    assert digest[1]["label"] == "搜索框"
    assert digest[2]["coordinate"] == [10, 20]


def test_explorer_streams_advisor_turn_events_and_persists_log(tmp_path: Path) -> None:
    """探索过程应实时推送 advisor_turn 留痕事件，并把完整对话留痕持久化到结果。"""
    device = FakeDiscoveryDevice(tmp_path, home_actions=2)
    for state in range(3):  # 顾问会读取截图字节，预置假图片
        (tmp_path / f"page-{state}.png").write_bytes(b"\x89PNG-fake")

    progress_events: list[tuple[str, dict[str, object]]] = []
    advisor = ExplorationAdvisor(_StubAdvisorProvider(), ExplorationPolicy(advisor_max_actions=2))
    explorer = BoundedExplorer(
        device=device,  # type: ignore[arg-type]
        target=_target(),
        output_dir=tmp_path / "discovery",
        run_id="run-advisor-log",
        policy=ExplorationPolicy(max_pages=2),
        progress=lambda kind, payload: progress_events.append((kind, payload)),
        advisor=advisor,
    )

    result = explorer.explore()

    assert advisor.turns and result.advisor_turns == advisor.turn_count
    assert len(result.advisor_log) == len(advisor.turns)
    first = result.advisor_log[0]
    assert first.source == "model"
    assert first.output is not None and first.output.page_summary == "页面 1"
    assert "候选动作" in first.input

    advisor_events = [payload for kind, payload in progress_events if kind == "advisor"]
    turn_events = [payload for kind, payload in progress_events if kind == "advisor_turn"]
    assert advisor_events and turn_events
    # 全部进度 payload 带 stage 标识，前端据此分发思考流阶段
    assert all("stage" in payload for _, payload in progress_events)
    digest = turn_events[0]["candidates"]
    assert isinstance(digest, list) and digest and {"index", "kind", "label"} <= set(digest[0])
    for entry in result.advisor_verdicts:
        assert "candidates" in entry


def test_risk_classifier_keeps_forbidden_actions_blocked_and_requires_explicit_opt_in() -> None:
    classifier = ActionRiskClassifier()

    forbidden, forbidden_reason, permission = classifier.classify("确认购买并支付", ExplorationPolicy())
    login_blocked, _, login_permission = classifier.classify("登录账户", ExplorationPolicy())
    login_allowed, _, allowed_permission = classifier.classify("登录账户", ExplorationPolicy(allow_login=True))
    uncertain, _, _ = classifier.classify("输入验证码", ExplorationPolicy())

    assert (forbidden, permission) == (ActionRisk.FORBIDDEN, None)
    assert "permanently forbidden" in forbidden_reason
    assert (login_blocked, login_permission) == (ActionRisk.REQUIRES_OPT_IN, "login")
    assert (login_allowed, allowed_permission) == (ActionRisk.DEFAULT_ALLOWED, "login")
    assert uncertain == ActionRisk.BLOCKED_UNCERTAIN
    assert SafetyPolicy(ExplorationPolicy())._blocked_match("确认提交") == "确认"
    assert SafetyPolicy(ExplorationPolicy(allow_submit=True))._blocked_match("确认提交") is None


def test_cross_bundle_navigation_is_blocked_and_target_is_recovered(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path)
    explorer = _explorer(tmp_path, device)
    before = device.snapshot(0)
    foreground = device.current_foreground_app()
    page = explorer._page(before, foreground, 1)
    action = ExplorationAction(
        action_id="leave-app",
        kind="click",
        coordinate=(999, 20),
        target_text="Open external app",
    )

    transition, after = explorer._perform(page, before, foreground, action, 0)

    assert after is None
    assert not transition.success
    assert transition.foreground_after is not None
    assert transition.foreground_after.bundle_name == "com.example.external"
    assert transition.blocked_reason == "cross-bundle navigation blocked: com.example.external"
    assert device.back_calls == 1
    assert device.current_foreground_app().bundle_name == _target().bundle_name


def test_restore_path_relaunches_and_replays_every_action_to_expected_page(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path)
    explorer = _explorer(tmp_path, device)
    path = [
        ExplorationAction(
            action_id="to-one",
            kind="click",
            locator_kind="key",
            locator_value="open_page_1",
            target_text="Page 1",
        ),
        ExplorationAction(
            action_id="to-two",
            kind="click",
            locator_kind="key",
            locator_value="open_page_2",
            target_text="Page 2",
        ),
    ]
    expected = device.snapshot(2)
    device.state = 7

    restored, foreground = explorer._restore_path(path, expected, device.current_foreground_app(), sequence=10)

    assert restored.page_path == "/page/2"
    assert foreground.bundle_name == _target().bundle_name
    assert device.stop_calls == 1
    assert device.start_calls == 1


def test_snapshot_signature_ignores_volatile_texts(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path)
    explorer = _explorer(tmp_path, device)
    foreground = device.current_foreground_app()

    device.home_texts = ["正在加载首页…", "06", ":", "46"]
    loading = device.snapshot(0)
    device.home_texts = ["加载中", "06", ":", "47"]
    reloaded = device.snapshot(0)
    device.home_texts = ["推荐内容", "如何看待 DeepSeek V4 PRO 9月14日之后继续提供服务？"]
    loaded = device.snapshot(0)

    assert explorer._snapshot_signature(loading, foreground) == explorer._snapshot_signature(reloaded, foreground)
    assert explorer._snapshot_signature(loading, foreground) != explorer._snapshot_signature(loaded, foreground)


def test_restore_path_accepts_content_drift_when_structure_matches(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path)
    explorer = _explorer(tmp_path, device)
    foreground = device.current_foreground_app()

    device.home_texts = ["正在加载首页…"]
    expected = device.snapshot(0)
    device.home_texts = [
        "推荐内容",
        "如何看待 DeepSeek V4 PRO 9月14日之后继续提供服务？",
        "2178 赞同",
        "06, :, 47",
    ]

    restored, restored_foreground = explorer._restore_path([], expected, foreground, sequence=0)

    assert restored.page_path == "/page/0"
    assert restored_foreground.bundle_name == _target().bundle_name
    assert device.stop_calls == 1
    assert device.start_calls == 1


def test_restore_path_retries_until_restored_page_matches(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path)
    explorer = _explorer(tmp_path, device)
    device.wrong_launch_states = [9]
    expected = device.snapshot(0)

    restored, _ = explorer._restore_path([], expected, device.current_foreground_app(), sequence=0)

    assert restored.page_path == "/page/0"
    assert device.start_calls == 2


def test_restore_path_raises_with_diff_detail_after_retries_exhausted(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path)
    explorer = _explorer(tmp_path, device)
    device.wrong_launch_states = [9, 9, 9, 9]
    expected = device.snapshot(0)

    with pytest.raises(DeviceError) as excinfo:
        explorer._restore_path([], expected, device.current_foreground_app(), sequence=0)

    message = str(excinfo.value)
    assert "does not match the queued page structure" in message
    assert "/page/9" in message
    assert device.start_calls == 3


def test_restore_path_ignores_key_differences_when_interactive_structure_matches(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path)
    explorer = _explorer(tmp_path, device)
    foreground = device.current_foreground_app()

    device.home_texts = ["推荐内容"]
    device.home_cards = ["p2_search_hot_12"]
    expected = device.snapshot(0)
    device.home_cards = ["p2_search_history_设计师称中国客厅已失去意义"]

    restored, _ = explorer._restore_path([], expected, foreground, sequence=0)

    assert restored.page_path == "/page/0"
    assert device.start_calls == 1


def test_restore_path_fails_when_expected_interactive_structure_is_missing(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path)
    explorer = _explorer(tmp_path, device)
    foreground = device.current_foreground_app()

    device.home_cards = ["p2_home_feed_card_answer_2082075214809249635"]
    expected = device.snapshot(0)
    device.home_cards = []

    with pytest.raises(DeviceError) as excinfo:
        explorer._restore_path([], expected, foreground, sequence=0)

    message = str(excinfo.value)
    assert "missing widgets=['Text/True/False/False']" in message
    assert device.start_calls == 3


def test_explore_skips_pages_that_cannot_be_restored(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path, home_actions=3)
    device.foreign_start_after = 1
    explorer = _explorer(tmp_path, device, max_pages=3, max_actions_per_page=2)

    result = explorer.explore()

    assert result.stop_reason == "queue_exhausted"
    # 首页首个动作成功、目标页照常入队；随后 back 失效且冷启动全部落在外包应用，
    # 源页候选中止，排队页恢复三次全部失败后被跳过。
    assert len(result.pages) == 2
    assert len(result.transitions) == 1
    assert all(item.success for item in result.transitions)
    assert all(item.source_page_id == result.pages[0].page_id for item in result.transitions)
    # 初始 1 次 + 首个动作后恢复 3 次 + 排队页恢复 3 次 = 7 次有界重试
    assert device.stop_calls == 7


def test_explore_stops_candidates_on_source_page_that_cannot_be_restored(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path, home_actions=3)
    device.foreign_start_after = 2
    explorer = _explorer(tmp_path, device, max_pages=3, max_actions_per_page=3)

    result = explorer.explore()

    assert result.stop_reason == "queue_exhausted"
    # 第二个动作的冷恢复仍成功（start#2 未越界），第三个动作后恢复失败并中止源页候选。
    assert len(result.transitions) == 2
    assert all(item.success for item in result.transitions)
    assert len(result.pages) == 3
    assert all(item.source_page_id == result.pages[0].page_id for item in result.transitions)


def test_capture_settled_returns_initial_frame_when_hierarchy_is_stable(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path)
    explorer = _explorer(tmp_path, device)

    snapshot = explorer._capture_settled("restore-005-000")

    assert snapshot.page_path == "/page/0"
    assert device.screenshot_labels == ["restore-005-000"]
    assert device.collect_calls == 1


def test_capture_settled_refreshes_when_hierarchy_never_stabilizes(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path)
    device.force_poll_state = 9
    explorer = _explorer(tmp_path, device)

    snapshot = explorer._capture_settled("restore-005-000")

    assert device.screenshot_labels == ["restore-005-000", "restore-005-000-settled"]
    assert snapshot.page_path == "/page/0"
    assert snapshot.snapshot_id.startswith("restore-005-000-settled")


def _feed_home_hierarchy(*card_ids: str) -> dict:
    """首页：导航按钮 + 可滚动列表 + 指定内容 ID 的信息流卡片。"""
    children: list[dict[str, object]] = [
        {
            "attributes": {
                "key": "p2_home_titlebar_search",
                "text": "搜索",
                "type": "Button",
                "clickable": "true",
                "visible": "true",
                "enabled": "true",
                "bounds": "[20,10][180,30]",
            },
            "children": [],
        },
        {
            "attributes": {
                "type": "List",
                "scrollable": "true",
                "visible": "true",
                "bounds": "[0,50][1080,1900]",
            },
            "children": [],
        },
    ]
    for index, card_id in enumerate(card_ids):
        children.append(
            {
                "attributes": {
                    "key": card_id,
                    "text": f"一条很长的信息流卡片标题内容示例文本用于触发内容判定{index}",
                    "type": "Text",
                    "clickable": "true",
                    "visible": "true",
                    "enabled": "true",
                    "bounds": f"[0,{100 + index * 160}][1080,{220 + index * 160}]",
                },
                "children": [],
            }
        )
    return {"attributes": {"pagePath": "/page/0"}, "children": children}


def _snapshot_from_hierarchy(tmp_path: Path, hierarchy: dict, snapshot_id: str) -> ScreenSnapshot:
    return ScreenSnapshot(
        snapshot_id=snapshot_id,
        run_id="run-discovery",
        image_path=tmp_path / f"{snapshot_id}.png",
        image_sha256=f"hash-{snapshot_id}",
        width=1080,
        height=1920,
        page_path=page_path(hierarchy),
        elements=normalize_layout(hierarchy, 1080, 1920),
    )


def test_structural_identity_ignores_content_instance_ids(tmp_path: Path) -> None:
    """信息流换卡（内容 ID 与文本变化）不改变逻辑页身份，但整树签名不同。"""
    device = FakeDiscoveryDevice(tmp_path, home_actions=0)
    explorer = _explorer(tmp_path, device)
    foreground = device.current_foreground_app()

    device.home_cards = ["p2_home_feed_card_answer_111"]
    first = device.snapshot(0)
    device.home_cards = ["p2_home_feed_card_answer_222", "p2_home_feed_card_answer_333"]
    second = device.snapshot(0)

    assert explorer._structural_identity(first, foreground) == explorer._structural_identity(second, foreground)
    assert BoundedExplorer._snapshot_signature(first, foreground) != BoundedExplorer._snapshot_signature(
        second, foreground
    )


def test_structural_identity_ignores_feed_container_variants(tmp_path: Path) -> None:
    """互不为子集的信息流渲染变体（feed_list 在场 vs feed_card_article 在场）收敛为同一身份。"""
    device = FakeDiscoveryDevice(tmp_path, home_actions=0)
    explorer = _explorer(tmp_path, device)
    foreground = device.current_foreground_app()

    def home(children: list[dict[str, object]]) -> ScreenSnapshot:
        return _snapshot_from_hierarchy(
            tmp_path, {"attributes": {"pagePath": "pages/Index"}, "children": children}, "home"
        )

    def card(key: str, top: int) -> dict[str, object]:
        return {
            "attributes": {
                "key": key,
                "text": "Card",
                "type": "Text",
                "clickable": "true",
                "visible": "true",
                "enabled": "true",
                "bounds": f"[0,{top}][1080,{top + 120}]",
            },
            "children": [],
        }

    tabs = {
        "attributes": {
            "key": "p1_hds_tabs",
            "type": "Row",
            "visible": "true",
            "bounds": "[0,1400][1080,1500]",
        },
        "children": [],
    }
    scrollable = {
        "attributes": {"type": "List", "scrollable": "true", "bounds": "[0,150][1080,1400]"},
        "children": [],
    }
    variant_list = home([tabs, scrollable, card("p2_home_feed_card_answer_111", 200)])
    variant_article = home(
        [tabs, scrollable, card("p2_home_feed_card_answer_222", 200), card("p2_home_feed_card_article_987654321", 400)]
    )

    assert explorer._structural_identity(variant_list, foreground) == explorer._structural_identity(
        variant_article, foreground
    )


def test_content_like_clicks_rank_below_input_and_swipe(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path, home_actions=0)
    explorer = _explorer(tmp_path, device)
    snapshot = _snapshot_from_hierarchy(
        tmp_path, _feed_home_hierarchy("p2_home_feed_card_answer_2081036721471468536"), "feed-home"
    )

    actions = explorer.candidate_actions(snapshot)

    kinds = [item.kind for item in actions]
    assert kinds[-1] == "click"
    assert actions[-1].content_like is True
    assert actions[-1].locator_value == "p2_home_feed_card_answer_2081036721471468536"
    assert "swipe" in kinds and "click" in kinds[: kinds.index("swipe")]


def test_type_quota_keeps_input_and_swipe_on_click_rich_pages(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path, home_actions=0)
    explorer = _explorer(tmp_path, device, max_actions_per_page=8)
    children = [
        {
            "attributes": {
                "key": f"p2_home_tab_{index}",
                "text": f"Tab {index}",
                "type": "Button",
                "clickable": "true",
                "visible": "true",
                "enabled": "true",
                "bounds": f"[{index * 40},10][{index * 40 + 20},30]",
            },
            "children": [],
        }
        for index in range(10)
    ]
    children.append(
        {
            "attributes": {
                "key": "p2_search_input",
                "type": "TextInput",
                "editable": "true",
                "visible": "true",
                "enabled": "true",
                "bounds": "[80,100][700,180]",
            },
            "children": [],
        }
    )
    children.append(
        {
            "attributes": {"type": "List", "scrollable": "true", "visible": "true", "bounds": "[0,200][1080,1900]"},
            "children": [],
        }
    )
    snapshot = _snapshot_from_hierarchy(
        tmp_path, {"attributes": {"pagePath": "/page/0"}, "children": children}, "rich-home"
    )

    selected = explorer._select_candidates(snapshot)

    assert {item.kind for item in selected} == {"click", "input", "swipe"}
    assert len(selected) == 8


def test_content_dependent_paths_are_not_enqueued(tmp_path: Path) -> None:
    """误触信息流卡片进入的内容页不入队，跃迁标记不可回放。"""
    device = FakeDiscoveryDevice(tmp_path, home_actions=0)
    device.home_cards = ["p2_home_feed_card_answer_2081036721471468536"]
    explorer = _explorer(tmp_path, device)

    result = explorer.explore()

    assert result.stop_reason == "queue_exhausted"
    assert len(result.pages) == 1
    card_transitions = [item for item in result.transitions if item.action.content_like]
    assert card_transitions and all(item.replayable is False for item in card_transitions)
    assert card_transitions[0].target_page_id is None
    # back 恢复失败后冷启动兜底一次：初始 + 1 次恢复
    assert device.stop_calls == 2
    assert device.back_calls >= 1


def test_recovery_uses_back_instead_of_cold_restart_when_possible(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path, home_actions=1)
    explorer = _explorer(tmp_path, device)

    result = explorer.explore()

    assert result.stop_reason == "queue_exhausted"
    assert len(result.pages) == 2
    # 点击进入 /page/1 后经 back 返回首页，无需冷启动；仅队列出队时冷启动回放一次。
    assert device.stop_calls == 2
    assert device.start_calls == 2
    assert device.back_calls == 1


def test_input_actions_always_cold_restore_for_keyboard_state(tmp_path: Path) -> None:
    device = FakeDiscoveryDevice(tmp_path, home_actions=0)
    explorer = _explorer(tmp_path, device)
    input_hierarchy = {
        "attributes": {"pagePath": "/page/0"},
        "children": [
            {
                "attributes": {
                    "key": "p2_search_input",
                    "type": "TextInput",
                    "editable": "true",
                    "visible": "true",
                    "enabled": "true",
                    "bounds": "[80,100][700,180]",
                },
                "children": [],
            }
        ],
    }
    original_hierarchy = device._hierarchy
    device._hierarchy = lambda state: input_hierarchy if state == 0 else original_hierarchy(state)  # type: ignore[method-assign]

    result = explorer.explore()

    assert result.stop_reason == "queue_exhausted"
    assert any(item.action.kind == "input" for item in result.transitions)
    # input 后跳过 back 直接冷恢复：初始 1 次 + input 后 1 次
    assert device.stop_calls == 2
    assert device.back_calls == 0


def test_early_success_requires_configurable_interaction_kinds(tmp_path: Path) -> None:
    click = ExplorationAction(action_id="c1", kind="click")
    enter = ExplorationAction(action_id="i1", kind="input")
    deep_click = ExplorationAction(action_id="c2", kind="click")
    paths: list[list[ExplorationAction]] = [[], [click], [click, enter], [click, enter, deep_click]]
    pages = [
        DiscoveryPage(
            page_id=f"page-{index}",
            signature=f"signature-{index}",
            page_path=f"/page/{index}",
            bundle_name=_target().bundle_name,
            snapshot_id=f"snapshot-{index}",
            image_path=tmp_path / f"page-{index}.png",
            element_count=1,
            discovered_order=index,
            path_actions=path,
        )
        for index, path in enumerate(paths, 1)
    ]
    result = DiscoveryResult(target=_target(), policy=ExplorationPolicy(), pages=pages)

    assert BoundedExplorer._early_success(result, 2) is True
    assert BoundedExplorer._early_success(result, 3) is False
