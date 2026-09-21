"""可复用用例库（改进计划 B1/B2）：IR 落盘、版本管理与双引擎重跑。

职责边界：

* **落盘**：把一个 :class:`~harmony_test_agent.cases.builder.CaseBuildResult` 的 IR 与
  两套引擎产物写进 ``<cases_root>/<case_id>/v<N>/``；``cases_root`` 就是
  ``Settings.resolved_cases_dir``（默认 ``artifacts/cases``），因此最终布局为
  ``artifacts/cases/<case_id>/v<N>/``：

  ```
  v<N>/
    case_spec.json
    safety_violations.json          # 仅当静态安全门禁报出违规时存在
    standalone/test_<safe>.py       # 独立 UiDriver 脚本
    standalone/test_<safe>.json     # 同 schema 配置 + 构建审计信息
    xdevice/                        # 官方 devicetest 工程（emitter 可用时）
    hypium/attempt-XX/…             # HypiumRunner 产出（run_dir = python_path.parent.parent）
    executions/<execution_id>/…     # 重跑证据快照（result.json / analysis.json）
  ```

  ``standalone/`` 这层嵌套是**有意的**：``HypiumRunner._execute`` 取
  ``run_dir = python_path.parent.parent``，嵌套后证据正好落在版本目录内，runner 不用改。

* **版本**：版本行由 :class:`~harmony_test_agent.storage.case_repository.CaseRepository`
  独占持有，本模块**不复制**它的去重语义：落盘前只做一次「与最新版本内容是否一致」的
  预判，用来把产物写进正确的 ``v<N>`` 目录；``repository.save`` 仍是版本号的最终权威
  （返回值与预判不一致时以仓库为准）。

* **重跑**：``runner/hypium.py``、``runner/xdevice.py``、``analysis/service.py`` 只在方法
  内部延迟 import，两个 runner 都通过构造参数注入——单测因此永不接触设备。

* **安全门禁**：``save_built`` 调 ``validate_case_spec``；违规**不硬失败**，而是把用例
  降级为 ``status="draft"``，并同时用两种方式留痕：
  1. ``spec.tags`` 追加 ``safety-violation`` 标签（可被 ``CaseSummary.tags`` 与
     ``list(tag="safety-violation")`` 直接发现/过滤）；
  2. 版本目录写 ``safety_violations.json``（完整中文违规文案，逐条可审计）。
"""

from __future__ import annotations

import inspect
import json
import re
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ..cases.builder import CaseBuilder, CaseBuildResult, slugify
from ..cases.safety import validate_case_spec
from ..cases.spec import CaseExecutionRecord, CaseRecord, CaseSummary, TestCaseSpec
from ..generation.standalone import StandaloneEmitter
from ..models import ExecutionAnalysis, GeneratedArtifact, ReplayResult, ScenarioKind, utc_now
from ..storage.case_repository import CaseRepository

if TYPE_CHECKING:  # 仅为类型标注：运行期绝不 import 这两个仍在落地中的模块
    from ..analysis.service import ExecutionAnalyzer
    from ..runner.hypium import HypiumRunner
    from ..runner.xdevice import XDeviceRunner

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

CASE_SPEC_FILENAME = "case_spec.json"
"""版本目录里的 IR 文件名（与 ``generation/hypium.py`` 的 ``CASE_SPEC_FILENAME`` 同名同义）。"""

SAFETY_VIOLATIONS_FILENAME = "safety_violations.json"
"""静态安全门禁违规明细（仅在违规时写出）。"""

SAFETY_VIOLATION_TAG = "safety-violation"
"""违规用例的标签；与 ``safety_violations.json`` 配对，前者可过滤、后者可细读。"""

STANDALONE_DIRNAME = "standalone"
XDEVICE_DIRNAME = "xdevice"
HYPIUM_DIRNAME = "hypium"
EXECUTIONS_DIRNAME = "executions"

ENGINE_HYPIUM = "hypium_standalone"
ENGINE_XDEVICE = "xdevice_devicetest"

PARAMS_ENV = "HARMONY_AGENT_CASE_PARAMS"
DEVICE_SN_ENV = "HARMONY_AGENT_DEVICE_SN"

DEFAULT_MAX_ITERATIONS = 5000
"""读不到 ``Settings.stress_max_iterations`` 时的兜底上限（计划 C2 的硬上限）。"""

STANDALONE_CONFIG_SCHEMA_VERSION = 2
"""``standalone/test_<safe>.json`` 的 schema 版本，与历史脚本配置保持一致。"""

ExecutionStatus = Literal["pending", "passed", "failed", "timed_out", "error", "invalid_result"]

STANDALONE_STATUS_MAP: dict[str, ExecutionStatus] = {
    # ``ReplayResult.status`` → ``CaseExecutionRecord.status``。
    # ``ineligible`` 只可能出现在走验收门禁的 ``HypiumRunner.execute`` 上；用例库重跑走
    # ``execute_diagnostic``，理论上永不出现，这里仍显式映射为 ``error`` 而不是静默忽略。
    "passed": "passed",
    "failed": "failed",
    "timed_out": "timed_out",
    "invalid_result": "invalid_result",
    "ineligible": "error",
}

