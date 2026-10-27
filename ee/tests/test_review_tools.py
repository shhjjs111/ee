"""审查（工具/权限/仓库地图/文档读取）发现的问题的回归测试。"""
import asyncio
import os
import subprocess
import threading
from pathlib import Path

import pytest

from ee.core import eeignore as EI
from ee.core.permissions import PermissionPolicy
from ee.tools.base import ToolContext, ToolError, ToolState

needs_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="需要 mkfifo")
needs_symlink = pytest.mark.skipif(os.name == "nt", reason="Windows 上建软链接需要权限")


def _policy(ws: Path, mode: str = "default") -> PermissionPolicy:
    return PermissionPolicy(mode=mode, workspace=ws)


# ------------------------------------------------------------------ 写路径：权限判断与工具解析一致

@pytest.mark.parametrize("raw", [" {out}", "{out} ", '"{out}"', "'{out}'"])
def test_write_outside_with_quotes_or_spaces_still_asks(tmp_path, raw):
    ws = tmp_path / "ws"
    ws.mkdir()
    out = tmp_path / "outside" / "x.txt"
    p = raw.format(out=out)
    # 工具实际会写到 out（ctx.resolve 去掉引号和首尾空白）
    ctx = ToolContext(cwd=ws, state=ToolState(), output_dir=tmp_path)
    assert ctx.resolve(p) == out
    d = _policy(ws).check("write", {"path": p, "content": "x"})
    assert d.action == "ask", d


