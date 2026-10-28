"""审查修复：edit 模糊匹配不得改错代码；工具规则不丢；SYSTEM.md 只换基础段；eval_profiles 在 Windows 可用。"""
import pytest
from conftest import run

from ee.tools import ToolContext, ToolError, ToolState
from ee.tools.edit import EditTool
from ee.tools.fileops import Edit, apply_edits, replace
from ee.tools.read import ReadTool


def _ctx(ws):
    return ToolContext(cwd=ws, state=ToolState(), output_dir=ws / ".out")


def _prep(ws, name, raw: bytes):
    p = ws / name
    p.write_bytes(raw)
    ctx = _ctx(ws)
    run(ReadTool().run({"path": name}, ctx))
    return p, ctx


# ------------------------------------------------------------------ 1) 模糊匹配不改错

PY = "def f(x):\n    if x:\n        a = 1\n        b = 2\n    return a\n"


def test_dedented_old_string_keeps_original_indent():
    # 模型把缩进丢了：old/new 都顶格写。命中后必须按原文缩进对齐，不能把代码顶到行首
    out, strat, _ = replace(PY, "a = 1\nb = 2", "a = 10\nb = 2")
    assert out == "def f(x):\n    if x:\n        a = 10\n        b = 2\n    return a\n"
    assert strat == "indent"


def test_over_indented_old_string_shifts_back():
    out, _, _ = replace(PY, "            a = 1\n            b = 2", "            a = 1\n            c = 3\n            b = 2")
    assert out == "def f(x):\n    if x:\n        a = 1\n        c = 3\n        b = 2\n    return a\n"


def test_relative_indent_kept_in_new_lines():
    # new 里新增的嵌套行（相对缩进更深）也按同一偏移对齐
    out, _, _ = replace(PY, "if x:\n    a = 1", "if x:\n    if a:\n        a = 1")
    assert out == "def f(x):\n    if x:\n        if a:\n            a = 1\n        b = 2\n    return a\n"


def test_relative_indent_mismatch_is_rejected():
    # old 的相对缩进和原文不同（不是整体平移）：无法确定该怎么对齐，必须报错，文件不动
    with pytest.raises(ToolError):
        replace(PY, "if x:\na = 1", "if x:\na = 5")


def test_tabs_vs_spaces_nested_rejected():
    src = "func f() {\n\tif x {\n\t\ta := 1\n\t}\n}\n"
    with pytest.raises(ToolError):
        replace(src, "    if x {\n        a := 1", "    if x {\n        a := 2")


def test_misremembered_middle_line_not_overwritten():
    # 旧实现按首尾锚点“最接近”命中，再用模型记错的 old_string 覆盖原文的中间行
    src = "def h():\n    x = compute(1, 2, retries=3)\n    y = x + 1\n    return y\n"
    old = "def h():\n    x = compute(1, 2)\n    y = x + 1\n    return y"
    new = "def h():\n    x = compute(1, 2)\n    y = x + 2\n    return y"
    with pytest.raises(ToolError, match="最接近"):
        replace(src, old, new)


def test_nfkc_semantic_chars_not_folded():
    # 上标²、连字ﬁ 之类 NFKC 会折叠成别的字符，语义不同，不能算匹配
    with pytest.raises(ToolError):
        replace('area = "m²"\n', 'area = "m2"', 'area = "m3"')
    with pytest.raises(ToolError):
        replace("name = 'ﬁle'\n", "name = 'file'", "name = 'path'")


def test_changed_line_with_symbol_mismatch_errors_not_rewrites():
    # 要改的那一行本身和原文有全角/弯引号差异：不知道改后该用哪种符号，宁可报错并给出原文
    src = 'print("价格：１００")\nx = 1\n'
    with pytest.raises(ToolError, match="价格：１００"):
        replace(src, 'print("价格:100")', 'print("价格:200")')


def test_symbol_mismatch_on_context_line_keeps_original():
    src = '# 说明：“保持”\nx = 1\n'
    out, strat, _ = replace(src, '# 说明:"保持"\nx = 1', '# 说明:"保持"\nx = 2')
    assert strat == "unicode" and out == '# 说明：“保持”\nx = 2\n'


def test_indent_fix_crlf_bom_file(workspace):
    raw = "\ufeffclass A:\r\n    def g(self):\r\n        return 1\r\n".encode("utf-8")
    p, ctx = _prep(workspace, "a.py", raw)
    r = run(EditTool().run({"path": "a.py", "edits": [{"old_string": "def g(self):\n    return 1",
                                                        "new_string": "def g(self):\n    return 2"}]}, ctx))
    assert r.ok
    assert p.read_bytes() == "\ufeffclass A:\r\n    def g(self):\r\n        return 2\r\n".encode("utf-8")


def test_trailing_newline_delete_whole_lines():
    src = "a\n    b\n    c\nd\n"
    out, _, _ = replace(src, "b\nc\n", "")
    assert out == "a\nd\n"


# ------------------------------------------------------------------ 2) 工具描述压缩后关键规则补回

