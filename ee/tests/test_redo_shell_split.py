"""权限命令拆分 / 归一重做（fix/redo-shell-split）。

1) 全自动模式下“强制确认 / 拒绝”不能被引号拆写、PowerShell 参数缩写、git 全局选项等绕过；
2) 按实际执行的 shell（posix / powershell / cmd）用对应语法拆分，解析不了按需要确认处理；
3) pytest 会执行仓库里的 conftest.py，不再算只读命令。
"""
import pytest

from ee.core.permissions import PermissionPolicy


def pol(ws, mode="default", shell=None, **kw):
    if shell is not None:
        kw["shell"] = shell
    return PermissionPolicy(mode=mode, workspace=ws, **kw)


# ------------------------------------------------------------------ 3) pytest 不再免问

@pytest.mark.parametrize("cmd", ["pytest -q", "python -m pytest tests", "py.test", "python3 -m pytest -x"])
def test_pytest_needs_confirm(tmp_path, cmd):
    assert pol(tmp_path).check("shell", {"command": cmd}).action == "ask"
    assert pol(tmp_path, "read-only").check("shell", {"command": cmd}).action == "deny"
    assert pol(tmp_path, "plan").check("shell", {"command": cmd}).action == "deny"


# ------------------------------------------------------------------ 2) 按实际 shell 的语法拆分

@pytest.mark.parametrize("shell,cmd", [
    # cmd：单引号不是引号，& 照样分隔；%VAR% 会在解析前展开（内容可能带 & 等分隔符）
    ("cmd", "echo 'a & del x'"),
    ("cmd", "type 'a | del x'"),
    ("cmd", "echo %X%"),
    ("cmd", "type %USERPROFILE%\\a.txt"),
    # PowerShell：双引号里反斜杠不是转义，\" 就把字符串结束了
    ("powershell", 'echo "a\\"; Remove-Item x; echo \\"b"'),
    ("powershell", 'Write-Output "x\\" ; Set-Content a.txt 1 ; \\""'),
    # posix：双引号里的 `...` 也是命令替换
    ("posix", 'echo "`rm x`"'),
    # 引号不配对：解析不了，按需要确认
    ("posix", "echo 'a; rm x"),
    ("powershell", "Write-Output 'a; Remove-Item x"),
])
def test_split_by_real_shell_default_asks(tmp_path, shell, cmd):
    assert pol(tmp_path, shell=shell).check("shell", {"command": cmd}).action == "ask", (shell, cmd)
    assert pol(tmp_path, "read-only", shell=shell).check("shell", {"command": cmd}).action == "deny", (shell, cmd)


@pytest.mark.parametrize("cmd", [
    "echo 'a & del x'",                          # cmd 下会删
    'echo "a\\"; Remove-Item x; echo \\"b"',      # PowerShell 下会删
    "echo %X%",
])
def test_unknown_shell_takes_strictest(tmp_path, cmd):
    """不知道实际 shell 时，三套语法都拆一遍，取最严。"""
    assert pol(tmp_path).check("shell", {"command": cmd}).action == "ask", cmd


@pytest.mark.parametrize("shell,cmd", [
    ("cmd", "echo a ^& b"),                      # ^& 是字面量 &
    ("cmd", "type a.txt | findstr foo"),
    ("cmd", "dir & where git"),
    ("powershell", "Write-Output 'a;b'"),
    ("powershell", "Get-ChildItem -Recurse | Select-Object Name"),
    ("powershell", "Get-Content a.txt; git status"),
    ("posix", "ls -la | wc -l; git status"),
    ("posix", "echo 'a & b'"),
])
def test_split_by_real_shell_reads_allowed(tmp_path, shell, cmd):
    assert pol(tmp_path, shell=shell).check("shell", {"command": cmd}).action == "allow", (shell, cmd)


@pytest.mark.parametrize("shell,cmd,expect", [
    ("cmd", "echo a & del x", ["echo a", "del x"]),
    ("cmd", "echo a ^& del x", ["echo a ^& del x"]),
    ("cmd", "echo a; del x", ["echo a; del x"]),               # ; 在 cmd 里不是分隔符
    ("cmd", "echo 'a & del x'", ["echo 'a", "del x'"]),
    ("powershell", "Get-ChildItem; Remove-Item x", ["Get-ChildItem", "Remove-Item x"]),
    ("powershell", "Get-ChildItem `; Remove-Item x", ["Get-ChildItem `; Remove-Item x"]),   # 反引号转义
    ("powershell", "git status & git log", ["git status", "git log"]),   # PS7 后台运算符
    ("powershell", "& 'C:\\tools\\x.exe' a", ["& 'C:\\tools\\x.exe' a"]),   # 行首 & 是调用运算符
    ("posix", "echo a \\; rm x", ["echo a \\; rm x"]),
])
def test_split_command_per_syntax(shell, cmd, expect):
    from ee.core.permissions import split_command
    assert split_command(cmd, shell) == expect


@pytest.mark.parametrize("shell,cmd,sub", [
    ("powershell", 'Write-Output "$(Remove-Item x)"', "Remove-Item x"),
    ("powershell", "Get-ChildItem | ForEach-Object { Remove-Item $_ }", "Remove-Item $_"),
    ("powershell", "Write-Output (Remove-Item x)", "Remove-Item x"),
    ("posix", 'echo "`rm x`"', "rm x"),
])
def test_split_command_subcommands(shell, cmd, sub):
    from ee.core.permissions import split_command
    assert sub in split_command(cmd, shell)


def test_session_policy_follows_shell(workspace, monkeypatch):
    from conftest import make_session
    from ee.tools.shellenv import ShellInfo
    import ee.core.session as session_mod
    s, _ = make_session(workspace, [])
    assert s.policy.shell == (s.shell.syntax if s.shell else None)
    monkeypatch.setattr(session_mod, "resolve_shell", lambda name: ShellInfo("pwsh", "pwsh", "PowerShell 7 (pwsh)"))
    s.set_shell("pwsh")
    assert s.policy.shell == "powershell"
    monkeypatch.setattr(session_mod, "resolve_shell", lambda name: ShellInfo("cmd", "cmd.exe", "cmd"))
    s.set_shell("cmd")
    assert s.policy.shell == "cmd"