def test_apply_patch_outside_with_extra_space_still_asks(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    out = tmp_path / "outside" / "x.txt"
    patch = f"*** Begin Patch\n*** Add File:  {out}\n+hi\n*** End Patch\n"
    d = _policy(ws).check("apply_patch", {"patch": patch})
    assert d.action == "ask", d


def test_apply_patch_input_alias_is_checked(tmp_path):
    # ApplyPatchTool 也接受 input 参数；权限判断必须看同一份补丁
    ws = tmp_path / "ws"
    ws.mkdir()
    out = tmp_path / "outside" / "x.txt"
    patch = f"*** Begin Patch\n*** Add File: {out}\n+hi\n*** End Patch\n"
    assert _policy(ws).check("apply_patch", {"input": patch}).action == "ask"
    (ws / ".eeignore").write_text("secret.txt\n")
    p2 = "*** Begin Patch\n*** Add File: secret.txt\n+hi\n*** End Patch\n"
    assert _policy(ws, "auto").check("apply_patch", {"input": p2}).action == "deny"


def test_write_quoted_deny_rule_still_applies(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    pol = PermissionPolicy(mode="auto", workspace=ws, deny=["write(*.env)"])
    assert pol.check("write", {"path": '".env"', "content": "x"}).action == "deny"


# ------------------------------------------------------------------ .eeignore 被软链接绕过

@needs_symlink
def test_eeignore_ignored_dir_that_is_symlink(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    real = tmp_path / "keys"
    real.mkdir()
    (real / "id_rsa").write_text("SECRET")
    (ws / "secrets").symlink_to(real, target_is_directory=True)
    (ws / ".eeignore").write_text("secrets/\n")
    pol = _policy(ws)
    assert pol.check("read", {"path": "secrets/id_rsa"}).action == "deny"
    assert EI.for_dir(ws).ignored(ws / "secrets" / "id_rsa", False)


@needs_symlink
def test_eeignore_ignored_file_that_is_symlink(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    real = tmp_path / "real.env"
    real.write_text("TOKEN=1")
    (ws / ".env").symlink_to(real)
    (ws / ".eeignore").write_text(".env\n")
    assert _policy(ws).check("read", {"path": ".env"}).action == "deny"
    assert _policy(ws).check("write", {"path": ".env", "content": "x"}).action == "deny"


# ------------------------------------------------------------------ 非普通文件（FIFO / 设备）不能把读取卡死

def _run_with_timeout(fn, fifo: Path, timeout: float = 5.0):
    box: dict = {}

    def target():
        try:
            box["ret"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    hung = t.is_alive()
    if hung:  # 放开阻塞在 open() 上的读者
        try:
            fd = os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
        except OSError:
            pass
        t.join(2)
    return hung, box


@needs_fifo
def test_read_tool_fifo_does_not_hang(tmp_path):
    from ee.tools.read import ReadTool
    f = tmp_path / "pipe.txt"
    os.mkfifo(f)
    ctx = ToolContext(cwd=tmp_path, state=ToolState(), output_dir=tmp_path)
    hung, box = _run_with_timeout(lambda: asyncio.run(ReadTool().run({"path": str(f)}, ctx)), f)
    assert not hung, "读 FIFO 卡死"
    assert isinstance(box.get("err"), ToolError)


@needs_fifo
def test_docread_fifo_does_not_hang(tmp_path):
    from ee.core import docread
    f = tmp_path / "pipe.pdf"
    os.mkfifo(f)
    hung, box = _run_with_timeout(lambda: docread.extract(f), f)
    assert not hung, "docread 读 FIFO 卡死"
    assert isinstance(box.get("err"), docread.DocError)


# ------------------------------------------------------------------ 仓库地图

def _rm_available():
    from ee.core import repomap as R
    return R.available()


needs_ts = pytest.mark.skipif(not _rm_available(), reason="tree-sitter 未安装")


@needs_ts
@needs_fifo
def test_repomap_fifo_source_does_not_hang(tmp_path):
    from ee.core import repomap as R
    root = tmp_path / "proj"
    root.mkdir()
    (root / "ok.py").write_text("def visible_func():\n    pass\n")
    f = root / "pipe.py"
    os.mkfifo(f)
    hung, box = _run_with_timeout(lambda: R.build_map(root, tmp_path / "home", 2000), f)
    assert not hung, "仓库地图遇到 FIFO 卡死"
    assert "visible_func" in box["ret"][0]


@needs_ts
@needs_symlink
def test_repomap_skips_symlink_escaping_root(tmp_path):
    from ee.core import repomap as R
    root = tmp_path / "proj"
    root.mkdir()
    (root / "ok.py").write_text("def visible_func():\n    pass\n")
    outside = tmp_path / "outside_secret.py"
    outside.write_text("def outside_secret_func():\n    pass\n")
    (root / "link.py").symlink_to(outside)
    text, _ = R.build_map(root, tmp_path / "home", 2000)
    assert "visible_func" in text
    assert "outside_secret_func" not in text


@needs_ts
def test_repomap_respects_workspace_eeignore_below_git_root(tmp_path):
    from ee.core import repomap as R
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("没有 git")
    root = tmp_path / "repo"
    ws = root / "sub"
    ws.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (ws / "ok.py").write_text("def visible_func():\n    pass\n")
    (ws / "secret.py").write_text("def hidden_secret_func():\n    pass\n")
    (ws / ".eeignore").write_text("secret.py\n")
    assert _policy(ws).check("read", {"path": "secret.py"}).action == "deny"
    text, _ = R.build_map(ws, tmp_path / "home", 2000)
    assert "visible_func" in text
    assert "hidden_secret_func" not in text


# ------------------------------------------------------------------ shell 只读白名单的绕过

@pytest.mark.parametrize("cmd", [
    "echo x 1>out.txt",                       # 带 fd 编号的重定向写文件
    "cat a 2>err.txt",
    "echo x &>out.txt",
    "echo x >out.txt 2>/dev/null",            # 一个重定向到 /dev/null 不能放过其他重定向
    "sed -n '1e touch pwned' f",              # GNU sed e 命令执行任意命令
    "sed -n 'w out' f",                       # sed w 写文件
    "sed 's/a/b/w out' f",
    "sed 's/a/b/e' f",
    "sed -f script.sed f",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.fsmonitor GIT_CONFIG_VALUE_0=evil git status",  # 环境变量注入配置
    "GIT_EXTERNAL_DIFF=evil git diff",
    "Get-ChildItem | ForEach-Object { Remove-Item $_ }",   # PowerShell 脚本块
    "Get-ChildItem | Where-Object { Remove-Item x; $true }",
    "Get-ChildItem | Select-Object @{n='a';e={Remove-Item x}}",
    "Write-Output (Remove-Item x)",           # PowerShell 括号表达式参数会被执行
    "echo @(Remove-Item x)",
    "cat <(rm x)",                            # bash 进程替换
    "sort --compress-program=sh big.txt",
    "pytest --basetemp=/home/me/docs",        # pytest 会清空 basetemp 目录
    "python -m pytest --junitxml=/etc/x.xml",
])
def test_readonly_shell_bypasses_denied(tmp_path, cmd):
    assert _policy(tmp_path, "read-only").check("shell", {"command": cmd}).action == "deny", cmd
    assert _policy(tmp_path).check("shell", {"command": cmd}).action == "ask", cmd


@pytest.mark.parametrize("shell,cmd", [   # 按实际 shell 的语法判（单引号在 cmd 里不是引号）
    ("posix", "sed -n '10,20p' a.py"), ("posix", "sed -n 5p a.py"), ("posix", "sed 's/foo/bar/g' a.py"),
    ("posix", "sed -n '/def /p' a.py"), ("posix", "sed -E 's#a/b#c#2' a.py"), ("posix", "sed '1d;$d' a.py"),
    ("posix", "wc -l a.py 2>&1"), ("posix", "rg foo 2>/dev/null"), ("posix", "ls 2> /dev/null"),
    ("posix", "rg 'foo(' src"), ("posix", 'grep "a{2}" f'), ("posix", "git show HEAD@{1}"),
    ("powershell", "git log -5 2>$null"), ("powershell", "Get-ChildItem -Recurse | Select-Object Name"),
    (None, "wc -l a.py 2>&1"), (None, "git show HEAD@{1}"), (None, "Get-ChildItem -Recurse | Select-Object Name"),
])
def test_readonly_shell_common_reads_still_allowed(tmp_path, shell, cmd):
    p = PermissionPolicy(mode="read-only", workspace=tmp_path, shell=shell)
    assert p.check("shell", {"command": cmd}).action == "allow", cmd
