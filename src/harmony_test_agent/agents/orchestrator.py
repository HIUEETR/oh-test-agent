"""串联规划、设备操作、页面感知、产物生成与回放的任务编排器。"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import Callable

from ..config import Settings
from ..devices import DeviceAdapter, DeviceError, HarmonyDeviceAdapter
from ..generation import HypiumGenerator
from ..graph import PageGraphBuilder
from ..models import (
    ActionResult,
    AppVersion,
    AssertionDefinition,
    AssertionResult,
    ConfidenceLevel,
    DeviceCompatibility,
    EventType,
    LocatorCandidate,
    LocatorKind,
    ProfileProvenance,
    ProfileStatus,
    PlannedStep,
    RunEvent,
    RunRequest,
    RunState,
    RunTrace,
    ResolvedTarget,
    ScreenSnapshot,
    StableLocator,
    TargetAppProfile,
    ToolDecision,
    ToolName,
    utc_now,
)
from ..discovery import BoundedExplorer, ExplorationAction, ProfileVerifier, StabilityLevel
from ..perception import PerceptionService
from ..reporting import ReportBuilder
from ..runner import HypiumRunner
from ..runtime import RunEventEmitter, SafetyError, SafetyPolicy, ToolExecutionError, ToolExecutor
from ..profiles import ProfileRegistry
from ..storage import ArtifactStore, RunRepository
from ..targets import TargetAmbiguousError, TargetNotFoundError, TargetResolver
from .providers import AgentProvider, create_provider

DeviceFactory = Callable[[str], DeviceAdapter]


class AgentOrchestrator:
    """协调单次测试任务的完整生命周期，并持续保存事件、轨迹和报告。"""

    def __init__(
        self,
        settings: Settings,
        *,
        provider: AgentProvider | None = None,
        repository: RunRepository | None = None,
        artifacts: ArtifactStore | None = None,
        device_factory: DeviceFactory | None = None,
        event_callback: Callable[[RunEvent], None] | None = None,
        settle_seconds: float = 0.8,
    ):
        self.settings = settings
        self.provider = provider or create_provider(settings)
        self.repository = repository or RunRepository(settings.resolved_database_path)
        self.artifacts = artifacts or ArtifactStore(settings.resolved_runtime_dir)
        self.device_factory = device_factory or self._default_device_factory
        self.event_callback = event_callback
        self.settle_seconds = settle_seconds
        self._stop_requested: set[str] = set()
        self._target_selections: dict[str, str] = {}
        self._target_selection_events: dict[str, asyncio.Event] = {}

    def _default_device_factory(self, device_id: str) -> DeviceAdapter:
        return HarmonyDeviceAdapter(device_id, self.settings.hdc_path, self.settings.agent_action_timeout)

    def request_stop(self, run_id: str) -> None:
        """记录停止请求；编排循环会在下一个安全检查点结束指定任务。"""
        self._stop_requested.add(run_id)
        if event := self._target_selection_events.get(run_id):
            event.set()

    async def select_target(
        self,
        run_id: str,
        *,
        bundle_name: str | None = None,
        candidate_id: str | None = None,
        selection: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Resume one waiting Run with an exact candidate bundle."""
        bundle = bundle_name or str((selection or {}).get("bundle_name") or candidate_id or "")
        event = self._target_selection_events.get(run_id)
        if not event or not bundle:
            raise ValueError("run is not waiting for a valid target selection")
        self._target_selections[run_id] = bundle
        event.set()
        return {"run_id": run_id, "accepted": True, "bundle_name": bundle}

    async def run(self, request: RunRequest, run_id: str | None = None) -> RunTrace:
        """执行任务并返回最终轨迹；设备连接、模型调用和工具动作均受配置超时约束。"""
        run_id = run_id or f"run-{utc_now():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        device_id = request.device_id or self.settings.harmony_device
        requested_id = request.target_app_id or request.target.bundle_name or request.target.app_name
        trace = RunTrace(
            run_id=run_id,
            target_app_id=requested_id,
            target_query=request.target,
            exploration_policy=request.exploration_policy,
            task=request.task,
            mode=request.mode,
            device_id=device_id,
            model_used=self.provider.name,
            model_mock=self.provider.mock,
        )
        emitter = RunEventEmitter(trace, self.repository, self.artifacts, self.event_callback)
        device = self.device_factory(device_id)
        graph = PageGraphBuilder(trace.graph)
        perception = PerceptionService(self.settings.vlm_min_confidence)
        profile: TargetAppProfile | None = None
        executor: ToolExecutor | None = None
        current_snapshot: ScreenSnapshot | None = None
        current_node = None
        unchanged_count = 0

        emitter.emit(EventType.RUN_STARTED, "运行任务已创建", {"mode": request.mode, "model": self.provider.name})
        try:
            trace.state = RunState.PREFLIGHT
            await asyncio.to_thread(device.connect)
            health = await asyncio.to_thread(device.health_check)
            if not health.get("connected"):
                raise DeviceError(f"device health check failed: {health}")
            emitter.emit(EventType.PREFLIGHT_PASSED, "设备预检通过", health)
            profile = await self._prepare_target(request, trace, emitter, device)
            if self._should_stop(trace.run_id):
                trace.state = RunState.STOPPED_BY_USER
                trace.ended_at = utc_now()
                emitter.emit(EventType.RUN_FINISHED, "任务已由用户停止")
                return trace
            trace.target_app_id = profile.target_app_id
            trace.profile_snapshot = profile.model_copy(deep=True)
            trace.profile_status_at_start = profile.status
            if request.bootstrap_only:
                trace.state = RunState.COMPLETED
                trace.ended_at = utc_now()
                emitter.emit(
                    EventType.RUN_FINISHED,
                    "Profile 发现与验证完成",
                    {"profile_status": profile.status},
                )
                return trace
            executor = ToolExecutor(
                device=device,
                profile=profile,
                safety=SafetyPolicy(exploration_policy=request.exploration_policy),
            )
            executor.safety.validate_task(request.task)
            trace.phase = "task"
            emitter.emit(EventType.ORIGINAL_TASK_STARTED, "开始执行用户原始测试任务", {"task": request.task})

            trace.state = RunState.PLANNING
            try:
                step_limit = min(request.max_steps, self.settings.agent_max_steps)
                plan = await asyncio.wait_for(
                    self.provider.plan(request.task, profile, step_limit),
                    timeout=self.settings.agent_model_timeout,
                )
            except Exception as exc:
                await self._fail(trace, emitter, RunState.FAILED_MODEL, f"planning failed: {exc}")
                return trace
            trace.plan = plan.steps
            trace.model_used = plan.model_used
            trace.model_mock = plan.mock
            emitter.emit(
                EventType.PLAN_CREATED,
                f"已生成 {len(trace.plan)} 个原子步骤",
                {"steps": [step.model_dump(mode="json") for step in trace.plan], "mock": plan.mock},
            )

            trace.state = RunState.EXECUTING
            for index, step in enumerate(trace.plan, start=1):
                if self._should_stop(trace.run_id):
                    trace.state = RunState.STOPPED_BY_USER
                    trace.ended_at = utc_now()
                    emitter.emit(EventType.RUN_FINISHED, "任务已由用户停止")
                    return trace
                if index > min(request.max_steps, self.settings.agent_max_steps):
                    raise ToolExecutionError("maximum step count reached", RunState.FAILED_ACTION)

                if current_snapshot is None:
                    current_snapshot, current_node = await self._capture(
                        trace, emitter, device, perception, graph, f"step_{index:02d}_before"
                    )
                try:
                    decision = await asyncio.wait_for(
                        self.provider.decide(step, current_snapshot),
                        timeout=self.settings.agent_model_timeout,
                    )
                except TimeoutError as exc:
                    raise ToolExecutionError(
                        f"model tool decision timed out after {self.settings.agent_model_timeout} seconds",
                        RunState.FAILED_MODEL,
                    ) from exc
                except Exception as exc:
                    raise ToolExecutionError(f"model tool decision failed: {exc}", RunState.FAILED_MODEL) from exc
                decision = self._constrain_finish_decision(step, decision)
                emitter.emit(
                    EventType.ACTION_STARTED,
                    step.instruction,
                    {"step_id": step.step_id, "decision": decision.model_dump(mode="json")},
                )

                before = current_snapshot
                action_started_at = utc_now()
                try:
                    result = await self._execute_with_retry(executor, step.step_id, decision, before)
                except (SafetyError, ToolExecutionError) as exc:
                    failed = ActionResult(
                        step_id=step.step_id,
                        tool=decision.tool,
                        params=decision.model_dump(exclude_none=True),
                        success=False,
                        started_at=action_started_at,
                        ended_at=utc_now(),
                        before_snapshot_id=before.snapshot_id if before else None,
                        after_snapshot_id=before.snapshot_id if before else None,
                        error=str(exc),
                    )
                    trace.actions.append(failed)
                    emitter.emit(
                        EventType.ACTION_FINISHED,
                        f"步骤失败：{step.instruction}",
                        failed.model_dump(mode="json"),
                    )
                    raise
                result.before_snapshot_id = before.snapshot_id if before else None

                if decision.tool == ToolName.FINISH:
                    trace.state = RunState.VERIFYING
                    after, after_node = await self._capture(
                        trace, emitter, device, perception, graph, f"step_{index:02d}_after"
                    )
                    result.before_snapshot_id = before.snapshot_id if before else None
                    result.after_snapshot_id = after.snapshot_id
                    trace.actions.append(result)
                    current_snapshot, current_node = after, after_node
                    emitter.emit(EventType.ACTION_FINISHED, "Agent 已结束工具循环", result.model_dump(mode="json"))
                    break

                if self.settle_seconds and decision.tool not in {ToolName.INSPECT_SCREEN, ToolName.WAIT}:
                    await asyncio.sleep(self.settle_seconds)
                trace.state = RunState.VERIFYING
                after, after_node = await self._capture(
                    trace, emitter, device, perception, graph, f"step_{index:02d}_after"
                )
                result.after_snapshot_id = after.snapshot_id
                trace.actions.append(result)
                if result.assertion:
                    trace.assertions.append(result.assertion)
                    emitter.emit(
                        EventType.ASSERTION_PASSED,
                        result.assertion.message,
                        result.assertion.model_dump(mode="json"),
                    )
                trace.state = RunState.GRAPH_UPDATING
                if current_node and after_node:
                    edge = graph.add_edge(current_node, after_node, decision.tool, decision.target or "")
                    if edge:
                        emitter.emit(EventType.EDGE_CREATED, "已记录页面跳转", edge.model_dump(mode="json"))

                if before and decision.tool in {
                    ToolName.OPEN_APP,
                    ToolName.CLICK_ELEMENT,
                    ToolName.CLICK_COORDINATE,
                    ToolName.INPUT_TEXT,
                    ToolName.SWIPE,
                    ToolName.BACK,
                }:
                    unchanged_count = unchanged_count + 1 if before.image_sha256 == after.image_sha256 else 0
                    if unchanged_count >= self.settings.unchanged_screen_limit:
                        raise ToolExecutionError("screen did not change after consecutive mutating actions")
                current_snapshot, current_node = after, after_node
                trace.state = RunState.EXECUTING
                self._save_action_command(trace.run_id, result)
                emitter.emit(EventType.ACTION_FINISHED, step.instruction, result.model_dump(mode="json"))

            trace.state = RunState.GRAPH_UPDATING
            trace.graph = graph.graph
            self.artifacts.write_json(self.artifacts.run_dir(trace.run_id) / "graph.json", trace.graph)

            if trace.provisional:
                request.auto_generate = False
                request.auto_execute = False
            if request.auto_generate:
                trace.state = RunState.SCRIPT_GENERATING
                trace.generated = HypiumGenerator(self.artifacts).generate(trace)
                emitter.emit(
                    EventType.SCRIPT_GENERATED,
                    "Hypium Python 和 JSON 已生成",
                    trace.generated.model_dump(mode="json"),
                )
            if request.auto_execute:
                if not trace.generated:
                    raise ToolExecutionError("cannot execute before generating a script", RunState.FAILED_SCRIPT)
                trace.state = RunState.SCRIPT_EXECUTING
                emitter.emit(EventType.EXECUTION_STARTED, "开始执行生成的 Hypium 用例")
                runner = HypiumRunner(self.settings.resolved_runtime_home)
                trace.replays = await self._run_replays(runner, trace.generated, trace.run_id, 3)
                if not all(item.passed for item in trace.replays):
                    raise ToolExecutionError("one or more Hypium replay attempts failed", RunState.FAILED_SCRIPT)
                emitter.emit(
                    EventType.EXECUTION_FINISHED,
                    "Hypium 用例连续执行三次成功",
                    {"replays": [item.model_dump(mode="json") for item in trace.replays]},
                )


            trace.state = RunState.COMPLETED
            trace.ended_at = utc_now()
            emitter.emit(
                EventType.RUN_FINISHED,
                "运行任务完成",
                {"pages": len(trace.graph.nodes), "edges": len(trace.graph.edges)},
            )
            return trace
        except asyncio.CancelledError:
            trace.state = RunState.STOPPED_BY_USER
            trace.ended_at = utc_now()
            emitter.emit(EventType.RUN_FINISHED, "任务已由用户停止")
            return trace
        except SafetyError as exc:
            await self._fail(trace, emitter, RunState.FAILED_ACTION, str(exc))
            return trace
        except DeviceError as exc:
            await self._fail(trace, emitter, RunState.FAILED_DEVICE, str(exc))
            return trace
        except ToolExecutionError as exc:
            await self._fail(trace, emitter, exc.state, str(exc))
            return trace
        except Exception as exc:
            await self._fail(trace, emitter, RunState.FAILED_ACTION, f"unexpected error: {type(exc).__name__}: {exc}")
            return trace
        finally:
            self._target_selection_events.pop(run_id, None)
            self._target_selections.pop(run_id, None)
            await asyncio.to_thread(device.close)
            self.repository.save_trace(trace)
            self.artifacts.save_trace(trace)
            ReportBuilder(self.artifacts).build(trace)

    async def _prepare_target(self, request, trace, emitter, device) -> TargetAppProfile:
        """Resolve, discover, validate and promote an application before the original task."""
        explicit_path = self.settings.resolved_target_profile_path
        registry = ProfileRegistry(self.settings.resolved_profiles_dir)
        explicit_override = bool(self.settings.target_profile_path and "TARGET_PROFILE_PATH" in os.environ)
        if explicit_override:
            print("warning: TARGET_PROFILE_PATH is deprecated; use the Profile Registry", file=sys.stderr)
        legacy = None
        explicit_resolved = None
        if explicit_path and explicit_path.exists() and (explicit_override or request.target_app_id == "zhihu-plus"):
            legacy = self.artifacts.load_profile(explicit_path)
            query_matches = (
                request.target_app_id == legacy.target_app_id
                or request.target.bundle_name == legacy.bundle_name
                or request.target.app_name in {legacy.target_app_id, legacy.display_name}
            )
            if query_matches:
                explicit_resolved = ResolvedTarget(
                    target_app_id=legacy.target_app_id,
                    display_name=legacy.display_name,
                    bundle_name=legacy.bundle_name,
                    main_ability=legacy.main_ability,
                    module_name=legacy.module_name,
                    version_name=legacy.app_version.version_name,
                    version_code=legacy.app_version.version_code,
                    signature_sha256=legacy.app_version.signature_sha256,
                    device_id=trace.device_id,
                    source="explicit_override",
                    profile_snapshot=legacy,
                )

        trace.state = RunState.RESOLVING_TARGET
        try:
            resolved = explicit_resolved or await asyncio.to_thread(TargetResolver(device).resolve, request.target)
        except TargetAmbiguousError as exc:
            candidates = [item.model_dump(mode="json") for item in exc.candidates]
            trace.state = RunState.WAITING_TARGET_SELECTION
            trace.target_candidates = candidates
            event = self._target_selection_events.setdefault(trace.run_id, asyncio.Event())
            emitter.emit(
                EventType.TARGET_CANDIDATES_FOUND,
                "应用名称匹配多个候选，等待用户选择",
                {"candidates": candidates, "continuation_token": trace.run_id},
            )
            await event.wait()
            if self._should_stop(trace.run_id):
                raise asyncio.CancelledError
            selected = self._target_selections.get(trace.run_id)
            allowed = {item.bundle_name for item in exc.candidates}
            if selected not in allowed:
                raise ToolExecutionError("selected target is not in the candidate set", RunState.FAILED_TARGET_RESOLUTION)
            trace.target_candidates = []
            resolved = await asyncio.to_thread(
                TargetResolver(device).resolve, request.target.model_copy(update={"app_name": None, "bundle_name": selected})
            )
        except (TargetNotFoundError, ValueError) as exc:
            raise ToolExecutionError(str(exc), RunState.FAILED_TARGET_RESOLUTION) from exc
        except DeviceError as exc:
            raise ToolExecutionError(str(exc), RunState.FAILED_TARGET_RESOLUTION) from exc
        if legacy and resolved.bundle_name == legacy.bundle_name:
            resolved = resolved.model_copy(update={"profile_snapshot": legacy}, deep=True)
        trace.resolved_target = resolved
        trace.target_app_id = resolved.target_app_id
        emitter.emit(EventType.TARGET_RESOLVED, "已解析唯一目标应用", resolved.model_dump(mode="json"))

        if legacy and explicit_resolved is not None:
            trace.state = RunState.PROFILE_REVALIDATING
            emitter.emit(EventType.PROFILE_REVALIDATION_STARTED, "开始复验显式 Profile", {})
            if await asyncio.to_thread(self._quick_revalidate, device, resolved, legacy, trace):
                emitter.emit(
                    EventType.PROFILE_REVALIDATION_FINISHED,
                    "显式 Profile 快速复验通过，按 provisional 模式使用",
                    {"passed": True, "provisional": True},
                )
                trace.provisional = True
                return legacy
            emitter.emit(
                EventType.PROFILE_REVALIDATION_FINISHED,
                "显式 Profile 快速复验失败，转入完整探索",
                {"passed": False},
            )

        existing = registry.get(bundle_name=resolved.bundle_name)
        if existing is not None:
            emitter.emit(EventType.PROFILE_FOUND, "发现 verified Profile，开始快速复验", existing.model_dump(mode="json"))
            trace.state = RunState.PROFILE_REVALIDATING
            emitter.emit(EventType.PROFILE_REVALIDATION_STARTED, "开始 Profile 快速复验", {})
            if await asyncio.to_thread(self._quick_revalidate, device, resolved, existing, trace):
                evidence = dict(existing.provenance.evidence)
                evidence["quick_verification"] = {
                    "passed": True,
                    "checked_at": utc_now().isoformat(),
                    "run_id": trace.run_id,
                }
                existing = existing.model_copy(
                    update={
                        "provenance": existing.provenance.model_copy(
                            update={"evidence": evidence}, deep=True
                        )
                    },
                    deep=True,
                )
                registry.update_verified(existing)
                trace.resolved_target = resolved.model_copy(
                    update={"source": "verified_profile", "profile_snapshot": existing}, deep=True
                )
                emitter.emit(EventType.PROFILE_REVALIDATION_FINISHED, "Profile 快速复验通过", {"passed": True})
                return existing
            emitter.emit(EventType.PROFILE_REVALIDATION_FINISHED, "Profile 快速复验失败，转入完整探索", {"passed": False})
            if not existing.locked:
                registry.invalidate(existing.target_app_id, "quick revalidation failed")

        if not request.exploration_policy.enabled:
            raise ToolExecutionError("no verified Profile and automatic discovery is disabled", RunState.FAILED_DISCOVERY)
        run_dir = self.artifacts.run_dir(trace.run_id)
        trace.state = RunState.PROBING_TARGET
        started = await asyncio.to_thread(
            device.start_app, resolved.bundle_name, resolved.main_ability, resolved.module_name
        )
        if not started.ok:
            raise ToolExecutionError(started.stderr or started.stdout, RunState.FAILED_TARGET_PROBE)
        foreground = await asyncio.to_thread(device.current_foreground_app)
        if not foreground:
            raise ToolExecutionError("launched foreground application could not be determined", RunState.FAILED_TARGET_PROBE)
        if foreground.bundle_name != resolved.bundle_name:
            raise ToolExecutionError("launched foreground bundle does not match resolved target", RunState.FAILED_TARGET_PROBE)
        if foreground.ability_name and foreground.ability_name != resolved.main_ability:
            raise ToolExecutionError("launched foreground Ability does not match resolved target", RunState.FAILED_TARGET_PROBE)
        emitter.emit(EventType.TARGET_STARTED, "目标应用启动探测通过", {"bundle_name": resolved.bundle_name})

        trace.state = RunState.DISCOVERING
        emitter.emit(EventType.DISCOVERY_STARTED, "开始有界自动探索", request.exploration_policy.model_dump(mode="json"))
        def discovery_progress(kind: str, payload: dict[str, object]) -> None:
            event_type = EventType.DISCOVERY_PATH_BLOCKED if kind == "blocked" else EventType.DISCOVERY_PROGRESS
            emitter.emit(event_type, "自动探索进度", payload)

        discovery = await asyncio.to_thread(
            BoundedExplorer(
                device,
                resolved,
                run_dir / "discovery",
                trace.run_id,
                request.exploration_policy,
                progress=discovery_progress,
                should_stop=lambda: self._should_stop(trace.run_id),
            ).explore
        )
        if discovery.stop_reason == "stopped_by_user":
            raise asyncio.CancelledError
        trace.discovery_result = discovery.model_dump(mode="json")
        for index, transition in enumerate(discovery.transitions, 1):
            if transition.command:
                self.artifacts.write_json(
                    run_dir / "commands" / f"discovery-{index:03d}-{transition.action.action_id}.json",
                    transition.command,
                )
        emitter.emit(
            EventType.DISCOVERY_FINISHED,
            "自动探索完成",
            {"pages": len(discovery.pages), "interactions": sorted(discovery.interaction_types), "stop_reason": discovery.stop_reason},
        )

        draft = self._build_profile(resolved, discovery, None, trace.run_id)
        trace.state = RunState.PROFILE_DRAFTING
        draft_path = registry.save_draft(draft)
        self.artifacts.write_json(run_dir / "discovery" / "draft-profile.json", draft)
        emitter.emit(EventType.PROFILE_DRAFT_SAVED, "已保存 draft Profile", {"path": str(draft_path)})
        trace.state = RunState.PROFILE_VERIFYING
        verification = await asyncio.to_thread(
            ProfileVerifier(
                device,
                resolved,
                run_dir / "verification",
                trace.run_id,
                should_stop=lambda: self._should_stop(trace.run_id),
            ).verify,
            discovery,
        )
        trace.verification_result = verification.model_dump(mode="json", exclude={"rounds": {"__all__": {"snapshots"}}})
        for round_result in verification.rounds:
            emitter.emit(
                EventType.PROFILE_VERIFICATION_ROUND_FINISHED,
                f"Profile 验证第 {round_result.round_number} 轮完成",
                round_result.model_dump(mode="json"),
            )
        if not verification.passed:
            failed_draft = draft.model_copy(update={"status": ProfileStatus.INVALID}, deep=True)
            registry.save_draft(failed_draft)
            trace.profile_snapshot = failed_draft
            if request.temporary_test:
                trace.provisional = True
                return failed_draft
            raise ToolExecutionError(
                "; ".join(verification.failures), RunState.FAILED_PROFILE_VERIFICATION
            )
        candidate = self._build_profile(resolved, discovery, verification, trace.run_id)
        candidate_pages = {item.page_signature for item in candidate.stable_locator_inventory}
        if len(candidate.stable_locator_inventory) < 3:
            raise ToolExecutionError(
                "Profile candidate does not contain three stable locators",
                RunState.FAILED_PROFILE_VERIFICATION,
            )
        if len(candidate_pages) < 3:
            raise ToolExecutionError(
                "Profile candidate does not contain stable locators on three pages",
                RunState.FAILED_PROFILE_VERIFICATION,
            )
        if len(candidate.assertion_inventory) < 2:
            raise ToolExecutionError(
                "Profile candidate does not contain two application-level assertions",
                RunState.FAILED_PROFILE_VERIFICATION,
            )
        registry.save_candidate(candidate)
        trace.profile_snapshot = candidate.model_copy(deep=True)
        validation_trace = self._profile_validation_trace(trace, candidate, discovery, verification)
        validation_generator = HypiumGenerator(self.artifacts)
        trace.state = RunState.SCRIPT_GENERATING
        trace.profile_validation_generated = await asyncio.to_thread(validation_generator.generate, validation_trace)
        emitter.emit(
            EventType.SCRIPT_GENERATED,
            "已从 Profile 验证证据生成 Hypium Driver 用例",
            trace.profile_validation_generated.model_dump(mode="json"),
        )
        if self._should_stop(trace.run_id):
            raise asyncio.CancelledError
        trace.state = RunState.SCRIPT_EXECUTING
        runner = HypiumRunner(self.settings.resolved_runtime_home)
        trace.profile_validation_replays = await self._run_replays(
            runner, trace.profile_validation_generated, trace.run_id, 3
        )
        for replay in trace.profile_validation_replays:
            emitter.emit(EventType.HYPIUM_REPLAY_FINISHED, f"Profile Hypium 回放第 {replay.attempt} 次完成", replay.model_dump(mode="json"))
        if not all(item.passed for item in trace.profile_validation_replays):
            raise ToolExecutionError("Profile Hypium replay gate failed", RunState.FAILED_SCRIPT)
        trace.state = RunState.PROFILE_PROMOTING
        replay_ids = [f"{trace.run_id}:profile-attempt-{item.attempt}" for item in trace.profile_validation_replays]
        try:
            verified_path = registry.promote(candidate, replay_run_ids=replay_ids)
        except Exception as exc:
            raise ToolExecutionError(str(exc), RunState.FAILED_PROFILE_PROMOTION) from exc
        promoted = self.artifacts.load_profile(verified_path)

        trace.profile_snapshot = promoted
        trace.resolved_target = resolved.model_copy(update={"profile_snapshot": promoted}, deep=True)
        emitter.emit(EventType.PROFILE_PROMOTED, "Profile 已自动晋级为 verified", {"target_app_id": promoted.target_app_id})
        return promoted

    @staticmethod
    def _profile_validation_trace(trace, profile, discovery, verification) -> RunTrace:
        """Build the admission replay from the same three-page discovery path."""
        validation_id = f"{trace.run_id}-profile-validation"
        validation = RunTrace(
            run_id=validation_id,
            target_app_id=profile.target_app_id,
            task="Profile admission validation",
            device_id=trace.device_id,
            model_used=trace.model_used,
            model_mock=trace.model_mock,
            target_query=trace.target_query,
            resolved_target=trace.resolved_target.model_copy(update={"profile_snapshot": profile}, deep=True),
            profile_status_at_start=ProfileStatus.CANDIDATE,
            profile_snapshot=profile,
            exploration_policy=trace.exploration_policy,
            discovery_result=trace.discovery_result,
            verification_result=trace.verification_result,
        )
        core_pages = ProfileVerifier._core_pages(discovery)
        if len(core_pages) < 3:
            raise ValueError("Profile admission replay requires a contiguous three-page core flow")
        validation.actions.append(
            ActionResult(
                step_id="profile-open-app",
                tool=ToolName.OPEN_APP,
                params={"target": profile.bundle_name},
                success=True,
            )
        )
        locators_by_page: dict[str, list[StableLocator]] = {}
        for locator in profile.stable_locator_inventory:
            locators_by_page.setdefault(locator.page_signature, []).append(locator)
        assertions_by_page = {
            item.page_signature: item for item in profile.assertion_inventory
        }
        for page_index, page in enumerate(core_pages):
            if page_index:
                action = page.path_actions[-1]
                tool = {
                    "click": ToolName.CLICK_ELEMENT,
                    "input": ToolName.CLICK_COORDINATE,
                    "swipe": ToolName.SWIPE,
                    "back": ToolName.BACK,
                }[action.kind]
                params: dict[str, object] = {"target": action.element_id or action.target_text}
                if action.coordinate:
                    params["coordinate"] = action.coordinate
                if action.kind == "input":
                    params["text"] = discovery.policy.fixed_input_text
                if action.kind == "swipe":
                    params["direction"] = action.direction or "up"
                action_locator = (
                    LocatorCandidate(kind=LocatorKind(action.locator_kind), value=action.locator_value)
                    if action.locator_kind != "coordinate" and action.locator_value
                    else None
                )
                if action.kind == "input" and action.coordinate:
                    validation.actions.append(
                        ActionResult(
                            step_id=f"profile-flow-{page_index:02d}-{action.action_id}-focus",
                            tool=ToolName.CLICK_COORDINATE,
                            params={"coordinate": action.coordinate},
                            success=True,
                        )
                    )
                    tool = ToolName.INPUT_TEXT
                validation.actions.append(
                    ActionResult(
                        step_id=f"profile-flow-{page_index:02d}-{action.action_id}",
                        tool=tool,
                        params=params,
                        success=True,
                        locator=action_locator,
                    )
                )
            locator = next(iter(locators_by_page.get(page.signature, [])), None)
            if locator is None:
                locator = None
            if locator is None:
                continue
            kind, value = AgentOrchestrator._profile_locator(locator)
            assertion = assertions_by_page.get(page.signature)
            validation.actions.append(
                ActionResult(
                    step_id=f"profile-page-{page_index + 1:02d}-assert",
                    tool=ToolName.ASSERT_VISIBLE,
                    params={"target": assertion.target if assertion else locator.name},
                    success=True,
                    assertion=AssertionResult(
                        kind=ToolName.ASSERT_VISIBLE,
                        target=assertion.target if assertion else locator.name,
                        passed=True,
                        message=f"verified application page {page.page_id}",
                    ),
                    locator=LocatorCandidate(kind=kind, value=value),
                )
            )
        validation.assertions = [item.assertion for item in validation.actions if item.assertion]
        covered_pages = {
            action.assertion.message.rsplit(" ", 1)[-1]
            for action in validation.actions
            if action.assertion
            and action.assertion.message.startswith("verified application page ")
        }
        if len(covered_pages) < 3 or len(validation.assertions) < 2:
            raise ValueError(
                "Profile admission replay requires 3 page checks and 2 application assertions"
            )
        validation.snapshots = [
            snapshot.model_copy(update={"run_id": validation_id}, deep=True)
            for round_result in verification.rounds
            for snapshot in round_result.snapshots
        ]
        validation.graph.nodes = []
        validation.discovery_result = {
            **(trace.discovery_result or {}),
            "admission_pages": [item.page_id for item in core_pages],
            "admission_actions": [
                item.model_dump(mode="json")
                for item in (core_pages[-1].path_actions if core_pages else [])
            ],
        }
        return validation
    @staticmethod
    def _profile_locator(locator: StableLocator) -> tuple[LocatorKind, str]:
        if locator.key:
            return LocatorKind.KEY, locator.key
        if locator.id:
            return LocatorKind.ID, locator.id
        if locator.type and locator.text:
            return LocatorKind.TYPE_TEXT, f"{locator.type}|{locator.text}"
        return LocatorKind.TEXT, locator.text

    def _quick_revalidate(
        self,
        device: DeviceAdapter,
        resolved: ResolvedTarget,
        profile: TargetAppProfile,
        trace: RunTrace,
    ) -> bool:
        if profile.app_version.version_code is not None and resolved.version_code != profile.app_version.version_code:
            return False
        if profile.main_ability != resolved.main_ability:
            return False
        if (
            profile.app_version.signature_sha256
            and resolved.signature_sha256 != profile.app_version.signature_sha256
        ):
            return False
        if type(device).stop_app is DeviceAdapter.stop_app or type(device).start_app is DeviceAdapter.start_app:
            started = device.open_app(profile, reset=True)
        else:
            stopped = device.stop_app(resolved.bundle_name)
            started = device.start_app(
                resolved.bundle_name, resolved.main_ability, resolved.module_name
            )
            if not stopped.ok:
                return False
        if not started.ok:
            return False
        output_dir = (
            Path(trace.snapshots[0].image_path).parent
            if trace.snapshots
            else self.artifacts.run_dir(trace.run_id) / "revalidation"
        )
        recovery_actions = profile.reset_strategy.get("recovery_actions") or []
        pages = (profile.core_flows[0].get("pages", []) if profile.core_flows else [])[:3]
        if len(pages) < 3 or not recovery_actions:
            return self._legacy_entry_revalidate(device, resolved, profile, trace, output_dir)
        locator_inventory = [
            item
            for item in profile.stable_locator_inventory
            if item.observed_rounds >= 3
            and item.unique_match_rounds >= 3
            and item.evidence_snapshot_ids
        ]
        page_locators = {
            page: next(
                (item for item in locator_inventory if item.page_signature == page),
                None,
            )
            for page in pages
        }
        if any(locator is None for locator in page_locators.values()):
            return False
        for page_index, page_signature in enumerate(pages):
            foreground = device.current_foreground_app()
            if (
                foreground is None
                or foreground.bundle_name != resolved.bundle_name
                or (foreground.ability_name and foreground.ability_name != resolved.main_ability)
            ):
                return False
            snapshot = device.screenshot(
                output_dir, trace.run_id, f"revalidate-{page_index:02d}"
            )
            if not self._snapshot_has_locator(snapshot, page_locators[page_signature]):
                return False
            if page_index < len(pages) - 1:
                if page_index >= len(recovery_actions):
                    return False
                if not self._replay_profile_action(
                    device, recovery_actions[page_index], snapshot, profile
                ):
                    return False
                device.wait(0.5)
        return True

    @staticmethod
    def _legacy_entry_revalidate(device, resolved, profile, trace, output_dir: Path) -> bool:
        foreground = device.current_foreground_app()
        if (
            foreground is None
            or foreground.bundle_name != resolved.bundle_name
            or (foreground.ability_name and foreground.ability_name != resolved.main_ability)
        ):
            return False
        snapshot = device.screenshot(output_dir, trace.run_id, "revalidate")
        locators = profile.stable_locator_inventory[:3]
        return bool(locators) and all(
            AgentOrchestrator._snapshot_has_locator(snapshot, locator)
            for locator in locators
        )

    @staticmethod
    def _snapshot_has_locator(snapshot: ScreenSnapshot, locator: StableLocator) -> bool:
        return any(
            (locator.key and locator.key == item.key)
            or (locator.id and locator.id == item.id)
            or (
                locator.text
                and locator.text == item.content
                and (not locator.type or locator.type == item.type)
            )
            for item in snapshot.elements
        )

    @staticmethod
    def _replay_profile_action(
        device,
        raw_action: object,
        snapshot: ScreenSnapshot,
        profile: TargetAppProfile,
    ) -> bool:
        if not isinstance(raw_action, dict):
            return False
        try:
            action = ExplorationAction.model_validate(raw_action)
            target = ResolvedTarget(
                target_app_id=profile.target_app_id,
                display_name=profile.display_name,
                bundle_name=profile.bundle_name,
                main_ability=profile.main_ability,
                module_name=profile.module_name,
                device_id=getattr(device, "device_id", "unknown"),
                source="verified_profile",
            )
            action = BoundedExplorer(
                device,
                target,
                Path(snapshot.image_path).parent,
                snapshot.run_id,
            ).resolve_replay_action(action, snapshot)
        except (DeviceError, ValueError):
            return False
        coordinate = action.coordinate
        if action.kind == "click" and coordinate:
            result = device.click(*coordinate)
        elif action.kind == "input" and coordinate:
            result = device.input_text("OpenHarmony", *coordinate)
        elif action.kind == "swipe":
            result = device.swipe(
                (snapshot.width // 2, int(snapshot.height * 0.75)),
                (snapshot.width // 2, int(snapshot.height * 0.25)),
            )
        elif action.kind == "back":
            result = device.back()
        else:
            return False
        return result.ok
    @staticmethod
    def _build_profile(resolved, discovery, verification, run_id: str) -> TargetAppProfile:
        locators: list[StableLocator] = []
        assertions: list[AssertionDefinition] = []
        if verification is not None:
            for item in verification.stability.locators:
                values = {"key": "", "id": "", "text": "", "type": ""}
                if item.kind == LocatorKind.TYPE_TEXT:
                    values["type"], _, values["text"] = item.value.partition("|")
                elif item.kind in {LocatorKind.KEY, LocatorKind.ID, LocatorKind.TEXT}:
                    values[item.kind.value] = item.value
                locators.append(StableLocator(
                    name=item.name,
                    page_signature=item.page_signatures[0] if item.page_signatures else "",
                    confidence=ConfidenceLevel.HIGH if item.level == StabilityLevel.HIGH else ConfidenceLevel.MEDIUM,
                    observed_rounds=len(item.rounds),
                    unique_match_rounds=len(item.rounds) if item.unique_each_round else 0,
                    evidence_snapshot_ids=[
                        round_result.snapshot_ids[0]
                        for round_result in verification.rounds
                        if round_result.snapshot_ids
                        and any(
                            observation.value == item.value
                            for observation in round_result.locator_observations
                        )
                    ],
                    **values,
                ))
            for index, item in enumerate(verification.stability.assertions, 1):
                assertions.append(
                    AssertionDefinition(
                        name=f"assertion-{index}",
                        kind=item.kind,
                        target=item.target,
                        page_signature=item.page_signatures[0] if item.page_signatures else "",
                        observed_rounds=len(item.rounds),
                        evidence_snapshot_ids=[
                            round_result.snapshot_ids[0]
                            for round_result in verification.rounds
                            if round_result.snapshot_ids
                            and any(
                                observation.target == item.target and observation.passed
                                for observation in round_result.assertion_observations
                            )
                        ],
                    )
                )
        return TargetAppProfile(
            status=ProfileStatus.CANDIDATE if verification and verification.passed else ProfileStatus.DRAFT,
            target_app_id=resolved.target_app_id,
            display_name=resolved.display_name,
            bundle_name=resolved.bundle_name,
            main_ability=resolved.main_ability,
            module_name=resolved.module_name,
            app_version=AppVersion(version_name=resolved.version_name, version_code=resolved.version_code, signature_sha256=resolved.signature_sha256),
            device_compatibility=DeviceCompatibility(
                validated_device_types=["phone"],
                validated_resolutions=sorted(
                    {item.resolution for item in verification.rounds if item.resolution}
                ) if verification else [],
            ),
            launch_strategy={"kind": "hdc_aa_start", "command_template": "aa start -b {bundle_name} -a {main_ability}"},
            reset_strategy={
                "kind": "stop_start_then_navigation_restore",
                "clear_app_data": False,
                "recovery_actions": [
                    action.model_dump(mode="json")
                    for action in (
                        ProfileVerifier._core_pages(discovery)[-1].path_actions
                        if ProfileVerifier._core_pages(discovery)
                        else []
                    )
                ],
            },
            test_data_strategy={"fixed_input_text": discovery.policy.fixed_input_text, "secrets": []},
            permission_and_popup_strategy={
                "login": "explicit_only", "permission": "explicit_only", "submit": "explicit_only",
                "publish": "explicit_only", "download": "explicit_only", "payment": "always_blocked",
                "delete": "always_blocked", "uninstall": "always_blocked", "clear_data": "always_blocked",
            },
            stable_locator_inventory=locators,
            assertion_inventory=assertions,
            known_limitations=[
                "coordinate fallbacks remain resolution-bound and do not count toward Profile admission"
            ] if verification and any(item.level == StabilityLevel.LOW for item in verification.stability.locators) else [],
            core_flows=[{
                "pages": [page.signature for page in ProfileVerifier._core_pages(discovery)],
                "page_ids": [page.page_id for page in ProfileVerifier._core_pages(discovery)],
                "steps": [
                    action.model_dump(mode="json")
                    for action in (
                        ProfileVerifier._core_pages(discovery)[-1].path_actions
                        if ProfileVerifier._core_pages(discovery)
                        else []
                    )
                ],
                "interaction_types": sorted(discovery.interaction_types),
            }],
            provenance=ProfileProvenance(
                discovery_run_id=run_id,
                evidence={
                    "identity": "discovery/resolved-target.json",
                    "application_version": "discovery/resolved-target.json",
                    "launch_strategy": "discovery/resolved-target.json",
                    "policy": "discovery/policy.json",
                    "pages": "discovery/pages.json",
                    "core_flows": "discovery/transitions.json",
                    "blocked_actions": "discovery/blocked-actions.json",
                    "stable_locators": "verification/profile-verification.json",
                    "assertions": "verification/profile-verification.json",
                    "verification_passed": bool(verification and verification.passed),
                    "cross_bundle_violations": sum(
                        1
                        for transition in discovery.transitions
                        if transition.blocked_reason
                        and "cross-bundle" in transition.blocked_reason
                    ),
                    "cross_bundle_recovery_failed": False,
                },
            ),
        )

    async def _run_replays(self, runner, generated, run_id: str, attempts: int):
        """Execute replay attempts independently and honor stop requests between attempts."""
        results = []
        for attempt in range(1, attempts + 1):
            if self._should_stop(run_id):
                raise asyncio.CancelledError
            results.append(await asyncio.to_thread(runner.execute, generated, attempt))
        return results

    async def _capture(self, trace, emitter, device, perception, graph, label):
        run_dir = self.artifacts.run_dir(trace.run_id)
        snapshot = await asyncio.to_thread(device.screenshot, run_dir / "screens", trace.run_id, label)
        try:
            observation = await asyncio.wait_for(
                self.provider.analyze(snapshot),
                timeout=self.settings.agent_model_timeout,
            )
        except Exception as exc:
            if not self.provider.mock:
                message = f"vision analysis failed: {type(exc).__name__}: {exc}"
                raise ToolExecutionError(message, RunState.FAILED_MODEL) from exc
            observation = None
            snapshot.summary = f"mock vision analysis unavailable: {type(exc).__name__}: {exc}"
        snapshot = perception.merge(snapshot, observation)
        trace.snapshots.append(snapshot)
        emitter.emit(
            EventType.SCREEN_CAPTURED,
            "已采集本地截图",
            {
                "snapshot_id": snapshot.snapshot_id,
                "image_path": str(snapshot.image_path),
                "width": snapshot.width,
                "height": snapshot.height,
                "sha256": snapshot.image_sha256,
            },
        )
        emitter.emit(
            EventType.ELEMENTS_DETECTED,
            f"识别到 {len(snapshot.elements)} 个元素",
            {"snapshot_id": snapshot.snapshot_id, "count": len(snapshot.elements)},
        )
        node, created = graph.add_snapshot(snapshot)
        if created:
            emitter.emit(EventType.PAGE_DISCOVERED, "发现新页面状态", node.model_dump(mode="json"))
        return snapshot, node

    async def _execute_with_retry(self, executor, step_id, decision, snapshot):
        last_error = None
        for attempt in range(self.settings.agent_retry_limit + 1):
            try:
                assert executor is not None
                return await asyncio.wait_for(
                    asyncio.to_thread(executor.execute, step_id, decision, snapshot),
                    timeout=self.settings.agent_action_timeout,
                )
            except TimeoutError as exc:
                last_error = ToolExecutionError(
                    f"tool action timed out after {self.settings.agent_action_timeout} seconds"
                )
                if attempt >= self.settings.agent_retry_limit:
                    raise last_error from exc
                await asyncio.sleep(min(0.5 * (attempt + 1), 1.5))
            except ToolExecutionError as exc:
                last_error = exc
                non_retryable = exc.state in {RunState.FAILED_ASSERTION, RunState.FAILED_ELEMENT}
                if non_retryable or attempt >= self.settings.agent_retry_limit:
                    raise
                await asyncio.sleep(min(0.5 * (attempt + 1), 1.5))
        raise last_error or ToolExecutionError("tool execution failed")

    @staticmethod
    def _constrain_finish_decision(step: PlannedStep, decision: ToolDecision) -> ToolDecision:
        planned_finish = step.tool == ToolName.FINISH
        premature_finish = decision.tool == ToolName.FINISH and not planned_finish
        missing_finish = planned_finish and decision.tool != ToolName.FINISH
        if not (premature_finish or missing_finish):
            return decision

        original_tool = decision.tool
        updates: dict[str, object] = {
            "tool": step.tool,
            "reasoning": (
                f"execution framework replaced {original_tool} with planned tool {step.tool}; "
                "finish is only valid for the planned finish step"
            ),
        }
        for field in ("target", "text", "coordinate", "direction", "wait_seconds"):
            if getattr(decision, field) is None and (planned_value := getattr(step, field)) is not None:
                updates[field] = planned_value
        return decision.model_copy(update=updates)

    def _save_action_command(self, run_id: str, result: ActionResult) -> None:
        if result.command:
            path = self.artifacts.run_dir(run_id) / "commands" / f"{result.step_id}_{result.tool}.json"
            self.artifacts.write_json(path, result.command)

    def _should_stop(self, run_id: str) -> bool:
        if run_id in self._stop_requested:
            return True
        stored = self.repository.get_trace(run_id)
        return bool(stored and stored.state == RunState.STOPPED_BY_USER)

    async def _fail(self, trace, emitter, state: RunState, message: str) -> None:
        trace.state = state
        trace.error = message
        trace.ended_at = utc_now()
        event_type = EventType.ASSERTION_FAILED if state == RunState.FAILED_ASSERTION else EventType.RUN_FAILED
        emitter.emit(event_type, message, {"state": state})
