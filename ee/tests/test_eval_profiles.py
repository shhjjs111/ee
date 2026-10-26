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
