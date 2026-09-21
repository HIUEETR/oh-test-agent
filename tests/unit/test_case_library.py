"""``CaseLibrary`` 的产物布局、版本去重、安全降级、重跑与查询委托测试。

全程离线：两个 runner 与 analyzer 都用注入的替身，**不接触任何设备**。

``CaseLibrary`` 是计划 B2 的用例库门面，覆盖：

* ``save_built`` 的精确产物布局（``<cases_root>/<case_id>/v<N>/`` + ``standalone/`` 嵌套）；
* 版本语义：相同 spec 复用版本号、变更 spec 追加版本；
* ``build_from_run`` / ``build_from_dc`` 的 ``replay_eligible`` 门槛；
* 静态安全门禁违规 → ``status="draft"`` + 双留痕（tag + ``safety_violations.json``）；
* ``execute`` 的双引擎分支、``extra_env`` 注入、pending→终态的 upsert 执行记录；
* ``cases_root`` 安全边界（越界路径一律拒绝）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from harmony_test_agent.cases.builder import PLACEHOLDER_BUNDLE, CaseBuildResult
from harmony_test_agent.cases.library import (
    CASE_SPEC_FILENAME,
    DEVICE_SN_ENV,
    ENGINE_XDEVICE,
    EXECUTIONS_DIRNAME,
    PARAMS_ENV,
    SAFETY_VIOLATION_TAG,
    SAFETY_VIOLATIONS_FILENAME,
    STANDALONE_DIRNAME,
    XDEVICE_DIRNAME,
    CaseLibrary,
    safe_script_id,
    scenario_for_trace,
)
from harmony_test_agent.cases.spec import (
    CaseExecutionRecord,
    CaseProvenance,
    CheckpointKind,
    CheckpointSpec,
    LocatorSpec,
    ScenarioKind,
    StepAction,
)

# 别名：避免 pytest 把 TestCaseSpec / TestStepSpec 当测试类收集（PytestCollectionWarning）。
from harmony_test_agent.cases.spec import TestCaseSpec as CaseSpec
from harmony_test_agent.cases.spec import TestStepSpec as StepSpec
from harmony_test_agent.dc.models import DcSessionSnapshot, DcToolInvocation, DcToolName, DcToolTier
from harmony_test_agent.models import (
    ActionResult,
    CommandResult,
    ExecutionAnalysis,
    LocatorCandidate,
    LocatorKind,
    ReplayError,
    ReplayResult,
    RunState,
    RunTrace,
    TargetAppProfile,
    ToolName,
    UIElement,
)
from harmony_test_agent.storage.case_repository import CaseRepository

CASE_ID = "case-20240101T000000Z-abc123"
TARGET_APP = "com.example.notes"
# 固定 provenance 时间：spec_json 的字节级比较依赖它（``CaseProvenance.created_at`` 默认取当前时间）。
CREATED_AT = datetime(2024, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------


def make_spec(
    *,
    case_id: str = CASE_ID,
    title_zh: str = "登录流程",
    scenario: ScenarioKind = ScenarioKind.CORE_FLOW,
    tags: tuple[str, ...] = (),
    target_app_id: str | None = TARGET_APP,
) -> CaseSpec:
    """构造一个最小但合法的用例 IR：一步点击 + 一个硬检查点（两个 emitter 都能渲染）。"""
    locator = LocatorSpec(kind=LocatorKind.KEY, value="login_button", target_label="登录按钮")
    checkpoint = CheckpointSpec(kind=CheckpointKind.ELEMENT_EXISTS, message_zh="登录按钮存在", locator=locator)
    step = StepSpec(
        step_id="step-1",
        index=1,
        action=StepAction.CLICK,
        title_zh="点击登录按钮",
        locator=locator,
        checkpoints=[checkpoint],
    )
    return CaseSpec(
        case_id=case_id,
        slug="login-flow",
        title_zh=title_zh,
        scenario=scenario,
        status="active",
        tags=list(tags),
        bundle_name="com.example.notes",
        main_ability="EntryAbility",
        device_sn="device-1",
        provenance=CaseProvenance(
            source_kind="manual",
            source_id="manual-1",
            profile_target_app_id=target_app_id,
            created_at=CREATED_AT,
        ),
        steps=[step],
    )


def make_built(spec: CaseSpec | None = None) -> CaseBuildResult:
    """把一个 spec 包成 ``CaseBuildResult``（``save_built`` 的唯一入参形态）。"""
    return CaseBuildResult(spec=spec or make_spec(), replay_eligible=True, purpose="acceptance")


def make_profile() -> TargetAppProfile:
    """被测应用 Profile（``from_trace`` 需要它提供 bundle/ability 与启动等待）。"""
    return TargetAppProfile(
        target_app_id="notes-app",
        display_name="便签",
        bundle_name="com.example.notes",
        main_ability="EntryAbility",
        launch_strategy={"wait_seconds": 3},
    )


def make_eligible_trace(
    *,
    run_id: str = "run-20240101T000000Z-aaa111",
    phase: str = "task",
    provisional: bool = False,
    live_mode: bool = False,
    scenario: ScenarioKind | None = None,
) -> RunTrace:
    """可回放的 Live 轨迹：点击 → 显式断言 → FINISH，带冻结 Profile。

    ``phase="task"`` 表示常规任务运行（``bootstrap`` 会被映射为 ``SMOKE`` 场景）。
    """
    locator = LocatorCandidate(kind=LocatorKind.KEY, value="login_button")
    return RunTrace(
        run_id=run_id,
        target_app_id="notes-app",
        task="登录流程",
        device_id="device-1",
        state=RunState.COMPLETED,
        agent_outcome="completed",
        started_at=CREATED_AT,
        phase=phase,  # type: ignore[arg-type]
        provisional=provisional,
        live_mode=live_mode,
        scenario=scenario,
        profile_snapshot=make_profile(),
        actions=[
            ActionResult(
                step_id="click",
                tool=ToolName.CLICK_ELEMENT,
                success=True,
                params={"target": "登录按钮"},
                locator=locator,
            ),
            ActionResult(
                step_id="assert",
                tool=ToolName.ASSERT_VISIBLE,
                success=True,
                params={"target": "登录按钮"},
                locator=locator,
            ),
            ActionResult(step_id="finish", tool=ToolName.FINISH, success=True),
        ],
    )


def make_dc_snapshot(
    *,
    session_id: str = "dc-20240101T000000Z-bbb222",
    invocations: list[DcToolInvocation] | None = None,
) -> DcSessionSnapshot:
    """DC 会话快照：一次点击 + 一次显式断言（默认满足 ``replay_eligible``）。"""
    element = UIElement(element_id="ui-login", key="login_button", type="Button", clickable=True)
    return DcSessionSnapshot(
        session_id=session_id,
        device_id="device-1",
        created_at=CREATED_AT,
        invocations=invocations
        if invocations is not None
        else [
            DcToolInvocation(
                invocation_id="inv-click",
                turn_id="turn-1",
                tool=DcToolName.CLICK,
                tier=DcToolTier.L2,
                resolved_element=element,
            ),
            DcToolInvocation(
                invocation_id="inv-assert",
                turn_id="turn-1",
                tool=DcToolName.ASSERT_VISIBLE,
                tier=DcToolTier.L2,
                args={"target": "登录按钮"},
                resolved_element=element,
            ),
        ],
    )


def make_replay(
    *,
    passed: bool,
    status: str | None = None,
    error: ReplayError | None = None,
    report_path: Path | None = None,
) -> ReplayResult:
    """构造一次回放结果（``status`` 缺省按 ``passed`` 推断）。"""
    return ReplayResult(
        attempt=1,
        command=CommandResult(
            command="python test_case.py",
            args=["python", "test_case.py"],
            returncode=0 if passed else 1,
        ),
        report_path=report_path,
        passed=passed,
        status=status or ("passed" if passed else "failed"),
        error=error,
    )


class FakeHypiumRunner:
    """替身 runner：记录调用参数并按预设返回，绝不启动子进程。"""

    def __init__(self, result: ReplayResult, calls: list[dict[str, Any]], on_execute: Any = None) -> None:
        self.result = result
        self.calls = calls
        self.on_execute = on_execute

    def execute_diagnostic(
        self,
        python_path: Path,
        attempt: int = 1,
        *,
        extra_env: dict[str, str] | None = None,
    ) -> ReplayResult:
        """记录 ``python_path`` / ``attempt`` / ``extra_env`` 后返回预设结果。"""
        self.calls.append({"python_path": Path(python_path), "attempt": attempt, "extra_env": extra_env})
        if self.on_execute is not None:
            self.on_execute()
        return self.result


class FakeXDeviceRunner:
    """替身 xdevice runner：只记录 ``XDeviceProject`` 与 ``extra_env``。"""

    def __init__(self, result: Any, calls: list[dict[str, Any]]) -> None:
        self.result = result
        self.calls = calls

    def execute(
        self,
        project: Any,
        attempt: int = 1,
        *,
        params: dict | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> Any:
        """记录工程描述与注入参数后返回预设结果。"""
        self.calls.append({"project": project, "attempt": attempt, "params": params, "extra_env": extra_env})
        return self.result


class RecordingAnalyzer:
    """替身分析与器：记录调用并返回固定分析结果（或抛错）。"""

    def __init__(self, *, analysis: ExecutionAnalysis | None = None, error: Exception | None = None) -> None:
        self.analysis = analysis
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def analyze_replay(self, replay: Any, **kwargs: Any) -> ExecutionAnalysis | None:
        """记录回放分析调用。"""
        self.calls.append({"replay": replay, **kwargs})
        if self.error is not None:
            raise self.error
        return self.analysis


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def cases_root(tmp_path: Path) -> Path:
    """用例库落盘根目录（对应 ``Settings.resolved_cases_dir``）。"""
    return tmp_path / "artifacts" / "cases"


def make_library(
    tmp_path: Path,
    cases_root: Path,
    *,
    runner_factory: Any = None,
    xdevice_runner_factory: Any = None,
    analyzer: Any = None,
) -> CaseLibrary:
    """构造一个使用独立 ``agent.db`` 的用例库。"""
    repository = CaseRepository(tmp_path / "agent.db")
    return CaseLibrary(
        repository,
        cases_root,
        runner_factory=runner_factory,
        xdevice_runner_factory=xdevice_runner_factory,
        analyzer=analyzer,
    )


def hypium_factory(
    result: ReplayResult,
    calls: list[dict[str, Any]],
    seen: list[tuple[Path, float]] | None = None,
    on_execute: Any = None,
):
    """构造 ``runner_factory``：记录 ``(runtime_home, timeout)`` 并返回替身 runner。"""

    def factory(runtime_home: Path, timeout: float) -> FakeHypiumRunner:
        if seen is not None:
            seen.append((Path(runtime_home), timeout))
        return FakeHypiumRunner(result, calls, on_execute=on_execute)

    return factory


# ---------------------------------------------------------------------------
# 产物布局与版本
# ---------------------------------------------------------------------------


def test_save_built_writes_exact_artifact_layout(tmp_path: Path, cases_root: Path):
    library = make_library(tmp_path, cases_root)

    record = library.save_built(make_built())

    assert record.version == 1
    assert record.case_id == CASE_ID
    assert record.status == "active"
    assert record.source_kind == "manual"

    version_dir = cases_root / CASE_ID / "v1"
    assert record.artifact_dir == str(version_dir)
    assert library.artifact_dir(CASE_ID, 1) == version_dir
    # 精确布局：只有这三个条目（hypium/ 与 executions/ 是执行期产物）。
    assert {path.name for path in version_dir.iterdir()} == {
        CASE_SPEC_FILENAME,
        STANDALONE_DIRNAME,
        XDEVICE_DIRNAME,
    }

    safe_id = safe_script_id(CASE_ID)
    assert (version_dir / STANDALONE_DIRNAME / f"test_{safe_id}.py").is_file()
    assert (version_dir / STANDALONE_DIRNAME / f"test_{safe_id}.json").is_file()
    # standalone/ 这层嵌套是有意的：HypiumRunner 取 python_path.parent.parent 当 run_dir。
    # xdevice 用例文件名必须等于 TestCase 类名（Phase 0 真机实测 devicetest Script-0203016）。
    assert (version_dir / XDEVICE_DIRNAME / "testcases" / "HarmonyAgentCases" / "HarmonyAgentCase.py").is_file()
    assert (version_dir / XDEVICE_DIRNAME / "testlist.txt").is_file()

    stored = CaseSpec.model_validate_json((version_dir / CASE_SPEC_FILENAME).read_text(encoding="utf-8"))
    assert stored.model_dump() == make_spec().model_dump()

    config = json.loads((version_dir / STANDALONE_DIRNAME / f"test_{safe_id}.json").read_text(encoding="utf-8"))
    assert config["case_id"] == CASE_ID
    assert config["case_version"] == 1
    assert config["purpose"] == "acceptance"
    assert config["replay_eligible"] is True
    assert config["standalone_python"] == f"test_{safe_id}.py"


def test_identical_spec_reuses_version_and_changed_spec_appends(tmp_path: Path, cases_root: Path):
    library = make_library(tmp_path, cases_root)
    built = make_built()

    first = library.save_built(built)
    # 同一个 built、以及内容等价的新 built，都不应灌水版本号（仓库按 spec_json 去重）。
    second = library.save_built(built)
    third = library.save_built(make_built())

    assert (first.version, second.version, third.version) == (1, 1, 1)
    assert [item.version for item in library.versions(CASE_ID)] == [1]

    changed = library.save_built(make_built(make_spec(title_zh="登录流程（回归）")))

    assert changed.version == 2
    assert changed.title_zh == "登录流程（回归）"
    assert [item.version for item in library.versions(CASE_ID)] == [2, 1]
    # 新版本目录同样是完整布局，旧版本行保持不变。
    assert (library.artifact_dir(CASE_ID, 2) / CASE_SPEC_FILENAME).is_file()
    assert library.get(CASE_ID, 1).title_zh == "登录流程"  # type: ignore[union-attr]


def test_save_built_overrides_device_sn(tmp_path: Path, cases_root: Path):
    library = make_library(tmp_path, cases_root)
    built = make_built()

    record = library.save_built(built, device_sn="SN-OVERRIDE")

    assert record.spec.device_sn == "SN-OVERRIDE"
    # 深拷贝：调用方的 built.spec 不被就地改写。
    assert built.spec.device_sn == "device-1"


# ---------------------------------------------------------------------------
# 安全门禁降级
# ---------------------------------------------------------------------------


def test_forbidden_word_downgrades_to_draft_with_audit_trail(tmp_path: Path, cases_root: Path):
    library = make_library(tmp_path, cases_root)
    locator = LocatorSpec(kind=LocatorKind.KEY, value="delete_button", target_label="删除按钮")
    checkpoint = CheckpointSpec(kind=CheckpointKind.ELEMENT_EXISTS, message_zh="按钮存在", locator=locator)
    spec = make_spec()
    spec.steps = [
        StepSpec(
            step_id="step-1",
            index=1,
            action=StepAction.INPUT_TEXT,
            title_zh="输入删除关键词",
            locator=locator,
            text="点击删除按钮",
            checkpoints=[checkpoint],
        )
    ]

    record = library.save_built(make_built(spec))

    assert record.status == "draft"
    assert SAFETY_VIOLATION_TAG in record.tags
    assert record.spec.status == "draft"
    # 违规明细逐条落盘，且能通过 tag 过滤定位。
    violations_path = library.artifact_dir(CASE_ID, 1) / SAFETY_VIOLATIONS_FILENAME
    payload = json.loads(violations_path.read_text(encoding="utf-8"))
    assert payload["case_id"] == CASE_ID
    assert any("删除" in item for item in payload["violations"])
    assert [item.case_id for item in library.list(tag=SAFETY_VIOLATION_TAG)] == [CASE_ID]


def test_clean_spec_stays_active_without_violation_file(tmp_path: Path, cases_root: Path):
    library = make_library(tmp_path, cases_root)

    record = library.save_built(make_built())

    assert record.status == "active"
    assert SAFETY_VIOLATION_TAG not in record.tags
    assert not (library.artifact_dir(CASE_ID, 1) / SAFETY_VIOLATIONS_FILENAME).exists()


# ---------------------------------------------------------------------------
# build_from_run / build_from_dc
# ---------------------------------------------------------------------------


def test_build_from_run_without_frozen_profile_returns_none(tmp_path: Path, cases_root: Path):
    library = make_library(tmp_path, cases_root)
    trace = make_eligible_trace()
    trace.profile_snapshot = None

    assert library.build_from_run(trace) is None
    assert library.list() == []


def test_build_from_run_persists_replay_eligible_trace(tmp_path: Path, cases_root: Path):
    library = make_library(tmp_path, cases_root)
    trace = make_eligible_trace()

    record = library.build_from_run(trace)

    assert record is not None
    assert record.version == 1
    assert record.source_kind == "live_run"
    assert record.source_id == trace.run_id
    assert record.bundle_name == "com.example.notes"
    assert record.spec.scenario == ScenarioKind.CORE_FLOW
    assert record.spec.case_id.startswith("case-20240101T000000Z-")
    assert record.created_at == trace.started_at
    # 同一 run 重复构建：case_id 稳定 + provenance 时间钉死 ⇒ 命中版本去重。
    again = library.build_from_run(trace)
    assert again is not None
    assert again.version == 1
    assert len(library.versions(record.case_id)) == 1


def test_build_from_run_returns_none_when_not_replay_eligible(tmp_path: Path, cases_root: Path):
    library = make_library(tmp_path, cases_root)
    trace = make_eligible_trace()
    # 没有显式断言 ⇒ builder 注入兜底断言并给出 incomplete_reasons ⇒ 不合格。
    trace.actions = [action for action in trace.actions if action.tool != ToolName.ASSERT_VISIBLE]
    trace.snapshots = []

    assert library.build_from_run(trace) is None
    assert library.list() == []


def test_scenario_for_trace_follows_plan_c1_mapping():
    # 显式 trace.scenario 优先；provisional/live-mode → EXPLORATORY；bootstrap → SMOKE；其余 CORE_FLOW。
    assert scenario_for_trace(make_eligible_trace(scenario=ScenarioKind.EXPLORATORY)) == ScenarioKind.EXPLORATORY
    assert scenario_for_trace(make_eligible_trace(provisional=True)) == ScenarioKind.EXPLORATORY
    assert scenario_for_trace(make_eligible_trace(phase="bootstrap")) == ScenarioKind.SMOKE
    assert scenario_for_trace(make_eligible_trace()) == ScenarioKind.CORE_FLOW


def test_build_from_run_persists_mapped_scenario(tmp_path: Path, cases_root: Path):
    library = make_library(tmp_path, cases_root)

    def scenario_of(trace: RunTrace) -> ScenarioKind:
        record = library.build_from_run(trace)
        assert record is not None
        return record.spec.scenario

    assert scenario_of(make_eligible_trace(run_id="run-live-mode", live_mode=True)) == ScenarioKind.EXPLORATORY
    assert scenario_of(make_eligible_trace(run_id="run-bootstrap", phase="bootstrap")) == ScenarioKind.SMOKE
    assert scenario_of(make_eligible_trace(run_id="run-task")) == ScenarioKind.CORE_FLOW


def test_build_from_dc_requires_real_identity(tmp_path: Path, cases_root: Path):
    library = make_library(tmp_path, cases_root)
    snapshot = make_dc_snapshot()

    # 注意：``EntryAbility`` 本身就是 builder 的**占位 ability**，合格用例必须给出真实全名。
    record = library.build_from_dc(snapshot, "com.example.notes", "com.example.notes.MainAbility")
    assert record is not None
    assert record.source_kind == "dc_session"
    assert record.source_id == snapshot.session_id
    assert record.spec.main_ability == "com.example.notes.MainAbility"

    # 占位身份（com.example.app / EntryAbility）⇒ 不可回放 ⇒ 不入库。
    assert library.build_from_dc(make_dc_snapshot(session_id="dc-other"), PLACEHOLDER_BUNDLE, "EntryAbility") is None
    assert library.build_from_dc(make_dc_snapshot(session_id="dc-third"), "com.example.notes", "EntryAbility") is None


# ---------------------------------------------------------------------------
# execute：双引擎、注入与执行记录
# ---------------------------------------------------------------------------


def test_execute_standalone_success_records_passed_and_evidence(tmp_path: Path, cases_root: Path):
    calls: list[dict[str, Any]] = []
    seen: list[tuple[Path, float]] = []
    analysis = ExecutionAnalysis(subject="case_execution", subject_id="exec-1", bundle_name="com.example.notes")
    analyzer = RecordingAnalyzer(analysis=analysis)
    library = make_library(
        tmp_path,
        cases_root,
        runner_factory=hypium_factory(make_replay(passed=True), calls, seen),
        analyzer=analyzer,
    )
    library.save_built(make_built())
    version_dir = library.artifact_dir(CASE_ID, 1)

    execution = library.execute(CASE_ID, params={"theme": "dark"}, device_sn="SN-1")

    assert execution.passed is True
    assert execution.status == "passed"
    assert execution.version == 1
    assert execution.engine == "hypium_standalone"
    assert execution.device_id == "SN-1"
    assert execution.execution_id.startswith("exec-")
    assert execution.ended_at is not None
    assert execution.error is None
    # 分析是附加信息，确实是注入分析器的返回值。
    assert execution.analysis == analysis
    assert len(analyzer.calls) == 1

    # runner 拿到的是版本目录内的独立脚本，以及 extra_env 双通道注入。
    assert calls[0]["python_path"] == version_dir / STANDALONE_DIRNAME / f"test_{safe_script_id(CASE_ID)}.py"
    assert calls[0]["attempt"] == 1
    assert calls[0]["extra_env"] == {
        DEVICE_SN_ENV: "SN-1",
        PARAMS_ENV: json.dumps({"theme": "dark"}),
    }
    # 超时取 spec.timeout_seconds，runtime_home 落在版本目录内（安全边界之内）。
    assert seen == [(version_dir, 300.0)]

    assert execution.result is not None
    assert execution.result["status"] == "passed"
    assert execution.result["generated_result"] is None

    # 终态记录落库（pending → 终态是同一行的 upsert）。
    stored = library.get_execution(execution.execution_id)
    assert stored is not None
    assert stored.passed is True
    assert [item.execution_id for item in library.list_executions(CASE_ID)] == [execution.execution_id]

    evidence_dir = version_dir / EXECUTIONS_DIRNAME / execution.execution_id
    assert json.loads((evidence_dir / "result.json").read_text(encoding="utf-8"))["status"] == "passed"
    assert (evidence_dir / "analysis.json").is_file()


def test_execute_standalone_failure_records_passed_false(tmp_path: Path, cases_root: Path):
    calls: list[dict[str, Any]] = []
    error = ReplayError(kind="script_error", message="断言失败：登录按钮不可见")
    library = make_library(
        tmp_path, cases_root, runner_factory=hypium_factory(make_replay(passed=False, error=error), calls)
    )
    library.save_built(make_built())

    execution = library.execute(CASE_ID)

    assert execution.passed is False
    assert execution.status == "failed"
    assert execution.error == "script_error: 断言失败：登录按钮不可见"
    assert execution.report_path is None
    stored = library.get_execution(execution.execution_id)
    assert stored is not None
    assert stored.passed is False
    assert stored.status == "failed"


def test_raising_analyzer_never_flips_passed(tmp_path: Path, cases_root: Path):
    calls: list[dict[str, Any]] = []
    analyzer = RecordingAnalyzer(error=RuntimeError("hilog unavailable"))
    library = make_library(
        tmp_path,
        cases_root,
        runner_factory=hypium_factory(make_replay(passed=True), calls),
        analyzer=analyzer,
    )
    library.save_built(make_built())

    execution = library.execute(CASE_ID)

    # 分析抛错既不上抛，也不把 passed 翻成 False。
    assert execution.passed is True
    assert execution.status == "passed"
    assert execution.analysis is None
    assert len(analyzer.calls) == 1


def test_execute_uses_caller_supplied_execution_id_and_updates_pending_row(tmp_path: Path, cases_root: Path):
    calls: list[dict[str, Any]] = []
    observed: list[str | None] = []
    library = make_library(
        tmp_path,
        cases_root,
        runner_factory=hypium_factory(
            make_replay(passed=True),
            calls,
            on_execute=lambda: observed.append(getattr(library.get_execution("exec-api-1"), "status", None)),
        ),
    )
    library.save_built(make_built())
    # HTTP 层先建 pending 行并返回 202，用例库重跑必须更新同一行。
    library.repository.record_execution(
        CaseExecutionRecord(
            execution_id="exec-api-1",
            case_id=CASE_ID,
            version=1,
            engine="hypium_standalone",
            device_id="SN-API",
            status="pending",
        )
    )

    execution = library.execute(CASE_ID, device_sn="SN-API", execution_id="exec-api-1")

    assert execution.execution_id == "exec-api-1"
    assert observed == ["pending"]  # runner 运行时 pending 行已经可见
    assert [item.execution_id for item in library.list_executions(CASE_ID)] == ["exec-api-1"]
    stored = library.get_execution("exec-api-1")
    assert stored is not None
    assert stored.status == "passed"
    assert stored.passed is True


def test_execute_without_runner_factory_raises_runtime_error(tmp_path: Path, cases_root: Path):
    library = make_library(tmp_path, cases_root)
    library.save_built(make_built())

    with pytest.raises(RuntimeError, match="runner_factory"):
        library.execute(CASE_ID)


def test_execute_xdevice_engine_builds_project_and_injects_params(tmp_path: Path, cases_root: Path):
    calls: list[dict[str, Any]] = []
    result = SimpleNamespace(
        attempt=1,
        status="passed",
        passed=True,
        command=CommandResult(command="python -m xdevice run", returncode=0),
        report_dir=None,
        summary_report_path="reports/20240101/report/summary_report.html",
        evidence_paths=["attempt-01/stdout.log"],
        error=None,
    )

    def factory(project_root: Path, timeout: float) -> FakeXDeviceRunner:
        return FakeXDeviceRunner(result, calls)

    library = make_library(tmp_path, cases_root, xdevice_runner_factory=factory)
    library.save_built(make_built())

    execution = library.execute(CASE_ID, engine=ENGINE_XDEVICE, device_sn="SN-2", params={"rounds": 3})

    assert execution.engine == ENGINE_XDEVICE
    assert execution.passed is True
    assert execution.status == "passed"
    project = calls[0]["project"]
    assert project.suite == "HarmonyAgentCases"
    assert project.class_name == "HarmonyAgentCase"
    assert project.device_sn == "SN-2"
    assert project.timeout_seconds == 300
    assert calls[0]["params"] == {"rounds": 3}
    assert calls[0]["extra_env"] == {DEVICE_SN_ENV: "SN-2", PARAMS_ENV: json.dumps({"rounds": 3})}
    # 执行前用库中 spec 幂等重建工程。
    assert (library.artifact_dir(CASE_ID, 1) / XDEVICE_DIRNAME / "testlist.txt").is_file()
    assert execution.result is not None
    assert execution.result["summary_report_path"] == "reports/20240101/report/summary_report.html"


def test_execute_xdevice_engine_without_factory_raises_runtime_error(tmp_path: Path, cases_root: Path):
    library = make_library(tmp_path, cases_root)
    library.save_built(make_built())

    with pytest.raises(RuntimeError, match="xdevice_runner_factory"):
        library.execute(CASE_ID, engine=ENGINE_XDEVICE)


def test_execute_rejects_unknown_case_and_engine(tmp_path: Path, cases_root: Path):
    library = make_library(tmp_path, cases_root, runner_factory=hypium_factory(make_replay(passed=True), []))
    library.save_built(make_built())

    with pytest.raises(KeyError):
        library.execute("case-20240101T000000Z-ffffff")
    with pytest.raises(ValueError, match="unknown case engine"):
        library.execute(CASE_ID, engine="appium")


# ---------------------------------------------------------------------------
# cases_root 安全边界
# ---------------------------------------------------------------------------


def test_execute_rejects_artifact_dir_outside_cases_root(tmp_path: Path, cases_root: Path):
    library = make_library(tmp_path, cases_root, runner_factory=hypium_factory(make_replay(passed=True), []))
    escaped = tmp_path / "elsewhere"
    library.repository.save(make_spec(), artifact_dir=escaped)

    with pytest.raises(ValueError, match="越出用例库根目录"):
        library.execute(CASE_ID)


def test_artifact_dir_rejects_path_traversal(tmp_path: Path, cases_root: Path):
    library = make_library(tmp_path, cases_root)

    with pytest.raises(ValueError):
        library.artifact_dir("../../evil", 1)
    with pytest.raises(ValueError):
        library.artifact_dir(CASE_ID, 0)


def test_execute_rejects_unsafe_execution_id(tmp_path: Path, cases_root: Path):
    library = make_library(tmp_path, cases_root, runner_factory=hypium_factory(make_replay(passed=True), []))
    library.save_built(make_built())

    with pytest.raises(ValueError, match="unsafe execution_id"):
        library.execute(CASE_ID, execution_id="../../escape")


# ---------------------------------------------------------------------------
# 查询与补丁委托
# ---------------------------------------------------------------------------


def test_get_list_versions_and_patch_delegate(tmp_path: Path, cases_root: Path):
    library = make_library(tmp_path, cases_root)
    library.save_built(make_built())
    other_id = "case-20240101T000000Z-def456"
    library.save_built(make_built(make_spec(case_id=other_id, title_zh="退出登录")))

    assert library.get(CASE_ID).version == 1  # type: ignore[union-attr]
    assert library.get(CASE_ID, 99) is None
    assert library.get("case-20240101T000000Z-ffffff") is None
    assert [item.case_id for item in library.list()] == [other_id, CASE_ID]
    assert [item.case_id for item in library.list(target_app_id=TARGET_APP, limit=1)] == [other_id]
    assert library.list(status="archived") == []
    assert [item.version for item in library.versions(CASE_ID)] == [1]

    patched = library.patch(CASE_ID, slug="login-flow-v2", tags=["smoke", "regression"])

    assert patched is not None
    assert patched.version == 2
    assert patched.slug == "login-flow-v2"
    assert patched.tags == ["smoke", "regression"]
    assert patched.status == "active"
    assert [item.version for item in library.versions(CASE_ID)] == [2, 1]
    assert [item.case_id for item in library.list(tag="regression")] == [CASE_ID]

    assert library.patch("case-20240101T000000Z-ffffff", tags=["x"]) is None
