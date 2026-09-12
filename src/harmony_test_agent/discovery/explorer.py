"""Bounded, auditable exploration of a resolved HarmonyOS application."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..devices import DeviceAdapter, DeviceError
from ..models import CommandResult, ExplorationPolicy, ScreenSnapshot, UIElement
from ..runtime.safety import SafetyPolicy
from ..targets import ForegroundApp, ResolvedTarget


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


class DiscoveryPage(BaseModel):
    """A deduplicated page in the exploration graph."""

    page_id: str
    signature: str
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


class DiscoveryResult(BaseModel):
    """Complete bounded exploration output suitable for draft Profile creation."""

    target: ResolvedTarget
    policy: ExplorationPolicy
    pages: list[DiscoveryPage] = Field(default_factory=list)
    transitions: list[DiscoveryTransition] = Field(default_factory=list)
    blocked_actions: list[ExplorationAction] = Field(default_factory=list)
    started_at_monotonic: float = Field(exclude=True, default=0)
    duration_seconds: float = 0
    stop_reason: str = "queue_exhausted"

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
    ) -> None:
        self.device = device
        self.target = target
        self.output_dir = output_dir
        self.run_id = run_id
        self.policy = policy or ExplorationPolicy()
        self.classifier = classifier or ActionRiskClassifier()
        self.progress = progress
        self.should_stop = should_stop or (lambda: False)

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
        page_by_signature: dict[str, DiscoveryPage] = {}
        snapshot = self.device.screenshot(self.output_dir, self.run_id, "discovery-000")
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
            current_snapshot, current_foreground = self._restore_path(
                current_path,
                current_snapshot,
                current_foreground,
                len(result.transitions),
            )
            page = self._page(current_snapshot, current_foreground, len(page_by_signature) + 1, current_path)
            if page.signature not in page_by_signature:
                if len(page_by_signature) >= self.policy.max_pages:
                    result.stop_reason = "page_limit"
                    break
                page_by_signature[page.signature] = page
                result.pages.append(page)
            else:
                page = page_by_signature[page.signature]

            candidates = self.candidate_actions(current_snapshot)[: self.policy.max_actions_per_page]
            for action in candidates:
                key = (page.signature, action.action_id)
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
                transition, after = self._perform(
                    page, current_snapshot, current_foreground, action, len(result.transitions)
                )
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
                    after_page = self._page(
                        after, transition.foreground_after, len(page_by_signature) + 1, [*current_path, action]
                    )
                    if after_page.signature not in page_by_signature and len(page_by_signature) < self.policy.max_pages:
                        page_by_signature[after_page.signature] = after_page
                        result.pages.append(after_page)
                        transition.target_page_id = after_page.page_id
                        queue.append((after, transition.foreground_after, [*current_path, action]))
                    elif after_page.signature in page_by_signature:
                        transition.target_page_id = page_by_signature[after_page.signature].page_id
                current_snapshot, current_foreground = self._restore_path(
                    current_path,
                    current_snapshot,
                    current_foreground,
                    len(result.transitions),
                )
            if self._early_success(result):
                result.stop_reason = "admission_metrics_reached"
                break

        result.duration_seconds = round(time.monotonic() - result.started_at_monotonic, 3)
        self._save(result)
        self._save_observations(result)
        self._progress("finished", {"stop_reason": result.stop_reason, "pages": len(result.pages)})
        return result

    def candidate_actions(self, snapshot: ScreenSnapshot) -> list[ExplorationAction]:
        """Rank hierarchy-backed actions and cap them later per page."""
        ranked: list[tuple[int, ExplorationAction]] = []
        for element in snapshot.elements:
            text = " ".join(filter(None, (element.content, element.description, element.key, element.id, element.type)))
            risk, reason, permission = self.classifier.classify(text, self.policy)
            stable = bool(element.key or element.id)
            if element.clickable and element.bbox:
                ranked.append((0 if stable else 1, self._action("click", element, text, risk, reason, permission)))
            if element.editable and element.bbox:
                ranked.append((2, self._action("input", element, text, risk, reason, permission)))
            if element.scrollable:
                ranked.append((3, self._action("swipe", element, text, risk, reason, permission)))
        unique: dict[str, tuple[int, ExplorationAction]] = {}
        for item in ranked:
            unique.setdefault(item[1].action_id, item)
        return [item[1] for item in sorted(unique.values(), key=lambda item: (item[0], item[1].action_id))]

    def _action(
        self,
        kind: Literal["click", "input", "swipe"],
        element: UIElement,
        text: str,
        risk: ActionRisk,
        reason: str,
        permission: str | None,
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
        """Rebuild one queued state from a clean launch and verify every path step."""
        stopped = self.device.stop_app(self.target.bundle_name)
        if not stopped.ok:
            raise DeviceError(f"force-stop failed while restoring exploration path: {stopped.stderr or stopped.stdout}")
        started = self.device.start_app(self.target.bundle_name, self.target.main_ability, self.target.module_name)
        if not started.ok:
            raise DeviceError(f"launch failed while restoring exploration path: {started.stderr or started.stdout}")
        foreground = self._assert_target_foreground()
        snapshot = self.device.screenshot(self.output_dir, self.run_id, f"restore-{sequence:03d}-000")
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
        actual = self._page(snapshot, foreground, 0, path)
        expected_signature = self._snapshot_signature(expected, expected_foreground)
        if actual.signature != expected_signature:
            raise DeviceError("restored exploration state does not match the queued page signature")
        return snapshot, foreground

    def _progress(self, kind: str, payload: dict[str, object]) -> None:
        if self.progress:
            self.progress(kind, payload)

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
        texts = sorted(
            {_normalize_text(item.content) for item in snapshot.elements if item.content and len(item.content) <= 80}
        )[:30]
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
    ) -> DiscoveryPage:
        signature = BoundedExplorer._snapshot_signature(snapshot, foreground)
        return DiscoveryPage(
            page_id=f"page-{signature[:12]}",
            signature=signature,
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
    def _early_success(result: DiscoveryResult) -> bool:
        # Metrics split across branches cannot satisfy the replay admission gate.
        return any(
            len(page.path_actions) >= 3 and len({action.kind for action in page.path_actions}) >= 3
            for page in result.pages
        )

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
