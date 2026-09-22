"""串联规划、设备操作、页面感知、产物生成与回放的任务编排器。"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..analysis.in_run import MUTATING_TOOLS
from ..config import Settings
from ..devices import DeviceAdapter, DeviceError, HarmonyDeviceAdapter
from ..discovery import (
    BoundedExplorer,
    ExplorationAction,
    ProfileVerifier,
    StabilityAnalyzer,
    StabilityLevel,
    dynamic_identifier_pattern,
    is_dynamic_identifier,
    is_volatile_evidence_key,
)
from ..discovery.advisor import ExplorationAdvisor
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
    ExplorationPolicy,
    GeneratedArtifact,
    LocatorCandidate,
    LocatorKind,
    PlannedStep,
    PlanResult,
    ProfileProvenance,
    ProfileStatus,
    ResolvedTarget,
    RunEvent,
    RunRequest,
    RunState,
    RunTrace,
    ScenarioKind,
    ScreenSnapshot,
    StableLocator,
    StepHistoryEntry,
    TargetAppProfile,
    ToolDecision,
    ToolName,
    VisionObservation,
    WorkaroundRecord,
    utc_now,
)
from ..perception import PerceptionService
from ..perception.normalizer import normalize_layout, page_path
from ..profiles import ProfileRegistry
from ..profiles.admission import (
    AdmissionEvidence,
    AdmissionThresholds,
    describe_admission_failure,
    evaluate_admission,
    order_admission_failures,
    pick_first_failure,
)
from ..reporting import ReportBuilder
from ..runner import make_hypium_runner
from ..runtime import LaunchSpec, RunEventEmitter, SafetyError, SafetyPolicy, ToolExecutionError, ToolExecutor
from ..storage import ArtifactStore, RunRepository
from ..targets import ForegroundApp, TargetAmbiguousError, TargetNotFoundError, TargetResolver
from .providers import AgentProvider, PlanningContext, create_provider

logger = logging.getLogger(__name__)

DeviceFactory = Callable[[str], DeviceAdapter]

# 瞬时 provider 故障特征：网关限流/上游暂时不可用/5xx。真机实测 commandcode 网关会间歇性
# 返回 429 "Upstream model provider is temporarily unavailable"，一次命中就会报废整轮运行
# （两次日历运行分别在第 1 次观测调用与规划调用上被 429 打断），退避重试的成本远低于重跑。
_TRANSIENT_MODEL_ERROR_TOKENS = (
    "429",
    "rate_limit",
    "rate limit",
    "temporarily unavailable",
    "overloaded",
    "502",
    "503",
    "504",
    "connection reset",
    "connection aborted",
    "read timed out",
)


def _is_transient_model_error(exc: Exception) -> bool:
    """判断模型调用异常是否属于「等一会儿再试就好」的瞬时故障。"""
    text = f"{type(exc).__name__}: {exc}".casefold()
    return any(token in text for token in _TRANSIENT_MODEL_ERROR_TOKENS)


def _default_in_run_detector_factory(settings: Settings) -> Callable[[DeviceAdapter, str], Any] | None:
    """按配置构造运行中检测器工厂；总开关关闭时返回 ``None``（行为与今天完全一致）。"""
    if not getattr(settings, "analysis_in_run_detection", True):
        return None
    from ..analysis.in_run import InRunDetector, InRunThresholds

    thresholds = InRunThresholds(
        escalate_count=int(getattr(settings, "unresponsive_escalate_count", 2)),
        probe_enabled=bool(getattr(settings, "analysis_in_run_probe", True)),
        screen_scan_enabled=bool(getattr(settings, "analysis_in_run_screen_scan", True)),
    )

    def factory(device: DeviceAdapter, bundle_name: str) -> InRunDetector:
        return InRunDetector(device, bundle_name=bundle_name, thresholds=thresholds)

    return factory


@dataclass(frozen=True, slots=True)
class HarvestedLocator:
    """任务期回收的一条定位器证据：候选 + 其出现页面的结构身份 + 证据帧。"""

    candidate: LocatorCandidate
    page_signature: str
    snapshot_id: str


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
        launch_settle_seconds: float = 3.0,
        case_library: Any | None = None,
        analyzer: Any | None = None,
        in_run_detector_factory: Callable[[DeviceAdapter, str], Any] | None = None,
        defect_recorder: Any | None = None,
    ):
        self.settings = settings
        self.provider = provider or create_provider(settings)
        self.repository = repository or RunRepository(settings.resolved_database_path)
        self.artifacts = artifacts or ArtifactStore(settings.resolved_runtime_dir)
        self.device_factory = device_factory or self._default_device_factory
        self.event_callback = event_callback
        self.settle_seconds = settle_seconds
        self.launch_settle_seconds = launch_settle_seconds
        # 用例库、执行分析与运行中检测均为可选注入：不注入时行为与今天完全一致，
        # 单测因此不会把产物写进仓库 artifacts/，也不会多发设备调用。
        self.case_library = case_library
        self.analyzer = analyzer
        self.in_run_detector_factory = in_run_detector_factory or _default_in_run_detector_factory(settings)
        self.defect_recorder = defect_recorder
        self._stop_requested: set[str] = set()
        self._target_selections: dict[str, str] = {}
        self._target_selection_events: dict[str, asyncio.Event] = {}
        # 计划 5.1/5.4：合并视觉请求的决策缓存、已观测帧与跨步历史所需的观测缓存。
        self._pending_decisions: dict[tuple[str, str], ToolDecision] = {}
        self._observed_snapshots: set[str] = set()
        self._observation_cache: dict[str, VisionObservation] = {}

    def _default_device_factory(self, device_id: str) -> DeviceAdapter:
        return HarmonyDeviceAdapter(device_id, self.settings.hdc_path, self.settings.agent_action_timeout)

    async def _plan_for_request(
        self,
        request: RunRequest,
        context: PlanningContext,
        step_limit: int,
    ) -> PlanResult:
        """选择规划路径：缺陷复现走专用 planner，其余走既有 planner。

        ``BUG_REPRODUCTION`` 是唯一改变 Live 执行路径的场景（计划 C1）：
        ``EXPLORATORY`` / ``SMOKE`` / ``CORE_FLOW`` 只作为标签透传给用例 IR。
        """
        if request.scenario == ScenarioKind.BUG_REPRODUCTION and request.bug_report is not None:
            planner = getattr(self.provider, "plan_bug_repro", None)
            if planner is not None:
                result = planner(request.bug_report, context, step_limit)
                if inspect.isawaitable(result):
                    result = await result
                if result is not None:
                    return _plan_from_bug_repro(result, request)
        return await self.provider.plan(request.task, context, step_limit)

    async def _plan_with_transient_retry(
        self,
        context: PlanningContext,
        request: RunRequest,
        step_limit: int,
    ) -> PlanResult:
        """规划调用套上超时与瞬时故障重试：429 打断规划会让整轮运行零动作。"""
        timeout = self.settings.agent_model_timeout
        retries = max(int(self.settings.agent_model_retry_limit), 0)
        for attempt in range(retries + 1):
            try:
                return await asyncio.wait_for(
                    self._plan_for_request(request, context, step_limit),
                    timeout=timeout,
                )
            except Exception as exc:  # noqa: BLE001 - 失败一律上抛，由调用方转 FAILED_MODEL
                if _is_transient_model_error(exc) and attempt < retries:
                    await self._wait_before_model_retry("planning", exc)
                    continue
                raise

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
        # 请求显式给出的探索字段优先；未给出的按 Settings 的 bootstrap 预算收紧（计划 4.2）。
        request = request.model_copy(
            update={"exploration_policy": self._bootstrap_policy(request.exploration_policy)}, deep=True
        )
        device_id = request.device_id or self.settings.harmony_device
        requested_id = request.target_app_id or request.target.bundle_name or request.target.app_name
        trace = RunTrace(
            run_id=run_id,
            target_app_id=requested_id,
            target_query=request.target,
            exploration_policy=request.exploration_policy,
            task=request.task,
            mode=request.mode,
            scenario=request.scenario or _scenario_for(request),
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
        in_run_detector: Any | None = None

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
            trace.target_app_id = profile.target_app_id if profile else trace.target_app_id
            if profile is not None:
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
            resolved = trace.resolved_target
            if resolved is None:  # pragma: no cover - _prepare_target guarantees resolution
                raise ToolExecutionError("target resolution missing after profile preparation", RunState.FAILED_ACTION)
            launch = (
                LaunchSpec.from_profile(profile)
                if profile
                else LaunchSpec.from_kwargs(resolved.bundle_name, resolved.main_ability, resolved.module_name)
            )
            executor = ToolExecutor(
                device=device,
                launch=launch,
                safety=SafetyPolicy(exploration_policy=request.exploration_policy),
                stable_locators=list(profile.stable_locator_inventory) if profile else [],
            )
            executor.safety.validate_task(request.task)
            trace.phase = "task"
            # 运行中即时检测（缺口 2）：设备已连接、身份已确定，构造检测器。
            # 未注入工厂（单测默认 / 总开关关闭）时为 None，行为与今天完全一致。
            if self.in_run_detector_factory is not None:
                try:
                    in_run_detector = self.in_run_detector_factory(device, launch.bundle_name)
                    in_run_detector.run_dir = self.artifacts.run_dir(trace.run_id)
                except Exception as exc:  # noqa: BLE001 - 检测是 advisory，构造失败只降级
                    logger.warning("in-run detector unavailable: %s: %s", type(exc).__name__, exc)
            emitter.emit(EventType.ORIGINAL_TASK_STARTED, "开始执行用户原始测试任务", {"task": request.task})

            trace.state = RunState.PLANNING
            try:
                step_limit = min(request.max_steps, self.settings.agent_max_steps)
                planning_context = (
                    PlanningContext.from_profile(profile) if profile else PlanningContext.from_resolved(resolved)
                )
                plan = await self._plan_with_transient_retry(planning_context, request, step_limit)
            except Exception as exc:
                await self._fail(trace, emitter, RunState.FAILED_MODEL, f"planning failed: {exc}", request)
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
                    trace.agent_outcome = "stopped"
                    trace.ended_at = utc_now()
                    emitter.emit(EventType.RUN_FINISHED, "任务已由用户停止")
                    return trace
                if index > min(request.max_steps, self.settings.agent_max_steps):
                    raise ToolExecutionError("maximum step count reached", RunState.FAILED_ACTION)

                if current_snapshot is None:
                    current_snapshot, current_node = await self._capture(
                        trace, emitter, device, perception, graph, f"step_{index:02d}_before", step=step
                    )
                decision, result, current_snapshot = await self._decide_and_execute(
                    trace, emitter, executor, perception, graph, device, step, current_snapshot
                )
                before = current_snapshot
                result.before_snapshot_id = before.snapshot_id if before else None

                if decision.tool == ToolName.FINISH:
                    trace.state = RunState.VERIFYING
                    after, after_node = await self._capture(
                        trace,
                        emitter,
                        device,
                        perception,
                        graph,
                        f"step_{index:02d}_after",
                        with_vision=not self._combined_vision_active(),
                    )
                    result.before_snapshot_id = before.snapshot_id if before else None
                    result.after_snapshot_id = after.snapshot_id
                    trace.actions.append(result)
                    current_snapshot, current_node = after, after_node
                    emitter.emit(EventType.ACTION_FINISHED, "Agent 已结束工具循环", result.model_dump(mode="json"))
                    break

                if decision.tool == ToolName.OPEN_APP:
                    await self._wait_launch_settled(device, trace)
                elif self.settle_seconds and decision.tool not in {ToolName.INSPECT_SCREEN, ToolName.WAIT}:
                    await asyncio.sleep(self.settle_seconds)
                trace.state = RunState.VERIFYING
                after, after_node = await self._capture(
                    trace,
                    emitter,
                    device,
                    perception,
                    graph,
                    f"step_{index:02d}_after",
                    with_vision=not self._combined_vision_active(),
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

                if before and decision.tool in MUTATING_TOOLS and in_run_detector is not None:
                    await self._detect_in_run_anomalies(
                        trace, emitter, in_run_detector, result, step, before, after, decision
                    )

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

            if (trace.provisional or trace.live_mode) and not self.settings.auto_execute_on_first_run:
                # 脚本现在**立即可执行**（live_mode 只影响 confidence，不再阻断执行与入库），
                # 但自动回放失败会把整个 run 判为 FAILED_SCRIPT（见 _finalize_artifacts 末段），
                # 首次运行不应因自动回放而失败：默认关掉自动回放，用户点一次「验收回放」即可；
                # settings.auto_execute_on_first_run=True 可恢复「生成即自动验收」的旧行为。
                request.auto_execute = False
            await self._finalize_artifacts(trace, emitter, request, allow_replay=True)

            trace.state = RunState.COMPLETED
            trace.agent_outcome = "completed"
            trace.agent_error = None
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
            await self._fail(trace, emitter, RunState.FAILED_ACTION, str(exc), request)
            return trace
        except DeviceError as exc:
            await self._fail(trace, emitter, RunState.FAILED_DEVICE, str(exc), request)
            return trace
        except ToolExecutionError as exc:
            await self._fail(trace, emitter, exc.state, str(exc), request)
            return trace
        except Exception as exc:
            await self._fail(
                trace, emitter, RunState.FAILED_ACTION, f"unexpected error: {type(exc).__name__}: {exc}", request
            )
            return trace
        finally:
            self._target_selection_events.pop(run_id, None)
            self._target_selections.pop(run_id, None)
            await asyncio.to_thread(device.close)
            self.repository.save_trace(trace)
            self.artifacts.save_trace(trace)
            ReportBuilder(self.artifacts).build(trace)

    async def _prepare_target(self, request, trace, emitter, device) -> TargetAppProfile | None:
        """Resolve、复验或探索晋级 Profile；实时模式降级时返回 None。"""
        explicit_path = self.settings.resolved_target_profile_path
        registry = self._registry(request.exploration_policy.min_interaction_kinds)
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
                raise asyncio.CancelledError from exc
            selected = self._target_selections.get(trace.run_id)
            allowed = {item.bundle_name for item in exc.candidates}
            if selected not in allowed:
                raise ToolExecutionError(
                    "selected target is not in the candidate set", RunState.FAILED_TARGET_RESOLUTION
                ) from exc
            trace.target_candidates = []
            resolved = await asyncio.to_thread(
                TargetResolver(device).resolve,
                request.target.model_copy(update={"app_name": None, "bundle_name": selected}),
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

        existing = registry.get_any(bundle_name=resolved.bundle_name)
        if existing is not None and existing.status == ProfileStatus.VERIFIED:
            emitter.emit(
                EventType.PROFILE_FOUND, "发现 verified Profile，开始快速复验", existing.model_dump(mode="json")
            )
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
                    update={"provenance": existing.provenance.model_copy(update={"evidence": evidence}, deep=True)},
                    deep=True,
                )
                registry.update_verified(existing)
                trace.resolved_target = resolved.model_copy(
                    update={"source": "verified_profile", "profile_snapshot": existing}, deep=True
                )
                emitter.emit(EventType.PROFILE_REVALIDATION_FINISHED, "Profile 快速复验通过", {"passed": True})
                return existing
            emitter.emit(
                EventType.PROFILE_REVALIDATION_FINISHED,
                "Profile 快速复验失败，保留既有 Profile 并转入完整探索重新验证",
                {"passed": False, "kept": True},
            )
            # 复验失败不销毁 verified Profile：单次失败可能来自内容抖动或临时环境问题，
            # 完整探索成功晋级时 promote 会自然覆盖旧文件；手动失效仍走 Profile 页操作。
            evidence = dict(existing.provenance.evidence)
            evidence["quick_verification"] = {
                "passed": False,
                "checked_at": utc_now().isoformat(),
                "run_id": trace.run_id,
            }
            existing = existing.model_copy(
                update={"provenance": existing.provenance.model_copy(update={"evidence": evidence}, deep=True)},
                deep=True,
            )
            registry.update_verified(existing)
        elif (
            existing is not None and existing.stable_locator_inventory and self._profile_compatible(resolved, existing)
        ):
            # 计划 3.1：candidate/draft 只要有定位器就直接复用，不再重新做完整 bootstrap 探索。
            # 本次任务期采到的证据会在收尾时累加回这份 Profile（Phase 3.3）。
            return self._enter_incremental_mode(trace, emitter, existing)

        if not request.exploration_policy.enabled:
            if request.bootstrap_only:
                raise ToolExecutionError(
                    "no verified Profile and automatic discovery is disabled", RunState.FAILED_DISCOVERY
                )
            return self._enter_live_mode(
                trace, emitter, "未发现 verified Profile 且自动探索已关闭，进入实时模式执行任务"
            )
        if not request.bootstrap_only and not self.settings.bootstrap_enabled_on_task_run:
            # 计划 4.2：任务型运行默认不前置完整探索——本次日历任务为此白烧 8.8 分钟（38% 墙钟）。
            # Profile 由任务期证据回收（Phase 3.3）作为副产物建立；完整探索通过
            # POST /api/profiles/{id}/verify（bootstrap_only=True）显式触发。
            return self._enter_live_mode(
                trace,
                emitter,
                "任务型运行默认不前置完整探索（BOOTSTRAP_ENABLED_ON_TASK_RUN=false），进入实时模式执行任务",
            )
        run_dir = self.artifacts.run_dir(trace.run_id)
        try:
            return await self._bootstrap_profile(request, trace, emitter, device, resolved, registry, run_dir)
        except asyncio.CancelledError:
            raise
        except (ToolExecutionError, DeviceError) as exc:
            # 候选 Profile 已保存后的回放门控/晋级失败属于明确的收尾失败，保持原语义；
            # 其余准备阶段失败按实时模式降级继续任务。
            if (
                request.bootstrap_only
                or isinstance(exc, ToolExecutionError)
                and exc.state in {RunState.FAILED_SCRIPT, RunState.FAILED_PROFILE_PROMOTION}
            ):
                raise
            return self._enter_live_mode(trace, emitter, f"Profile 准备未完成，降级为实时模式继续任务：{exc}")

    @staticmethod
    def _enter_live_mode(trace: RunTrace, emitter: RunEventEmitter, reason: str) -> None:
        """记录实时模式并通知前端。

        实时模式**照常生成脚本**：没有经过设备验证的定位器只影响 ``confidence``
        （``provisional`` / ``live_mode`` 会写进 ``promotion_blockers``），脚本本身立即可执行、
        可入库、可手动回放。
        """
        trace.live_mode = True
        trace.profile_status_at_start = ProfileStatus.ABSENT
        emitter.emit(EventType.PROFILE_LIVE_MODE, reason, {"live_mode": True})
        return None

    @staticmethod
    def _enter_incremental_mode(
        trace: RunTrace,
        emitter: RunEventEmitter,
        profile: TargetAppProfile,
    ) -> TargetAppProfile:
        """复用未 verified 但已有定位器的 Profile：跳过完整 bootstrap，直接执行任务。

        与 ``_enter_live_mode`` 的区别：这里把 draft/candidate 当成任务期提示源使用
        （``executor.stable_locators`` 与 ``PlanningContext``），任务期证据在收尾时累加回它。
        **绝不**把 ``resolved_target.source`` 改成 ``verified_profile``，也不调用
        ``registry.update_verified``——draft/candidate 只是复用，不是复验通过（计划风险 §5.3）。
        """
        trace.live_mode = False
        trace.profile_status_at_start = profile.status
        emitter.emit(
            EventType.PROFILE_INCREMENTAL,
            f"复用 {profile.status} Profile（{len(profile.stable_locator_inventory)} 个定位器），进入增量模式",
            {
                "status": str(profile.status),
                "locators": len(profile.stable_locator_inventory),
                "assertions": len(profile.assertion_inventory),
                "target_app_id": profile.target_app_id,
            },
        )
        return profile

    @staticmethod
    def _profile_compatible(resolved: ResolvedTarget, profile: TargetAppProfile) -> bool:
        """低成本身份校验：版本/Ability/签名不一致时不复用既有 Profile 证据。"""
        if profile.app_version.version_code is not None and resolved.version_code != profile.app_version.version_code:
            return False
        if profile.main_ability != resolved.main_ability:
            return False
        if profile.app_version.signature_sha256 and resolved.signature_sha256 != profile.app_version.signature_sha256:
            return False
        return True

    async def _bootstrap_profile(
        self,
        request: RunRequest,
        trace: RunTrace,
        emitter: RunEventEmitter,
        device: DeviceAdapter,
        resolved: ResolvedTarget,
        registry: ProfileRegistry,
        run_dir: Path,
    ) -> TargetAppProfile:
        """有界探索 → draft → 验证 → 准入 → Hypium 回放 → 晋级 verified。"""
        trace.state = RunState.PROBING_TARGET
        started = await asyncio.to_thread(
            device.start_app, resolved.bundle_name, resolved.main_ability, resolved.module_name
        )
        if not started.ok:
            raise ToolExecutionError(started.stderr or started.stdout, RunState.FAILED_TARGET_PROBE)
        foreground = await asyncio.to_thread(device.current_foreground_app)
        if not foreground:
            raise ToolExecutionError(
                "launched foreground application could not be determined", RunState.FAILED_TARGET_PROBE
            )
        if foreground.bundle_name != resolved.bundle_name:
            raise ToolExecutionError(
                "launched foreground bundle does not match resolved target", RunState.FAILED_TARGET_PROBE
            )
        if foreground.ability_name and foreground.ability_name != resolved.main_ability:
            raise ToolExecutionError(
                "launched foreground Ability does not match resolved target", RunState.FAILED_TARGET_PROBE
            )
        emitter.emit(EventType.TARGET_STARTED, "目标应用启动探测通过", {"bundle_name": resolved.bundle_name})

        trace.state = RunState.DISCOVERING
        emitter.emit(
            EventType.DISCOVERY_STARTED, "开始有界自动探索", request.exploration_policy.model_dump(mode="json")
        )

        def discovery_progress(kind: str, payload: dict[str, object]) -> None:
            if kind == "blocked":
                event_type = EventType.DISCOVERY_PATH_BLOCKED
            else:
                event_type = EventType.DISCOVERY_PROGRESS
            emitter.emit(event_type, "自动探索进度", payload)

        def exploration_snapshot(snapshot: ScreenSnapshot) -> None:
            """把探索期的每一帧推进 trace 与事件流，驱动实时视图的设备画面与元素表。"""
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
                    "summary": snapshot.summary,
                },
            )
            emitter.emit(
                EventType.ELEMENTS_DETECTED,
                f"识别到 {len(snapshot.elements)} 个元素",
                {"snapshot_id": snapshot.snapshot_id, "count": len(snapshot.elements)},
            )

        advisor: ExplorationAdvisor | None = None
        if request.exploration_policy.advisor_enabled and not self.provider.mock:
            advisor = ExplorationAdvisor(self.provider, request.exploration_policy)
        discovery = await asyncio.to_thread(
            BoundedExplorer(
                device,
                resolved,
                run_dir / "discovery",
                trace.run_id,
                request.exploration_policy,
                progress=discovery_progress,
                should_stop=lambda: self._should_stop(trace.run_id),
                advisor=advisor,
                on_snapshot=exploration_snapshot,
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
            {
                "pages": len(discovery.pages),
                "interactions": sorted(discovery.interaction_types),
                "stop_reason": discovery.stop_reason,
            },
        )

        draft = self._build_profile(resolved, discovery, None, trace.run_id)
        trace.state = RunState.PROFILE_DRAFTING
        draft_path = registry.save_draft(draft)
        self.artifacts.write_json(run_dir / "discovery" / "draft-profile.json", draft)
        emitter.emit(EventType.PROFILE_DRAFT_SAVED, "已保存 draft Profile", {"path": str(draft_path)})
        trace.state = RunState.PROFILE_VERIFYING
        emitter.emit(
            EventType.PROFILE_VERIFICATION_STARTED,
            f"开始 Profile 设备验证（{self.settings.profile_verification_rounds} 轮重启回放）",
            {"rounds": self.settings.profile_verification_rounds},
        )
        verification = await asyncio.to_thread(
            ProfileVerifier(
                device,
                resolved,
                run_dir / "verification",
                trace.run_id,
                should_stop=lambda: self._should_stop(trace.run_id),
                min_interaction_kinds=request.exploration_policy.min_interaction_kinds,
                rounds=self.settings.profile_verification_rounds,
                # 每轮开始/结束即时推送：验证全程约数分钟，攒批发送会让实时视图长时间无反馈。
                on_round_started=lambda number: emitter.emit(
                    EventType.PROFILE_VERIFICATION_ROUND_STARTED,
                    f"Profile 验证第 {number} 轮开始",
                    {"round_number": number},
                ),
                on_round_finished=lambda round_result: emitter.emit(
                    EventType.PROFILE_VERIFICATION_ROUND_FINISHED,
                    f"Profile 验证第 {round_result.round_number} 轮完成",
                    round_result.model_dump(mode="json"),
                ),
            ).verify,
            discovery,
        )
        trace.verification_result = verification.model_dump(mode="json", exclude={"rounds": {"__all__": {"snapshots"}}})
        if not verification.passed:
            # 计划 R5：失败时持久化「填充过的」Profile——定位器/断言来自本次真实验证观测，
            # 不是 :648 那个 0 定位器 / 0 断言的构造态草稿。有定位器即保持 DRAFT（可被下次
            # 运行的增量模式复用并累加），完全为空才记为 INVALID（本次探索确实一无所获）。
            failed_draft = self._build_profile(resolved, discovery, verification, trace.run_id)
            has_locators = bool(failed_draft.stable_locator_inventory)
            evidence = dict(failed_draft.provenance.evidence)
            evidence.update(
                {
                    "verification_passed": False,
                    "verification_failures": list(verification.failures),
                    "verification_attempted_at": utc_now().isoformat(),
                }
            )
            failed_draft = failed_draft.model_copy(
                update={
                    "status": ProfileStatus.DRAFT if has_locators else ProfileStatus.INVALID,
                    "provenance": failed_draft.provenance.model_copy(update={"evidence": evidence}, deep=True),
                },
                deep=True,
            )
            registry.save_draft(failed_draft)
            trace.profile_snapshot = failed_draft
            self.artifacts.write_json(run_dir / "discovery" / "draft-profile.json", failed_draft)
            emitter.emit(
                EventType.PROFILE_DRAFT_SAVED,
                f"验证未通过，已保留 {len(failed_draft.stable_locator_inventory)} 个定位器的失败草稿",
                {"path": str(registry.draft_dir / f"{failed_draft.target_app_id}.json"), "status": failed_draft.status},
            )
            if request.temporary_test:
                trace.provisional = True
                return failed_draft
            raise ToolExecutionError("; ".join(verification.failures), RunState.FAILED_PROFILE_VERIFICATION)
        candidate = self._build_profile(resolved, discovery, verification, trace.run_id)
        thresholds = self._admission_thresholds()
        candidate_evidence = AdmissionEvidence(
            stable_locator_count=len(candidate.stable_locator_inventory),
            distinct_page_state_count=len({item.page_signature for item in candidate.stable_locator_inventory}),
            app_assertion_count=len(candidate.assertion_inventory),
        )
        thresholds = self._admission_thresholds()
        for gate in order_admission_failures(
            evaluate_admission(candidate_evidence, thresholds=thresholds), style="candidate"
        ):
            raise ToolExecutionError(
                describe_admission_failure(gate, style="candidate", thresholds=thresholds),
                RunState.FAILED_PROFILE_VERIFICATION,
            )
        registry.save_candidate(candidate)
        trace.profile_snapshot = candidate.model_copy(deep=True)
        validation_trace = self._profile_validation_trace(trace, candidate, discovery, verification)
        validation_generator = HypiumGenerator(
            self.artifacts, min_observed_rounds=self.settings.profile_verification_rounds
        )
        trace.state = RunState.SCRIPT_GENERATING
        trace.profile_validation_generated = await asyncio.to_thread(validation_generator.generate, validation_trace)
        # 记录门禁脚本路径：POST /api/profiles/{id}/replay 需要复用它异步追加回放证据。
        candidate = self._attach_generated_script(candidate, trace.profile_validation_generated)
        registry.save_candidate(candidate)
        trace.profile_snapshot = candidate.model_copy(deep=True)
        emitter.emit(
            EventType.SCRIPT_GENERATED,
            "已从 Profile 验证证据生成 Hypium Driver 用例",
            trace.profile_validation_generated.model_dump(mode="json"),
        )
        if self._should_stop(trace.run_id):
            raise asyncio.CancelledError
        trace.state = RunState.SCRIPT_EXECUTING
        runner = make_hypium_runner(
            self.settings,
            analyzer=self.analyzer,
            bundle_name=candidate.bundle_name,
            device_id=trace.device_id,
        )
        trace.profile_validation_replays = await self._run_replays(
            runner, trace.profile_validation_generated, trace.run_id, self.settings.hypium_replay_attempts, emitter
        )
        # 让前端流水线的「回放」阶段知道本门禁计划跑几次（默认 1 次，可配 3）；
        # 不再依赖前端硬编码 3。RunTrace validator 只在 trace.replays 非空时重算这些字段。
        trace.replay_total = self.settings.hypium_replay_attempts
        trace.replay_completed = len(trace.profile_validation_replays)
        trace.replay_passed = sum(item.passed for item in trace.profile_validation_replays)
        if not all(item.passed for item in trace.profile_validation_replays):
            raise ToolExecutionError("Profile Hypium replay gate failed", RunState.FAILED_SCRIPT)
        trace.state = RunState.PROFILE_PROMOTING
        # 晋级证据只能来自 Profile 验证脚本；用显式断言把「生成器产物缺失」挡在晋级之前。
        if trace.profile_validation_generated is None:
            raise ToolExecutionError("profile validation script is missing", RunState.FAILED_SCRIPT)
        replay_ids = [f"{trace.run_id}:profile-attempt-{item.attempt}" for item in trace.profile_validation_replays]
        try:
            verified_path = registry.promote(candidate, replay_run_ids=replay_ids)
        except Exception as exc:
            raise ToolExecutionError(str(exc), RunState.FAILED_PROFILE_PROMOTION) from exc
        promoted = self.artifacts.load_profile(verified_path)

        trace.profile_snapshot = promoted
        trace.resolved_target = resolved.model_copy(update={"profile_snapshot": promoted}, deep=True)
        emitter.emit(
            EventType.PROFILE_PROMOTED, "Profile 已自动晋级为 verified", {"target_app_id": promoted.target_app_id}
        )
        return promoted

    @staticmethod
    def _attach_generated_script(profile: TargetAppProfile, generated: GeneratedArtifact | None) -> TargetAppProfile:
        """把门禁 Hypium 脚本路径写入 provenance。

        ``POST /api/profiles/{id}/replay`` 依赖该路径异步追加剩余回放证据；
        缺少它时端点会以 409 拒绝，而不是猜测脚本位置。
        """
        if generated is None or not getattr(generated, "python_path", None):
            return profile
        provenance = profile.provenance.model_copy(
            update={"generated_script_path": str(generated.python_path)}, deep=True
        )
        return profile.model_copy(update={"provenance": provenance}, deep=True)

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
            agent_outcome="completed",
        )
        core_pages = ProfileVerifier._core_pages(discovery, trace.exploration_policy.min_interaction_kinds)
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
        assertions_by_page = {item.page_signature: item for item in profile.assertion_inventory}
        for page_index, page in enumerate(core_pages):
            if page_index:
                previous = core_pages[page_index - 1]
                pending = page.path_actions[len(previous.path_actions) :]
                for step_index, action in enumerate(pending):
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
                                step_id=f"profile-flow-{page_index:02d}-{step_index:02d}-{action.action_id}-focus",
                                tool=ToolName.CLICK_COORDINATE,
                                params={"coordinate": action.coordinate},
                                success=True,
                            )
                        )
                        tool = ToolName.INPUT_TEXT
                    validation.actions.append(
                        ActionResult(
                            step_id=f"profile-flow-{page_index:02d}-{step_index:02d}-{action.action_id}",
                            tool=tool,
                            params=params,
                            success=True,
                            locator=action_locator,
                        )
                    )
            page_key = page.structural_identity or page.signature
            locator = next(iter(locators_by_page.get(page_key, [])), None)
            if locator is None:
                continue
            kind, value = AgentOrchestrator._profile_locator(locator)
            assertion = assertions_by_page.get(page_key)
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
            if action.assertion and action.assertion.message.startswith("verified application page ")
        }
        replay_gate = pick_first_failure(
            evaluate_admission(
                AdmissionEvidence(
                    distinct_page_state_count=len(covered_pages),
                    app_assertion_count=len(validation.assertions),
                ),
                thresholds=AgentOrchestrator._default_admission_thresholds(),
            ),
            style="admission_replay",
        )
        if replay_gate is not None:
            raise ValueError(describe_admission_failure(replay_gate, style="admission_replay"))
        # 合成准入轨迹代表一次完整成功的准入运行；缺少完成结局与 FINISH 会被判为
        # confidence=low，并因此无法满足 Profile 晋级门禁。
        validation.actions.append(ActionResult(step_id="profile-finish", tool=ToolName.FINISH, success=True))
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
                item.model_dump(mode="json") for item in (core_pages[-1].path_actions if core_pages else [])
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
        if not self._profile_compatible(resolved, profile):
            return False
        if type(device).stop_app is DeviceAdapter.stop_app or type(device).start_app is DeviceAdapter.start_app:
            started = device.open_app(profile, reset=True)
        else:
            stopped = device.stop_app(resolved.bundle_name)
            started = device.start_app(resolved.bundle_name, resolved.main_ability, resolved.module_name)
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
        pages = list(profile.core_flows[0].get("pages", []) if profile.core_flows else [])
        # 计划 3.2/R9：门槛必须与 settings.profile_verification_rounds 一致。此处原先硬编码
        # ``>= 3``，而 _build_profile 写入的 observed_rounds == 验证轮数（默认 1）⇒ 恒为空 ⇒
        # 三页复验路径是死代码，任何默认配置晋级出来的 Profile 复用时都只做单页检查。
        required_rounds = max(int(self.settings.profile_verification_rounds), 1)
        locator_inventory = [
            item
            for item in profile.stable_locator_inventory
            if item.observed_rounds >= required_rounds
            and item.unique_match_rounds >= required_rounds
            and item.evidence_snapshot_ids
        ]
        page_locators = {
            page: next(
                (item for item in locator_inventory if item.page_signature == page),
                None,
            )
            for page in pages
        }
        if not pages or not recovery_actions or any(locator is None for locator in page_locators.values()):
            # 页面数量不足（单页应用）或旧版 Profile 的 core_flows.pages 是发现期整树签名、
            # 与验证期定位器的结构身份不在同一哈希空间时，退回入口页强定位器检查而不是直接判失败。
            return self._legacy_entry_revalidate(device, resolved, profile, trace, output_dir)
        for page_index, page_signature in enumerate(pages):
            foreground = device.current_foreground_app()
            if (
                foreground is None
                or foreground.bundle_name != resolved.bundle_name
                or (foreground.ability_name and foreground.ability_name != resolved.main_ability)
            ):
                return False
            snapshot = device.screenshot(output_dir, trace.run_id, f"revalidate-{page_index:02d}")
            if not self._snapshot_has_locator(snapshot, page_locators[page_signature]):
                return False
            if page_index < len(pages) - 1:
                if page_index >= len(recovery_actions):
                    return False
                if not self._replay_profile_action(device, recovery_actions[page_index], snapshot, profile):
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
            AgentOrchestrator._snapshot_has_locator(snapshot, locator) for locator in locators
        )

    @staticmethod
    def _snapshot_has_locator(snapshot: ScreenSnapshot, locator: StableLocator) -> bool:
        return any(
            (locator.key and locator.key == item.key)
            or (locator.id and locator.id == item.id)
            or (locator.text and locator.text == item.content and (not locator.type or locator.type == item.type))
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
        except DeviceError, ValueError:
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
                locators.append(
                    StableLocator(
                        name=item.name,
                        page_signature=item.page_signatures[0] if item.page_signatures else "",
                        confidence=ConfidenceLevel.HIGH
                        if item.level == StabilityLevel.HIGH
                        else ConfidenceLevel.MEDIUM,
                        observed_rounds=len(item.rounds),
                        unique_match_rounds=len(item.rounds) if item.unique_each_round else 0,
                        evidence_snapshot_ids=[
                            round_result.snapshot_ids[0]
                            for round_result in verification.rounds
                            if round_result.snapshot_ids
                            and any(
                                observation.value == item.value for observation in round_result.locator_observations
                            )
                        ],
                        **values,
                    )
                )
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
            app_version=AppVersion(
                version_name=resolved.version_name,
                version_code=resolved.version_code,
                signature_sha256=resolved.signature_sha256,
            ),
            device_compatibility=DeviceCompatibility(
                validated_device_types=["phone"],
                validated_resolutions=sorted({item.resolution for item in verification.rounds if item.resolution})
                if verification
                else [],
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
            assertion_inventory=assertions,
            known_limitations=["coordinate fallbacks remain resolution-bound and do not count toward Profile admission"]
            if verification and any(item.level == StabilityLevel.LOW for item in verification.stability.locators)
            else [],
            core_flows=[
                {
                    # pages 必须与验证期定位器的页签名同空间：结构身份跨启动稳定，
                    # 整树签名随信息流内容抖动，会导致快速复验的页面映射恒失败。
                    "pages": [page.structural_identity for page in ProfileVerifier._core_pages(discovery)],
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
                }
            ],
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
                        if transition.blocked_reason and "cross-bundle" in transition.blocked_reason
                    ),
                    "cross_bundle_recovery_failed": False,
                },
            ),
        )

    async def _run_replays(self, runner, generated, run_id: str, attempts: int, emitter: RunEventEmitter):
        """Execute replay attempts independently and honor stop requests between attempts."""
        results = []
        for attempt in range(1, attempts + 1):
            if self._should_stop(run_id):
                raise asyncio.CancelledError
            emitter.emit(EventType.HYPIUM_REPLAY_STARTED, f"Hypium 回放第 {attempt} 次开始", {"attempt": attempt})
            replay = await asyncio.to_thread(runner.execute, generated, attempt)
            results.append(replay)
            emitter.emit(
                EventType.HYPIUM_REPLAY_FINISHED,
                f"Hypium 回放第 {replay.attempt} 次完成",
                replay.model_dump(mode="json"),
            )
        return results

    async def _wait_launch_settled(self, device, trace) -> None:
        """冷启动静默期：OPEN_APP 后至少等待 launch_settle_seconds，期间确认前台已是目标应用。

        `aa start` 命令返回不代表首页已渲染；固定等待加前台校验，避免把启动 logo 页当作任务首页截图。
        """
        resolved = trace.resolved_target
        budget = max(self.launch_settle_seconds, 0.0)
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            foreground = await asyncio.to_thread(device.current_foreground_app)
            if (
                foreground
                and foreground.bundle_name == resolved.bundle_name
                and (
                    not foreground.ability_name
                    or not resolved.main_ability
                    or foreground.ability_name == resolved.main_ability
                )
            ):
                break
            await asyncio.sleep(0.3)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)

    @staticmethod
    def _capture_stable_frame(device, run_dir: Path, trace, label: str):
        """采集前轮询 UI 层级稳定：静止页返回首帧，持续变化页复用最新层级而不是补抓一帧。

        与探索阶段 `_capture_settled` 同语义，用于过滤点击/输入后的加载动画与过渡帧；
        静态启动页对轮询完全"稳定"，由 OPEN_APP 的冷启动静默期兜底。

        预算耗尽时**不再补抓一帧**：补抓要走「snapshot_display + file recv + PNG 编码 +
        dumpLayout + cat」整条链路。实测（run-20260921T063745Z-ba36e30e /
        run-20260921T053514Z-8418044b 的逐步耗时拆解）动态页面每步都要付一次，是任务阶段
        最大的单笔开销（≈15-25s/步，而模型往返仅 ≈9s）；复用最后一次轮询到的层级刷新元素表
        即可，画面只比元素表早一个轮询周期。
        """
        snapshot = device.screenshot(run_dir / "screens", trace.run_id, label)
        budget = trace.exploration_policy.settle_timeout_seconds if trace.exploration_policy else 0
        if budget <= 0:
            return snapshot
        fingerprint = BoundedExplorer._stability_fingerprint(snapshot.page_path, snapshot.elements)
        deadline = time.monotonic() + budget
        latest_hierarchy: dict | None = None
        while time.monotonic() < deadline:
            device.wait(0.5)
            try:
                hierarchy = device.collect_ui_hierarchy()
            except DeviceError:
                break
            latest_hierarchy = hierarchy
            elements = normalize_layout(hierarchy, snapshot.width, snapshot.height)
            if BoundedExplorer._stability_fingerprint(page_path(hierarchy), elements) == fingerprint:
                return snapshot
        if latest_hierarchy is None:
            return snapshot
        polled_elements = normalize_layout(latest_hierarchy, snapshot.width, snapshot.height)
        if not polled_elements:
            # 轮询到的层级为空（dump 失败或空帧）：保留首帧自带的元素表，绝不把元素清空。
            return snapshot
        return snapshot.model_copy(
            update={"elements": polled_elements, "page_path": page_path(latest_hierarchy)},
            deep=True,
        )

    async def _decide_and_execute(
        self,
        trace: RunTrace,
        emitter: RunEventEmitter,
        executor: ToolExecutor,
        perception: PerceptionService,
        graph: PageGraphBuilder,
        device: DeviceAdapter,
        step: PlannedStep,
        snapshot: ScreenSnapshot | None,
    ) -> tuple[ToolDecision, ActionResult, ScreenSnapshot | None]:
        """决策并执行一个计划步骤；语义失败时进入自适应恢复循环（SafetyError 不恢复）。

        恢复尝试把失败反馈交回模型并重新截屏，纠正动作成功不算完成——断言类步骤必须由
        模型重新发出断言且通过；预算（agent_step_recovery_limit）用尽后按最后失败终止。
        """
        feedback: str | None = None
        last_error: ToolExecutionError | None = None
        last_decision: ToolDecision | None = None
        last_failed_action: ActionResult | None = None
        for attempt in range(self.settings.agent_step_recovery_limit + 1):
            decision, snapshot = await self._resolve_decision(trace, emitter, perception, step, snapshot, feedback)
            decision = self._constrain_finish_decision(step, decision)
            emitter.emit(
                EventType.ACTION_STARTED,
                step.instruction,
                {"step_id": step.step_id, "decision": decision.model_dump(mode="json"), "recovery_attempt": attempt},
            )
            action_started_at = utc_now()
            try:
                result = await self._execute_with_retry(executor, step.step_id, decision, snapshot)
            except SafetyError as exc:
                self._record_failed_action(trace, emitter, step, decision, snapshot, action_started_at, exc)
                raise
            except ToolExecutionError as exc:
                failed = self._record_failed_action(trace, emitter, step, decision, snapshot, action_started_at, exc)
                if exc.state not in {RunState.FAILED_ELEMENT, RunState.FAILED_ASSERTION}:
                    raise
                if attempt >= self.settings.agent_step_recovery_limit:
                    raise
                last_error = exc
                last_decision = decision
                last_failed_action = failed
                feedback = (
                    f"attempt {attempt + 1} failed: {exc}. The attached elements and screenshot are the latest "
                    "state; you may first perform corrective actions (for example an anchored swipe inside a wheel "
                    "column or clicking another control) and then re-attempt the planned goal."
                )
                trace.state = RunState.VERIFYING
                snapshot, _ = await self._capture(
                    trace, emitter, device, perception, graph, f"{step.step_id}_recovery_{attempt + 1:02d}"
                )
                continue
            if self._step_completed(step, decision, result):
                # 绕路留痕（设计 6 / G4）：恢复循环的能力保留不动，但必须**可见**。
                if attempt > 0:
                    self._record_workaround(trace, result, attempt, step, last_decision, last_failed_action)
                return decision, result, snapshot
            # 纠正动作成功但断言类步骤尚未重新完成：继续消耗恢复预算。
            feedback = (
                "the corrective action succeeded, but the planned assertion has not been re-issued yet; "
                "now complete the planned step against the latest state."
            )
            trace.state = RunState.VERIFYING
            snapshot, _ = await self._capture(
                trace, emitter, device, perception, graph, f"{step.step_id}_recovery_{attempt + 1:02d}"
            )
        raise last_error or ToolExecutionError(
            f"step recovery exhausted without completing: {step.instruction}", RunState.FAILED_ASSERTION
        )

    async def _resolve_decision(
        self,
        trace: RunTrace,
        emitter: RunEventEmitter,
        perception: PerceptionService,
        step: PlannedStep,
        snapshot: ScreenSnapshot | None,
        feedback: str | None,
    ) -> tuple[ToolDecision, ScreenSnapshot | None]:
        """取得本步骤的工具决策，必要时顺手完成该帧的观测（计划 5.1）。

        优先级：``_capture`` 里合并请求缓存下来的决策 → 该帧尚未观测且启用合并时现场合并请求
        → 既有的单独 ``decide`` 路径。返回的 snapshot 可能是刚被观测合并过的新对象。
        """
        if snapshot is not None:
            cached = self._pending_decisions.pop((step.step_id, snapshot.snapshot_id), None)
            if cached is not None:
                return cached, snapshot
        if (
            snapshot is not None
            and self._combined_vision_active()
            and snapshot.snapshot_id not in self._observed_snapshots
        ):
            observation, decision = await self._observe_and_decide_with_timeout(
                step, snapshot, feedback, self._decision_history(trace)
            )
            snapshot = self._publish_observation(trace, emitter, perception, snapshot, observation, emit_capture=False)
            return decision, snapshot
        return await self._decide_with_timeout(step, snapshot, feedback), snapshot

    async def _decide_with_timeout(
        self,
        step: PlannedStep,
        snapshot: ScreenSnapshot | None,
        feedback: str | None,
    ) -> ToolDecision:
        """调用模型决策并捕获超时；第一次超时后用收敛提示重试，仍超时才失败。

        原先此处直接 ``asyncio.wait_for`` 且无 try/except：一次慢到 90s 的 ``decide`` 会以
        消息为空的 ``TimeoutError`` 冒泡到兜底 ``except Exception``，把整次运行标成
        ``unexpected error: TimeoutError``（实测一次调用报废了 23 分钟的运行）。这里把超时
        转成显式的 ``FAILED_MODEL`` 与可读错误，并先用一次「只输出工具决策」的收敛提示重试，
        重试成本远低于重跑整个任务。
        """
        timeout = self.settings.agent_model_timeout
        retries = max(int(self.settings.agent_model_retry_limit), 0)
        attempt_feedback = feedback
        for attempt in range(retries + 1):
            try:
                return await asyncio.wait_for(
                    self.provider.decide(step, snapshot, feedback=attempt_feedback),
                    timeout=timeout,
                )
            except TimeoutError as exc:
                if attempt >= retries:
                    raise ToolExecutionError(
                        f"model decision timed out after {timeout:g} seconds",
                        RunState.FAILED_MODEL,
                    ) from exc
                timeout_hint = (
                    "the previous attempt timed out. Reply with ONLY the single tool decision JSON object "
                    "and do not expand long reasoning."
                )
                attempt_feedback = timeout_hint + (f" Previous failure feedback: {feedback}" if feedback else "")
            except ToolExecutionError:
                raise
            except Exception as exc:  # noqa: BLE001 - 模型侧故障（429/5xx/网关错误）必须可读且归类为 FAILED_MODEL
                if _is_transient_model_error(exc) and attempt < retries:
                    await self._wait_before_model_retry("decision", exc)
                    continue
                raise ToolExecutionError(
                    f"model call failed: {type(exc).__name__}: {exc}", RunState.FAILED_MODEL
                ) from exc
        raise ToolExecutionError(  # pragma: no cover - 循环内必然 return 或 raise
            f"model decision timed out after {timeout:g} seconds", RunState.FAILED_MODEL
        )

    async def _wait_before_model_retry(self, what: str, exc: Exception) -> None:
        """瞬时 provider 故障（429/5xx/网关）退避后重试。"""
        delay = float(self.settings.agent_model_retry_backoff_seconds)
        logger.warning("model %s hit a transient provider error (%s); retrying in %.1fs", what, exc, delay)
        if delay > 0:
            await asyncio.sleep(delay)

    @staticmethod
    def _record_failed_action(
        trace: RunTrace,
        emitter: RunEventEmitter,
        step: PlannedStep,
        decision: ToolDecision,
        snapshot: ScreenSnapshot | None,
        started_at,
        error: Exception,
    ) -> ActionResult:
        failed = ActionResult(
            step_id=step.step_id,
            tool=decision.tool,
            params=decision.model_dump(exclude_none=True),
            success=False,
            started_at=started_at,
            ended_at=utc_now(),
            before_snapshot_id=snapshot.snapshot_id if snapshot else None,
            after_snapshot_id=snapshot.snapshot_id if snapshot else None,
            error=str(error),
        )
        trace.actions.append(failed)
        emitter.emit(
            EventType.ACTION_FINISHED,
            f"步骤失败：{step.instruction}",
            failed.model_dump(mode="json"),
        )
        return failed

    @staticmethod
    def _record_workaround(
        trace: RunTrace,
        result: ActionResult,
        attempt: int,
        step: PlannedStep,
        last_decision: ToolDecision | None,
        last_failed_action: ActionResult | None,
    ) -> None:
        """记录一次「靠恢复循环绕路完成」（设计 6 / G4）。

        行为完全不变（绕路能力是有价值的），只是让它可见：步骤卡片渲染失败原因与绕路标记，
        报告汇总「N 步靠绕路完成」。绝不改变 ``success`` / ``state``。
        """
        result.recovery_attempts = attempt
        result.workaround = WorkaroundRecord(
            original_step_id=step.step_id,
            original_instruction=step.instruction,
            original_error=(last_failed_action.error if last_failed_action and last_failed_action.error else "")
            or f"attempt {attempt} required recovery",
            corrective_tool=str(last_decision.tool) if last_decision else "",
            corrective_target=(last_decision.target or "") if last_decision else "",
        )
        trace.workaround_count += 1

    @staticmethod
    def _step_completed(step: PlannedStep, decision: ToolDecision, result: ActionResult) -> bool:
        """步骤完成判定：断言类计划步骤必须由模型重新发出断言且通过。"""
        if not result.success:
            return False
        if step.tool in {ToolName.ASSERT_VISIBLE, ToolName.ASSERT_NOT_VISIBLE, ToolName.ASSERT_TEXT}:
            return decision.tool == step.tool and result.assertion is not None and result.assertion.passed
        return True

    def _combined_vision_active(self) -> bool:
        """是否启用「单次请求同时拿到观测与决策」（计划 5.1）。

        mock provider 的 analyze/decide 都是本地确定性实现、没有延迟可省：对 mock 保持原路径，
        使全部既有 mock 测试的事件顺序与产物逐字不变。自定义 provider 默认不声明该能力，
        因此继续走两次调用路径（连带保留其各自的失败语义）。
        """
        if self.provider.mock:
            return False
        support = getattr(self.provider, "supports_combined_observation", None)
        return bool(support()) if callable(support) else False

    async def _analyze(self, snapshot: ScreenSnapshot) -> VisionObservation | None:
        """视觉分析（含超时与降级）；真实 provider 失败即按 FAILED_MODEL 终止。"""
        retries = max(int(self.settings.agent_model_retry_limit), 0)
        for attempt in range(retries + 1):
            try:
                return await asyncio.wait_for(
                    self.provider.analyze(snapshot),
                    timeout=self.settings.agent_model_timeout,
                )
            except Exception as exc:  # noqa: BLE001 - 真实 provider 的任何失败都要归类为 FAILED_MODEL
                if not self.provider.mock and _is_transient_model_error(exc) and attempt < retries:
                    await self._wait_before_model_retry("vision analysis", exc)
                    continue
                if not self.provider.mock:
                    message = f"vision analysis failed: {type(exc).__name__}: {exc}"
                    raise ToolExecutionError(message, RunState.FAILED_MODEL) from exc
                snapshot.summary = f"mock vision analysis unavailable: {type(exc).__name__}: {exc}"
                return None
        return None  # pragma: no cover - 循环内必然 return 或 raise

    def _emit_frame(self, emitter: RunEventEmitter, snapshot: ScreenSnapshot) -> None:
        """推送一帧的画面与元素规模（摘要可能稍后由合并观测回填）。"""
        emitter.emit(
            EventType.SCREEN_CAPTURED,
            "已采集本地截图",
            {
                "snapshot_id": snapshot.snapshot_id,
                "image_path": str(snapshot.image_path),
                "width": snapshot.width,
                "height": snapshot.height,
                "sha256": snapshot.image_sha256,
                # 视觉模型对页面的理解摘要（mock 或分析失败时为占位说明），供前端思考流展示 LLM 输入理解。
                "summary": snapshot.summary,
            },
        )
        emitter.emit(
            EventType.ELEMENTS_DETECTED,
            f"识别到 {len(snapshot.elements)} 个元素",
            {"snapshot_id": snapshot.snapshot_id, "count": len(snapshot.elements), "summary": snapshot.summary},
        )

    async def _capture(
        self,
        trace,
        emitter,
        device,
        perception,
        graph,
        label,
        *,
        step: PlannedStep | None = None,
        feedback: str | None = None,
        with_vision: bool = True,
    ):
        """采集一帧；可携带本步骤做一次合并的观测+决策请求（计划 5.1）。

        ``with_vision=False``（合并模式下的动作后帧）：只推送画面与元素规模，观测留到下一步的
        合并请求里一次完成——每步因此只花 1 次视觉往返，而非今天的 2 次。
        """
        run_dir = self.artifacts.run_dir(trace.run_id)
        snapshot = await asyncio.to_thread(self._capture_stable_frame, device, run_dir, trace, label)
        if not with_vision:
            trace.snapshots.append(snapshot)
            self._emit_frame(emitter, snapshot)
            node, created = graph.add_snapshot(snapshot)
            if created:
                emitter.emit(EventType.PAGE_DISCOVERED, "发现新页面状态", node.model_dump(mode="json"))
            return snapshot, node
        if step is not None and self._combined_vision_active():
            observation, decision = await self._observe_and_decide_with_timeout(
                step, snapshot, feedback, self._decision_history(trace)
            )
            self._pending_decisions[(step.step_id, snapshot.snapshot_id)] = decision
        else:
            observation = await self._analyze(snapshot)
        snapshot = self._publish_observation(trace, emitter, perception, snapshot, observation, emit_capture=True)
        node, created = graph.add_snapshot(snapshot)
        if created:
            emitter.emit(EventType.PAGE_DISCOVERED, "发现新页面状态", node.model_dump(mode="json"))
        return snapshot, node

    def _publish_observation(
        self,
        trace: RunTrace,
        emitter: RunEventEmitter,
        perception: PerceptionService,
        snapshot: ScreenSnapshot,
        observation: VisionObservation | None,
        *,
        emit_capture: bool,
    ) -> ScreenSnapshot:
        """合并观测并登记为「已观测帧」；``emit_capture`` 决定是否补发 SCREEN_CAPTURED。"""
        merged = perception.merge(snapshot, observation)
        if all(item.snapshot_id != merged.snapshot_id for item in trace.snapshots):
            trace.snapshots.append(merged)
        self._observed_snapshots.add(merged.snapshot_id)
        if observation is not None:
            self._observation_cache[merged.image_sha256] = observation
            while len(self._observation_cache) > 8:
                self._observation_cache.pop(next(iter(self._observation_cache)))
        if emit_capture:
            self._emit_frame(emitter, merged)
        else:
            emitter.emit(
                EventType.ELEMENTS_DETECTED,
                f"识别到 {len(merged.elements)} 个元素",
                {"snapshot_id": merged.snapshot_id, "count": len(merged.elements), "summary": merged.summary},
            )
        return merged

    def _decision_history(self, trace: RunTrace) -> list[StepHistoryEntry]:
        """紧凑的跨步历史（计划 5.4）：让 Live 决策能看到此前几步做了什么、结果如何。"""
        limit = int(self.settings.live_decision_history_steps)
        if limit <= 0 or not trace.actions:
            return []
        snapshot_by_id = {item.snapshot_id: item for item in trace.snapshots}
        entries: list[StepHistoryEntry] = []
        for index, action in enumerate(trace.actions, start=1):
            frame = snapshot_by_id.get(action.after_snapshot_id or "") or snapshot_by_id.get(
                action.before_snapshot_id or ""
            )
            plan_step = trace.plan[index - 1] if index <= len(trace.plan) else None
            entries.append(
                StepHistoryEntry(
                    index=index,
                    instruction=plan_step.instruction if plan_step else "",
                    tool=str(action.tool),
                    target=str(action.params.get("target") or action.params.get("text") or ""),
                    ok=bool(action.success),
                    page_path=frame.page_path if frame else "",
                    note=(action.error or "")[:120],
                )
            )
        return entries[-limit:]

    async def _observe_and_decide_with_timeout(
        self,
        step: PlannedStep,
        snapshot: ScreenSnapshot | None,
        feedback: str | None,
        history: list[StepHistoryEntry],
    ) -> tuple[VisionObservation | None, ToolDecision]:
        """合并观测+决策的调用入口：同一帧的观测复用缓存，超时语义与 ``decide`` 保持一致。"""
        cached = self._observation_cache.get(snapshot.image_sha256) if snapshot is not None else None
        if cached is not None:
            return cached, await self._decide_with_timeout(step, snapshot, feedback)
        timeout = self.settings.agent_model_timeout
        retries = max(int(self.settings.agent_model_retry_limit), 0)
        attempt_feedback = feedback
        for attempt in range(retries + 1):
            try:
                return await asyncio.wait_for(
                    self.provider.observe_and_decide(
                        step, snapshot, feedback=attempt_feedback, history=history or None
                    ),
                    timeout=timeout,
                )
            except TimeoutError as exc:
                if attempt >= retries:
                    raise ToolExecutionError(
                        f"model decision timed out after {timeout:g} seconds",
                        RunState.FAILED_MODEL,
                    ) from exc
                attempt_feedback = (
                    "the previous attempt timed out. Reply with ONLY the single observation+decision JSON object "
                    "and do not expand long reasoning."
                )
            except ToolExecutionError:
                raise
            except Exception as exc:  # noqa: BLE001 - 模型侧故障（429/5xx/网关错误）必须可读且归类为 FAILED_MODEL
                if _is_transient_model_error(exc) and attempt < retries:
                    await self._wait_before_model_retry("observe+decide", exc)
                    continue
                raise ToolExecutionError(
                    f"model call failed: {type(exc).__name__}: {exc}", RunState.FAILED_MODEL
                ) from exc
        raise ToolExecutionError(  # pragma: no cover - 循环内必然 return 或 raise
            f"model decision timed out after {timeout:g} seconds", RunState.FAILED_MODEL
        )

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

    @staticmethod
    def _update_replay_summary(trace: RunTrace) -> None:
        """Keep replay summary fields aligned after direct orchestrator execution."""
        trace.replay_total = len(trace.replays)
        trace.replay_completed = len(trace.replays)
        trace.replay_passed = sum(item.passed for item in trace.replays)
        if trace.replay_total == 0:
            trace.replay_status = "not_requested"
        elif trace.replay_passed == trace.replay_total:
            trace.replay_status = "passed"
        elif trace.replay_passed:
            trace.replay_status = "partial"
        else:
            trace.replay_status = "failed"

    async def _fail(self, trace, emitter, state: RunState, message: str, request: RunRequest) -> None:
        trace.state = state
        trace.agent_outcome = "stopped" if state == RunState.STOPPED_BY_USER else "failed"
        trace.agent_error = message
        trace.error = message
        trace.ended_at = utc_now()
        event_type = EventType.ASSERTION_FAILED if state == RunState.FAILED_ASSERTION else EventType.RUN_FAILED
        emitter.emit(event_type, message, {"state": state})
        if any(action.success for action in trace.actions):
            # 失败路径同样收尾产物（计划 R3）：本次实测 13 个成功动作、7 个真实定位器全被丢弃。
            # 收尾过程会临时改写 state（SCRIPT_GENERATING），必须恢复原始失败状态与错误信息。
            try:
                await self._finalize_artifacts(trace, emitter, request, allow_replay=False)
            finally:
                trace.state = state
                trace.agent_error = message
                trace.error = message

    # ------------------------------------------------------------------
    # 用例沉淀与执行结果分析（均为可选注入，失败绝不影响运行结论）
    # ------------------------------------------------------------------

    def _save_case_from_trace(self, trace: RunTrace, emitter: Any) -> None:
        """把合格的运行自动沉淀为可复用用例；未注入用例库时什么都不做。"""
        if self.case_library is None:
            return
        try:
            record = self.case_library.build_from_run(trace)
        except Exception as exc:  # noqa: BLE001 - 用例沉淀失败不得影响运行结果
            logger.warning("case library save failed for %s: %s", trace.run_id, exc)
            return
        if record is None:
            # G5 之后这里只在两条物理必要条件不满足时命中（缺可回放动作 / 身份占位），
            # 或 trace 没有冻结 Profile 快照；留一条日志便于诊断「为什么没入库」。
            logger.info(
                "case not persisted for run %s: runnable_blockers=%s has_profile_snapshot=%s",
                trace.run_id,
                list(getattr(trace.generated, "runnable_blockers", []) or []),
                trace.profile_snapshot is not None,
            )
            return
        emitter.emit(
            EventType.CASE_SAVED,
            f"运行已沉淀为用例 {record.case_id}",
            {"case_id": record.case_id, "version": record.version, "scenario": str(record.scenario)},
        )

    async def _detect_in_run_anomalies(
        self,
        trace: RunTrace,
        emitter: Any,
        detector: Any,
        result: ActionResult,
        step: PlannedStep,
        before: ScreenSnapshot,
        after: ScreenSnapshot,
        decision: ToolDecision,
    ) -> None:
        """运行中检测一个变更类动作（缺口 2）。

        设计红线：检测是 **advisory** —— 命中只挂到 ``ActionResult.anomaly``、写进
        ``trace.defects`` 并发 ``ANOMALY_DETECTED`` 事件，**绝不**改变 ``result.success``、
        ``trace.state`` 或任何 assert 结论。最后的硬中止守卫仍然独立生效。
        """
        try:
            observation = await asyncio.to_thread(
                detector.observe,
                action_id=step.step_id,
                before=before,
                after=after,
                tool=decision.tool,
                target=decision.target or "",
            )
        except Exception as exc:  # noqa: BLE001 - 检测失败不得打断任务
            logger.warning("in-run detection failed for %s: %s: %s", step.step_id, type(exc).__name__, exc)
            return
        if observation is None or observation.finding is None:
            return
        finding = observation.finding
        result.anomaly = finding
        trace.defects.append(finding)
        payload = finding.model_dump(mode="json")
        payload["action_id"] = step.step_id
        payload["evidence_paths"] = list(observation.evidence_paths)
        emitter.emit(EventType.ANOMALY_DETECTED, finding.summary_zh, payload)

    @staticmethod
    def _analysis_bundle(trace: RunTrace) -> str:
        """分析 hook 的 bundle 归属来源：冻结 Profile → 解析目标 → 合成 Profile。"""
        for profile in (trace.profile_snapshot, getattr(trace.resolved_target, "profile_snapshot", None)):
            bundle = getattr(profile, "bundle_name", "") if profile is not None else ""
            if bundle:
                return str(bundle)
        synthetic = AgentOrchestrator._synthetic_profile(trace)
        return str(getattr(synthetic, "bundle_name", "") or "")

    def _attach_analysis(self, trace: RunTrace, emitter: Any) -> None:
        """对本次运行做执行结果分析，并把缺陷记为一等产物。

        分析永不翻转 passed（红线）。三步：

        1. ``analyze_run`` 做事后分析（失败只记日志）；
        2. ``trace.defects``（运行中）与 ``analysis.findings``（事后）**按 defect_id 合并去重**；
        3. 把合并后的 finding 交给可选的 :class:`DefectRecorder` 落库并发 ``DEFECT_RECORDED``。
        """
        analysis = None
        if self.analyzer is not None:
            try:
                analysis = self.analyzer.analyze_run(trace, self.artifacts.run_dir(trace.run_id))
            except Exception as exc:  # noqa: BLE001 - 分析是附加信息，失败只记日志
                logger.warning("execution analysis failed for %s: %s", trace.run_id, exc)
                analysis = None
            if analysis is not None:
                trace.analysis = analysis
                emitter.emit(
                    EventType.RESULT_ANALYSIS_FINISHED,
                    "执行结果分析完成" if analysis.healthy else f"执行结果分析发现 {len(analysis.findings)} 项异常",
                    analysis.model_dump(mode="json"),
                )
        self._record_defects(trace, emitter, analysis)

    def _record_defects(self, trace: RunTrace, emitter: Any, analysis: Any | None) -> None:
        """把运行中 + 事后发现的 finding 交给 recorder 归并落库（缺口 5 的核心）。

        ``DefectRecorder`` 按 ``(bundle, kind, page_path, action_target)`` 归并，因此
        「运行中发现的 finding」与「事后分析发现的同一问题」会并成同一条缺陷
        （``occurrences`` 累加），不需要额外的映射表。未注入 recorder 时只发事件、不落库。
        """
        candidates = [*trace.defects, *(analysis.findings if analysis is not None else [])]
        if self.defect_recorder is None or not candidates:
            return
        try:
            records = self.defect_recorder.record_from_findings(
                findings=candidates,
                bundle_name=self._analysis_bundle(trace),
                run_id=trace.run_id,
                device_id=trace.device_id,
            )
        except Exception as exc:  # noqa: BLE001 - 缺陷落库失败不得影响运行结论
            logger.warning("defect recording failed for %s: %s: %s", trace.run_id, type(exc).__name__, exc)
            return
        for record in records:
            emitter.emit(
                EventType.DEFECT_RECORDED,
                f"疑似应用缺陷：{record.title_zh}",
                record.model_dump(mode="json"),
            )

    # ------------------------------------------------------------------
    # 运行收尾：成功与失败路径共用，保证任何有成功动作的运行都留下产物
    # ------------------------------------------------------------------

    async def _finalize_artifacts(
        self,
        trace: RunTrace,
        emitter: RunEventEmitter,
        request: RunRequest,
        *,
        allow_replay: bool,
    ) -> None:
        """生成脚本、执行回放、附加分析与沉淀用例。

        成功路径与失败路径（``_fail`` 内以 ``allow_replay=False``）都调用：失败运行同样产出
        ``generated/`` 与 ``reports/``，语义由生成器判定的 ``purpose="diagnostic"`` 表达
        （计划 R3/G1）。除回放门禁外每一步失败都只记 warning，绝不覆盖原始失败状态与错误信息。
        """
        if request.auto_generate and trace.generated is None:
            try:
                trace.state = RunState.SCRIPT_GENERATING
                trace.generated = HypiumGenerator(
                    self.artifacts, min_observed_rounds=self.settings.profile_verification_rounds
                ).generate(trace, self._synthetic_profile(trace))
                emitter.emit(
                    EventType.SCRIPT_GENERATED,
                    "Hypium Python 和 JSON 已生成",
                    trace.generated.model_dump(mode="json"),
                )
            except Exception as exc:  # noqa: BLE001 - 收尾生成失败不得影响运行结论
                logger.warning("script generation failed for %s: %s", trace.run_id, exc)
        if allow_replay and request.auto_execute:
            if not trace.generated:
                raise ToolExecutionError("cannot execute before generating a script", RunState.FAILED_SCRIPT)
            trace.state = RunState.SCRIPT_EXECUTING
            emitter.emit(EventType.EXECUTION_STARTED, "开始执行生成的 Hypium 用例")
            runner = make_hypium_runner(
                self.settings,
                analyzer=self.analyzer,
                bundle_resolver=lambda: self._analysis_bundle(trace),
                device_id=trace.device_id,
                symptom_kind=("functional" if trace.scenario == ScenarioKind.BUG_REPRODUCTION else None),
            )
            trace.replays = await self._run_replays(
                runner, trace.generated, trace.run_id, self.settings.hypium_replay_attempts, emitter
            )
            self._update_replay_summary(trace)
            if not all(item.passed for item in trace.replays):
                raise ToolExecutionError("one or more Hypium replay attempts failed", RunState.FAILED_SCRIPT)
            emitter.emit(
                EventType.EXECUTION_FINISHED,
                f"Hypium 用例连续执行 {self.settings.hypium_replay_attempts} 次成功",
                {"replays": [item.model_dump(mode="json") for item in trace.replays]},
            )
        self._attach_analysis(trace, emitter)
        self._save_case_from_trace(trace, emitter)
        self._harvest_profile_evidence(trace, emitter)

    @staticmethod
    def _synthetic_profile(trace: RunTrace) -> TargetAppProfile | None:
        """实时模式（磁盘无任何 Profile）下的最小冻结快照，让产物生成路径仍然可用。

        这类运行没有任何经过设备验证的定位器：生成器据此把 ``live_mode`` 写进
        ``promotion_blockers``（不作为晋级证据），但脚本本身**可执行**——``purpose`` 与
        ``replay_eligible`` 只由「有可回放动作 + 应用身份非占位」两条物理条件决定，
        质量顾虑只体现在 ``confidence`` 上。
        无解析结果（例如目标解析阶段即失败）时返回 ``None``，生成器会自行报错并被降级为 warning。
        """
        if trace.profile_snapshot is not None:
            return trace.profile_snapshot
        resolved = trace.resolved_target
        if resolved is None:
            return None
        return TargetAppProfile(
            status=ProfileStatus.DRAFT,
            target_app_id=resolved.target_app_id,
            display_name=resolved.display_name,
            bundle_name=resolved.bundle_name,
            main_ability=resolved.main_ability,
            module_name=resolved.module_name,
            app_version=AppVersion(
                version_name=resolved.version_name,
                version_code=resolved.version_code,
                signature_sha256=resolved.signature_sha256,
            ),
            launch_strategy={
                "kind": "hdc_aa_start",
                "command_template": "aa start -b {bundle_name} -a {main_ability}",
            },
            reset_strategy={
                "kind": "stop_start_then_navigation_restore",
                "clear_app_data": False,
                "recovery_actions": [],
            },
            test_data_strategy={"fixed_input_text": "OpenHarmony", "secrets": []},
            permission_and_popup_strategy={"payment": "always_blocked", "delete": "always_blocked"},
            known_limitations=["live mode without a verified Profile: locators and assertions are unverified"],
            provenance=ProfileProvenance(
                discovery_run_id=trace.run_id,
                evidence={"verification_passed": False, "live_mode": True},
            ),
        )

    # ------------------------------------------------------------------
    # 任务期证据回收（计划 3.3）：把任务期真实解析成功的定位器累加回 Profile
    # ------------------------------------------------------------------

    def _harvest_profile_evidence(self, trace: RunTrace, emitter: RunEventEmitter) -> None:
        """收尾时把本次任务期证据累加进 Profile；任何失败都只记日志。"""
        if not self.settings.profile_harvest_enabled:
            return
        try:
            profile = self._synthetic_profile(trace)
            if profile is None:
                return
            registry = self._registry(trace.exploration_policy.min_interaction_kinds)
            harvested = self._harvest_task_evidence(trace, profile, registry, emitter)
            if harvested is not None:
                trace.profile_snapshot = harvested
        except Exception as exc:  # noqa: BLE001 - 证据回收失败不得影响运行结论
            logger.warning("task evidence harvest failed for %s: %s", trace.run_id, exc)

    def _bootstrap_policy(self, requested: ExplorationPolicy) -> ExplorationPolicy:
        """把 bootstrap 预算注入探索策略；请求显式给出的字段优先（计划 4.2）。

        ``ExplorationPolicy`` 原先只能按请求设置，``max_duration_seconds`` 默认 900 且没有
        对应 Settings 项，无法用 env 调整：本次日历运行因此烧掉 473 s 纯探索。
        """
        explicit = set(requested.model_fields_set)
        updates: dict[str, object] = {}
        if "max_pages" not in explicit:
            updates["max_pages"] = min(int(self.settings.bootstrap_max_pages), 20)
        if "max_actions_per_page" not in explicit:
            updates["max_actions_per_page"] = min(int(self.settings.bootstrap_max_actions_per_page), 8)
        if "max_duration_seconds" not in explicit:
            updates["max_duration_seconds"] = min(int(self.settings.bootstrap_max_duration_seconds), 900)
        if "settle_timeout_seconds" not in explicit:
            updates["settle_timeout_seconds"] = min(int(self.settings.bootstrap_settle_timeout_seconds), 30)
        if "advisor_enabled" not in explicit:
            updates["advisor_enabled"] = bool(self.settings.bootstrap_advisor_enabled)
        return requested.model_copy(update=updates) if updates else requested

    def _admission_thresholds(self, min_interaction_kinds: int | None = None) -> AdmissionThresholds:
        """门禁阈值：统一从 ``Settings`` 读取（计划 4.1/4.2）。"""
        return AdmissionThresholds.from_settings(
            self.settings,
            min_interaction_kinds=min_interaction_kinds,
            min_evidence_rounds=self.settings.profile_verification_rounds,
        )

    @staticmethod
    def _default_admission_thresholds() -> AdmissionThresholds:
        """静态调用点（``_profile_validation_trace``，也被 dc/distill 复用）的默认阈值。"""
        from ..config import get_settings

        return AdmissionThresholds.from_settings(get_settings())

    def _registry(self, min_interaction_kinds: int = 2) -> ProfileRegistry:
        return ProfileRegistry(
            self.settings.resolved_profiles_dir,
            min_interaction_kinds=min_interaction_kinds,
            promotion_replay_attempts=self.settings.hypium_replay_attempts,
            min_evidence_rounds=self.settings.profile_verification_rounds,
        )

    def _harvest_task_evidence(
        self,
        trace: RunTrace,
        profile: TargetAppProfile,
        registry: ProfileRegistry,
        emitter: RunEventEmitter,
    ) -> TargetAppProfile | None:
        """把 ``trace`` 里真实成功动作的定位器与断言合并进 ``profile`` 并存为 draft。

        合并语义（不是覆盖）：同 ``(kind, value, page_signature)`` 的条目 ``observed_rounds += 1``，
        新条目以 1 起算——这是「多次运行累加到门禁」的机制（计划 3.3/G2）。

        安全边界（计划 3.4）：回收产物**绝不**写入 ``verification_passed``，因此
        ``registry.save_candidate`` 的准入校验必然拒绝它，只能停留在 draft；要晋级必须真的
        跑绿一次 ``ProfileVerifier.verify``。若未来配置使准入通过，这里也会自然走 candidate。
        """
        if profile.status == ProfileStatus.VERIFIED:
            # 已验证 Profile 已含设备验证证据与完整资产：把任务期观测并进去会生成一份继承了
            # verification_passed 的 candidate（真机 run-20260921T063745Z-ba36e30e 实测），
            # 之后可被 append_replay_evidence 直接晋级——等于用未经设备验证的定位器替换已验证
            # 资产。明确不做：已验证 Profile 的资产更新只能走完整验证 + 晋级。
            logger.info("skip task evidence harvest for verified profile %s", profile.target_app_id)
            return None
        locators = self._collect_task_evidence(trace, profile)
        assertions = [item for item in trace.assertions if item.passed]
        if not locators and not assertions:
            return None
        merged = self._merge_profile_evidence(trace, profile, locators, assertions)
        try:
            registry.save_candidate(merged)
        except Exception:  # noqa: BLE001 - 未获验证证据时按设计退化为 draft
            registry.save_draft(merged)
        emitter.emit(
            EventType.LOCATOR_CANDIDATE_OBSERVED,
            f"任务期回收 {len(locators)} 个定位器与 {len(assertions)} 条断言证据",
            {
                "run_id": trace.run_id,
                "locators": [{"kind": str(item.candidate.kind), "value": item.candidate.value} for item in locators],
                "assertions": [{"kind": str(item.kind), "target": item.target} for item in assertions],
            },
        )
        emitter.emit(
            EventType.PROFILE_HARVESTED,
            f"Profile 证据已累加（定位器 {len(merged.stable_locator_inventory)}、"
            f"断言 {len(merged.assertion_inventory)}、状态 {merged.status}）",
            {
                "status": str(merged.status),
                "locators": len(merged.stable_locator_inventory),
                "assertions": len(merged.assertion_inventory),
                "page_states": len({item.page_signature for item in merged.stable_locator_inventory}),
            },
        )
        return merged

    def _collect_task_evidence(self, trace: RunTrace, profile: TargetAppProfile) -> list[HarvestedLocator]:
        """从任务期轨迹收集可入库的定位器证据。

        数据来源全部真实存在却被丢弃（计划 3.3）：``trace.actions[*].locator`` 与
        ``before_snapshot_id`` 指向的原始帧；当动作只解析出坐标类候选（SPATIAL / VLM_BBOX）
        时，按 ``params["target"]`` 回查该帧的原始 ``UIElement`` 取出 key/id 作为长期证据。
        """
        snapshot_by_id = {item.snapshot_id: item for item in trace.snapshots}
        foreground = ForegroundApp(bundle_name=profile.bundle_name, ability_name=profile.main_ability)
        identity_cache: dict[str, str] = {}
        harvested: list[HarvestedLocator] = []
        for action in trace.actions:
            if not action.success:
                continue
            snapshot_id = action.before_snapshot_id or ""
            snapshot = snapshot_by_id.get(snapshot_id)
            candidate = self._harvest_candidate(action, snapshot)
            if candidate is None:
                continue
            if candidate.kind in {LocatorKind.KEY, LocatorKind.ID} and is_volatile_evidence_key(candidate.value):
                # 易变 key（时钟/日期格/列表实例序号）直接丢弃，不入库（计划 3.3/2.2）。
                continue
            page_signature = ""
            if snapshot is not None:
                page_signature = identity_cache.setdefault(
                    snapshot.snapshot_id, BoundedExplorer._structural_identity(snapshot, foreground)
                )
            harvested.append(
                HarvestedLocator(candidate=candidate, page_signature=page_signature, snapshot_id=snapshot_id)
            )
        return harvested

    @staticmethod
    def _harvest_candidate(action: ActionResult, snapshot: ScreenSnapshot | None) -> LocatorCandidate | None:
        """把一次成功动作还原成可长期复用的定位器候选；坐标类回退不作为长期证据。"""
        usable = {LocatorKind.KEY, LocatorKind.ID, LocatorKind.TEXT, LocatorKind.TYPE_TEXT}
        candidate = action.locator
        if candidate is not None and candidate.kind in usable and candidate.value:
            return candidate
        target = str(action.params.get("target") or "")
        if snapshot is not None and target:
            element = next((item for item in snapshot.elements if item.element_id == target), None)
            if element is None:
                element = next(
                    (item for item in snapshot.elements if target in {item.key, item.id, item.content}), None
                )
            if element is not None:
                for item in element.locator_candidates:
                    if item.kind in usable and item.value:
                        return item
                if element.key:
                    return LocatorCandidate(kind=LocatorKind.KEY, value=element.key, score=1)
                if element.id:
                    return LocatorCandidate(kind=LocatorKind.ID, value=element.id, score=1)
                if element.type and element.content:
                    return LocatorCandidate(
                        kind=LocatorKind.TYPE_TEXT, value=f"{element.type}|{element.content}", score=0.85
                    )
        return None  # SPATIAL / VLM_BBOX / COORDINATE：resolution-bound，不入库

    @staticmethod
    def _merge_profile_evidence(
        trace: RunTrace,
        profile: TargetAppProfile,
        harvested: list[HarvestedLocator],
        assertions: list[AssertionResult],
    ) -> TargetAppProfile:
        """合并语义：同键累加轮数，新键以 1 起算；绝不修改 verification_passed。"""
        # 同一次运行内同一帧的重复观测只算一次：observed_rounds 是「跨轮观察次数」，
        # 被同一轮里的重复动作抬升会让门禁统计失去意义。
        seen: set[tuple[str, str, str, str]] = set()
        unique: list[HarvestedLocator] = []
        for item in harvested:
            key = (str(item.candidate.kind), item.candidate.value, item.page_signature, item.snapshot_id)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        incoming = [AgentOrchestrator._stable_locator_from_candidate(item, trace.run_id) for item in unique]
        merged_locators = AgentOrchestrator._merge_locators(profile.stable_locator_inventory, incoming)
        merged_assertions = AgentOrchestrator._merge_assertions(
            profile.assertion_inventory, assertions, merged_locators, trace.run_id
        )
        evidence = dict(profile.provenance.evidence)
        if evidence.get("verification_passed"):
            # 纵深防御：回收证据不是设备验证证据，合并结果永远不得继承「验证通过」。
            evidence["verification_passed"] = False
            evidence["verification_invalidated_by_harvest"] = True
        return profile.model_copy(
            update={
                "status": ProfileStatus.DRAFT,
                "stable_locator_inventory": merged_locators,
                "assertion_inventory": merged_assertions,
                "provenance": profile.provenance.model_copy(update={"evidence": evidence}, deep=True),
            },
            deep=True,
        )

    @staticmethod
    def _stable_locator_from_candidate(harvested: HarvestedLocator, run_id: str) -> StableLocator:
        """把运行时候选定位器转成长期证据条目；动态 key 折叠为稳定前缀模式。"""
        candidate = harvested.candidate
        values = {"key": "", "id": "", "text": "", "type": ""}
        if candidate.kind == LocatorKind.TYPE_TEXT:
            values["type"], _, values["text"] = candidate.value.partition("|")
        elif candidate.kind in {LocatorKind.KEY, LocatorKind.ID, LocatorKind.TEXT}:
            values[candidate.kind.value] = candidate.value
        confidence = (
            ConfidenceLevel.HIGH
            if StabilityAnalyzer._level(candidate.kind) == StabilityLevel.HIGH
            else ConfidenceLevel.MEDIUM
        )
        warning = None
        dynamic_pattern = None
        if candidate.kind in {LocatorKind.KEY, LocatorKind.ID} and is_dynamic_identifier(candidate.value):
            dynamic_pattern = dynamic_identifier_pattern(candidate.value)
            confidence = ConfidenceLevel.MEDIUM
            warning = f"identifier appears dynamic; reusable prefix {dynamic_pattern}"
        name = (candidate.value.split("|", 1)[-1] or candidate.value)[:80]
        return StableLocator(
            name=name,
            page_signature=harvested.page_signature,
            confidence=confidence,
            observed_rounds=1,
            unique_match_rounds=1,
            source="live_task_harvest",
            dynamic_pattern=dynamic_pattern,
            warning=warning,
            evidence_snapshot_ids=[harvested.snapshot_id] if harvested.snapshot_id else [],
            last_observed_at=utc_now(),
            **values,
        )

    @staticmethod
    def _merge_locators(existing: list[StableLocator], incoming: list[StableLocator]) -> list[StableLocator]:
        def key_of(item: StableLocator) -> tuple[str, str, str, str, str]:
            return (item.key, item.id, item.text, item.type, item.page_signature)

        merged = [item.model_copy(deep=True) for item in existing]
        index = {key_of(item): item for item in merged}
        for candidate in incoming:
            found = index.get(key_of(candidate))
            if found is None:
                merged.append(candidate)
                index[key_of(candidate)] = candidate
                continue
            # 累加语义：同一 (kind, value, page_signature) 再次观测到即递增轮数。
            found.observed_rounds = found.observed_rounds + 1
            found.unique_match_rounds = found.unique_match_rounds + 1
            found.last_observed_at = utc_now()
            for snapshot_id in candidate.evidence_snapshot_ids:
                if snapshot_id not in found.evidence_snapshot_ids:
                    found.evidence_snapshot_ids.append(snapshot_id)
            if found.dynamic_pattern is None and candidate.dynamic_pattern is not None:
                found.dynamic_pattern = candidate.dynamic_pattern
            if found.warning is None and candidate.warning is not None:
                found.warning = candidate.warning
        return merged

    @staticmethod
    def _merge_assertions(
        existing: list[AssertionDefinition],
        incoming: list[AssertionResult],
        locators: list[StableLocator],
        run_id: str,
    ) -> list[AssertionDefinition]:
        merged = [item.model_copy(deep=True) for item in existing]
        index = {(item.kind, item.target, item.page_signature): item for item in merged}
        for item in incoming:
            page_signature = ""
            for locator in locators:
                if locator.name and locator.name in item.target:
                    page_signature = locator.page_signature
                    break
            key = (str(item.kind), item.target, page_signature)
            found = index.get(key)
            if found is None:
                merged.append(
                    AssertionDefinition(
                        name=f"harvested-{len(merged) + 1}",
                        kind=str(item.kind),
                        target=item.target,
                        page_signature=page_signature,
                        confidence=ConfidenceLevel.MEDIUM,
                        observed_rounds=1,
                        evidence_snapshot_ids=[run_id],
                    )
                )
                index[key] = merged[-1]
                continue
            found.observed_rounds = found.observed_rounds + 1
            if run_id not in found.evidence_snapshot_ids:
                found.evidence_snapshot_ids.append(run_id)
        return merged


# ---------------------------------------------------------------------------
# 场景标签与缺陷复现规划（模块级：纯函数，便于单测）
# ---------------------------------------------------------------------------


def _scenario_for(request: RunRequest) -> ScenarioKind:
    """没有显式 ``scenario`` 时按运行形态推导场景标签（不复活 RunMode 业务分支）。"""
    if request.bug_report is not None:
        return ScenarioKind.BUG_REPRODUCTION
    if request.bootstrap_only:
        return ScenarioKind.SMOKE
    return ScenarioKind.CORE_FLOW


def _plan_from_bug_repro(plan: Any, request: RunRequest) -> PlanResult:
    """把 ``BugReproPlan`` 转成现有执行器可跑的 ``PlanResult``。

    检查点草案里能在运行时校验的部分转成追加的 ``ASSERT_*`` 步骤；纯 IR 语义
    （toast / current_app / page_signature / screenshot）留作用例生成时的检查点。
    """
    steps: list[PlannedStep] = list(plan.steps)
    drafts = list(plan.checkpoints)
    if plan.symptom_checkpoint is not None:
        drafts.append(plan.symptom_checkpoint)
    for index, draft in enumerate(drafts, start=1):
        step = _assert_step_from_draft(draft, f"repro-assert-{index:02d}")
        if step is not None:
            steps.append(step)
    goal = plan.title_zh
    if not goal and request.bug_report is not None:
        goal = request.bug_report.title
    return PlanResult(
        goal=goal or request.task,
        steps=steps,
        model_used=getattr(plan, "model_used", "mock"),
        mock=bool(getattr(plan, "mock", True)),
    )


def _assert_step_from_draft(draft: Any, step_id: str) -> PlannedStep | None:
    """把检查点草案映射为可在真实运行时求值的断言步骤；不可表达时返回 ``None``。"""
    kind = str(getattr(draft, "kind", ""))
    target = str(getattr(draft, "target", "") or "")
    expected = getattr(draft, "expected", None)
    if kind == "element_exists" and target:
        return PlannedStep(
            step_id=step_id,
            instruction=f"断言「{target}」可见",
            tool=ToolName.ASSERT_VISIBLE,
            target=target,
        )
    if kind == "element_absent" and target:
        return PlannedStep(
            step_id=step_id,
            instruction=f"断言「{target}」不可见",
            tool=ToolName.ASSERT_NOT_VISIBLE,
            target=target,
        )
    if kind in {"text_equals", "text_contains"}:
        text = str(expected or target)
        if text:
            return PlannedStep(
                step_id=step_id,
                instruction=f"断言文本「{text}」",
                tool=ToolName.ASSERT_TEXT,
                target=text,
            )
    return None
