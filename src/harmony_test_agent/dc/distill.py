"""DC 会话 → Profile 资产蒸馏器（2026-09-17 重构，比赛亮点）。

输入源全部来自会话已落盘/内存产物：

- ``recorder.invocations``（含 ``resolved_element`` 的 key/id/text 与 ``page_path``）
- ``session.snapshots``（会话内采集过的帧，含 UI 元素与逻辑页路径）
- ``session.dir/screens/*.jpeg``（已 SHA256 去重的截图存档）

设计取舍（计划 §6.2）：

- **步骤 1-4 是纯 CPU + 内存**（页面覆盖校验、定位器/断言证据提取、稳定性分析），
  不接触设备，耗时 << 1s。
- **步骤 5-8 仍需重新驱动设备**：蒸馏出的 Profile 必须通过与 Live 流水线同一套
  设备端结构身份校验（1 轮 ``ProfileVerifier`` + 1 次 Hypium 回放），否则「verified」
  语义会被稀释。相比重构前的 3 轮 + 3 次，耗时从 8-15 分钟降至 <2 分钟。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import Settings
from ..discovery import BoundedExplorer, DiscoveryResult, ExplorationAction, ExplorationPolicy, ProfileVerifier
from ..discovery.stability import (
    AssertionObservation,
    LocatorObservation,
    StabilityAnalyzer,
    StabilityReport,
)
from ..generation import HypiumGenerator
from ..models import (
    LocatorKind,
    ProfileStatus,
    ResolvedTarget,
    RunTrace,
    StableLocator,
    TargetAppProfile,
)
from ..profiles import ProfileRegistry
from ..runner import HypiumRunner
from ..storage import ArtifactStore
from ..targets import ForegroundApp
from .models import (
    DcDistillResult,
    DcError,
    DcToolInvocation,
    DcToolName,
)

if TYPE_CHECKING:  # 仅类型注解：session ↔ distill 存在运行期循环依赖
    from .session import DcSession

logger = logging.getLogger(__name__)

# 蒸馏要求的最小页面覆盖（比赛硬性要求：≥3 个页面/核心流程）。
MIN_DISTILL_PAGES = 3

# 可转成可回放动作的 DC 工具（与 dc/generator.py::_REPLAYABLE_MAP 的可交互子集一致）。
_ACTION_TOOLS: frozenset[DcToolName] = frozenset(
    {
        DcToolName.CLICK,
        DcToolName.SWIPE,
        DcToolName.INPUT_TEXT,
        DcToolName.BACK,
        DcToolName.KEY_EVENT,
    }
)

_ASSERT_TOOLS: frozenset[DcToolName] = frozenset(
    {DcToolName.ASSERT_VISIBLE, DcToolName.ASSERT_NOT_VISIBLE, DcToolName.ASSERT_TEXT}
)

# 占位应用身份：与 dc/generator.py 同一约束，避免把示例值蒸馏成 Profile。
_PLACEHOLDER_BUNDLES = frozenset({"com.example.app", ""})
_PLACEHOLDER_ABILITIES = frozenset({"EntryAbility", ""})

# 蒸馏动作数量上限：超过后不再追加核心流（防止把整段长会话当作单条核心流回放）。
_MAX_CORE_ACTIONS = 24

# ``foreground_app`` 的结果摘要格式（见 ``dc/tools.py::tool_foreground_app``）。
_FOREGROUND_RE = re.compile(r"^bundle=(?P<bundle>\S+),\s*ability=(?P<ability>\S+)$")

# 设备未上报 ability 时工具层输出的字面占位值（``tool_foreground_app`` 的兜底）。
_UNKNOWN_ABILITY = "unknown"


def infer_session_identity(session: DcSession) -> tuple[str, str] | None:
    """从会话录制记录推断 ``(bundle_name, main_ability)``。

    优先级（先命中先返回）：

    1. 最近一次成功 ``foreground_app`` 的 ``result_summary``：bundle 非占位，
       且 ability 非空、非 ``unknown``（后者是设备未上报 ability 时的兜底值）；
    2. 最近一次成功 ``start_app`` 的 ``args``：``bundle_name`` / ``ability_name`` 均非占位；
    3. ``session.last_foreground_app`` 作 bundle，配同 bundle 的任一成功 ``start_app``
       的 ``ability_name``（非占位）；
    4. 均不满足返回 ``None``。

    返回值只做「可推断」判断，占位校验仍由 :meth:`DcProfileDistiller.prepare` 统一负责。
    """
    invocations = list(session.recorder.invocations)

    for invocation in reversed(invocations):
        if invocation.tool != DcToolName.FOREGROUND_APP or not invocation.success:
            continue
        match = _FOREGROUND_RE.match(invocation.result_summary.strip())
        if match is None:
            continue
        bundle = match.group("bundle")
        ability = match.group("ability")
        if bundle in _PLACEHOLDER_BUNDLES or ability in {"", _UNKNOWN_ABILITY}:
            continue
        return bundle, ability

    for invocation in reversed(invocations):
        if invocation.tool != DcToolName.START_APP or not invocation.success:
            continue
        bundle = str(invocation.args.get("bundle_name") or "")
        ability = str(invocation.args.get("ability_name") or "")
        if bundle in _PLACEHOLDER_BUNDLES or ability in _PLACEHOLDER_ABILITIES:
            continue
        return bundle, ability

    bundle = session.last_foreground_app
    if bundle and bundle not in _PLACEHOLDER_BUNDLES:
        for invocation in reversed(invocations):
            if invocation.tool != DcToolName.START_APP or not invocation.success:
                continue
            if str(invocation.args.get("bundle_name") or "") != bundle:
                continue
            ability = str(invocation.args.get("ability_name") or "")
            if ability in _PLACEHOLDER_ABILITIES:
                continue
            return bundle, ability

    return None


def resolve_distill_identity(
    session: DcSession,
    bundle_name: str | None,
    main_ability: str | None,
) -> tuple[str, str] | None:
    """解析蒸馏身份：显式参数（非空）原样返回，否则回落到会话录制推断。

    显式值原样返回（不在此处做占位校验）：调用方传入 ``com.example.app`` 这类占位身份时，
    仍应由 ``DcProfileDistiller.prepare`` 抛出「placeholder」错误，保持既有 422 文案不变。
    """
    if bundle_name and main_ability:
        return bundle_name, main_ability
    return infer_session_identity(session)


@dataclass(slots=True)
class DistillPreparation:
    """纯 CPU 蒸馏阶段的中间结果（不接触设备，便于单测与审计）。"""

    run_id: str
    target: ResolvedTarget
    discovery: DiscoveryResult
    draft: TargetAppProfile
    stability: StabilityReport
    page_paths: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class DcProfileDistiller:
    """把一次 DC 会话蒸馏为 Profile 资产。

    依赖注入说明：``verifier_factory`` 与 ``runner`` 由调用方（``DcSession``）提供，
    使本类可以在没有真实设备时用假实现完整测试纯 CPU 部分与状态流转。
    """

    def __init__(
        self,
        artifacts: ArtifactStore,
        registry: ProfileRegistry | None,
        settings: Settings,
        *,
        verifier_factory: Callable[[ResolvedTarget, Path, str], ProfileVerifier] | None = None,
        runner: HypiumRunner | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.registry = registry
        self.settings = settings
        self.verifier_factory = verifier_factory
        self.runner = runner

    # ------------------------------------------------------------------
    # 纯 CPU 阶段（步骤 1-4）
    # ------------------------------------------------------------------

    def prepare(
        self,
        session: DcSession,
        bundle_name: str,
        main_ability: str,
    ) -> DistillPreparation:
        """校验页面覆盖并提取定位器/断言证据，产出 draft Profile。

        Raises:
            DcError: 应用身份为占位值、页面覆盖 < 3、无「3 个可回放动作」的核心流。
        """
        if bundle_name in _PLACEHOLDER_BUNDLES or main_ability in _PLACEHOLDER_ABILITIES:
            raise DcError("bundle_name/main_ability must be the real application identity, not the placeholder")

        invocations = [inv for inv in session.recorder.invocations if inv.success]
        page_paths = sorted({inv.page_path for inv in invocations if inv.page_path})
        if len(page_paths) < MIN_DISTILL_PAGES:
            raise DcError(
                f"DC session covers only {len(page_paths)} pages; need >= {MIN_DISTILL_PAGES} to distill a Profile"
            )

        run_id = session.session_id
        target = self._resolved_target(session, bundle_name, main_ability)
        warnings: list[str] = []

        locator_observations = self._locator_observations(invocations)
        assertion_observations = self._assertion_observations(invocations)

        analyzer = StabilityAnalyzer(required_rounds=self.settings.profile_verification_rounds)
        stability = analyzer.analyze(locator_observations, assertion_observations)

        discovery = self._discovery_result(session, target, invocations, warnings)
        if not discovery.pages:
            raise DcError("DC session has no replayable core flow; record at least 3 replayable actions first")

        draft = self._draft_profile(
            session=session,
            target=target,
            stability=stability,
            page_paths=page_paths,
            warnings=warnings,
        )
        return DistillPreparation(
            run_id=run_id,
            target=target,
            discovery=discovery,
            draft=draft,
            stability=stability,
            page_paths=page_paths,
            warnings=warnings,
        )

    def _locator_observations(self, invocations: list[DcToolInvocation]) -> list[LocatorObservation]:
        """从 ``resolved_element`` 提取定位器证据（单轮，round_number=1）。"""
        observations: list[LocatorObservation] = []
        for inv in invocations:
            element = inv.resolved_element
            if element is None or not (element.key or element.id):
                continue
            kind = LocatorKind.KEY if element.key else LocatorKind.ID
            value = element.key or element.id
            observations.append(
                LocatorObservation(
                    round_number=1,
                    page_signature=inv.page_path or element.element_id or "unknown",
                    kind=kind,
                    value=value,
                    element_type=element.type,
                    text=element.content,
                    match_count=1,
                    interactive=bool(element.clickable or element.editable or element.scrollable),
                    resolution=None,
                )
            )
        return observations

    def _assertion_observations(self, invocations: list[DcToolInvocation]) -> list[AssertionObservation]:
        """从成功的 ``assert_*`` 调用提取应用级断言证据（单轮）。"""
        observations: list[AssertionObservation] = []
        for inv in invocations:
            if inv.tool not in _ASSERT_TOOLS:
                continue
            target = str(inv.args.get("target") or inv.args.get("text") or "")
            if not target:
                continue
            observations.append(
                AssertionObservation(
                    round_number=1,
                    page_signature=inv.page_path or "unknown",
                    kind=inv.tool.value,
                    target=target,
                    passed=True,
                    app_level=True,
                )
            )
        return observations

    def _discovery_result(
        self,
        session: DcSession,
        target: ResolvedTarget,
        invocations: list[DcToolInvocation],
        warnings: list[str],
    ) -> DiscoveryResult:
        """把线性 DC 会话重建为可回放的 DiscoveryResult（页面 = 动作前缀状态）。

        验证器 ``_core_pages`` 依赖「页面的 path_actions 是更深路径的前缀」，线性会话
        天然满足：第 i 页的路径就是前 i 个动作。
        """
        foreground = ForegroundApp(
            bundle_name=target.bundle_name,
            ability_name=target.main_ability,
            window_type="main",
        )
        actions: list[ExplorationAction] = []
        pages = []
        snapshots = {snapshot.snapshot_id: snapshot for snapshot in session.snapshots}

        # 首页 = 任一动作之前的那个帧（取最早采集的帧）。
        ordered_snapshots = list(session.snapshots)
        if ordered_snapshots:
            pages.append(BoundedExplorer._page(ordered_snapshots[0], foreground, 1, []))

        for inv in invocations:
            if len(actions) >= _MAX_CORE_ACTIONS:
                warnings.append(f"core flow truncated at {_MAX_CORE_ACTIONS} actions")
                break
            action = self._action_from_invocation(inv, warnings)
            if action is None:
                continue
            actions.append(action)
            snapshot = snapshots.get(inv.after_snapshot_id or "")
            if snapshot is not None:
                pages.append(BoundedExplorer._page(snapshot, foreground, len(actions) + 1, list(actions)))

        # 去重同一结构状态的重复页（例如点击后页面未变），保留最深的页面定义。
        deduped = []
        seen: set[str] = set()
        for page in sorted(pages, key=lambda item: len(item.path_actions)):
            if page.page_path and page.page_path in seen and not page.path_actions:
                continue
            seen.add(page.page_path)
            deduped.append(page)

        return DiscoveryResult(
            target=target,
            policy=ExplorationPolicy(enabled=True, temporary_test=False),
            pages=deduped,
            transitions=[],
            stop_reason="dc_session_distill",
        )

    def _action_from_invocation(
        self,
        inv: DcToolInvocation,
        warnings: list[str],
    ) -> ExplorationAction | None:
        """把一条 DC 调用转成可回放动作；不可回放时返回 None。"""
        if inv.tool not in _ACTION_TOOLS:
            return None
        if inv.tool == DcToolName.KEY_EVENT and str(inv.args.get("key", "")).lower() != "back":
            return None

        element = inv.resolved_element
        kind: str
        coordinate: tuple[int, int] | None = None
        locator_kind = "coordinate"
        locator_value = ""
        target_text = ""

        if inv.tool == DcToolName.CLICK:
            kind = "click"
            coordinate = _coordinate(inv.args.get("x"), inv.args.get("y"))
            if element is not None and (element.key or element.id):
                locator_kind = "key" if element.key else "id"
                locator_value = element.key or element.id
                target_text = element.content
        elif inv.tool == DcToolName.INPUT_TEXT:
            kind = "input"
            coordinate = _coordinate_from_pair(inv.args.get("coordinate"))
            if element is not None and (element.key or element.id):
                locator_kind = "key" if element.key else "id"
                locator_value = element.key or element.id
                target_text = element.content
        elif inv.tool == DcToolName.SWIPE:
            kind = "swipe"
            direction = str(inv.args.get("direction") or "").lower()
            if direction not in {"up", "down"}:
                direction = _infer_direction(inv.args)
        else:  # BACK / KEY_EVENT(back)
            kind = "back"

        if kind == "input" and coordinate is None:
            # 验证器按坐标聚焦输入框；缺坐标的输入动作无法确定性回放。
            if element is not None and element.bbox is not None:
                coordinate = element.bbox.center
            else:
                warnings.append(f"{inv.invocation_id}: input action skipped (no coordinate or element bbox)")
                return None

        action = ExplorationAction(
            action_id=inv.invocation_id,
            kind=kind,  # type: ignore[arg-type]
            element_id=element.element_id if element is not None else None,
            locator_kind=locator_kind,  # type: ignore[arg-type]
            locator_value=locator_value,
            target_text=target_text,
            coordinate=coordinate,
        )
        if kind == "swipe":
            action.direction = _infer_direction(inv.args)  # type: ignore[assignment]
        return action

    def _draft_profile(
        self,
        *,
        session: DcSession,
        target: ResolvedTarget,
        stability: StabilityReport,
        page_paths: list[str],
        warnings: list[str],
    ) -> TargetAppProfile:
        """由蒸馏证据构造 draft Profile（不含设备验证证据，因此不满足晋级门槛）。"""
        locators: list[StableLocator] = []
        for item in stability.locators:
            values = {"key": "", "id": "", "text": "", "type": ""}
            if item.kind == LocatorKind.TYPE_TEXT:
                values["type"], _, values["text"] = item.value.partition("|")
            elif item.kind in {LocatorKind.KEY, LocatorKind.ID, LocatorKind.TEXT}:
                values[item.kind.value] = item.value
            locators.append(
                StableLocator(
                    name=item.name,
                    page_signature=item.page_signatures[0] if item.page_signatures else "",
                    observed_rounds=len(item.rounds),
                    unique_match_rounds=len(item.rounds) if item.unique_each_round else 0,
                    confidence="high" if item.level == "high" else "medium",
                    **values,
                )
            )
        if len(locators) < 3:
            warnings.append(
                "distilled draft has fewer than three stable locators; device verification decides admission"
            )

        return TargetAppProfile(
            status=ProfileStatus.DRAFT,
            target_app_id=target.target_app_id,
            display_name=f"DC Distilled {target.display_name}",
            bundle_name=target.bundle_name,
            main_ability=target.main_ability,
            module_name=target.module_name,
            launch_strategy={
                "kind": "hdc_aa_start",
                "command_template": "aa start -b {bundle_name} -a {main_ability}",
            },
            reset_strategy={"kind": "stop_start_only", "clear_app_data": False, "recovery_actions": []},
            permission_and_popup_strategy={
                "login": "explicit_only",
                "permission": "explicit_only",
                "submit": "explicit_only",
                "publish": "explicit_only",
                "download": "explicit_only",
                "payment": "always_blocked",
                "delete": "always_blocked",
                "uninstall": "always_blocked",
                "clear_data": "always_blocked",
            },
            stable_locator_inventory=locators,
            assertion_inventory=[],
            core_flows=[
                {
                    "pages": list(page_paths)[:4],
                    "steps": [],
                    "interaction_types": sorted(
                        {inv.tool.value for inv in session.recorder.invocations if inv.success}
                    ),
                }
            ],
            provenance={
                "discovery_run_id": session.session_id,
                "evidence": {
                    "identity": "dc_session",
                    "verification_passed": False,
                    "distilled_from_dc_session": session.session_id,
                    "page_paths": list(page_paths),
                    "locator_evidence": len(locators),
                },
            },
        )

    @staticmethod
    def _resolved_target(session: DcSession, bundle_name: str, main_ability: str) -> ResolvedTarget:
        return ResolvedTarget(
            target_app_id=f"dc-{session.session_id}",
            display_name=bundle_name,
            bundle_name=bundle_name,
            main_ability=main_ability,
            device_id=session.device_id,
            source="explicit_override",
        )

    # ------------------------------------------------------------------
    # 完整蒸馏（步骤 5-8，需要设备）
    # ------------------------------------------------------------------

    async def distill(
        self,
        session: DcSession,
        bundle_name: str,
        main_ability: str,
    ) -> DcDistillResult:
        """执行完整蒸馏：draft → 1 轮设备验证 → candidate → 1 次回放 → verified。

        设备验证失败时保留 invalid draft（与 Live 流水线语义一致），不晋级 candidate。
        """
        if self.registry is None:
            raise DcError("Profile registry is unavailable; cannot distill a Profile")

        prepared = await asyncio.to_thread(self.prepare, session, bundle_name, main_ability)
        self.registry.save_draft(prepared.draft)
        self._write_json(session, "distill/draft-profile.json", prepared.draft)

        if self.verifier_factory is None:
            raise DcError("device verification is unavailable; cannot promote the distilled Profile")

        run_dir = session.dir
        verification_dir = run_dir / "verification"
        verifier = self.verifier_factory(prepared.target, verification_dir, prepared.run_id)
        verification = await asyncio.to_thread(verifier.verify, prepared.discovery)
        if not verification.passed:
            failed = prepared.draft.model_copy(update={"status": ProfileStatus.INVALID}, deep=True)
            self.registry.save_draft(failed)
            return DcDistillResult(
                profile_id=prepared.draft.target_app_id,
                status="draft",
                pages_covered=len(prepared.page_paths),
                stable_locators=len(prepared.draft.stable_locator_inventory),
                assertions=0,
                warnings=[*prepared.warnings, *verification.failures],
            )

        candidate = self._candidate_from_verification(prepared, verification)
        self.registry.save_candidate(candidate)
        self._write_json(session, "distill/candidate-profile.json", candidate)

        replay = await self._run_admission_replay(session, candidate, prepared, verification)
        replay_id = f"{prepared.run_id}:profile-attempt-{replay.attempt}"
        if not replay.passed:
            return DcDistillResult(
                profile_id=candidate.target_app_id,
                status="candidate",
                pages_covered=len(prepared.page_paths),
                stable_locators=len(candidate.stable_locator_inventory),
                assertions=len(candidate.assertion_inventory),
                replay_run_id=replay_id,
                replay_passed=False,
                warnings=[*prepared.warnings, "Hypium admission replay failed; Profile stays candidate"],
            )

        self.registry.promote(candidate.target_app_id, replay_run_ids=[replay_id])
        promoted = self.registry.read(candidate.target_app_id, ProfileStatus.VERIFIED)
        return DcDistillResult(
            profile_id=promoted.target_app_id,
            status="verified",
            pages_covered=len(prepared.page_paths),
            stable_locators=len(promoted.stable_locator_inventory),
            assertions=len(promoted.assertion_inventory),
            replay_run_id=replay_id,
            replay_passed=True,
            warnings=list(prepared.warnings),
        )

    def _candidate_from_verification(self, prepared: DistillPreparation, verification) -> TargetAppProfile:
        """用设备验证证据重建 candidate（复用 Live 流水线的 Profile 构造）。"""
        from ..agents import AgentOrchestrator

        candidate = AgentOrchestrator._build_profile(
            prepared.target,
            prepared.discovery,
            verification,
            prepared.run_id,
        )
        evidence = dict(candidate.provenance.evidence)
        evidence["distilled_from_dc_session"] = prepared.run_id
        return candidate.model_copy(
            update={
                "status": ProfileStatus.CANDIDATE,
                "provenance": candidate.provenance.model_copy(update={"evidence": evidence}, deep=True),
            },
            deep=True,
        )

    async def _run_admission_replay(self, session: DcSession, candidate: TargetAppProfile, prepared, verification):
        """生成门禁脚本并执行 1 次 Hypium 回放，同时把脚本路径写回 candidate。"""
        from ..agents import AgentOrchestrator
        from ..models import GeneratedArtifact

        host_trace = RunTrace(
            run_id=prepared.run_id,
            target_app_id=candidate.target_app_id,
            task="DC distilled Profile admission",
            device_id=session.device_id,
            profile_snapshot=candidate,
            resolved_target=prepared.target.model_copy(update={"profile_snapshot": candidate}, deep=True),
            exploration_policy=prepared.discovery.policy,
            agent_outcome="completed",
        )
        validation_trace = AgentOrchestrator._profile_validation_trace(
            host_trace, candidate, prepared.discovery, verification
        )
        generator = HypiumGenerator(self.artifacts, min_observed_rounds=self.settings.profile_verification_rounds)
        generated: GeneratedArtifact = await asyncio.to_thread(generator.generate, validation_trace, candidate)

        updated = candidate.model_copy(
            update={
                "provenance": candidate.provenance.model_copy(
                    update={"generated_script_path": str(generated.python_path)}, deep=True
                )
            },
            deep=True,
        )
        self.registry.save_candidate(updated)

        runner = self.runner or HypiumRunner(self.settings.resolved_runtime_home)
        return await asyncio.to_thread(runner.execute, generated, 1)

    def _write_json(self, session: DcSession, relative: str, payload) -> None:
        path = session.dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _coordinate(x: object, y: object) -> tuple[int, int] | None:
    if isinstance(x, int) and isinstance(y, int):
        return (x, y)
    return None


def _coordinate_from_pair(value: object) -> tuple[int, int] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        x, y = value
        if isinstance(x, int) and isinstance(y, int):
            return (x, y)
    return None


def _infer_direction(args: dict[str, object]) -> str:
    """由起止坐标推断滑动方向（仅 up/down，与验证器的滑词语义一致）。"""
    start = _coordinate_from_pair(args.get("start"))
    end = _coordinate_from_pair(args.get("end"))
    if start is None or end is None:
        return "up"
    return "down" if end[1] > start[1] else "up"


__all__ = [
    "DistillPreparation",
    "DcProfileDistiller",
    "MIN_DISTILL_PAGES",
    "infer_session_identity",
    "resolve_distill_identity",
]