XDEVICE_STATUS_MAP: dict[str, ExecutionStatus] = {
    # ``XDeviceRunResult.status`` 的词汇表与 ``CaseExecutionRecord`` 完全一致。
    "passed": "passed",
    "failed": "failed",
    "timed_out": "timed_out",
    "error": "error",
    "invalid_result": "invalid_result",
}

_SAFE_SCRIPT_RE = re.compile(r"[^a-zA-Z0-9_]")
_EXECUTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SLUG_FALLBACK_RE = re.compile(r"case-[0-9a-f]{6}")
"""``CaseBuilder.slugify`` 对纯中文标题的随机兜底形态 ``case-<uuid4 hex6>``。"""


# ---------------------------------------------------------------------------
# 模块级工具
# ---------------------------------------------------------------------------


def safe_script_id(value: str) -> str:
    """把用例 ID 压成可安全用作文件名的片段（镜像 ``generation/hypium.py::_safe_id``）。

    该函数私有实现位于生成器内部，这里保留一份等价实现并公开导出，避免测试与调用方
    各自复制正则：``case-20240101T000000Z-abc123`` → ``case_20240101T000000Z_abc123``。
    """
    return _SAFE_SCRIPT_RE.sub("_", value)


def case_id_for_source(source_kind: str, source_id: str, created_at: datetime) -> str:
    """由**来源身份**推导稳定的 ``case_id``。

    计划 B2 的字面写法是 ``case-<utc_now>-<uuid4 hex6>``；这里把 6 位十六进制换成
    ``uuid5(NAMESPACE_URL, "<source_kind>:<source_id>")`` 的前 6 位，因为「同一来源重复
    generate 不灌水版本号」只有 ``case_id`` 稳定时才成立：``uuid4`` 会让每次
    ``POST /api/cases/from-run/{run_id}`` 都长出一个新用例。格式仍满足
    ``TestCaseSpec.case_id`` 的正则（``case-<时间戳>-<6 位小写十六进制>``）。
    """
    digest = uuid.uuid5(uuid.NAMESPACE_URL, f"harmony-test-agent:{source_kind}:{source_id}").hex[:6]
    return f"case-{created_at:%Y%m%dT%H%M%SZ}-{digest}"


def _stable_case_slug(title_zh: str, case_id: str) -> str:
    """给用例一个**确定性** slug。

    ``CaseBuilder.slugify`` 在标题里没有任何 ASCII 字母数字（例如纯中文任务名）时会退化
    成 ``case-<uuid4 hex6>``；这会让同一来源的重复构建产出不同 spec、版本号灌水。这里只在
    命中该随机兜底形态时改用 ``case_id`` 尾部的稳定哈希。
    """
    slug = slugify(title_zh)
    if _SLUG_FALLBACK_RE.fullmatch(slug):
        return f"case-{case_id.rsplit('-', 1)[-1]}"
    return slug


def scenario_for_trace(trace: Any) -> ScenarioKind:
    """按计划 C1 的映射把 ``RunTrace`` 归到用例场景。

    优先级：显式的 ``trace.scenario``（``POST /api/runs`` 已带场景时原样透传）→
    ``provisional`` / ``live_mode`` 的探索型运行 → ``EXPLORATORY`` →
    ``phase="bootstrap"``（Profile 引导/校验运行）→ ``SMOKE`` → 其余 ``CORE_FLOW``。

    公开导出：``POST /api/cases/from-run/{run_id}?force=true`` 这类「绕过
    ``replay_eligible`` 门槛」的构建路径可以直接复用它，保证强制入库的用例场景一致。
    注意 provisional 轨迹永远不会通过 ``build_from_run`` 的合格门槛，因此该分支只在这条
    强制路径上生效。
    """
    declared = getattr(trace, "scenario", None)
    if declared is not None:
        return ScenarioKind(declared)
    if bool(getattr(trace, "provisional", False)) or bool(getattr(trace, "live_mode", False)):
        return ScenarioKind.EXPLORATORY
    if str(getattr(trace, "phase", "") or "") == "bootstrap":
        return ScenarioKind.SMOKE
    return ScenarioKind.CORE_FLOW


