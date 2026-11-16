"""全力以赴基准：普通模式 vs 火力全开（2026-11 第三批，真实模型）。

**默认不花钱**：不带 `--run --yes` 时只做 `--dry-run`（校验夹具、打印将执行的命令与模型）。

夹具在 `research/bench/allout/<slug>/`：
- `task.md`   给两种模式的任务说明（普通模式只收到它）
- `seed/`     起始代码（复制到临时项目并 git init/commit）
- `public/`   公开验收（模型可见；**只有火力全开**把它作为判据）
- `hidden/`   隐藏验收（运行结束后才执行，放在独立目录、用 PYTHONPATH 指回项目，模型碰不到）

判分口径（与设计评审一致）：
- 独立通过率 = 隐藏用例通过的样本 / 样本数；
- 复验一致性 = 声明的 met 与隐藏通过是否一致；
- 每解决任务成本 = 全部样本花费 / 隐藏通过的样本数（没有成功样本时只报总支出）；
- 超时与金额缺失单独标记，不混进成功率。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "ee") not in sys.path:
    sys.path.insert(0, str(ROOT / "ee"))     # 直接 `python scripts/allout_bench.py` 时也能 import ee.*
BENCH = ROOT / "research" / "bench" / "allout"
MODES = ("plain", "allout")


@dataclass(frozen=True)
class Task:
    slug: str
    path: Path
    prompt: str
    criterion: str = ""          # 夹具自定义判据（criterion.txt）；空 = 用 public/ 的 pytest

    @property
    def seed(self) -> Path:
        return self.path / "seed"

    @property
    def public(self) -> Path:
        return self.path / "public"

    @property
    def hidden(self) -> Path:
        return self.path / "hidden"


def load_tasks(root: Path = BENCH) -> list[Task]:
    out: list[Task] = []
    for path in sorted(p for p in root.iterdir() if p.is_dir()):
        if path.name.startswith((".", "__")):     # __pycache__ / .venv 之类的产物，不是夹具
            continue
        prompt = (path / "task.md")
        if not prompt.is_file():
            # 不是夹具目录就跳过（pytest 的 tmp_path 里可能有 user-home 之类的别的东西）；
            # 但"有 seed/ 却没有 task.md"是真配错了，要报出来。
            if (path / "seed").is_dir():
                raise SystemExit(f"夹具缺 task.md：{path}")
            continue
        crit = path / "criterion.txt"
        # `public/` 只在"用公开测试当判据"时才必需；有 criterion.txt 的夹具可以没有它
        # （空目录 git 不跟踪，CI checkout 后本来就不存在）。
        need = ("seed", "hidden") if crit.is_file() else ("seed", "public", "hidden")
        for sub in need:
            if not (path / sub).is_dir():
                raise SystemExit(f"夹具缺 {sub}/：{path}")
        out.append(Task(path.name, path, prompt.read_text(encoding="utf-8"),
                        crit.read_text(encoding="utf-8").strip() if crit.is_file() else ""))
    if not out:
        raise SystemExit(f"没有夹具：{root}")
    return out


def _pycmd(*args: str) -> str:
    """按当前 shell 语法拼一条 python 命令（PowerShell 需要调用运算符）。"""
    from ee.tools.shellenv import resolve_shell

    body = " ".join(f'"{a}"' if " " in a else a for a in args)
    return f"& {body}" if resolve_shell("auto").syntax == "powershell" else body


def public_command(task: Task | None = None, counter: str = "<counter>") -> str:
    """夹具自定义判据优先。

    `{python}` 换成当前解释器（不依赖 PATH）；`{counter}` 换成项目外的计数器路径
    （主目录与候选 worktree 共享同一个，冒烟夹具用它让前几次判据失败）。
    """
    if task is not None and task.criterion:
        return (task.criterion.replace("{python}", f'"{sys.executable}"')
                .replace("{counter}", counter))
    return _pycmd(sys.executable, "-m", "pytest", "-q", "public/test_public.py")


def prepare(task: Task, dest: Path) -> Path:
    """复制 seed → 临时项目，git init/commit，公开测试放进 public/。"""
    project = dest / task.slug
    shutil.copytree(task.seed, project)
    if task.public.is_dir():
        shutil.copytree(task.public, project / "public")
    for cmd in (["init", "-q", "-b", "main"],
                ["config", "user.email", "bench@example.com"],
                ["config", "user.name", "bench"],
                ["add", "-A"], ["commit", "-qm", "seed"]):
        subprocess.run(["git", *cmd], cwd=project, check=False, capture_output=True)
    return project


def public_sources(project: Path) -> list[Path]:
    """公开测试的**源文件**：只算 .py，排除 `__pycache__` 之类的运行产物。

    跑过 pytest 就会生成 pyc；把它算进"公开测试被改动"会让每个样本都误报篡改。
    """
    return sorted(p for p in (project / "public").glob("**/*.py") if "__pycache__" not in p.parts)


def fingerprint(paths: list[Path]) -> dict[str, str]:
    import hashlib

    out: dict[str, str] = {}
    for p in paths:
        try:
            out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        except OSError:
            out[str(p)] = "missing"
    return out


def score(task: Task, project: Path, timeout: float = 180.0) -> dict:
    """在独立目录里跑隐藏用例：模型看不到、也改不到这套验收。"""
    from ee.core.goal import parse_tests

    with tempfile.TemporaryDirectory(prefix="ee-bench-score-") as tmp:
        hidden = Path(tmp) / "hidden"
        shutil.copytree(task.hidden, hidden)
        env = {**__import__("os").environ, "PYTHONPATH": str(project)}
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", str(hidden)],
                              cwd=tmp, env=env, capture_output=True, text=True, check=False,
                              timeout=timeout, encoding="utf-8", errors="replace")
        text = (proc.stdout or "") + (proc.stderr or "")
        return {"passed": proc.returncode == 0, "tests": parse_tests(text),
                "tail": text[-800:]}


async def run_sample(task: Task, mode: str, args) -> dict:
    """跑一个样本。真实模型调用只在这里发生（调用方必须显式 --run --yes）。"""
    from ee.server.threads import Thread

    # 每个样本一个独立目录：同一个 workdir 下两次 prepare 会撞同名项目。
    sample_dir = Path(tempfile.mkdtemp(prefix=f"{task.slug}-{mode}-", dir=args.workdir))
    project = prepare(task, sample_dir)
    before = fingerprint(public_sources(project))
    home = Path(tempfile.mkdtemp(prefix="ee-bench-home-"))

    # 冒烟夹具（90_two_winners）靠计数器让前几次判据失败：计数器放在项目外的临时文件，
    # 直接写进判据命令——主目录与候选 worktree 共享同一个，不会因为 worktree 里没有
    # 那个未跟踪文件而重新从 0 开始。
    counter = str(Path(tempfile.gettempdir()) / f"ee-bench-{task.slug}-{mode}-{uuid.uuid4().hex[:8]}.runs")

    def configure(cfg) -> None:
        cfg.data.setdefault("budget", {})["session"] = args.budget   # argparse 已保证是 float
        if args.max_steps:
            # 冒烟时把每回合步数压小：判据被人为失败时，模型会在修复轮里一直尝试，
            # 不限步就会把墙钟全吃掉、永远轮不到分叉。
            cfg.data["max_steps"] = int(args.max_steps)
            cfg.data["max_steps_segments"] = 1

    # 记录 allout 的计划/阶段事件：报告里要能说明"多路到底有没有真的启动"，
    # 而不是只看最终 met（首轮/修复就过时不会分叉）。
    phases: list[dict] = []

    def notify(method: str, payload: dict) -> None:
        if method.startswith("allout"):
            phases.append({"m": method, **{k: payload.get(k) for k in
                                          ("phase", "started", "candidates", "requested", "winner", "state", "met")
                                          if k in payload}})

    th = Thread(f"bench-{task.slug}-{mode}", home, notify,
                {"cwd": str(project), "mode": "auto", "model": args.model or ""},
                persist=False, configure=configure, user_home=Path.home())
    th.ensure_session()
    goal = ({"command": public_command(task, counter), "max_rounds": 3}
            if mode == "allout" else None)
    allout = {"enabled": True, "candidates": args.candidates} if mode == "allout" else None
    started = time.monotonic()
    timed_out = False
    error = ""
    try:
        th.start_turn(task.prompt, goal=goal, allout=allout)
        if th.task is None:                      # 排队路径不会起 task；基准里不该出现
            raise RuntimeError("回合没有启动（turn 被排队了？）")
        await asyncio.wait_for(asyncio.shield(th.task), float(args.timeout))
    except TimeoutError:
        timed_out = True
        th.interrupt()
    except Exception as e:  # noqa: BLE001  单样本失败不拖垮整批
        error = f"{type(e).__name__}: {e}"
    duration = time.monotonic() - started
    turn_end = next((it for it in reversed(th.items) if it.get("type") == "turn_end"), {})
    usage = turn_end.get("usage") or {}
    after = fingerprint(public_sources(project))
    record = {
        "task": task.slug, "mode": mode,
        "reason": turn_end.get("reason", ""),
        "delivery": (turn_end.get("delivery") or {}).get("status", ""),
        "duration_s": round(duration, 1),
        "cost": usage.get("cost"), "cost_complete": bool(usage.get("cost_complete", False)),
        "timed_out": timed_out, "error": error,
        "public_changed": before != after,
        "public_changed_files": sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k)),
        "phases": phases,
        "fanout_started": next((p.get("started") for p in phases
                                if p.get("m") == "allout_progress" and p.get("phase") == "fanout"), 0),
    }
    try:
        record.update(await asyncio.to_thread(score, task, project))
    except Exception as e:  # noqa: BLE001  单个样本判分失败（超时/夹具异常）不能拖垮整批
        record.update({"passed": False, "tests": 0, "tail": f"{type(e).__name__}: {e}"})
    # pi-lens-ignore: unchecked-throwing-call-python -- ignore_errors=True 已吞掉 OSError
    shutil.rmtree(home, ignore_errors=True)
    return record


def aggregate(records: list[dict]) -> dict:
    """按模式汇总。超时/缺金额单列，不混进成功率。"""
    out: dict[str, dict] = {}
    for mode in MODES:
        rows = [r for r in records if r["mode"] == mode]
        if not rows:
            continue
        solved = sum(1 for r in rows if r["passed"])
        met = [r for r in rows if r["reason"] == "met"]
        inconsistent = sum(1 for r in met if not r["passed"])
        costs = [r["cost"] for r in rows if isinstance(r["cost"], (int, float))]
        out[mode] = {
            "samples": len(rows), "solved": solved,
            "pass_rate": round(solved / len(rows), 3),
            "reported_met": len(met),
            "consistency_error_rate": None if not met else round(inconsistent / len(met), 3),
            "unverified": sum(1 for r in rows if r["delivery"] == "unverified"),
            "timeouts": sum(1 for r in rows if r["timed_out"]),
            "cost_total": round(sum(costs), 6) if costs else None,
            "cost_complete": all(r["cost_complete"] for r in rows),
            "cost_per_solved": round(sum(costs) / solved, 6) if solved and costs else None,
            "duration_s": round(sum(r["duration_s"] for r in rows), 1),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="全力以赴基准（默认 dry-run，不调用模型）")
    ap.add_argument("--tasks", nargs="*", default=None, help="只跑这些夹具（默认全部）")
    ap.add_argument("--modes", default="plain,allout")
    ap.add_argument("--model", default="", help="模型别名（空=配置默认）")
    ap.add_argument("--budget", type=float, default=0.5, help="每个样本的会话费用上限")
    ap.add_argument("--candidates", type=int, default=1,
                    help="火力全开允许的候选上限（实际路数仍由本机资源推导）")
    ap.add_argument("--max-steps", type=int, default=0,
                    help="压缩每回合步数（0=用配置默认；冒烟用，避免修复轮吃光墙钟）")
    ap.add_argument("--timeout", type=float, default=600, help="每个样本的墙钟上限（秒）")
    ap.add_argument("--workdir", default=None, help="临时项目根（默认系统临时目录）")
    ap.add_argument("--out", default="", help="JSON 结果写到这个路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划、不调用模型（默认行为）")
    ap.add_argument("--run", action="store_true", help="真的调用模型（会花钱）")
    ap.add_argument("--yes", action="store_true", help="与 --run 一起用，确认花费")
    args = ap.parse_args(argv)

    tasks = load_tasks()
    if args.tasks:
        tasks = [t for t in tasks if t.slug in set(args.tasks)]
        if not tasks:
            raise SystemExit("--tasks 没有匹配到夹具")
    modes = [m for m in args.modes.split(",") if m]
    if any(m not in MODES for m in modes):
        raise SystemExit(f"未知模式：{args.modes}（可选 {MODES}）")

    plan = [{"task": t.slug, "mode": m, "criterion": public_command(t) if m == "allout" else "",
             "candidates": args.candidates if m == "allout" else 0,
             "hidden": str(t.hidden)} for t in tasks for m in modes]
    if not args.run:
        print(json.dumps({"dry_run": True, "model": args.model or "(配置默认)",
                          "samples": len(plan), "plan": plan}, ensure_ascii=False, indent=2))
        return 0
    if not args.yes:
        raise SystemExit("真的调用模型会花钱：加 --run --yes 再执行")

    workdir = Path(args.workdir or tempfile.mkdtemp(prefix="ee-bench-"))
    workdir.mkdir(parents=True, exist_ok=True)
    args.workdir = str(workdir)          # run_sample 只读这个字段：默认临时目录也要回填
    records = []
    for i, (t, m) in enumerate(((t, m) for t in tasks for m in modes), 1):
        print(f"[{i}/{len(plan)}] {t.slug} · {m}", flush=True)
        records.append(asyncio.run(run_sample(t, m, args)))
    summary = aggregate(records)
    result = {"dry_run": False, "model": args.model or "(配置默认)",
              "records": records, "summary": summary}
    if args.out:
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
