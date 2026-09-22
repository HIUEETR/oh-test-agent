"""``analysis/service.py::_detect_stale_locator`` 单测（真机定位器失效复盘）。

真实失败：``artifacts/runs/dc-20260922T115708Z-f2acffa4`` 的一次 DC 脚本回放
``passed=false``（``DeviceTestError: Can't find component with [BY.key('add_agenda_title-...')]``），
而当时的 ``analysis.json`` 仍是 ``healthy: true`` / ``findings: []`` —— 分析层与应用判定自相矛盾。
本文件用那次失败的**原样** ``generated_result.json``（``tests/fixtures/replay/``）作为 fixture
锁定修复行为：选择器、出错脚本行与源码行都必须被抽出来，且 ``healthy`` 必须翻转为 ``False``。

不变式（5.3）：定位器失效与页面无响应是**两个独立信号，互不抑制**——页面没有停滞时只报
``LOCATOR_STALE``；页面确实停滞时两条 finding 都要在。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from harmony_test_agent.analysis.service import ExecutionAnalyzer
from harmony_test_agent.models import AnomalyKind, CommandResult, ExecutionAnalysis, ReplayResult

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "replay"
LOCATOR_FIXTURE = FIXTURES / "generated_result_locator_not_found.json"
BUNDLE = "com.zhihu.hmos"
SELECTOR = "BY.key('add_agenda_title-1790078405913')"

#: 与 ``tests/unit/test_analysis_service.py::CLEAN_LAYOUT`` 同结构：两份相同指纹即「UI 树停滞」。
STALE_LAYOUT: dict = {
    "attributes": {"pagePath": "pages/Index", "visible": "true", "bounds": "[0,0][1320,2232]"},
    "children": [
        {
            "attributes": {
                "key": "p2_home_titlebar_search",
                "type": "Button",
                "text": "搜索",
                "visible": "true",
                "bounds": "[100,80][300,160]",
            }
        }
    ],
}


def _no_device(device_id: str) -> None:
    """设备替身：一旦被调用即失败——定位器失效判定不得依赖任何设备操作。"""
    raise AssertionError(f"定位器失效判定不应接触设备（device_id={device_id}）")


def _payload() -> dict:
    """真机失败的 ``generated_result.json``（原样）。"""
    return json.loads(LOCATOR_FIXTURE.read_text(encoding="utf-8"))


def _transcript(payload: dict) -> str:
    """把失败信息拼成「stdout 文本」形态（message + traceback，stderr 为空）。"""
    return f"{payload['error']['message']}\n{payload['traceback']}"


def _replay(
    tmp_path: Path,
    *,
    status: str = "failed",
    stdout: str = "",
    stderr: str = "",
    generated_result: dict | None = None,
    layouts: tuple[dict, ...] = (),
) -> tuple[Path, ReplayResult]:
    """构造一次 attempt 的 run 目录与 ReplayResult（无截图 / 无设备）。"""
    run_dir = tmp_path / "run-locator"
    attempt_dir = run_dir / "hypium" / "attempt-01"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    if layouts:
        layout_dir = run_dir / "layouts"
        layout_dir.mkdir(parents=True, exist_ok=True)
        base = 1_700_000_000
        for index, dump in enumerate(layouts):
            path = layout_dir / f"{index + 1:03d}.json"
            path.write_text(json.dumps(dump, ensure_ascii=False), encoding="utf-8")
            stamp = base + index
            os.utime(path, (stamp, stamp))
    passed = status == "passed"
    command = CommandResult(
        command="python dc_test_dc_20260922T115708Z_f2acffa4.py",
        args=[],
        returncode=0 if passed else 1,
        stdout=stdout,
        stderr=stderr,
        timed_out=status == "timed_out",
    )
    replay = ReplayResult(
        attempt=1,
        command=command,
        report_path=attempt_dir,
        passed=passed,
        status=status,
        generated_result=generated_result,
    )
    return run_dir, replay


def _analyze(tmp_path: Path, **kwargs) -> ExecutionAnalysis:
    """跑一次回放分析；注入的设备工厂只会抛异常（设备绝不参与判定）。"""
    run_dir, replay = _replay(tmp_path, **kwargs)
    return ExecutionAnalyzer(device_factory=_no_device).analyze_replay(
        replay, run_dir=run_dir, bundle_name=BUNDLE, device_id="SN1"
    )


def _stale(analysis: ExecutionAnalysis):
    return next(finding for finding in analysis.findings if finding.kind is AnomalyKind.LOCATOR_STALE)


def _assert_stale_finding(analysis: ExecutionAnalysis, *, matched_in: str) -> None:
    stale = _stale(analysis)
    assert stale.severity == "critical"
    assert stale.source == "stdout"
    assert stale.summary_zh == f"脚本定位器在设备上已失效：{SELECTOR}"
    # 选择器不得带尾部 ``]`` / 换行（hypium 方括号内的原文）。
    assert stale.evidence["selector"] == SELECTOR
    assert stale.evidence["script_line"] == 57
    assert "driver.input_text" in stale.evidence["source_line"]
    assert stale.evidence["exception"] == "HypiumComponentNotFoundError"
    assert stale.evidence["matched_in"] == matched_in
    assert analysis.healthy is False


def test_locator_stale_is_reported_from_stdout_and_generated_result(tmp_path):
    """两路都有信息时：只产出一条 LOCATOR_STALE，且页面没停滞就不该有别的 finding。"""
    payload = _payload()
    analysis = _analyze(
        tmp_path,
        stdout=_transcript(payload),
        generated_result=payload,
    )

    assert [finding.kind.value for finding in analysis.findings] == ["locator_stale"]
    _assert_stale_finding(analysis, matched_in="stdout")


def test_locator_stale_is_reported_when_only_stdout_carries_it(tmp_path):
    """真机形态：失败信息只出现在回放输出里（stderr 为空）。"""
    payload = _payload()
    analysis = _analyze(tmp_path, stdout=_transcript(payload), stderr="")

    assert [finding.kind.value for finding in analysis.findings] == ["locator_stale"]
    _assert_stale_finding(analysis, matched_in="stdout")


def test_locator_stale_is_reported_when_only_generated_result_carries_it(tmp_path):
    """真机形态：``stderr.log`` 为空，信息只在 ``generated_result.json`` 里。"""
    analysis = _analyze(tmp_path, stdout="", generated_result=_payload())

    assert [finding.kind.value for finding in analysis.findings] == ["locator_stale"]
    _assert_stale_finding(analysis, matched_in="generated_result")


def test_clean_pass_stays_healthy_without_locator_stale(tmp_path):
    analysis = _analyze(tmp_path, status="passed", generated_result={})

    assert analysis.findings == []
    assert analysis.healthy is True
    assert not any(finding.kind is AnomalyKind.LOCATOR_STALE for finding in analysis.findings)


def test_locator_stale_and_page_unresponsive_are_both_reported(tmp_path):
    """5.3：定位器失效 + 页面停滞（超时标记 + UI 树停滞）→ 两条 finding 都在。"""
    payload = _payload()
    analysis = _analyze(
        tmp_path,
        status="timed_out",
        stdout=_transcript(payload),
        layouts=(STALE_LAYOUT, STALE_LAYOUT),
    )

    kinds = sorted(finding.kind.value for finding in analysis.findings)
    assert kinds == ["locator_stale", "page_unresponsive"]
    unresponsive = next(finding for finding in analysis.findings if finding.kind is AnomalyKind.PAGE_UNRESPONSIVE)
    assert unresponsive.evidence["layout_stale"] is True
    _assert_stale_finding(analysis, matched_in="stdout")


def test_missing_or_malformed_generated_result_is_tolerated(tmp_path):
    """缺失 / 非字典 / 缺嵌套键的 ``generated_result`` 不得产出空 finding，也不得抛异常。"""
    payloads: tuple = (None, {}, {"error": None}, {"error": "boom"}, {"traceback": 123}, {"error": {}, "traceback": ""})
    for index, payload in enumerate(payloads):
        analysis = _analyze(tmp_path / f"case-{index}", status="failed", generated_result=payload)
        assert [finding.kind.value for finding in analysis.findings] == []


def test_message_without_a_selector_yields_no_finding(tmp_path):
    """抽不到选择器就不产出 finding（绝不报一条没有证据的「定位器失效」）。"""
    analysis = _analyze(tmp_path, status="failed", stdout="DeviceTestError: 组件查找失败\n")

    assert [finding.kind.value for finding in analysis.findings] == []
    assert analysis.healthy is True
