"""DcShellPolicy 安全策略测试：黑名单、白名单、路径校验、绕过尝试。"""

from __future__ import annotations

import pytest

from harmony_test_agent.dc.models import DcSafetyError
from harmony_test_agent.dc.safety import DcShellPolicy


@pytest.fixture
def policy() -> DcShellPolicy:
    return DcShellPolicy()


# ---------------------------------------------------------------------------
# Shell 黑名单
# ---------------------------------------------------------------------------


class TestShellBlacklist:
    """黑名单必须拦截已知破坏性命令及其常见变形。"""

    @pytest.mark.parametrize(
        "argv",
        [
            ["reboot"],
            ["REBOOT"],
            ["Reboot"],
            ["/system/bin/reboot"],
            ["reboot", "&"],
            ["mkfs"],
            ["mkfs.ext4"],
            ["dd", "if=/dev/zero", "of=/dev/sda"],
            ["rm", "-rf", "/"],
            ["rm", "-rf", "/*"],
            ["param", "set", "persist.sys.timezone", "xxx"],
            ["fastboot", "devices"],
            ["format", "C:"],
            ["shutdown", "-h", "now"],
            ["poweroff"],
            ["halt"],
            ["init", "0"],
        ],
    )
    def test_blocked_commands(self, policy: DcShellPolicy, argv: list[str]) -> None:
        with pytest.raises(DcSafetyError):
            policy.validate_shell(argv)

    @pytest.mark.parametrize(
        "argv",
        [
            ["ls", "-l", "/data/local/tmp"],
            ["cat", "/proc/meminfo"],
            ["aa", "start", "-b", "com.example", "-a", "EntryAbility"],
            ["bm", "dump", "-a"],
            ["hilog", "-x"],
            ["uitest", "uiInput", "click", "100", "200"],
            ["hidumper", "--mem", "com.example"],
            ["snapshot_display", "-i", "0", "-f", "/data/local/tmp/s.jpeg"],
            ["grep", "-r", "error", "/data/log"],
            ["ps", "-ef"],
            ["echo", "hello"],
            ["param", "get", "const.product.name"],
        ],
    )
    def test_allowed_commands(self, policy: DcShellPolicy, argv: list[str]) -> None:
        # 不应抛出异常
        policy.validate_shell(argv)


# ---------------------------------------------------------------------------
# 命令前缀白名单
# ---------------------------------------------------------------------------


class TestShellPrefixWhitelist:
    """argv[0] 不在白名单中时必须拒绝。"""

    @pytest.mark.parametrize(
        "argv",
        [
            ["python3", "script.py"],
            ["sh", "script.sh"],
            ["bash", "script.sh"],
            ["curl", "http://example.com"],
            ["wget", "http://example.com"],
            ["nc", "-l", "4444"],
            ["chmod", "777", "/data"],
        ],
    )
    def test_blocked_prefixes(self, policy: DcShellPolicy, argv: list[str]) -> None:
        with pytest.raises(DcSafetyError, match="not in the allowed list"):
            policy.validate_shell(argv)


# ---------------------------------------------------------------------------
# Shell 元字符与结构限制
# ---------------------------------------------------------------------------


class TestShellStructure:
    """拒绝 shell 元字符、超长命令和过多分隔符。"""

    def test_rejects_semicolons(self, policy: DcShellPolicy) -> None:
        with pytest.raises(DcSafetyError, match="metacharacters"):
            policy.validate_shell(["ls", ";", "reboot"])

    def test_rejects_pipes(self, policy: DcShellPolicy) -> None:
        with pytest.raises(DcSafetyError, match="metacharacters"):
            policy.validate_shell(["cat", "file", "|", "grep", "x"])

    def test_rejects_ampersand(self, policy: DcShellPolicy) -> None:
        with pytest.raises(DcSafetyError, match="metacharacters"):
            policy.validate_shell(["ls", "&&", "reboot"])

    def test_rejects_backtick(self, policy: DcShellPolicy) -> None:
        with pytest.raises(DcSafetyError, match="metacharacters"):
            policy.validate_shell(["echo", "`reboot`"])

    def test_rejects_dollar_paren(self, policy: DcShellPolicy) -> None:
        with pytest.raises(DcSafetyError, match="metacharacters"):
            policy.validate_shell(["echo", "$(reboot)"])

    def test_rejects_empty_argv(self, policy: DcShellPolicy) -> None:
        with pytest.raises(DcSafetyError, match="must not be empty"):
            policy.validate_shell([])

    def test_rejects_long_command(self, policy: DcShellPolicy) -> None:
        long_arg = "a" * 600
        with pytest.raises(DcSafetyError, match="exceeds"):
            policy.validate_shell(["echo", long_arg])


