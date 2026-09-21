"""探索期崩溃探测（Phase 2.4）：跨 bundle 恢复不再静默吞掉一次真崩溃。

历史行为：应用崩溃弹回桌面 → 命中跨 bundle 规则 → 记
``blocked_reason="cross-bundle navigation blocked"`` → back 键恢复 → 继续探索，**当无事发生**；
探索期写下的 ``transition-NNN.hilog.txt`` 也从来没有任何代码读回。本文件把该行为钉成测试。
"""

from __future__ import annotations

from pathlib import Path

from defect_fixtures import (
    BUNDLE,
    FAULTLOG_CPPCRASH_HEAD,
    DefectFakeDevice,
    faultlog_index,
    hilog_clean,
    hilog_cppcrash,
)

from harmony_test_agent.devices import DeviceAdapter
from harmony_test_agent.discovery.explorer import (
    BoundedExplorer,
    DiscoveryPage,
    ExplorationAction,
)
from harmony_test_agent.models import (
    BoundingBox,
    CommandResult,
    ExplorationPolicy,
    ResolvedTarget,
    ScreenSnapshot,
    UIElement,
)
from harmony_test_agent.targets import ForegroundApp

OTHER_BUNDLE = "com.huawei.hmos.settings"


class _CrashyDevice(DefectFakeDevice):
    """前台应用脚本：初始在目标应用，``click`` 后跑到 ``escaped_bundle``，``back`` 后恢复。

    按**语义**推进而不是按查前台次数推进：``_perform`` 会多次查前台（探测 + 恢复校验），
    按次数推进会污染恢复校验读到的前台值。
    """

    def __init__(self, *, escaped_bundle: str | None = OTHER_BUNDLE, **kwargs) -> None:
        super().__init__(foreground=BUNDLE, **kwargs)
        self._escaped_bundle = escaped_bundle
        self._escaped = False
        self.screenshots = 0

    def current_foreground_app(self):
        self.foreground_calls += 1
        bundle = self._escaped_bundle if self._escaped else BUNDLE
        if bundle is None:
            return None
        return ForegroundApp(bundle_name=bundle, ability_name="EntryAbility", window_type="main")

    # -- 探索器需要的其余设备面 ------------------------------------
    def screenshot(self, output_dir: Path, run_id: str, label: str = "screen") -> ScreenSnapshot:
        self.screenshots += 1
        output_dir.mkdir(parents=True, exist_ok=True)
        image_path = output_dir / f"{label}.png"
        from PIL import Image

        Image.new("RGB", (200, 400), "navy").save(image_path)
        return ScreenSnapshot(
            snapshot_id=f"{label}-{self.screenshots}",
            run_id=run_id,
            image_path=image_path.resolve(),
            image_sha256=f"sha-{self.screenshots}",
            width=200,
            height=400,
            page_path="pages/EntryPage",
            elements=[
                UIElement(
                    element_id="card",
                    content="热榜",
                    key="p2_feed_card",
                    type="Button",
                    bbox=BoundingBox(left=10, top=10, right=100, bottom=60),
                    clickable=True,
                )
            ],
        )

    def wait(self, seconds: float) -> CommandResult:
        return CommandResult(command="wait", returncode=0)

    def back(self) -> CommandResult:
        # 恢复动作把应用带回目标应用（真实恢复路径的最小语义）
        self._escaped = False
        return CommandResult(command="back", returncode=0)

    def start_app(self, bundle_name: str, ability_name: str, module_name: str | None = None) -> CommandResult:
        self._escaped = False
        return CommandResult(command="start_app", returncode=0)

    def click(self, x: int, y: int) -> CommandResult:
        # 点击把应用踢出前台（崩溃 / 跳外链的共同现象）
        self._escaped = True
        return CommandResult(command="click", returncode=0)


def _target() -> ResolvedTarget:
    return ResolvedTarget(
        target_app_id="zhihu",
        display_name="知乎++",
        bundle_name=BUNDLE,
        main_ability="EntryAbility",
        device_id="SN1",
        source="explicit_override",
    )


def _explorer(tmp_path: Path, device: DeviceAdapter) -> BoundedExplorer:
    return BoundedExplorer(
        device,
        _target(),
        output_dir=tmp_path,
        run_id="run-explore",
        policy=ExplorationPolicy(settle_timeout_seconds=0, restore_retries=0),
    )


