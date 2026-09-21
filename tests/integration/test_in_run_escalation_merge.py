"""真机验收暴露的接线缺陷的回归测试。

**缺陷**：``analysis/service.py::_detect_unresponsive`` 事后升级出的 critical
``PAGE_UNRESPONSIVE`` finding 不携带 ``page_path`` / ``action_id`` / ``target``，
而它其实是运行中那条 stall finding 的「加强版」。两者归并键不同 ⇒
``DefectRecorder`` 把它们记成**两条**缺陷，且 ``to-bug-repro`` 闭环可能选中
**没有页面/动作/证据上下文**的那条。

真机证据：网易云音乐运行中 force-stop 后，缺陷库出现
``defect-89a2f43ac4eb``（page=pages/Index, action=s4，上下文完整）与
``defect-665ecc216611``（page/action 全空）两条同源缺陷。
"""

from __future__ import annotations

from pathlib import Path

from harmony_test_agent.analysis.defects import merge_key, stable_defect_id
from harmony_test_agent.analysis.service import ExecutionAnalyzer
from harmony_test_agent.models import (
    AnomalyFinding,
    AnomalyKind,
    RunState,
    RunTrace,
    ScreenSnapshot,
    TargetAppProfile,
    utc_now,
)

BUNDLE = "com.zhihu.hmos"


def _stall_finding(*, page_path: str = "pages/Index", action_id: str = "s3", target: str = "") -> AnomalyFinding:
    """运行中检测器产出的结构停滞 finding（带完整上下文）。"""
    return AnomalyFinding(
        kind=AnomalyKind.PAGE_UNRESPONSIVE,
        severity="warning",
        summary_zh="点击后页面结构无变化，疑似无响应控件",
        detail="动作 s3（click_element）后页面结构指纹与动作前完全相同",
        source="screenshot",
        action_id=action_id,
        page_path=page_path,
        phase="in_run",
        evidence={
            "action_id": action_id,
            "tool": "click_element",
            "target": target,
            "page_path": page_path,
            "bundle_name": BUNDLE,
            "struct_same": True,
        },
    )


def _snapshot(tmp_path: Path, *, snapshot_id: str, page_path: str = "pages/Index") -> ScreenSnapshot:
    """一张**有内容**的截图。

    刻意不用纯色：纯色图会被白屏判定命中，引入与归并无关的 noise finding，
    让「同源 finding 是否合并」的断言失去焦点。
    """
    from PIL import Image

    image = tmp_path / f"{snapshot_id}.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("L", (300, 600), 120)
    for x in range(300):
        for y in range(0, 600, 5):
            canvas.putpixel((x, y), (x * 7 + y * 13) % 256)
    canvas.save(image, format="PNG")
    return ScreenSnapshot(
        snapshot_id=snapshot_id,
        run_id="run-live",
        image_path=image,
        image_sha256="same-digest",
        width=1080,
        height=2340,
        page_path=page_path,
        elements=[],
    )


def _trace(tmp_path: Path, *, defects: list[AnomalyFinding]) -> RunTrace:
    snapshots = [
        _snapshot(tmp_path, snapshot_id="snap-1"),
        _snapshot(tmp_path, snapshot_id="snap-2"),
        _snapshot(tmp_path, snapshot_id="snap-3"),
    ]
    return RunTrace(
        run_id="run-live",
        target_app_id="zhihu",
        task="结构停滞",
        device_id="SN1",
        state=RunState.COMPLETED,
        profile_snapshot=TargetAppProfile(target_app_id="zhihu", display_name="知乎++", bundle_name=BUNDLE),
        snapshots=snapshots,
        defects=list(defects),
        started_at=utc_now(),
    )


