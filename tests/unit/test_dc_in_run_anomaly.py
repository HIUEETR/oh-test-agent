"""DC 侧运行中检测（Phase 2.5）：消费 ``changed`` 标志、回填 snapshot 链、``collect_logs`` 摘要。

对应缺口 5 的三个「声明了但从未被使用」的字段/返回值：

- ``DcToolInvocation.before_snapshot_id`` / ``after_snapshot_id``；
- ``DcSnapshotHolder.update_jpeg`` 的 ``changed`` 返回值；
- ``collect_logs`` 只回 ``ok: N log lines saved``，日志正文模型永远读不到。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from defect_fixtures import BUNDLE, FAULTLOG_CPPCRASH_HEAD, faultlog_index, hilog_clean, hilog_cppcrash

from harmony_test_agent.dc.models import DcEventType, DcToolName, DcToolTier
from harmony_test_agent.dc.tools import (
    DcActionRecorder,
    DcSnapshotHolder,
    DcToolContext,
    tool_click,
    tool_collect_logs,
    tool_screenshot,
)
from harmony_test_agent.models import BoundingBox, CommandResult, ScreenSnapshot, UIElement


class _Device:
    """可编排截图内容的设备替身；实现 DC 截图快路径。"""

    def __init__(self, *, frames: list[bytes] | None = None, hilog: str = "") -> None:
        self.frames = list(frames or [b"frame-a"])
        self.hilog = hilog
        self.screenshot_calls = 0
        self.log_calls = 0

    def _frame(self) -> bytes:
        index = min(self.screenshot_calls, len(self.frames) - 1)
        self.screenshot_calls += 1
        return self.frames[index]

    def snapshot_from_capture(self, image_path: Path, run_id: str, *, width: int, height: int, label: str = "screen"):
        resolved = image_path.resolve()
        return ScreenSnapshot(
            snapshot_id=f"snap-{self.screenshot_calls}",
            run_id=run_id,
            image_path=resolved,
            image_sha256="sha-snapshot",
            width=width,
            height=height,
            page_path="pages/Feed",
            elements=[
                UIElement(
                    element_id="card",
                    content=f"卡片 {self.screenshot_calls}",
                    key="p2_feed_card",
                    type="Button",
                    bbox=BoundingBox(left=10, top=10, right=100, bottom=60),
                    clickable=True,
                )
            ],
        )

    def click(self, x: int, y: int) -> CommandResult:
        return CommandResult(command="click", returncode=0)

    def collect_logs(self, output_path: Path) -> CommandResult:
        self.log_calls += 1
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(self.hilog, encoding="utf-8")
        return CommandResult(
            command="hdc shell hilog -x",
            args=["shell", "hilog", "-x"],
            returncode=0,
            stdout=self.hilog,
        )

    def _run(self, *args: str, **kwargs) -> CommandResult:
        return CommandResult(command="hdc", args=list(args), returncode=0, stdout="")


class _Hdc:
    """DC 截图快路径的 JPEG 替身：按调用顺序回放下一个编排好的帧。"""

    def __init__(self, device: _Device) -> None:
        self.device = device

    def screenshot_jpeg(self, screens_dir: Path, name: str, on_phase=None):
        screens_dir.mkdir(parents=True, exist_ok=True)
        payload = self.device._frame()
        path = screens_dir / f"{name}.jpeg"
        path.write_bytes(payload)
        return path, payload, 1080, 1920


class _Harness:
    def __init__(self, tmp_path: Path, *, frames: list[bytes] | None = None, hilog: str = "") -> None:
        from pydantic_ai import RunContext
        from pydantic_ai.usage import RunUsage

        self.holder = DcSnapshotHolder()
        self.device = _Device(frames=frames, hilog=hilog)
        self.recorder = DcActionRecorder()
        self.events: list[tuple[DcEventType, str, dict]] = []
        self.defects: list = []
        deps = DcToolContext(
            session_id="dc-inrun",
            device=self.device,  # type: ignore[arg-type]
            hdc=_Hdc(self.device),  # type: ignore[arg-type]
            safety=None,  # type: ignore[arg-type]
            recorder=self.recorder,
            artifacts=_Artifacts(tmp_path / "runs"),
            session_dir=tmp_path,
            snapshot_holder=self.holder,
            tier=DcToolTier.L1,
            bundle_name=BUNDLE,
            defects=self.defects,
        )
        self.deps = deps
        self.ctx = RunContext(deps=deps, model=None, usage=RunUsage())  # type: ignore[arg-type]
        self.recorder._emit_event = self._capture  # type: ignore[method-assign]

    def _capture(self, event_type: DcEventType, message: str, payload: dict | None = None) -> None:
        self.events.append((event_type, message, payload or {}))

    def anomaly_events(self) -> list[tuple[DcEventType, str, dict]]:
        return [item for item in self.events if item[0] is DcEventType.ANOMALY_DETECTED]


class _Artifacts:
    """最小 ArtifactStore 替身：只需要 ``run_dir``。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def run_dir(self, run_id: str) -> Path:
        path = self.root / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path


