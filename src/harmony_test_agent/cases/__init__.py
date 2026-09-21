"""可复用 UI 自动化测试用例：IR、构建器、用例库与安全门禁。"""

from __future__ import annotations

from typing import Any

from .spec import (
    BugReproSpec,
    CaseExecutionRecord,
    CaseExecutionRequest,
    CaseProvenance,
    CaseRecord,
    CaseSummary,
    CheckpointKind,
    CheckpointSpec,
    DataSetSpec,
    LocatorEvidence,
    LocatorSpec,
    MatchMode,
    ScenarioKind,
    SetupSpec,
    StepAction,
    StressKind,
    StressSpec,
    TeardownSpec,
    TestCaseSpec,
    TestParamSpec,
    TestStepSpec,
)
from .titles import checkpoint_title_zh, step_title_zh

__all__ = [
    "BugReproSpec",
    "CaseExecutionRecord",
    "CaseExecutionRequest",
    "CaseProvenance",
    "CaseRecord",
    "CaseSummary",
    "CheckpointKind",
    "CheckpointSpec",
    "DataSetSpec",
    "LocatorEvidence",
    "LocatorSpec",
    "MatchMode",
    "ScenarioKind",
    "SetupSpec",
    "StepAction",
    "StressKind",
    "StressSpec",
    "TeardownSpec",
    "TestCaseSpec",
    "TestParamSpec",
    "TestStepSpec",
    "checkpoint_title_zh",
    "evaluate_confidence",
    "evaluate_runnable",
    "step_title_zh",
    "validate_case_spec",
]


def __getattr__(name: str) -> Any:
    """惰性导出 ``validate_case_spec`` / ``evaluate_runnable`` / ``evaluate_confidence``。

    ``cases.safety`` 依赖 ``runtime.safety``，而 ``runtime.events`` 又依赖
    ``storage``；若在本包初始化时就急加载 safety，``storage/__init__`` →
    ``cases.spec`` → ``cases`` → ``safety`` → ``runtime`` → ``storage`` 会形成
    循环导入（部分初始化的 storage 缺 ``RunRepository``）。``evaluate_*`` 同理走
    ``cases.builder``（它会拉入 ``..models``），因此也按需加载。
    """
    if name == "validate_case_spec":
        from .safety import validate_case_spec

        return validate_case_spec
    if name in {"evaluate_runnable", "evaluate_confidence"}:
        from . import builder

        return getattr(builder, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
