"""解析 hilog 文本与 faultlog 索引，产出崩溃 / 卡死 / ANR 类异常发现。

设计要点（对应计划 D2）：

- hilog 时间戳没有年份，用 ``window[0].year``（无 window 时用当前年）补全；
- 归属判定优先看行内是否含 ``bundle_name``，其次看行内 pid 是否命中 pidof 标记
  （调用方可在文本头部插入 ``# pidof <bundle>: <pid...>`` 标记行，见
  :func:`pidof_marker`）；无法归属的行降一级 severity 并标 ``attribution=system``；
- 相同 ``(kind, 匹配行前 200 字符)`` 去重，最多保留 10 行匹配原文；
- 本模块只产出 :class:`~harmony_test_agent.models.AnomalyFinding`，永不翻转用例的 ``passed``。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import AnomalyFinding, AnomalyKind

if TYPE_CHECKING:
    from ..devices.base import DeviceAdapter

logger = logging.getLogger(__name__)

HILOG_TIMESTAMP_RE = re.compile(r"^(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})\.(\d{1,3})")
PID_MARKER_RE = re.compile(r"^#\s*pidof\s+(\S+)\s*:\s*([\d\s]*)$")
FAULTLOG_DATE_RE = re.compile(r"-(\d{8})")
FAULTLOG_NAME_TIME_RE = re.compile(r"-(\d{8})-(\d{6})")
FAULTLOG_REASON_RE = re.compile(r"^\s*Reason:\s*(.+)$", re.M)
FAULTLOG_PROCESS_RE = re.compile(r"^\s*(?:Process name|process name):\s*(\S+)", re.M)
FAULTLOG_FRAME_RE = re.compile(r"^\s*#\d{2}\s+pc\s+[0-9a-f]{8,}.*$", re.M)

#: 异常模式表：``(kind, severity, 正则)``；顺序即优先级，同一行命中多条时取最高 severity。
CRASH_PATTERNS: tuple[tuple[AnomalyKind, str, re.Pattern[str]], ...] = (
    (AnomalyKind.CPP_CRASH, "critical", re.compile(r"\bcppcrash\b", re.I)),
    (AnomalyKind.CPP_CRASH, "critical", re.compile(r"Reason:\s*Signal\s*:?\s*SIG(SEGV|ABRT|ILL|FPE|BUS)")),
    (AnomalyKind.CPP_CRASH, "warning", re.compile(r"^\s*#\d{2}\s+pc\s+[0-9a-f]{8,}", re.M)),
    (AnomalyKind.JS_CRASH, "critical", re.compile(r"\bjscrash\b|\bJsError\b", re.I)),
    (AnomalyKind.JS_CRASH, "warning", re.compile(r"Error\s+name:\s*\w*Error")),
    (AnomalyKind.APP_FREEZE, "critical", re.compile(r"\bappfreeze\b", re.I)),
    (AnomalyKind.ANR, "critical", re.compile(r"THREAD_BLOCK_\d+S|LIFECYCLE_TIMEOUT|APP_INPUT_BLOCK")),
)

SUMMARY_ZH: dict[AnomalyKind, str] = {
    AnomalyKind.CPP_CRASH: "hilog 中出现 C++ 崩溃（cppcrash / 信号或调用栈）",
    AnomalyKind.JS_CRASH: "hilog 中出现 JS 崩溃（jscrash / JsError）",
    AnomalyKind.APP_FREEZE: "hilog 中出现应用冻屏（appfreeze）",
    AnomalyKind.ANR: "hilog 中出现应用无响应（THREAD_BLOCK / LIFECYCLE_TIMEOUT / APP_INPUT_BLOCK）",
}

FAULTLOG_KIND_BY_PREFIX: dict[str, AnomalyKind] = {
    "cppcrash": AnomalyKind.CPP_CRASH,
    "jscrash": AnomalyKind.JS_CRASH,
    "appfreeze": AnomalyKind.APP_FREEZE,
}

FAULTLOG_DIR = "/data/log/faultlog/faultlogger"
FAULTLOG_MAX_FILES = 3
FAULTLOG_HEAD_BYTES = 4096
MAX_EVIDENCE_LINES = 10
SIGNATURE_CHARS = 200
RAW_LINE_CHARS = 500
HEAD_EXCERPT_CHARS = 2000

_SEVERITY_ORDER = ("info", "warning", "critical")


def _downgrade(severity: str) -> str:
    """把无法归属被测应用的 finding 降一级 severity。"""
    index = _SEVERITY_ORDER.index(severity) if severity in _SEVERITY_ORDER else 0
    return _SEVERITY_ORDER[max(0, index - 1)]


def _naive(value: datetime | None) -> datetime | None:
    """把带时区的时间转换成设备本地朴素时间，便于与 hilog 时间戳比较。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone().replace(tzinfo=None)


