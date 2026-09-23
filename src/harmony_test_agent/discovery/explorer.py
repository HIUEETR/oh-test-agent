"""Bounded, auditable exploration of a resolved HarmonyOS application.

INTERNAL CAPABILITY (2026-09-17 重构后): 不再通过 CLI/API/Web 直接暴露。

消费方：
- agents/orchestrator.py（资产流水线状态机内部调用）
- dc/distill.py::DcProfileDistiller（DC 会话蒸馏 Profile 时复用结构身份重建核心流）
- dc/tools.py（复用 ``_structural_identity``/``_page`` 等静态能力）

禁止从 cli.py 或 web/ 反向依赖本模块。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from ..devices import DeviceAdapter, DeviceError
from ..models import AnomalyFinding, CommandResult, ExplorationPolicy, ScreenSnapshot, UIElement
from ..perception.normalizer import normalize_layout, page_path
from ..perception.volatility import (
    _CONTENT_LIKE_KEY_ID_PATTERN,
    is_volatile_evidence_key,
    is_volatile_structural_key,
)
from ..runtime.safety import SafetyPolicy
from ..targets import ForegroundApp, ResolvedTarget
from .advisor import AdvisorTurnRecord

if TYPE_CHECKING:
    from .advisor import AdvisorVerdict, ExplorationAdvisor

_LOG = logging.getLogger(__name__)

# 显式导出：只有这些符号是 orchestration / dc 蒸馏依赖的公开能力，
# 其余模块级辅助函数与私有方法视为实现细节（2026-09-17 重构 §8.7）。
__all__ = [
    "ActionRisk",
    "ActionRiskClassifier",
    "BoundedExplorer",
    "DiscoveryPage",
    "DiscoveryResult",
    "DiscoveryTransition",
    "ExplorationAction",
    "is_volatile_evidence_key",
    "is_volatile_structural_key",
]


class ActionRisk(StrEnum):
    """Deterministic action risk tiers enforced before device execution."""

    DEFAULT_ALLOWED = "default_allowed"
    REQUIRES_OPT_IN = "requires_opt_in"
    FORBIDDEN = "forbidden"
    BLOCKED_UNCERTAIN = "blocked_uncertain"


class ExplorationAction(BaseModel):
    """One proposed action with a stable identity and precomputed risk."""

    action_id: str
    kind: Literal["click", "input", "swipe", "back"]
    element_id: str | None = None
    locator_kind: Literal["key", "id", "text", "type_text", "coordinate"] = "coordinate"
    locator_value: str = ""
    target_text: str = ""
    coordinate: tuple[int, int] | None = None
    direction: Literal["up", "down"] | None = None
    risk: ActionRisk = ActionRisk.DEFAULT_ALLOWED
    risk_reason: str = ""
    required_permission: str | None = None
    content_like: bool = False


class DiscoveryPage(BaseModel):
    """A deduplicated page in the exploration graph."""

    page_id: str
    signature: str
    structural_identity: str = ""
    # 结构身份的组成部分（折叠 key 集与可交互结构集）：验证首页身份子集匹配使用；
    # 旧探索结果缺省为空，验证器据此回退严格全等校验。
    identity_keys: list[str] = Field(default_factory=list)
    identity_interactive: list[str] = Field(default_factory=list)
    # 因时间/日期/内容实例/列表实例等易变特征被剔除、未进入结构身份的原始 key。
    # 报告用：解释「为什么这些 key 没进身份」（计划 2.2，additive）。
    volatile_keys: list[str] = Field(default_factory=list)
    # 单页应用状态分类：page（真正的独立页面）/ tab_state（同页不同 Tab 选中态）/ dialog（弹窗态）。
    # 只作报告与诊断元数据，不参与身份哈希或门禁判定（计划 2.3，additive）。
    state_kind: Literal["page", "tab_state", "dialog"] = "page"
    page_path: str
    bundle_name: str
    ability_name: str | None = None
    window_type: str | None = None
    snapshot_id: str
    image_path: Path
    hierarchy_path: Path | None = None
    element_count: int
    discovered_order: int
    path_actions: list[ExplorationAction] = Field(default_factory=list)


class DiscoveryTransition(BaseModel):
    """Auditable before/after result for an explored action."""

    source_page_id: str
    target_page_id: str | None = None
    action: ExplorationAction
    before_snapshot_id: str
    after_snapshot_id: str | None = None
    foreground_before: ForegroundApp | None = None
    foreground_after: ForegroundApp | None = None
    command: CommandResult | None = None
    success: bool = False
    blocked_reason: str | None = None
    elapsed_ms: int = 0
    replayable: bool = True


class DiscoveryResult(BaseModel):
    """Complete bounded exploration output suitable for draft Profile creation."""

    target: ResolvedTarget
    policy: ExplorationPolicy
    pages: list[DiscoveryPage] = Field(default_factory=list)
    transitions: list[DiscoveryTransition] = Field(default_factory=list)
    blocked_actions: list[ExplorationAction] = Field(default_factory=list)
    advisor_turns: int = 0
    advisor_verdicts: list[dict[str, object]] = Field(default_factory=list)
    advisor_log: list[AdvisorTurnRecord] = Field(default_factory=list)
    started_at_monotonic: float = Field(exclude=True, default=0)
    duration_seconds: float = 0
    stop_reason: str = "queue_exhausted"
    anomalies: list[AnomalyFinding] = Field(default_factory=list)
    """探索期发现的异常（additive，Phase 2）：跨 bundle 恢复前的崩溃探测结果。

    落进 ``discovery/summary.json``：应用崩溃弹回桌面与「点了跳到别的应用的链接」在现象上
    完全相同，历史上被统一记成 ``cross-bundle navigation blocked`` 并静默恢复，一次真崩溃
    因此被当成无事发生。"""

    @property
    def interaction_types(self) -> set[str]:
        return {item.action.kind for item in self.transitions if item.success}


@dataclass(slots=True)
class ActionRiskClassifier:
    """Classify actions from deterministic UI text and identifiers."""

    def classify(self, text: str, policy: ExplorationPolicy) -> tuple[ActionRisk, str, str | None]:
        """Use the runtime safety vocabulary and preserve explicit risk categories."""
        runtime = SafetyPolicy(policy)
        value = _normalize_text(text)
        permanent = next(
            (term for term in runtime.always_blocked if term.casefold() in value),
            None,
        )
        if permanent:
            return ActionRisk.FORBIDDEN, f"permanently forbidden operation: {permanent}", None
        credential_terms = (
            "密码",
            "验证码",
            "password",
            "captcha",
            "otp",
            "passcode",
            "pin",
        )
        if any(term in value for term in credential_terms):
            return (
                ActionRisk.BLOCKED_UNCERTAIN,
                "credential input requires an explicit secret reference",
                None,
            )
        permission_names = {
            "allow_login": "login",
            "allow_permission": "permission",
            "allow_submit": "submit",
            "allow_publish": "publish",
            "allow_download": "download",
        }
        for setting, terms in (runtime.gated_terms or {}).items():
            if any(term.casefold() in value for term in terms):
                permission = permission_names[setting]
                if bool(getattr(policy, setting)):
                    return ActionRisk.DEFAULT_ALLOWED, f"explicitly allowed: {permission}", permission
                return ActionRisk.REQUIRES_OPT_IN, f"requires {setting}", permission
        uncertain = (
            "密码",
            "验证码",
            "password",
            "captcha",
            "otp",
            "充值",
            "转账",
            "transfer",
            "同意",
            "接受",
            "开启",
            "启用",
            "继续",
            "allow",
            "accept",
            "enable",
            "continue",
        )
        if any(term in value for term in uncertain):
            return ActionRisk.BLOCKED_UNCERTAIN, "sensitive intent is uncertain", None
        return ActionRisk.DEFAULT_ALLOWED, "ordinary UI interaction", None


DiscoveryProgress = Callable[[str, dict[str, object]], None]
SnapshotListener = Callable[[ScreenSnapshot], None]

_LOADING_TEXT_PATTERN = re.compile(r"正在加载|加载中|加载更多|loading|refreshing|请稍候|请等待", re.IGNORECASE)
_TIME_TEXT_PATTERN = re.compile(r"^\d{1,2}[:：]\d{2}([:：]\d{2})?$")
_DIGIT_PUNCT_TEXT_PATTERN = re.compile(r"^[\d\s:：.，,。、%/+-]+$")
_STRUCTURAL_KEY_ID_PATTERN = re.compile(r"\d{4,}")
_IDENTITY_KEY_ID_PATTERN = re.compile(r"\d+")
_CONTENT_LIKE_TEXT_LIMIT = 40
# 弹窗/遮罩容器与 Tab 容器 key：单页应用状态分类使用（计划 2.3，仅作报告元数据）。
# 刻意不含 menu/toast：菜单按钮与提示条是普通页面元素，会造成大量误判。
_DIALOG_KEY_PATTERN = re.compile(r"(?i)(dialog|popup|overlay|mask|sheet|alert)")
_TAB_KEY_PATTERN = re.compile(r"(?i)(^|_)(tab|tabs|tabbar|tabcontent|tab_item)(_|$)")

# 「什么算易变」的三个不同宽度判定已抽到 ``perception/volatility.py``（唯一归属地）：
# 页面身份 / 证据回收 / 脚本定位器三处代价不对称，必须用三个函数（见该模块 docstring
# 里的真机误杀反例）。这里 import 它们只是 re-export，``__all__`` 已列出，
# 既有 ``discovery`` 公开导出面与 ``tests/unit/test_discovery.py`` 的钉法都不变。


class BoundedExplorer:
    """Explore safe UI candidates within page, action, duration, and bundle boundaries."""

    def __init__(
        self,
        device: DeviceAdapter,
        target: ResolvedTarget,
        output_dir: Path,
        run_id: str,
        policy: ExplorationPolicy | None = None,
        classifier: ActionRiskClassifier | None = None,
        progress: DiscoveryProgress | None = None,
        should_stop: Callable[[], bool] | None = None,
        advisor: ExplorationAdvisor | None = None,
        on_snapshot: SnapshotListener | None = None,
    ) -> None:
        self.device = device
        self.target = target
        self.output_dir = output_dir
        self.run_id = run_id
        self.policy = policy or ExplorationPolicy()
        self.classifier = classifier or ActionRiskClassifier()
        self.progress = progress
        self.should_stop = should_stop or (lambda: False)
        self.advisor = advisor
        self.on_snapshot = on_snapshot
        # 结构身份 -> (建议, 来源, 建议时的候选摘要)；候选摘要用于把编号建议映射回可读控件。
        self._advisor_verdicts: dict[str, tuple[AdvisorVerdict, str, list[dict[str, object]]]] = {}
        self._advisor_summaries: list[str] = []
        # 探索期异常（Phase 2）：崩回桌面不再静默，收进 DiscoveryResult.anomalies。
        self._anomalies: list[AnomalyFinding] = []

    def explore(self) -> DiscoveryResult:
        result = DiscoveryResult(target=self.target, policy=self.policy, started_at_monotonic=time.monotonic())
        installed_apps_raw = self.output_dir / "installed-apps.raw.txt"
        installed_apps_raw.parent.mkdir(parents=True, exist_ok=True)
        installed_apps = self.device.list_installed_apps()
        catalog_raw = str(getattr(self.device, "last_catalog_raw", ""))
        installed_apps_raw.write_text(
            catalog_raw + "\n\n" + "\n\n".join(app.raw_output for app in installed_apps if app.raw_output),
            encoding="utf-8",
        )
        if not self.policy.enabled:
            result.stop_reason = "disabled"
            self._save(result)
            return result
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stopped = self.device.stop_app(self.target.bundle_name)
        if not stopped.ok:
            raise DeviceError(f"force-stop failed: {stopped.stderr or stopped.stdout}")
        started = self.device.start_app(self.target.bundle_name, self.target.main_ability, self.target.module_name)
        if not started.ok:
            raise DeviceError(f"aa start failed: {started.stderr or started.stdout}")

        visited_actions: set[tuple[str, str]] = set()
        pages_by_identity: dict[str, DiscoveryPage] = {}
        snapshot = self._capture_settled("discovery-000")
        foreground = self._assert_target_foreground()
        queue: list[tuple[ScreenSnapshot, ForegroundApp, list[ExplorationAction]]] = [(snapshot, foreground, [])]

        while queue:
            if self.should_stop():
                result.stop_reason = "stopped_by_user"
                break
            if time.monotonic() - result.started_at_monotonic >= self.policy.max_duration_seconds:
                result.stop_reason = "duration_limit"
                break
            current_snapshot, current_foreground, current_path = queue.pop(0)
            if current_path:
                # 空路径即启动后的首页快照，无需恢复；非空路径冷启动重放以验证可回放性。
                try:
                    current_snapshot, current_foreground = self._restore_path(
                        current_path,
                        current_snapshot,
                        current_foreground,
                        len(result.transitions),
                    )
                except DeviceError as exc:
                    self._progress("page_unreachable", {"path_length": len(current_path), "error": str(exc)})
                    continue
            source_identity = self._structural_identity(current_snapshot, current_foreground)
            page = pages_by_identity.get(source_identity)
            if page is None:
                if len(pages_by_identity) >= self.policy.max_pages:
                    result.stop_reason = "page_limit"
                    break
                page = self._page(
                    current_snapshot,
                    current_foreground,
                    len(pages_by_identity) + 1,
                    current_path,
                    structural_identity=source_identity,
                )
                pages_by_identity[source_identity] = page
                result.pages.append(page)

            candidates = self._select_candidates(current_snapshot)
            verdict, advisor_source = self._advise_page(page, current_snapshot, candidates)
            if verdict is not None:
                candidates = self._apply_advisor(candidates, verdict)
            for action in candidates:
                key = (page.structural_identity or page.signature, action.action_id)
                if key in visited_actions:
                    continue
                visited_actions.add(key)
                if action.risk != ActionRisk.DEFAULT_ALLOWED:
                    result.blocked_actions.append(action)
                    self._progress("blocked", {"page_id": page.page_id, "action": action.model_dump(mode="json")})
                    result.transitions.append(
                        DiscoveryTransition(
                            source_page_id=page.page_id,
                            action=action,
                            before_snapshot_id=current_snapshot.snapshot_id,
                            foreground_before=current_foreground,
                            blocked_reason=action.risk_reason,
                        )
                    )
                    continue
                if time.monotonic() - result.started_at_monotonic >= self.policy.max_duration_seconds:
                    result.stop_reason = "duration_limit"
                    break
                if self.should_stop():
                    result.stop_reason = "stopped_by_user"
                    break
                try:
                    action = self.resolve_replay_action(action, current_snapshot)
                except DeviceError as exc:
                    result.transitions.append(
                        DiscoveryTransition(
                            source_page_id=page.page_id,
                            action=action,
                            before_snapshot_id=current_snapshot.snapshot_id,
                            foreground_before=current_foreground,
                            blocked_reason=f"candidate locator no longer resolves: {exc}",
                            replayable=False,
                        )
                    )
                    self._progress(
                        "stale_candidate",
                        {"page_id": page.page_id, "action_id": action.action_id, "error": str(exc)},
                    )
                    continue
                transition, after = self._perform(
                    page, current_snapshot, current_foreground, action, len(result.transitions)
                )
                transition.replayable = not (action.kind == "click" and action.content_like)
                result.transitions.append(transition)
                if transition.blocked_reason and "cross-bundle" in transition.blocked_reason:
                    result.blocked_actions.append(action)
                    self._progress(
                        "blocked",
                        {
                            "page_id": page.page_id,
                            "action": action.model_dump(mode="json"),
                            "reason": transition.blocked_reason,
                        },
                    )
                self._progress(
                    "progress",
                    {
                        "pages": len(result.pages),
                        "transitions": len(result.transitions),
                        "interaction_types": sorted(result.interaction_types),
                        "transition": transition.model_dump(mode="json"),
                    },
                )
                if after is not None and transition.foreground_after:
                    after_identity = self._structural_identity(after, transition.foreground_after)
                    known = pages_by_identity.get(after_identity)
                    if known is not None:
                        transition.target_page_id = known.page_id
                    elif len(pages_by_identity) < self.policy.max_pages and not (
                        action.kind == "click" and action.content_like
                    ):
                        after_page = self._page(
                            after,
                            transition.foreground_after,
                            len(pages_by_identity) + 1,
                            [*current_path, action],
                            structural_identity=after_identity,
                        )
                        pages_by_identity[after_identity] = after_page
                        result.pages.append(after_page)
                        transition.target_page_id = after_page.page_id
                        queue.append((after, transition.foreground_after, [*current_path, action]))
                try:
                    current_snapshot, current_foreground = self._recover_to_source(
                        current_path,
                        source_identity,
                        current_snapshot,
                        current_foreground,
                        action,
                        after,
                        transition,
                        len(result.transitions),
                    )
                except DeviceError as exc:
                    self._progress("source_unreachable", {"page_id": page.page_id, "error": str(exc)})
                    break
            if self._early_success(result, self.policy.min_interaction_kinds):
                result.stop_reason = "admission_metrics_reached"
                break

        result.duration_seconds = round(time.monotonic() - result.started_at_monotonic, 3)
        result.anomalies = list(self._anomalies)
        result.advisor_turns = self.advisor.turn_count if self.advisor else 0
        result.advisor_verdicts = [
            {"identity": identity, "source": source, "candidates": digest, **verdict.model_dump()}
            for identity, (verdict, source, digest) in self._advisor_verdicts.items()
        ]
        if self.advisor:
            result.advisor_log = list(self.advisor.turns)
        self._save(result)
        self._save_observations(result)
        self._progress("finished", {"stop_reason": result.stop_reason, "pages": len(result.pages)})
        return result

    def candidate_actions(self, snapshot: ScreenSnapshot) -> list[ExplorationAction]:
        """Rank hierarchy-backed actions; content-like clicks drop below input/swipe."""
        ranked: list[tuple[int, ExplorationAction]] = []
        for element in snapshot.elements:
            text = " ".join(filter(None, (element.content, element.description, element.key, element.id, element.type)))
            risk, reason, permission = self.classifier.classify(text, self.policy)
            stable = bool(element.key or element.id)
            content_like = self._is_content_like(element)
            if element.clickable and element.bbox:
                rank = 4 if content_like else (0 if stable else 1)
                ranked.append((rank, self._action("click", element, text, risk, reason, permission, content_like)))
            if element.editable and element.bbox:
                ranked.append((2, self._action("input", element, text, risk, reason, permission)))
            if element.scrollable:
                ranked.append((3, self._action("swipe", element, text, risk, reason, permission)))
        unique: dict[str, tuple[int, ExplorationAction]] = {}
        for item in ranked:
            unique.setdefault(item[1].action_id, item)
        return [item[1] for item in sorted(unique.values(), key=lambda item: (item[0], item[1].action_id))]

    def _select_candidates(self, snapshot: ScreenSnapshot) -> list[ExplorationAction]:
        """Type-balanced per-page budget so input/swipe survive on click-rich pages."""
        ranked = self.candidate_actions(snapshot)
        budget = self.policy.max_actions_per_page
        inputs = [item for item in ranked if item.kind == "input"][:1]
        swipes = [item for item in ranked if item.kind == "swipe"][:1]
        clicks = [item for item in ranked if item.kind == "click"]
        click_budget = max(budget - len(inputs) - len(swipes), 1)
        selected = [*inputs, *swipes, *clicks[:click_budget]]
        order = {item.action_id: index for index, item in enumerate(ranked)}
        return sorted(selected, key=lambda item: order[item.action_id])[:budget]

    @staticmethod
    def _is_content_like(element: UIElement) -> bool:
        """信息流卡片/内容实例的 key 内嵌长数字 ID 或标题超长，主动点击只会进入不可回放的内容页。"""
        identity_text = " ".join(filter(None, (element.key, element.id)))
        if _CONTENT_LIKE_KEY_ID_PATTERN.search(identity_text):
            return True
        return bool(element.content and len(element.content) > _CONTENT_LIKE_TEXT_LIMIT)

    def _advise_page(
        self,
        page: DiscoveryPage,
        snapshot: ScreenSnapshot,
        candidates: list[ExplorationAction],
    ) -> tuple[AdvisorVerdict | None, str]:
        """为逻辑页请求顾问建议；同一结构身份复用既有建议，不再追加对话轮次。"""
        if self.advisor is None:
            return None, "heuristic"
        known = self._advisor_verdicts.get(page.structural_identity)
        if known is not None:
            verdict, source, digest = known
            self._progress(
                "advisor",
                {
                    "page_id": page.page_id,
                    "source": "reuse",
                    "turns": self.advisor.turn_count,
                    "verdict": verdict.model_dump(),
                    "candidates": digest,
                },
            )
            return verdict, "reuse"
        turns_before = len(self.advisor.turns)
        verdict, source = self.advisor.advise(snapshot, candidates, self._advisor_context_note())
        digest = self._candidate_digest(candidates)
        if verdict is not None:
            self._advisor_verdicts[page.structural_identity] = (verdict, source, digest)
            summary = f"{page.page_path}: {verdict.page_summary or '（无摘要）'}"
            self._advisor_summaries.append(summary)
        self._progress(
            "advisor",
            {
                "page_id": page.page_id,
                "source": source,
                "turns": self.advisor.turn_count,
                "verdict": verdict.model_dump() if verdict else None,
                "candidates": digest,
            },
        )
        # 把本轮（或异常兜底轮）LLM 调用的输入/输出留痕作为独立进度事件推送，供前端实时渲染思考流。
        for record in self.advisor.turns[turns_before:]:
            self._progress(
                "advisor_turn",
                {"page_id": page.page_id, "turn": record.model_dump(mode="json"), "candidates": digest},
            )
        return verdict, source

    @staticmethod
    def _candidate_digest(candidates: list[ExplorationAction]) -> list[dict[str, object]]:
        """候选动作的可读摘要：顾问的 recommended/avoid 编号即此列表的下标。"""
        digest: list[dict[str, object]] = []
        for index, action in enumerate(candidates):
            locator = action.locator_value if action.locator_kind in {"key", "id"} else action.target_text
            digest.append(
                {
                    "index": index,
                    "kind": action.kind,
                    "label": str(locator)[:60],
                    "coordinate": list(action.coordinate) if action.coordinate else None,
                }
            )
        return digest

    def _apply_advisor(
        self,
        candidates: list[ExplorationAction],
        verdict: AdvisorVerdict,
    ) -> list[ExplorationAction]:
        """按建议重排/过滤候选：recommended 提前，avoid 剔除（input/swipe 不受 avoid 影响）。"""
        limit = len(candidates)
        recommended_ids = {
            candidates[index].action_id
            for index in verdict.recommended
            if isinstance(index, int) and 0 <= index < limit
        }
        avoid_ids = {
            candidates[index].action_id
            for index in verdict.avoid
            if isinstance(index, int) and 0 <= index < limit and candidates[index].kind == "click"
        }
        recommended = [item for item in candidates if item.action_id in recommended_ids]
        rest = [
            item for item in candidates if item.action_id not in recommended_ids and item.action_id not in avoid_ids
        ]
        ordered = recommended + rest
        return ordered[: max(self.policy.max_actions_per_page, len(recommended))]

    def _advisor_context_note(self) -> str:
        """已探索页面的确定性摘要，随 payload 携带；不额外调用模型做压缩。"""
        if not self._advisor_summaries:
            return ""
        lines = [f"- {summary}" for summary in self._advisor_summaries[-10:]]
        return "此前已探索页面摘要（避免重复推荐已走过的入口）：\n" + "\n".join(lines)

    def _recover_to_source(
        self,
        path: list[ExplorationAction],
        source_identity: str,
        expected: ScreenSnapshot,
        expected_foreground: ForegroundApp,
        action: ExplorationAction,
        after: ScreenSnapshot | None,
        transition: DiscoveryTransition,
        sequence: int,
    ) -> tuple[ScreenSnapshot, ForegroundApp]:
        """Cheaply return to the source page between candidates; cold restore is the last resort.

        队列出队的冷启动回放承担路径验证；候选动作之间按代价递增三级恢复：

        1. 落地页身份与源页一致 → 直接复用当前帧（零设备动作）；
        2. 连续 ``back``（至多 ``restore_retries + 1`` 次）逐次校验身份；
        3. 仍不匹配才走 ``stop_app`` + ``start_app`` + 全路径重放。

        计划 4.3：``input`` 动作原先**无条件**冷恢复，是单动作最大开销；软键盘通常只需一次
        ``back`` 即可消除，落地页身份未变时更不该重启应用。因此改为与其它动作同一路径。
        """
        if after is not None and transition.foreground_after:
            if self._structural_identity(after, transition.foreground_after) == source_identity:
                return after, transition.foreground_after
        cheap_limit = max(1, self.policy.restore_retries + 1)
        for attempt in range(1, cheap_limit + 1):
            backed = self.device.back()
            if not backed.ok:
                break
            self.device.wait(0.5)
            try:
                recovered_foreground = self._assert_target_foreground()
                recovered = self._capture_settled(f"recover-{sequence:03d}-{attempt:02d}")
            except DeviceError:
                break
            if self._structural_identity(recovered, recovered_foreground) == source_identity:
                return recovered, recovered_foreground
        return self._restore_path(path, expected, expected_foreground, sequence)

    def _action(
        self,
        kind: Literal["click", "input", "swipe"],
        element: UIElement,
        text: str,
        risk: ActionRisk,
        reason: str,
        permission: str | None,
        content_like: bool = False,
    ) -> ExplorationAction:
        identity = element.key or element.id or element.element_id
        if element.key:
            locator_kind, locator_value = "key", element.key
        elif element.id:
            locator_kind, locator_value = "id", element.id
        elif element.type and element.content:
            locator_kind, locator_value = "type_text", f"{element.type}|{element.content}"
        elif element.content:
            locator_kind, locator_value = "text", element.content
        else:
            locator_kind, locator_value = "coordinate", str(element.bbox.center if element.bbox else "")
        action_id = hashlib.sha256(f"{kind}|{identity}|{text}".encode()).hexdigest()[:16]
        return ExplorationAction(
            action_id=action_id,
            kind=kind,
            element_id=element.element_id,
            locator_kind=locator_kind,
            locator_value=locator_value,
            target_text=text,
            coordinate=element.bbox.center if element.bbox else None,
            direction="up" if kind == "swipe" else None,
            risk=risk,
            risk_reason=reason,
            required_permission=permission,
            content_like=content_like,
        )

    def resolve_replay_action(
        self,
        action: ExplorationAction,
        snapshot: ScreenSnapshot,
    ) -> ExplorationAction:
        """Re-resolve a replay action against the current page and reapply safety classification."""
        if action.kind in {"back", "swipe"}:
            return action
        match = next(
            (
                item
                for item in snapshot.elements
                if (action.locator_kind == "key" and item.key == action.locator_value)
                or (action.locator_kind == "id" and item.id == action.locator_value)
                or (action.locator_kind == "text" and item.content == action.locator_value)
                or (action.locator_kind == "type_text" and f"{item.type}|{item.content}" == action.locator_value)
            ),
            None,
        )
        if match is None or match.bbox is None:
            raise DeviceError(f"replay locator no longer resolves: {action.action_id}")
        text = " ".join(
            filter(
                None,
                (match.content, match.description, match.key, match.id, match.type),
            )
        )
        risk, reason, permission = self.classifier.classify(text, self.policy)
        if risk != ActionRisk.DEFAULT_ALLOWED:
            raise DeviceError(f"replay action blocked: {reason}")
        return action.model_copy(
            update={
                "element_id": match.element_id,
                "coordinate": match.bbox.center,
                "target_text": text,
                "risk": risk,
                "risk_reason": reason,
                "required_permission": permission,
            }
        )

    def _perform(
        self,
        page: DiscoveryPage,
        before: ScreenSnapshot,
        foreground: ForegroundApp,
        action: ExplorationAction,
        sequence: int,
    ) -> tuple[DiscoveryTransition, ScreenSnapshot | None]:
        started = time.monotonic()
        command: CommandResult
        if action.kind == "click":
            assert action.coordinate
            command = self.device.click(*action.coordinate)
        elif action.kind == "input":
            assert action.coordinate
            command = self.device.input_text(self.policy.fixed_input_text, *action.coordinate)
        elif action.kind == "swipe":
            width, height = before.width, before.height
            command = self.device.swipe((width // 2, int(height * 0.75)), (width // 2, int(height * 0.25)))
        else:
            command = self.device.back()
        transition = DiscoveryTransition(
            source_page_id=page.page_id,
            action=action,
            before_snapshot_id=before.snapshot_id,
            foreground_before=foreground,
            command=command,
            success=command.ok,
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )
        if not command.ok:
            transition.blocked_reason = command.stderr or command.stdout or "device command failed"
            return transition, None
        self.device.wait(0.5)
        after = self.device.screenshot(self.output_dir, self.run_id, f"discovery-{sequence + 1:03d}")
        self._report_snapshot(after)
        transition.after_snapshot_id = after.snapshot_id
        after_foreground = self.device.current_foreground_app()
        transition.foreground_after = after_foreground
        if (
            not after_foreground
            or after_foreground.bundle_name != self.target.bundle_name
            or (after_foreground.ability_name and after_foreground.ability_name != self.target.main_ability)
        ):
            transition.success = False
            actual = after_foreground.bundle_name if after_foreground else "unknown"
            transition.blocked_reason = f"cross-bundle navigation blocked: {actual}"
            # 崩回桌面与「点了个跳到别的应用的链接」在现象上相同，必须靠日志区分。
            # 历史上这里直接静默恢复，一次真崩溃被记成「跨应用导航被拦截」当无事发生。
            crash_findings = self._probe_crash_after_foreground_loss(sequence)
            if crash_findings:
                transition.blocked_reason = f"app crash suspected: {transition.blocked_reason}"
                self._record_anomalies(crash_findings)
            backed = self.device.back()
            restored = self.device.current_foreground_app() if backed.ok else None
            if not restored or restored.bundle_name != self.target.bundle_name:
                restarted = self.device.start_app(
                    self.target.bundle_name, self.target.main_ability, self.target.module_name
                )
                restored = self.device.current_foreground_app() if restarted.ok else None
            if (
                not restored
                or restored.bundle_name != self.target.bundle_name
                or (restored.ability_name and restored.ability_name != self.target.main_ability)
            ):
                raise DeviceError("cross-bundle navigation recovery failed")
            return transition, None
        log_path = self.output_dir / f"transition-{sequence + 1:03d}.hilog.txt"
        self.device.collect_logs(log_path)
        return transition, after

    def _restore_path(
        self,
        path: list[ExplorationAction],
        expected: ScreenSnapshot,
        expected_foreground: ForegroundApp,
        sequence: int,
    ) -> tuple[ScreenSnapshot, ForegroundApp]:
        """Rebuild one queued state from a clean launch with bounded retries per attempt."""
        attempts = 1 + self.policy.restore_retries
        failure: str | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._restore_attempt(path, expected, expected_foreground, sequence, attempt)
            except DeviceError as exc:
                failure = str(exc)
                if attempt >= attempts:
                    break
                self._progress("restore_retry", {"sequence": sequence, "attempt": attempt, "error": failure})
        raise DeviceError(failure or "exploration path restoration failed")

    def _restore_attempt(
        self,
        path: list[ExplorationAction],
        expected: ScreenSnapshot,
        expected_foreground: ForegroundApp,
        sequence: int,
        attempt: int,
    ) -> tuple[ScreenSnapshot, ForegroundApp]:
        """Rebuild one queued state from a clean launch and verify the structural match."""
        stopped = self.device.stop_app(self.target.bundle_name)
        if not stopped.ok:
            raise DeviceError(f"force-stop failed while restoring exploration path: {stopped.stderr or stopped.stdout}")
        started = self.device.start_app(self.target.bundle_name, self.target.main_ability, self.target.module_name)
        if not started.ok:
            raise DeviceError(f"launch failed while restoring exploration path: {started.stderr or started.stdout}")
        foreground = self._assert_target_foreground()
        snapshot = self._capture_settled(f"restore-{sequence + attempt - 1:03d}-000")
        for index, action in enumerate(path, 1):
            source = self._page(snapshot, foreground, 0, path[: index - 1])
            action = self.resolve_replay_action(action, snapshot)
            transition, after = self._perform(source, snapshot, foreground, action, sequence + index)
            if not transition.success or after is None or transition.foreground_after is None:
                raise DeviceError(
                    f"failed to restore exploration path at action {action.action_id}: "
                    f"{transition.blocked_reason or 'unknown error'}"
                )
            snapshot = after
            foreground = transition.foreground_after
        if not self._structural_match(expected, expected_foreground, snapshot, foreground):
            raise DeviceError(self._restore_mismatch_detail(expected, snapshot))
        return snapshot, foreground

    def _report_snapshot(self, snapshot: ScreenSnapshot) -> None:
        """Push one settled exploration frame to the live view (device screen + element table)."""
        if self.on_snapshot is None:
            return
        self.on_snapshot(snapshot)

    def _capture_settled(self, label: str) -> ScreenSnapshot:
        """Capture one frame and poll the hierarchy within the settle budget for stability."""
        snapshot = self.device.screenshot(self.output_dir, self.run_id, label)
        if self.policy.settle_timeout_seconds <= 0:
            self._report_snapshot(snapshot)
            return snapshot
        fingerprint = self._stability_fingerprint(snapshot.page_path, snapshot.elements)
        deadline = time.monotonic() + self.policy.settle_timeout_seconds
        while time.monotonic() < deadline:
            self.device.wait(0.5)
            try:
                hierarchy = self.device.collect_ui_hierarchy()
            except DeviceError:
                break
            elements = normalize_layout(hierarchy, snapshot.width, snapshot.height)
            if self._stability_fingerprint(page_path(hierarchy), elements) == fingerprint:
                self._report_snapshot(snapshot)
                return snapshot
        snapshot = self.device.screenshot(self.output_dir, self.run_id, f"{label}-settled")
        self._report_snapshot(snapshot)
        return snapshot

    @classmethod
    def _stability_fingerprint(cls, page: str, elements: list[UIElement]) -> tuple[str, int, tuple[str, ...]]:
        return (page, len(elements), tuple(sorted(cls._stable_texts(elements))))

    def _probe_crash_after_foreground_loss(self, sequence: int) -> list[AnomalyFinding]:
        """跨 bundle 恢复前的崩溃探测：读回 ``transition-NNN.hilog.txt`` + faultlog 索引。

        历史上这些 ``transition-NNN.hilog.txt`` **从来没有任何代码读回**，应用崩溃弹回桌面
        因此被静默恢复。这里按 sequence 复用同一次 transition 的 hilog 文件（若已存在则复用，
        否则新采一次），把崩溃痕迹变成可追溯的 finding。
        """
        try:
            from ..analysis.in_run import probe_crash_after_foreground_loss

            log_path = self.output_dir / f"transition-{sequence + 1:03d}.hilog.txt"
            return probe_crash_after_foreground_loss(self.device, self.target.bundle_name, log_path)
        except Exception as exc:  # noqa: BLE001 - 探测失败不得影响探索恢复
            _LOG.warning("exploration crash probe failed: %s: %s", type(exc).__name__, exc)
            return []

    def _record_anomalies(self, findings: list[AnomalyFinding]) -> None:
        """把探索期 finding 收进探索结果（additive，落 ``discovery/summary.json``）。"""
        for finding in findings:
            finding.phase = "exploration"
            if finding.detected_at is None:
                from ..models import utc_now

                finding.detected_at = utc_now()
            if finding.screenshot == "" and finding.evidence.get("screenshot"):
                finding.screenshot = str(finding.evidence["screenshot"])
            self._anomalies.append(finding)

    @staticmethod
    def _stable_texts(elements: list[UIElement]) -> set[str]:
        return {
            _normalize_text(item.content)
            for item in elements
            if item.content and len(item.content) <= 80 and not _is_volatile_text(item.content)
        }

    @classmethod
    def _structural_match(
        cls,
        expected: ScreenSnapshot,
        expected_foreground: ForegroundApp,
        actual: ScreenSnapshot,
        actual_foreground: ForegroundApp,
    ) -> bool:
        """Page identity holds when the queued page's interactive structure is still present.

        key/id 集合不参与判定：真实应用的 key 常携带内容实例 ID 或面板模式（热榜/历史），
        跨启动不可复现；错误页由每个回放动作的定位符重解析兜底。
        """
        _, expected_interactive = cls._structural_features(expected)
        _, actual_interactive = cls._structural_features(actual)
        if (
            expected.page_path != actual.page_path
            or expected_foreground.bundle_name != actual_foreground.bundle_name
            or expected_foreground.window_type != actual_foreground.window_type
        ):
            return False
        return expected_interactive.issubset(actual_interactive)

    @staticmethod
    def _identity_key_set(elements: list[UIElement]) -> tuple[set[str], list[str]]:
        """折叠后的结构骨架 key 集合，以及被判定为易变而剔除的原始 key。

        唯一实现（计划 2.1/2.2）：``_structural_features`` 与 ``_structural_identity``
        必须用同一套折叠与过滤规则。此前 ``_structural_features`` 只折叠 ``\\d{4,}``，
        而 ``_structural_identity`` 折叠 ``\\d+``，于是 1-2 位日期数字在身份哈希里被折叠、
        在 ``_identity_subset`` 子集判定里被保留，同一份证据两套判定必然打架（R7）。

        被剔除的 key 分三类：长数字/哈希型内容实例 ID、信息流容器（feed/card/...）、
        时间与日期型 key、列表项实例 key（``_item193``）。

        选中态（``selected``）以 ``#selected`` 后缀参与骨架：同页不同 Tab 选中态因此
        拥有不同身份，仍可被计为「不同的页面状态」；仅切换 Tab 不会改变物理 page_path。
        """
        keys: set[str] = set()
        volatile: list[str] = []
        for item in elements:
            raw = item.key or item.id
            if not raw:
                continue
            if is_volatile_structural_key(raw):
                volatile.append(raw)
                continue
            folded = _IDENTITY_KEY_ID_PATTERN.sub("#", raw)
            keys.add(f"{folded}#selected" if item.selected else folded)
        return keys, volatile

    @staticmethod
    def _structural_features(snapshot: ScreenSnapshot) -> tuple[set[str], set[tuple[str, bool, bool, bool]]]:
        """结构骨架 key（统一折叠规则 + 易变 key 过滤）与可交互结构。"""
        keys, _ = BoundedExplorer._identity_key_set(snapshot.elements)
        interactive = {
            (item.type, item.clickable, item.editable, item.scrollable)
            for item in snapshot.elements
            if item.clickable or item.editable or item.scrollable
        }
        return keys, interactive

    @staticmethod
    def _state_kind(elements: list[UIElement]) -> Literal["page", "tab_state", "dialog"]:
        """单页应用状态分类（计划 2.3，additive 元数据，不参与任何门禁）。

        优先级：出现遮罩/弹窗容器 key → ``dialog``；出现带选中态的 Tab 容器 key →
        ``tab_state``（同一 ``page_path`` 的不同 Tab 状态）；其余 → ``page``。
        """
        for item in elements:
            raw = item.key or item.id
            if raw and _DIALOG_KEY_PATTERN.search(raw):
                return "dialog"
        for item in elements:
            raw = item.key or item.id
            if raw and item.selected and _TAB_KEY_PATTERN.search(raw):
                return "tab_state"
        return "page"

    @classmethod
    def _structural_identity(cls, snapshot: ScreenSnapshot, foreground: ForegroundApp) -> str:
        """内容抖动稳定的逻辑页身份：page_path + 前台 + 结构骨架 key + 可交互结构。

        与 `_structural_match` 的差异：身份用于探索去重与跨轮比对，key 折叠到任意
        数字串，并进一步剔除内容流噪音 key——信息流卡片（携带内容实例 ID）与
        feed/card 等内容容器会随推荐内容在"存在/不存在"之间翻转（知乎首页实测存在
        feed_list 与 feed_card_article 两个互不为子集的变体），只保留结构骨架才能
        让"同一页面、不同内容实例"收敛为同一逻辑页。
        时间/日期型 key 同样剔除：日历类应用的状态栏时钟与日期格每次启动都不同（R7）。
        """
        keys, _ = cls._identity_key_set(snapshot.elements)
        _, interactive = cls._structural_features(snapshot)
        raw = json.dumps(
            [
                snapshot.page_path,
                foreground.bundle_name,
                foreground.window_type,
                sorted(keys),
                sorted(interactive),
            ],
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _restore_mismatch_detail(expected: ScreenSnapshot, actual: ScreenSnapshot) -> str:
        expected_texts = BoundedExplorer._stable_texts(expected.elements)
        actual_texts = BoundedExplorer._stable_texts(actual.elements)
        expected_keys, expected_interactive = BoundedExplorer._structural_features(expected)
        actual_keys, actual_interactive = BoundedExplorer._structural_features(actual)
        missing_texts = sorted(expected_texts - actual_texts)[:5]
        unexpected_texts = sorted(actual_texts - expected_texts)[:5]
        missing_keys = sorted(expected_keys - actual_keys)[:5]
        unexpected_keys = sorted(actual_keys - expected_keys)[:5]
        missing_widgets = sorted(
            f"{kind}/{clickable}/{editable}/{scrollable}"
            for kind, clickable, editable, scrollable in (expected_interactive - actual_interactive)
        )[:5]
        return (
            "restored exploration state does not match the queued page structure: "
            f"expected page_path={expected.page_path!r}, actual page_path={actual.page_path!r}; "
            f"missing widgets={missing_widgets}; "
            f"missing keys={missing_keys}, unexpected keys={unexpected_keys}; "
            f"missing texts={missing_texts}, unexpected texts={unexpected_texts}"
        )

    def _progress(self, kind: str, payload: dict[str, object]) -> None:
        if self.progress:
            # 注入 stage 便于前端按阶段分发（advisor/advisor_turn/blocked/finished 等）。
            self.progress(kind, {"stage": kind, **payload})

    def _assert_target_foreground(self) -> ForegroundApp:
        foreground = self.device.current_foreground_app()
        if not foreground:
            raise DeviceError("foreground application could not be determined after launch")
        if foreground.bundle_name != self.target.bundle_name:
            raise DeviceError(
                f"launched bundle mismatch: expected {self.target.bundle_name}, got {foreground.bundle_name}"
            )
        if foreground.ability_name and foreground.ability_name != self.target.main_ability:
            raise DeviceError(
                f"launched Ability mismatch: expected {self.target.main_ability}, got {foreground.ability_name}"
            )
        return foreground

    @staticmethod
    def _snapshot_signature(snapshot: ScreenSnapshot, foreground: ForegroundApp) -> str:
        stable_keys = sorted({item.key or item.id for item in snapshot.elements if item.key or item.id})
        texts = sorted(BoundedExplorer._stable_texts(snapshot.elements))[:30]
        raw = json.dumps(
            [
                snapshot.page_path,
                foreground.bundle_name,
                foreground.window_type,
                stable_keys,
                texts,
                sorted((item.type, item.clickable, item.editable, item.scrollable) for item in snapshot.elements),
            ],
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _page(
        snapshot: ScreenSnapshot,
        foreground: ForegroundApp,
        order: int,
        path_actions: list[ExplorationAction] | None = None,
        structural_identity: str = "",
    ) -> DiscoveryPage:
        signature = BoundedExplorer._snapshot_signature(snapshot, foreground)
        identity_keys, identity_interactive = BoundedExplorer._structural_features(snapshot)
        _, volatile_keys = BoundedExplorer._identity_key_set(snapshot.elements)
        return DiscoveryPage(
            page_id=f"page-{signature[:12]}",
            signature=signature,
            structural_identity=structural_identity or BoundedExplorer._structural_identity(snapshot, foreground),
            identity_keys=sorted(identity_keys),
            volatile_keys=sorted(set(volatile_keys)),
            state_kind=BoundedExplorer._state_kind(snapshot.elements),
            identity_interactive=sorted(
                f"{kind}/{clickable}/{editable}/{scrollable}"
                for kind, clickable, editable, scrollable in identity_interactive
            ),
            page_path=snapshot.page_path,
            bundle_name=foreground.bundle_name,
            ability_name=foreground.ability_name,
            window_type=foreground.window_type,
            snapshot_id=snapshot.snapshot_id,
            image_path=snapshot.image_path,
            hierarchy_path=snapshot.hierarchy_path,
            element_count=len(snapshot.elements),
            discovered_order=order,
            path_actions=list(path_actions or []),
        )

    @staticmethod
    def _early_success(result: DiscoveryResult, min_interaction_kinds: int) -> bool:
        # 与 Profile 验证准入门槛一致：一条动作类型达标的路径覆盖 ≥3 个逻辑页即可早停。
        for page in result.pages:
            if len({action.kind for action in page.path_actions}) < min_interaction_kinds:
                continue
            flow_pages = [
                item for item in result.pages if page.path_actions[: len(item.path_actions)] == item.path_actions
            ]
            if len(flow_pages) >= 3:
                return True
        return False

    def _save_observations(self, result: DiscoveryResult) -> None:
        locator_observations: list[dict[str, object]] = []
        assertion_candidates: list[dict[str, object]] = []
        for page in result.pages:
            locator_observations.append(
                {"page_signature": page.signature, "snapshot_id": page.snapshot_id, "source": "ui_hierarchy"}
            )
            assertion_candidates.append(
                {"page_signature": page.signature, "kind": "page_exists", "target": page.page_path}
            )
        for name, payload in {
            "locator-observations.json": locator_observations,
            "assertion-candidates.json": assertion_candidates,
        }.items():
            path = self.output_dir / name
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)

    def _save(self, result: DiscoveryResult) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        documents = {
            "resolved-target.json": result.target.model_dump(mode="json"),
            "policy.json": result.policy.model_dump(mode="json"),
            "pages.json": [item.model_dump(mode="json") for item in result.pages],
            "transitions.json": [item.model_dump(mode="json") for item in result.transitions],
            "blocked-actions.json": [item.model_dump(mode="json") for item in result.blocked_actions],
            "summary.json": result.model_dump(mode="json", exclude={"started_at_monotonic"}),
        }
        for name, payload in documents.items():
            path = self.output_dir / name
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _is_volatile_text(value: str) -> bool:
    """加载占位、时间样式与纯数字/标点文本跨启动或跨分钟不稳定，不参与页面签名。"""
    normalized = _normalize_text(value)
    if not normalized:
        return True
    if _LOADING_TEXT_PATTERN.search(normalized):
        return True
    if _TIME_TEXT_PATTERN.match(normalized):
        return True
    return bool(_DIGIT_PUNCT_TEXT_PATTERN.match(normalized))
