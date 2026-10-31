import pytest

from ee.core.permissions import PermissionPolicy, command_prefix, split_command


def pol(ws, mode="default", **kw):
    return PermissionPolicy(mode=mode, workspace=ws, **kw)


def test_split_command():
    assert split_command("ls -la && git status | head -5; echo hi") == ["ls -la", "git status", "head -5", "echo hi"]
    assert split_command('echo "a && b"') == ['echo "a && b"']
    segs = split_command("echo $(rm -rf build) `curl x.com`")
    assert "rm -rf build" in segs and "curl x.com" in segs
    assert split_command("pytest -q 2>&1") == ["pytest -q 2>&1"]
    assert split_command("Get-ChildItem; Remove-Item x") == ["Get-ChildItem", "Remove-Item x"]


@pytest.mark.parametrize("cmd", [
    "git status", "git diff HEAD~1", "git log --oneline -5", "git show HEAD", "git branch", "git blame a.py",
    "ls -la | wc -l", "rg foo src", "Get-ChildItem -Recurse | Select-Object Name",
    "Get-Content a.txt", "gh pr view 12", "gh issue list", "git -C sub status", "dir", "python --version",
])
def test_safe_commands_allowed(tmp_path, cmd):
    assert pol(tmp_path).check("shell", {"command": cmd}).action == "allow"


@pytest.mark.parametrize("cmd", [
    "git commit -m x", "git checkout main", "git switch dev", "git stash", "git merge dev", "git rebase main",
    "npm install", "curl https://x.com", "echo hi > out.txt", "gh pr create", "gh pr merge 1", "git branch -d old",
    "Invoke-WebRequest https://x.com", "python script.py", "ls && rm a.txt",
    "pytest -q", "python -m pytest tests",   # pytest 会执行仓库里的 conftest.py，不算只读
])
def test_unsafe_commands_ask(tmp_path, cmd):
    assert pol(tmp_path).check("shell", {"command": cmd}).action == "ask"


@pytest.mark.parametrize("cmd", [
    "rm -rf /", "rm -rf ~", "sudo rm -rf / ", "git push --force origin main", "git push -f",
    "Remove-Item -Recurse -Force C:\\", "Remove-Item C:\\ -Recurse -Force", "del /s /q C:\\", "rd /s /q C:\\",
    "format C:", "mkfs.ext4 /dev/sda1", "ls; git push origin +main",
])
def test_deny_even_in_auto(tmp_path, cmd):
    assert pol(tmp_path, "auto").check("shell", {"command": cmd}).action == "deny"


@pytest.mark.parametrize("cmd", [
    "git reset --hard HEAD~1", "git clean -fd", "Remove-Item -Recurse -Force build", "rm -rf build",
    "del /s /q build", "rd /s /q build", "git push --force-with-lease", "git checkout -- a.py",
])
def test_force_ask_even_in_auto(tmp_path, cmd):
    d = pol(tmp_path, "auto").check("shell", {"command": cmd})
    assert d.action == "ask" and d.force


def test_auto_allows_normal(tmp_path):
    assert pol(tmp_path, "auto").check("shell", {"command": "npm install"}).action == "allow"
    assert pol(tmp_path, "auto").check("write", {"path": "/etc/x"}).action == "allow"


def test_readonly_mode(tmp_path):
    p = pol(tmp_path, "read-only")
    assert p.check("shell", {"command": "git status"}).action == "allow"
    assert p.check("shell", {"command": "git commit -m x"}).action == "deny"
    assert p.check("edit", {"path": "a.py"}).action == "deny"
    assert p.check("read", {"path": "a.py"}).action == "allow"


def test_write_inside_outside_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    p = pol(ws)
    assert p.check("write", {"path": "src/a.py"}).action == "allow"
    assert p.check("edit", {"path": str(ws / "b.py")}).action == "allow"
    d = p.check("write", {"path": str(tmp_path / "other" / "x.py")})
    assert d.action == "ask" and d.prefix
    p.remember(d, "write")
    assert p.check("write", {"path": str(tmp_path / "other" / "y.py")}).action == "allow"
    assert p.check("write", {"path": ".git/config"}).action == "ask"
    patch = "*** Begin Patch\n*** Add File: ../escape.txt\n+x\n*** End Patch"
    assert p.check("apply_patch", {"patch": patch}).action == "ask"


