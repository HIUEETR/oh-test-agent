"""DC ``list_apps`` 工具：单次 ``bm dump -a`` 快速路径。

回归背景：旧实现走 ``HarmonyDeviceAdapter.list_installed_apps``，对 66 个已安装
bundle 逐个 ``bm dump -n``（实测每包 ≈1.3s，合计 ≈86s），必然触发
``DcActionRecorder`` 的 30s 超时，工具从未成功过。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from harmony_test_agent.dc.models import DcEventType, DcToolName, DcToolTier
from harmony_test_agent.dc.tools import (
    _MAX_LISTED_APPS,
    DcActionRecorder,
    DcSnapshotHolder,
    DcToolContext,
    tool_list_apps,
)
from harmony_test_agent.models import CommandResult
from harmony_test_agent.storage.artifacts import ArtifactStore

BUNDLES = [f"com.example.app{i:02d}" for i in range(66)]


class FakeHdc:
    """只支持 ``list_bundle_names`` 的 HDC 桩，记录调用次数。"""

    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.calls = 0

    def list_bundle_names(self, timeout: float = 20) -> CommandResult:
        self.calls += 1
        return self.result


class RecordingDevice:
    """记录调用次数的设备桩：回归护栏用。"""

    def __init__(self) -> None:
        self.list_calls = 0

    def list_installed_apps(self) -> Any:
        self.list_calls += 1
        raise AssertionError("list_apps must not enumerate per-bundle metadata")


def bm_dump_all_ok(bundles: list[str]) -> CommandResult:
    stdout = "".join(f"  bundleName: {bundle}\n" for bundle in bundles)
    return CommandResult(command="hdc shell bm dump -a", returncode=0, stdout=stdout)


def make_context(
    tmp_path: Path,
    hdc: FakeHdc,
    device: Any | None = None,
) -> tuple[Any, list[tuple[str, str, dict[str, Any]]]]:
    events: list[tuple[str, str, dict[str, Any]]] = []
    recorder = DcActionRecorder()
    recorder.set_emitter(lambda etype, msg, payload: events.append((etype.value, msg, payload)))
    deps = DcToolContext(
        session_id="dc-test",
        device=device if device is not None else RecordingDevice(),
        hdc=hdc,  # type: ignore[arg-type]
        safety=SimpleNamespace(),  # type: ignore[arg-type]
        recorder=recorder,
        artifacts=ArtifactStore(tmp_path / "runs"),
        session_dir=tmp_path / "runs" / "dc-test",
        snapshot_holder=DcSnapshotHolder(),
        tier=DcToolTier.L2,
        turn_id="turn-1",
    )
    return SimpleNamespace(deps=deps), events


class TestListAppsFastPath:
    async def test_single_hdc_call_and_all_bundles_returned(self, tmp_path: Path) -> None:
        hdc = FakeHdc(bm_dump_all_ok(BUNDLES))
        ctx, events = make_context(tmp_path, hdc)

        result = await tool_list_apps(ctx)

        assert hdc.calls == 1
        assert result.startswith(f"{len(BUNDLES)} apps installed:")
        for bundle in BUNDLES:
            assert bundle in result
        finished = [event for event in events if event[0] == DcEventType.TOOL_CALL_FINISHED.value]
        assert finished and finished[0][2]["success"] is True
        assert finished[0][2]["tool"] == DcToolName.LIST_APPS.value

    async def test_query_filters_bundles(self, tmp_path: Path) -> None:
        hdc = FakeHdc(bm_dump_all_ok([*BUNDLES, "com.huawei.hmos.browser"]))
        ctx, _ = make_context(tmp_path, hdc)

        result = await tool_list_apps(ctx, "HUAWEI")

        assert "com.huawei.hmos.browser" in result
        assert "com.example.app01" not in result
        assert result.startswith("1 apps installed matching 'HUAWEI'")

    async def test_query_without_match_reports_zero(self, tmp_path: Path) -> None:
        hdc = FakeHdc(bm_dump_all_ok(BUNDLES))
        ctx, _ = make_context(tmp_path, hdc)

        result = await tool_list_apps(ctx, "com.absent")

        assert result == "0 apps installed matching 'com.absent'"

    async def test_output_is_capped_with_explicit_header(self, tmp_path: Path) -> None:
        bundles = [f"com.example.app{i:03d}" for i in range(_MAX_LISTED_APPS + 12)]
        hdc = FakeHdc(bm_dump_all_ok(bundles))
        ctx, _ = make_context(tmp_path, hdc)

        result = await tool_list_apps(ctx)

        listed = [line for line in result.splitlines()[1:] if line.strip()]
        assert len(listed) == _MAX_LISTED_APPS
        assert f"(showing first {_MAX_LISTED_APPS})" in result
        assert f"{len(bundles)} apps installed" in result

    async def test_failed_bm_dump_marks_invocation_failed(self, tmp_path: Path) -> None:
        failure = CommandResult(command="hdc shell bm dump -a", returncode=1, stderr="hdc: device offline")
        hdc = FakeHdc(failure)
        ctx, events = make_context(tmp_path, hdc)

        result = await tool_list_apps(ctx)

        finished = [event for event in events if event[0] == DcEventType.TOOL_CALL_FINISHED.value]
        assert finished and finished[0][2]["success"] is False
        assert "FAILED" in result or "hdc: device offline" in result

    async def test_empty_catalog_is_not_an_error(self, tmp_path: Path) -> None:
        hdc = FakeHdc(bm_dump_all_ok([]))
        ctx, events = make_context(tmp_path, hdc)

        result = await tool_list_apps(ctx)

        assert result == "0 apps installed"
        finished = [event for event in events if event[0] == DcEventType.TOOL_CALL_FINISHED.value]
        assert finished and finished[0][2]["success"] is True


class TestListAppsDoesNotUseSlowCatalog:
    """回归护栏：不得回退到逐包 inspect 的 ``list_installed_apps``。"""

    async def test_slow_catalog_is_never_called(self, tmp_path: Path) -> None:
        hdc = FakeHdc(bm_dump_all_ok(BUNDLES))
        device = RecordingDevice()
        ctx, _ = make_context(tmp_path, hdc, device)

        result = await tool_list_apps(ctx)

        assert device.list_calls == 0
        assert result.startswith(f"{len(BUNDLES)} apps installed:")
