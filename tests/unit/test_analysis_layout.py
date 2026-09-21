"""``analysis/layout.py`` 单测：L1–L4 规则、原始 dump 语义、max_findings 截断。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from harmony_test_agent.analysis.layout import detect_layout_anomalies
from harmony_test_agent.perception.normalizer import normalize_layout

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "analysis"
WIDTH = 1320
HEIGHT = 2232


def _dump() -> dict:
    return json.loads((FIXTURES / "layout_raw.json").read_text(encoding="utf-8"))


def _rules(findings) -> list[str]:
    return [finding.evidence["rule"] for finding in findings]


def test_raw_dump_reports_all_four_rules_in_order():
    findings = detect_layout_anomalies(_dump(), WIDTH, HEIGHT)
    assert _rules(findings) == [
        "L1_out_of_bounds",
        "L2_text_overlap",
        "L3_clipped_child",
        "L4_zero_size_visible",
    ]
    assert [finding.kind.value for finding in findings] == ["layout_anomaly"] * 4
    assert [finding.severity for finding in findings] == ["warning", "warning", "info", "info"]
    assert all(finding.source == "ui_dump" for finding in findings)
    assert all(finding.evidence["hierarchy"] == "raw_arkui_dump" for finding in findings)


def test_out_of_bounds_uses_raw_dump_that_normalize_layout_discards():
    dump = _dump()
    elements = normalize_layout(dump, WIDTH, HEIGHT)
    offscreen = next(element for element in elements if element.key == "p2_off_left_title")
    # normalize_layout 丢弃越界 bbox（perception/normalizer.py:80-81），异常检测必须看到原始 bounds。
    assert offscreen.bbox is None
    findings = detect_layout_anomalies(dump, WIDTH, HEIGHT)
    l1 = next(finding for finding in findings if finding.evidence["rule"] == "L1_out_of_bounds")
    assert l1.evidence["key"] == "p2_off_left_title"
    assert l1.evidence["bounds"] == "[-40,300][900,900]"
    assert l1.evidence["overflow_px"]["left"] == 38
    assert l1.evidence["screen_size"] == [WIDTH, HEIGHT]
    assert l1.evidence["area_ratio"] > 0.01


def test_out_of_bounds_detects_right_and_bottom_overflow():
    dump = _dump()
    node = dump["children"][0]["attributes"]
    node["bounds"] = "[1000,2100][1400,2300]"
    findings = detect_layout_anomalies(dump, WIDTH, HEIGHT)
    l1 = next(finding for finding in findings if finding.evidence["rule"] == "L1_out_of_bounds")
    assert l1.evidence["overflow_px"]["right"] == 78
    assert l1.evidence["overflow_px"]["bottom"] == 66


def test_ignores_invisible_and_small_nodes():
    dump = _dump()
    findings = detect_layout_anomalies(dump, WIDTH, HEIGHT)
    keys = [finding.evidence["key"] for finding in findings]
    assert "p2_hidden_offscreen" not in keys
    tiny = copy.deepcopy(dump)
    tiny["children"][0]["attributes"]["bounds"] = "[100,300][120,320]"
    assert _rules(detect_layout_anomalies(tiny, WIDTH, HEIGHT)) == [
        "L2_text_overlap",
        "L3_clipped_child",
        "L4_zero_size_visible",
    ]


def test_text_overlap_reports_both_siblings():
    dump = _dump()
    l2 = next(
        finding
        for finding in detect_layout_anomalies(dump, WIDTH, HEIGHT)
        if finding.evidence["rule"] == "L2_text_overlap"
    )
    assert l2.evidence["key"] in {"overlap_a", "overlap_b"}
    assert l2.evidence["other"]["key"] in {"overlap_a", "overlap_b"}
    assert l2.evidence["iou"] > 0.5


def test_clipped_child_reports_parent_and_coverage():
    dump = _dump()
    l3 = next(
        finding
        for finding in detect_layout_anomalies(dump, WIDTH, HEIGHT)
        if finding.evidence["rule"] == "L3_clipped_child"
    )
    assert l3.evidence["key"] == "clip_child_text"
    assert l3.evidence["parent"]["key"] == "clip_group"
    assert l3.evidence["coverage"] < 0.9


def test_zero_size_visible_node_reported_as_info():
    dump = _dump()
    l4 = next(
        finding
        for finding in detect_layout_anomalies(dump, WIDTH, HEIGHT)
        if finding.evidence["rule"] == "L4_zero_size_visible"
    )
    assert l4.severity == "info"
    assert l4.evidence["key"] == "p2_zero_size_label"
    assert l4.evidence["degenerate"] is True


def test_max_findings_truncates():
    findings = detect_layout_anomalies(_dump(), WIDTH, HEIGHT, max_findings=2)
    assert _rules(findings) == ["L1_out_of_bounds", "L2_text_overlap"]
    assert detect_layout_anomalies(_dump(), WIDTH, HEIGHT, max_findings=0) == []


def test_tolerance_is_configurable():
    dump = _dump()
    node = dump["children"][0]["attributes"]
    node["bounds"] = "[-1,300][900,900]"
    assert "L1_out_of_bounds" not in _rules(detect_layout_anomalies(dump, WIDTH, HEIGHT))
    assert "L1_out_of_bounds" in _rules(detect_layout_anomalies(dump, WIDTH, HEIGHT, tol=0))


def test_invalid_inputs_return_empty_list():
    assert detect_layout_anomalies({}, WIDTH, HEIGHT) == []
    assert detect_layout_anomalies({"attributes": {}}, 0, HEIGHT) == []
    assert detect_layout_anomalies({"attributes": {}}, WIDTH, 0) == []
