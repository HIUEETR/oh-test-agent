"""编排一次执行的结果分析：hilog / faultlog + 截图 + 原始布局 + 压测指标。

设计红线（计划 D2）：

- **分析永远是建议性的**：``ExecutionAnalysis`` 只作为附加信息，从不翻转 ``passed``；
  :class:`ExecutionAnalyzer` 的每个内部步骤都 best-effort，异常只记日志并降级到
  ``log_coverage`` / ``metrics["log"]["degradations"]``，对外始终返回合法模型。
- 干净通过的回放**不做设备采集**（只做一次廉价的 faultlog 索引扫描）；失败 / 超时才采
  ``hilog -x`` + faultlog。
- 页面无响应需要**两个独立信号**：stdout/stderr 的超时标记（或 ``status == "timed_out"``）
  **且** 帧或 UI 树停滞。
- ``analysis.json`` 写在 attempt 证据目录旁边；Live / xdevice 场景写在各自的证据目录。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..models import (
    AnomalyFinding,
    AnomalyKind,
    ExecutionAnalysis,
    ReplayResult,
    RunTrace,
    ScenarioKind,
)
from ..perception.normalizer import normalize_layout, page_path
from .hilog import fetch_faultlog_evidence, parse_hilog, pidof_marker
from .layout import BOUNDS_RE, detect_layout_anomalies
from .screen import (
    DEFAULT_SCREEN_THRESHOLDS,
    ScreenThresholds,
    detect_blank_screen,
    detect_identical_frames,
)

if TYPE_CHECKING:
    from ..devices.base import DeviceAdapter

logger = logging.getLogger(__name__)

#: 页面无响应的第一路信号：回放输出里的超时标记。
TIMEOUT_MARKER_RE = re.compile(
    r"wait_for_idle|WaitForIdle|find component.*(timeout|not found)|UiTimedOutError",
    re.I,
)

#: 脚本定位器失效：hypium 的实际文案是 ``Can't find component with [BY.key('...')]``。
#: 与 :data:`TIMEOUT_MARKER_RE` 的 ``find component.*(timeout|not found)`` **不匹配**（实测），
#: 因此定位器失效此前完全不被识别，导致 ``passed=false`` 与 ``healthy=true`` 并存。
STALE_LOCATOR_RE = re.compile(
    r"Can't find component with \[(?P<selector>.+?)\]"
    r"|HypiumComponentNotFoundError:\s*(?P<selector2>.+)",
)
STALE_STEP_RE = re.compile(r"line (?P<line>\d+), in \w+\s*\n\s*(?P<source>.+)")
#: traceback 帧的文件路径；用于区分「生成的脚本帧」与 ``site-packages`` 内部帧。
FRAME_PATH_RE = re.compile(r'File "(?P<path>[^"]+)"')

#: 压测内存增长阈值（KB）。生成的用例未附带阈值时使用该默认值。
DEFAULT_MEMORY_GROWTH_THRESHOLD_KB = 30_000

#: 证据目录里的截图名（按时间顺序：失败帧在前，最终帧在后）。
ATTEMPT_IMAGE_ORDER = ("failure.jpeg", "final.jpeg")
IMAGE_SUFFIXES = (".jpeg", ".jpg", ".png")
MAX_LIVE_FRAMES = 3
LAYOUT_GLOB = "layouts/*.json"

ADVISORY_SEVERITIES = frozenset({"warning", "critical"})
CRASH_KINDS = frozenset({AnomalyKind.CPP_CRASH, AnomalyKind.JS_CRASH, AnomalyKind.APP_FREEZE, AnomalyKind.ANR})

FREEZE_KINDS = frozenset({AnomalyKind.APP_FREEZE, AnomalyKind.ANR, AnomalyKind.PAGE_UNRESPONSIVE})

#: ``symptom_kind`` → 判定缺陷复现所需的 :class:`AnomalyKind` 集合（计划 D1 映射表）。
#: 同时接受直接给出的 ``AnomalyKind`` 值（``cppcrash`` / ``anr`` / ``page_unresponsive`` …）。
SYMPTOM_KINDS: dict[str, frozenset[AnomalyKind]] = {
    "crash": frozenset({AnomalyKind.CPP_CRASH, AnomalyKind.JS_CRASH}),
    "freeze": FREEZE_KINDS,
    # 「无响应」比整页冻屏宽一档：页面还活着、轮播还在动，但点击没有产生导航
    # （``NO_OP_NAVIGATION``）同样是用户视角的「点了没反应」，因此并入本条目。
    # 不并入 ``FREEZE_KINDS`` 本身——那会让 ``symptom_kind="freeze"`` 也接受它。
    "unresponsive": FREEZE_KINDS | {AnomalyKind.NO_OP_NAVIGATION},
    "white_screen": frozenset({AnomalyKind.WHITE_SCREEN}),
    "layout": frozenset({AnomalyKind.LAYOUT_ANOMALY}),
    "cppcrash": frozenset({AnomalyKind.CPP_CRASH}),
    "jscrash": frozenset({AnomalyKind.JS_CRASH}),
    "appfreeze": frozenset({AnomalyKind.APP_FREEZE}),
    "anr": frozenset({AnomalyKind.ANR}),
    "page_unresponsive": frozenset({AnomalyKind.PAGE_UNRESPONSIVE}),
    "layout_anomaly": frozenset({AnomalyKind.LAYOUT_ANOMALY}),
    # 缺口 5 的补齐：每个 AnomalyKind 都必须能进入缺陷复现链路。
    # ``memory_growth`` 原先不在任何条目里，``defect_to_bug_repro_request`` 因此无法为它
    # 生成能判出「已复现」的复现用例（Phase 4 反向映射的前置条件）。
    "memory_growth": frozenset({AnomalyKind.MEMORY_GROWTH}),
    "other": frozenset({AnomalyKind.MEMORY_GROWTH}),
    # 脚本定位器失效**不是应用缺陷**，因此绝不能并入 ``"other"``（那会让它冒充
    # 「other 症状已复现」）。它按 ``functional`` 口径判定：重跑时硬 checkpoint 失败
    # 即视为复现——该分支在 :func:`_symptom_reproduced` 里提前返回，下面的集合只为让
    # ``ANOMALY_TO_SYMPTOM`` ↔ ``SYMPTOM_KINDS`` 的反函数不变式成立而登记，不参与判定。
    "functional": frozenset({AnomalyKind.LOCATOR_STALE}),
}


def _naive(value: datetime | None) -> datetime | None:
    """把带时区的时间转成设备本地朴素时间（hilog 时间戳无时区）。"""
    if value is None:
        return None
    return value.astimezone().replace(tzinfo=None) if value.tzinfo is not None else value


def _default_since() -> datetime:
    """没有 trace 起始时间时，用"最近 30 分钟"作为 faultlog 索引的兜底时间窗。"""
    return datetime.now() - timedelta(minutes=30)


def _existing(paths: list[Path]) -> list[Path]:
    """过滤出真实存在的文件。"""
    return [path for path in paths if path.is_file()]


def _hard_checkpoint_failure(replay: ReplayResult) -> bool:
    """判定回放是否发生了硬 checkpoint 失败（结构化核对据优先，其次按状态兜底）。"""
    result = replay.generated_result or {}
    structured = False
    for key in ("checkpoints", "hard_failures", "failures"):
        entries = result.get(key)
        if not isinstance(entries, list) or not entries:
            continue
        structured = True
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("soft") or entry.get("optional"):
                continue
            if entry.get("passed") is False or entry.get("ok") is False or entry.get("failed") is True:
                return True
            if str(entry.get("status", "")).lower() in {"failed", "fail"}:
                return True
    if structured:
        # 只有软 checkpoint 失败时不算硬失败（soft 项在上面的循环里已被跳过）。
        return False
    # 生成脚本含至少一个硬 checkpoint（IR 不变式 5），passed=false 即硬 checkpoint 失败。
    return replay.status == "failed"


def _symptom_reproduced(
    symptom_kind: str | None,
    findings: list[AnomalyFinding],
    hard_checkpoint_failure: bool,
) -> bool | None:
    """按 D1 映射表计算 ``symptom_reproduced``；未映射的症状类型返回 ``None``。"""
    if not symptom_kind:
        return None
    key = symptom_kind.strip().casefold()
    if key == "functional":
        return bool(hard_checkpoint_failure)
    expected = SYMPTOM_KINDS.get(key)
    if expected is None:
        return None
    return any(finding.kind in expected and finding.severity in ADVISORY_SEVERITIES for finding in findings)


def _clean_selector(value: str) -> str:
    """规范化选择器文本。

    ``HypiumComponentNotFoundError:`` 分支会把整句 ``Can't find component with [...]``
    带回来，此时再退一层取方括号内容；hypium 文案里的选择器可能带尾部 ``]`` / 换行。
    """
    text = str(value or "").strip()
    inner = re.search(r"\[(.*)\]", text, re.S)
    if inner is not None:
        text = inner.group(1)
    return text.strip().rstrip("]").strip()


def _extract_stale_selector(text: str) -> str | None:
    """按 :data:`STALE_LOCATOR_RE` 抽取失效选择器；抽不到返回 ``None``。"""
    try:
        match = STALE_LOCATOR_RE.search(text or "")
    except Exception:  # 文本扫描异常不得中断分析
        return None
    if match is None:
        return None
    for group in ("selector", "selector2"):
        selector = _clean_selector(match.group(group) or "")
        if selector:
            return selector
    return None


def _extract_stale_step(text: str) -> tuple[int, str, bool]:
    """抽取 traceback 里的「出错脚本行 + 源码行」，返回 ``(行号, 源码行, 是否脚本帧)``。

    生成的脚本帧优先：同一条 traceback 里还有 ``site-packages``（``.venv`` / ``devicetest``
    / ``hypium``）的内部帧，直接取第一条会把行号指到库代码而不是生成的脚本；两条判据
    （源码行含 ``driver.`` / 帧文件路径不在 ``site-packages`` 里）都不满足时退回第一条。
    """
    first: tuple[int, str] | None = None
    for match in STALE_STEP_RE.finditer(text or ""):
        try:
            line = int(match.group("line"))
        except TypeError, ValueError:
            continue
        source = (match.group("source") or "").strip()
        if first is None:
            first = (line, source)
        paths = list(FRAME_PATH_RE.finditer(text, 0, match.start()))
        path = paths[-1].group("path") if paths else ""
        if "driver." in source or (bool(path) and "site-packages" not in path.replace("\\", "/")):
            return line, source, True
    return (*first, False) if first is not None else (0, "", False)


def _screen_size(hierarchy: Any) -> tuple[int, int] | None:
    """从原始层级的根 bounds 推断屏幕尺寸（ArkUI dump 的第一行常是整屏）。"""
    if not isinstance(hierarchy, dict):
        return None
    attrs = hierarchy.get("attributes")
    if not isinstance(attrs, dict):
        return None
    match = BOUNDS_RE.search(str(attrs.get("bounds") or ""))
    if not match:
        return None
    left, top, right, bottom = (int(item) for item in match.groups())
    if right - left <= 0 or bottom - top <= 0:
        return None
    return right - left, bottom - top


def _structure_fingerprint(hierarchy: Any, width: int, height: int) -> Any:
    """页面结构指纹：语义照 ``BoundedExplorer._stability_fingerprint(page, elements)``。"""
    if not isinstance(hierarchy, dict):
        return None
    elements = normalize_layout(hierarchy, width, height)
    page = page_path(hierarchy)
    try:
        from ..discovery.explorer import BoundedExplorer

        return BoundedExplorer._stability_fingerprint(page, elements)
    except Exception as exc:  # 探索栈不可用时退化为等价语义的本地指纹
        logger.debug("stability fingerprint fallback: %s: %s", type(exc).__name__, exc)
        texts = tuple(sorted(element.content for element in elements if element.content and len(element.content) <= 80))
        return (page, len(elements), texts)


class _Coverage:
    """记录日志采集覆盖度、降级原因与说明性备注。"""

    def __init__(self, need_logs: bool) -> None:
        self.need_logs = need_logs
        self.sources_ok = 0
        self.degradations: list[dict[str, str]] = []
        self.notes: dict[str, Any] = {}

    def ok(self) -> None:
        """记录一路可用的日志来源。"""
        self.sources_ok += 1

    def note(self, key: str, value: Any) -> None:
        """记录说明性信息（不算降级）。"""
        self.notes[key] = value

    def degrade(self, code: str, detail: str) -> None:
        """记录一次降级；只写日志，绝不抛出（相同原因只记一次）。"""
        entry = {"code": code, "detail": detail[:300]}
        if entry in self.degradations:
            return
        logger.warning("analysis degradation [%s]: %s", code, detail)
        self.degradations.append(entry)

    def final(self) -> str:
        """按"有没有降级 / 有没有成功来源"给出 ``log_coverage``。"""
        if self.degradations and self.sources_ok == 0:
            return "unavailable"
        if self.degradations:
            return "partial"
        return "full" if self.sources_ok else "unavailable"

    def metrics(self) -> dict[str, Any]:
        """可 JSON 序列化的覆盖度详情。"""
        return {
            "need_logs": self.need_logs,
            "sources_ok": self.sources_ok,
            "degradations": list(self.degradations),
            "notes": dict(self.notes),
        }


@dataclass
class _FrameGroup:
    """一组按时间顺序排列的帧；每组最多产出一条白屏 finding。"""

    label: str
    paths: list[Path]


@dataclass
class _AnalysisInput:
    """一次分析所需的全部输入（三个公开入口各自组装）。"""

    subject: str
    subject_id: str
    bundle_name: str = ""
    device_id: str = ""
    run_dir: Path | None = None
    evidence_dir: Path | None = None
    stdout_text: str = ""
    frames: list[Path] = field(default_factory=list)
    frame_groups: list[_FrameGroup] = field(default_factory=list)
    layout_candidates: list[Path] = field(default_factory=list)
    generated_result: dict[str, Any] | None = None
    stress_context: bool = False
    hard_checkpoint_failure: bool = False
    symptom_kind: str | None = None
    since: datetime | None = None
    need_logs: bool = False
    timed_out: bool = False
    screen_size: tuple[int, int] | None = None
    extra_metrics: dict[str, Any] = field(default_factory=dict)
    in_run_stall: bool = False
    """Live 运行中由 :class:`~harmony_test_agent.analysis.in_run.InRunDetector` 观测到的
    结构停滞信号（缺口 3：Live 没有 replay stdout，信号 (a) 必须另有来源）。"""

    in_run_stall_finding: AnomalyFinding | None = None
    """触发 ``in_run_stall`` 的那条运行中 finding。

    事后分析据此**继承它的 page_path / action_id / target**：否则升级出的 critical finding
    与它自己归并键不同，同一次前台丢失会在缺陷库里裂成两条，而闭环可能选中没有上下文的那条。"""

    in_run_noop_finding: AnomalyFinding | None = None
    """运行中检测到的 ``NO_OP_NAVIGATION`` finding（如果有）。

    与 ``in_run_stall_finding`` 并列的第二类运行中信号 (a)：它证明「点击确实没产生导航」
    这个**语义级**结论，而帧 / UI 树是否停滞是另一回事——轮播在动的页面正是帧不停滞、
    但点击无反应的场景（真机复盘 run-20260922T141003Z-6bf8bf42）。"""


class ExecutionAnalyzer:
    """执行结果分析器：所有入口都返回合法 :class:`ExecutionAnalysis`，永不抛出。

    ``device_factory`` 形如 ``HarmonyDeviceAdapter``（``Callable[[str], DeviceAdapter]``）；
    未注入时不进行任何设备操作（单测 / 离线场景）。``collect_logs=False`` 只跳过 hilog，
    faultlog 索引仍会尝试。
    """

    def __init__(
        self,
        *,
        device_factory: Callable[[str], DeviceAdapter] | None = None,
        thresholds: ScreenThresholds = DEFAULT_SCREEN_THRESHOLDS,
        collect_logs: bool = True,
        collect_logs_on_success: bool = False,
    ) -> None:
        self.device_factory = device_factory
        self.thresholds = thresholds
        self.collect_logs = collect_logs
        self.collect_logs_on_success = collect_logs_on_success
        self._devices: dict[str, Any] = {}

    def _needs_logs(self, *, passed: bool) -> bool:
        """是否采集 hilog 尾部：干净通过默认不采（除非显式打开 ``collect_logs_on_success``）。"""
        return self.collect_logs_on_success or not passed

    # ------------------------------------------------------------------ 公开入口

    def analyze_replay(
        self,
        replay: ReplayResult,
        *,
        run_dir: Path,
        bundle_name: str,
        device_id: str,
        trace: RunTrace | None = None,
        symptom_kind: str | None = None,
    ) -> ExecutionAnalysis:
        """分析一次回放 attempt：设备日志 + 截图 + 布局 + 压测指标。"""
        run_dir = Path(run_dir)
        subject_id = f"{trace.run_id if trace else run_dir.name}#attempt-{replay.attempt:02d}"
        try:
            attempt_dir = self._attempt_dir(replay, run_dir)
            live_frames = self._snapshot_frames(trace)
            attempt_frames = _existing([attempt_dir / name for name in ATTEMPT_IMAGE_ORDER])
            groups = [
                group
                for group in (
                    _FrameGroup("live_snapshots", live_frames),
                    _FrameGroup("attempt_images", attempt_frames),
                )
                if group.paths
            ]
            frames = live_frames + [path for path in attempt_frames if path not in live_frames]
            layout_candidates = self._layout_candidates(run_dir / "layouts")
            stdout_text = f"{replay.command.stdout}\n{replay.command.stderr}"
            generated = replay.generated_result or {}
            analysis_input = _AnalysisInput(
                subject="hypium_replay",
                subject_id=subject_id,
                bundle_name=bundle_name,
                device_id=device_id,
                run_dir=run_dir,
                evidence_dir=attempt_dir,
                stdout_text=stdout_text,
                frames=frames,
                frame_groups=groups,
                layout_candidates=layout_candidates,
                generated_result=replay.generated_result,
                stress_context=(trace.scenario == ScenarioKind.STRESS if trace else False)
                or bool(generated.get("stress")),
                hard_checkpoint_failure=_hard_checkpoint_failure(replay),
                symptom_kind=symptom_kind,
                since=trace.started_at if trace else None,
                need_logs=self._needs_logs(passed=replay.status == "passed"),
                timed_out=replay.timed_out or replay.status == "timed_out",
                screen_size=self._snapshot_size(trace),
            )
            return self._analyze(analysis_input, fetch_layout=self._can_fetch_layout(analysis_input))
        except Exception as exc:
            return self._fallback(
                "hypium_replay",
                subject_id,
                bundle_name,
                device_id,
                exc,
                evidence_dir=self._safe_attempt_dir(replay, run_dir),
            )

    def analyze_dc_script(
        self,
        replay: ReplayResult,
        *,
        run_dir: Path,
        bundle_name: str,
        device_id: str,
        session_id: str,
        symptom_kind: str | None = None,
    ) -> ExecutionAnalysis:
        """分析一次直流（DC）录制脚本的诊断执行；``subject="dc_script"``。

        实现上委托 :meth:`analyze_replay` 后改写 ``subject`` / ``subject_id``：DC 脚本
        执行**没有** trace 上下文，因此 ``need_logs`` 独立判定（``replay.status != "passed"``
        才采 hilog），其余分析步骤（faultlog 索引、白屏、布局、无响应、压测指标）完全复用。

        这是缺口 1 里 "``ExecutionAnalysis.subject`` 声明了 ``dc_script`` 却从未被构造"
        的修复点。
        """
        analysis = self.analyze_replay(
            replay,
            run_dir=run_dir,
            bundle_name=bundle_name,
            device_id=device_id,
            trace=None,
            symptom_kind=symptom_kind,
        )
        return analysis.model_copy(
            update={
                "subject": "dc_script",
                "subject_id": f"{session_id or run_dir.name}#attempt-{replay.attempt:02d}",
            }
        )

    def analyze_run(self, trace: RunTrace, run_dir: Path) -> ExecutionAnalysis:
        """分析一次 Live 运行：合并各 attempt 的 finding，并扫描 Live 截图与布局。"""
        run_dir = Path(run_dir)
        try:
            bundle_name = self._trace_bundle(trace)
            snapshots = trace.snapshots[-MAX_LIVE_FRAMES:]
            live_frames = _existing([snapshot.image_path for snapshot in snapshots])
            replays = list(trace.replays)
            stdout_text = "\n".join(f"{item.command.stdout}\n{item.command.stderr}" for item in replays)
            generated = next((item.generated_result for item in reversed(replays) if item.generated_result), None)
            need_logs = (
                bool(trace.agent_error) or trace.replay_status in {"failed", "partial"} or self.collect_logs_on_success
            )
            analysis_input = _AnalysisInput(
                subject="live_run",
                subject_id=trace.run_id,
                bundle_name=bundle_name,
                device_id=trace.device_id,
                run_dir=run_dir,
                evidence_dir=run_dir,
                stdout_text=stdout_text,
                frames=live_frames,
                frame_groups=[_FrameGroup("live_snapshots", live_frames)] if live_frames else [],
                layout_candidates=self._layout_candidates(run_dir / "layouts"),
                generated_result=generated,
                stress_context=trace.scenario == ScenarioKind.STRESS,
                hard_checkpoint_failure=any(_hard_checkpoint_failure(item) for item in replays),
                symptom_kind="functional" if trace.scenario == ScenarioKind.BUG_REPRODUCTION else None,
                since=trace.started_at,
                need_logs=need_logs,
                timed_out=any(item.timed_out for item in replays),
                screen_size=self._snapshot_size(trace),
                # 缺口 3：Live 没有 replay stdout，无响应判定的信号 (a) 来自运行中检测。
                # 同时把触发它的那条 finding 传下去：升级后的 critical finding 必须继承
                # 它的 page_path / action_id / target，才能与它归并成同一条缺陷。
                in_run_stall_finding=next(
                    (item for item in trace.defects if item.kind == AnomalyKind.PAGE_UNRESPONSIVE),
                    None,
                ),
                # 同一来源的第二类信号：运行中检测已经判出「点击没产生导航」。
                in_run_noop_finding=next(
                    (item for item in trace.defects if item.kind == AnomalyKind.NO_OP_NAVIGATION),
                    None,
                ),
            )
            analysis = self._analyze(analysis_input, fetch_layout=self._can_fetch_layout(analysis_input))
            symptom_kind = "functional" if trace.scenario == ScenarioKind.BUG_REPRODUCTION else None
            return self._merge_attempt_findings(analysis, replays, symptom_kind)
        except Exception as exc:
            return self._fallback("live_run", trace.run_id, "", trace.device_id, exc, evidence_dir=run_dir)

    def analyze_xdevice(
        self,
        result: Any,
        *,
        project_root: Path,
        bundle_name: str,
        device_id: str,
        symptom_kind: str | None = None,
    ) -> ExecutionAnalysis:
        """分析一次 xdevice 执行；``result`` 只按鸭子类型取属性，绝不 import runner.xdevice。"""
        project_root = Path(project_root)
        report_dir = Path(getattr(result, "report_dir", None) or project_root)
        subject_id = report_dir.name or project_root.name
        try:
            command = getattr(result, "command", None)
            stdout_text = f"{getattr(command, 'stdout', '') or ''}\n{getattr(command, 'stderr', '') or ''}"
            status = str(getattr(result, "status", "") or "")
            passed = bool(getattr(result, "passed", False))
            frames = self._xdevice_frames(result, report_dir, project_root)
            generated = self._read_json(report_dir / "generated_result.json")
            analysis_input = _AnalysisInput(
                subject="xdevice_run",
                subject_id=subject_id,
                bundle_name=bundle_name,
                device_id=device_id,
                run_dir=report_dir,
                evidence_dir=report_dir,
                stdout_text=stdout_text,
                frames=frames,
                frame_groups=[_FrameGroup("xdevice_evidence", frames)] if frames else [],
                layout_candidates=self._xdevice_layouts(result, report_dir, project_root),
                generated_result=generated,
                stress_context=bool((generated or {}).get("stress")),
                hard_checkpoint_failure=(status == "failed" and not passed),
                symptom_kind=symptom_kind,
                since=None,
                need_logs=not passed or status == "timed_out" or self.collect_logs_on_success,
                timed_out=status == "timed_out" or bool(getattr(result, "timed_out", False)),
            )
            return self._analyze(analysis_input, fetch_layout=self._can_fetch_layout(analysis_input))
        except Exception as exc:
            return self._fallback("xdevice_run", subject_id, bundle_name, device_id, exc, evidence_dir=report_dir)

    # ------------------------------------------------------------------ 分析流水线

    def _analyze(self, analysis_input: _AnalysisInput, *, fetch_layout: bool) -> ExecutionAnalysis:
        """按 D2 的 8 步流水线做分析；每一步都已单独兜底。"""
        coverage = _Coverage(analysis_input.need_logs)
        log_findings = self._collect_device_logs(analysis_input, coverage)
        groups_metrics: dict[str, Any] = {}
        blank_findings, tail_run, tail_digests = self._scan_frames(analysis_input, coverage, groups_metrics)
        layout_findings, layout_stale, layout_file = self._scan_layout(analysis_input, coverage, fetch_layout)
        unresponsive = self._detect_unresponsive(analysis_input, tail_run, layout_stale)
        # 5.3 的区分规则：定位器失效与页面无响应是**两个独立信号，互不抑制**——
        # 页面没有停滞时只留下 LOCATOR_STALE；页面确实停滞时 _detect_unresponsive 会另外
        # 产出一条 PAGE_UNRESPONSIVE，这里原样并进 findings，不做任何一方对另一方的过滤。
        stale_locator = self._detect_stale_locator(analysis_input)
        stress_finding, stress_metrics = self._scan_stress(analysis_input, coverage)
        findings = [*log_findings, *blank_findings, *layout_findings, *unresponsive]
        if stale_locator is not None:
            findings.append(stale_locator)
        if stress_finding is not None:
            findings.append(stress_finding)
        findings = self._escalate_blank_screen(findings)
        for finding in findings:
            # 只有 InRunDetector 已经标过 in_run 的才保留；其余都是事后分析发现。
            if finding.phase != "in_run":
                finding.phase = "post_hoc"
        healthy = not any(finding.severity in ADVISORY_SEVERITIES for finding in findings)
        metrics: dict[str, Any] = {
            "log": coverage.metrics(),
            "screens": groups_metrics,
            "identical_tail_frames": tail_run,
            "frame_tail_sha256": tail_digests,
            "layout": {"file": layout_file, "stale": layout_stale, "findings": len(layout_findings)},
            "stress": stress_metrics,
            "hard_checkpoint_failure": analysis_input.hard_checkpoint_failure,
            **analysis_input.extra_metrics,
        }
        analysis = ExecutionAnalysis(
            subject=analysis_input.subject,
            subject_id=analysis_input.subject_id,
            bundle_name=analysis_input.bundle_name,
            device_id=analysis_input.device_id,
            healthy=healthy,
            findings=findings,
            symptom_reproduced=_symptom_reproduced(
                analysis_input.symptom_kind, findings, analysis_input.hard_checkpoint_failure
            ),
            metrics=metrics,
            log_coverage=coverage.final(),
        )
        self._write_analysis(analysis, analysis_input.evidence_dir, coverage)
        return analysis

    def _collect_device_logs(self, analysis_input: _AnalysisInput, coverage: _Coverage) -> list[AnomalyFinding]:
        """第 1 步：faultlog 索引（总是）与 hilog 采集（仅失败 / 超时）。"""
        findings: list[AnomalyFinding] = []
        device = self._device(analysis_input, coverage)
        if device is None:
            return findings
        try:
            faultlog = fetch_faultlog_evidence(
                device,
                analysis_input.bundle_name,
                analysis_input.since or _default_since(),
                output_dir=analysis_input.evidence_dir,
            )
            for finding in faultlog:
                saved = finding.evidence.get("saved")
                if isinstance(saved, str) and analysis_input.evidence_dir is not None:
                    # 追加 run 内相对路径，供 HTML 报告建产物链接。
                    finding.evidence["faultlog_relative"] = self._relative(
                        analysis_input.run_dir, Path(analysis_input.evidence_dir) / saved
                    )
            findings.extend(faultlog)
            coverage.ok()
            coverage.note("faultlog_findings", len(faultlog))
        except Exception as exc:
            coverage.degrade("faultlog_failed", f"{type(exc).__name__}: {exc}")
        if not (analysis_input.need_logs and self.collect_logs):
            coverage.note("hilog_skipped", "clean_pass")
            return findings
        if analysis_input.evidence_dir is None:
            coverage.degrade("hilog_failed", "no attempt evidence directory for hilog_after.txt")
            return findings
        try:
            target = Path(analysis_input.evidence_dir) / "hilog_after.txt"
            result = device.collect_logs(target)
            text = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
            if not text:
                text = f"{getattr(result, 'stdout', '')}\n{getattr(result, 'stderr', '')}"
            header = self._pidof_marker(device, analysis_input.bundle_name, coverage)
            window = self._log_window(analysis_input)
            parsed = parse_hilog(f"{header}{text}", bundle_name=analysis_input.bundle_name, window=window)
            findings.extend(parsed)
            coverage.ok()
            coverage.note("hilog_findings", len(parsed))
        except Exception as exc:
            coverage.degrade("hilog_failed", f"{type(exc).__name__}: {exc}")
        return findings

    def _pidof_marker(self, device: Any, bundle_name: str, coverage: _Coverage) -> str:
        """尝试用 ``pidof`` 解析被测应用 pid，作为归属判定的补充信号。"""
        runner = getattr(device, "_run", None)
        if runner is None or not bundle_name:
            return ""
        try:
            result = runner("shell", "pidof", bundle_name)
        except Exception as exc:
            coverage.note("pidof", f"failed: {type(exc).__name__}")
            return ""
        pids = re.findall(r"\d+", str(getattr(result, "stdout", "") or ""))
        coverage.note("pidof", pids[:5])
        return pidof_marker(bundle_name, pids)

    def _scan_frames(
        self, analysis_input: _AnalysisInput, coverage: _Coverage, groups_metrics: dict[str, Any]
    ) -> tuple[list[AnomalyFinding], int, list[str]]:
        """第 2 步：白屏扫描（每个帧组最多一条 finding）+ 末尾同帧长度。"""
        findings: list[AnomalyFinding] = []
        tail_run, tail_digests = 0, []
        if analysis_input.frames:
            try:
                tail_run, tail_digests = detect_identical_frames(analysis_input.frames)
            except Exception as exc:
                coverage.degrade("frame_compare_failed", f"{type(exc).__name__}: {exc}")
        for group in analysis_input.frame_groups:
            try:
                blanks = [
                    (path, finding)
                    for path in group.paths
                    if (finding := detect_blank_screen(path, self.thresholds)) is not None
                ]
            except Exception as exc:
                coverage.degrade("blank_screen_failed", f"{group.label}: {type(exc).__name__}: {exc}")
                continue
            if not blanks:
                groups_metrics[group.label] = {"scanned": len(group.paths), "blank": 0}
                continue
            coverage.note("screenshot_scan", group.label)
            last_path, finding = blanks[-1]
            finding.evidence["frame_group"] = group.label
            finding.evidence["images"] = [path.name for path, _ in blanks]
            finding.evidence["image_relative"] = self._relative(analysis_input.run_dir, last_path)
            finding.evidence["frames_scanned"] = len(group.paths)
            groups_metrics[group.label] = {"scanned": len(group.paths), "blank": len(blanks)}
            findings.append(finding)
        return findings, tail_run, tail_digests

    def _scan_layout(
        self, analysis_input: _AnalysisInput, coverage: _Coverage, fetch_layout: bool
    ) -> tuple[list[AnomalyFinding], bool, str | None]:
        """第 4 步：最新原始 ArkUI dump 上的布局异常 + 最后两帧树是否停滞。"""
        hierarchy: Any = None
        layout_file: Path | None = None
        fetched = self._fetch_layout(analysis_input, coverage) if fetch_layout else None
        if fetched is not None:
            hierarchy, layout_file = fetched
        elif analysis_input.layout_candidates:
            layout_file = analysis_input.layout_candidates[0]
            hierarchy = self._read_json(layout_file)
        stale = False
        if len(analysis_input.layout_candidates) >= 2:
            stale = self._layouts_stale(analysis_input, coverage)
        if hierarchy is None:
            coverage.note("layout", "unavailable")
            return [], stale, None
        size = _screen_size(hierarchy) or analysis_input.screen_size
        if size is None:
            coverage.note("layout", "skipped:unknown_screen_size")
            return [], stale, self._relative(analysis_input.run_dir, layout_file) if layout_file else None
        try:
            findings = detect_layout_anomalies(hierarchy, size[0], size[1])
        except Exception as exc:
            coverage.degrade("layout_detection_failed", f"{type(exc).__name__}: {exc}")
            return [], stale, self._relative(analysis_input.run_dir, layout_file) if layout_file else None
        relative = self._relative(analysis_input.run_dir, layout_file) if layout_file else None
        for finding in findings:
            finding.evidence["layout_relative"] = relative
        coverage.note("layout_findings", len(findings))
        return findings, stale, relative

    def _fetch_layout(
        self, analysis_input: _AnalysisInput, coverage: _Coverage
    ) -> tuple[dict[str, Any], Path | None] | None:
        """失败 / 超时时现取一次设备 UI 树，把原始 dump 存成 attempt 证据。"""
        if analysis_input.evidence_dir is None:
            return None
        device = self._device(analysis_input, coverage)
        if device is None:
            return None
        try:
            hierarchy = device.collect_ui_hierarchy()
        except Exception as exc:
            coverage.note("layout_after", f"failed: {type(exc).__name__}")
            return None
        if not isinstance(hierarchy, dict):
            coverage.note("layout_after", "failed:not_a_dict")
            return None
        path = Path(analysis_input.evidence_dir) / "layout_after.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(hierarchy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            coverage.note("layout_after", path.name)
        except OSError as exc:
            coverage.note("layout_after", f"unwritten: {exc}")
            path = None
        return hierarchy, path

    def _layouts_stale(self, analysis_input: _AnalysisInput, coverage: _Coverage) -> bool:
        """比较最新的两份 layout 的结构指纹（语义同 BoundedExplorer 的稳定性指纹）。"""
        try:
            latest = self._read_json(analysis_input.layout_candidates[0])
            previous = self._read_json(analysis_input.layout_candidates[1])
            if latest is None or previous is None:
                return False
            fallback = analysis_input.screen_size or (0, 0)
            size = _screen_size(latest) or fallback
            left = _structure_fingerprint(latest, size[0], size[1])
            right = _structure_fingerprint(previous, size[0], size[1])
            return left is not None and left == right
        except Exception as exc:
            coverage.degrade("layout_fingerprint_failed", f"{type(exc).__name__}: {exc}")
            return False

    def _detect_unresponsive(
        self, analysis_input: _AnalysisInput, tail_run: int, layout_stale: bool
    ) -> list[AnomalyFinding]:
        """第 3 步：两个独立信号（超时标记 **或** 运行中结构停滞 + 帧/树停滞）才判页面无响应。"""
        try:
            match = TIMEOUT_MARKER_RE.search(analysis_input.stdout_text or "")
        except Exception as exc:  # 文本异常不得中断分析
            logger.warning("timeout marker scan failed: %s: %s", type(exc).__name__, exc)
            return []
        marker = bool(match) or analysis_input.timed_out
        noop_origin = analysis_input.in_run_noop_finding
        stalled = tail_run >= 2 or layout_stale
        # 「点击后未导航」不依赖帧 / UI 树停滞：轮播在动的页面正是帧不停滞、但点击无反应，
        # 因此只要有运行中信号就继续（其余分支仍要求 stalled，规则不变）。
        if not stalled and noop_origin is None:
            return []
        # 继承触发信号的那条运行中 finding 的上下文：升级后的 critical finding 必须与它
        # 归并成同一条缺陷（否则同一次前台丢失会裂成两条，闭环可能选到没有上下文的那条）。
        origin = analysis_input.in_run_stall_finding
        origin_evidence = dict(origin.evidence) if origin is not None and origin.evidence else {}
        evidence: dict[str, Any] = {
            "timeout_marker": bool(match),
            "timeout_match": match.group(0) if match else "",
            "timed_out_status": analysis_input.timed_out,
            # 缺口 3：Live 运行没有 replay stdout，信号 (a) 由 InRunDetector 的结构停滞观测提供
            "in_run_stall": analysis_input.in_run_stall or origin is not None,
            "in_run_noop_navigation": noop_origin is not None,
            "identical_tail_frames": tail_run,
            "layout_stale": layout_stale,
            "stress_context": analysis_input.stress_context,
        }
        if origin is not None:
            evidence["in_run_origin"] = origin.summary_zh
            if origin.action_id:
                evidence["action_id"] = origin.action_id
            for key in ("tool", "target"):
                value = origin_evidence.get(key)
                if value:
                    evidence[key] = value
        findings: list[AnomalyFinding] = []
        if marker:
            findings.append(
                AnomalyFinding(
                    kind=AnomalyKind.PAGE_UNRESPONSIVE,
                    severity="critical",
                    summary_zh="页面无响应：回放等待超时且画面 / UI 树停滞",
                    detail="超时标记与帧（或 UI 树）停滞两个信号同时命中",
                    source="stdout",
                    phase="replay",
                    action_id=origin.action_id if origin is not None else "",
                    page_path=origin.page_path if origin is not None else "",
                    evidence=evidence,
                )
            )
        elif evidence["in_run_stall"]:
            findings.append(
                AnomalyFinding(
                    kind=AnomalyKind.PAGE_UNRESPONSIVE,
                    severity="critical",
                    summary_zh="页面无响应：多个动作后页面结构持续无变化",
                    detail="运行中检测（InRunDetector）观测到结构停滞，且帧或 UI 树停滞",
                    source="screenshot",
                    phase="in_run",
                    action_id=origin.action_id if origin is not None else "",
                    page_path=origin.page_path if origin is not None else "",
                    screenshot=origin.screenshot if origin is not None else "",
                    evidence=evidence,
                )
            )
        if noop_origin is not None:
            # 「点击后未导航」与整页冻结是**两种故障模式**，互不抑制：前者在这里只把运行中
            # 那条 finding 带进事后分析（analysis.findings / healthy / symptom_reproduced），
            # 结论与证据都沿用运行中那条，避免另造一条归并键不同的缺陷。
            # severity 保持 warning —— 单次点击无反应是信号不是判决。
            findings.append(
                AnomalyFinding(
                    kind=AnomalyKind.NO_OP_NAVIGATION,
                    severity=noop_origin.severity,
                    summary_zh=noop_origin.summary_zh or "点击后页面未发生跳转，疑似无响应控件",
                    detail=noop_origin.detail or "运行中检测（InRunDetector）观测到点击后页面未发生跳转",
                    source="screenshot",
                    phase="in_run",
                    action_id=noop_origin.action_id,
                    page_path=noop_origin.page_path,
                    screenshot=noop_origin.screenshot,
                    detected_at=noop_origin.detected_at,
                    evidence={**dict(noop_origin.evidence), **evidence},
                )
            )
        if not findings and analysis_input.stress_context:
            findings.append(
                AnomalyFinding(
                    kind=AnomalyKind.PAGE_UNRESPONSIVE,
                    severity="warning",
                    summary_zh="压测过程中画面长期不变（仅同帧信号，疑似界面卡住）",
                    detail="压测上下文中只命中帧停滞信号",
                    source="screenshot",
                    evidence=evidence,
                )
            )
        return findings

    def _detect_stale_locator(self, analysis_input: _AnalysisInput) -> AnomalyFinding | None:
        """第 3.5 步：脚本定位器在设备上已失效（选择器过期，非应用缺陷）。

        与 :meth:`_detect_unresponsive` 的关系（**两个信号互不抑制**）：

        - 本检测只看「选择器在设备上找不到」，页面是否真的卡死是另一个独立信号；
        - 页面**没有**停滞 → 只报 ``LOCATOR_STALE``；
        - 页面**确实**停滞（``_detect_unresponsive`` 也产出了 finding）→ 两条都报：
          既不因为「有定位器失效」就吞掉 ``PAGE_UNRESPONSIVE``，也不反过来。

        信息可能只存在于 ``generated_result.json``（这次真机失败的 ``stderr.log`` 是空的），
        因此 stdout/stderr 文本与 ``generated_result`` 的 ``error.message`` / ``traceback``
        **两路都必须扫**。best-effort：任何异常都只记日志并返回 ``None``。
        """
        try:
            return self._detect_stale_locator_unsafe(analysis_input)
        except Exception as exc:  # 定位器扫描失败不得中断分析
            logger.warning("stale locator scan failed: %s: %s", type(exc).__name__, exc)
            return None

    @staticmethod
    def _detect_stale_locator_unsafe(analysis_input: _AnalysisInput) -> AnomalyFinding | None:
        """定位器失效检测主体（由 :meth:`_detect_stale_locator` 包裹兜底）。"""
        sources: list[tuple[str, str]] = []
        if analysis_input.stdout_text:
            sources.append(("stdout", analysis_input.stdout_text))
        payload = analysis_input.generated_result
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str) and message:
                    sources.append(("generated_result", message))
            traceback_text = payload.get("traceback")
            if isinstance(traceback_text, str) and traceback_text:
                sources.append(("generated_result", traceback_text))
        selector, matched_in = "", ""
        for origin, text in sources:
            selector = _extract_stale_selector(text)
            if selector:
                matched_in = origin
                break
        if not selector:
            # 抽不到选择器就不产出 finding：绝不报一条没有证据的「定位器失效」。
            return None
        line, source, preferred = 0, "", False
        for _, text in sources:
            candidate_line, candidate_source, candidate_preferred = _extract_stale_step(text)
            if candidate_source and (candidate_preferred or not source):
                line, source, preferred = candidate_line, candidate_source, candidate_preferred
            if preferred:
                break
        return AnomalyFinding(
            kind=AnomalyKind.LOCATOR_STALE,
            severity="critical",
            summary_zh=f"脚本定位器在设备上已失效：{selector}",
            detail=f"第 {line} 行：{source}" if source else "",
            evidence={
                "selector": selector,
                "script_line": line,
                "source_line": source,
                # ``source`` 字段只有 "stdout" 这个合法取值（``AnomalyFinding.source`` 的
                # Literal 未定义「generated_result」），命中来源另记 ``matched_in`` 保持如实。
                "matched_in": matched_in,
                "exception": "HypiumComponentNotFoundError",
            },
            source="stdout",
            phase="replay",
        )

    def _scan_stress(self, analysis_input: _AnalysisInput, coverage: _Coverage) -> tuple[AnomalyFinding | None, dict]:
        """第 5 步：``generated_result.json["stress"]`` 的内存增长判定。"""
        try:
            return self._scan_stress_unsafe(analysis_input, coverage)
        except Exception as exc:  # 压测统计格式漂移不得中断分析
            coverage.degrade("stress_scan_failed", f"{type(exc).__name__}: {exc}")
            return None, {}

    @staticmethod
    def _scan_stress_unsafe(analysis_input: _AnalysisInput, coverage: _Coverage) -> tuple[AnomalyFinding | None, dict]:
        """压测指标解析主体（由 :meth:`_scan_stress` 包裹兜底）。"""
        stress = (analysis_input.generated_result or {}).get("stress")
        if not isinstance(stress, dict):
            return None, {}
        samples = [
            sample
            for sample in (stress.get("memory_samples") or [])
            if isinstance(sample, dict) and isinstance(sample.get("pss_kb"), int)
        ]
        iterations = stress.get("iterations_completed")
        coverage.note("stress_iterations", iterations)
        metrics: dict[str, Any] = {"iterations_completed": iterations, "samples": len(samples)}
        if len(samples) < 2:
            return None, metrics
        first = int(samples[0]["pss_kb"])
        last = int(samples[-1]["pss_kb"])
        growth = last - first
        threshold = stress.get("memory_growth_threshold_kb")
        if not isinstance(threshold, int) or threshold <= 0:
            threshold = DEFAULT_MEMORY_GROWTH_THRESHOLD_KB
        metrics.update({"pss_first_kb": first, "pss_last_kb": last, "growth_kb": growth, "threshold_kb": threshold})
        if growth <= threshold:
            return None, metrics
        return (
            AnomalyFinding(
                kind=AnomalyKind.MEMORY_GROWTH,
                severity="warning",
                summary_zh=f"压测内存增长 {growth} KB 超过阈值 {threshold} KB",
                detail=f"PSS 从 {first} KB 增长到 {last} KB（{len(samples)} 次采样）",
                source="stress_stats",
                evidence=metrics,
            ),
            metrics,
        )

    def _escalate_blank_screen(self, findings: list[AnomalyFinding]) -> list[AnomalyFinding]:
        """有崩溃 / 卡死佐证时把白屏 finding 从 warning 提升为 critical。"""
        corroborated = any(
            finding.kind in CRASH_KINDS or finding.kind == AnomalyKind.PAGE_UNRESPONSIVE for finding in findings
        )
        if not corroborated:
            return findings
        return [
            finding.model_copy(update={"severity": "critical"})
            if finding.kind == AnomalyKind.WHITE_SCREEN and finding.severity == "warning"
            else finding
            for finding in findings
        ]

    # ------------------------------------------------------------------ 辅助

    def _device(self, analysis_input: _AnalysisInput, coverage: _Coverage) -> Any | None:
        """按需构造（并缓存）设备适配器；任何失败都降级为"没有设备"。"""
        if self.device_factory is None or not analysis_input.device_id:
            if analysis_input.need_logs:
                coverage.degrade("device_unavailable", "device_factory 未注入或 device_id 为空，跳过设备日志采集")
            else:
                coverage.note("device", "unavailable")
            return None
        cached = self._devices.get(analysis_input.device_id)
        if cached is not None:
            return cached
        try:
            device = self.device_factory(analysis_input.device_id)
        except Exception as exc:
            coverage.degrade("device_unavailable", f"{type(exc).__name__}: {exc}")
            return None
        self._devices[analysis_input.device_id] = device
        return device

    @staticmethod
    def _can_fetch_layout(analysis_input: _AnalysisInput) -> bool:
        """只在失败 / 超时、已有证据目录且注入了设备工厂时现取设备 UI 树。"""
        return analysis_input.need_logs and analysis_input.evidence_dir is not None and bool(analysis_input.device_id)

    @staticmethod
    def _log_window(analysis_input: _AnalysisInput) -> tuple[datetime, datetime] | None:
        """hilog 的时间窗（不可用时不做时间过滤）。"""
        start = _naive(analysis_input.since)
        if start is None:
            return None
        return start, _naive(datetime.now()) or datetime.now()

    @staticmethod
    def _read_json(path: Path | None) -> dict[str, Any] | None:
        """读取 JSON 对象；失败返回 None。"""
        if path is None:
            return None
        try:
            content = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            logger.debug("cannot read json %s: %s", path, exc)
            return None
        return content if isinstance(content, dict) else None

    @staticmethod
    def _relative(run_dir: Path | None, path: Path | None) -> str | None:
        """转成 run 目录内相对 posix 路径（不在 run 目录内时退回绝对 posix）。"""
        if path is None:
            return None
        try:
            resolved = Path(path).resolve()
        except OSError:
            return Path(path).as_posix()
        if run_dir is None:
            return resolved.as_posix()
        try:
            return resolved.relative_to(Path(run_dir).resolve()).as_posix()
        except ValueError:
            return resolved.as_posix()

    @staticmethod
    def _sort_by_mtime(paths: list[Path]) -> list[Path]:
        """按修改时间从新到旧排序（不可读的文件排在最后）。"""

        def _mtime(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        return sorted(paths, key=_mtime, reverse=True)

    @classmethod
    def _layout_candidates(cls, directory: Path) -> list[Path]:
        """按修改时间从新到旧返回 layout JSON。"""
        try:
            files = [path for path in Path(directory).glob("*.json") if path.is_file()]
        except OSError:
            return []
        return cls._sort_by_mtime(files)

    @staticmethod
    def _snapshot_frames(trace: RunTrace | None) -> list[Path]:
        """Live 运行最后 N 张截图（按时间顺序）。"""
        if trace is None:
            return []
        return _existing([snapshot.image_path for snapshot in trace.snapshots[-MAX_LIVE_FRAMES:]])

    @staticmethod
    def _snapshot_size(trace: RunTrace | None) -> tuple[int, int] | None:
        """Live 运行最后一张截图的设备尺寸。"""
        if trace is None or not trace.snapshots:
            return None
        snapshot = trace.snapshots[-1]
        if snapshot.width <= 0 or snapshot.height <= 0:
            return None
        return snapshot.width, snapshot.height

    @staticmethod
    def _trace_bundle(trace: RunTrace) -> str:
        """从冻结 Profile / 解析目标里取被测 bundle。"""
        profile = trace.profile_snapshot
        if profile is None and trace.resolved_target is not None:
            profile = trace.resolved_target.profile_snapshot
        return profile.bundle_name if profile is not None else ""

    @staticmethod
    def _attempt_dir(replay: ReplayResult, run_dir: Path) -> Path:
        """解析 attempt 证据目录（``ReplayResult.report_path`` 即 attempt 目录）。"""
        if replay.report_path is not None:
            candidate = Path(replay.report_path)
            if candidate.is_dir():
                return candidate.resolve()
            if candidate.parent.is_dir():
                return candidate.parent.resolve()
        return Path(run_dir) / "hypium" / f"attempt-{replay.attempt:02d}"

    @classmethod
    def _safe_attempt_dir(cls, replay: ReplayResult, run_dir: Path) -> Path | None:
        """兜底路径：解析 attempt 目录失败时返回 None。"""
        try:
            return cls._attempt_dir(replay, run_dir)
        except Exception:
            return None

    @staticmethod
    def _xdevice_frames(result: Any, report_dir: Path, project_root: Path) -> list[Path]:
        """xdevice 证据目录 + ``evidence_paths`` 中的截图（最多 3 张）。"""
        frames = _existing([report_dir / name for name in ATTEMPT_IMAGE_ORDER])
        for raw in list(getattr(result, "evidence_paths", None) or []):
            candidate = Path(str(raw))
            if not candidate.is_absolute():
                candidate = project_root / candidate
            if candidate.suffix.lower() in IMAGE_SUFFIXES and candidate not in frames:
                frames.append(candidate)
        return _existing(frames)[-MAX_LIVE_FRAMES:]

    @staticmethod
    def _xdevice_layouts(result: Any, report_dir: Path, project_root: Path) -> list[Path]:
        """xdevice 证据里指向 layouts 目录的 JSON（新→旧）。"""
        candidates: list[Path] = []
        for raw in list(getattr(result, "evidence_paths", None) or []):
            candidate = Path(str(raw))
            if not candidate.is_absolute():
                candidate = project_root / candidate
            if candidate.suffix.lower() == ".json" and "layouts" in candidate.parts and candidate.is_file():
                candidates.append(candidate)
        for json_file in ExecutionAnalyzer._layout_candidates(report_dir / "layouts"):
            if json_file not in candidates:
                candidates.append(json_file)
        return ExecutionAnalyzer._sort_by_mtime(candidates)

    def _merge_attempt_findings(
        self, analysis: ExecutionAnalysis, replays: list[ReplayResult], symptom_kind: str | None
    ) -> ExecutionAnalysis:
        """把各 attempt 已有分析的 finding 合并进 run 级分析（按 kind + detail 去重）。"""
        seen = {(finding.kind, finding.detail[:200]) for finding in analysis.findings}
        merged = list(analysis.findings)
        for replay in replays:
            existing = replay.analysis
            if existing is None:
                continue
            for finding in existing.findings:
                key = (finding.kind, finding.detail[:200])
                if key in seen:
                    continue
                seen.add(key)
                merged.append(finding)
        if len(merged) == len(analysis.findings):
            return analysis
        healthy = not any(finding.severity in ADVISORY_SEVERITIES for finding in merged)
        hard_failure = bool(analysis.metrics.get("hard_checkpoint_failure"))
        return analysis.model_copy(
            update={
                "findings": merged,
                "healthy": healthy,
                "symptom_reproduced": _symptom_reproduced(symptom_kind, merged, hard_failure),
            }
        )

    @staticmethod
    def _write_analysis(analysis: ExecutionAnalysis, evidence_dir: Path | None, coverage: _Coverage) -> None:
        """第 8 步：把 ``analysis.json`` 写到证据目录旁边；失败只记日志。"""
        if evidence_dir is None:
            coverage.degrade("analysis_json_failed", "没有可用的证据目录")
            return
        target = Path(evidence_dir) / "analysis.json"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(analysis.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("cannot write analysis.json to %s: %s", target, exc)
            log_metrics = analysis.metrics.setdefault("log", {})
            if isinstance(log_metrics, dict):
                degradations = log_metrics.setdefault("degradations", [])
                if isinstance(degradations, list):
                    degradations.append({"code": "analysis_json_failed", "detail": str(exc)[:300]})

    def _fallback(
        self,
        subject: str,
        subject_id: str,
        bundle_name: str,
        device_id: str,
        exc: Exception,
        *,
        evidence_dir: Path | None,
    ) -> ExecutionAnalysis:
        """兜底：任何未预料的异常都只记日志，并返回合法的降级分析结果。"""
        logger.warning("analysis failed for %s: %s: %s", subject_id, type(exc).__name__, exc)
        log_metrics = {
            "need_logs": False,
            "sources_ok": 0,
            "degradations": [{"code": "analysis_failed", "detail": f"{type(exc).__name__}: {exc}"[:300]}],
            "notes": {},
        }
        analysis = ExecutionAnalysis(
            subject=subject,
            subject_id=subject_id,
            bundle_name=bundle_name,
            device_id=device_id,
            healthy=True,
            findings=[],
            symptom_reproduced=None,
            metrics={"log": log_metrics},
            log_coverage="unavailable",
        )
        coverage = _Coverage(False)
        self._write_analysis(analysis, evidence_dir, coverage)
        return analysis


__all__ = [
    "DEFAULT_MEMORY_GROWTH_THRESHOLD_KB",
    "STALE_LOCATOR_RE",
    "STALE_STEP_RE",
    "SYMPTOM_KINDS",
    "TIMEOUT_MARKER_RE",
    "ExecutionAnalyzer",
]
