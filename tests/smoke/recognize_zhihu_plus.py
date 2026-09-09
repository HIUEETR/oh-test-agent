from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mvp_phase1.backend import (  # noqa: E402
    DEFAULT_ABILITY,
    DEFAULT_BUNDLE,
    DEFAULT_DEVICE,
    PHASE_DIR,
    capture_remote_screenshot,
    compact_result,
    dump_layout,
    ensure_dir,
    hdc_device,
    iso_now,
    node_center,
    profile,
    serialize_node,
    timestamp,
    visible_nodes,
    walk_nodes,
    write_json,
)


def page_path(layout: dict) -> str:
    for node in walk_nodes(layout):
        value = (node.get("attributes") or {}).get("pagePath")
        if value:
            return value
    return "unknown"


def stable_elements(layout: dict) -> list[dict]:
    result = []
    for node in visible_nodes(layout):
        item = serialize_node(node)
        if not item["key"] and not item["id"] and not item["text"] and not item["description"]:
            continue
        if item["key"] or item["id"] or item["clickable"] or item["scrollable"]:
            result.append(item)
    return result


def signature(elements: list[dict], path: str) -> str:
    keys = []
    for item in elements:
        value = item.get("key") or item.get("id") or ""
        if not value or value.startswith(
            ("StatusBar", "Battery", "Signal", "Clock", "Live", "Key", "Keyboard", "Skin")
        ):
            continue
        value = re.sub(r"_\d{8,}$", "_*", value)
        if value not in keys:
            keys.append(value)
    keys.sort()
    types = sorted({item["type"] for item in elements if item.get("type") and not item["type"].startswith("TextClock")})
    return f"{path}|keys={','.join(keys[:36])}|types={','.join(types[:20])}"


def command_record(name: str, result: dict, out: Path) -> None:
    write_json(out / f"command_{name}.json", compact_result(result))


def stop_start(out: Path) -> list[dict]:
    bundle = profile().get("bundle_name", DEFAULT_BUNDLE)
    ability = profile().get("main_ability", DEFAULT_ABILITY)
    records = []
    stop = hdc_device("shell", "aa", "force-stop", bundle, timeout=30)
    command_record("force_stop", stop, out)
    records.append({"step": "force_stop", **compact_result(stop)})
    time.sleep(1)
    start = hdc_device("shell", "aa", "start", "-b", bundle, "-a", ability, timeout=30)
    command_record("start", start, out)
    records.append({"step": "start", **compact_result(start)})
    time.sleep(6)
    return records


def dump_page(out: Path, label: str) -> tuple[dict, list[dict]]:
    layout_path = out / f"{label}.json"
    layout = dump_layout(layout_path)
    elements = stable_elements(layout)
    write_json(out / f"{label}_elements.json", elements)
    screenshot = capture_remote_screenshot(f"/data/local/tmp/zhihu_phase1_{label}.png")
    command_record(f"screenshot_{label}", screenshot, out)
    return layout, elements


def find_by_key(layout: dict, prefix: str) -> dict | None:
    for node in visible_nodes(layout):
        attrs = node.get("attributes") or {}
        key = attrs.get("key") or attrs.get("id") or ""
        if key == prefix or key.startswith(prefix):
            return node
    return None


def clickable_feed_card(layout: dict) -> dict | None:
    candidates = []
    for node in visible_nodes(layout):
        attrs = node.get("attributes") or {}
        key = attrs.get("key") or attrs.get("id") or ""
        if attrs.get("clickable") == "true" and key.startswith("p2_home_feed_card_") and "block" not in key:
            center = node_center(serialize_node(node))
            if center:
                candidates.append((node, center))
    return candidates[0][0] if candidates else None


def editable_node(layout: dict) -> dict | None:
    for node in visible_nodes(layout):
        attrs = node.get("attributes") or {}
        type_name = attrs.get("type", "").lower()
        if any(token in type_name for token in ("input", "search", "textarea")):
            return node
    return None


def tap_node(layout: dict, node: dict, out: Path, name: str) -> dict:
    serialized = serialize_node(node)
    center = node_center(serialized)
    if not center:
        raise RuntimeError(f"node has no usable bounds: {serialized}")
    result = hdc_device("shell", "uitest", "uiInput", "click", str(center[0]), str(center[1]), timeout=30)
    command_record(name, result, out)
    time.sleep(3)
    return {"action": "click", "target": serialized, "center": center, **compact_result(result)}


