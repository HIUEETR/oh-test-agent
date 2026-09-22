"""DC ``collect_logs`` 的崩溃摘要（Phase 3.7）。

DC 没有读宿主文件的工具：日志正文模型永远看不到。历史实现只回
``ok: N log lines saved to X``，因此 DC agent 即使采到了 cppcrash 也无从得知。
本文件钉住「有命中就附摘要、无命中就与历史逐字一致」。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from defect_fixtures import BUNDLE, hilog_appfreeze, hilog_clean, hilog_cppcrash

from harmony_test_agent.dc.models import DcEventType, DcToolTier
from harmony_test_agent.dc.tools import (
    MAX_CRASH_LINES,
    DcActionRecorder,
    DcSnapshotHolder,
    DcToolContext,
    tool_collect_logs,
)
from harmony_test_agent.models import CommandResult


class _Device:
    def __init__(self, hilog: str) -> None:
        self.hilog = hilog
        self.calls = 0

    def collect_logs(self, output_path: Path) -> CommandResult:
        self.calls += 1
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(self.hilog, encoding="utf-8")
        return CommandResult(command="hilog", returncode=0, stdout=self.hilog)

    def _run(self, *args: str, **kwargs) -> CommandResult:
        return CommandResult(command="hdc", args=list(args), returncode=0)


class _Artifacts:
    def __init__(self, root: Path) -> None:
        self.root = root

    def run_dir(self, run_id: str) -> Path:
        path = self.root / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path


def _harness(tmp_path: Path, hilog: str, *, bundle_name: str = BUNDLE):
    from pydantic_ai import RunContext
    from pydantic_ai.usage import RunUsage

    device = _Device(hilog)
    deps = DcToolContext(
        session_id="dc-logs",
        device=device,  # type: ignore[arg-type]
        hdc=None,  # type: ignore[arg-type]
        safety=None,  # type: ignore[arg-type]
        recorder=DcActionRecorder(),
        artifacts=_Artifacts(tmp_path / "runs"),
        session_dir=tmp_path,
        snapshot_holder=DcSnapshotHolder(),
        tier=DcToolTier.L2,
        bundle_name=bundle_name,
    )
    return RunContext(deps=deps, model=None, usage=RunUsage()), device  # type: ignore[arg-type]


class TestCrashSummary:
    def test_cppcrash_fixture_yields_a_suspected_crash_line(self, tmp_path: Path) -> None:
        ctx, device = _harness(tmp_path, hilog_cppcrash())

        result = asyncio.run(tool_collect_logs(ctx))

        assert result.startswith("ok: ")
        assert "SUSPECTED CRASH:" in result
        assert "cppcrash" in result
        assert device.calls == 1

    def test_appfreeze_fixture_is_reported(self, tmp_path: Path) -> None:
        ctx, _device = _harness(tmp_path, hilog_appfreeze())

        result = asyncio.run(tool_collect_logs(ctx))

        assert "SUSPECTED CRASH:" in result
        assert "appfreeze" in result or "anr" in result

    def test_clean_hilog_keeps_the_historic_string(self, tmp_path: Path) -> None:
        """无命中时返回字符串与历史实现逐字一致（保护既有测试与模型行为）。"""
        ctx, _device = _harness(tmp_path, hilog_clean())

        result = asyncio.run(tool_collect_logs(ctx))

        assert result.startswith("ok: ")
        assert result.count("\n") == 0
        assert "SUSPECTED CRASH" not in result

    def test_empty_log_file_keeps_the_historic_string(self, tmp_path: Path) -> None:
        ctx, _device = _harness(tmp_path, "")

        result = asyncio.run(tool_collect_logs(ctx))

        assert result == "ok: 0 log lines saved to " + result.split("saved to ", 1)[1]

    def test_summary_is_capped_to_avoid_context_bloat(self, tmp_path: Path) -> None:
        """最多 ``MAX_CRASH_LINES`` 条，每条截断 —— 避免挤占模型上下文（计划风险 4）。"""
        repeated = hilog_cppcrash() + hilog_appfreeze() + hilog_clean()
        ctx, _device = _harness(tmp_path, repeated * 5)

        result = asyncio.run(tool_collect_logs(ctx))

        assert result.count("SUSPECTED CRASH:") <= MAX_CRASH_LINES

    def test_bundle_mismatch_does_not_suppress_a_crash(self, tmp_path: Path) -> None:
        """归属不上时会降级 severity，但仍必须让模型看见（降级不等于隐藏）。"""
        ctx, _device = _harness(tmp_path, hilog_cppcrash(), bundle_name="com.other.app")

        result = asyncio.run(tool_collect_logs(ctx))

        assert "SUSPECTED CRASH:" in result

    def test_missing_log_file_is_tolerated(self, tmp_path: Path) -> None:
        """工具返回值绝不能在解析失败时变成异常。"""

        class Broken(_Device):
            def collect_logs(self, output_path: Path) -> CommandResult:
                # 声称成功但不写文件
                self.calls += 1
                return CommandResult(command="hilog", returncode=0, stdout="")

        from pydantic_ai import RunContext
        from pydantic_ai.usage import RunUsage

        device = Broken("")
        deps = DcToolContext(
            session_id="dc-logs",
            device=device,  # type: ignore[arg-type]
            hdc=None,  # type: ignore[arg-type]
            safety=None,  # type: ignore[arg-type]
            recorder=DcActionRecorder(),
            artifacts=_Artifacts(tmp_path / "runs"),
            session_dir=tmp_path,
            snapshot_holder=DcSnapshotHolder(),
            tier=DcToolTier.L2,
            bundle_name=BUNDLE,
        )
        ctx = RunContext(deps=deps, model=None, usage=RunUsage())  # type: ignore[arg-type]

        result = asyncio.run(tool_collect_logs(ctx))

        assert result.startswith("ok: ")
        assert "SUSPECTED CRASH" not in result


class TestAnomalyEvent:
    def test_collect_logs_emits_no_anomaly_event_by_itself(self, tmp_path: Path) -> None:
        """摘要只进返回值；``ANOMALY_DETECTED`` 由截图 ``changed`` 标志那条路径发出。"""
        ctx, _device = _harness(tmp_path, hilog_cppcrash())
        events: list[DcEventType] = []
        ctx.deps.recorder._emit_event = lambda event_type, *args, **kwargs: events.append(event_type)  # type: ignore[method-assign]

        asyncio.run(tool_collect_logs(ctx))

        assert DcEventType.ANOMALY_DETECTED not in events
