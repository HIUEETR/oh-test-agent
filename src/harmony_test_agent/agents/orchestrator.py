from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable

from ..config import Settings
from ..devices import DeviceAdapter, DeviceError, HarmonyDeviceAdapter
from ..generation import HypiumGenerator
from ..graph import PageGraphBuilder
from ..models import (
    ActionResult,
    EventType,
    RunEvent,
    RunRequest,
    RunState,
    RunTrace,
    ScreenSnapshot,
    ToolName,
    utc_now,
)
from ..perception import PerceptionService
from ..reporting import ReportBuilder
from ..runner import HypiumRunner
from ..runtime import RunEventEmitter, SafetyError, SafetyPolicy, ToolExecutionError, ToolExecutor
from ..storage import ArtifactStore, RunRepository
from .providers import AgentProvider, create_provider

DeviceFactory = Callable[[str], DeviceAdapter]


class AgentOrchestrator:
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

    def _default_device_factory(self, device_id: str) -> DeviceAdapter:
        return HarmonyDeviceAdapter(device_id, self.settings.hdc_path, self.settings.agent_action_timeout)

    def request_stop(self, run_id: str) -> None:
        self._stop_requested.add(run_id)

    async def run(self, request: RunRequest, run_id: str | None = None) -> RunTrace:
        run_id = run_id or f"run-{utc_now():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        profile = self.artifacts.load_profile(self.settings.resolved_target_profile_path)
        if profile.target_app_id != request.target_app_id:
            raise ValueError(f"unknown target_app_id: {request.target_app_id}")
        device_id = request.device_id or profile.device_selector.get("serial") or self.settings.harmony_device
        trace = RunTrace(
            run_id=run_id,
            target_app_id=profile.target_app_id,
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
        executor = ToolExecutor(device=device, profile=profile, safety=SafetyPolicy())
        current_snapshot: ScreenSnapshot | None = None
        current_node = None
        unchanged_count = 0

        emitter.emit(EventType.RUN_STARTED, "运行任务已创建", {"mode": request.mode, "model": self.provider.name})
        try:
            executor.safety.validate_task(request.task)
            trace.state = RunState.PREFLIGHT
            await asyncio.to_thread(device.connect)
            health = await asyncio.to_thread(device.health_check)
            if not health.get("connected"):
                raise DeviceError(f"device health check failed: {health}")
            emitter.emit(EventType.PREFLIGHT_PASSED, "设备预检通过", health)

            trace.state = RunState.PLANNING
            try:
                step_limit = min(request.max_steps, self.settings.agent_max_steps)
                plan = await asyncio.wait_for(
                    self.provider.plan(request.task, profile, step_limit),
                    timeout=self.settings.agent_action_timeout,
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
                        timeout=self.settings.agent_action_timeout,
                    )
                except Exception as exc:
                    raise ToolExecutionError(f"model tool decision failed: {exc}", RunState.FAILED_MODEL) from exc
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

            if request.auto_generate:
                trace.state = RunState.SCRIPT_GENERATING
                trace.generated = HypiumGenerator(self.artifacts).generate(trace, profile)
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
                trace.replays = await asyncio.to_thread(runner.execute_repeated, trace.generated, 3)
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
            await asyncio.to_thread(device.close)
            self.repository.save_trace(trace)
            self.artifacts.save_trace(trace)
            ReportBuilder(self.artifacts).build(trace)

    async def _capture(self, trace, emitter, device, perception, graph, label):
        run_dir = self.artifacts.run_dir(trace.run_id)
        snapshot = await asyncio.to_thread(device.screenshot, run_dir / "screens", trace.run_id, label)
        try:
            observation = await asyncio.wait_for(
                self.provider.analyze(snapshot),
                timeout=self.settings.agent_action_timeout,
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