def update_profile(summary: dict, element_inventory: list[dict]) -> None:
    path = ROOT / "target_app_profile.json"
    current = profile()
    existing = current.get("stable_locator_inventory", [])
    seen = {(item.get("key"), item.get("id"), item.get("type")) for item in existing}
    for item in element_inventory:
        key = (item.get("key"), item.get("id"), item.get("type"))
        if (
            (item.get("key") or item.get("id"))
            and key not in seen
            and (item.get("clickable") or item.get("scrollable") or item.get("editable"))
        ):
            existing.append(
                {
                    "name": item.get("key") or item.get("id"),
                    "key": item.get("key", ""),
                    "id": item.get("id", ""),
                    "type": item.get("type", ""),
                    "clickable": item.get("clickable", False),
                    "scrollable": item.get("scrollable", False),
                    "editable": item.get("editable", False),
                    "locator_priority": 1 if item.get("key") or item.get("id") else 2,
                    "source": "direct_observation",
                }
            )
            seen.add(key)
    current["stable_locator_inventory"] = existing
    current["observed_pages"] = summary.get("pages", [])
    current["last_observed_at"] = iso_now()
    current["status"] = "observed_unverified_install"
    write_json(path, current)


def run() -> int:
    parser = argparse.ArgumentParser(description="Direct HDC UI recognition and read-only flow validation for Zhihu++.")
    parser.add_argument("--runs", type=int, default=2, help="number of restart-and-observe runs")
    parser.add_argument("--no-click", action="store_true", help="only dump and recognize, do not inject clicks")
    args = parser.parse_args()

    run_id = timestamp()
    out = ensure_dir(PHASE_DIR / "recognition" / run_id)
    summary = {
        "run_id": run_id,
        "started_at": iso_now(),
        "device": DEFAULT_DEVICE,
        "bundle_name": profile().get("bundle_name", DEFAULT_BUNDLE),
        "main_ability": profile().get("main_ability", DEFAULT_ABILITY),
        "mode": "direct_hdc_recognition",
        "policy": "anonymous_read_only_restart_only",
        "runs": [],
        "pages": [],
        "actions": [],
        "warnings": ["screenshots are kept on the emulator when hdc file recv is unavailable"],
    }
    inventory: list[dict] = []

    for index in range(1, max(1, args.runs) + 1):
        round_dir = ensure_dir(out / f"round_{index}")
        round_info = {"round": index, "started_at": iso_now(), "actions": [], "pages": [], "warnings": []}
        home_after_back = None
        round_info["reset"] = stop_start(round_dir)
        layout, elements = dump_page(round_dir, "home_before_action")
        path = page_path(layout)
        page = {
            "id": f"round{index}_page1",
            "name": "首页信息流",
            "page_path": path,
            "signature": signature(elements, path),
            "elements_file": str(round_dir / "home_before_action_elements.json"),
        }
        round_info["pages"].append(page)
        inventory.extend(elements)

        if not args.no_click:
            search = find_by_key(layout, "p2_home_titlebar_search")
            if search:
                action = tap_node(layout, search, round_dir, "click_search")
                round_info["actions"].append(action)
                after_search, search_elements = dump_page(round_dir, "search_after_click")
                search_path = page_path(after_search)
                round_info["pages"].append(
                    {
                        "id": f"round{index}_page2",
                        "name": "搜索入口页",
                        "page_path": search_path,
                        "signature": signature(search_elements, search_path),
                        "elements_file": str(round_dir / "search_after_click_elements.json"),
                    }
                )
                inventory.extend(search_elements)
                edit = editable_node(after_search)
                if edit:
                    edit_data = serialize_node(edit)
                    center = node_center(edit_data)
                    if center:
                        input_result = hdc_device(
                            "shell",
                            "uitest",
                            "uiInput",
                            "inputText",
                            str(center[0]),
                            str(center[1]),
                            "OpenHarmony",
                            timeout=30,
                        )
                        command_record("input_keyword", input_result, round_dir)
                        round_info["actions"].append(
                            {
                                "action": "input_text",
                                "target": edit_data,
                                "text": "OpenHarmony",
                                **compact_result(input_result),
                            }
                        )
                        time.sleep(2)
                        after_input, input_elements = dump_page(round_dir, "search_after_input")
                        input_path = page_path(after_input)
                        round_info["pages"].append(
                            {
                                "id": f"round{index}_page3",
                                "name": "搜索输入状态",
                                "page_path": input_path,
                                "signature": signature(input_elements, input_path),
                                "elements_file": str(round_dir / "search_after_input_elements.json"),
                            }
                        )
                        inventory.extend(input_elements)
                    else:
                        round_info["warnings"].append("editable control has no bounds")
                else:
                    round_info["warnings"].append("search page did not expose an editable control")
                submit = hdc_device("shell", "uitest", "uiInput", "keyEvent", "Enter", timeout=30)
                command_record("submit_search", submit, round_dir)
                round_info["actions"].append({"action": "submit_search", **compact_result(submit)})
                time.sleep(4)
                after_submit, submit_elements = dump_page(round_dir, "search_after_submit")
                submit_path = page_path(after_submit)
                round_info["pages"].append(
                    {
                        "id": f"round{index}_page4",
                        "name": "搜索结果或提交状态",
                        "page_path": submit_path,
                        "signature": signature(submit_elements, submit_path),
                        "elements_file": str(round_dir / "search_after_submit_elements.json"),
                    }
                )
                inventory.extend(submit_elements)
                for back_index in range(1, 4):
                    back = hdc_device("shell", "uitest", "uiInput", "keyEvent", "Back", timeout=30)
                    command_record(f"back_to_home_{back_index}", back, round_dir)
                    round_info["actions"].append({"action": "back", "back_index": back_index, **compact_result(back)})
                    time.sleep(2)
                    candidate_layout, candidate_elements = dump_page(round_dir, f"after_back_{back_index}")
                    if find_by_key(candidate_layout, "p2_home_titlebar_search") and not find_by_key(
                        candidate_layout, "p2_search_input"
                    ):
                        home_after_back, back_elements = candidate_layout, candidate_elements
                        inventory.extend(back_elements)
                        break
                    if back_index == 3:
                        home_after_back, back_elements = candidate_layout, candidate_elements
                        inventory.extend(back_elements)

            card = clickable_feed_card(home_after_back or layout)
            if card:
                action = tap_node(home_after_back or layout, card, round_dir, "click_feed_card")
                round_info["actions"].append(action)
                detail, detail_elements = dump_page(round_dir, "detail_after_click")
                detail_path = page_path(detail)
                round_info["pages"].append(
                    {
                        "id": f"round{index}_page4",
                        "name": "内容详情页",
                        "page_path": detail_path,
                        "signature": signature(detail_elements, detail_path),
                        "elements_file": str(round_dir / "detail_after_click_elements.json"),
                    }
                )
                inventory.extend(detail_elements)
                back = hdc_device("shell", "uitest", "uiInput", "keyEvent", "Back", timeout=30)
                command_record("back_from_detail", back, round_dir)
                round_info["actions"].append({"action": "back", **compact_result(back)})
                time.sleep(2)
                restored, restored_elements = dump_page(round_dir, "home_restored")
                inventory.extend(restored_elements)
                round_info["assertions"] = [
                    {
                        "name": "returned_to_target_app",
                        "passed": page_path(restored) == page_path(layout),
                        "observed_page_path": page_path(restored),
                    },
                    {
                        "name": "visible_text_or_controls",
                        "passed": len(restored_elements) > 0,
                        "count": len(restored_elements),
                    },
                ]
            else:
                round_info["warnings"].append("no stable feed card was exposed for detail navigation")

        round_info["ended_at"] = iso_now()
        round_info["status"] = "observed" if not round_info.get("warnings") else "observed_with_warnings"
        write_json(round_dir / "round_summary.json", round_info)
        summary["runs"].append(round_info)
        summary["pages"].extend(round_info["pages"])
        summary["actions"].extend(round_info["actions"])

    summary["ended_at"] = iso_now()
    summary["page_count"] = len({item["signature"] for item in summary["pages"]})
    summary["interaction_types"] = sorted({item.get("action") for item in summary["actions"]})
    summary["repeatable"] = len(summary["runs"]) >= 2 and all(
        run.get("status") in {"observed", "observed_with_warnings"} for run in summary["runs"]
    )
    write_json(out / "recognition_summary.json", summary)
    update_profile(summary, inventory)
    print(
        json.dumps(
            {
                "summary": str(out / "recognition_summary.json"),
                "page_count": summary["page_count"],
                "interaction_types": summary["interaction_types"],
                "repeatable": summary["repeatable"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