def _prefix_text(workspace, profile=None):
    from test_prefix_budget import _prefix
    _, p = _prefix(workspace, **({"profile": profile} if profile else {}))
    tools = {t["function"]["name"]: t["function"]["description"] for t in p.tools}
    return p.system["content"], tools


@pytest.mark.parametrize("profile", [None, "lean"])
def test_key_edit_and_shell_rules_present(ee_home, workspace, profile):
    system, tools = _prefix_text(workspace, profile)
    blob = system + "\n" + tools["edit"] + "\n" + tools["write"]
    assert "先 read" in tools["edit"]                       # edit 前必须先 read
    assert "唯一" in blob and "replace_all" in blob         # old_string 必须唯一
    assert "缩进" in blob                                   # 保留原缩进
    assert "行号" in blob                                   # 不带行号前缀
    assert "新建" in blob                                   # 新建用 write
    # 不要用 shell 写文件改代码
    assert any(("shell" in line and "写文件" in line) for line in system.split("\n"))


def test_full_mode_keeps_dedicated_tool_rules(ee_home, workspace):
    system, _ = _prefix_text(workspace)
    assert "cat" in system and "grep" in system and "glob" in system


# ------------------------------------------------------------------ 3) SYSTEM.md 只替换基础段

def test_system_md_replaces_only_base_tool_rules_kept(ee_home, workspace):
    from conftest import collect, finish, make_session, text_chunks
    from ee.core.context import builtin_base
    from ee.tools import tool_rules
    (ee_home / "SYSTEM.md").write_bytes("\ufeff我的提示词\r\n".encode("utf-8"))
    s, fake = make_session(workspace, [text_chunks("好") + finish("stop")])
    run(collect(s, "你好"))
    text = fake.calls[0]["messages"][0]["content"]
    assert text.startswith("我的提示词\n\n# 工具使用规则\n")
    assert builtin_base() not in text
    for r in tool_rules(s.tools):
        assert f"- {r}" in text


def test_system_md_tool_rules_follow_enabled_tools(ee_home, workspace):
    from ee.core import sysprompt as SP
    from ee.core.context import system_prompt
    from ee.tools import tool_rules
    from test_prefix_budget import _prefix
    s, _ = _prefix(workspace, profile="lean")
    (ee_home / "SYSTEM.md").write_text("X", encoding="utf-8")
    src = SP.resolve(workspace, ee_home, {}, {})
    text = system_prompt(s.profile, "", src, s.tools, s.agent_profile)
    assert text.startswith("X\n\n# 工具使用规则\n")
    rules = tool_rules(s.tools)
    assert rules and all(f"- {r}" in text for r in rules)


# ------------------------------------------------------------------ 4) eval_profiles.py：Windows 可用 + --preset

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import eval_profiles as E  # noqa: E402

TASK_MD = """---
id: 01-demo
title: 演示
kind: feature
check: python -c "import pathlib,sys; sys.exit(0 if pathlib.Path('done.txt').exists() else 1)"
check_cwd: .
---
把它做完。
"""

FAKE = r'''
import json, sys, pathlib, subprocess, time
args = sys.argv[1:]
if "--help" in args or "-h" in args:
    print("usage: ee exec [-C CD] [--max-steps N] [--json]" + (" [--profile P]" if PROFILE_FLAG else ""))
    sys.exit(0)
if "--profile" in args and not PROFILE_FLAG:
    print("error: unrecognized arguments: --profile", file=sys.stderr); sys.exit(2)
prompt = sys.stdin.read()
cwd = pathlib.Path(args[args.index("-C") + 1])
def ev(**d): print(json.dumps(d, ensure_ascii=False), flush=True)
ev(type="session", id="x", profile="full", version=VERSION)
if "挂住" in prompt:
    g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])   # 孙进程继承管道
    import os; pathlib.Path(os.environ["EVAL_PID_FILE"]).write_text(str(g.pid))
    time.sleep(60)
ev(type="usage", prompt_tokens=TOKENS, completion_tokens=5)
(cwd / "done.txt").write_text(VERSION)
ev(type="turn_done", reason="completed", duration_ms=100,
   usage={"prompt": TOKENS, "completion": 5, "hit": 0, "cost": 0.01, "currency": "¥"})
'''


def _fake(version, tokens, profile_flag):
    return f"VERSION = {version!r}\nTOKENS = {tokens}\nPROFILE_FLAG = {profile_flag}\n" + FAKE


def _git(*a, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def evrepo(tmp_path):
    repo = tmp_path / "repo"
    pkg = repo / "ee" / "ee"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__main__.py").write_text(_fake("base", 3000, False), encoding="utf-8")
    _git("init", "-q", cwd=repo)
    _git("add", ".", cwd=repo)
    _git("commit", "-qm", "base", cwd=repo)
    (pkg / "__main__.py").write_text(_fake("cur", 800, True), encoding="utf-8")
    _git("commit", "-qam", "cur", cwd=repo)
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "01-demo.md").write_text(TASK_MD, encoding="utf-8")
    return repo, tasks


def _gbk_console(monkeypatch):
    out = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")
    err = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    return out, err


