import sys

import pytest
from conftest import run

from ee.tools import ToolContext, ToolError, ToolState
from ee.tools.search import GlobTool, GrepTool, _glob_match
from ee.tools.shell import ShellTool
from ee.tools.shellenv import ShellNotFound, resolve_shell
from ee.tools.todo import TodoTool
from ee.tools.truncate import truncate_output, truncate_output_info


def test_truncate_by_lines(tmp_path):
    text = "\n".join(f"line {i}" for i in range(5000))
    out, cut = truncate_output(text, tmp_path, "t")
    assert cut and "line 0" in out and "line 4999" in out and "line 2500" not in out
    saved = list(tmp_path.glob("*-t-*.txt"))
    assert saved and saved[0].read_text() == text and str(saved[0]) in out


def test_truncate_by_bytes(tmp_path):
    text = "x" * 60_000
    out, cut = truncate_output(text, tmp_path, "b")
    assert cut and len(out.encode()) < 30_000


def test_no_truncate_small(tmp_path):
    assert truncate_output("small", tmp_path) == ("small", False)


def _ctx(ws, shell=None):
    return ToolContext(cwd=ws, state=ToolState(), output_dir=ws / ".out", shell=shell)


def test_shell_runs_and_reports_exit(workspace):
    sh = resolve_shell("auto")
    cmd = "echo hello; exit 3" if sh.syntax == "powershell" else "echo hello && exit 3"
    r = run(ShellTool().run({"command": cmd}, _ctx(workspace, sh)))
    assert "hello" in r.output and "exit 3" in r.output and not r.ok
    assert "CLIXML" not in r.output


def test_shell_failed_command_reports_error(workspace):
    sh = resolve_shell("auto")
    cmd = "Get-Item C:/__ee_no_such__" if sh.syntax == "powershell" else "ls /__ee_no_such__"
    r = run(ShellTool().run({"command": cmd}, _ctx(workspace, sh)))
    assert not r.ok and "CLIXML" not in r.output and "<Objs" not in r.output


def test_shell_timeout(workspace):
    r = run(ShellTool().run({"command": "sleep 10", "timeout": 1}, _ctx(workspace, resolve_shell("auto"))))
    assert "超时" in r.output and not r.ok


def test_shell_long_output_truncated(workspace):
    cmd = f'{sys.executable} -c "print(chr(10).join(str(i) for i in range(6000)))"'
    r = run(ShellTool().run({"command": cmd}, _ctx(workspace, resolve_shell("auto"))))
    assert "已截断" in r.output and "5999" in r.output


def test_shell_resolution_errors():
    with pytest.raises(ShellNotFound):
        resolve_shell("fish")
    if not sys.platform.startswith("win"):
        assert resolve_shell("auto").syntax == "posix"


def test_powershell_argv():
    from ee.tools.shellenv import ShellInfo
    sh = ShellInfo("pwsh", "pwsh.exe", "PowerShell 7 (pwsh)")
    argv, path = sh.prepare("Get-ChildItem 中文")
    try:
        assert argv[:4] == ["pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive"]
        assert argv[-2] == "-File" and argv[-1] == str(path)
        raw = path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        script = raw.decode("utf-8-sig")
        assert "Get-ChildItem 中文" in script and "UTF8" in script and "LASTEXITCODE" in script
    finally:
        path.unlink()
    assert ShellInfo("cmd", "cmd.exe", "cmd").argv("dir")[:4] == ["cmd.exe", "/d", "/s", "/c"]


def test_strip_clixml():
    from ee.tools.shellenv import strip_clixml
    raw = 'hello\n#< CLIXML\n<Objs Version="1.1.0.1"><S S="Error">boom_x000D__x000A_</S></Objs>\n[x]'
    assert strip_clixml(raw) == "hello\nboom\r\n\n[x]"
    assert strip_clixml("plain") == "plain"


def test_grep_and_glob(workspace):
    (workspace / "src").mkdir()
    (workspace / "src" / "a.py").write_text("def main():\n    pass\n", encoding="utf-8")
    (workspace / "src" / "b.ts").write_text("function main() {}\n", encoding="utf-8")
    ctx = _ctx(workspace)
    r = run(GrepTool().run({"pattern": r"def\s+main"}, ctx))
    assert "a.py" in r.output and "1: def main" in r.output and "b.ts" not in r.output
    r = run(GrepTool().run({"pattern": "main", "include": "*.ts", "output_mode": "files"}, ctx))
    assert r.output.strip().endswith("b.ts")
    r = run(GlobTool().run({"pattern": "**/*.py"}, ctx))
    assert "a.py" in r.output and "b.ts" not in r.output
    assert _glob_match("src/x/y.tsx", "src/**/*.{ts,tsx}")


