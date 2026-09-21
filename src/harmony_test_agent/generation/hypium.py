"""把已执行的任务轨迹转换为可审计的 Hypium Driver 回放产物。

2026-09 用例 IR 重构：本模块不再自己拼脚本行，而是

``RunTrace`` ──► ``CaseBuilder.from_trace`` ──► ``TestCaseSpec`` ──► ``StandaloneEmitter``

产物路径、``generated/test_*.json`` 的 schema、``generation_metadata.json`` 的 key 与
``generated_result.json`` 的契约**全部保持原样**（``runner/hypium.py`` 与 Profile
晋级门禁依赖它们），新增内容只有 ``generated/case_spec.json`` 与若干追加字段。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ..cases.builder import CaseBuilder, CaseBuildResult
from ..cases.spec import LocatorSpec
from ..models import GeneratedArtifact, LocatorCandidate, RunTrace, TargetAppProfile
from ..storage import ArtifactStore
from .selectors import render_selector

#: 生成 ``generated/case_spec.json`` 时使用的 IR schema 版本。
CASE_SPEC_FILENAME = "case_spec.json"


class HypiumGenerator:
    """从轨迹生成确定性初始化、业务步骤、断言及完整性元数据。"""

    def __init__(self, artifacts: ArtifactStore, min_observed_rounds: int = 3):
        self.artifacts = artifacts
        # 动态 key 前缀泛化所需的跨轮观察门槛；资产流水线精简后由
        # settings.profile_verification_rounds 注入（默认 1 轮），此处默认 3 保持向后兼容。
        self.min_observed_rounds = max(int(min_observed_rounds), 1)

    # ------------------------------------------------------------------
    # IR 构建（供 Live 生成、用例库与 API 复用）
    # ------------------------------------------------------------------

    def build(self, trace: RunTrace, profile: TargetAppProfile) -> CaseBuildResult:
        """把轨迹构建为用例 IR（不做任何落盘）。"""
        return CaseBuilder(min_observed_rounds=self.min_observed_rounds).from_trace(trace, profile)

    def generate(self, trace: RunTrace, profile: TargetAppProfile | None = None) -> GeneratedArtifact:
        """从冻结 Profile 生成脚本；旧 Trace 只允许显式提供一次兼容快照。"""
        frozen = trace.profile_snapshot
        if frozen is None and profile is not None:
            trace.profile_snapshot = profile.model_copy(deep=True)
            frozen = trace.profile_snapshot
        if frozen is None:
            raise ValueError("RunTrace does not contain a frozen profile_snapshot")
        profile = frozen
        output_dir = self.artifacts.run_dir(trace.run_id) / "generated"
        safe_id = _safe_id(trace.run_id)
        python_path = output_dir / f"test_{safe_id}.py"
        config_path = output_dir / f"test_{safe_id}.json"
        metadata_path = output_dir / "generation_metadata.json"
        case_spec_path = output_dir / CASE_SPEC_FILENAME

        built = self.build(trace, profile)
        # 延迟 import：Phase 2 的官方 xdevice 产物与 IR 产物同源，但保持本模块可独立加载。
        from .standalone import StandaloneEmitter

        rendered = StandaloneEmitter().render(built.spec, run_id=trace.run_id, device_id=trace.device_id)
        outcome = built.source_agent_outcome
        purpose = built.purpose
        counts = built.counts
        incomplete_reasons = built.incomplete_reasons
        replay_eligible = built.replay_eligible
        warnings = built.warnings
        omitted_actions = built.omitted_actions
        explicit_assertions = built.explicit_assertions

        python_path.write_text(rendered.python_text, encoding="utf-8")
        config: dict[str, Any] = {
            # 历史 schema：键与取值必须逐字保持不变（storage/script_catalog.py 读取 case_id）。
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
            "warnings": warnings,
            "application_assertion_count": explicit_assertions,
            "validated_resolutions": profile.device_compatibility.validated_resolutions,
        }
        # 追加：用例 IR 侧字段（ir_case_id 避免覆盖历史的脚本 case_id）。
        config.update(rendered.config)
        config["ir_case_id"] = built.spec.case_id
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
            "included_action_count": counts["generated_actions"] + counts["generated_assertions"],
            "omitted_action_count": len(omitted_actions),
            "counts": counts,
            "omitted_actions": omitted_actions,
            "incomplete_reasons": incomplete_reasons,
            "warnings": warnings,
            "application_assertion_count": explicit_assertions,
            "coordinate_constraints": profile.device_compatibility.model_dump(mode="json"),
            "case_id": built.spec.case_id,
            "case_spec_path": CASE_SPEC_FILENAME,
            "scenario": str(built.spec.scenario),
            "hard_checkpoint_count": config["hard_checkpoint_count"],
        }
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        case_spec_path.write_text(
            json.dumps(built.spec.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return GeneratedArtifact(
            python_path=python_path.resolve(),
            config_path=config_path.resolve(),
            metadata_path=metadata_path.resolve(),
            purpose=purpose,
            replay_eligible=replay_eligible,
            source_agent_outcome=outcome,
            source_action_count=len(trace.actions),
            included_action_count=counts["generated_actions"] + counts["generated_assertions"],
            omitted_action_count=len(omitted_actions),
            counts=counts,
            incomplete_reasons=incomplete_reasons,
            warnings=warnings,
            case_spec_path=case_spec_path.resolve(),
            case_id=built.spec.case_id,
        )

    # ------------------------------------------------------------------
    # 兼容薄壳：既有调用方仍可拿到同一套模板与选择器
    # ------------------------------------------------------------------

    @staticmethod
    def _render_script(trace: RunTrace, profile: TargetAppProfile, body: list[str]) -> str:
        """以预渲染脚本体复用统一的脚本文档模板。

        DC 生成器已切到 ``CaseBuilder`` + ``StandaloneEmitter`` 的 IR 路径；本方法保留
        给仍持有「已渲染行」的外部调用方，避免两套脚本模板漂移。
        """
        from .standalone import StandaloneEmitter

        spec = StandaloneEmitter.shell_spec(
            bundle_name=profile.bundle_name,
            main_ability=profile.main_ability,
            startup_wait_seconds=float(profile.launch_strategy.get("wait_seconds", 2)),
            device_sn=trace.device_id,
            title_zh=trace.task or "未命名用例",
        )
        return StandaloneEmitter().render_document(spec, body, run_id=trace.run_id, device_id=trace.device_id)

    @staticmethod
    def _selector(
        locator: LocatorCandidate | None,
        target: str | None,
        warnings: list[str],
        profile: TargetAppProfile | None,
        min_observed_rounds: int = 3,
    ) -> str:
        """把运行时候选定位器渲染为 hypium 选择器源码文本（薄壳，走 IR 定位器策略）。"""
        builder = CaseBuilder(min_observed_rounds=min_observed_rounds)
        spec: LocatorSpec = builder.locator_from_candidate(locator, target, profile, warnings)
        return render_selector(spec)

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", value)
