"""``analysis/hilog.py`` 单测：崩溃 / 卡死 / ANR 解析、归属降级、时间窗、去重与 faultlog 索引。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from harmony_test_agent.analysis.hilog import (
    FAULTLOG_DIR,
    collect_faultlog_index,
    fetch_faultlog_evidence,
    parse_hilog,
)
from harmony_test_agent.models import AnomalyKind, CommandResult

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "analysis"
BUNDLE = "com.zhihu.hmos"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_hilog_classifies_crash_freeze_and_anr_with_attribution():
    findings = parse_hilog(_fixture("hilog_crash.txt"), bundle_name=BUNDLE)
    observed = [(item.kind.value, item.severity, item.evidence["attribution"]) for item in findings]
    assert observed == [
        ("cppcrash", "critical", "app"),
        ("cppcrash", "critical", "app"),
        ("jscrash", "critical", "app"),
        ("appfreeze", "critical", "app"),
        ("anr", "critical", "app"),
        ("anr", "critical", "app"),
        ("anr", "critical", "app"),
        ("cppcrash", "warning", "system"),
        ("cppcrash", "warning", "system"),
    ]


def test_parse_hilog_keeps_raw_lines_and_detail_for_crash():
    findings = parse_hilog(_fixture("hilog_crash.txt"), bundle_name=BUNDLE)
    cppcrash = findings[0]
    assert cppcrash.kind is AnomalyKind.CPP_CRASH
    assert cppcrash.source == "hilog"
    assert cppcrash.evidence["lines"] == [
        "02-10 10:00:01.200  4321  4321 E C01310/com.zhihu.hmos/Crash: cppcrash happened, process name com.zhihu.hmos"
    ]
    assert cppcrash.evidence["count"] == 1
    assert "cppcrash" in cppcrash.detail


def test_parse_hilog_downgrades_unattributed_lines_and_marks_system():
    line = "02-10 10:00:05.800  9001  9001 E C01310/com.other.app/Crash: cppcrash happened\n"
    findings = parse_hilog(line, bundle_name=BUNDLE)
    assert len(findings) == 1
    assert findings[0].severity == "warning"
    assert findings[0].evidence["attribution"] == "system"


def test_parse_hilog_attributes_by_pidof_marker():
    text = (
        f"# pidof {BUNDLE}: 7777\n"
        "02-10 10:01:00.000  7777  7777 E C01310/SomeTag: cppcrash happened\n"
        "02-10 10:01:01.000  8888  8888 E C01310/OtherTag: jscrash happened\n"
    )
    findings = parse_hilog(text, bundle_name=BUNDLE)
    assert [(item.kind.value, item.severity, item.evidence["attribution"]) for item in findings] == [
        ("cppcrash", "critical", "app"),
        ("jscrash", "warning", "system"),
    ]
    without_marker = parse_hilog(text.split("\n", 1)[1], bundle_name=BUNDLE)
    assert without_marker[0].severity == "warning"
    assert without_marker[0].evidence["attribution"] == "system"


def test_parse_hilog_filters_by_window_and_keeps_unparsable_lines():
    text = "\n".join(
        [
            "02-10 09:59:59.000  4321  4321 E C01310/com.zhihu.hmos/Crash: cppcrash before window",
            "02-10 10:00:10.000  4321  4321 E C01310/com.zhihu.hmos/Crash: cppcrash inside window",
            "02-10 10:00:31.000  4321  4321 E C01310/com.zhihu.hmos/Crash: cppcrash after window",
            "raw kernel cppcrash without timestamp",
        ]
    )
    window = (datetime(2026, 2, 10, 10, 0, 0), datetime(2026, 2, 10, 10, 0, 30))
    findings = parse_hilog(text, bundle_name=BUNDLE, window=window)
    assert [item.detail for item in findings] == [
        "02-10 10:00:10.000  4321  4321 E C01310/com.zhihu.hmos/Crash: cppcrash inside window",
        "raw kernel cppcrash without timestamp",
    ]
    assert findings[0].severity == "critical"
    assert findings[0].evidence["time_filtered"] is True
    assert findings[1].severity == "warning"
    assert findings[1].evidence["time_filtered"] is False


def test_parse_hilog_dedupes_and_caps_evidence_lines():
    line = "02-10 10:00:01.200  4321  4321 E C01310/com.zhihu.hmos/Crash: cppcrash happened repeatedly"
    findings = parse_hilog("\n".join([line] * 12), bundle_name=BUNDLE)
    assert len(findings) == 1
    assert findings[0].evidence["count"] == 12
    assert len(findings[0].evidence["lines"]) == 10


def test_parse_hilog_error_name_rule_is_warning():
    text = "02-10 10:02:00.000  4321  4321 E C01310/com.zhihu.hmos/AppKit: Error name: ReferenceError\n"
    findings = parse_hilog(text, bundle_name=BUNDLE)
    assert len(findings) == 1
    assert findings[0].kind is AnomalyKind.JS_CRASH
    assert findings[0].severity == "warning"


def test_parse_hilog_backtrace_rule_matches_bare_frame_line():
    text = "backtrace head\n#00 pc 0000000000123456 /system/lib64/libark.so\n"
    findings = parse_hilog(text, bundle_name=BUNDLE)
    assert len(findings) == 1
    assert findings[0].kind is AnomalyKind.CPP_CRASH
    # 调用栈规则本身是 warning，且该行无法归属被测应用 → 再降一级到 info。
    assert findings[0].severity == "info"
    assert findings[0].evidence["attribution"] == "system"


def test_parse_hilog_returns_empty_list_when_nothing_matches():
    assert parse_hilog(_fixture("hilog_clean.txt"), bundle_name=BUNDLE) == []
    assert parse_hilog("", bundle_name=BUNDLE) == []


class _FakeDevice:
    """最小设备替身：记录 argv 并按路径返回预置文本。"""

    def __init__(self, listing: str, heads: dict[str, str], default_head: str = "") -> None:
        self.listing = listing
        self.heads = heads
        self.default_head = default_head
        self.calls: list[tuple[str, ...]] = []

    def _run(self, *args: str, timeout: float | None = None, device: bool = True) -> CommandResult:
        self.calls.append(tuple(args))
        if args[:2] == ("shell", "ls"):
            return CommandResult(command="hdc shell ls", args=list(args), returncode=0, stdout=self.listing)
        if args[:2] == ("shell", "head"):
            name = Path(args[-1]).name
            return CommandResult(
                command="hdc shell head",
                args=list(args),
                returncode=0,
                stdout=self.heads.get(name, self.default_head),
            )
        return CommandResult(command="hdc", args=list(args), returncode=1)


def test_collect_faultlog_index_parses_names_dates_and_head():
    listing = _fixture("faultlog_listing.txt")
    head = _fixture("faultlog_cppcrash_head.txt")
    device = _FakeDevice(listing, {}, default_head=head)
    findings = collect_faultlog_index(device, BUNDLE, datetime(2026, 2, 1))
    names = [item.evidence["file"] for item in findings]
    assert names == [
        "cppcrash-com.zhihu.hmos-20260210-102000",
        "appfreeze-com.zhihu.hmos-20260210-101545",
        "cppcrash-com.zhihu.hmos-20260210-101530",
    ]
    assert [item.kind for item in findings] == [
        AnomalyKind.CPP_CRASH,
        AnomalyKind.APP_FREEZE,
        AnomalyKind.CPP_CRASH,
    ]
    assert all(item.severity == "critical" and item.source == "faultlog" for item in findings)
    first = findings[0]
    assert first.evidence["reason"] == "Signal:SIGSEGV(SEGV_MAPERR)@0x0000000000000000"
    assert first.evidence["process"] == BUNDLE
    assert first.evidence["top_frame"] == "#00 pc 0000000000123456 /system/lib64/libark.so"
    assert first.evidence["saved"] is None
    assert ("shell", "head", "-c", "4096", f"{FAULTLOG_DIR}/cppcrash-com.zhihu.hmos-20260210-102000") in device.calls


def test_collect_faultlog_index_excludes_other_bundle_and_old_dates():
    device = _FakeDevice(_fixture("faultlog_listing.txt"), {}, default_head="")
    findings = collect_faultlog_index(device, BUNDLE, datetime(2026, 2, 9))
    assert [item.evidence["file"] for item in findings] == [
        "cppcrash-com.zhihu.hmos-20260210-102000",
        "appfreeze-com.zhihu.hmos-20260210-101545",
        "cppcrash-com.zhihu.hmos-20260210-101530",
    ]
    only_new = collect_faultlog_index(device, BUNDLE, datetime(2026, 2, 11))
    assert only_new == []
    assert collect_faultlog_index(device, "", datetime(2026, 2, 1)) == []


def test_fetch_faultlog_evidence_persists_hits(tmp_path):
    head = _fixture("faultlog_cppcrash_head.txt")
    device = _FakeDevice(_fixture("faultlog_listing.txt"), {}, default_head=head)
    findings = fetch_faultlog_evidence(device, BUNDLE, datetime(2026, 2, 1), output_dir=tmp_path)
    saved = sorted(path.name for path in tmp_path.glob("faultlog_*.txt"))
    assert saved == [
        "faultlog_appfreeze-com.zhihu.hmos-20260210-101545.txt",
        "faultlog_cppcrash-com.zhihu.hmos-20260210-101530.txt",
        "faultlog_cppcrash-com.zhihu.hmos-20260210-102000.txt",
    ]
    assert findings[0].evidence["saved"] == "faultlog_cppcrash-com.zhihu.hmos-20260210-102000.txt"
    assert (tmp_path / "faultlog_cppcrash-com.zhihu.hmos-20260210-102000.txt").read_text(encoding="utf-8") == head


def test_faultlog_scan_survives_device_failure():
    class _Broken:
        def _run(self, *args, **kwargs):
            raise RuntimeError("hdc is not available")

    assert collect_faultlog_index(_Broken(), BUNDLE, datetime(2026, 2, 1)) == []
