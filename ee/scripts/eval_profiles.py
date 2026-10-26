"""完整模式（full）与精简模式（lean）的真实任务对比。

用 `ee exec --json` 在仓库的干净副本里跑一组任务（ee/scripts/eval_tasks/*.md），每个任务每种模式跑 N 次，
跑完用任务自带的验收命令判定是否完成，汇总完成率、步数、token、命中率、花费、耗时、edit 失败次数。

用法（在 ee 仓库根目录）：
  set DEEPSEEK_API_KEY=...            （PowerShell：$env:DEEPSEEK_API_KEY="..."；key 只从环境变量读）
  python ee/scripts/eval_profiles.py --runs 3
  python ee/scripts/eval_profiles.py --runs 1 --tasks 01,04 --model deepseek-pro --out eval-out
  python ee/scripts/eval_profiles.py --selfcheck      不调模型：检查每个任务的注入/验收是否有效

任务文件格式（Markdown）：
  ---
  id: 01-xxx
  title: 一句话标题
  kind: bugfix | feature | multi-file | tests | docs | homework
  check: python -m pytest -q tests/test_x.py tests/_eval_check.py   # 验收命令，退出码 0 = 完成
  check_cwd: ee                     # 验收命令的工作目录（相对仓库根），默认 ee
  inject: [{"file": "ee/ee/x.py", "old": "...", "new": "..."}]     # 可选：跑之前注入的 bug（JSON，单行）
  revert_passes: true               # 可选：未注入时验收应当通过（--selfcheck 用）
  ---
  给模型的任务说明……
  ## 验收测试
  ```python
  # 跑之前不写，跑完才写到 ee/tests/_eval_check.py（模型看不到）
  ```

不调用任何网络服务以外的东西；key 不会打印、不会写进结果文件。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECK_FILE = "ee/tests/_eval_check.py"


@dataclass
class Task:
    id: str
    title: str
    prompt: str
    check: str
    check_cwd: str = "ee"
    kind: str = ""
    inject: list = field(default_factory=list)
    hidden_test: str = ""
    revert_passes: bool = False


def parse_task(path: Path) -> Task:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise ValueError(f"{path.name}：缺少 --- 开头的头部")
    meta: dict = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    body = m.group(2)
    hidden = ""
    if "\n## 验收测试" in "\n" + body:
        body, rest = ("\n" + body).split("\n## 验收测试", 1)
        cm = re.search(r"```python\n(.*?)```", rest, re.S)
        hidden = cm.group(1) if cm else ""
    for k in ("id", "check"):
        if not meta.get(k):
            raise ValueError(f"{path.name}：缺少 {k}")
    return Task(id=meta["id"], title=meta.get("title", meta["id"]), prompt=body.strip(), check=meta["check"],
                check_cwd=meta.get("check_cwd", "ee"), kind=meta.get("kind", ""),
                inject=json.loads(meta["inject"]) if meta.get("inject") else [],
                hidden_test=hidden, revert_passes=meta.get("revert_passes", "").lower() == "true")


def load_tasks(d: Path, only: list[str] | None = None) -> list[Task]:
    tasks = [parse_task(p) for p in sorted(d.glob("*.md"))]
    if only:
        tasks = [t for t in tasks if any(t.id == o or t.id.startswith(o + "-") or t.id.startswith(o) for o in only)]
    return tasks


# ------------------------------------------------------------------ 仓库副本

def make_copy(repo: Path, dest: Path) -> None:
    """干净副本：git 仓库用本地 clone（只含已提交内容，结果可复现），否则复制目录。"""
    if (repo / ".git").exists():
        r = subprocess.run(["git", "clone", "--quiet", "--local", "--no-hardlinks", str(repo), str(dest)],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            raise RuntimeError(f"git clone 失败：{r.stderr.strip()}")
        for k, v in (("user.name", "ee-eval"), ("user.email", "ee-eval@localhost")):
            subprocess.run(["git", "-C", str(dest), "config", k, v], capture_output=True, stdin=subprocess.DEVNULL)
    else:
        shutil.copytree(repo, dest, ignore=shutil.ignore_patterns("node_modules", ".venv", "__pycache__", "dist*"))


def apply_inject(root: Path, inject: list) -> None:
    for i, it in enumerate(inject):
        p = root / it["file"]
        text = p.read_text(encoding="utf-8")
        if text.count(it["old"]) != 1:
            raise RuntimeError(f"注入 {i}：{it['file']} 里找不到唯一的原文（仓库变了？请更新任务文件）")
        p.write_bytes(text.replace(it["old"], it["new"]).encode("utf-8"))
    if inject and (root / ".git").exists():     # 注入的 bug 提交掉：模型看到的是“干净”的工作区
        subprocess.run(["git", "-C", str(root), "commit", "-qam", "eval: setup"], capture_output=True,
                       stdin=subprocess.DEVNULL)


def run_check(root: Path, task: Task, timeout: float) -> tuple[bool, str]:
    if task.hidden_test:
        f = root / CHECK_FILE
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(task.hidden_test, encoding="utf-8")
    cwd = root / task.check_cwd
    env = {**os.environ, "PYTHONPATH": str(root / "ee"), "PYTHONDONTWRITEBYTECODE": "1"}
    cmd = task.check.replace("python ", f'"{sys.executable}" ', 1) if task.check.startswith("python ") else task.check
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env,
                           stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False, "验收超时"
    tail = (r.stdout + r.stderr).strip().splitlines()[-5:]
    return r.returncode == 0, "\n".join(tail)


# ------------------------------------------------------------------ 跑一次

def summarize_events(lines: list[str]) -> dict:
    ev = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            ev.append(json.loads(ln))
        except ValueError:
            continue
    done = next((e for e in reversed(ev) if e.get("type") == "turn_done"), {})
    usage = done.get("usage") or {}
    tool_ends = [e for e in ev if e.get("type") == "tool_end"]
    return {
        "reason": done.get("reason") or ("no_output" if not ev else "crashed"),
        "steps": sum(1 for e in ev if e.get("type") == "usage"),
        "tool_calls": sum(1 for e in ev if e.get("type") == "tool_start"),
        "edit_failures": sum(1 for e in tool_ends if e.get("name") in ("edit", "apply_patch") and e.get("status") == "error"),
        "tool_errors": sum(1 for e in tool_ends if e.get("status") == "error"),
        "prompt_tokens": int(usage.get("prompt") or 0),
        "completion_tokens": int(usage.get("completion") or 0),
        "cache_hit_tokens": int(usage.get("hit") or 0),
        "cost": usage.get("cost"),
        "currency": usage.get("currency") or "¥",
        "duration_s": round((done.get("duration_ms") or 0) / 1000, 1),
        "profile": next((e.get("profile") for e in ev if e.get("type") == "session"), None),
    }


def run_once(task: Task, profile: str, idx: int, args, repo: Path) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix=f"ee-eval-{task.id}-{profile}-"))
    root = tmp / "repo"
    rec = {"task": task.id, "kind": task.kind, "profile": profile, "run": idx}
    try:
        make_copy(repo, root)
        apply_inject(root, task.inject)
        cmd = list(args.ee_cmd) + ["exec", "--json", "--no-persist", "--mode", "auto", "--approve", "auto",
                                   "--profile", profile, "-C", str(root)]
        if args.model:
            cmd += ["-m", args.model]
        if args.max_steps:
            cmd += ["--max-steps", str(args.max_steps)]
        cmd.append("-")
        t0 = time.monotonic()
        try:
            r = subprocess.run(cmd, input=task.prompt, capture_output=True, text=True, timeout=args.timeout,
                               encoding="utf-8", errors="replace", cwd=root)
            out, code = r.stdout, r.returncode
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            code = None
        rec.update(summarize_events(out.splitlines()))
        rec["exit_code"] = code
        rec["wall_s"] = round(time.monotonic() - t0, 1)
        if code is None:
            rec["reason"] = "timeout"
        ok, detail = run_check(root, task, args.check_timeout)
        rec["passed"] = ok
        rec["check_tail"] = detail
    except Exception as e:  # noqa: BLE001
        rec.update({"passed": False, "reason": "harness_error", "error": str(e)})
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            rec["workdir"] = str(root)
    return rec


# ------------------------------------------------------------------ 汇总

def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def aggregate(results: list[dict]) -> dict:
    by: dict = {}
    for r in results:
        by.setdefault(r["profile"], []).append(r)
    out = {}
    for prof, rs in by.items():
        prompt = sum(r.get("prompt_tokens", 0) for r in rs)
        hit = sum(r.get("cache_hit_tokens", 0) for r in rs)
        costs = [r.get("cost") for r in rs]
        out[prof] = {
            "runs": len(rs),
            "pass_rate": sum(1 for r in rs if r.get("passed")) / len(rs),
            "avg_steps": _avg([r.get("steps") for r in rs]),
            "avg_tool_calls": _avg([r.get("tool_calls") for r in rs]),
            "avg_prompt": prompt / len(rs),
            "avg_completion": _avg([r.get("completion_tokens") for r in rs]),
            "hit_rate": hit / prompt if prompt else None,
            "avg_cost": _avg(costs) if all(c is not None for c in costs) else None,
            "currency": next((r.get("currency") for r in rs if r.get("currency")), "¥"),
            "avg_duration": _avg([r.get("duration_s") for r in rs]),
            "edit_failures": sum(r.get("edit_failures", 0) for r in rs),
        }
    return out


def _f(v, fmt="{:.1f}"):
    return "—" if v is None else fmt.format(v)


def render(results: list[dict], tasks: list[Task]) -> str:
    agg = aggregate(results)
    lines = ["## 总体", "",
             "| 模式 | 次数 | 完成率 | 平均步数 | 平均工具调用 | 平均输入 token | 平均输出 token | 缓存命中率 | 平均花费 | 平均耗时(s) | edit 失败 |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for prof in sorted(agg):
        a = agg[prof]
        cost = "—" if a["avg_cost"] is None else f"{a['currency']}{a['avg_cost']:.4f}"
        lines.append(f"| {prof} | {a['runs']} | {a['pass_rate']:.0%} | {_f(a['avg_steps'])} | {_f(a['avg_tool_calls'])} | "
                     f"{a['avg_prompt']:.0f} | {_f(a['avg_completion'], '{:.0f}')} | {_f(a['hit_rate'], '{:.0%}')} | "
                     f"{cost} | {_f(a['avg_duration'])} | {a['edit_failures']} |")
    profs = sorted(agg)
    lines += ["", "## 按任务（完成次数 / 总次数，平均步数）", "", "| 任务 | " + " | ".join(profs) + " |",
              "|---|" + "---|" * len(profs)]
    for t in tasks:
        cells = []
        for p in profs:
            rs = [r for r in results if r["task"] == t.id and r["profile"] == p]
            if not rs:
                cells.append("—")
                continue
            cells.append(f"{sum(1 for r in rs if r.get('passed'))}/{len(rs)}，{_f(_avg([r.get('steps') for r in rs]))} 步")
        lines.append(f"| {t.id} {t.title} | " + " | ".join(cells) + " |")
    if "full" in agg and "lean" in agg:
        f, l_ = agg["full"], agg["lean"]
        lines += ["", "## 判定", ""]
        drop = f["pass_rate"] - l_["pass_rate"]
        tok = (1 - l_["avg_prompt"] / f["avg_prompt"]) if f["avg_prompt"] else None
        lines.append(f"- 完成率：full {f['pass_rate']:.0%}，lean {l_['pass_rate']:.0%}（差 {drop:+.0%}；标准：lean 不低于 full 减 5 个百分点）")
        if tok is not None:
            lines.append(f"- 平均输入 token：lean 比 full {'少' if tok >= 0 else '多'} {abs(tok):.0%}")
        lines.append("- 结论：" + ("lean 达标，可以考虑设为默认" if drop <= 0.05 and (tok or 0) > 0
                                 else "lean 未达标，保持可选，看按任务表里哪类任务掉得多"))
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ 自检

def selfcheck(tasks: list[Task], repo: Path, timeout: float) -> int:
    bad = 0
    for t in tasks:
        tmp = Path(tempfile.mkdtemp(prefix=f"ee-eval-check-{t.id}-"))
        try:
            root = tmp / "a"
            make_copy(repo, root)
            apply_inject(root, t.inject)
            ok, detail = run_check(root, t, timeout)
            msg = "OK  " if not ok else "BAD "
            note = "未做任务时验收不通过" if not ok else "未做任务验收就通过了：任务无效"
            if not ok and t.revert_passes:
                root2 = tmp / "b"
                make_copy(repo, root2)
                ok2, detail2 = run_check(root2, t, timeout)
                if not ok2:
                    msg, note = "BAD ", "不注入 bug 时验收也不通过：\n" + detail2
                else:
                    note += "；不注入时通过"
            bad += msg.startswith("BAD")
            print(f"{msg}{t.id}：{note}")
        except Exception as e:  # noqa: BLE001
            bad += 1
            print(f"BAD {t.id}：{e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return 1 if bad else 0


# ------------------------------------------------------------------ 入口

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="ee 完整模式 / 精简模式真实任务对比")
    p.add_argument("--repo", default=str(HERE.parents[1]), help="被改的仓库（默认本仓库根目录，只用已提交内容）")
    p.add_argument("--tasks-dir", default=str(HERE / "eval_tasks"))
    p.add_argument("--tasks", help="只跑这些任务（逗号分隔的编号或 id 前缀，如 01,04）")
    p.add_argument("--profiles", default="full,lean")
    p.add_argument("--runs", type=int, default=3, help="每个任务每种模式跑几次")
    p.add_argument("--model", help="模型名（默认用 ee 配置里的默认模型）")
    p.add_argument("--max-steps", type=int, default=40)
    p.add_argument("--timeout", type=float, default=900, help="单次 ee exec 超时秒数")
    p.add_argument("--check-timeout", type=float, default=300)
    p.add_argument("--ee", dest="ee_cmd", default=None,
                   help="调用 ee 的命令（默认当前 Python 的 -m ee；可写成 \"ee\" 或 exe 路径）")
    p.add_argument("--key-env", default="DEEPSEEK_API_KEY", help="检查哪个环境变量里有 key（不打印）")
    p.add_argument("--out", help="结果目录：results.jsonl + summary.md")
    p.add_argument("--keep", action="store_true", help="保留每次运行的仓库副本（排查用）")
    p.add_argument("--selfcheck", action="store_true", help="不调模型，只检查任务文件是否有效")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    args.ee_cmd = (args.ee_cmd.split() if args.ee_cmd and " " in args.ee_cmd and not Path(args.ee_cmd).exists()
                   else [args.ee_cmd] if args.ee_cmd else [sys.executable, "-m", "ee"])
    repo = Path(args.repo).resolve()
    tasks = load_tasks(Path(args.tasks_dir), [x.strip() for x in args.tasks.split(",")] if args.tasks else None)
    if not tasks:
        print("没有任务", file=sys.stderr)
        return 2
    if args.selfcheck:
        return selfcheck(tasks, repo, args.check_timeout)
    if args.key_env and not os.environ.get(args.key_env):
        print(f"环境变量 {args.key_env} 里没有 API key（也可以在 ee 设置里配好后用 --key-env \"\" 跳过检查）。",
              file=sys.stderr)
        return 2
    profiles = [p.strip() for p in args.profiles.split(",") if p.strip()]
    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    total = len(tasks) * len(profiles) * args.runs
    n = 0
    for i in range(args.runs):                 # 交错跑：两种模式受同一时段的服务端波动影响
        for t in tasks:
            for prof in profiles:
                n += 1
                print(f"[{n}/{total}] {t.id} · {prof} · 第 {i + 1} 次 …", file=sys.stderr, flush=True)
                rec = run_once(t, prof, i + 1, args, repo)
                results.append(rec)
                print(f"    {'完成' if rec.get('passed') else '未完成'} · {rec.get('reason')} · {rec.get('steps', 0)} 步 · "
                      f"{rec.get('duration_s', 0)}s", file=sys.stderr, flush=True)
                if out_dir:
                    with open(out_dir / "results.jsonl", "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    text = render(results, tasks)
    print(text)
    if out_dir:
        (out_dir / "summary.md").write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