# ---------------------------------------------------------------------------
# 黑名单绕过尝试
# ---------------------------------------------------------------------------


class TestShellBypassAttempts:
    """至少 15 个绕过尝试用例。"""

    @pytest.mark.parametrize(
        "argv",
        [
            # 引号绕过
            ['re""boot'],
            ["re'boot"],
            # 反斜杠绕过
            ["\\reboot"],
            ["r\\eboot"],
            # 路径绕过
            ["/system/bin/reboot"],
            ["/bin/reboot"],
            # 大小写绕过
            ["REBOOT"],
            ["ReBoOt"],
            # 空格绕过
            ["reboot  "],
            ["  reboot"],
            # 变形绕过
            ["rm", "-rf", "/"],
            ["rm", "-rf", "/*"],
            ["dd", "if=/dev/zero"],
            ["mkfs.ext4", "/dev/sda"],
            ["param", "set", "key", "value"],
            # fork bomb
            [":(){", "|", "&", "};:"],
        ],
    )
    def test_bypass_blocked(self, policy: DcShellPolicy, argv: list[str]) -> None:
        """绕过尝试应被黑名单或元字符检查拦截。

        注意：某些绕过可能通过元字符检查而非黑名单拦截，
        只要抛出 DcSafetyError 即视为成功。
        """
        with pytest.raises(DcSafetyError):
            policy.validate_shell(argv)


# ---------------------------------------------------------------------------
# 文件路径校验
# ---------------------------------------------------------------------------


class TestFilePathValidation:
    """设备侧路径白名单/黑名单。"""

    @pytest.mark.parametrize(
        "path",
        ["/data/local/tmp/file.txt", "/storage/emulated/0/doc.pdf", "/data/app/com.example/file"],
    )
    def test_allowed_paths(self, policy: DcShellPolicy, path: str) -> None:
        policy.validate_file_path(path, "send")

    @pytest.mark.parametrize(
        "path",
        ["/system/bin/su", "/proc/1/cmdline", "/dev/null", "/vendor/lib/hw", "/sys/class/net"],
    )
    def test_blocked_paths(self, policy: DcShellPolicy, path: str) -> None:
        with pytest.raises(DcSafetyError):
            policy.validate_file_path(path, "recv")

    def test_path_outside_whitelist(self, policy: DcShellPolicy) -> None:
        with pytest.raises(DcSafetyError, match="not in an allowed region"):
            policy.validate_file_path("/random/path/file.txt", "list")


# ---------------------------------------------------------------------------
# 安装文件校验
# ---------------------------------------------------------------------------


class TestInstallValidation:
    def test_allowed_suffixes(self, policy: DcShellPolicy) -> None:
        for suffix in (".hap", ".app", ".hsp"):
            policy.validate_install(f"/path/to/file{suffix}")

    def test_blocked_suffix(self, policy: DcShellPolicy) -> None:
        with pytest.raises(DcSafetyError, match="must have one of suffixes"):
            policy.validate_install("/path/to/file.apk")


# ---------------------------------------------------------------------------
# 卸载校验
# ---------------------------------------------------------------------------


class TestUninstallValidation:
    def test_blocks_system_bundles(self, policy: DcShellPolicy) -> None:
        for bundle in ("com.ohos.sceneboard", "com.huawei.hmos.launcher", "com.ohos.systemui"):
            with pytest.raises(DcSafetyError, match="system-critical"):
                policy.validate_uninstall(bundle)

    def test_allows_normal_bundles(self, policy: DcShellPolicy) -> None:
        policy.validate_uninstall("com.example.myapp")
