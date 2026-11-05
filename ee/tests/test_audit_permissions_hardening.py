"""权限判定的边界回归（2026-11 审计修复）。

每条都对应一个实测过的绕过路径：修复前返回 allow，修复后必须拦住。
放在单独文件里，和被修的具体实现文件没有同名关系，避免再出现「改了核心模块却
找不到对应测试」的覆盖真空。
"""
import os
from pathlib import Path

import pytest

from ee.core.permissions import PermissionPolicy, command_prefix

WS = Path("C:/ws") if os.name == "nt" else Path("/ws")


def pol(mode="default", **kw):
    return PermissionPolicy(mode=mode, workspace=WS, **kw)


# ---------------------------------------------------------------- 反斜杠转义

@pytest.mark.skipif(os.name != "nt", reason="反斜杠是 Windows 路径分隔符，用例针对 bash 语义")
@pytest.mark.parametrize("cmd", [
    "find . -exe\\c touch own.txt \\;",      # bash 还原成 -exec
    "sort -\\o out.txt in.txt",              # bash 还原成 -o
    "sort --out\\put=o.txt in.txt",          # bash 还原成 --output
])
def test_readonly_mode_sees_through_backslash_escapes(cmd):
    """只读白名单必须按 shell 真正执行的词判定，不能看字面。

    修复前：tokens() 见到 `\\` 就走 posix=False，词里留着 -exe\\c，白名单认不出来 → allow。
    """
    assert pol("read-only").check("shell", {"command": cmd}).action == "deny"


def test_readonly_mode_still_allows_plain_safe_commands():
    assert pol("read-only").check("shell", {"command": "ls -la"}).action == "allow"


# ---------------------------------------------------------------- powershell 带值开关

@pytest.mark.parametrize("cmd", [
    'powershell -ExecutionPolicy Bypass -Command "git push origin main"',
    'powershell -ep Bypass -Command "git push origin main"',
    'powershell -WorkingDirectory C:/ws -Command "git push origin main"',
])
def test_ps_value_opts_do_not_hide_the_real_command(cmd):
    """powershell 的带值开关不能把它的值当成命令。

    修复前：模块里 _PS_VALUE_OPTS 被后面那组带 '-' 的集合覆盖，`-ExecutionPolicy` 不再被
    跳过，值 Bypass 被当成位置参数，嵌套分析从 `Bypass -Command ...` 起算 → 用户的 deny 规则失效。
    """
    d = pol("auto", deny=["shell(git push *)"]).check("shell", {"command": cmd})
    assert d.action == "deny"


def test_ps_command_alone_is_still_analyzed():
    d = pol("auto", deny=["shell(git push *)"]).check(
        "shell", {"command": 'powershell -Command "git push origin main"'})
    assert d.action == "deny"


# ---------------------------------------------------------------- deny 的路径写法

@pytest.mark.skipif(os.name != "nt", reason=".. / 结尾点 / ::$DATA 只有 Windows 上指向同一文件")
@pytest.mark.parametrize("path", [
    "x/../secret/f",          # 相对路径带 ..
    "./secret/f",             # 纯相对
    "C:/ws/x/../secret/f",    # 绝对路径带 ..
])
def test_deny_write_matches_equivalent_paths(path):
    d = pol("auto", deny=["write(C:/ws/secret/*)"]).check("write", {"path": path, "content": "x"})
    assert d.action == "deny"


@pytest.mark.skipif(os.name != "nt", reason="::$DATA / 结尾点只有 Windows 上等价")
@pytest.mark.parametrize("path", [
    "C:/ws/a/../secret.env",
    "C:/ws/secret.env.",          # 结尾点：Windows 打开的还是 secret.env
    "C:/ws/secret.env::$DATA",    # NTFS 数据流别名
])
def test_deny_read_matches_equivalent_paths(path):
    d = pol("auto", deny=["read(C:/ws/secret.env)"]).check("read", {"path": path})
    assert d.action == "deny"


