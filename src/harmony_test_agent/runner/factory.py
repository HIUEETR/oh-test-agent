"""统一的 :class:`~harmony_test_agent.runner.hypium.HypiumRunner` 构造点。

**为什么需要这个模块**（缺口 1）：``HypiumRunner`` 早就支持 ``analysis_hook``，但历史
上有 10 处各自 ``HypiumRunner(settings.resolved_runtime_home)``，无一传 hook —— 挂钩成了
死代码，"能识别异常" 在实际运行中大部分时候等于 "识别不到"。

hook 的签名是 ``Callable[[ReplayResult, Path], ExecutionAnalysis | None]``，第二个参数是
attempt 证据目录。``run_dir`` 可以从 ``attempt_dir.parent.parent`` 反推（与
``runner/hypium.py`` 里 ``run_dir = python_path.parent.parent`` 一致），但
**``bundle_name`` / ``device_id`` 无法从路径反推**，必须由调用方提供：见
:func:`make_analysis_hook` 的 ``bundle_resolver`` / ``device_resolver`` 参数。

调用方约定：

- 接入点已持有身份（Profile / trace / DC 会话）时，直接传 ``bundle_name="..."``；
- 身份需要延迟读取（脚本执行时才知道）时，传 ``bundle_resolver=lambda: ...``；
- ``analyze=False`` 表示「分析已在别处完成」，用于避免同一 attempt 被分析两次；
- 未注入 analyzer 时本模块**自行构造**一个（与 ``api/app.py`` / ``cli.py`` 同口径），
  单测通过 ``analyze=False`` 或不打开开关来保持历史行为。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .hypium import HypiumRunner

if TYPE_CHECKING:
    from ..analysis.service import ExecutionAnalyzer
    from ..config import Settings
    from ..models import ExecutionAnalysis, ReplayResult

logger = logging.getLogger(__name__)

__all__ = ["build_execution_analyzer", "make_analysis_hook", "make_hypium_runner"]


def build_execution_analyzer(settings: Settings) -> ExecutionAnalyzer | None:
    """构造执行结果分析器；开关关闭或依赖缺失时返回 ``None``（功能降级，不阻塞）。

    与历史 ``api/app.py::_execution_analyzer`` / ``cli.py::_execution_analyzer`` 同口径：
    任何构造异常都只降级为「不分析」。
    """
    if not getattr(settings, "case_analysis_enabled", True):
        return None
    try:
        from ..analysis.service import ExecutionAnalyzer
        from ..devices import HarmonyDeviceAdapter

        return ExecutionAnalyzer(
            device_factory=lambda device_id: HarmonyDeviceAdapter(
                device_id, settings.hdc_path, settings.agent_action_timeout
            ),
            collect_logs_on_success=bool(getattr(settings, "analysis_collect_logs_on_success", False)),
        )
    except Exception as exc:  # noqa: BLE001 - 分析能力缺失只降级为「不分析」
        logger.warning("execution analyzer unavailable: %s: %s", type(exc).__name__, exc)
        return None


def _resolve(value: str | None, resolver: Callable[[], str] | None) -> str:
    """先取显式字符串，其次调用延迟解析器；都没有则返回空串。"""
    if value:
        return value
    if resolver is None:
        return ""
    try:
        return str(resolver() or "")
    except Exception as exc:  # 身份解析失败不得影响回放本身
        logger.warning("analysis hook identity resolver failed: %s: %s", type(exc).__name__, exc)
        return ""


def make_analysis_hook(
    analyzer: ExecutionAnalyzer | None,
    *,
    bundle_name: str | None = None,
    device_id: str | None = None,
    bundle_resolver: Callable[[], str] | None = None,
    device_resolver: Callable[[], str] | None = None,
    symptom_kind: str | None = None,
    session_id: str | None = None,
    subject: str = "hypium_replay",
) -> Callable[[ReplayResult, Path], ExecutionAnalysis | None] | None:
    """构造回放结束时的分析挂钩；``analyzer`` 为空时返回 ``None``。

    ``run_dir`` 由 ``attempt_dir.parent.parent`` 反推。``subject="dc_script"`` 时走
    :meth:`ExecutionAnalyzer.analyze_dc_script`（DC 录制脚本没有 trace 上下文，
    ``need_logs`` 判定独立）。
    """
    if analyzer is None:
        return None

    def hook(replay: ReplayResult, attempt_dir: Path) -> ExecutionAnalysis | None:
        run_dir = Path(attempt_dir).resolve().parent.parent
        resolved_bundle = _resolve(bundle_name, bundle_resolver)
        resolved_device = _resolve(device_id, device_resolver)
        if subject == "dc_script":
            return analyzer.analyze_dc_script(
                replay,
                run_dir=run_dir,
                bundle_name=resolved_bundle,
                device_id=resolved_device,
                session_id=session_id or run_dir.name,
                symptom_kind=symptom_kind,
            )
        return analyzer.analyze_replay(
            replay,
            run_dir=run_dir,
            bundle_name=resolved_bundle,
            device_id=resolved_device,
            symptom_kind=symptom_kind,
        )

    return hook


def make_hypium_runner(
    settings: Settings,
    *,
    analyzer: Any | None = None,
    timeout: float = 300,
    bundle_name: str | None = None,
    device_id: str | None = None,
    bundle_resolver: Callable[[], str] | None = None,
    device_resolver: Callable[[], str] | None = None,
    symptom_kind: str | None = None,
    session_id: str | None = None,
    subject: str = "hypium_replay",
    analyze: bool = True,
) -> HypiumRunner:
    """统一构造带 ``analysis_hook`` 的 :class:`HypiumRunner`。

    ``analyzer`` 省略时按 ``settings`` 自行构造；显式传 ``analyzer=None`` +
    ``analyze=False`` 表示「分析已在别处完成」（例如用例库的 ``runner_factory``：
    ``cases/library.py`` 自己调用 ``analyze_replay``，两处都挂 hook 会重复分析）。
    """
    hook = None
    if analyze and getattr(settings, "case_analysis_enabled", True):
        resolved_analyzer = analyzer if analyzer is not None else build_execution_analyzer(settings)
        hook = make_analysis_hook(
            resolved_analyzer,
            bundle_name=bundle_name,
            device_id=device_id,
            bundle_resolver=bundle_resolver,
            device_resolver=device_resolver,
            symptom_kind=symptom_kind,
            session_id=session_id,
            subject=subject,
        )
    return HypiumRunner(settings.resolved_runtime_home, timeout, analysis_hook=hook)