def pidof_marker(bundle_name: str, pids: list[str]) -> str:
    """构造 pidof 归属标记行；解析时用于把无 bundle 文本的行归给被测应用。"""
    cleaned = [pid.strip() for pid in pids if pid.strip().isdigit()]
    if not bundle_name or not cleaned:
        return ""
    return f"# pidof {bundle_name}: {' '.join(cleaned[:5])}\n"


def _marker_pids(text: str, bundle_name: str) -> set[str]:
    """从文本头部的 pidof 标记行解析被测应用的 pid 集合。"""
    pids: set[str] = set()
    for line in text.splitlines()[:20]:
        match = PID_MARKER_RE.match(line.strip())
        if not match:
            continue
        if bundle_name and match.group(1) != bundle_name:
            continue
        pids.update(part for part in match.group(2).split() if part.isdigit())
    return pids


def _line_pid(line: str) -> str:
    """取 hilog 行的时间戳之后的第一个整数（pid 字段）；不可解析时返回空串。"""
    match = HILOG_TIMESTAMP_RE.match(line)
    if not match:
        return ""
    rest = line[match.end() :].strip()
    token = rest.split(" ", 1)[0].strip()
    return token if token.isdigit() else ""


def _parse_line_time(line: str, year: int) -> datetime | None:
    """解析 ``MM-DD HH:MM:SS.mmm`` 前缀；不可解析时返回 None。"""
    match = HILOG_TIMESTAMP_RE.match(line)
    if not match:
        return None
    month, day, hour, minute, second, millis = match.groups()
    try:
        return datetime(
            year,
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second),
            int(millis.ljust(3, "0")[:3]) * 1000,
        )
    except ValueError:
        return None


def _attribute(line: str, bundle_name: str, pids: set[str]) -> bool:
    """判定该行是否归属被测应用。"""
    if bundle_name and bundle_name in line:
        return True
    if pids:
        return _line_pid(line) in pids
    return False


def parse_hilog(
    text: str,
    *,
    bundle_name: str = "",
    window: tuple[datetime, datetime] | None = None,
) -> list[AnomalyFinding]:
    """逐行扫描 hilog 文本，返回可归属 / 已降级的崩溃与卡死发现。

    ``window`` 给出运行时间窗时按行内时间戳过滤；时间戳不可解析的行保留，但在
    ``evidence["time_filtered"]`` 标 False。去重键为 ``(kind, 匹配行前 200 字符)``。
    """
    if not text:
        return []
    start = _naive(window[0]) if window else None
    end = _naive(window[1]) if window else None
    year = (start or datetime.now()).year
    pids = _marker_pids(text, bundle_name) if bundle_name else set()

    findings: list[AnomalyFinding] = []
    grouped: dict[tuple[AnomalyKind, str], AnomalyFinding] = {}

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line or PID_MARKER_RE.match(line.strip()):
            continue
        moment = _parse_line_time(line, year)
        time_filtered = moment is not None
        if window is not None and moment is not None:
            if start is not None and moment < start:
                continue
            if end is not None and moment > end:
                continue
        attributed = _attribute(line, bundle_name, pids)
        signature = line.strip()[:SIGNATURE_CHARS]
        for kind, severity, pattern in CRASH_PATTERNS:
            if not pattern.search(line):
                continue
            applied = severity if attributed else _downgrade(severity)
            key = (kind, signature)
            finding = grouped.get(key)
            if finding is None:
                finding = AnomalyFinding(
                    kind=kind,
                    severity=applied,
                    summary_zh=SUMMARY_ZH[kind],
                    detail=signature,
                    source="hilog",
                    evidence={
                        "matched_line": signature,
                        "lines": [],
                        "count": 0,
                        "attribution": "app" if attributed else "system",
                        "time_filtered": bool(window is not None and time_filtered),
                        "bundle_name": bundle_name,
                    },
                )
                grouped[key] = finding
                findings.append(finding)
            evidence = finding.evidence
            evidence["count"] = int(evidence.get("count", 0)) + 1
            lines = evidence["lines"]
            if len(lines) < MAX_EVIDENCE_LINES:
                lines.append(line.strip()[:RAW_LINE_CHARS])
            if _SEVERITY_ORDER.index(applied) > _SEVERITY_ORDER.index(finding.severity):
                finding.severity = applied
            if not attributed:
                evidence["attribution"] = "system"
            if window is not None and not time_filtered:
                evidence["time_filtered"] = False
    return findings


def _faultlog_listing(device: DeviceAdapter) -> str:
    """列出远端 faultlog 目录；调用失败时返回空串。"""
    result = device._run("shell", "ls", FAULTLOG_DIR)
    return f"{getattr(result, 'stdout', '')}\n{getattr(result, 'stderr', '')}"