def test_grep_python_fallback(workspace, monkeypatch):
    import ee.tools.search as S
    monkeypatch.setattr(S.shutil, "which", lambda n: None)
    (workspace / "a.py").write_text("hello world\n", encoding="utf-8")
    r = run(GrepTool().run({"pattern": "hello"}, _ctx(workspace)))
    assert "a.py" in r.output
    r = run(GlobTool().run({"pattern": "*.py"}, _ctx(workspace)))
    assert "a.py" in r.output


def test_todo_rules(workspace):
    ctx = _ctx(workspace)
    r = run(TodoTool().run({"todos": [{"content": "a", "status": "completed"}, {"content": "b", "status": "in_progress"}]}, ctx))
    assert "1/2" in r.output and ctx.state.todos[1]["status"] == "in_progress"
    with pytest.raises(ToolError):
        run(TodoTool().run({"todos": [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "in_progress"}]}, ctx))


# ---------------------------------------------------------------- 按工具调阈值（缓存优化）
#
# 为什么需要：DeepSeek 的前缀缓存只在「用户输入末尾」和「模型输出末尾」落缓存单元，
# 后续请求必须完全匹配某个单元才命中。一个回合内每多一次工具调用，之前的工具结果就
# 要重新计费一次。所以**工具输出越大，后续每一步的重复成本越高**。
#
# 实测（~/.ee/sessions 9 个真实会话 / 2084 次请求 / 命中率 97.19%）：某个回合内
# 20 次工具调用、输出合计 8 万字符，实际 miss 却是 23.9 万 token——是新增量的 8.9 倍。
# 按「每个工具输出在其后每次请求都重算」建模得 36.2 万，落在同一量级，机制吻合。
#
# 天枢用同一思路（src/tools/artifact-threshold.ts）：按工具类型设不同阈值，
# 并明确「Cache impact: zero」——只改归档阈值，不动工具定义/系统提示/请求构造。
# 这里给 web_fetch / web_search 这类「单次输出大、后续极少复用」的工具用更严的阈值。

def test_web_fetch_truncated_earlier_than_default(tmp_path):
    """web_fetch 的输出上限必须比默认阈值小：网页正文是缓存杀手。"""
    from ee.tools.truncate import limit_for, MAX_BYTES
    assert limit_for("web_fetch").bytes < MAX_BYTES, "web_fetch 应比默认更早归档"


def test_web_search_truncated_earlier_than_default(tmp_path):
    from ee.tools.truncate import limit_for, MAX_BYTES
    assert limit_for("web_search").bytes < MAX_BYTES


def test_small_tool_output_not_truncated(tmp_path):
    """普通小输出不受影响：不能为了缓存把所有结果都砍掉。"""
    text = "x" * 5_000
    out, cut = truncate_output(text, tmp_path, "read")
    assert not cut and out == text


def test_unknown_tool_uses_default(tmp_path):
    from ee.tools.truncate import limit_for, MAX_BYTES
    assert limit_for("some_new_tool").bytes == MAX_BYTES


def test_web_fetch_big_output_wrapped_and_recoverable(tmp_path):
    """大网页正文被归档后，必须能按路径找回全文（不能真丢）。"""
    text = "网页正文" * 20_000          # 远超 web_fetch 阈值
    out, info = truncate_output_info(text, tmp_path, "web_fetch")
    assert info is not None and info["path"], "应归档到磁盘并给出路径"
    assert str(info["path"]) in out, "提示里必须带可读的路径"
    from pathlib import Path
    assert Path(info["path"]).read_text(encoding="utf-8") == text, "归档内容必须是完整原文"
    assert len(out) < len(text) / 2, "进历史的预览要显著变小"


def test_truncate_output_respects_per_tool_limit(tmp_path):
    from ee.tools.truncate import limit_for as _lf
    """真实调用路径必须按工具阈值走。

    上一版只测了 truncate_output_info，而生产用的是 truncate_output——它把
    MAX_LINES/MAX_BYTES 作为实参传下去，limit_for 永远收不到 None，按工具阈值
    形同虚设（实测：20KB 的 web_fetch 输出一个字都没截）。
    """
    text = "x" * (20 * 1024)          # 超过 web_fetch 的 12KB 阈值，低于 50KB 默认值
    out, cut = truncate_output(text, tmp_path, "web_fetch")
    assert cut, f"web_fetch 阈值 {_lf('web_fetch').bytes} 字节，20KB 应被截断（实际 {len(text)} 字节未截）"
    # 截断会把大段原文归档到磁盘、返回头尾预览 + 提示；只要显著短于原文即可
    assert len(out) < len(text), "截断后必须比原文短"


def test_truncate_output_unknown_tool_unaffected(tmp_path):
    """未配置的工具仍走默认 50KB：不能顺手把所有工具都改严。"""
    text = "y" * (20 * 1024)          # 低于默认 50KB
    out, cut = truncate_output(text, tmp_path, "read")
    assert not cut and out == text