def test_config_rules(tmp_path):
    p = pol(tmp_path, allow=["shell(make *)"], deny=["shell(npm publish*)", "write(*.env)"])
    assert p.check("shell", {"command": "make test"}).action == "allow"
    assert p.check("shell", {"command": "npm publish"}).action == "deny"
    assert p.check("write", {"path": "prod.env"}).action == "deny"


def test_prefix_and_session_memory(tmp_path):
    assert command_prefix("git commit -m 'x'") == "git commit"
    assert command_prefix("python -m pip install x") == "python -m pip"
    p = pol(tmp_path)
    d = p.check("shell", {"command": "git commit -m a"})
    p.remember(d, "shell")
    assert p.check("shell", {"command": "git commit -am b"}).action == "allow"
    assert p.check("shell", {"command": "git push"}).action == "ask"
    # 强制询问类不受“以后都行”影响
    assert p.check("shell", {"command": "git commit -m a && git reset --hard"}).action == "ask"


def test_readonly_whitelist_bypass_flags(tmp_path):
    """白名单命令里能执行程序 / 写文件的参数不能算只读（2026-10-26 仓库审查补）。"""
    from ee.core.permissions import PermissionPolicy
    p = PermissionPolicy(mode="read-only", workspace=tmp_path)
    for c in ["git diff --output=x", "rg foo --pre rm", "git -c core.pager=rm log", "git diff --ext-diff",
              "sort -o x a", "uniq a b", "tree -o x", "find . -fprintf x %p", "git grep -O foo"]:
        assert p.check("shell", {"command": c}).action == "deny", c
    for c in ["git -C sub status", "git log --oneline -5", "rg -n foo", "sort a", "git diff --stat", "uniq a"]:
        assert p.check("shell", {"command": c}).action == "allow", c


def test_readonly_whitelist_bypass_clustered_flags(tmp_path):
    """危险选项的**连写**形式（-cX ≡ -c X、-oX ≡ -o X）同样不能算只读。

    背景：短选项簇走 `-{body[0]}` 回退放行，于是 `-ccore.fsmonitor=calc.exe` 被当成
    「-c + 紧贴的值」直接 allow，而 getopt 语义里它就是 -c（注入配置、可执行任意程序）；
    `sort -no secret.txt data` 同理等于 `-n -o secret.txt`，会静默覆盖工作区文件。
    """
    from ee.core.permissions import PermissionPolicy
    p = PermissionPolicy(mode="read-only", workspace=tmp_path)
    for c in ["git -ccore.fsmonitor=calc.exe status", "git -c core.fsmonitor=calc.exe status",
              "git -Ofoo status", "sort -no secret.txt data", "sort -osecret.txt data",
              "tree -ox a", "git -c core.pager=rm log"]:
        assert p.check("shell", {"command": c}).action != "allow", c
    # 不带值选项的正常簇仍然可用（别把 -la / -n5 一起打死）
    for c in ["ls -la", "sort -n data", "git log --oneline -5", "git status"]:
        assert p.check("shell", {"command": c}).action == "allow", c


def test_eeignore_also_blocks_shell_reads(tmp_path):
    """.eeignore 排除的文件，shell 也必须挡住。

    拒绝消息里写着「也不要用 shell 绕过」，但 _eeignored 只覆盖
    read/write/edit/apply_patch，shell 完全不管：`cat .env` / `rg secret`
    免问就直接读到被排除的内容（报告 P2-24）。
    """
    from ee.core.permissions import PermissionPolicy
    (tmp_path / ".eeignore").write_text(".env\n", encoding="utf-8")
    p = PermissionPolicy(mode="default", workspace=tmp_path)
    for c in ["cat .env", "rg secret .env", "type .env", "Get-Content .env"]:
        d = p.check("shell", {"command": c})
        assert d.action != "allow", f".eeignore 被 shell 绕过了：{c} → {d.action}"
    # 无关命令不受影响
    assert p.check("shell", {"command": "ls"}).action == "allow"