class TestUpgradedFindingInheritsContext:
    def test_escalated_critical_finding_keeps_page_and_action(self, tmp_path: Path) -> None:
        """升级出的 critical finding 必须继承 page_path / action_id / target。"""
        origin = _stall_finding(page_path="pages/Feed", action_id="s7", target="ui-hot-list")
        trace = _trace(tmp_path, defects=[origin])
        analyzer = ExecutionAnalyzer()  # 不注入设备工厂：纯离线

        analysis = analyzer.analyze_run(trace, tmp_path)

        escalated = [
            finding
            for finding in analysis.findings
            if finding.kind is AnomalyKind.PAGE_UNRESPONSIVE and finding.severity == "critical"
        ]
        assert escalated, "应升级出一条 critical PAGE_UNRESPONSIVE"
        finding = escalated[0]
        assert finding.page_path == "pages/Feed"
        assert finding.action_id == "s7"
        assert finding.evidence["target"] == "ui-hot-list"
        assert finding.evidence["tool"] == "click_element"
        assert finding.evidence["in_run_origin"] == origin.summary_zh

    def test_escalated_finding_merges_with_its_origin(self, tmp_path: Path) -> None:
        """**核心回归**：升级后的 finding 与运行中那条必须算出同一个归并键（同一条缺陷）。"""
        origin = _stall_finding(page_path="pages/Feed", action_id="s7", target="ui-hot-list")
        trace = _trace(tmp_path, defects=[origin])
        analyzer = ExecutionAnalyzer()

        analysis = analyzer.analyze_run(trace, tmp_path)

        assert merge_key(origin, BUNDLE) == merge_key(
            next(
                finding
                for finding in analysis.findings
                if finding.kind is AnomalyKind.PAGE_UNRESPONSIVE and finding.severity == "critical"
            ),
            BUNDLE,
        )

    def test_defect_ids_are_identical_so_the_recorder_merges_them(self, tmp_path: Path) -> None:
        """用真实 recorder 验证：只应落一条缺陷，且上下文来自运行中那条。"""
        from harmony_test_agent.analysis.defects import DefectRecorder, record_from_finding
        from harmony_test_agent.storage import DefectRepository

        origin = _stall_finding(page_path="pages/Feed", action_id="s7", target="ui-hot-list")
        trace = _trace(tmp_path, defects=[origin])
        analysis = ExecutionAnalyzer().analyze_run(trace, tmp_path)
        escalated = next(
            finding
            for finding in analysis.findings
            if finding.kind is AnomalyKind.PAGE_UNRESPONSIVE and finding.severity == "critical"
        )

        repository = DefectRepository(tmp_path / "agent.db")
        recorder = DefectRecorder(repository)
        recorder.record_from_findings(findings=[origin, escalated], bundle_name=BUNDLE, run_id="run-live")

        assert repository.count() == 1, "同源 finding 只应落一条缺陷"
        summary = repository.list()[0]
        assert summary.occurrences == 2
        assert summary.page_path == "pages/Feed"
        assert summary.action_id == "s7"
        # 两条 finding 的 id 一致（便于报告与 API 对账）
        assert record_from_finding(origin, bundle_name=BUNDLE).defect_id == escalated.defect_id

    def test_evidence_links_survive_the_escalation(self, tmp_path: Path) -> None:
        """截图证据链接不能在升级过程中丢失（闭环产物需要它）。"""
        origin = _stall_finding(page_path="pages/Feed", action_id="s7")
        origin.screenshot = "screens/step-07.png"
        trace = _trace(tmp_path, defects=[origin])

        analysis = ExecutionAnalyzer().analyze_run(trace, tmp_path)

        escalated = next(
            finding
            for finding in analysis.findings
            if finding.kind is AnomalyKind.PAGE_UNRESPONSIVE and finding.severity == "critical"
        )
        assert escalated.screenshot == "screens/step-07.png"


class TestWithoutAnInRunOrigin:
    def test_escalation_does_not_introduce_white_screen_noise(self, tmp_path: Path) -> None:
        """有内容的帧上不得因升级逻辑引入白屏 finding（保证证据链干净）。"""
        origin = _stall_finding()
        trace = _trace(tmp_path, defects=[origin])

        analysis = ExecutionAnalyzer().analyze_run(trace, tmp_path)

        assert not [finding for finding in analysis.findings if finding.kind is AnomalyKind.WHITE_SCREEN]

    def test_stall_signal_without_origin_still_reports_critical(self, tmp_path: Path) -> None:
        """没有任何 in-run finding 时，纯靠帧/树停滞仍应报 critical（向后兼容）。"""
        trace = _trace(tmp_path, defects=[])

        analysis = ExecutionAnalyzer().analyze_run(trace, tmp_path)

        # 三帧同摘要 + 无 in-run 信号：只靠 tail_run 不足以在没有 marker 时判 critical
        # （这保持既有语义：无响应需要两个独立信号），因此这里断言不误报。
        assert not [
            finding
            for finding in analysis.findings
            if finding.kind is AnomalyKind.PAGE_UNRESPONSIVE and finding.severity == "critical"
        ]

    def test_identical_defect_id_is_stable(self) -> None:
        key = merge_key(_stall_finding(), BUNDLE)

        assert stable_defect_id(key) == stable_defect_id(key)
