import sys

import pytest
from conftest import run

from ee.tools import ToolContext, ToolError, ToolState
from ee.tools.search import GlobTool, GrepTool, _glob_match
from ee.tools.shell import ShellTool
from ee.tools.shellenv import ShellNotFound, resolve_shell
from ee.tools.todo import TodoTool
from ee.tools.truncate import truncate_output


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
    r = run(ShellTool().run({"command": "echo hello && exit 3"}, _ctx(workspace, resolve_shell("auto"))))
    assert "hello" in r.output and "exit 3" in r.output and not r.ok


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
    argv = sh.argv("Get-ChildItem")
    import base64
    assert argv[:5] == ["pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand"]
    script = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert script.endswith("Get-ChildItem") and "UTF8" in script
    assert ShellInfo("cmd", "cmd.exe", "cmd").argv("dir")[:4] == ["cmd.exe", "/d", "/s", "/c"]


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
