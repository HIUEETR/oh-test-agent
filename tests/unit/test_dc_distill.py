"""DC 会话 → Profile 蒸馏器单测（纯 CPU 阶段 + 状态流转）。

设备验证与 Hypium 回放通过注入的假实现替换，因此本文件不依赖真机：
真实设备链路的端到端覆盖在 ``tests/integration/test_dc_distill_flow.py``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from harmony_test_agent.config import Settings
from harmony_test_agent.dc.distill import (
    DcProfileDistiller,
    DistillPreparation,
    infer_session_identity,
    resolve_distill_identity,
)
from harmony_test_agent.dc.models import (
    TOOL_TIER,
    DcError,
    DcToolInvocation,
    DcToolName,
    utc_now,
)
from harmony_test_agent.dc.session import DcSession
from harmony_test_agent.models import BoundingBox, CommandResult, ProfileStatus, ReplayResult, ScreenSnapshot, UIElement
from harmony_test_agent.profiles import ProfileRegistry
from harmony_test_agent.storage import ArtifactStore

BUNDLE = "com.example.notes"
ABILITY = "MainAbility"


# ---------------------------------------------------------------------------
# 最小会话替身：只提供蒸馏需要的属性
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self, invocations: list[DcToolInvocation]) -> None:
        self.invocations = invocations


class _Holder:
    def __init__(self, snapshots: list[ScreenSnapshot]) -> None:
        self.history = snapshots


class _FakeSession:
    """蒸馏器只读取 session_id/device_id/dir/recorder/snapshots/last_foreground_app。"""

    def __init__(
        self,
        tmp_path: Path,
        invocations: list[DcToolInvocation],
        snapshots: list[ScreenSnapshot] | None = None,
        *,
        last_foreground_app: str | None = None,
    ) -> None:
        self.session_id = "dc-20260917T000000Z-abcd1234"
        self.device_id = "127.0.0.1:5555"
        self.dir = tmp_path / "runs" / self.session_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.recorder = _Recorder(invocations)
        self.snapshots = snapshots or []
        self.last_foreground_app = last_foreground_app


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(
        runtime_dir=tmp_path / "runs",
        database_path=tmp_path / "agent.db",
        target_profile_path=None,
        profiles_dir=tmp_path / "profiles",
        runtime_home=tmp_path / "runtime-home",
        agent_provider="mock",
        **overrides,
    )


def _element(key: str, content: str, page: int = 1) -> UIElement:
    return UIElement(
        element_id=key,
        key=key,
        content=content,
        type="Button",
        clickable=True,
        enabled=True,
        bbox=BoundingBox(left=10 * page, top=20, right=10 * page + 80, bottom=60),
    )


def _snapshot(snapshot_id: str, page: int, elements: list[UIElement] | None = None) -> ScreenSnapshot:
    return ScreenSnapshot(
        snapshot_id=snapshot_id,
        run_id="dc-20260917T000000Z-abcd1234",
        image_path=Path(f"page-{page}.png"),
        image_sha256=f"sha-{snapshot_id}",
        width=1080,
        height=1920,
        page_path=f"pages/Page{page}",
        elements=elements if elements is not None else [_element(f"page{page}_control", f"Page {page}")],
    )


def _invocation(
    tool: DcToolName,
    *,
    page_path: str,
    args: dict[str, Any] | None = None,
    element: UIElement | None = None,
    after_snapshot_id: str | None = None,
    invocation_id: str = "inv-001",
    success: bool = True,
    result_summary: str = "",
) -> DcToolInvocation:
    return DcToolInvocation(
        invocation_id=invocation_id,
        turn_id="turn-1",
        tool=tool,
        tier=TOOL_TIER[tool],
        args=args or {},
        success=success,
        started_at=utc_now(),
        ended_at=utc_now(),
        duration_ms=5,
        resolved_element=element,
        page_path=page_path,
        after_snapshot_id=after_snapshot_id,
        result_summary=result_summary,
    )


def _three_page_session(tmp_path: Path) -> _FakeSession:
    """构造一个覆盖 3 页、含 3 个可回放动作与 1 条断言的会话。"""
    invocations = [
        _invocation(
            DcToolName.CLICK,
            page_path="pages/Page1",
            args={"x": 30, "y": 40},
            element=_element("home_search", "搜索"),
            after_snapshot_id="snap-2",
            invocation_id="inv-001",
        ),
        _invocation(
            DcToolName.INPUT_TEXT,
            page_path="pages/Page2",
            args={"text": "OpenHarmony", "coordinate": [40, 200]},
            element=_element("search_input", "搜索输入框", page=2),
            after_snapshot_id="snap-3",
            invocation_id="inv-002",
        ),
        _invocation(
            DcToolName.ASSERT_VISIBLE,
            page_path="pages/Page3",
            args={"target": "OpenHarmony"},
            after_snapshot_id="snap-4",
            invocation_id="inv-003",
        ),
        _invocation(
            DcToolName.SWIPE,
            page_path="pages/Page3",
            args={"start": [500, 1500], "end": [500, 500]},
            after_snapshot_id="snap-4",
            invocation_id="inv-004",
        ),
    ]
    snapshots = [
        _snapshot("snap-1", 1),
        _snapshot("snap-2", 2, [_element("page2_control", "Page 2", page=2)]),
        _snapshot("snap-3", 3, [_element("page3_control", "Page 3", page=3)]),
        _snapshot("snap-4", 3, [_element("page3_control", "Page 3", page=3)]),
    ]
    return _FakeSession(tmp_path, invocations, snapshots)


def _distiller(tmp_path: Path, *, registry: ProfileRegistry | None = None, **settings: Any) -> DcProfileDistiller:
    resolved = _settings(tmp_path, **settings)
    return DcProfileDistiller(
        ArtifactStore(resolved.resolved_runtime_dir),
        registry,
        resolved,
    )


# ---------------------------------------------------------------------------
# 纯 CPU 阶段
# ---------------------------------------------------------------------------


def test_distill_requires_three_pages(tmp_path: Path) -> None:
    """页面覆盖 < 3 → DcError（比赛硬性要求；前端据此提示继续操作）。"""
    invocations = [
        _invocation(DcToolName.CLICK, page_path="pages/Page1", args={"x": 1, "y": 2}, invocation_id="inv-001"),
        _invocation(DcToolName.BACK, page_path="pages/Page1", invocation_id="inv-002"),
    ]
    session = _FakeSession(tmp_path, invocations, [_snapshot("snap-1", 1)])

    with pytest.raises(DcError, match="need >= 3"):
        _distiller(tmp_path).prepare(session, BUNDLE, ABILITY)  # type: ignore[arg-type]


def test_distill_rejects_placeholder_identity(tmp_path: Path) -> None:
    session = _three_page_session(tmp_path)

    with pytest.raises(DcError, match="placeholder"):
        _distiller(tmp_path).prepare(session, "com.example.app", ABILITY)  # type: ignore[arg-type]


def test_distill_extracts_locators_from_invocations(tmp_path: Path) -> None:
    """定位器证据来自 resolved_element 的 key/id，且是单轮（round_number=1）。"""
    session = _three_page_session(tmp_path)

    prepared = _distiller(tmp_path).prepare(session, BUNDLE, ABILITY)  # type: ignore[arg-type]

    names = {item.value for item in prepared.stability.locators}
    assert {"home_search", "search_input"} <= names
    assert all(item.rounds == (1,) for item in prepared.stability.locators)
    assert prepared.draft.status == ProfileStatus.DRAFT
    assert {item.key for item in prepared.draft.stable_locator_inventory} >= {"home_search", "search_input"}


def test_distill_extracts_assertions_from_invocations(tmp_path: Path) -> None:
    """断言证据来自成功的 assert_* 调用；失败断言不计入。"""
    session = _three_page_session(tmp_path)
    session.recorder.invocations.append(
        _invocation(
            DcToolName.ASSERT_NOT_VISIBLE,
            page_path="pages/Page3",
            args={"target": "加载中"},
            invocation_id="inv-005",
            success=False,
        )
    )

    prepared = _distiller(tmp_path).prepare(session, BUNDLE, ABILITY)  # type: ignore[arg-type]

    targets = {item.target for item in prepared.stability.assertions}
    assert targets == {"OpenHarmony"}
    assert prepared.stability.app_assertion_count == 1


def test_distill_calls_stability_analyzer_with_configured_rounds(tmp_path: Path) -> None:
    """分析器必须使用 Settings.profile_verification_rounds，而不是硬编码 3。"""
    session = _three_page_session(tmp_path)
    single = _distiller(tmp_path, profile_verification_rounds=1).prepare(session, BUNDLE, ABILITY)  # type: ignore[arg-type]
    legacy = _distiller(tmp_path, profile_verification_rounds=3).prepare(session, BUNDLE, ABILITY)  # type: ignore[arg-type]

    # 1 轮门禁下证据被接受；3 轮门禁下只有 1 轮观察的证据全部被拒绝。
    assert single.stability.promotable_locator_count >= 1
    assert legacy.stability.promotable_locator_count == 0


def test_distill_builds_replayable_discovery_from_session(tmp_path: Path) -> None:
    """线性会话被重建为「页面 = 动作前缀」的可回放 DiscoveryResult。"""
    session = _three_page_session(tmp_path)

    prepared = _distiller(tmp_path).prepare(session, BUNDLE, ABILITY)  # type: ignore[arg-type]

    discovery = prepared.discovery
    assert discovery.target.bundle_name == BUNDLE
    assert len(discovery.pages) >= 3
    assert {page.page_path for page in discovery.pages} >= {"pages/Page1", "pages/Page2", "pages/Page3"}
    # 最深页面的 path_actions 是完整核心流，且每个前缀都有对应页面（_core_pages 的前提）
    deepest = max(discovery.pages, key=lambda item: len(item.path_actions))
    assert len(deepest.path_actions) == 3
    assert {len(page.path_actions) for page in discovery.pages} >= {0, 1, 2, 3}


def test_distill_skips_non_replayable_and_failed_invocations(tmp_path: Path) -> None:
    """观察/管理类工具与失败调用不进入核心流。"""
    session = _three_page_session(tmp_path)
    session.recorder.invocations.insert(
        0, _invocation(DcToolName.SCREENSHOT, page_path="pages/Page1", invocation_id="inv-000")
    )
    session.recorder.invocations.append(
        _invocation(
            DcToolName.CLICK,
            page_path="pages/Page3",
            args={"x": 1, "y": 1},
            invocation_id="inv-006",
            success=False,
        )
    )

    prepared = _distiller(tmp_path).prepare(session, BUNDLE, ABILITY)  # type: ignore[arg-type]

    deepest = max(prepared.discovery.pages, key=lambda item: len(item.path_actions))
    assert [action.action_id for action in deepest.path_actions] == ["inv-001", "inv-002", "inv-004"]


def test_distill_draft_records_session_provenance(tmp_path: Path) -> None:
    session = _three_page_session(tmp_path)

    prepared: DistillPreparation = _distiller(tmp_path).prepare(session, BUNDLE, ABILITY)  # type: ignore[arg-type]

    assert prepared.draft.provenance.discovery_run_id == session.session_id
    assert prepared.draft.provenance.evidence["distilled_from_dc_session"] == session.session_id
    assert prepared.draft.target_app_id == f"dc-{session.session_id}"


# ---------------------------------------------------------------------------
# 会话身份推断（前端一键蒸馏与 422 兜底的身份来源）
# ---------------------------------------------------------------------------


def _foreground(app: str, ability: str, *, invocation_id: str = "inv-fg", success: bool = True) -> DcToolInvocation:
    """构造一条 ``foreground_app`` 记录，摘要格式与 tools.py::tool_foreground_app 一致。"""
    return _invocation(
        DcToolName.FOREGROUND_APP,
        page_path="pages/Page1",
        invocation_id=invocation_id,
        success=success,
        result_summary=f"bundle={app}, ability={ability}",
    )


def _start_app(
    bundle: str, ability: str, *, invocation_id: str = "inv-start", success: bool = True
) -> DcToolInvocation:
    """构造一条 ``start_app`` 记录，args 键与 tools.py::tool_start_app 一致。"""
    return _invocation(
        DcToolName.START_APP,
        page_path="pages/Page1",
        args={"bundle_name": bundle, "ability_name": ability},
        invocation_id=invocation_id,
        success=success,
    )


def test_infer_identity_from_foreground_summary(tmp_path: Path) -> None:
    """首选来源是最近一次**成功**的 FOREGROUND_APP 结果摘要（失败记录被跳过）。"""
    session = _FakeSession(
        tmp_path,
        [
            _foreground("com.example.notes", "MainAbility", invocation_id="inv-failed", success=False),
            _foreground("com.old.app", "OldAbility", invocation_id="inv-old"),
            _foreground(BUNDLE, ABILITY),
        ],
    )

    assert infer_session_identity(session) == (BUNDLE, ABILITY)  # type: ignore[arg-type]


def test_infer_identity_falls_back_to_start_app_when_ability_unknown(tmp_path: Path) -> None:
    """``ability == "unknown"``（设备未上报）视为无效 → 退化到成功 START_APP 的 args。"""
    session = _FakeSession(tmp_path, [_foreground(BUNDLE, "unknown"), _start_app(BUNDLE, ABILITY)])

    assert infer_session_identity(session) == (BUNDLE, ABILITY)  # type: ignore[arg-type]


def test_infer_identity_combines_last_foreground_app_with_start_app_ability(tmp_path: Path) -> None:
    """无 FOREGROUND_APP 记录时：bundle 来自 last_foreground_app，ability 来自同 bundle 的 START_APP。"""
    session = _FakeSession(
        tmp_path,
        [_start_app(BUNDLE, ABILITY, invocation_id="inv-matching")],
        last_foreground_app=BUNDLE,
    )

    assert infer_session_identity(session) == (BUNDLE, ABILITY)  # type: ignore[arg-type]

    # last_foreground_app 存在但没有任何同 bundle 的成功 START_APP → 仍然推断失败
    no_matching_start = _FakeSession(
        tmp_path,
        [_start_app("com.example.app", ABILITY, invocation_id="inv-placeholder")],
        last_foreground_app=BUNDLE,
    )

    assert infer_session_identity(no_matching_start) is None  # type: ignore[arg-type]


def test_infer_identity_returns_none_without_evidence(tmp_path: Path) -> None:
    """完全没有身份线索（含无法解析的 FOREGROUND_APP 摘要）→ None。"""
    session = _FakeSession(
        tmp_path,
        [
            _invocation(DcToolName.CLICK, page_path="pages/Page1", args={"x": 1, "y": 2}),
            _invocation(
                DcToolName.FOREGROUND_APP,
                page_path="pages/Page1",
                invocation_id="inv-no-fg",
                result_summary="no foreground app detected",
            ),
        ],
    )

    assert infer_session_identity(session) is None  # type: ignore[arg-type]


def test_infer_identity_skips_placeholder_values(tmp_path: Path) -> None:
    """占位身份既不作为结果返回，也不阻断更靠后的真实线索。"""
    placeholders_only = _FakeSession(
        tmp_path,
        [
            _foreground("com.example.app", ABILITY, invocation_id="inv-fg-placeholder"),
            _start_app(BUNDLE, "EntryAbility", invocation_id="inv-start-placeholder"),
        ],
    )

    assert infer_session_identity(placeholders_only) is None  # type: ignore[arg-type]

    with_real_evidence = _FakeSession(
        tmp_path,
        [
            _foreground("com.example.app", ABILITY, invocation_id="inv-fg-placeholder"),
            _start_app(BUNDLE, "EntryAbility", invocation_id="inv-start-placeholder"),
            _start_app(BUNDLE, ABILITY, invocation_id="inv-start-real"),
        ],
    )

    assert infer_session_identity(with_real_evidence) == (BUNDLE, ABILITY)  # type: ignore[arg-type]


def test_infer_identity_defers_foreground_placeholder_ability_to_prepare(tmp_path: Path) -> None:
    """优先级 1 只把 ``unknown``/空视为无效 ability（按规格）：占位 ability 仍原样返回。

    ``EntryAbility`` 属于 ``_PLACEHOLDER_ABILITIES``，因此这一步的结果仍会被
    ``prepare()`` 以「placeholder」422 拒绝——占位校验保持单点，不在推断层重复。
    """
    session = _FakeSession(tmp_path, [_foreground(BUNDLE, "EntryAbility")])

    assert infer_session_identity(session) == (BUNDLE, "EntryAbility")  # type: ignore[arg-type]


def test_resolve_identity_prefers_explicit_params_then_infers(tmp_path: Path) -> None:
    """显式参数原样返回（占位校验交给 prepare）；缺省时回落到会话录制推断。"""
    session = _FakeSession(tmp_path, [_foreground(BUNDLE, ABILITY)])

    assert resolve_distill_identity(session, "com.other.app", "OtherAbility") == (  # type: ignore[arg-type]
        "com.other.app",
        "OtherAbility",
    )
    assert resolve_distill_identity(session, "com.example.app", "EntryAbility") == (  # type: ignore[arg-type]
        "com.example.app",
        "EntryAbility",
    )
    assert resolve_distill_identity(session, None, None) == (BUNDLE, ABILITY)  # type: ignore[arg-type]
    assert resolve_distill_identity(session, "", "") == (BUNDLE, ABILITY)  # type: ignore[arg-type]

    no_evidence = _FakeSession(tmp_path, [])
    assert resolve_distill_identity(no_evidence, None, None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 完整蒸馏（假验证器 + 假回放）
# ---------------------------------------------------------------------------


class _FakeVerification:
    """最小可用验证结果：准入轨迹只需 rounds[].snapshots 与 passed。"""

    def __init__(self, *, passed: bool) -> None:
        from harmony_test_agent.discovery import VerificationRound

        self.passed = passed
        self.failures = [] if passed else ["device verification round failed"]
        self.rounds = [
            VerificationRound(
                round_number=1,
                passed=passed,
                snapshot_ids=["snap-1"],
                snapshots=[_snapshot("snap-1", 1)],
                visited_page_signatures=["pages/Page1", "pages/Page2", "pages/Page3"],
                recovery_passed=passed,
            )
        ]
        self.stability = None


def _fake_registry(tmp_path: Path, *, attempts: int = 1) -> ProfileRegistry:
    return ProfileRegistry(
        tmp_path / "profiles",
        promotion_replay_attempts=attempts,
        min_evidence_rounds=1,
    )


def test_distill_promotes_to_verified_with_single_round_and_replay(tmp_path: Path, monkeypatch) -> None:
    """端到端（假设备）：draft → candidate → verified，并记录 1 个 replay ID。"""
    session = _three_page_session(tmp_path)
    registry = _fake_registry(tmp_path)
    distiller = _distiller(tmp_path, registry=registry)

    verification = _FakeVerification(passed=True)
    monkeypatch.setattr(
        distiller,
        "_candidate_from_verification",
        lambda prepared, result: _candidate_profile(prepared),
    )

    class _Verifier:
        def verify(self, discovery):
            return verification

    distiller.verifier_factory = lambda target, output_dir, run_id: _Verifier()  # type: ignore[assignment]

    class _PassingRunner:
        def execute(self, generated, attempt: int) -> ReplayResult:
            return ReplayResult(attempt=attempt, command=CommandResult(command="hypium", returncode=0), passed=True)

    distiller.runner = _PassingRunner()  # type: ignore[assignment]

    import asyncio

    result = asyncio.run(distiller.distill(session, BUNDLE, ABILITY))  # type: ignore[arg-type]

    assert result.status == "verified"
    assert result.replay_passed is True
    assert result.replay_run_id == f"{session.session_id}:profile-attempt-1"
    stored = registry.read(f"dc-{session.session_id}", ProfileStatus.VERIFIED)
    assert stored.provenance.hypium_replay_run_ids == [result.replay_run_id]


def test_distill_keeps_invalid_draft_when_verification_fails(tmp_path: Path, monkeypatch) -> None:
    session = _three_page_session(tmp_path)
    registry = _fake_registry(tmp_path)
    distiller = _distiller(tmp_path, registry=registry)

    class _Verifier:
        def verify(self, discovery):
            return _FakeVerification(passed=False)

    distiller.verifier_factory = lambda target, output_dir, run_id: _Verifier()  # type: ignore[assignment]

    import asyncio

    result = asyncio.run(distiller.distill(session, BUNDLE, ABILITY))  # type: ignore[arg-type]

    assert result.status == "draft"
    assert any("device verification" in warning for warning in result.warnings)
    invalid = registry.read(f"dc-{session.session_id}", ProfileStatus.INVALID)
    assert invalid.status == ProfileStatus.INVALID
    assert registry.get(target_app_id=f"dc-{session.session_id}", status=ProfileStatus.VERIFIED) is None


def test_distill_without_registry_raises(tmp_path: Path) -> None:
    session = _three_page_session(tmp_path)
    distiller = _distiller(tmp_path, registry=None)

    import asyncio

    with pytest.raises(DcError, match="registry is unavailable"):
        asyncio.run(distiller.distill(session, BUNDLE, ABILITY))  # type: ignore[arg-type]


def test_distill_without_verifier_factory_raises(tmp_path: Path) -> None:
    session = _three_page_session(tmp_path)
    distiller = _distiller(tmp_path, registry=_fake_registry(tmp_path))

    import asyncio

    with pytest.raises(DcError, match="device verification is unavailable"):
        asyncio.run(distiller.distill(session, BUNDLE, ABILITY))  # type: ignore[arg-type]


def _candidate_profile(prepared: DistillPreparation):
    """构造一个满足准入门禁的 candidate（替代真实设备验证证据）。

    页签名必须取自 discovery 页面的**结构身份**：``AgentOrchestrator``
    的准入轨迹按 ``page.structural_identity`` 建立页面→定位器映射，
    用任意字符串会得到「0 页覆盖」。
    """
    from harmony_test_agent.models import AssertionDefinition, StableLocator, TargetAppProfile

    ordered_pages = sorted(prepared.discovery.pages, key=lambda item: len(item.path_actions))
    unique_identities: list[str] = []
    for page in ordered_pages:
        identity = page.structural_identity or page.signature
        if identity not in unique_identities:
            unique_identities.append(identity)
    identities = unique_identities[:3]
    return TargetAppProfile(
        status=ProfileStatus.CANDIDATE,
        target_app_id=prepared.target.target_app_id,
        display_name="DC Distilled",
        bundle_name=prepared.target.bundle_name,
        main_ability=prepared.target.main_ability,
        stable_locator_inventory=[
            StableLocator(
                name=f"locator-{index}",
                page_signature=identity,
                key=f"dc_key_{index}",
                observed_rounds=1,
                unique_match_rounds=1,
                evidence_snapshot_ids=[f"snap-{index}"],
            )
            for index, identity in enumerate(identities, 1)
        ],
        assertion_inventory=[
            AssertionDefinition(
                # 断言 target 必须与 '_profile_validation_trace' 的页面 key 一致：
                # 它按 page_signature 反查断言，键不匹配会得到「0 应用级断言」。
                name=f"assertion-{index}",
                kind="visible",
                target=identity,
                page_signature=identity,
                observed_rounds=1,
                evidence_snapshot_ids=[f"asnap-{index}"],
            )
            for index, identity in enumerate(identities[:2], 1)
        ],
        core_flows=[
            {
                "pages": identities,
                "steps": [],
                "interaction_types": ["click", "input"],
            }
        ],
        provenance={
            "discovery_run_id": prepared.run_id,
            "evidence": {"verification_passed": True},
        },
    )


def test_dc_session_is_importable_as_type_reference() -> None:
    """``distill`` 通过 TYPE_CHECKING 引用 DcSession，运行期不应有循环导入。"""
    assert DcSession is not None
