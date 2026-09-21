"""执行结果分析（Phase 7 / 计划 D2）：hilog、截图、布局与压测指标。

分析只产出建议性的 :class:`~harmony_test_agent.models.ExecutionAnalysis`，
**永不翻转** 用例 / 回放的 ``passed``。
"""

from __future__ import annotations

from .hilog import collect_faultlog_index, fetch_faultlog_evidence, parse_hilog, pidof_marker
from .layout import detect_layout_anomalies
from .screen import ScreenThresholds, detect_blank_screen, detect_identical_frames, screen_metrics
from .service import ExecutionAnalyzer

__all__ = [
    "ExecutionAnalyzer",
    "ScreenThresholds",
    "collect_faultlog_index",
    "detect_blank_screen",
    "detect_identical_frames",
    "detect_layout_anomalies",
    "fetch_faultlog_evidence",
    "parse_hilog",
    "pidof_marker",
    "screen_metrics",
]
