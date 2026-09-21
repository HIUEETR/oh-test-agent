"""DC 模式用例生成：录制调用 → 用例 IR → 独立 UiDriver 脚本。

2026-09 用例 IR 重构后，本模块只负责**落盘与 DC 特有的资格判定**；
工具映射与脚本渲染分别由 ``cases/builder.py`` 与 ``generation/standalone.py`` 承担，
因此 Live 与 DC 产物共用同一套模板、定位器策略与检查点原语。

保持不变的契约（``tests/unit/test_dc_generator.py`` 逐条断言）：
脚本落盘路径、``dc_test_*.json`` 的键值、warning/omitted 文案、
``replay_eligible`` 的**可执行性**语义（缺可回放动作或身份占位才为 False）、``# skipped: ...`` 与
``pass  # no replayable operations`` 字面量。
"""

from __future__ import annotations

import json
import re

from ..cases.builder import NON_REPLAYABLE_DC_TOOLS, CaseBuilder, CaseBuildResult, format_args
from ..cases.safety import validate_case_spec
from ..generation.standalone import StandaloneEmitter
from ..models import ScreenSnapshot, utc_now
from ..storage.artifacts import ArtifactStore
from .models import DcScriptArtifact, DcToolInvocation, DcToolName

#: 兼容视图：既有契约测试（``tests/api/test_dc_contract.py``）直接导入这两个名字。
#: 权威分类已迁移到 ``cases/builder.py`` 的 IR 构建侧，这里只把字符串集合投影回枚举。
_NON_REPLAYABLE: frozenset[DcToolName] = frozenset(tool for tool in DcToolName if tool.value in NON_REPLAYABLE_DC_TOOLS)
_REPLAYABLE_MAP: dict[DcToolName, DcToolName] = {
    tool: tool for tool in DcToolName if tool.value not in NON_REPLAYABLE_DC_TOOLS
}

#: 生成 ``case_spec.json`` 时使用的文件名。
CASE_SPEC_FILENAME = "case_spec.json"

#: 兼容别名：``dc/session.py`` 历史上从本模块导入 ``_format_args``。
_format_args = format_args

__all__ = ["CaseBuildResult", "DcHypiumGenerator", "format_args"]


class DcHypiumGenerator:
    """从 DC 会话录制的操作生成 Hypium Python 脚本。"""

    def __init__(self, artifacts: ArtifactStore, min_observed_rounds: int = 3):
        self.artifacts = artifacts
        # 动态 key 前缀泛化所需的跨轮观察门槛；由调用方注入
        # settings.profile_verification_rounds（精简后默认 1 轮）。
        self.min_observed_rounds = max(int(min_observed_rounds), 1)

    # ------------------------------------------------------------------
    # IR 构建（供用例库与 API 复用）
    # ------------------------------------------------------------------

    def build(
        self,
        session_id: str,
        device_id: str,
        invocations: list[DcToolInvocation],
        snapshots: list[ScreenSnapshot] | None = None,
        bundle_name: str = "com.example.app",
        main_ability: str = "EntryAbility",
    ) -> CaseBuildResult:
        """把录制的工具调用构建为用例 IR（不做任何落盘）。

        DC 从不注入兜底断言：模型没调用 ``assert_*`` 时用例只能是诊断用途，
        因此 ``inject_fallback_assertion=False``。
        """
        builder = CaseBuilder(min_observed_rounds=self.min_observed_rounds, inject_fallback_assertion=False)
        return builder.from_dc_invocations(
            session_id,
            device_id,
            invocations,
            bundle_name=bundle_name,
            main_ability=main_ability,
            snapshots=snapshots,
        )

    def generate(
        self,
        session_id: str,
        device_id: str,
        invocations: list[DcToolInvocation],
        snapshots: list[ScreenSnapshot] | None = None,
        bundle_name: str = "com.example.app",
        main_ability: str = "EntryAbility",
    ) -> DcScriptArtifact:
        """从录制的工具调用生成 Hypium 脚本。

        Args:
            session_id: DC 会话 ID（``dc-`` 前缀）。
            device_id: 设备序列号。
            invocations: 录制的工具调用列表。
            snapshots: 可选的截图列表（用于元素解析）。
            bundle_name: 目标应用 bundle name（用于脚本头部）。
            main_ability: 目标应用 ability name。

        Returns:
            DcScriptArtifact 包含脚本路径、源码、警告和省略操作。
        """
        built = self.build(
            session_id,
            device_id,
            invocations,
            snapshots=snapshots,
            bundle_name=bundle_name,
            main_ability=main_ability,
        )
        warnings = list(built.warnings)
        # 纵深防御：IR 静态安全门禁违规不硬失败，只把用例降级为 draft 并追加警告。
        violations = validate_case_spec(built.spec, max_iterations=5000)
        if violations:
            built.spec.status = "draft"
            warnings += [f"case safety downgrade: {item}" for item in violations]
        rendered = StandaloneEmitter().render(built.spec, run_id=session_id, device_id=device_id)

        output_dir = self.artifacts.run_dir(session_id) / "generated"
        output_dir.mkdir(parents=True, exist_ok=True)
        safe_id = _safe_id(session_id)
        python_path = output_dir / f"dc_test_{safe_id}.py"
        python_path.write_text(rendered.python_text, encoding="utf-8")

        config_path = output_dir / f"dc_test_{safe_id}.json"
        config = {
            # 历史 schema：键与取值逐字保持（storage/script_catalog.py 读 case_id）。
            "schema_version": 2,
            "runner_mode": "driver",
            "case_id": safe_id,
            "device_id": device_id,
            "target_app_id": f"dc-{session_id}",
            "bundle_name": bundle_name,
            "main_ability": main_ability,
            "purpose": "acceptance" if built.replay_eligible else "dc_recording",
            "replay_eligible": built.replay_eligible,
            "confidence": built.confidence,
            "confidence_factors": built.confidence_factors,
            "promotion_eligible": built.promotion_eligible,
            "runnable_blockers": built.runnable_blockers,
            "explicit_assertions": built.explicit_assertions,
            "generated_from_session_id": session_id,
            "included_operations": built.counts["generated_actions"],
            "omitted_operations": len(built.omitted_actions),
            "warnings": warnings,
        }
        config.update(rendered.config)
        config["ir_case_id"] = built.spec.case_id
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        case_spec_path = output_dir / CASE_SPEC_FILENAME
        case_spec_path.write_text(
            json.dumps(built.spec.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        return DcScriptArtifact(
            python_path=str(python_path.resolve()),
            python_text=rendered.python_text,
            warnings=warnings,
            generated_at=utc_now(),
            included_operations=built.counts["generated_actions"],
            omitted_operations=built.omitted_actions,
            replay_eligible=built.replay_eligible,
            confidence=built.confidence,
            confidence_factors=built.confidence_factors,
            promotion_eligible=built.promotion_eligible,
            runnable_blockers=built.runnable_blockers,
            explicit_assertions=built.explicit_assertions,
            case_id=built.spec.case_id,
            case_spec_path=str(case_spec_path.resolve()),
            xdevice_project_path=None,
        )


def _safe_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", value)