class TestSnapshotChain:
    def test_click_records_before_snapshot_and_screenshot_backfills_after(self, tmp_path: Path) -> None:
        """``before_snapshot_id`` 调用前补录；``after_snapshot_id`` 由下一次 screenshot 回填。"""
        harness = _Harness(tmp_path, frames=[b"frame-a", b"frame-a"])

        asyncio.run(tool_screenshot(harness.ctx))
        asyncio.run(tool_click(harness.ctx, 50, 30))
        asyncio.run(tool_screenshot(harness.ctx))

        invocations = harness.recorder.invocations
        click = next(item for item in invocations if item.tool is DcToolName.CLICK)
        assert click.before_snapshot_id == "snap-1"
        assert click.after_snapshot_id == "snap-2"

    def test_before_snapshot_is_none_without_a_prior_frame(self, tmp_path: Path) -> None:
        harness = _Harness(tmp_path)

        asyncio.run(tool_click(harness.ctx, 50, 30))

        click = next(item for item in harness.recorder.invocations if item.tool is DcToolName.CLICK)
        assert click.before_snapshot_id is None
        assert click.after_snapshot_id is None


class TestChangedFlagConsumption:
    def test_unchanged_frame_after_a_side_effect_emits_anomaly_and_notes_the_model(self, tmp_path: Path) -> None:
        """连续两次相同帧 + 中间夹一个 click ⇒ 发 ANOMALY_DETECTED 且模型可见 ``NOTE:``。"""
        harness = _Harness(tmp_path, frames=[b"same-frame"])

        asyncio.run(tool_screenshot(harness.ctx))
        asyncio.run(tool_click(harness.ctx, 50, 30))
        summary = asyncio.run(tool_screenshot(harness.ctx))

        events = harness.anomaly_events()
        assert len(events) == 1
        _event_type, message, payload = events[0]
        assert "未生效" in message or "无响应" in message
        assert payload["kind"] == "page_unresponsive"
        assert payload["phase"] == "in_run"
        assert harness.defects, "finding 必须写进会话 defects"
        assert harness.defects[0].phase == "in_run"

        assert "NOTE:" in summary
        assert "未生效" in summary

    def test_changed_frame_after_a_side_effect_is_silent(self, tmp_path: Path) -> None:
        """画面确实变了 ⇒ 不误报，且返回字符串保持历史格式。"""
        harness = _Harness(tmp_path, frames=[b"before-frame", b"after-frame"])

        asyncio.run(tool_screenshot(harness.ctx))
        asyncio.run(tool_click(harness.ctx, 50, 30))
        summary = asyncio.run(tool_screenshot(harness.ctx))

        assert harness.anomaly_events() == []
        assert harness.defects == []
        assert "NOTE:" not in summary

    def test_no_side_effect_in_between_is_silent(self, tmp_path: Path) -> None:
        """两次相同截图之间没有副作用工具 ⇒ 不产 finding（可能只是静止页面）。"""
        harness = _Harness(tmp_path, frames=[b"same-frame"])

        first = asyncio.run(tool_screenshot(harness.ctx))
        second = asyncio.run(tool_screenshot(harness.ctx))

        assert "NOTE:" not in first
        assert "NOTE:" not in second
        assert harness.anomaly_events() == []


class TestCollectLogsSummary:
    def test_crash_hilog_yields_a_suspected_crash_summary(self, tmp_path: Path) -> None:
        harness = _Harness(tmp_path, hilog=hilog_cppcrash())

        result = asyncio.run(tool_collect_logs(harness.ctx))

        assert result.startswith("ok: ")
        assert "SUSPECTED CRASH" in result
        assert "cppcrash" in result

    def test_clean_hilog_keeps_the_historic_string(self, tmp_path: Path) -> None:
        """无命中时返回字符串与历史实现逐字一致（保护既有测试与模型行为）。"""
        harness = _Harness(tmp_path, hilog=hilog_clean())

        result = asyncio.run(tool_collect_logs(harness.ctx))

        assert result.startswith("ok: ")
        assert "\n" not in result
        assert "SUSPECTED CRASH" not in result

    def test_appfreeze_is_reported(self, tmp_path: Path) -> None:
        from defect_fixtures import hilog_appfreeze

        harness = _Harness(tmp_path, hilog=hilog_appfreeze())

        result = asyncio.run(tool_collect_logs(harness.ctx))

        assert "SUSPECTED CRASH" in result
        assert "appfreeze" in result or "anr" in result

    def test_faultlog_listing_is_not_needed_for_the_summary(self, tmp_path: Path) -> None:
        """摘要只读刚采集到的 hilog 文件，不额外发设备调用。"""
        harness = _Harness(tmp_path, hilog=hilog_cppcrash())

        asyncio.run(tool_collect_logs(harness.ctx))

        assert harness.device.log_calls == 1
        # 说明：faultlog_index / FAULTLOG_CPPCRASH_HEAD 是 Phase 3 报告证据链用的 fixture，
        # 这里显式引用避免「fixture 存在但无人用」的死资源。
        assert faultlog_index() and FAULTLOG_CPPCRASH_HEAD
