"""DC 模式专属安全策略：Shell 黑名单 + 命令前缀白名单 + 文件路径白名单。

不复用 ``runtime/safety.py`` 的 ``SafetyPolicy``——后者面向 Live Mode 的
``ToolDecision`` 语义校验，与 DC 模式的通用工具参数校验需求不同。
"""

from __future__ import annotations

import re
from typing import ClassVar

from .models import DcSafetyError

# Shell 元字符：出现即拒绝（argv-list 形式下不应包含这些）
_SHELL_METACHARACTERS = re.compile(r"[;|&`$(){}]")

# 单条命令最大长度
_MAX_COMMAND_LENGTH = 500

# 分号/管道数量上限
_MAX_SEPARATORS = 3


class DcShellPolicy:
    """DC 模式安全策略。

    三层防护：
    1. 正则黑名单——拦截已知破坏性命令（含常见变形）
    2. 命令前缀白名单——``execute_shell`` 的 argv[0] 必须在允许列表中
    3. 结构限制——拒绝 shell 元字符、超长命令、过多分隔符

    .. note::
       黑名单是**最佳努力**而非安全边界。Agent 仍可能通过未知变形绕过；
       所有调用均完整录制为 ``DcToolInvocation`` 供事后审计。
    """

    # ------------------------------------------------------------------
    # 正则黑名单（覆盖大小写、空格、常见变形）
    # ------------------------------------------------------------------
    BLOCKED_SHELL_PATTERNS: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"\breboot\b", re.IGNORECASE),
        re.compile(r"\bmkfs(\.\w+)?\b", re.IGNORECASE),
        re.compile(r"\bdd\s+if=", re.IGNORECASE),
        re.compile(r"rm\s+-rf\s+(/|\*)", re.IGNORECASE),
        re.compile(r"\bparam\s+set\b", re.IGNORECASE),
        re.compile(r"\bfastboot\b", re.IGNORECASE),
        re.compile(r"\bformat\b", re.IGNORECASE),
        re.compile(r":\(\)\s*\{.*\}", re.IGNORECASE),  # fork bomb
        re.compile(r"\bshutdown\b", re.IGNORECASE),
        re.compile(r"\bpoweroff\b", re.IGNORECASE),
        re.compile(r"\bhalt\b", re.IGNORECASE),
        re.compile(r"\binit\s+0\b", re.IGNORECASE),
    ]

    # ------------------------------------------------------------------
    # 命令前缀白名单（execute_shell 的 argv[0]）
    # ------------------------------------------------------------------
    ALLOWED_SHELL_PREFIXES: ClassVar[frozenset[str]] = frozenset(
        {
            "aa",
            "bm",
            "uitest",
            "hilog",
            "hidumper",
            "snapshot_display",
            "cat",
            "ls",
            "mkdir",
            "echo",
            "grep",
            "head",
            "tail",
            "wc",
            "ps",
            "top",
            "df",
            "du",
            "find",
            "stat",
            "getprop",
            "param",  # param get 允许；param set 被黑名单拦截
        }
    )

    # ------------------------------------------------------------------
    # 设备侧文件路径白名单 / 黑名单
    # ------------------------------------------------------------------
    ALLOWED_DEVICE_PATHS: ClassVar[tuple[str, ...]] = (
        "/data/local/tmp/",
        "/storage/",
        "/data/app/",
    )
    BLOCKED_DEVICE_PATHS: ClassVar[tuple[str, ...]] = (
        "/system/",
        "/proc/",
        "/dev/",
        "/vendor/",
        "/sys/",
        "/boot/",
    )

    # ------------------------------------------------------------------
    # 安装文件后缀白名单
    # ------------------------------------------------------------------
    ALLOWED_INSTALL_SUFFIXES: ClassVar[frozenset[str]] = frozenset({".hap", ".app", ".hsp"})

    # ==================================================================
    # 校验方法
    # ==================================================================

    def validate_shell(self, argv: list[str]) -> None:
        """校验 ``execute_shell`` 的 argv 列表。

        Raises:
            DcSafetyError: 命中黑名单、前缀不在白名单、含 shell 元字符或超长。
        """
        if not argv:
            raise DcSafetyError("shell command must not be empty")

        joined = " ".join(argv)

        # 长度限制
        if len(joined) > _MAX_COMMAND_LENGTH:
            raise DcSafetyError(f"shell command exceeds {_MAX_COMMAND_LENGTH} characters: {len(joined)}")

        # shell 元字符
        if _SHELL_METACHARACTERS.search(joined):
            raise DcSafetyError("shell command must not contain shell metacharacters (; | & ` $ ( ) { })")

        # 分隔符数量
        separator_count = sum(joined.count(sep) for sep in (";", "|", "&&"))
        if separator_count > _MAX_SEPARATORS:
            raise DcSafetyError(f"shell command contains too many separators: {separator_count} > {_MAX_SEPARATORS}")

        # 黑名单
        normalized = self._normalize(joined)
        for pattern in self.BLOCKED_SHELL_PATTERNS:
            if pattern.search(normalized):
                raise DcSafetyError(f"shell command blocked by safety policy: matches {pattern.pattern!r}")

        # 前缀白名单
        first = argv[0].split("/")[-1]  # 取 basename，兼容 /system/bin/ls
        if first not in self.ALLOWED_SHELL_PREFIXES:
            raise DcSafetyError(
                f"shell command prefix {first!r} is not in the allowed list; use dedicated tools instead"
            )

    def validate_file_path(self, path: str, direction: str) -> None:
        """校验文件传输的设备侧路径。

        Args:
            path: 设备侧绝对路径。
            direction: ``"send"``、``"recv"`` 或 ``"list"``。

        Raises:
            DcSafetyError: 路径在黑名单中或不在白名单中。
        """
        normalized = path.replace("\\", "/").rstrip("/") + "/"

        for blocked in self.BLOCKED_DEVICE_PATHS:
            if normalized.startswith(blocked):
                raise DcSafetyError(f"device path {path!r} is in a blocked region ({blocked})")

        if not any(normalized.startswith(allowed) for allowed in self.ALLOWED_DEVICE_PATHS):
            raise DcSafetyError(
                f"device path {path!r} is not in an allowed region; allowed prefixes: {self.ALLOWED_DEVICE_PATHS}"
            )

    def validate_install(self, path: str) -> None:
        """校验待安装文件的后缀。

        Raises:
            DcSafetyError: 后缀不在白名单中。
        """
        lower = path.lower()
        if not any(lower.endswith(suffix) for suffix in self.ALLOWED_INSTALL_SUFFIXES):
            raise DcSafetyError(
                f"install file {path!r} must have one of suffixes: {sorted(self.ALLOWED_INSTALL_SUFFIXES)}"
            )

    def validate_uninstall(self, bundle_name: str) -> None:
        """校验卸载目标不是系统关键应用。

        Raises:
            DcSafetyError: 试图卸载 launcher 或系统 UI。
        """
        blocked_bundles = frozenset(
            {
                "com.ohos.sceneboard",
                "com.huawei.hmos.launcher",
                "com.ohos.launcher",
                "com.ohos.systemui",
                "com.ohos.settings",
            }
        )
        if bundle_name in blocked_bundles:
            raise DcSafetyError(f"uninstalling system-critical bundle {bundle_name!r} is blocked")

    # ==================================================================
    # 内部
    # ==================================================================

    @staticmethod
    def _normalize(command: str) -> str:
        """归一化命令字符串以匹配黑名单：去多余空白、转义反斜杠。"""
        # 去除多余空白
        normalized = re.sub(r"\s+", " ", command).strip()
        # 去除引号（防止 "re""boot" 绕过）
        normalized = normalized.replace('"', "").replace("'", "")
        # 去除反斜杠转义（防止 \reboot 绕过）
        normalized = normalized.replace("\\", "")
        return normalized
