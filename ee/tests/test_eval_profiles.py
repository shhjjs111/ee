"""eval_profiles.py 的离线自测：假的 ee 命令输出 JSONL，不调用任何模型。"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import eval_profiles as E  # noqa: E402

FAKE_EE = r'''
import json, sys, pathlib
args = sys.argv[1:]
prompt = sys.stdin.read()
prof = args[args.index("--profile") + 1]
cwd = pathlib.Path(args[args.index("-C") + 1])
p = 3000 if prof == "full" else 800
def ev(**d): print(json.dumps(d, ensure_ascii=False), flush=True)
ev(type="session", id="x", profile=prof)
ev(type="tool_start", call_id="c1", name="edit")
ev(type="tool_end", call_id="c1", name="edit", ok=False, status="error")
ev(type="usage", prompt_tokens=p, completion_tokens=50)
ev(type="usage", prompt_tokens=p, completion_tokens=50)
if prof == "full" and "做完" in prompt:
    (cwd / "done.txt").write_text("ok")
ev(type="turn_done", reason="completed", duration_ms=1500,
   usage={"prompt": 2 * p, "completion": 100, "hit": p, "cost": 0.01, "currency": "¥"})
'''

TASK = """---
id: 01-demo
title: 演示
kind: feature
check: python -c "import pathlib,sys; sys.exit(0 if pathlib.Path('done.txt').exists() else 1)"
check_cwd: .
---
把它做完。
"""


def _git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    _git("init", "-q", cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "add", ".", cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=repo)
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "01-demo.md").write_text(TASK, encoding="utf-8")
    fake = tmp_path / "fake_ee.py"
    fake.write_text(FAKE_EE, encoding="utf-8")
    return repo, tasks, fake


def test_builtin_tasks_parse():
    tasks = E.load_tasks(SCRIPTS / "eval_tasks")
    assert len(tasks) == 10 and len({t.id for t in tasks}) == 10
    kinds = {t.kind for t in tasks}
    assert {"bugfix", "feature", "multi-file", "tests", "docs", "homework"} <= kinds
    for t in tasks:
        assert t.prompt and t.check
        if "_eval_check.py" in t.check:
            assert "def test" in t.hidden_test, t.id
    assert [t.id for t in E.load_tasks(SCRIPTS / "eval_tasks", ["04"])] == ["04-feature-fmt-tokens"]


def test_run_and_summary(setup, tmp_path, monkeypatch):
    repo, tasks, fake = setup
    monkeypatch.setenv("FAKE_KEY", "sk-test")
    out = tmp_path / "out"
    code = E.main(["--repo", str(repo), "--tasks-dir", str(tasks), "--runs", "2", "--key-env", "FAKE_KEY",
                   "--ee", str(fake), "--out", str(out)])
    # --ee 给的是脚本路径：用当前 Python 跑
    assert code == 0
    rows = [json.loads(x) for x in (out / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 4 and {r["profile"] for r in rows} == {"full", "lean"}
    full = [r for r in rows if r["profile"] == "full"]
    lean = [r for r in rows if r["profile"] == "lean"]
    assert all(r["passed"] for r in full) and not any(r["passed"] for r in lean)
    assert full[0]["steps"] == 2 and full[0]["tool_calls"] == 1 and full[0]["edit_failures"] == 1
    assert full[0]["prompt_tokens"] == 6000 and lean[0]["prompt_tokens"] == 1600 and lean[0]["profile"] == "lean"
    agg = E.aggregate(rows)
    assert agg["full"]["pass_rate"] == 1 and agg["lean"]["pass_rate"] == 0
    assert agg["full"]["hit_rate"] == 0.5 and agg["full"]["avg_cost"] == pytest.approx(0.01)
    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert "| full | 2 | 100% |" in summary and "| lean | 2 | 0% |" in summary
    assert "lean 未达标" in summary and "少 73%" in summary
    assert "sk-test" not in summary and "sk-test" not in (out / "results.jsonl").read_text(encoding="utf-8")


def test_missing_key_refuses(setup, monkeypatch, capsys):
    repo, tasks, fake = setup
    monkeypatch.delenv("NOPE_KEY", raising=False)
    assert E.main(["--repo", str(repo), "--tasks-dir", str(tasks), "--key-env", "NOPE_KEY", "--ee", str(fake)]) == 2
    assert "没有 API key" in capsys.readouterr().err


def test_inject_and_selfcheck(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("X = 1\n", encoding="utf-8")
    _git("init", "-q", cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "add", ".", cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=repo)
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    chk = 'python -c "import sys; sys.exit(0 if open(\'m.py\').read() == \'X = 1\\n\' else 1)"'
    (tasks / "01-bug.md").write_text(
        f'---\nid: 01-bug\ntitle: t\ncheck: {chk}\ncheck_cwd: .\n'
        'inject: [{"file": "m.py", "old": "X = 1", "new": "X = 2"}]\nrevert_passes: true\n---\n修好它\n',
        encoding="utf-8")
    assert E.selfcheck(E.load_tasks(tasks), repo, 60) == 0
    (tasks / "02-bad.md").write_text('---\nid: 02-bad\ncheck: python -c "pass"\ncheck_cwd: .\n---\n无效任务\n',
                                     encoding="utf-8")
    assert E.selfcheck(E.load_tasks(tasks, ["02"]), repo, 60) == 1


def test_parses_real_exec_jsonl(ee_home, workspace):
    """真的 ee exec --json（假模型）输出的 JSONL 能被统计脚本正确解析。"""
    from conftest import finish, text_chunks
    from test_exec import run_exec
    code, out, _, _ = run_exec(ee_home, [text_chunks("好") + finish("stop", prompt=1234, completion=7, hit=1000)],
                               ["-C", str(workspace), "--json", "--profile", "lean", "x"])
    s = E.summarize_events(out.splitlines())
    assert code == 0 and s["reason"] == "completed" and s["profile"] == "lean"
    assert s["steps"] == 1 and s["prompt_tokens"] == 1234 and s["cache_hit_tokens"] == 1000 and s["cost"] is not None


# ---------------------------------------------------------------- --ee 默认值（cwd 无关）

def test_default_ee_cmd_does_not_depend_on_cwd(tmp_path, monkeypatch):
    """默认的 ee 命令不能依赖 cwd。

    评测会把子进程的 cwd 切到临时副本（`-C <root>`），而 `python -m ee` 要求 cwd 在
    ee/ 包目录内——cwd 一旦切走，`ee` 就会被解析成命名空间包、`__version__` 缺失，
    子进程立刻 ImportError 退出。表现是 `no_output · 0 步`，一分钱没花但也什么都没测到。

    所以默认命令必须是一个**绝对路径的可执行文件**（venv 里的 ee.exe），
    或者显式带上包目录让它可被导入。
    """
    import importlib.util
    from pathlib import Path

    from scripts.eval_profiles import resolve_ee_cmd

    cmd = resolve_ee_cmd(None)
    assert cmd, "默认命令不能为空"
    # 判据一：不能是 "python -m ee" 这种依赖 cwd 的形式
    assert not (len(cmd) >= 3 and cmd[1] == "-m"), f"默认命令仍依赖 cwd：{cmd}"
    # 判据二：第一个元素是可执行文件（绝对路径或能在 PATH 找到）
    first = cmd[0]
    looks_exe = Path(first).is_file() or importlib.util.find_spec("shutil") is not None and bool(
        __import__("shutil").which(first))
    assert looks_exe, f"默认命令的第一个元素不是可执行文件：{first}"


def test_explicit_ee_exe_is_used_as_is(tmp_path):
    """显式给可执行文件路径时原样使用（这是我们验证过的可用方式）。"""
    from scripts.eval_profiles import resolve_ee_cmd

    exe = tmp_path / "ee.exe"
    exe.write_text("", encoding="utf-8")
    assert resolve_ee_cmd(str(exe)) == [str(exe)]


def test_summarize_events_splits_tool_time_from_wall_clock():
    """工具耗时必须能从事件流里单独算出来。

    起因：原先只有 turn_done.duration_ms（整回合墙钟），性能诊断时只能拿它除以步数，
    把「一条命令跑满 600 秒」误读成「每步模型都慢」。tool_end 现在带 duration_ms /
    timed_out，汇总要能把两者分开，并指出最慢的那一次。
    """
    lines = [
        json.dumps({"type": "session", "profile": "full"}),
        json.dumps({"type": "usage", "prompt_tokens": 100, "completion_tokens": 10}),
        json.dumps({"type": "tool_start", "name": "shell", "call_id": "c1"}),
        # 一次正常的快调用
        json.dumps({"type": "tool_end", "name": "read", "call_id": "c2", "status": "ok",
                    "duration_ms": 40, "timed_out": False}),
        # 一次跑满上限的慢调用——正是实测里 600.3 秒那种
        json.dumps({"type": "tool_end", "name": "shell", "call_id": "c1", "status": "error",
                    "duration_ms": 600_300, "timed_out": True}),
        json.dumps({"type": "turn_done", "reason": "completed", "duration_ms": 669_000,
                    "usage": {"prompt": 95735, "completion": 1747, "hit": 86784}}),
    ]
    s = E.summarize_events(lines)
    assert s["tool_s"] == 600.3                     # 600_300 + 40 ms → 秒
    assert s["tool_timed_out"] == 1
    assert s["slowest_tool"]["name"] == "shell"
    assert s["slowest_tool"]["duration_ms"] == 600_300
    assert s["duration_s"] == 669.0                 # 墙钟仍然是整回合
    # 关键区分：工具只占 600.3s，剩下约 68.7s 才是模型 + 启动开销
    assert s["duration_s"] - s["tool_s"] < 100


def test_summarize_events_without_tool_timings_is_safe():
    """老版本 ee 的事件流没有 duration_ms：不能炸，也不能把 None 加起来。"""
    lines = [
        json.dumps({"type": "tool_end", "name": "read", "call_id": "c1", "status": "ok"}),
        json.dumps({"type": "turn_done", "reason": "completed", "duration_ms": 1000,
                    "usage": {"prompt": 1, "completion": 1}}),
    ]
    s = E.summarize_events(lines)
    assert s["tool_s"] == 0.0
    assert s["slowest_tool"] is None
    assert s["tool_timed_out"] == 0


def test_summarize_events_records_what_slow_tool_was_doing():
    """最慢的那次工具调用必须能看出「在跑什么」。

    起因：实测里出现过 180.3 秒的 shell 调用，但结果文件里只有
    {'name': 'shell', 'duration_ms': 180328} —— 命令原文丢了，没法判断是
    合理的全量测试还是模型跑偏。tool_start 事件本来就带 args，汇总时按
    call_id 接回来即可。
    """
    lines = [
        json.dumps({"type": "tool_start", "call_id": "c1", "name": "shell",
                    "summary": "pytest", "args": {"command": "python -m pytest -q", "timeout": 600}}),
        json.dumps({"type": "tool_end", "call_id": "c1", "name": "shell", "status": "error",
                    "duration_ms": 180_328, "timed_out": True}),
        json.dumps({"type": "tool_start", "call_id": "c2", "name": "read",
                    "summary": "读文件", "args": {"path": "ee/core/context.py"}}),
        json.dumps({"type": "tool_end", "call_id": "c2", "name": "read", "status": "ok",
                    "duration_ms": 12}),
        json.dumps({"type": "turn_done", "reason": "completed", "duration_ms": 200_000,
                    "usage": {"prompt": 100, "completion": 10}}),
    ]
    s = E.summarize_events(lines)
    assert s["slowest_tool"]["duration_ms"] == 180_328
    assert s["slowest_tool"]["what"] == "python -m pytest -q"      # 命令原文不再丢
    # tools 按耗时降序，且每条都带上下文
    assert [t["name"] for t in s["tools"]] == ["shell", "read"]
    assert s["tools"][0]["timed_out"] is True and s["tools"][0]["s"] == 180.3
    assert s["tools"][1]["what"].endswith("context.py")


def test_summarize_events_tool_without_start_is_safe():
    """tool_end 找不到对应的 tool_start（事件流被截断）时不能炸。"""
    lines = [
        json.dumps({"type": "tool_end", "call_id": "zz", "name": "shell", "status": "ok",
                    "duration_ms": 5, "summary": "Ran ls"}),
        json.dumps({"type": "turn_done", "reason": "completed", "duration_ms": 10,
                    "usage": {"prompt": 1, "completion": 1}}),
    ]
    s = E.summarize_events(lines)
    assert s["slowest_tool"]["duration_ms"] == 5
    assert s["slowest_tool"]["what"] == "Ran ls"        # 退回 summary
