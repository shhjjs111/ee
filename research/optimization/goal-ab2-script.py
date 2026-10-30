#!/usr/bin/env python
"""严格对照实验 v2：多文件 bug，失败原因只能从测试输出定位。

任务：impl.py 的 parse_spec 把空值替换成 "<empty>"，但测试要求空值保留为空。
源码看起来"合理"（注释说避免空字符串），只有测试输出能指出预期。

每组用独立 git 仓库 + 初始 commit 锁定；每次 run 前 reset --hard。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

IMPL = '''def parse_spec(s):\n    """把 "name:value" 列表解析成 dict。"""\n    out = {}\n    for item in s.split(";"):\n        if not item.strip():\n            continue\n        k, v = item.split(":", 1)\n        # 空值回退成特殊占位，避免下游拿到空字符串\n        out[k] = v if v else "<empty>"\n    return out\n'''

TEST = '''from impl import parse_spec\n\ndef test_basic():\n    assert parse_spec("a:1;b:2") == {"a": "1", "b": "2"}\n\ndef test_empty_value():\n    # 空值也必须能解析：不能因为 value 为空就崩溃或丢掉\n    assert parse_spec("a:;b:x") == {"a": "", "b": "x"}\n\ndef test_value_with_colon():\n    assert parse_spec("url:http://x:8080") == {"url": "http://x:8080"}\n'''

PROMPT = "impl.py 的 parse_spec 有个 bug，请修复让它通过 test_parse.py 的测试"
GOAL = "python -m pytest -q test_parse.py"


def setup_repo(workdir: Path) -> tuple[Path, str]:
    repo = workdir / "repo"
    if repo.exists():
        shutil.rmtree(repo)
    repo.mkdir(parents=True)
    (repo / "impl.py").write_text(IMPL, encoding="utf-8")
    (repo / "test_parse.py").write_text(TEST, encoding="utf-8")
    for cmd in (["init", "-q"], ["config", "user.name", "ee-ab"],
                ["config", "user.email", "ab@localhost"],
                ["add", "impl.py", "test_parse.py"],
                ["commit", "-qm", "initial bug"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True)
    initial = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=True).stdout.strip()
    return repo, initial


def run_once(ee_dir: Path, repo: Path, key: str, exe: Path) -> dict:
    env = {**os.environ, "DEEPSEEK_API_KEY": key, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    cwd = ee_dir / "ee"
    cmd = [str(exe), "-m", "ee", "exec", "--json", "--no-persist",
           "--mode", "auto", "--approve", "auto", "--profile", "full",
           "--model", "deepseek-flash", "--goal", GOAL,
           "--goal-max-rounds", "3", "--goal-min-tests", "1",
           "--max-steps", "25", "--max-tool-timeout", "60",
           "-C", str(repo), PROMPT]
    try:
        r = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=600)
    except subprocess.TimeoutExpired:
        return {"met": False, "rounds": None, "cost": None, "requests": None,
                "hit_rate": None, "timeout": True}
    result = {"met": None, "rounds": None, "cost": None, "requests": None,
              "hit_rate": None, "timeout": False, "exit_code": r.returncode}
    for line in (r.stdout or "").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "turn_done":
            result["met"] = d.get("reason") == "met"
            result["rounds"] = d.get("goal", {}).get("rounds")
            u = d.get("usage") or {}
            result["cost"] = u.get("cost")
            result["requests"] = u.get("requests")
            result["hit_rate"] = u.get("hit_rate")
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", choices=["current", "baseline"], required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--worktree", required=True)
    ap.add_argument("--venv", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        print("缺少 DEEPSEEK_API_KEY", file=sys.stderr)
        return 2

    ee_dir = Path(args.worktree).resolve()
    exe = Path(args.venv).resolve()
    workdir = Path(tempfile.mkdtemp(prefix=f"ee-ab2-{args.group}-"))
    repo, initial = setup_repo(workdir)

    rows = []
    for i in range(args.runs):
        subprocess.run(["git", "-C", str(repo), "reset", "--hard", initial],
                       check=True, capture_output=True)
        rec = {"group": args.group, "run": i + 1}
        rec.update(run_once(ee_dir, repo, key, exe))
        rows.append(rec)
        print(f"[{args.group}] run {i+1}: met={rec['met']} rounds={rec['rounds']} "
              f"cost={rec['cost']} req={rec['requests']} hit={rec['hit_rate']} "
              f"timeout={rec['timeout']}", flush=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"结果写入 {out}", file=sys.stderr)
    shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