def test_deny_does_not_block_unrelated_paths():
    assert pol("auto", deny=["read(C:/ws/secret.env)"]).check(
        "read", {"path": "C:/ws/public.txt"}).action == "allow"


# ---------------------------------------------------------------- deny 匹配裸命令

def test_deny_rule_matches_bare_command():
    """规则写 shell(git push *)，命令正好是 `git push`（后面没有参数）也要拦住。

    修复前：通配符要求 `git push` 后面还有个空格，裸命令漏过去；allow 分支一直有这层补偿。
    """
    assert pol("auto", deny=["shell(git push *)"]).check(
        "shell", {"command": "git push"}).action == "deny"
    assert pol("auto", deny=["shell(git push *)"]).check(
        "shell", {"command": "git push origin main"}).action == "deny"


# ---------------------------------------------------------------- 只读模式的 git reflog

def test_readonly_git_reflog_reads_are_allowed():
    assert pol("read-only").check("shell", {"command": "git reflog"}).action == "allow"
    assert pol("read-only").check("shell", {"command": "git reflog show HEAD"}).action == "allow"


@pytest.mark.parametrize("cmd", [
    "git reflog delete HEAD@{0}",
    "git reflog expire --all",
])
def test_readonly_git_reflog_writes_are_denied(cmd):
    """.git/logs 是误操作后的最后抓手，只读模式不能改它。"""
    assert pol("read-only").check("shell", {"command": cmd}).action == "deny"


# ---------------------------------------------------------------- git 长选项前缀缩写

@pytest.mark.parametrize("cmd", [
    "git reset --har",       # = --hard，唯一前缀
    "git reset --hard",
    "git clean --forc",      # = --force
    "git clean --force",     # 长写形式之前也没被认出来
    "git clean -fd",
])
def test_git_force_options_are_asked_even_when_abbreviated(cmd):
    """git 接受唯一前缀缩写，判据不能只认完整拼写。

    修复前：`git reset --har` / `git clean --forc` 在 auto 模式直接 allow。
    """
    assert pol("auto").check("shell", {"command": cmd}).action == "ask"


def test_ambiguous_git_prefix_is_not_forced():
    """--forc 在 push 上有歧义（--force / --force-with-lease / --force-if-includes），
    git 自己会报 ambiguous 并拒绝执行，不需要我们额外拦截。"""
    d = pol("auto").check("shell", {"command": "git push origin main --forc"})
    assert d.action in ("allow", "deny")


# ---------------------------------------------------------------- 「以后都行」的前缀

@pytest.mark.parametrize("cmd", [
    "bash -c 'mkdir build'",
    "bash -lc 'mkdir build'",          # 组合短选项
    "sh -c 'mkdir build'",
    'powershell -Command "Get-Item x"',
    "cmd /c dir",
    "python -c 'print(1)'",
])
def test_inline_script_prefix_is_the_command_itself(cmd):
    """解释器的「前缀」不能退化成解释器名。

    修复前：批准 `bash -c 'mkdir build'` 记下的前缀是 `bash`，此后
    `bash -c 'cat /etc/passwd'` 被「以后都行」直接放行，还会被永久写进
    ~/.ee/permissions.json —— 一次审批等于永久放行任意脚本。
    """
    assert command_prefix(cmd) == cmd


def test_approving_inline_bash_does_not_whitelist_other_scripts():
    p = pol("default")
    d = p.check("shell", {"command": "bash -c 'mkdir build'"})
    p.remember(d, "shell")
    assert p.check("shell", {"command": "bash -c 'cat /etc/passwd'"}).action == "ask"


def test_subcommand_prefix_still_works():
    """收紧不能把正常前缀一起毁掉：子命令粒度仍按原样记。"""
    assert command_prefix("git commit -m x") == "git commit"
    assert command_prefix("python -m pytest -q") == "python -m pytest"