def _page() -> DiscoveryPage:
    return DiscoveryPage(
        page_id="page-1",
        signature="sig-1",
        page_path="pages/EntryPage",
        bundle_name=BUNDLE,
        snapshot_id="snap-1",
        image_path=Path("snap-1.png"),
        element_count=1,
        discovered_order=0,
    )


def _snapshot() -> ScreenSnapshot:
    return ScreenSnapshot(
        snapshot_id="snap-1",
        run_id="run-explore",
        image_path=Path("snap-1.png"),
        image_sha256="sha-1",
        width=200,
        height=400,
        page_path="pages/EntryPage",
        elements=[],
    )


def _action() -> ExplorationAction:
    return ExplorationAction(
        action_id="act-1",
        kind="click",
        coordinate=(50, 30),
    )


class TestCrashProbeOnCrossBundleRecovery:
    def test_crash_hilog_marks_the_transition_and_records_an_anomaly(self, tmp_path: Path) -> None:
        """跨 bundle + hilog 含崩溃 ⇒ blocked_reason 带 ``app crash suspected`` + anomalies 非空。"""
        device = _CrashyDevice(
            hilog=hilog_cppcrash(),
            listing=faultlog_index(),
            head=FAULTLOG_CPPCRASH_HEAD,
        )
        explorer = _explorer(tmp_path, device)
        foreground = ForegroundApp(bundle_name=BUNDLE, ability_name="EntryAbility", window_type="main")

        transition, after = explorer._perform(_page(), _snapshot(), foreground, _action(), sequence=4)

        assert transition.success is False
        assert transition.blocked_reason is not None
        assert transition.blocked_reason.startswith("app crash suspected: ")
        assert "cross-bundle navigation blocked" in transition.blocked_reason
        # 恢复仍然成功（行为不变：跨 bundle 分支返回 None 表示该动作没有可用后帧），只是留痕
        assert after is None
        assert explorer._anomalies, "崩溃必须被收进探索结果"
        assert all(finding.phase == "exploration" for finding in explorer._anomalies)
        assert all(finding.detected_at is not None for finding in explorer._anomalies)
        # 探索期写下的 transition-NNN.hilog.txt 终于被读回
        assert (tmp_path / "transition-005.hilog.txt").is_file()

    def test_clean_logs_keep_the_historic_blocked_reason(self, tmp_path: Path) -> None:
        """跨 bundle + hilog 干净 ⇒ 保持原文案，不误报崩溃。"""
        device = _CrashyDevice(
            hilog=hilog_clean(),
            listing="total 0\n",
        )
        explorer = _explorer(tmp_path, device)
        foreground = ForegroundApp(bundle_name=BUNDLE, ability_name="EntryAbility", window_type="main")

        transition, after = explorer._perform(_page(), _snapshot(), foreground, _action(), sequence=4)

        assert transition.success is False
        assert transition.blocked_reason == f"cross-bundle navigation blocked: {OTHER_BUNDLE}"
        assert after is None
        assert explorer._anomalies == []

    def test_anomalies_land_in_the_discovery_result(self, tmp_path: Path) -> None:
        """``DiscoveryResult.anomalies`` 是 additive 字段，必须被填充（落 summary.json）。"""
        device = _CrashyDevice(
            hilog=hilog_cppcrash(),
            listing=faultlog_index(),
            head=FAULTLOG_CPPCRASH_HEAD,
        )
        explorer = _explorer(tmp_path, device)
        from harmony_test_agent.models import AnomalyFinding, AnomalyKind

        explored = explorer._probe_crash_after_foreground_loss(4)
        explorer._record_anomalies(explored)
        result_kwargs = {"anomalies": list(explorer._anomalies)}

        assert explored, "cppcrash fixture 必须被解析出 finding"
        assert any(finding.kind in {AnomalyKind.CPP_CRASH, AnomalyKind.JS_CRASH} for finding in explored)
        assert all(isinstance(finding, AnomalyFinding) for finding in result_kwargs["anomalies"])

    def test_probe_failure_never_breaks_recovery(self, tmp_path: Path) -> None:
        """探测本身抛异常时必须只记日志：恢复流程绝不能因此失败。"""

        class BrokenProbe(DefectFakeDevice):
            def current_foreground_app(self):
                self.foreground_calls += 1
                return None

            def collect_logs(self, path):
                raise RuntimeError("hdc exploded")

            def _run(self, *args, **kwargs):
                raise RuntimeError("hdc exploded")

        explorer = _explorer(tmp_path, BrokenProbe())

        assert explorer._probe_crash_after_foreground_loss(1) == []
