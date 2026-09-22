"""统一脚本目录：扫描运行产物中的 Hypium 脚本（Live 运行 + 直流会话）。

「Hypium Python 脚本」Tab 需要把两类脚本放在同一个可选列表里：

- Live 运行：``<runtime_dir>/run-*/generated/test_*.py``
- 直流模式：``<runtime_dir>/dc-*/generated/dc_test_*.py``

目录只读、不做缓存：脚本数量与产物目录同量级（几十个），每次列目录都能反映
最新生成结果。所有路径访问都经过 :meth:`ScriptCatalog.resolve` 的穿越检查。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

# 生成目录名与文件后缀（与 generation/hypium.py、dc/generator.py 保持一致）
GENERATED_DIR = "generated"
SCRIPT_SUFFIX = ".py"
# Live 运行的生成元数据（比 config 多出动作计数与不合格原因）
_METADATA_NAME = "generation_metadata.json"
# 用例 IR 产物（2026-09 用例库）：存在时给目录项补上用例身份字段。
_CASE_SPEC_NAME = "case_spec.json"
_XDEVICE_DIR = "xdevice"


class ScriptCatalogEntry(BaseModel):
    """脚本列表项：路径 + 两侧 config/metadata 的公共字段。"""

    script_id: str  # "<run_id>/generated/<file>.py"（相对 runtime_dir 的 POSIX 路径）
    run_id: str
    source: Literal["run", "dc"]
    filename: str
    python_path: str
    case_id: str | None = None
    """脚本级 ID（config 的 ``case_id``，即 ``safe_id``）；与用例 IR 的 ID 不同。"""
    ir_case_id: str | None = None
    """用例 IR 的 ``case_id``（``case_spec.json``）；无用例产物时为 ``None``。"""
    scenario: str | None = None
    """用例场景（``core_flow`` / ``bug_reproduction`` / ``stress`` / …）。"""
    case_version: int | None = None
    """用例 IR 的 ``schema_version``；缺 ``case_spec.json`` 时为 ``None``。"""
    xdevice_project: str | None = None
    """官方 devicetest 工程目录（存在时为绝对路径）。"""
    bundle_name: str | None = None
    main_ability: str | None = None
    purpose: str | None = None
    replay_eligible: bool = False
    """脚本是否可执行（runnable）；不代表质量合格——质量看 ``confidence``。"""
    confidence: str | None = None
    """质量分档 ``high`` / ``medium`` / ``low``；旧产物缺该键时为 ``None``。"""
    confidence_factors: list[str] = Field(default_factory=list)
    """质量顾虑清单（非阻断）；旧产物回退到 ``incomplete_reasons``。"""
    promotion_eligible: bool = False
    """能否作为 Profile 晋级证据；旧产物缺该键时为 ``False``。"""
    runnable_blockers: list[str] = Field(default_factory=list)
    """不可执行的原因；非空时脚本物理上跑不起来。"""
    included_actions: int | None = None
    omitted_actions: int | None = None
    warnings: list[str] = Field(default_factory=list)
    incomplete_reasons: list[str] = Field(default_factory=list)
    """.. deprecated:: 与 ``confidence_factors`` 同值的兼容别名。"""
    generated_at: str | None = None
    modified_at: str | None = None
    size_bytes: int = 0

    @property
    def diagnostic(self) -> bool:
        """是否**不可执行**（缺可回放动作或应用身份为占位）。"""
        return not self.replay_eligible


class ScriptCatalog:
    """按目录扫描脚本产物并提供安全读取。"""

    def __init__(self, runtime_dir: Path):
        # 统一为绝对路径：resolve() 返回绝对路径，若根目录是相对路径，
        # _entry() 中的 relative_to 会失败（列表能列出但详情读取 404）。
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()

    # ------------------------------------------------------------------
    # 列出
    # ------------------------------------------------------------------

    def list_entries(self) -> list[ScriptCatalogEntry]:
        """列出全部脚本，按最后修改时间倒序（最新的在最前）。"""
        if not self.runtime_dir.is_dir():
            return []
        entries: list[ScriptCatalogEntry] = []
        for directory in sorted(self.runtime_dir.glob(f"*/{GENERATED_DIR}")):
            if not directory.is_dir():
                continue
            for script in sorted(directory.glob(f"*{SCRIPT_SUFFIX}")):
                if script.name.startswith("_"):
                    continue
                entry = self._entry(script, directory)
                if entry is not None:
                    entries.append(entry)
        return sorted(entries, key=lambda item: item.modified_at or "", reverse=True)

    def _entry(self, script: Path, directory: Path) -> ScriptCatalogEntry | None:
        try:
            stat = script.stat()
            script_id = script.relative_to(self.runtime_dir).as_posix()
        except OSError, ValueError:
            return None
        config = self._read_json(script.with_suffix(".json"))
        metadata = self._read_json(directory / _METADATA_NAME)
        case_spec = self._read_json(directory / _CASE_SPEC_NAME)
        xdevice_dir = directory / _XDEVICE_DIR
        run_id = directory.parent.name
        return ScriptCatalogEntry(
            script_id=script_id,
            run_id=run_id,
            source="dc" if run_id.startswith("dc-") else "run",
            filename=script.name,
            python_path=str(script.resolve()),
            case_id=_str_or_none(config.get("case_id")),
            ir_case_id=_str_or_none(case_spec.get("case_id")),
            scenario=_str_or_none(case_spec.get("scenario")),
            case_version=_int_or_none(case_spec.get("schema_version")),
            xdevice_project=str(xdevice_dir.resolve()) if xdevice_dir.is_dir() else None,
            bundle_name=_str_or_none(config.get("bundle_name")),
            main_ability=_str_or_none(config.get("main_ability")),
            purpose=_str_or_none(config.get("purpose")) or _str_or_none(metadata.get("purpose")),
            replay_eligible=bool(config.get("replay_eligible", metadata.get("replay_eligible", False))),
            confidence=_str_or_none(metadata.get("confidence")) or _str_or_none(config.get("confidence")),
            confidence_factors=_str_list(
                metadata.get("confidence_factors")
                or config.get("confidence_factors")
                or metadata.get("incomplete_reasons")
            ),
            promotion_eligible=bool(metadata.get("promotion_eligible", config.get("promotion_eligible", False))),
            runnable_blockers=_str_list(metadata.get("runnable_blockers") or config.get("runnable_blockers")),
            included_actions=_int_or_none(config.get("included_operations", metadata.get("included_action_count"))),
            omitted_actions=_int_or_none(config.get("omitted_operations", metadata.get("omitted_action_count"))),
            warnings=_str_list(config.get("warnings") or metadata.get("warnings")),
            incomplete_reasons=_str_list(metadata.get("incomplete_reasons")),
            generated_at=_str_or_none(config.get("generated_at")) or _str_or_none(metadata.get("generated_at")),
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
            size_bytes=stat.st_size,
        )

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except OSError, ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def resolve(self, script_id: str) -> Path:
        """把 script_id 解析为产物目录内的绝对路径（越界或不存在则抛 ValueError）。"""
        if not script_id or script_id.startswith(("/", "\\")) or ".." in Path(script_id).parts:
            raise ValueError(f"invalid script id: {script_id!r}")
        candidate = (self.runtime_dir / script_id).resolve()
        runtime_root = self.runtime_dir.resolve()
        if not candidate.is_relative_to(runtime_root) or not candidate.is_file():
            raise ValueError(f"script not found: {script_id!r}")
        return candidate

    def entry_for(self, script_id: str) -> ScriptCatalogEntry:
        """返回单个脚本的目录项（不存在则抛 ValueError）。"""
        script = self.resolve(script_id)
        entry = self._entry(script, script.parent)
        if entry is None:
            raise ValueError(f"script not found: {script_id!r}")
        return entry

    def read(self, script_id: str) -> tuple[ScriptCatalogEntry, str, dict[str, Any]]:
        """返回 (目录项, 源码, config)。"""
        entry = self.entry_for(script_id)
        python_text = Path(entry.python_path).read_text(encoding="utf-8")
        return entry, python_text, self._read_json(Path(entry.python_path).with_suffix(".json"))


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]