def _new_execution_id() -> str:
    """生成 ``exec-<UTC 时间戳>-<6 位十六进制>`` 形式的执行 ID（对齐 ``run-``/``dc-`` 习惯）。"""
    return f"exec-{utc_now():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def _canonical_spec_json(spec: TestCaseSpec) -> str:
    """把 spec 序列化为稳定形式，用于「与最新版本是否一致」的预判。

    与 ``CaseRepository._canonical_json`` 的比较口径一致（排序键 + 紧凑分隔符）；
    这只是**预判**，真正的去重仍由 ``repository.save`` 裁决。
    """
    payload = json.loads(spec.model_dump_json())
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _invoke_with_supported_keywords(
    func: Callable[..., Any],
    *args: Any,
    candidates: dict[str, Any],
    **kwargs: Any,
) -> Any:
    """只把 ``func`` 签名里确实存在的命名参数传进去。

    ``HypiumRunner.environment()/execute_diagnostic()`` 的 ``extra_env`` 参数仍在落地
    （计划 B2「重跑」一节写明要新增），因此这里用 ``inspect`` 做**防御式注入**：签名没有
    该参数时静默跳过，绝不因为前进中的 runner 变更而硬失败。``**kwargs`` 不算「支持」——
    避免把参数喂给身份不明的透传层。
    """
    try:
        parameters = inspect.signature(func).parameters
    except TypeError, ValueError:  # 内建 / 不可内省的调用对象：退回不带可选参数调用
        parameters = {}
    for name, value in candidates.items():
        if name in parameters:
            kwargs[name] = value
    return func(*args, **kwargs)


def _execution_extra_env(device: str, params: dict | None) -> dict[str, str]:
    """构造注入 runner 的额外环境变量（设备号与用例参数双通道）。"""
    extra: dict[str, str] = {}
    if device:
        extra[DEVICE_SN_ENV] = str(device)
    if params:
        # 生成的脚本自己会读 HARMONY_AGENT_CASE_PARAMS；params 为空时不写，避免覆盖默认值。
        extra[PARAMS_ENV] = json.dumps(params, ensure_ascii=False)
    return extra


def _requested_params_text(params: dict | None) -> dict[str, Any]:
    """把 ``params`` 归一化为落库用的普通 dict。"""
    return dict(params) if params else {}


@dataclass
class _RunOutcome:
    """一次重跑归一化后的结论（由两个引擎分支共同产出）。"""

    status: ExecutionStatus
    passed: bool
    report_path: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    analysis: ExecutionAnalysis | None = None


def _replay_error_text(error: Any) -> str | None:
    """把 ``ReplayError`` 压成一行可读文本（缺字段时退化为 ``str``）。"""
    if error is None:
        return None
    kind = getattr(error, "kind", None)
    message = getattr(error, "message", None)
    if kind and message:
        return f"{kind}: {message}"
    return str(error)


def _replay_result_payload(replay: ReplayResult) -> dict[str, Any]:
    """回放结果的落库快照（``generated_result.json`` 原样保留在 ``generated_result`` 键下）。"""
    return {
        "attempt": replay.attempt,
        "status": str(replay.status),
        "exit_code": replay.exit_code,
        "timed_out": replay.timed_out,
        "evidence_paths": list(replay.evidence_paths),
        "generated_result_path": replay.generated_result_path,
        "generated_result": replay.generated_result,
    }


def _xdevice_result_payload(result: Any) -> dict[str, Any]:
    """xdevice 结果的落库快照；只按鸭子类型取属性，绝不 import ``runner/xdevice.py``。"""
    command = getattr(result, "command", None)
    return {
        "attempt": getattr(result, "attempt", None),
        "status": str(getattr(result, "status", "") or ""),
        "exit_code": getattr(command, "returncode", None),
        "timed_out": bool(getattr(command, "timed_out", False)),
        "evidence_paths": list(getattr(result, "evidence_paths", None) or []),
        "summary_report_path": getattr(result, "summary_report_path", None),
    }


# ---------------------------------------------------------------------------
# 用例库
# ---------------------------------------------------------------------------


