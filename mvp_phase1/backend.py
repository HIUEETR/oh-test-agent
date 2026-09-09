"""第一阶段 HDC 冒烟验证的 legacy 公共函数；为兼容历史脚本和证据格式而保留。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PHASE_DIR = ROOT / "artifacts" / "phase1"
PROFILE_PATH = ROOT / "target_app_profile.json"
DEFAULT_DEVICE = "127.0.0.1:5555"
DEFAULT_BUNDLE = "com.github.zhuoyi233.zhplus"
DEFAULT_ABILITY = "EntryAbility"


def timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def iso_now() -> str:
    return datetime.now(UTC).isoformat()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def find_hdc() -> str:
    configured = os.environ.get("HDC_PATH")
    if configured and Path(configured).exists():
        return configured
    candidates = [
        ROOT.parent / "devecostudio" / "sdk" / "default" / "openharmony" / "toolchains" / "hdc.exe",
        Path(found) if (found := shutil.which("hdc")) else None,
    ]
    for candidate in candidates:
        if candidate and candidate.exists():
            return str(candidate)
    return "hdc"


def run_command(args: list[str], *, timeout: float = 30, cwd: Path | None = None) -> dict[str, Any]:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd or ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return {
            "command": subprocess.list2cmdline(args),
            "args": args,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "timed_out": False,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": subprocess.list2cmdline(args),
            "args": args,
            "returncode": None,
            "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "") if isinstance(exc.stderr, str) else "",
            "timed_out": True,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
    except OSError as exc:
        return {
            "command": subprocess.list2cmdline(args),
            "args": args,
            "returncode": None,
            "stdout": "",
            "stderr": repr(exc),
            "timed_out": False,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }


def hdc(*args: str, timeout: float = 30) -> dict[str, Any]:
    return run_command([find_hdc(), *args], timeout=timeout, cwd=ROOT)


def hdc_device(*args: str, timeout: float = 30) -> dict[str, Any]:
    return hdc("-t", os.environ.get("HARMONY_DEVICE", DEFAULT_DEVICE), *args, timeout=timeout)


def save_command_evidence(path: Path, result: dict[str, Any]) -> None:
    path.write_text(
        "COMMAND\n"
        + result.get("command", "")
        + "\n\nSTDOUT\n"
        + result.get("stdout", "")
        + "\nSTDERR\n"
        + result.get("stderr", "")
        + "\n",
        encoding="utf-8",
    )


def parse_bounds(value: str | None) -> tuple[int, int, int, int] | None:
    if not value:
        return None
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", value.strip())
    if not match:
        return None
    return tuple(int(item) for item in match.groups())  # type: ignore[return-value]


def walk_nodes(root: dict[str, Any]) -> Iterable[dict[str, Any]]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.get("children") or []))


def visible_nodes(root: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for node in walk_nodes(root):
        attrs = node.get("attributes") or {}
        if attrs.get("visible", "true") != "true":
            continue
        if (
            any(attrs.get(key) for key in ("key", "id", "text", "description"))
            or attrs.get("clickable") == "true"
            or attrs.get("scrollable") == "true"
        ):
            result.append(node)
    return result


def serialize_node(node: dict[str, Any]) -> dict[str, Any]:
    attrs = dict(node.get("attributes") or {})
    type_name = attrs.get("type", "")
    return {
        "text": attrs.get("text") or attrs.get("originalText") or "",
        "type": type_name,
        "key": attrs.get("key", ""),
        "id": attrs.get("id", ""),
        "description": attrs.get("description", ""),
        "clickable": attrs.get("clickable") == "true",
        "editable": type_name in {"TextInput", "Search", "Input"} or "input" in type_name.lower(),
        "scrollable": attrs.get("scrollable") == "true",
        "enabled": attrs.get("enabled") != "false",
        "selected": attrs.get("selected") == "true",
        "bounds": attrs.get("bounds", ""),
        "page_path": attrs.get("pagePath", ""),
        "hierarchy": attrs.get("hierarchy", ""),
        "source": "hdc_uitest_dumpLayout",
    }


def dump_layout(output_path: Path) -> dict[str, Any]:
    result = hdc_device("shell", "uitest", "dumpLayout", "-a", timeout=30)
    output = (result.get("stdout", "") + "\n" + result.get("stderr", "")).strip()
    match = re.search(r"DumpLayout saved to:\s*(\S+)", output)
    if not match:
        raise RuntimeError(f"dumpLayout did not return a remote path: {output[-1000:]}")
    remote_path = match.group(1)
    cat = hdc_device("shell", "cat", remote_path, timeout=30)
    text = cat.get("stdout", "").strip()
    if not text:
        raise RuntimeError(f"empty layout from {remote_path}: {cat.get('stderr', '')}")
    data = json.loads(text)
    write_json(output_path, data)
    return data


def capture_remote_screenshot(remote_path: str = "/data/local/tmp/zhihu_phase1.png") -> dict[str, Any]:
    return hdc_device("shell", "uitest", "screenCap", "-p", remote_path, timeout=30)


def profile() -> dict[str, Any]:
    return read_json(PROFILE_PATH, {}) or {}


def node_center(node: dict[str, Any]) -> tuple[int, int] | None:
    bounds = parse_bounds(node.get("bounds"))
    if not bounds:
        return None
    left, top, right, bottom = bounds
    return ((left + right) // 2, (top + bottom) // 2)


def compact_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "command": result.get("command"),
        "returncode": result.get("returncode"),
        "timed_out": result.get("timed_out", False),
        "duration_ms": result.get("duration_ms"),
        "stdout_tail": result.get("stdout", "")[-4000:],
        "stderr_tail": result.get("stderr", "")[-4000:],
    }
