"""执行结果分析（Phase 7 / 计划 D2）：hilog、截图、布局与压测指标。

分析只产出建议性的 :class:`~harmony_test_agent.models.ExecutionAnalysis`，
**永不翻转** 用例 / 回放的 ``passed``。

``defects`` 只在这里导出**纯函数与模型**（``DefectRecord`` / ``DefectStatus`` /
``DefectRecorder`` / ``defect_to_bug_repro_request``）。仓库实现
（``storage.defect_repository``）刻意不在这里导出：它反向 import 本包，在
``__init__`` 里再导出会形成易碎的循环。
"""

from __future__ import annotations

from .defects import (
    ANOMALY_TO_SYMPTOM,
    DefectRecord,
    DefectRecorder,
    DefectStatus,
    DefectSummary,
    defect_to_bug_repro_request,
    record_from_finding,
)
from .hilog import collect_faultlog_index, fetch_faultlog_evidence, parse_hilog, pidof_marker
from .in_run import InRunDetector, InRunThresholds, probe_crash_after_foreground_loss
from .layout import detect_layout_anomalies
from .screen import ScreenThresholds, detect_blank_screen, detect_identical_frames, screen_metrics
from .service import ExecutionAnalyzer

__all__ = [
    "ANOMALY_TO_SYMPTOM",
    "DefectRecord",
    "DefectRecorder",
    "DefectStatus",
    "DefectSummary",
    "ExecutionAnalyzer",
    "InRunDetector",
    "InRunThresholds",
    "ScreenThresholds",
    "collect_faultlog_index",
    "defect_to_bug_repro_request",
    "detect_blank_screen",
    "detect_identical_frames",
    "detect_layout_anomalies",
    "fetch_faultlog_evidence",
    "parse_hilog",
    "pidof_marker",
    "probe_crash_after_foreground_loss",
    "record_from_finding",
    "screen_metrics",
]
