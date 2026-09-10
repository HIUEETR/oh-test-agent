"""把已执行的任务轨迹转换为可审计的 Hypium Driver 回放产物。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..models import ActionResult, GeneratedArtifact, LocatorKind, RunState, RunTrace, TargetAppProfile, ToolName
from ..storage import ArtifactStore


@dataclass
class GenerationCoverage:
    """累积生成动作、断言和被省略动作的数量与原因。"""

    lines: list[str] = field(default_factory=list)
    omitted_actions: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    generated_actions: int = 0
    generated_assertions: int = 0
    explicit_assertions: int = 0
    coordinate_fallbacks: int = 0

    def omit(self, action: ActionResult, reason: str) -> None:
        self.omitted_actions.append({"step_id": action.step_id, "tool": str(action.tool), "reason": reason})


class HypiumGenerator:
    """从轨迹生成确定性初始化、业务步骤、断言及完整性元数据。"""

    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts

    def generate(self, trace: RunTrace, profile: TargetAppProfile) -> GeneratedArtifact:
        """生成回放文件；只有完整成功轨迹会获得 acceptance 和回放资格。"""
        output_dir = self.artifacts.run_dir(trace.run_id) / "generated"
        safe_id = re.sub(r"[^a-zA-Z0-9_]", "_", trace.run_id)
        python_path = output_dir / f"test_{safe_id}.py"
        config_path = output_dir / f"test_{safe_id}.json"
        metadata_path = output_dir / "generation_metadata.json"
        coverage = self._render_actions(trace)
        outcome = self._agent_outcome(trace)
        incomplete_reasons = self._incomplete_reasons(trace, outcome, coverage)
        replay_eligible = not incomplete_reasons
        purpose = "acceptance" if replay_eligible else "diagnostic"
        counts = {
            "source_actions": len(trace.actions),
            "source_assertions": len(trace.assertions),
            "generated_actions": coverage.generated_actions,
            "generated_assertions": coverage.generated_assertions,
            "omitted_actions": len(coverage.omitted_actions),
            "failed_actions": sum(not action.success for action in trace.actions),
            "coordinate_fallbacks": coverage.coordinate_fallbacks,
        }
        python_path.write_text(self._render_script(trace, profile, coverage.lines), encoding="utf-8")
        config = {
            "schema_version": 2,
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
            "purpose": purpose,
            "replay_eligible": replay_eligible,
            "warnings": coverage.warnings,
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        metadata = {
            "schema_version": 2,
            "run_id": trace.run_id,
            "python_sha256": self._sha256(python_path),
            "config_sha256": self._sha256(config_path),
            "purpose": purpose,
            "replay_eligible": replay_eligible,
            "source_agent_outcome": outcome,
            "source_action_count": len(trace.actions),
            "included_action_count": coverage.generated_actions + coverage.generated_assertions,
            "omitted_action_count": len(coverage.omitted_actions),
            "counts": counts,
            "omitted_actions": coverage.omitted_actions,
            "incomplete_reasons": incomplete_reasons,
            "warnings": coverage.warnings,
        }
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return GeneratedArtifact(
            python_path=python_path.resolve(),
            config_path=config_path.resolve(),
            metadata_path=metadata_path.resolve(),
            purpose=purpose,
            replay_eligible=replay_eligible,
            source_agent_outcome=outcome,
            source_action_count=len(trace.actions),
            included_action_count=coverage.generated_actions + coverage.generated_assertions,
            omitted_action_count=len(coverage.omitted_actions),
            counts=counts,
            incomplete_reasons=incomplete_reasons,
            warnings=coverage.warnings,
        )

    def _render_actions(self, trace: RunTrace) -> GenerationCoverage:
        coverage = GenerationCoverage()
        for action in trace.actions:
            if not action.success:
                coverage.omit(action, action.error or "source action failed")
                continue
            tool = action.tool
            if tool == ToolName.OPEN_APP:
                coverage.omit(action, "OPEN_APP is replaced by deterministic stop/start/wait setup")
            elif tool in {ToolName.INSPECT_SCREEN, ToolName.FINISH}:
                coverage.omit(action, f"{tool} is an agent-control action")
            elif tool == ToolName.CLICK_ELEMENT and self._is_nondeterministic_system_click(trace, action):
                coverage.omit(action, "desktop AppIcon launch click is replaced by deterministic app setup")
            elif tool == ToolName.CLICK_ELEMENT:
                coordinate = self._runtime_element_coordinate(trace, action)
                if action.locator and action.locator.kind in {LocatorKind.SPATIAL, LocatorKind.VLM_BBOX} and coordinate:
                    target = action.params.get("target")
                    coverage.lines.append(f"        driver.touch({coordinate!r})  # coordinate fallback for {target!r}")
                    coverage.warnings.append(
                        f"{action.step_id}: runtime element {target!r} uses coordinate {coordinate}"
                    )
                    coverage.coordinate_fallbacks += 1
                else:
                    selector = self._selector(action.locator, action.params.get("target"), coverage.warnings)
                    coverage.lines.append(f"        driver.touch({selector})")
                coverage.generated_actions += 1
            elif tool == ToolName.CLICK_COORDINATE:
                coordinate = action.params.get("coordinate")
                point = tuple(coordinate) if coordinate else (0, 0)
                coverage.lines.append(f"        driver.touch({point})  # coordinate fallback")
                coverage.warnings.append(f"{action.step_id}: click uses a coordinate fallback")
                coverage.coordinate_fallbacks += 1
                coverage.generated_actions += 1
            elif tool == ToolName.INPUT_TEXT:
                selector = self._selector(action.locator, action.params.get("target") or "输入框", coverage.warnings)
                coverage.lines.append(f"        driver.input_text({selector}, {action.params.get('text', '')!r})")
                coverage.generated_actions += 1
            elif tool == ToolName.SWIPE:
                coverage.lines.append(f"        driver.swipe({(action.params.get('direction') or 'up')!r})")
                coverage.generated_actions += 1
            elif tool == ToolName.BACK:
                coverage.lines.append("        driver.go_back()")
                coverage.generated_actions += 1
            elif tool == ToolName.WAIT:
                coverage.lines.append(f"        driver.wait({float(action.params.get('wait_seconds') or 1)!r})")
                coverage.generated_actions += 1
            elif tool in {ToolName.ASSERT_VISIBLE, ToolName.ASSERT_TEXT}:
                target = action.params.get("target") or action.params.get("text")
                coverage.lines.append(
                    "        driver.check_component_exist("
                    f"{self._selector(action.locator, target, coverage.warnings)}, expect_exist=True)"
                )
                coverage.generated_assertions += 1
                coverage.explicit_assertions += 1
            elif tool == ToolName.ASSERT_NOT_VISIBLE:
                selector = self._selector(action.locator, action.params.get("target"), coverage.warnings)
                coverage.lines.append(f"        driver.check_component_exist({selector}, expect_exist=False)")
                coverage.generated_assertions += 1
                coverage.explicit_assertions += 1
            else:
                coverage.omit(action, f"unsupported replay tool: {tool}")
        if coverage.explicit_assertions == 0:
            coverage.warnings.append("source trace has no successful explicit assertion")
            stable = self._first_stable_locator(trace)
            if stable:
                coverage.lines.append(f"        driver.check_component_exist({stable}, expect_exist=True)")
                coverage.warnings.append("generated a fallback assertion from an observed stable locator")
                coverage.generated_assertions += 1
            else:
                coverage.warnings.append("no stable UI assertion was available")
        return coverage

    @staticmethod
    def _agent_outcome(trace: RunTrace) -> str:
        if trace.agent_outcome != "unknown":
            return trace.agent_outcome
        if trace.state == RunState.COMPLETED:
            return "completed"
        if trace.state == RunState.STOPPED_BY_USER:
            return "stopped"
        if trace.error or any(not action.success for action in trace.actions):
            return "failed"
        if trace.actions and trace.actions[-1].tool == ToolName.FINISH and trace.actions[-1].success:
            return "completed"
        return "unknown"

    @staticmethod
    def _incomplete_reasons(trace: RunTrace, outcome: str, coverage: GenerationCoverage) -> list[str]:
        reasons: list[str] = []
        if outcome != "completed":
            reasons.append(f"source agent outcome is {outcome}")
        if trace.agent_error:
            reasons.append(f"source agent error: {trace.agent_error}")
        if any(not action.success for action in trace.actions):
            reasons.append("source trace contains failed actions")
        if not trace.actions or trace.actions[-1].tool != ToolName.FINISH or not trace.actions[-1].success:
            reasons.append("source trace does not end with a successful FINISH action")
        if coverage.explicit_assertions == 0:
            reasons.append("source trace has no successful explicit assertion")
        unsupported = [
            item for item in coverage.omitted_actions if item["reason"].startswith("unsupported replay tool")
        ]
        if unsupported:
            reasons.append("source trace contains unsupported replay actions")
        return list(dict.fromkeys(reasons))

    @staticmethod
    def _is_nondeterministic_system_click(trace: RunTrace, action: ActionResult) -> bool:
        target = str(action.params.get("target") or "")
        if "appicon" in target.lower():
            return True
        if not action.before_snapshot_id:
            return False
        snapshot = next((item for item in trace.snapshots if item.snapshot_id == action.before_snapshot_id), None)
        if snapshot is None:
            return False
        element = next((item for item in snapshot.elements if item.element_id == target), None)
        if element is None:
            return False
        launch_markers = " ".join(
            value or "" for value in (element.type, element.source, element.key, element.id, element.content)
        ).lower()
        return (
            "appicon" in launch_markers
            or "keyhidekbd" in launch_markers
            or (
                "launcher" in snapshot.page_path.lower()
                and trace.target_app_id.replace("-", "") in launch_markers.replace("-", "")
            )
        )

    @staticmethod
    def _runtime_element_coordinate(trace: RunTrace, action: ActionResult) -> tuple[int, int] | None:
        target = action.params.get("target")
        if not target or not action.before_snapshot_id:
            return None
        snapshot = next((item for item in trace.snapshots if item.snapshot_id == action.before_snapshot_id), None)
        if snapshot is None:
            return None
        element = next((item for item in snapshot.elements if item.element_id == target), None)
        return element.bbox.center if element and element.bbox else None

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
        startup_wait = float(profile.launch_strategy.get("wait_seconds", 2))
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
STARTUP_WAIT_SECONDS = {startup_wait!r}
DEFAULT_REPORT_DIR = Path(__file__).resolve().parent.parent / "reports"
REPORT_DIR = Path(os.environ.get("HARMONY_AGENT_REPORT_DIR", DEFAULT_REPORT_DIR))
RESULT_PATH = REPORT_DIR / "generated_result.json"


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    result = {{"run_id": {trace.run_id!r}, "passed": False, "error": None}}
    driver = None
    try:
        driver = UiDriver.connect(device_sn=DEVICE_ID, report_path=str(REPORT_DIR), log_level="info")
        driver.stop_app(BUNDLE_NAME)
        driver.start_app(BUNDLE_NAME, MAIN_ABILITY)
        driver.wait(STARTUP_WAIT_SECONDS)
{actions}
        driver.capture_screen(str(REPORT_DIR / "final.jpeg"))
        result["passed"] = True
        return 0
    except Exception as exc:
        result["error"] = {{"type": type(exc).__name__, "message": str(exc)}}
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