class CaseLibrary:
    """可复用用例库门面：落盘、版本查询与双引擎重跑。

    ``cases_root`` 是**安全边界**：``execute`` 涉及的所有路径都必须位于其内，越界一律拒绝。
    """

    def __init__(
        self,
        repository: CaseRepository,
        cases_root: Path,
        *,
        min_observed_rounds: int = 1,
        runner_factory: Callable[[Path, float], HypiumRunner] | None = None,
        xdevice_runner_factory: Callable[[Path, float], XDeviceRunner] | None = None,
        analyzer: ExecutionAnalyzer | None = None,
    ) -> None:
        self.repository = repository
        self.cases_root = Path(cases_root).resolve()
        self.min_observed_rounds = max(int(min_observed_rounds), 1)
        # 协作者全部注入：``None`` 表示该引擎不可用（``execute`` 会抛清晰的 RuntimeError，
        # 绝不静默跑一个假的「成功」）。
        self.runner_factory = runner_factory
        self.xdevice_runner_factory = xdevice_runner_factory
        self.analyzer = analyzer

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------

    def artifact_dir(self, case_id: str, version: int) -> Path:
        """返回 ``<cases_root>/<case_id>/v<version>``（不创建目录）。"""
        if not case_id:
            raise ValueError("case_id must not be empty")
        if int(version) < 1:
            raise ValueError(f"version must be >= 1, got {version}")
        return self._require_inside(self.cases_root / case_id / f"v{int(version)}", "用例产物目录")

    def _require_inside(self, path: Path | str, label: str) -> Path:
        """把路径解析为绝对路径，并要求它落在 ``cases_root`` 内（安全边界断言）。"""
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.cases_root):
            raise ValueError(f"{label}越出用例库根目录：{resolved} 不在 {self.cases_root} 内")
        return resolved

    # ------------------------------------------------------------------
    # 保存
    # ------------------------------------------------------------------

    def save_built(self, built: CaseBuildResult, *, device_sn: str | None = None) -> CaseRecord:
        """落盘一个构建结果并返回用例记录。

        流程：深度复制 spec（不改调用方对象）→ 覆盖 ``device_sn`` → 静态安全门禁 →
        预判版本号 → 写产物 → ``repository.save``（版本号以它为准）。

        安全门禁违规**不硬失败**：spec 降级为 ``draft``，``safety-violation`` 标签与
        ``safety_violations.json`` 双留痕（见模块 docstring）。
        """
        spec = built.spec.model_copy(deep=True)
        if device_sn is not None:
            spec.device_sn = device_sn
        violations = self._validate(spec)
        if violations:
            spec.status = "draft"
            if SAFETY_VIOLATION_TAG not in spec.tags:
                spec.tags.append(SAFETY_VIOLATION_TAG)

        planned = self._plan_version(spec)
        directory = self.artifact_dir(spec.case_id, planned)
        created = not directory.exists()
        directory.mkdir(parents=True, exist_ok=True)
        self._write_artifacts(directory, spec, built, version=planned, violations=violations)

        saved = self.repository.save(spec, artifact_dir=directory, status=spec.status)
        if saved != planned and created:
            # 去重回落到既有版本（内容与旧行一致）时，丢弃本次多写的目录，避免留垃圾。
            shutil.rmtree(directory, ignore_errors=True)
        record = self.get(spec.case_id, saved)
        if record is None:  # pragma: no cover - 刚写入的行不可能读不到
            raise RuntimeError(f"case {spec.case_id!r} v{saved} vanished right after save")
        return record

    def _validate(self, spec: TestCaseSpec) -> list[str]:
        """跑静态安全门禁；``max_iterations`` 取 Settings，读不到时退 5000。"""
        return validate_case_spec(spec, max_iterations=self._max_iterations())

    @staticmethod
    def _max_iterations() -> int:
        """``Settings.stress_max_iterations``；配置不可用时退化成 5000。"""
        try:
            from ..config import get_settings

            return max(int(get_settings().stress_max_iterations), 1)
        except Exception:  # 配置缺失 / .env 损坏都不该让用例保存失败
            return DEFAULT_MAX_ITERATIONS

    def _plan_version(self, spec: TestCaseSpec) -> int:
        """预判将要写入的版本号：与最新版本内容一致则复用，否则追加。"""
        latest = self.repository.latest(spec.case_id)
        if latest is None:
            return 1
        latest_spec, latest_version, _status, _artifact_dir = latest
        if _canonical_spec_json(latest_spec) == _canonical_spec_json(spec):
            return latest_version
        return latest_version + 1

    def _write_artifacts(
        self,
        directory: Path,
        spec: TestCaseSpec,
        built: CaseBuildResult,
        *,
        version: int,
        violations: list[str],
    ) -> None:
        """写出 ``case_spec.json`` / ``standalone/`` / ``xdevice/``（以及违规明细）。"""
        self._write_standalone(directory, spec, built, version=version, violations=violations)
        (directory / CASE_SPEC_FILENAME).write_text(
            json.dumps(spec.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        violations_path = directory / SAFETY_VIOLATIONS_FILENAME
        if violations:
            violations_path.write_text(
                json.dumps({"case_id": spec.case_id, "violations": list(violations)}, ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )
        elif violations_path.exists():
            # 同一版本被重写且已无违规（例如 spec 被修正后重新保存）：清掉陈旧证据。
            violations_path.unlink()
        self._write_xdevice_project(directory, spec, version=version)

    def _write_standalone(
        self,
        directory: Path,
        spec: TestCaseSpec,
        built: CaseBuildResult,
        *,
        version: int,
        violations: list[str],
    ) -> None:
        """写 ``standalone/test_<safe>.py`` 与同名 ``.json`` 配置。"""
        standalone_dir = directory / STANDALONE_DIRNAME
        standalone_dir.mkdir(parents=True, exist_ok=True)
        safe_id = safe_script_id(spec.case_id)
        rendered = StandaloneEmitter().render(
            spec,
            run_id=spec.provenance.source_id,
            device_id=spec.device_sn or "",
        )
        (standalone_dir / f"test_{safe_id}.py").write_text(rendered.python_text, encoding="utf-8")
        config: dict[str, Any] = {
            "schema_version": STANDALONE_CONFIG_SCHEMA_VERSION,
            "runner_mode": "driver",
            "case_version": version,
            "purpose": built.purpose,
            "replay_eligible": built.replay_eligible,
            "source_agent_outcome": built.source_agent_outcome,
            "incomplete_reasons": list(built.incomplete_reasons),
            "warnings": list(built.warnings),
            "counts": dict(built.counts),
            "omitted_actions": list(built.omitted_actions),
            "safety_violations": list(violations),
            # 与历史脚本配置同义的证据目录：HypiumRunner 会写 <vN>/hypium/attempt-XX/。
            "report_dir": str((directory / HYPIUM_DIRNAME).resolve()),
            "standalone_python": f"test_{safe_id}.py",
        }
        config.update(rendered.config)
        (standalone_dir / f"test_{safe_id}.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _write_xdevice_project(self, directory: Path, spec: TestCaseSpec, *, version: int) -> None:
        """写出官方 devicetest 工程；emitter 不可用（或渲染不出）时跳过这一附加产物。

        xdevice 工程是**附加**产物：standalone 脚本与 IR 才是权威可执行面，因此 emitter
        尚未落地或 IR 里有 xdevice 表达不了的动作时，用例照常入库（``execute`` 走
        ``xdevice_devicetest`` 时会用同一个 emitter 按库中 spec 幂等重建工程）。
        """
        try:
            from ..generation.xdevice_case import XDeviceEmitter
        except ImportError:  # pragma: no cover - 只有 emitter 尚未落地才会命中
            return
        target = directory / XDEVICE_DIRNAME
        existed = target.exists()
        try:
            XDeviceEmitter().render(spec, target, version=version)
        except ValueError:
            if not existed:
                shutil.rmtree(target, ignore_errors=True)
            return

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------

    def build_from_run(self, trace: Any) -> CaseRecord | None:
        """从 Live 轨迹构建并保存用例；不合格返回 ``None``。

        规则（与计划 B2 一致）：

        * ``trace.profile_snapshot`` 为空 ⇒ ``None``（没有冻结 Profile 就没有可复现的应用身份）；
        * ``CaseBuilder.from_trace`` 判定 ``replay_eligible=False`` ⇒ ``None``（诊断态不入库）；
        * ``case_id`` 由 ``run_id`` 稳定推导，因此同一 run 重复构建会命中版本去重。
        """
        profile = getattr(trace, "profile_snapshot", None)
        if profile is None:
            return None
        run_id = str(getattr(trace, "run_id", ""))
        source_time = self._source_time(trace)
        case_id = case_id_for_source("live_run", run_id, source_time)
        built = CaseBuilder(min_observed_rounds=self.min_observed_rounds).from_trace(
            trace,
            profile,
            case_id=case_id,
            scenario=scenario_for_trace(trace),
        )
        if not built.replay_eligible:
            return None
        return self.save_built(self._pin_source_identity(built, case_id=case_id, source_time=source_time))

    def build_from_dc(self, snapshot: Any, bundle_name: str, main_ability: str) -> CaseRecord | None:
        """从 DC 会话快照构建并保存用例；不合格返回 ``None``。

        规则：``CaseBuilder.from_dc_invocations`` 判定 ``replay_eligible=False`` ⇒ ``None``
        （没有显式断言、没有可回放操作，或身份仍是占位 ``com.example.app``）。
        ``snapshots`` 显式传 ``None``：DC 会话快照不携带屏幕分辨率，坐标兜底会带
        ``unknown resolution bound`` 警告（宁可用坐标也不静默丢步骤）。
        """
        session_id = str(getattr(snapshot, "session_id", ""))
        invocations = list(getattr(snapshot, "invocations", None) or [])
        source_time = self._source_time(snapshot)
        case_id = case_id_for_source("dc_session", session_id, source_time)
        built = CaseBuilder(min_observed_rounds=self.min_observed_rounds).from_dc_invocations(
            session_id,
            str(getattr(snapshot, "device_id", "") or ""),
            invocations,
            bundle_name=bundle_name,
            main_ability=main_ability,
            snapshots=None,
            case_id=case_id,
        )
        if not built.replay_eligible:
            return None
        return self.save_built(self._pin_source_identity(built, case_id=case_id, source_time=source_time))

    @staticmethod
    def _pin_source_identity(built: CaseBuildResult, *, case_id: str, source_time: datetime) -> CaseBuildResult:
        """把「来源身份」钉进 spec 后返回同一个构建结果。

        构建结果里有两处**非确定**取值，会让同一来源的两次构建产出不同 ``spec_json``，
        计划 B2 的「同一来源重复 generate 跳过」就永不命中：

        1. ``provenance.created_at`` 默认取「当前时间」；
        2. ``CaseBuilder.slugify`` 对纯中文标题会退化成 ``case-<uuid4>``。

        钉成由来源推导的稳定值后，同一 run / 同一 DC 会话重复构建得到**字节级相同**的
        spec，既满足去重，也让 ``CaseRecord.created_at`` 表达「案例来自哪一刻的运行」。
        """
        built.spec.provenance.created_at = source_time
        built.spec.slug = _stable_case_slug(built.spec.title_zh, case_id)
        return built

    @staticmethod
    def _source_time(source: Any) -> datetime:
        """取来源的起始时间（``RunTrace.started_at`` / ``DcSessionSnapshot.created_at``）。"""
        for attribute in ("started_at", "created_at", "last_active_at"):
            value = getattr(source, attribute, None)
            if isinstance(value, datetime):
                return value
        return utc_now()

    # ------------------------------------------------------------------
    # 查询与补丁
    # ------------------------------------------------------------------

    def get(self, case_id: str, version: int | None = None) -> CaseRecord | None:
        """读取一个用例版本；``version`` 为空时取最新版本，不存在返回 ``None``。

        ``created_at`` 取 ``spec.provenance.created_at``（用例的来源时间），因为仓库的
        ``get`` 只回传 ``(spec, version, status, artifact_dir)``；``list/versions`` 的
        ``CaseSummary.created_at`` 则是落库行时间，两者语义不同、都可用。
        """
        row = self.repository.get(case_id, version)
        if row is None:
            return None
        spec, stored_version, status, artifact_dir = row
        return CaseRecord(
            case_id=spec.case_id,
            version=stored_version,
            slug=spec.slug,
            title_zh=spec.title_zh,
            scenario=spec.scenario,
            status=status,
            tags=list(spec.tags),
            target_app_id=spec.provenance.profile_target_app_id,
            bundle_name=spec.bundle_name,
            source_kind=spec.provenance.source_kind,
            source_id=spec.provenance.source_id,
            created_at=spec.provenance.created_at,
            artifact_dir=str(artifact_dir),
            spec=spec,
        )

    def list(self, **filters: Any) -> list[CaseSummary]:
        """委托仓库列表查询（``target_app_id`` / ``scenario`` / ``tag`` / ``status`` / ``limit``）。"""
        return self.repository.list(**filters)

    def versions(self, case_id: str) -> list[CaseSummary]:
        """返回某用例的全部版本摘要（新版本在前）。"""
        return self.repository.versions(case_id)

    def patch(
        self,
        case_id: str,
        *,
        slug: str | None = None,
        tags: list[str] | None = None,
        status: str | None = None,
    ) -> CaseRecord | None:
        """按需覆盖 slug/tags/status 并追加新版本；用例不存在返回 ``None``。

        用例版本行不可变：``patch`` 一定产生新版本行（``repository.patch`` 的语义），
        返回的是新版本对应的记录。
        """
        try:
            version = self.repository.patch(case_id, slug=slug, tags=tags, status=status)
        except KeyError:
            return None
        return self.get(case_id, version)

    # ------------------------------------------------------------------
    # 重跑
    # ------------------------------------------------------------------

    def execute(
        self,
        case_id: str,
        *,
        version: int | None = None,
        engine: str = ENGINE_HYPIUM,
        device_sn: str | None = None,
        params: dict | None = None,
        attempt: int = 1,
        execution_id: str | None = None,
    ) -> CaseExecutionRecord:
        """重跑一个已入库的用例，返回终态执行记录。

        * ``engine="hypium_standalone"``：构造 ``GeneratedArtifact`` 后调
          ``HypiumRunner.execute_diagnostic``——**故意绕过 run 级验收门禁**（用例库重跑不是
          Profile 晋级证据），但证据布局与 ``generated_result.json`` 解析完全一致。
        * ``engine="xdevice_devicetest"``：用 emitter 幂等重建工程 → ``build_project`` →
          ``XDeviceRunner.execute``。
        * 超时取 ``spec.timeout_seconds``；``device_sn`` / ``params`` 通过 ``extra_env``
          （``HARMONY_AGENT_DEVICE_SN`` / ``HARMONY_AGENT_CASE_PARAMS``）注入。
        * 先写 ``pending`` 行，再写终态行（同 ``execution_id``，仓库按键 upsert）；
          ``execution_id`` 可由调用方给定（HTTP 层先建 pending 行再返回 202）。
        * 注入的 ``analyzer`` 只作附加信息：分析异常被吞掉，``passed`` 永远是 runner 的判定。

        拒绝语义（都在写任何执行记录之前）：用例/版本不存在 ⇒ ``KeyError``；引擎名未知 ⇒
        ``ValueError``；产物目录越出 ``cases_root`` ⇒ ``ValueError``；所需 runner 工厂为
        ``None`` ⇒ ``RuntimeError``。
        """
        row = self.repository.get(case_id, version)
        if row is None:
            raise KeyError(case_id)
        spec, stored_version, _status, artifact_dir = row
        # 安全边界先行：越界产物目录不得进入任何执行路径（哪怕引擎未注入）。
        directory = self._require_inside(artifact_dir, "用例产物目录")
        if engine == ENGINE_HYPIUM:
            if self.runner_factory is None:
                raise RuntimeError(
                    "engine='hypium_standalone' 需要注入 runner_factory，当前 CaseLibrary.runner_factory 为 None"
                )
        elif engine == ENGINE_XDEVICE:
            if self.xdevice_runner_factory is None:
                raise RuntimeError("engine='xdevice_devicetest' 需要注入 xdevice_runner_factory，当前该工厂为 None")
        else:
            raise ValueError(f"unknown case engine: {engine!r}")
        if attempt < 1:
            raise ValueError(f"attempt must be >= 1, got {attempt}")
        if execution_id is not None and not _EXECUTION_ID_RE.match(execution_id):
            raise ValueError(f"unsafe execution_id: {execution_id!r}")

        device = device_sn or spec.device_sn or ""
        timeout = float(spec.timeout_seconds)
        extra_env = _execution_extra_env(device, params)
        execution = CaseExecutionRecord(
            execution_id=execution_id or _new_execution_id(),
            case_id=case_id,
            version=stored_version,
            engine=engine,  # type: ignore[arg-type]
            device_id=device,
            status="pending",
            passed=False,
            started_at=utc_now(),
        )
        self.repository.record_execution(execution)

        try:
            if engine == ENGINE_HYPIUM:
                outcome = self._run_standalone(
                    directory=directory,
                    spec=spec,
                    version=stored_version,
                    device=device,
                    params=params,
                    extra_env=extra_env,
                    attempt=attempt,
                    timeout=timeout,
                )
            else:
                outcome = self._run_xdevice(
                    directory=directory,
                    spec=spec,
                    version=stored_version,
                    device=device,
                    params=params,
                    extra_env=extra_env,
                    attempt=attempt,
                    timeout=timeout,
                )
        except Exception as exc:  # 重跑异常必须留下可轮询的终态记录，而不是 500
            outcome = _RunOutcome(status="error", passed=False, error=f"{type(exc).__name__}: {exc}")

        execution.status = outcome.status
        execution.passed = outcome.passed
        execution.ended_at = utc_now()
        execution.report_path = outcome.report_path
        execution.result = outcome.result
        execution.error = outcome.error
        execution.analysis = outcome.analysis
        self._write_execution_evidence(directory, execution)
        self.repository.record_execution(execution)
        return execution

    def _run_standalone(
        self,
        *,
        directory: Path,
        spec: TestCaseSpec,
        version: int,
        device: str,
        params: dict | None,
        extra_env: dict[str, str],
        attempt: int,
        timeout: float,
    ) -> _RunOutcome:
        """独立脚本分支：``<vN>/standalone/test_<safe>.py`` → ``execute_diagnostic``。"""
        python_path = self._require_inside(
            directory / STANDALONE_DIRNAME / f"test_{safe_script_id(spec.case_id)}.py",
            "独立脚本",
        )
        if not python_path.is_file():
            raise FileNotFoundError(f"case {spec.case_id!r} v{version} has no standalone script: {python_path}")
        # purpose / replay_eligible 只作审计说明：``execute_diagnostic`` 内部会重建一个
        # ``replay_eligible=False`` 的产物，从而**故意**绕过 run 级验收门禁。
        generated = GeneratedArtifact(
            python_path=python_path,
            config_path=python_path.with_suffix(".json"),
            metadata_path=python_path.with_suffix(".json"),
            purpose="acceptance" if spec.status == "active" else "diagnostic",
            replay_eligible=False,
            case_id=spec.case_id,
        )
        factory = self.runner_factory
        if factory is None:  # pragma: no cover - execute 已经拦过一次
            raise RuntimeError("engine='hypium_standalone' 需要注入 runner_factory")
        runner = factory(directory, timeout)
        replay = _invoke_with_supported_keywords(
            runner.execute_diagnostic,
            generated.python_path,
            candidates={"extra_env": extra_env},
            attempt=attempt,
        )
        return _RunOutcome(
            status=STANDALONE_STATUS_MAP.get(str(replay.status), "error"),
            # ``passed`` 原样沿用 runner 的判定；分析永不改写它。
            passed=bool(replay.passed),
            report_path=str(replay.report_path) if replay.report_path else None,
            result=_replay_result_payload(replay),
            error=_replay_error_text(replay.error),
            analysis=self._analyze_replay(replay, directory=directory, spec=spec, device=device),
        )

    def _run_xdevice(
        self,
        *,
        directory: Path,
        spec: TestCaseSpec,
        version: int,
        device: str,
        params: dict | None,
        extra_env: dict[str, str],
        attempt: int,
        timeout: float,
    ) -> _RunOutcome:
        """官方 xdevice 分支：重建工程 → ``build_project`` → ``XDeviceRunner.execute``。"""
        try:
            from ..generation.xdevice_case import XDeviceEmitter
            from ..runner.xdevice import build_project
        except ImportError as exc:  # pragma: no cover - 只有对应模块尚未落地才会命中
            raise RuntimeError("xdevice 引擎不可用：缺少 generation/xdevice_case.py 或 runner/xdevice.py") from exc
        factory = self.xdevice_runner_factory
        if factory is None:  # pragma: no cover - execute 已经拦过一次
            raise RuntimeError("engine='xdevice_devicetest' 需要注入 xdevice_runner_factory")
        project_dir = self._require_inside(directory / XDEVICE_DIRNAME, "xdevice 工程目录")
        # 用库中 spec 幂等重建工程：保证目录内容与版本行严格一致（也能修复缺失的工程目录）。
        artifact = XDeviceEmitter().render(spec, project_dir, version=version)
        project = build_project(artifact, device)
        runner = factory(project_dir, timeout)
        result = _invoke_with_supported_keywords(
            runner.execute,
            project,
            candidates={"params": _requested_params_text(params), "extra_env": extra_env},
            attempt=attempt,
        )
        return _RunOutcome(
            status=XDEVICE_STATUS_MAP.get(str(getattr(result, "status", "") or ""), "error"),
            passed=bool(getattr(result, "passed", False)),
            report_path=str(getattr(result, "report_dir", None) or project_dir),
            result=_xdevice_result_payload(result),
            error=_replay_error_text(getattr(result, "error", None)),
            analysis=self._analyze_xdevice(result, project_root=project_dir, spec=spec, device=device),
        )

    # ------------------------------------------------------------------
    # 分析（附加信息，永不翻转 passed）
    # ------------------------------------------------------------------

    def _analyze_replay(
        self,
        replay: ReplayResult,
        *,
        directory: Path,
        spec: TestCaseSpec,
        device: str,
    ) -> ExecutionAnalysis | None:
        """调 ``ExecutionAnalyzer.analyze_replay``；任何异常都吞掉并返回 ``None``。"""
        method = getattr(self.analyzer, "analyze_replay", None)
        if not callable(method):
            return None
        try:
            analysis = method(
                replay,
                run_dir=directory,
                bundle_name=spec.bundle_name,
                device_id=device,
                symptom_kind=self._symptom_kind(spec),
            )
        except Exception:  # 分析是附加信息：失败绝不影响用例结论
            return None
        return analysis if isinstance(analysis, ExecutionAnalysis) else None

    def _analyze_xdevice(
        self,
        result: Any,
        *,
        project_root: Path,
        spec: TestCaseSpec,
        device: str,
    ) -> ExecutionAnalysis | None:
        """调 ``ExecutionAnalyzer.analyze_xdevice``；异常与非法返回一律吞掉。"""
        method = getattr(self.analyzer, "analyze_xdevice", None)
        if not callable(method):
            return None
        try:
            analysis = method(
                result,
                project_root=project_root,
                bundle_name=spec.bundle_name,
                device_id=device,
                symptom_kind=self._symptom_kind(spec),
            )
        except Exception:
            return None
        return analysis if isinstance(analysis, ExecutionAnalysis) else None

    @staticmethod
    def _symptom_kind(spec: TestCaseSpec) -> str | None:
        """缺陷复现用例按 ``functional`` 症状分析（与 ``analyze_run`` 同口径）。"""
        return "functional" if spec.scenario == ScenarioKind.BUG_REPRODUCTION else None

    # ------------------------------------------------------------------
    # 执行证据
    # ------------------------------------------------------------------

    def _write_execution_evidence(self, directory: Path, execution: CaseExecutionRecord) -> None:
        """把执行记录与分析快照写进 ``<vN>/executions/<execution_id>/``。

        纯附加产物：写盘失败（锁、权限、路径异常）绝不影响已经落库的执行结论。
        """
        try:
            evidence_dir = self._require_inside(
                directory / EXECUTIONS_DIRNAME / execution.execution_id,
                "执行证据目录",
            )
            evidence_dir.mkdir(parents=True, exist_ok=True)
            (evidence_dir / "result.json").write_text(
                json.dumps(execution.result or {}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            if execution.analysis is not None:
                (evidence_dir / "analysis.json").write_text(
                    execution.analysis.model_dump_json(indent=2) + "\n",
                    encoding="utf-8",
                )
        except Exception:
            return

    # ------------------------------------------------------------------
    # 执行历史
    # ------------------------------------------------------------------

    def list_executions(self, case_id: str, limit: int = 20) -> list[CaseExecutionRecord]:
        """返回某用例的执行历史（新→旧）。"""
        return self.repository.list_executions(case_id, limit=limit)

    def get_execution(self, execution_id: str) -> CaseExecutionRecord | None:
        """按执行标识读取执行记录；不存在返回 ``None``。"""
        return self.repository.get_execution(execution_id)


__all__ = [
    "CASE_SPEC_FILENAME",
    "DEFAULT_MAX_ITERATIONS",
    "DEVICE_SN_ENV",
    "ENGINE_HYPIUM",
    "ENGINE_XDEVICE",
    "EXECUTIONS_DIRNAME",
    "HYPIUM_DIRNAME",
    "PARAMS_ENV",
    "SAFETY_VIOLATIONS_FILENAME",
    "SAFETY_VIOLATION_TAG",
    "STANDALONE_CONFIG_SCHEMA_VERSION",
    "STANDALONE_DIRNAME",
    "STANDALONE_STATUS_MAP",
    "XDEVICE_DIRNAME",
    "XDEVICE_STATUS_MAP",
    "CaseLibrary",
    "case_id_for_source",
    "safe_script_id",
    "scenario_for_trace",
]
