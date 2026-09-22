"""运行中即时异常检测：**廉价门 → 昂贵取证**（计划设计 1，G2/G7）。

设计红线
--------

- **常态路径零额外设备调用**（G7）：每个变更类动作后先做两次纯本地计算
  （全图摘要比较 + 结构指纹比较）；两者都不命中即返回 ``None``，一次设备调用都不发。
- **用结构指纹而非全图 SHA256 做主判据**：状态栏时钟跳一秒 / 光标 blink 一下就会让
  全图摘要变化，历史守卫因此几乎永不触发。结构指纹（``page_path`` + 元素数 + 稳定文本集）
  天然抗这类抖动。
- **单次无视觉变化是「信号」不是「判决」**：合法的无视觉变化动作存在（勾选 checkbox
  而文本不变），因此首次只记 ``warning`` 并挂到 action，不中止、不失败；同一 run 内累计
  ``escalate_count`` 个**不同动作**命中结构停滞才升 ``critical``。
- 打开应用的崩溃与「点了个跳到别的应用的链接」在现象上相同，必须靠日志区分：前台丢失时
  立即采 hilog 尾部 + faultlog 索引，命中崩溃模式报具体崩溃类型，否则报
  ``PAGE_UNRESPONSIVE(critical, 前台应用丢失)``。
- 分析永远是 advisory，永不翻转用例的 ``passed``（与 ``analysis/service.py`` 同一条红线）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..models import (
    AnomalyFinding,
    AnomalyKind,
    InRunObservation,
    ScreenSnapshot,
    ToolName,
    utc_now,
)
from ..perception.normalizer import normalize_layout, page_path
from .hilog import collect_faultlog_index, parse_hilog
from .layout import detect_layout_anomalies
from .screen import DEFAULT_SCREEN_THRESHOLDS, ScreenThresholds, detect_blank_screen

if TYPE_CHECKING:
    from ..analysis.service import ExecutionAnalyzer
    from ..devices.base import DeviceAdapter

logger = logging.getLogger(__name__)

#: 会改变界面状态的工具；只有这些动作之后才值得做「有没有变化」的判定。
MUTATING_TOOLS: frozenset[ToolName] = frozenset(
    {
        ToolName.OPEN_APP,
        ToolName.CLICK_ELEMENT,
        ToolName.CLICK_COORDINATE,
        ToolName.INPUT_TEXT,
        ToolName.SWIPE,
        ToolName.BACK,
    }
)

CRASH_KINDS: frozenset[AnomalyKind] = frozenset(
    {AnomalyKind.CPP_CRASH, AnomalyKind.JS_CRASH, AnomalyKind.APP_FREEZE, AnomalyKind.ANR}
)

FOREGROUND_LOST_SUMMARY = "动作后前台应用丢失，疑似崩溃"
STALL_SUMMARY = "点击后页面结构无变化，疑似无响应控件"


@dataclass(frozen=True)
class InRunThresholds:
    """运行中检测阈值（全部可调，默认保守）。"""

    escalate_count: int = 2
    """累计 N 个**不同动作**命中结构停滞后升为 critical。"""

    probe_enabled: bool = True
    """昂贵取证（前台检查 + hilog + faultlog）总开关。关闭后只做本地廉价判定。"""

    hilog_tail_bytes: int = 65_536
    screen_scan_enabled: bool = True
    """每步对 after 帧做白屏 / 布局本地扫描（纯本地计算，无设备调用）。"""


@dataclass
class InRunObservationState:
    """一次观测的内部累计状态（只用于升级判定与 metrics）。"""

    stall_actions: set[str] = field(default_factory=set)


def stability_fingerprint(page: str, elements: list[Any]) -> tuple[str, int, tuple[str, ...]]:
    """页面结构指纹：**严格复用** ``BoundedExplorer._stability_fingerprint`` 的语义。

    刻意委托而不是本地重写：「稳定文本」的判定包含加载占位、时间样式与纯数字/标点三类
    易变文本过滤（``_is_volatile_text``），本地重写极易漏掉其中一条，从而把状态栏时钟
    当成结构变化 —— 那正是这个模块要修的 bug。探索栈不可用时退化为等价语义的本地实现
    （与 ``analysis/service.py::_structure_fingerprint`` 同一成例）。
    """
    try:
        from ..discovery.explorer import BoundedExplorer

        return BoundedExplorer._stability_fingerprint(page, elements)
    except Exception as exc:  # noqa: BLE001 - 探索栈不可用时退化为等价语义
        logger.debug("stability fingerprint fallback: %s: %s", type(exc).__name__, exc)
        texts = tuple(sorted(element.content.strip() for element in elements if getattr(element, "content", "")))
        return (page, len(elements), texts)


def snapshot_fingerprint(snapshot: ScreenSnapshot) -> tuple[str, int, tuple[str, ...]]:
    """对 :class:`ScreenSnapshot` 取结构指纹。"""
    return stability_fingerprint(snapshot.page_path, snapshot.elements)


def fingerprint_from_hierarchy(hierarchy: Any, *, width: int, height: int) -> tuple[str, int, tuple[str, ...]] | None:
    """从原始 ArkUI dump 取结构指纹；不可解析时返回 ``None``。"""
    if not isinstance(hierarchy, dict) or not hierarchy:
        return None
    try:
        return stability_fingerprint(page_path(hierarchy), normalize_layout(hierarchy, width, height))
    except Exception as exc:  # noqa: BLE001 - 指纹失败不得影响检测流程
        logger.debug("hierarchy fingerprint failed: %s: %s", type(exc).__name__, exc)
        return None


def probe_crash_after_foreground_loss(
    device: DeviceAdapter,
    bundle_name: str,
    log_path: Path,
    *,
    since: datetime | None = None,
    tail_bytes: int = 65_536,
) -> list[AnomalyFinding]:
    """前台丢失 / 跨应用跳转之后的崩溃探测（探索期与运行期共用）。

    采一次 hilog 尾部并解析；再扫一次 faultlog 索引。命中崩溃模式返回对应 finding
    （critical），全部干净则返回空列表 —— 调用方据此区分「崩回桌面」与「点了外链」。
    任何设备 / 解析异常都只记日志并返回空列表，绝不抛出。
    """
    findings: list[AnomalyFinding] = []
    if not bundle_name:
        return findings
    header = ""
    try:
        pids = _pidof(device, bundle_name)
        if pids:
            from .hilog import pidof_marker

            header = pidof_marker(bundle_name, pids)
    except Exception as exc:  # noqa: BLE001
        logger.debug("pidof for crash probe failed: %s: %s", type(exc).__name__, exc)
    try:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        device.collect_logs(Path(log_path))
        text = Path(log_path).read_text(encoding="utf-8", errors="replace") if Path(log_path).is_file() else ""
    except Exception as exc:  # noqa: BLE001 - 设备采集失败只降级
        logger.warning("crash probe log collection failed: %s: %s", type(exc).__name__, exc)
        text = ""
    if text:
        if len(text) > tail_bytes:
            # 只解析尾部：崩溃痕迹总在最后
            text = text[-tail_bytes:]
        window = (since, utc_now()) if since is not None else None
        findings.extend(parse_hilog(f"{header}{text}", bundle_name=bundle_name, window=window))
    try:
        findings.extend(collect_faultlog_index(device, bundle_name, since or _default_since()))
    except Exception as exc:  # noqa: BLE001 - faultlog 扫描失败只降级
        logger.debug("crash probe faultlog scan failed: %s: %s", type(exc).__name__, exc)
    return _dedupe(findings)


def _pidof(device: Any, bundle_name: str) -> list[str]:
    """尽力解析被测应用 pid（缺失时返回空列表）。"""
    import re

    runner = getattr(device, "_run", None)
    if runner is None:
        return []
    result = runner("shell", "pidof", bundle_name)
    return re.findall(r"\d+", str(getattr(result, "stdout", "") or ""))[:5]


def _default_since() -> datetime:
    """没有起始时间时用「最近 30 分钟」作为 faultlog 索引的兜底时间窗。"""
    from datetime import timedelta

    return utc_now() - timedelta(minutes=30)


def _dedupe(findings: list[AnomalyFinding]) -> list[AnomalyFinding]:
    """按 ``(kind, summary_zh, detail 前 200 字)`` 去重。"""
    seen: set[tuple[AnomalyKind, str, str]] = set()
    unique: list[AnomalyFinding] = []
    for finding in findings:
        key = (finding.kind, finding.summary_zh, finding.detail[:200])
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique


class InRunDetector:
    """运行中即时异常检测：廉价门（本地计算）→ 昂贵取证（设备调用）。"""

    def __init__(
        self,
        device: DeviceAdapter,
        *,
        bundle_name: str,
        thresholds: InRunThresholds | None = None,
        analyzer: ExecutionAnalyzer | None = None,
        run_dir: Path | None = None,
        screen_thresholds: ScreenThresholds = DEFAULT_SCREEN_THRESHOLDS,
    ) -> None:
        self.device = device
        self.bundle_name = bundle_name
        self.thresholds = thresholds or InRunThresholds()
        self.analyzer = analyzer
        self.run_dir = Path(run_dir) if run_dir is not None else None
        self.screen_thresholds = screen_thresholds
        self.state = InRunObservationState()
        self.metrics: dict[str, Any] = {"observations": 0, "suspicious": 0, "device_calls": 0}

    # ------------------------------------------------------------------ 主入口

    def observe(
        self,
        *,
        action_id: str,
        before: ScreenSnapshot,
        after: ScreenSnapshot,
        tool: ToolName,
        target: str = "",
    ) -> InRunObservation | None:
        """判定一个变更类动作后是否出现异常。

        返回 ``None`` 表示「不值得关注」（Pareto 大多数情况，**零设备调用**）。
        返回 :class:`InRunObservation` 时 ``finding`` 可能仍为 ``None``
        （有信号但不构成异常）。
        """
        try:
            return self._observe(action_id=action_id, before=before, after=after, tool=tool, target=target)
        except Exception as exc:  # noqa: BLE001 - 检测是 advisory，任何异常都不得打断任务
            logger.warning("in-run detection failed for %s: %s: %s", action_id, type(exc).__name__, exc)
            return None

    def _observe(
        self,
        *,
        action_id: str,
        before: ScreenSnapshot,
        after: ScreenSnapshot,
        tool: ToolName,
        target: str,
    ) -> InRunObservation | None:
        self.metrics["observations"] = int(self.metrics["observations"]) + 1
        pixel_same = bool(before.image_sha256) and before.image_sha256 == after.image_sha256
        struct_same = snapshot_fingerprint(before) == snapshot_fingerprint(after)
        local_findings: list[AnomalyFinding] = []
        evidence_paths: list[str] = []
        if self.thresholds.screen_scan_enabled:
            local_findings, evidence_paths = self._local_scan(after, action_id)

        suspicious = pixel_same or struct_same
        if not suspicious:
            # 常见路径：画面与结构都变了 ⇒ 零设备调用（G7 的机器证明点）
            if not local_findings:
                return None
            return self._observation(
                action_id=action_id,
                pixel_same=pixel_same,
                struct_same=struct_same,
                finding=self._primary(local_findings),
                findings=local_findings,
                evidence_paths=evidence_paths,
            )

        self.metrics["suspicious"] = int(self.metrics["suspicious"]) + 1
        foreground_lost = False
        foreground_bundle = ""
        findings = list(local_findings)

        if self.thresholds.probe_enabled:
            foreground_bundle, foreground_lost = self._foreground_state()
            if foreground_lost:
                crash_findings, crash_paths = self._probe_crash(action_id)
                evidence_paths.extend(crash_paths)
                if crash_findings:
                    findings.extend(crash_findings)
                else:
                    findings.append(
                        self._finding(
                            AnomalyKind.PAGE_UNRESPONSIVE,
                            "critical",
                            FOREGROUND_LOST_SUMMARY,
                            source="screenshot",
                            action_id=action_id,
                            after=after,
                            target=target,
                            tool=tool,
                            detail=(
                                f"动作后前台应用为 {foreground_bundle or 'unknown'}，"
                                f"期望 {self.bundle_name}；hilog / faultlog 未命中崩溃模式"
                            ),
                            extra={"foreground_lost": True, "foreground_bundle": foreground_bundle},
                        )
                    )

        if not foreground_lost and struct_same:
            findings.append(self._stall_finding(action_id=action_id, after=after, tool=tool, target=target))

        if not findings:
            # 仅像素相同、结构已变（时钟 / 光标 / 动画）⇒ 只记 metrics，不产 finding
            return InRunObservation(
                action_id=action_id,
                pixel_same=pixel_same,
                struct_same=struct_same,
                foreground_lost=foreground_lost,
                foreground_bundle=foreground_bundle,
                finding=None,
                evidence_paths=evidence_paths,
                metrics=self._observation_metrics(pixel_same, struct_same),
            )

        findings = _dedupe(findings)
        return self._observation(
            action_id=action_id,
            pixel_same=pixel_same,
            struct_same=struct_same,
            foreground_lost=foreground_lost,
            foreground_bundle=foreground_bundle,
            finding=self._primary(findings),
            findings=findings,
            evidence_paths=evidence_paths,
        )

    # ------------------------------------------------------------------ 分支

    def _stall_finding(self, *, action_id: str, after: ScreenSnapshot, tool: ToolName, target: str) -> AnomalyFinding:
        """结构停滞：首次 warning，累计到 ``escalate_count`` 升 critical。"""
        self.state.stall_actions.add(action_id)
        escalated = len(self.state.stall_actions) >= max(1, self.thresholds.escalate_count)
        severity = "critical" if escalated else "warning"
        summary = "页面无响应：多个动作后页面结构持续无变化" if escalated else STALL_SUMMARY
        return self._finding(
            AnomalyKind.PAGE_UNRESPONSIVE,
            severity,
            summary,
            source="screenshot",
            action_id=action_id,
            after=after,
            target=target,
            tool=tool,
            detail=(
                f"动作 {action_id}（{tool}）后页面结构指纹与动作前完全相同"
                f"（累计命中动作数 {len(self.state.stall_actions)}，"
                f"阈值 {self.thresholds.escalate_count}）"
            ),
            extra={
                "struct_same": True,
                "stall_actions": sorted(self.state.stall_actions),
                "escalated": escalated,
            },
        )

    def _local_scan(self, after: ScreenSnapshot, action_id: str) -> tuple[list[AnomalyFinding], list[str]]:
        """每步的纯本地扫描：白屏（PIL 缩略图）+ 布局异常（已有 dump 解析）。"""
        findings: list[AnomalyFinding] = []
        evidence: list[str] = []
        image = getattr(after, "image_path", None)
        if image is not None and Path(image).is_file():
            try:
                blank = detect_blank_screen(Path(image), self.screen_thresholds)
            except Exception as exc:  # noqa: BLE001 - 单帧扫描失败只降级
                logger.debug("blank screen scan failed: %s: %s", type(exc).__name__, exc)
                blank = None
            if blank is not None:
                blank.action_id = action_id
                blank.page_path = after.page_path
                blank.phase = "in_run"
                blank.detected_at = utc_now()
                blank.evidence["screenshot"] = self._relative(image)
                findings.append(blank)
                evidence.append(self._relative(image))
        hierarchy_path = getattr(after, "hierarchy_path", None)
        if hierarchy_path is not None and Path(hierarchy_path).is_file():
            try:
                import json

                hierarchy = json.loads(Path(hierarchy_path).read_text(encoding="utf-8-sig"))
                layout_findings = detect_layout_anomalies(hierarchy, after.width, after.height)
            except Exception as exc:  # noqa: BLE001 - 布局解析失败只降级
                logger.debug("layout scan failed: %s: %s", type(exc).__name__, exc)
                layout_findings = []
            for finding in layout_findings:
                finding.action_id = action_id
                finding.page_path = after.page_path
                finding.phase = "in_run"
                finding.detected_at = utc_now()
                finding.evidence["layout_relative"] = self._relative(hierarchy_path)
                findings.append(finding)
                evidence.append(self._relative(hierarchy_path))
        return findings, [path for path in evidence if path]

    def _foreground_state(self) -> tuple[str, bool]:
        """查前台应用；``(bundle, 是否丢失)``。异常时保守地当作「未丢失」。"""
        self.metrics["device_calls"] = int(self.metrics["device_calls"]) + 1
        try:
            foreground = self.device.current_foreground_app()
        except Exception as exc:  # noqa: BLE001 - 前台查询失败不得误报崩溃
            logger.debug("foreground query failed: %s: %s", type(exc).__name__, exc)
            return "", False
        bundle = str(getattr(foreground, "bundle_name", "") or "")
        if not bundle:
            return "", True
        if self.bundle_name and bundle != self.bundle_name:
            return bundle, True
        return bundle, False

    def _probe_crash(self, action_id: str) -> tuple[list[AnomalyFinding], list[str]]:
        """昂贵取证：hilog 尾部 + faultlog 索引。"""
        log_path = self._evidence_path(f"hilog_inrun_{_safe_id(action_id)}.txt")
        findings = probe_crash_after_foreground_loss(
            self.device,
            self.bundle_name,
            log_path,
            since=None,
            tail_bytes=self.thresholds.hilog_tail_bytes,
        )
        self.metrics["device_calls"] = int(self.metrics["device_calls"]) + 1
        relative = self._relative(log_path)
        for finding in findings:
            finding.action_id = action_id
            finding.phase = "in_run"
            finding.detected_at = utc_now()
            if relative:
                finding.evidence["hilog_relative"] = relative
        return findings, ([relative] if relative else [])

    # ------------------------------------------------------------------ 辅助

    def _observation(
        self,
        *,
        action_id: str,
        pixel_same: bool,
        struct_same: bool,
        finding: AnomalyFinding | None,
        findings: list[AnomalyFinding],
        evidence_paths: list[str],
        foreground_lost: bool = False,
        foreground_bundle: str = "",
    ) -> InRunObservation:
        if finding is not None:
            finding.phase = "in_run"
            finding.action_id = finding.action_id or action_id
        return InRunObservation(
            action_id=action_id,
            pixel_same=pixel_same,
            struct_same=struct_same,
            foreground_lost=foreground_lost,
            foreground_bundle=foreground_bundle,
            finding=finding,
            evidence_paths=evidence_paths,
            metrics=self._observation_metrics(pixel_same, struct_same, extra={"findings": len(findings)}),
        )

    def _observation_metrics(self, pixel_same: bool, struct_same: bool, extra: dict | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "pixel_same": pixel_same,
            "struct_same": struct_same,
            "stall_actions": len(self.state.stall_actions),
        }
        payload.update(extra or {})
        return payload

    def _finding(
        self,
        kind: AnomalyKind,
        severity: str,
        summary: str,
        *,
        source: str,
        action_id: str,
        after: ScreenSnapshot,
        target: str,
        tool: ToolName,
        detail: str,
        extra: dict[str, Any] | None = None,
    ) -> AnomalyFinding:
        evidence: dict[str, Any] = {
            "action_id": action_id,
            "tool": str(tool),
            "target": target,
            "page_path": after.page_path,
            "bundle_name": self.bundle_name,
            "detected_by": "in_run",
        }
        screenshot = self._relative(getattr(after, "image_path", None))
        if screenshot:
            evidence["screenshot"] = screenshot
        evidence.update(extra or {})
        return AnomalyFinding(
            kind=kind,
            severity=severity,  # type: ignore[arg-type]
            summary_zh=summary,
            detail=detail,
            source=source,  # type: ignore[arg-type]
            action_id=action_id,
            page_path=after.page_path,
            screenshot=screenshot,
            detected_at=utc_now(),
            phase="in_run",
            evidence=evidence,
        )

    @staticmethod
    def _primary(findings: list[AnomalyFinding]) -> AnomalyFinding:
        """从多条 finding 里挑最严重的一条作为 action 的主异常。"""
        order = {"info": 0, "warning": 1, "critical": 2}
        return max(findings, key=lambda item: order.get(item.severity, 0))

    def _evidence_path(self, name: str) -> Path:
        base = self.run_dir if self.run_dir is not None else Path(".")
        return Path(base) / name

    def _relative(self, path: Any) -> str:
        if path is None:
            return ""
        candidate = Path(path)
        if self.run_dir is None:
            return candidate.as_posix()
        try:
            return candidate.resolve().relative_to(Path(self.run_dir).resolve()).as_posix()
        except ValueError, OSError:
            return candidate.as_posix()


def _safe_id(value: str) -> str:
    """把 action_id 变成安全的文件名片段。"""
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in value)[:60] or "action"


__all__ = [
    "CRASH_KINDS",
    "FOREGROUND_LOST_SUMMARY",
    "MUTATING_TOOLS",
    "STALL_SUMMARY",
    "InRunDetector",
    "InRunObservationState",
    "InRunThresholds",
    "fingerprint_from_hierarchy",
    "probe_crash_after_foreground_loss",
    "snapshot_fingerprint",
    "stability_fingerprint",
]