def _select_faultlog_names(listing: str, bundle_name: str, since: datetime | None) -> list[tuple[str, datetime]]:
    """按文件名解析 crash 类型与内嵌日期，过滤 bundle 与时间不匹配的条目。"""
    pattern = re.compile(rf"((?:cppcrash|jscrash|appfreeze)-{re.escape(bundle_name)}-\d{{8}}\S*)")
    threshold = _naive(since)
    selected: list[tuple[str, datetime]] = []
    seen: set[str] = set()
    for line in listing.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        name = match.group(1)
        if name in seen:
            continue
        date_match = FAULTLOG_DATE_RE.search(name)
        if not date_match:
            continue
        time_match = FAULTLOG_NAME_TIME_RE.search(name)
        try:
            if time_match:
                embedded = datetime.strptime(f"{time_match.group(1)}{time_match.group(2)}", "%Y%m%d%H%M%S")
            else:
                embedded = datetime.strptime(date_match.group(1), "%Y%m%d")
        except ValueError:
            continue
        if threshold is not None and embedded.date() < threshold.date():
            continue
        seen.add(name)
        selected.append((name, embedded))
    selected.sort(key=lambda item: item[1], reverse=True)
    return selected[:FAULTLOG_MAX_FILES]


def _faultlog_finding(name: str, embedded: datetime, head: str, saved: str | None) -> AnomalyFinding:
    """把单个 faultlog 文件的头部内容转成一条 finding。"""
    prefix = name.split("-", 1)[0]
    kind = FAULTLOG_KIND_BY_PREFIX.get(prefix, AnomalyKind.CPP_CRASH)
    reason_match = FAULTLOG_REASON_RE.search(head)
    process_match = FAULTLOG_PROCESS_RE.search(head)
    frame_match = FAULTLOG_FRAME_RE.search(head)
    reason = reason_match.group(1).strip() if reason_match else ""
    process = process_match.group(1).strip() if process_match else ""
    top_frame = frame_match.group(0).strip() if frame_match else ""
    detail = f"{name}: {reason or top_frame or 'faultlog 头部无可解析字段'}"
    return AnomalyFinding(
        kind=kind,
        severity="critical",
        summary_zh=f"faultlog 记录到 {kind.value}（{name}）",
        detail=detail[:RAW_LINE_CHARS],
        source="faultlog",
        evidence={
            "file": name,
            "date": embedded.strftime("%Y%m%d"),
            "reason": reason[:RAW_LINE_CHARS],
            "process": process,
            "top_frame": top_frame[:RAW_LINE_CHARS],
            "head": head[:HEAD_EXCERPT_CHARS],
            "saved": saved,
            "attribution": "app",
        },
    )


def fetch_faultlog_evidence(
    device: DeviceAdapter,
    bundle_name: str,
    since: datetime,
    output_dir: Path | None = None,
) -> list[AnomalyFinding]:
    """扫描 faultlog 目录并把命中文件的头部（最多 3 个）落到证据目录。

    ``output_dir`` 给定时每个命中文件写成 ``faultlog_<name>.txt``；任何设备或
    解析异常都只记录日志并返回已经拿到的部分结果。
    """
    findings: list[AnomalyFinding] = []
    if not bundle_name:
        return findings
    try:
        listing = _faultlog_listing(device)
    except Exception as exc:  # 设备异常不得影响分析结论
        logger.warning("faultlog listing failed: %s: %s", type(exc).__name__, exc)
        return findings
    candidates = _select_faultlog_names(listing, bundle_name, since)
    for name, embedded in candidates:
        head = ""
        try:
            result = device._run(
                "shell",
                "head",
                "-c",
                str(FAULTLOG_HEAD_BYTES),
                f"{FAULTLOG_DIR}/{name}",
            )
            head = getattr(result, "stdout", "") or ""
        except Exception as exc:
            logger.warning("faultlog head failed for %s: %s: %s", name, type(exc).__name__, exc)
        saved: str | None = None
        if output_dir is not None:
            path = Path(output_dir) / f"faultlog_{name}.txt"
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(head, encoding="utf-8")
                saved = path.name
            except OSError as exc:
                logger.warning("cannot persist faultlog evidence %s: %s", path, exc)
        findings.append(_faultlog_finding(name, embedded, head, saved))
    return findings


def collect_faultlog_index(device: DeviceAdapter, bundle_name: str, since: datetime) -> list[AnomalyFinding]:
    """列出并解析 faultlog 目录（不落盘），返回崩溃 / 冻屏发现。

    需要把命中文件的头部归档到 attempt 目录时用 :func:`fetch_faultlog_evidence`。
    """
    return fetch_faultlog_evidence(device, bundle_name, since, output_dir=None)


__all__ = [
    "CRASH_PATTERNS",
    "FAULTLOG_DIR",
    "FAULTLOG_HEAD_BYTES",
    "FAULTLOG_MAX_FILES",
    "collect_faultlog_index",
    "fetch_faultlog_evidence",
    "parse_hilog",
    "pidof_marker",
]
