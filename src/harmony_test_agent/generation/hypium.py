from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..models import GeneratedArtifact, LocatorKind, RunTrace, TargetAppProfile, ToolName
from ..storage import ArtifactStore


class HypiumGenerator:
    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts

    def generate(self, trace: RunTrace, profile: TargetAppProfile) -> GeneratedArtifact:
        output_dir = self.artifacts.run_dir(trace.run_id) / "generated"
        safe_id = re.sub(r"[^a-zA-Z0-9_]", "_", trace.run_id)
        python_path = output_dir / f"test_{safe_id}.py"
        config_path = output_dir / f"test_{safe_id}.json"
        metadata_path = output_dir / "generation_metadata.json"
        warnings: list[str] = []
        body = self._render_actions(trace, profile, warnings)
        python_path.write_text(self._render_script(trace, profile, body), encoding="utf-8")
        config = {
            "schema_version": 1,
            "runner_mode": "driver",
            "case_id": safe_id,
            "device_id": trace.device_id,
            "target_app_id": profile.target_app_id,
            "bundle_name": profile.bundle_name,
            "main_ability": profile.main_ability,
            "reset_strategy": profile.reset_strategy,
            "report_dir": str((self.artifacts.run_dir(trace.run_id) / "hypium").resolve()),
            "timeout_seconds": 300,
            "generated_from_run_id": trace.run_id,
            "warnings": warnings,
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        metadata = {
            "run_id": trace.run_id,
            "python_sha256": self._sha256(python_path),
            "config_sha256": self._sha256(config_path),
            "source_action_count": len(trace.actions),
            "source_assertion_count": len(trace.assertions),
            "coordinate_fallbacks": sum(
                1 for action in trace.actions if action.locator and action.locator.kind == LocatorKind.COORDINATE
            ),
            "warnings": warnings,
        }
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return GeneratedArtifact(
            python_path=python_path.resolve(),
            config_path=config_path.resolve(),
            metadata_path=metadata_path.resolve(),
            warnings=warnings,
        )

    def _render_actions(
        self,
        trace: RunTrace,
        profile: TargetAppProfile,
        warnings: list[str],
    ) -> list[str]:
        lines: list[str] = []
        for action in trace.actions:
            if not action.success:
                continue
            tool = action.tool
            if tool in {ToolName.INSPECT_SCREEN, ToolName.FINISH}:
                continue
            if tool == ToolName.OPEN_APP:
                if profile.reset_strategy.get("kind") == "stop_then_start_only":
                    lines.append("        driver.stop_app(BUNDLE_NAME)")
                lines.append("        driver.start_app(BUNDLE_NAME, MAIN_ABILITY)")
            elif tool == ToolName.CLICK_ELEMENT:
                selector = self._selector(action.locator, action.params.get("target"), warnings)
                lines.append(f"        driver.touch({selector})")
            elif tool == ToolName.CLICK_COORDINATE:
                coordinate = action.params.get("coordinate")
                point = tuple(coordinate) if coordinate else (0, 0)
                lines.append(f"        driver.touch({point})  # coordinate fallback")
                warnings.append(f"{action.step_id}: click uses a coordinate fallback")
            elif tool == ToolName.INPUT_TEXT:
                selector = self._selector(action.locator, action.params.get("target") or "输入框", warnings)
                lines.append(f"        driver.input_text({selector}, {action.params.get('text', '')!r})")
            elif tool == ToolName.SWIPE:
                direction = action.params.get("direction") or "up"
                lines.append(f"        driver.swipe({direction!r})")
            elif tool == ToolName.BACK:
                lines.append("        driver.go_back()")
            elif tool == ToolName.WAIT:
                lines.append(f"        driver.wait({float(action.params.get('wait_seconds') or 1)!r})")
            elif tool in {ToolName.ASSERT_VISIBLE, ToolName.ASSERT_TEXT}:
                target = action.params.get("target") or action.params.get("text")
                selector = self._selector(action.locator, target, warnings)
                lines.append(f"        driver.check_component_exist({selector}, expect_exist=True)")
            elif tool == ToolName.ASSERT_NOT_VISIBLE:
                selector = self._selector(action.locator, action.params.get("target"), warnings)
                lines.append(f"        driver.check_component_exist({selector}, expect_exist=False)")
        if not any("check_component" in line for line in lines):
            stable = self._first_stable_locator(trace)
            if stable:
                lines.append(f"        driver.check_component_exist({stable}, expect_exist=True)")
                warnings.append("generated a fallback assertion from an observed stable locator")
            else:
                lines.append("        assert driver.device_sn, 'Hypium driver did not connect to a device'")
                warnings.append("no stable UI assertion was available; generated a connection assertion")
        return lines

    @staticmethod
    def _selector(locator, target: str | None, warnings: list[str]) -> str:
        if locator:
            if locator.kind in {LocatorKind.KEY, LocatorKind.ID}:
                method = "key" if locator.kind == LocatorKind.KEY else "id"
                dynamic = re.fullmatch(r"(.+_)\d{8,}", locator.value)
                if dynamic:
                    prefix = dynamic.group(1)
                    warnings.append(f"dynamic {method} {locator.value!r} generalized to prefix {prefix!r}")
                    return f"BY.{method}({prefix!r}, MatchPattern.STARTS_WITH)"
                return f"BY.{method}({locator.value!r})"
            if locator.kind == LocatorKind.TEXT:
                return f"BY.text({locator.value!r})"
            if locator.kind == LocatorKind.TYPE_TEXT:
                type_name, _, text = locator.value.partition("|")
                return f"BY.type({type_name!r}).text({text!r})"
        warnings.append(f"semantic target {target!r} fell back to exact text")
        return f"BY.text({target or ''!r})"

    @staticmethod
    def _first_stable_locator(trace: RunTrace) -> str | None:
        for snapshot in reversed(trace.snapshots):
            for element in snapshot.elements:
                if element.key and element.enabled and not re.search(r"_\d{8,}$", element.key):
                    return f"BY.key({element.key!r})"
                if element.content and element.enabled:
                    return f"BY.text({element.content!r})"
        return None

    @staticmethod
    def _render_script(trace: RunTrace, profile: TargetAppProfile, body: list[str]) -> str:
        actions = "\n".join(body) or "        pass"
        return f"""from __future__ import annotations

import json
import os
import traceback
from pathlib import Path

from hypium import BY, MatchPattern, UiDriver

DEVICE_ID = {trace.device_id!r}
BUNDLE_NAME = {profile.bundle_name!r}
MAIN_ABILITY = {profile.main_ability!r}
DEFAULT_REPORT_DIR = Path(__file__).resolve().parent.parent / "reports"
REPORT_DIR = Path(os.environ.get("HARMONY_AGENT_REPORT_DIR", DEFAULT_REPORT_DIR))
RESULT_PATH = REPORT_DIR / "generated_result.json"


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    result = {{"run_id": {trace.run_id!r}, "passed": False, "error": None}}
    driver = None
    try:
        driver = UiDriver.connect(device_sn=DEVICE_ID, report_path=str(REPORT_DIR), log_level="info")
{actions}
        driver.capture_screen(str(REPORT_DIR / "final.jpeg"))
        result["passed"] = True
        return 0
    except Exception as exc:
        result["error"] = f"{{type(exc).__name__}}: {{exc}}"
        result["traceback"] = traceback.format_exc()
        if driver is not None:
            try:
                driver.capture_screen(str(REPORT_DIR / "failure.jpeg"))
            except Exception:
                pass
        return 1
    finally:
        RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
"""

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()
