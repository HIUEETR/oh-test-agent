"""缺陷一等产物：``DefectRecord`` / ``DefectStatus`` / ``DefectRecorder`` 与复现用例转换。

**为什么需要本模块**（缺口 5）：``RunState`` 的 12 个失败态全是 agent 视角，没有任何一个
表示「被测应用有问题」；``reporting.py`` 里「缺陷」两个字一次都没出现。结果是
**一次 agent 步骤全部跑完、但分析抓到 critical ``cppcrash`` 的运行，报告顶部状态是
``completed``，下面是一张红色异常表，没有任何逻辑把两者联系起来。**

设计红线（与 ``analysis/service.py`` 完全一致）：

- 缺陷是**附加结论**，不改变用例 pass/fail，也不改变 Profile 晋级门禁；
- 归并键 ``(bundle_name, kind, page_path, action_target)``：同一应用同一页面同一动作
  反复触发同一类异常 → ``occurrences += 1``、``last_seen_at`` 更新，**不新增记录**；
- ``findings`` 列表（上限 20）与 ``occurrences`` 都保留：即使误并也不丢证据。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from ..models import AnomalyFinding, AnomalyKind, BugReproRequest, utc_now

if TYPE_CHECKING:
    from ..storage.defect_repository import DefectRepository

logger = logging.getLogger(__name__)

#: 单条缺陷保留的 finding 证据上限（超出丢最旧的）。
MAX_FINDINGS_PER_DEFECT = 20
#: 单条缺陷保留的证据路径上限。
MAX_EVIDENCE_PATHS = 40

HARD_FAILURE_SUMMARY = "页面应正常响应并保持在前台"


class DefectStatus(StrEnum):
    """缺陷生命周期状态。"""

    SUSPECTED = "suspected"
    """单次观测，待确认。"""

    CONFIRMED = "confirmed"
    """复现用例重跑后 ``symptom_reproduced=True``。"""

    NOT_REPRODUCED = "not_reproduced"
    DISMISSED = "dismissed"
    """人工判定为误报 / 非缺陷。"""


#: ``symptom_sentinel``（``cases/bug_repro.py``）的**反向**映射。
#: 必须与之保持一致：``tests/unit/test_defect_to_bug_repro.py`` 断言两张表互为反函数。
ANOMALY_TO_SYMPTOM: dict[AnomalyKind, str] = {
    AnomalyKind.CPP_CRASH: "crash",
    AnomalyKind.JS_CRASH: "crash",
    AnomalyKind.APP_FREEZE: "freeze",
    AnomalyKind.ANR: "unresponsive",
    AnomalyKind.PAGE_UNRESPONSIVE: "unresponsive",
    AnomalyKind.WHITE_SCREEN: "white_screen",
    AnomalyKind.LAYOUT_ANOMALY: "layout",
    AnomalyKind.MEMORY_GROWTH: "other",
    # 脚本定位器失效**不是应用缺陷**：并入 ``"other"`` 会让它冒充「other 症状已复现」
    # （``SYMPTOM_KINDS["other"]`` 只认 ``MEMORY_GROWTH``），所以按 ``functional`` 口径
    # 转换——复现判据是「重跑时硬 checkpoint 是否再次失败」，与 ``LOCATOR_STALE`` 无关。
    AnomalyKind.LOCATOR_STALE: "functional",
}

SEVERITY_ORDER: dict[str, int] = {"info": 0, "warning": 1, "critical": 2}


class DefectRecord(BaseModel):
    """一条应用缺陷：跨运行归并的持久化产物。"""

    schema_version: Literal[1] = 1
    defect_id: str
    run_id: str = ""
    session_id: str = ""
    """DC 来源。"""

    case_id: str = ""
    """用例库来源。"""

    bundle_name: str
    kind: AnomalyKind
    severity: Literal["info", "warning", "critical"]
    title_zh: str
    summary_zh: str = ""
    status: DefectStatus = DefectStatus.SUSPECTED
    page_path: str = ""
    action_id: str = ""
    first_seen_at: datetime = Field(default_factory=utc_now)
    last_seen_at: datetime = Field(default_factory=utc_now)
    occurrences: int = 1
    findings: list[AnomalyFinding] = Field(default_factory=list)
    evidence_paths: list[str] = Field(default_factory=list)
    device_id: str = ""
    app_version: str = ""
    repro_case_id: str | None = None
    repro_execution_id: str | None = None
    notes: str = ""


class DefectSummary(BaseModel):
    """列表投影：不含完整 findings（避免列表响应过大）。"""

    defect_id: str
    bundle_name: str
    kind: AnomalyKind
    severity: Literal["info", "warning", "critical"]
    status: DefectStatus
    title_zh: str
    summary_zh: str = ""
    page_path: str = ""
    action_id: str = ""
    occurrences: int = 1
    first_seen_at: datetime
    last_seen_at: datetime
    run_id: str = ""
    session_id: str = ""
    case_id: str = ""
    repro_case_id: str | None = None
    finding_count: int = 0


def action_target(finding: AnomalyFinding) -> str:
    """从 finding 里取「触发动作的目标」，用于归并键。"""
    evidence = finding.evidence or {}
    for key in ("target", "action_target", "tool"):
        value = evidence.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def merge_key(finding: AnomalyFinding, bundle_name: str) -> tuple[str, str, str, str]:
    """缺陷归并键：``(bundle_name, kind, page_path, action_target)``。

    同一应用同一页面同一动作反复触发同一类异常必须并成一条 —— ``occurrences`` 就是
    「偶现 / 必现」的证据强度。
    """
    return (
        bundle_name or str(finding.evidence.get("bundle_name") or ""),
        str(finding.kind),
        finding.page_path or str(finding.evidence.get("page_path") or ""),
        action_target(finding),
    )


def stable_defect_id(key: tuple[str, ...]) -> str:
    """由归并键派生的**稳定** id：同一个缺陷在任何一次运行里都得到同一个 id。

    这让「运行中发现的 finding」与「事后分析发现的 finding」能在 ``_attach_analysis``
    里按 id 去重合并，而不需要额外的映射表。
    """
    digest = hashlib.sha256("\u0000".join(key).encode("utf-8")).hexdigest()[:12]
    return f"defect-{digest}"


def title_for(finding: AnomalyFinding) -> str:
    """人类可读的缺陷标题：优先复用 finding 的摘要。"""
    summary = (finding.summary_zh or "").strip()
    if summary:
        return summary[:200]
    return f"{finding.kind} 异常"[:200]


def make_defect_id() -> str:
    """生成一条新缺陷的 id（``defect-<UTC 时间戳>-<随机后缀>``）。"""
    return f"defect-{utc_now():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


class DefectRecorder:
    """把 :class:`AnomalyFinding` 归并、落库为 :class:`DefectRecord`。

    **可选注入纪律**：只有 ``api/app.py`` 与 ``cli.py`` 注入具体仓库；单测不注入时
    根本不构造本类，因此不会把产物写进仓库 ``artifacts/``。
    """

    def __init__(self, repository: DefectRepository):
        self.repository = repository

    # ------------------------------------------------------------------ 写入

    def record_from_findings(
        self,
        *,
        findings: list[AnomalyFinding],
        bundle_name: str,
        run_id: str = "",
        session_id: str = "",
        case_id: str = "",
        device_id: str = "",
        app_version: str = "",
    ) -> list[DefectRecord]:
        """归并并落库一批 finding；返回受影响的缺陷记录（同 id 只返回一次）。"""
        if not findings:
            return []
        records: dict[str, DefectRecord] = {}
        for finding in findings:
            try:
                record = self.record_one(
                    finding,
                    bundle_name=bundle_name,
                    run_id=run_id,
                    session_id=session_id,
                    case_id=case_id,
                    device_id=device_id,
                    app_version=app_version,
                )
            except Exception as exc:  # noqa: BLE001 - 缺陷落库是 advisory，绝不打断收尾
                logger.warning("cannot record defect from finding %s: %s: %s", finding.kind, type(exc).__name__, exc)
                continue
            if record is not None:
                records[record.defect_id] = record
        return list(records.values())

    def record_one(
        self,
        finding: AnomalyFinding,
        *,
        bundle_name: str,
        run_id: str = "",
        session_id: str = "",
        case_id: str = "",
        device_id: str = "",
        app_version: str = "",
    ) -> DefectRecord | None:
        """记录一条 finding；返回归并后的缺陷记录。

        「查已有 → 归并 → 覆盖写」必须在**同一把写锁**内完成，否则并发两次观测都会读到
        旧记录，``occurrences`` 少算（丢更新）。
        """
        with self.repository.write_lock:
            return self._record_one_locked(
                finding,
                bundle_name=bundle_name,
                run_id=run_id,
                session_id=session_id,
                case_id=case_id,
                device_id=device_id,
                app_version=app_version,
            )

    def _record_one_locked(
        self,
        finding: AnomalyFinding,
        *,
        bundle_name: str,
        run_id: str,
        session_id: str,
        case_id: str,
        device_id: str,
        app_version: str,
    ) -> DefectRecord | None:
        key = merge_key(finding, bundle_name)
        existing = self.repository.find_by_merge_key(key)
        if finding.defect_id and existing is None:
            # 调用方已经给了 id（例如运行中检测器）：允许按 id 直接命中。
            existing = self.repository.get(finding.defect_id)
        if existing is None:
            record = self._new_record(
                finding,
                key=key,
                bundle_name=bundle_name,
                run_id=run_id,
                session_id=session_id,
                case_id=case_id,
                device_id=device_id,
                app_version=app_version,
            )
        else:
            record = self._merge(
                existing,
                finding,
                run_id=run_id,
                session_id=session_id,
                case_id=case_id,
                device_id=device_id,
                app_version=app_version,
            )
        if finding.defect_id != record.defect_id:
            finding.defect_id = record.defect_id
        self.repository.save(record)
        return record

    def _new_record(
        self,
        finding: AnomalyFinding,
        *,
        key: tuple[str, ...],
        bundle_name: str,
        run_id: str,
        session_id: str,
        case_id: str,
        device_id: str,
        app_version: str,
    ) -> DefectRecord:
        now = utc_now()
        finding.defect_id = stable_defect_id(key)
        return DefectRecord(
            defect_id=finding.defect_id,
            run_id=run_id,
            session_id=session_id,
            case_id=case_id,
            bundle_name=key[0] or bundle_name,
            kind=finding.kind,
            severity=finding.severity,
            title_zh=title_for(finding),
            summary_zh=finding.summary_zh,
            status=DefectStatus.SUSPECTED,
            page_path=finding.page_path,
            action_id=finding.action_id,
            first_seen_at=finding.detected_at or now,
            last_seen_at=finding.detected_at or now,
            occurrences=1,
            findings=[finding],
            evidence_paths=list(_evidence_paths(finding)),
            device_id=device_id,
            app_version=app_version,
        )

    def _merge(
        self,
        record: DefectRecord,
        finding: AnomalyFinding,
        *,
        run_id: str,
        session_id: str,
        case_id: str,
        device_id: str,
        app_version: str,
    ) -> DefectRecord:
        """同 key 再次观测：``occurrences += 1``、更新 ``last_seen_at``、追加证据。"""
        now = utc_now()
        findings = [*record.findings, finding]
        if len(findings) > MAX_FINDINGS_PER_DEFECT:
            findings = findings[-MAX_FINDINGS_PER_DEFECT:]
        paths = list(record.evidence_paths)
        for path in _evidence_paths(finding):
            if path not in paths:
                paths.append(path)
        if len(paths) > MAX_EVIDENCE_PATHS:
            paths = paths[-MAX_EVIDENCE_PATHS:]
        return record.model_copy(
            update={
                "occurrences": record.occurrences + 1,
                "last_seen_at": finding.detected_at or now,
                "findings": findings,
                "evidence_paths": paths,
                "severity": _more_severe(record.severity, finding.severity),
                "run_id": run_id or record.run_id,
                "session_id": session_id or record.session_id,
                "case_id": case_id or record.case_id,
                "device_id": device_id or record.device_id,
                "app_version": app_version or record.app_version,
                "page_path": record.page_path or finding.page_path,
                "action_id": record.action_id or finding.action_id,
            },
            deep=True,
        )


def _more_severe(left: str, right: str) -> str:
    """取两者中更严重的级别。"""
    return left if SEVERITY_ORDER.get(left, 0) >= SEVERITY_ORDER.get(right, 0) else right


def _evidence_paths(finding: AnomalyFinding) -> list[str]:
    """从 finding 里挑出 Run / 会话内相对产物路径（报告直接建链接）。"""
    paths: list[str] = []
    if finding.screenshot:
        paths.append(finding.screenshot)
    evidence = finding.evidence or {}
    for key, value in evidence.items():
        if key.endswith("_relative") or key in {"screenshot", "hilog_relative", "layout_relative"}:
            if isinstance(value, str) and value:
                paths.append(value)
    return list(dict.fromkeys(paths))


# ---------------------------------------------------------------------------
# 缺陷 → 复现用例请求（Phase 4）
# ---------------------------------------------------------------------------


def defect_to_bug_repro_request(
    record: DefectRecord,
    *,
    trace: object | None = None,
) -> BugReproRequest:
    """把一条缺陷记录转成 :class:`BugReproRequest`，供 ``/api/cases/bug-repro`` 复用。

    转换规则（计划设计 5）：

    - ``symptom_kind`` ← :data:`ANOMALY_TO_SYMPTOM`（与 ``symptom_sentinel`` 互逆）；
    - ``symptom`` ← ``record.summary_zh``；
    - ``repro_steps_nl`` ← ``trace.plan[: action_index+1]`` 的 ``instruction`` 序列
      （到出问题的动作为止）；DC 来源则从 invocations 的工具序列反推中文描述；
    - ``expected`` ← 出问题那一步的 ``PlannedStep.expected``；缺失时用兜底文案；
    - ``actual`` ← ``record.summary_zh`` + 关键 evidence 摘录；
    - ``preconditions`` ← 目标应用身份 + 起始页面。
    """
    symptom_kind = ANOMALY_TO_SYMPTOM.get(record.kind, "other")
    steps, expected = _repro_steps(record, trace)
    preconditions = [f"目标应用：{record.bundle_name or '未知'}"]
    if record.page_path:
        preconditions.append(f"起始页面：{record.page_path}")
    if record.device_id:
        preconditions.append(f"设备：{record.device_id}")
    actual = record.summary_zh or record.title_zh
    evidence_excerpt = _evidence_excerpt(record)
    return BugReproRequest(
        title=record.title_zh[:200] or f"{record.kind} 缺陷",
        symptom=(record.summary_zh or record.title_zh)[:2000],
        symptom_kind=symptom_kind,  # type: ignore[arg-type]
        preconditions=preconditions,
        repro_steps_nl=steps or ["启动目标应用", f"复现页面 {record.page_path or '未知'}"],
        expected=expected,
        actual=(f"{actual}\n证据：{evidence_excerpt}" if evidence_excerpt else actual)[:2000],
    )


def _repro_steps(record: DefectRecord, trace: object | None) -> tuple[list[str], str]:
    """从 trace 的计划 / DC 工具序列反推复现步骤与期望结果。"""
    plan = list(getattr(trace, "plan", None) or [])
    index = _action_index(record, plan)
    if plan:
        steps = [str(getattr(step, "instruction", "") or "").strip() for step in plan[: index + 1]]
        steps = [step for step in steps if step]
        expected = ""
        if 0 <= index < len(plan):
            expected = str(getattr(plan[index], "expected", "") or "").strip()
        return steps, expected or HARD_FAILURE_SUMMARY
    invocations = list(getattr(record, "findings", None) or [])
    steps = [f"执行动作 {finding.action_id}" for finding in invocations if finding.action_id][:10]
    return steps, HARD_FAILURE_SUMMARY


def _action_index(record: DefectRecord, plan: list) -> int:
    """定位出问题的计划步骤下标；找不到时退到最后一个动作。"""
    if record.action_id:
        for position, step in enumerate(plan):
            if str(getattr(step, "step_id", "")) == record.action_id:
                return position
    return max(0, len(plan) - 1)


def _evidence_excerpt(record: DefectRecord, limit: int = 300) -> str:
    """证据摘录：优先用最后一条 finding 的 detail。"""
    for finding in reversed(record.findings):
        detail = (finding.detail or "").strip()
        if detail:
            return detail[:limit]
    matched = ""
    for finding in reversed(record.findings):
        value = finding.evidence.get("matched_line")
        if isinstance(value, str) and value:
            matched = value
            break
    return matched[:limit]


def record_from_finding(
    finding: AnomalyFinding,
    *,
    bundle_name: str,
    run_id: str = "",
    device_id: str = "",
) -> DefectRecord:
    """不落库地构造一条 :class:`DefectRecord`（纯函数，供报告与单测使用）。"""
    key = merge_key(finding, bundle_name)
    finding.defect_id = stable_defect_id(key)
    now = utc_now()
    return DefectRecord(
        defect_id=finding.defect_id,
        run_id=run_id,
        bundle_name=key[0] or bundle_name,
        kind=finding.kind,
        severity=finding.severity,
        title_zh=title_for(finding),
        summary_zh=finding.summary_zh,
        page_path=finding.page_path,
        action_id=finding.action_id,
        first_seen_at=finding.detected_at or now,
        last_seen_at=finding.detected_at or now,
        findings=[finding],
        evidence_paths=_evidence_paths(finding),
        device_id=device_id,
    )


__all__ = [
    "ANOMALY_TO_SYMPTOM",
    "HARD_FAILURE_SUMMARY",
    "MAX_EVIDENCE_PATHS",
    "MAX_FINDINGS_PER_DEFECT",
    "SEVERITY_ORDER",
    "DefectRecord",
    "DefectRecorder",
    "DefectStatus",
    "DefectSummary",
    "action_target",
    "defect_to_bug_repro_request",
    "make_defect_id",
    "merge_key",
    "record_from_finding",
    "stable_defect_id",
    "title_for",
]