def test_eval_gbk_console_does_not_crash(evrepo, tmp_path, monkeypatch):
    repo, tasks = evrepo
    fake = tmp_path / "fake_ee.py"
    fake.write_text(_fake("x", 1000, True), encoding="utf-8")
    monkeypatch.setenv("FAKE_KEY", "sk-test")
    _gbk_console(monkeypatch)
    code = E.main(["--repo", str(repo), "--tasks-dir", str(tasks), "--runs", "1", "--profiles", "full",
                   "--key-env", "FAKE_KEY", "--ee", str(fake)])
    assert code == 0


def test_eval_timeout_kills_process_tree(evrepo, tmp_path, monkeypatch):
    repo, tasks = evrepo
    (tasks / "01-demo.md").write_text(TASK_MD.replace("把它做完。", "挂住"), encoding="utf-8")
    fake = tmp_path / "fake_ee.py"
    fake.write_text(_fake("x", 1000, True), encoding="utf-8")
    monkeypatch.setenv("FAKE_KEY", "sk-test")
    monkeypatch.setenv("EVAL_PID_FILE", str(tmp_path / "grandchild.pid"))
    out = tmp_path / "out"
    t0 = time.monotonic()
    code = E.main(["--repo", str(repo), "--tasks-dir", str(tasks), "--runs", "1", "--profiles", "full",
                   "--key-env", "FAKE_KEY", "--ee", str(fake), "--timeout", "3", "--out", str(out)])
    assert time.monotonic() - t0 < 30
    rec = json.loads((out / "results.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert code == 0 and rec["reason"] == "timeout"
    # 整棵进程树都要结束（Windows 上孙进程拿着管道不放会让 communicate 一直等）
    pid = int((tmp_path / "grandchild.pid").read_text())
    time.sleep(0.5)
    alive = True
    try:
        os.kill(pid, 0)
        st = Path(f"/proc/{pid}/stat")
        alive = not (st.exists() and st.read_text().split()[2] == "Z")
    except OSError:
        alive = False
    assert not alive


def test_eval_check_argv_without_shell():
    argv = E.check_argv('python -m pytest -q tests/a.py tests/_eval_check.py')
    assert argv == [sys.executable, "-m", "pytest", "-q", "tests/a.py", "tests/_eval_check.py"]
    argv = E.check_argv('python -c "import sys; sys.exit(0)"')
    assert argv == [sys.executable, "-c", "import sys; sys.exit(0)"]
    assert E.check_argv("python -m pytest && echo ok") is None      # 有 shell 运算符才交给 shell


def test_eval_ee_cmd_resolution(tmp_path):
    py = tmp_path / "x y" / "fake.py"
    py.parent.mkdir()
    py.write_text("", encoding="utf-8")
    assert E.resolve_ee_cmd(str(py)) == [sys.executable, str(py)]
    # 默认命令**不能**依赖 cwd。历史上这里断言的是 `[python, "-m", "ee"]`，
    # 而那正是 1bc0e12 修掉的缺陷：评测会把子进程 cwd 切到临时副本，`-m ee`
    # 在那里解析不到包，表现为 `no_output · 0 步`（一分钱没花，也什么都没测到）。
    # 1bc0e12 改了实现但漏改这条断言，全量套件里就一直红着。
    cmd = E.resolve_ee_cmd(None)
    assert cmd, "默认命令不能为空"
    assert not (len(cmd) >= 2 and cmd[1] == "-m"), f"默认命令仍依赖 cwd：{cmd}"
    # 要么是现成的可执行文件，要么是 -c + sys.path 这种不依赖 cwd 的形式
    first_is_exe = Path(cmd[0]).is_file() or bool(__import__("shutil").which(cmd[0]))
    assert first_is_exe, f"首个元素不是可执行文件：{cmd[0]}"
    bat = tmp_path / "ee.cmd"
    bat.write_text("", encoding="utf-8")
    assert E.resolve_ee_cmd(str(bat), windows=True) == ["cmd.exe", "/d", "/s", "/c", str(bat)]


def test_eval_preset_definitions():
    assert E.PRESETS["a2"]["base"] == "a6e2b22~1"     # 等同 a6e2b22^；cmd.exe 里 ^ 是转义符
    assert E.PRESETS["a3"]["base"] == "4cc28dc~1"


def test_eval_preset_worktree_compare(evrepo, tmp_path, monkeypatch):
    repo, tasks = evrepo
    monkeypatch.setenv("FAKE_KEY", "sk-test")
    out = tmp_path / "out"
    code = E.main(["--repo", str(repo), "--tasks-dir", str(tasks), "--runs", "1", "--key-env", "FAKE_KEY",
                   "--preset", "a2", "--base", "HEAD~1", "--out", str(out)])
    assert code == 0
    rows = [json.loads(x) for x in (out / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    by = {r["variant"]: r for r in rows}
    assert set(by) == {"base", "cur"}
    assert by["base"]["prompt_tokens"] == 3000 and by["cur"]["prompt_tokens"] == 800   # 基线确实跑的旧代码
    assert all(r["passed"] for r in rows)
    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert "base" in summary and "cur" in summary and "HEAD~1" in summary
    wt = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True).stdout
    assert len(wt.strip().splitlines()) == 1                                       # 基线 worktree 已清理
