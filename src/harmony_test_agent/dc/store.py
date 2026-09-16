"""DC 模式会话快照的落盘与读取。

每个会话一个 ``<runtime_dir>/<session_id>/dc_session.json``：
- 会话只存在于内存，服务重启/空闲淘汰后仍可从该文件恢复（列表 + 继续对话）。
- ``history`` 用 pydantic-ai 的 ``ModelMessagesTypeAdapter`` 序列化，写入前剥离
  图片（BinaryContent）与 thinking part，避免体积膨胀与回喂风险。
- 写入采用「临时文件 + 原子替换」，避免读到半截 JSON。

本模块只做文件 IO，不持有会话状态，也不引用 ``DcSession``（避免循环依赖）。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelMessagesTypeAdapter

from ..models import utc_now
from .models import (
    DcScriptArtifact,
    DcSessionSnapshot,
    DcToolInvocation,
    DcToolTier,
)

logger = logging.getLogger(__name__)

# 单条命令 stdout/stderr 的落盘上限（bm dump / hilog 等输出可达数百 KB）
COMMAND_OUTPUT_LIMIT = 8_000

# 历史中图片被剥离后的占位文本
_IMAGE_PLACEHOLDER = "[screenshot omitted from history]"


class DcSessionStore:
    """会话快照仓库：保存、读取、扫描摘要。"""

    FILE_NAME = "dc_session.json"

    def __init__(self, runtime_dir: Path, default_tier: DcToolTier = DcToolTier.L2):
        self.runtime_dir = Path(runtime_dir)
        self.default_tier = default_tier
        # 后台 checkpoint 线程与会话收尾可能并发保存同一会话，串行化写入
        self._write_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------

    def path_for(self, session_id: str) -> Path:
        """返回会话快照文件路径。"""
        return self.runtime_dir / session_id / self.FILE_NAME

    def session_dir(self, session_id: str) -> Path:
        """返回会话产物目录（与 ArtifactStore.run_dir 保持一致）。"""
        return self.runtime_dir / session_id

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def save(self, snapshot: DcSessionSnapshot) -> Path:
        """原子写入快照；命令输出超限时截断后再落盘。

        临时文件名带随机后缀：后台 checkpoint 线程与会话收尾可能并发保存，
        共用固定的 ``.tmp`` 名字会互相 rename 失败（Windows 上表现为 WinError 32）。
        """
        path = self.path_for(snapshot.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        trimmed = snapshot.model_copy(
            update={"invocations": [self._trim_invocation(inv) for inv in snapshot.invocations]}
        )
        temporary = path.with_name(f"{self.FILE_NAME}.{uuid.uuid4().hex[:8]}.tmp")
        with self._write_lock:
            try:
                temporary.write_text(trimmed.model_dump_json(indent=2) + "\n", encoding="utf-8")
                temporary.replace(path)
            except OSError:
                temporary.unlink(missing_ok=True)
                raise
        return path

    def _trim_invocation(self, invocation: DcToolInvocation) -> DcToolInvocation:
        command = invocation.command
        if command is None:
            return invocation
        stdout = self._truncate(command.stdout)
        stderr = self._truncate(command.stderr)
        if stdout == command.stdout and stderr == command.stderr:
            return invocation
        return invocation.model_copy(
            update={"command": command.model_copy(update={"stdout": stdout, "stderr": stderr})}
        )

    @staticmethod
    def _truncate(text: str | None) -> str | None:
        if not text or len(text) <= COMMAND_OUTPUT_LIMIT:
            return text
        omitted = len(text) - COMMAND_OUTPUT_LIMIT
        return f"{text[:COMMAND_OUTPUT_LIMIT]}\n... [truncated {omitted} chars]"

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def load(self, session_id: str) -> DcSessionSnapshot | None:
        """读取会话快照。

        - 有快照 → 直接返回；
        - 只有产物目录（升级前创建的会话 / 被手工删除快照）→ 返回最小合成快照，
          让历史条目可见、可恢复（turns 为空，脚本从 ``generated/`` 读回）；
        - 目录也不存在 → ``None``。
        """
        path = self.path_for(session_id)
        if path.exists():
            snapshot = self._load_path(path)
            if snapshot is not None:
                return snapshot
        return self._synthesize(session_id)

    def _load_path(self, path: Path) -> DcSessionSnapshot | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            logger.warning("skipping unreadable DC session snapshot %s: %s", path, exc)
            return None
        try:
            return DcSessionSnapshot.model_validate(payload)
        except ValueError as exc:
            logger.warning("skipping invalid DC session snapshot %s: %s", path, exc)
            return None

    def _synthesize(self, session_id: str) -> DcSessionSnapshot | None:
        session_dir = self.session_dir(session_id)
        if not session_dir.is_dir():
            return None
        script = self._read_disk_script(session_id)
        return DcSessionSnapshot(
            schema_version=0,
            session_id=session_id,
            device_id="",
            tier=self.default_tier,
            status="closed",
            last_active_at=_mtime(session_dir),
            script=script,
            has_snapshot=False,
        )

    def _read_disk_script(self, session_id: str) -> DcScriptArtifact | None:
        generated_dir = self.session_dir(session_id) / "generated"
        if not generated_dir.is_dir():
            return None
        candidates = sorted(generated_dir.glob("*.py"))
        if not candidates:
            return None
        python_path = candidates[0]
        try:
            python_text = python_path.read_text(encoding="utf-8")
        except OSError:
            return None
        return DcScriptArtifact(python_path=str(python_path.resolve()), python_text=python_text)

    def list_summaries(self) -> list[DcSessionSnapshot]:
        """扫描磁盘上全部 DC 会话快照（含仅有产物目录的历史会话），按活跃时间倒序。"""
        if not self.runtime_dir.is_dir():
            return []
        snapshots: list[DcSessionSnapshot] = []
        for path in sorted(self.runtime_dir.glob(f"*/{self.FILE_NAME}")):
            snapshot = self._load_path(path)
            if snapshot is not None:
                snapshots.append(snapshot)
        known = {snapshot.session_id for snapshot in snapshots}
        for session_dir in sorted(self.runtime_dir.glob("dc-*")):
            if session_dir.name in known or not session_dir.is_dir():
                continue
            synthesized = self._synthesize(session_dir.name)
            if synthesized is not None:
                snapshots.append(synthesized)
        return sorted(snapshots, key=lambda item: item.last_active_at, reverse=True)

    # ------------------------------------------------------------------
    # 消息历史序列化
    # ------------------------------------------------------------------

    def dump_history(self, history: list[Any]) -> list[Any]:
        """把 pydantic-ai 消息历史序列化为 JSON 友好的结构。

        图片 part 替换为文本占位符、thinking part 丢弃；序列化失败时返回空列表
        （调用方据此回退为按 turns 重建的纯文本上下文）。
        """
        if not history:
            return []
        cleaned = [strip_message_for_storage(message) for message in history]
        try:
            return ModelMessagesTypeAdapter.dump_python(cleaned, mode="json")
        except Exception as exc:  # noqa: BLE001 - 任何序列化异常都退化为文本上下文
            logger.warning("failed to serialize DC history: %s", exc)
            return []

    @staticmethod
    def load_history(payload: list[Any]) -> list[Any] | None:
        """反序列化消息历史；失败返回 None（调用方回退为文本重建）。"""
        if not payload:
            return None
        try:
            return list(ModelMessagesTypeAdapter.validate_python(payload))
        except Exception as exc:  # noqa: BLE001 - 旧版本/损坏数据不应阻塞恢复
            logger.warning("failed to restore DC history: %s", exc)
            return None


def copy_message_with_parts(message: Any, parts: list[Any]) -> Any:
    """用新的 parts 复制一条 pydantic-ai 消息。

    pydantic-ai 的 ``ModelRequest`` / ``ModelResponse`` 是普通 dataclass
    （没有 ``model_copy``），旧代码用 ``message.model_copy(...)`` 复制图片剥离结果
    时抛 ``AttributeError`` 并被静默吞掉，导致图片/thinking 实际从未被剥离。
    """
    return replace_fields(message, parts=parts)


def replace_fields(obj: Any, **changes: Any) -> Any:
    """按字段复制 dataclass 或 pydantic 模型；失败时原样返回。"""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        try:
            return dataclasses.replace(obj, **changes)
        except Exception:  # noqa: BLE001 - 复制失败时按原样保留
            return obj
    model_copy = getattr(obj, "model_copy", None)
    if callable(model_copy):
        try:
            return model_copy(update=changes)
        except Exception:  # noqa: BLE001
            return obj
    return obj


def strip_message_for_storage(message: Any) -> Any:
    """剥离消息中的图片与 thinking part，返回可安全持久化的副本。

    图片不能被替换为 ``TextPart`` 占位符：``ModelRequest.parts`` 的联合类型不接受
    ``text`` 标签，会导致反序列化失败（从而丢失全部工具上下文）。请求侧改为把
    占位说明并入相邻的 prompt 文本，响应侧才使用 ``TextPart``。
    """
    from pydantic_ai.messages import TextPart

    parts = getattr(message, "parts", None)
    if parts is None:
        return message
    is_response = type(message).__name__ == "ModelResponse"
    new_parts: list[Any] = []
    changed = False
    dropped_image = False
    for part in parts:
        kind = getattr(part, "part_kind", None)
        media_type = str(getattr(part, "media_type", "") or "")
        is_image = type(part).__name__ == "BinaryContent" or "image" in media_type
        if is_image:
            changed = True
            dropped_image = True
            if is_response:
                new_parts.append(TextPart(content=_IMAGE_PLACEHOLDER))
            continue
        if kind == "thinking":
            changed = True
            continue
        new_parts.append(part)
    if not changed:
        return message
    if dropped_image and not is_response:
        new_parts = _annotate_last_prompt(new_parts)
    return copy_message_with_parts(message, new_parts)


def _annotate_last_prompt(parts: list[Any]) -> list[Any]:
    """把图片占位说明并入最后一条纯文本 prompt part。"""
    for index in range(len(parts) - 1, -1, -1):
        part = parts[index]
        content = getattr(part, "content", None)
        if getattr(part, "part_kind", None) not in ("user-prompt", "system-prompt"):
            continue
        if not isinstance(content, str):
            continue
        annotated = replace_fields(part, content=f"{content}\n{_IMAGE_PLACEHOLDER}")
        return [*parts[:index], annotated, *parts[index + 1 :]]
    return parts


def _mtime(path: Path) -> datetime:
    """返回目录 mtime（用作无快照历史会话的近似活跃时间）。"""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return utc_now()
