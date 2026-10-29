"""完整模式（full）与精简模式（lean）的真实任务对比。

用 `ee exec --json` 在仓库的干净副本里跑一组任务（ee/scripts/eval_tasks/*.md），每个任务每种模式跑 N 次，
跑完用任务自带的验收命令判定是否完成，汇总完成率、步数、token、命中率、花费、耗时、edit 失败次数。

用法（在 ee 仓库根目录）：
  set DEEPSEEK_API_KEY=...            （PowerShell：$env:DEEPSEEK_API_KEY="..."；key 只从环境变量读）
  python ee/scripts/eval_profiles.py --runs 3
  python ee/scripts/eval_profiles.py --runs 1 --tasks 01,04 --model deepseek-pro --out eval-out
  python ee/scripts/eval_profiles.py --selfcheck      不调模型：检查每个任务的注入/验收是否有效
  python ee/scripts/eval_profiles.py --preset a2 --runs 3   改前/改后对比：git worktree 检出基线提交，
                                      基线和当前各跑一遍（A2 = 工具描述压缩，A3 = edit 多处/容错）
  python ee/scripts/eval_profiles.py --preset a3 --base <提交>   自己指定基线（cmd.exe 里别用 ^，写 ~1）

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
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECK_FILE = "ee/tests/_eval_check.py"
WINDOWS = os.name == "nt"
# 子进程一律按 UTF-8 输出：中文 Windows 默认 GBK，事件流里的 ¥ 等字符编不出来会让子进程崩掉
CHILD_ENV = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}

# 改前/改后对比的基线（~1 = 第一父提交，等同 ^；cmd.exe 里 ^ 是转义符，所以不用 ^）
PRESETS = {
    "a2": {"base": "a6e2b22~1", "title": "A2 工具描述压缩与规则拼装（合并 feat/pi-lean 之前）"},
    "a3": {"base": "4cc28dc~1", "title": "A3 edit 多处编辑/参数容错/模糊匹配（合并 feat/pi-edit 之前）"},
}


def safe_console() -> None:
    """GBK 控制台打不出 ¥、✓ 之类的字符时用 ? 代替，不让 print 抛 UnicodeEncodeError 中断整轮评测。"""
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _kill_tree(p: subprocess.Popen) -> None:
    if WINDOWS:
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True,
                       stdin=subprocess.DEVNULL)
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        p.kill()
    except OSError:
        pass


def run_proc(cmd, *, cwd, env, timeout: float, input: str | None = None, shell: bool = False) -> tuple[str, str, int | None]:
    """跑子进程并收集输出；超时结束整棵进程树（否则 Windows 上孙进程拿着管道，communicate 会一直等）。
    返回 (stdout, stderr, 退出码)；超时时退出码为 None，输出是超时前拿到的部分。"""
    kw: dict = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {"start_new_session": True}
    p = subprocess.Popen(cmd, shell=shell, cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace",
                         stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    try:
        out, err = p.communicate(input, timeout=timeout)
        return out or "", err or "", p.returncode
    except subprocess.TimeoutExpired as e:
        partial = e.stdout
        _kill_tree(p)
        try:
            out, err = p.communicate(timeout=10)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            for f in (p.stdout, p.stderr):
                try:
                    f.close()
                except OSError:
                    pass
            out, err = "", ""
        if not out and partial:
            out = partial.decode("utf-8", "replace") if isinstance(partial, bytes) else partial
        return out or "", err or "", None


def _rmtree(path: Path) -> None:
    """Windows 上 git 对象文件是只读的，直接 rmtree 删不掉：去掉只读再删。"""
    def onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            func(p)
        except OSError:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=onerror)
    else:
        shutil.rmtree(path, onerror=onerror)
    if Path(path).exists():
        shutil.rmtree(path, ignore_errors=True)


_PY_NAMES = {"python", "python3", "python.exe", "python3.exe", "py"}


def check_argv(cmd: str) -> list[str] | None:
    """验收命令拆成参数列表、不经 shell 执行（cmd.exe 对带空格的解释器路径和嵌套引号处理不可靠）；
    开头的 python 换成当前解释器。含 &&、|、> 之类 shell 运算符时返回 None，交给 shell。"""
    try:
        lx = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lx.whitespace_split = True
        toks = list(lx)
    except ValueError:
        return None
    if not toks or any(t and all(c in "();<>|&" for c in t) for t in toks):
        return None
    if toks[0].lower() in _PY_NAMES:
        toks[0] = sys.executable
    return toks


def resolve_ee_cmd(spec: str | None, windows: bool = WINDOWS) -> list[str]:
    """--ee 的写法：省略 = 优先用旁路可执行文件，否则退回当前 Python 的 -m ee；
    .py = 用当前 Python 跑；.cmd/.bat 要经 cmd.exe；带空格又不是现成文件 = 整条命令按空格拆。

    为什么默认不直接用 `python -m ee`：评测调用时会把子进程 cwd 切到临时副本
    （`cmd += ["-C", str(root)]`），而 `-m ee` 要求 cwd 能找到 ee 包、且它是**常规包**。
    cwd 一旦切到别处，`ee` 会被解析成命名空间包（`__file__` 是 None、没有 `__version__`），
    子进程启动即 `ImportError`，评测结果是 `no_output · 0 步`——看起来像模型没输出，
    实际是被测程序根本没起来。所以默认先找与当前解释器同目录的 `ee(.exe)`：
    可执行文件不依赖 cwd。
    """
    if not spec:
        exe_dir = Path(sys.executable).parent
        for name in ("ee.exe", "ee.cmd", "ee.bat", "ee") if windows else ("ee",):
            cand = exe_dir / name
            if cand.is_file():
                if windows and name.lower().endswith((".cmd", ".bat")):
                    return ["cmd.exe", "/d", "/s", "/c", str(cand)]
                return [str(cand)]
        # 最后手段：让 `-m ee` 不依赖 cwd。HERE = <repo>/ee/scripts，包目录是 <repo>/ee/ee。
        # 裸 `-m ee` 会让评测再次退化成 `no_output · 0 步`——这正是 1bc0e12 想修、
        # 但只修了「旁边有 ee.exe」这条正常路径的那个缺陷。
        pkg = HERE.parent / "ee"
        if (pkg / "__init__.py").is_file():
            return [sys.executable, "-c",
                    "import sys; sys.path.insert(0, sys.argv[1]);"
                    " from ee.cli import main; sys.exit(main(sys.argv[2:]))",
                    str(pkg.parent)]
        return [sys.executable, "-m", "ee"]
    p = Path(spec)
    if spec.lower().endswith(".py") and p.is_file():
        return [sys.executable, str(p)]
    found = str(p) if p.is_file() else (shutil.which(spec) or "")
    if windows and found.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/d", "/s", "/c", found]
    if " " in spec and not p.exists():
        return shlex.split(spec, posix=not windows)
    return [spec]


@dataclass
class Engine:
    """被测的 ee：命令 + 额外环境变量（PYTHONPATH 指向某个版本的源码）。"""
    variant: str                 # 非 preset 时为空；preset 时 base / cur
    cmd: list[str]
    env: dict = field(default_factory=dict)
    profile_flag: bool = True    # 这个版本的 ee exec 认不认 --profile（A2 之前没有）
    label: str = ""


def engine_from_src(variant: str, src: Path, label: str) -> Engine:
    pp = str(src / "ee")
    old = os.environ.get("PYTHONPATH")
    env = {"PYTHONPATH": pp + (os.pathsep + old if old else "")}
    eng = Engine(variant, [sys.executable, "-m", "ee"], env, True, label)
    out, err, code = run_proc(eng.cmd + ["exec", "--help"], cwd=str(src), env={**os.environ, **CHILD_ENV, **env},
                              timeout=60)
    if code != 0:
        raise RuntimeError(f"{label}：ee exec --help 失败：{(err or out).strip()[-300:]}")
    eng.profile_flag = "--profile" in out
    return eng


def _git(repo: Path, *a: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL)


def add_worktree(repo: Path, ref: str, dest: Path) -> str:
    """把基线提交检出到独立 worktree（不动当前工作区），返回短 hash。"""
    r = _git(repo, "rev-parse", "--short", "--verify", ref + "^{commit}")
    if r.returncode != 0:
        raise RuntimeError(f"找不到基线提交 {ref}：{r.stderr.strip()}")
    sha = r.stdout.strip()
    r = _git(repo, "worktree", "add", "--detach", "--force", str(dest), sha)
    if r.returncode != 0:
        raise RuntimeError(f"git worktree add 失败：{r.stderr.strip()}")
    return sha


def remove_worktree(repo: Path, dest: Path) -> None:
    _git(repo, "worktree", "remove", "--force", str(dest))
    if dest.exists():
        _rmtree(dest)
    _git(repo, "worktree", "prune")


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
        cm = re.search(r"```python\n(.*?)(?:```|\Z)", rest, re.S)
        hidden = cm.group(1) if cm else ""
        if not hidden.strip():
            raise ValueError(f"{path.name}：「## 验收测试」下没有 python 代码块")
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
    env = {**os.environ, **CHILD_ENV, "PYTHONPATH": str(root / "ee")}
    argv = check_argv(task.check)
    if argv is not None:
        out, err, code = run_proc(argv, cwd=str(cwd), env=env, timeout=timeout)
    else:
        cmd = task.check.replace("python ", f'"{sys.executable}" ', 1) if task.check.startswith("python ") else task.check
        out, err, code = run_proc(cmd, cwd=str(cwd), env=env, timeout=timeout, shell=True)
    if code is None:
        return False, "验收超时"
    tail = (out + err).strip().splitlines()[-5:]
    return code == 0, "\n".join(tail)


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
    # 把模型时间与工具时间分开记：只有 duration_s（整回合墙钟）时，
    # 一步跑满 600 秒的命令会让「平均每步耗时」看起来像模型慢，实测就踩过这个坑。
    # 同时按 call_id 把 tool_start 的参数接回来——慢命令到底在跑什么，
    # 靠 tool_end.duration_ms 是看不出来的（命令原文曾经因此彻底丢失）。
    starts = {e.get("call_id"): e for e in ev if e.get("type") == "tool_start"}

    def _what(e: dict) -> str:
        """这条工具调用实际在做什么：命令 / 路径 / 模式，取一个够定位的摘要。"""
        args = (starts.get(e.get("call_id")) or {}).get("args") or {}
        for k in ("command", "path", "pattern", "query", "url", "id"):
            v = args.get(k)
            if v:
                s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                return s if len(s) <= 160 else s[:160] + "…"
        return e.get("summary") or ""

    tools = [{"name": e.get("name"), "duration_ms": e.get("duration_ms"),
              "timed_out": bool(e.get("timed_out")), "ok": bool(e.get("ok")),
              "what": _what(e)}
             for e in tool_ends]
    timed = [t for t in tools if isinstance(t["duration_ms"], (int, float))]
    tool_s = round(sum(t["duration_ms"] for t in timed) / 1000, 1) if timed else 0.0
    slowest = max(timed, key=lambda t: t["duration_ms"], default=None)
    return {
        "reason": done.get("reason") or ("no_output" if not ev else "crashed"),
        "steps": sum(1 for e in ev if e.get("type") == "usage"),
        "tool_calls": sum(1 for e in ev if e.get("type") == "tool_start"),
        "edit_failures": sum(1 for e in tool_ends if e.get("name") in ("edit", "apply_patch") and e.get("status") == "error"),
        "tool_errors": sum(1 for e in tool_ends if e.get("status") == "error"),
        "tool_s": tool_s,                                    # 工具执行总耗时（来自 tool_end.duration_ms）
        "tool_timed_out": sum(1 for t in tools if t["timed_out"]),
        "slowest_tool": ({"name": slowest["name"], "duration_ms": slowest["duration_ms"],
                          "what": slowest["what"]} if slowest else None),
        # 每条工具调用的 (名称, 秒, 是否超时, 在做什么)，按耗时降序；排查慢在哪一步用
        "tools": [{"name": t["name"], "s": round((t["duration_ms"] or 0) / 1000, 1),
                   "timed_out": t["timed_out"], "what": t["what"]}
                  for t in sorted(timed, key=lambda x: -x["duration_ms"])],
        "prompt_tokens": int(usage.get("prompt") or 0),
        "completion_tokens": int(usage.get("completion") or 0),
        "cache_hit_tokens": int(usage.get("hit") or 0),
        "cost": usage.get("cost"),
        "costs": usage.get("costs") or {},
        "cost_complete": usage.get("cost_complete"),
        "currency": usage.get("currency") or "¥",
        "duration_s": round((done.get("duration_ms") or 0) / 1000, 1),
        "profile": next((e.get("profile") for e in ev if e.get("type") == "session"), None),
    }


def run_once(task: Task, profile: str, idx: int, args, repo: Path, engine: Engine | None = None) -> dict:
    engine = engine or Engine("", list(args.ee_cmd))
    tmp = Path(tempfile.mkdtemp(prefix=f"ee-eval-{task.id}-{engine.variant or profile}-"))
    root = tmp / "repo"
    group = (engine.variant if len(args.profile_list) == 1 else f"{engine.variant}/{profile}") if engine.variant \
        else profile
    rec = {"task": task.id, "kind": task.kind, "profile": profile, "run": idx, "group": group}
    if engine.variant:
        rec["variant"] = engine.variant
    try:
        make_copy(repo, root)
        apply_inject(root, task.inject)
        cmd = list(engine.cmd) + ["exec", "--json", "--no-persist", "--mode", "auto", "--approve", "auto"]
        if engine.profile_flag:
            cmd += ["--profile", profile]
        elif profile != "full":
            raise RuntimeError(f"{engine.label or engine.variant} 的 ee exec 不支持 --profile，只能跑 full")
        cmd += ["-C", str(root)]
        if args.model:
            cmd += ["-m", args.model]
        if args.max_steps:
            cmd += ["--max-steps", str(args.max_steps)]
        # 单次命令硬上限：不给的话，模型传 timeout=600 就能合法占满 10 分钟，
        # 一条跑不完的命令把整轮耗时全盖住（实测三次 600.3 秒空档就是这么来的）。
        if args.max_tool_timeout:
            cmd += ["--max-tool-timeout", str(args.max_tool_timeout)]
        cmd.append("-")
        t0 = time.monotonic()
        # 子进程按本机代码页输出（中文 Windows 是 GBK），而事件流里有 ¥ 之类的字符
        # （usage 里的 currency），GBK 编不出来会让 fake_ee/ee exec 直接抛
        # UnicodeEncodeError 崩掉，本行只拿到半截事件流（表现为 crashed）。
        # 这里显式给子进程指定 UTF-8，和读取端 encoding="utf-8" 对齐。
        child_env = {**os.environ, **CHILD_ENV, **engine.env}
        out, _err, code = run_proc(cmd, input=task.prompt, timeout=args.timeout, cwd=str(root), env=child_env)
        rec.update(summarize_events(out.splitlines()))
        rec["profile"] = profile          # 旧版 ee 的 session 事件可能没有 profile 字段
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
            _rmtree(tmp)
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
        by.setdefault(r.get("group") or r["profile"], []).append(r)
    out = {}
    for prof, rs in by.items():
        prompt = sum(r.get("prompt_tokens", 0) for r in rs)
        hit = sum(r.get("cache_hit_tokens", 0) for r in rs)
        # 费用按币种分桶，并单独数“有几条没费用”。
        # 不能用 `all(c is not None)` 把关：**只要一条缺费用，整组金额就变 None**，
        # 评测越不稳定越看不见钱（最需要看钱的时候恰好看不到）。
        costs_by_cur: dict[str, list[float]] = {}
        missing = 0
        known = 0
        for r in rs:
            c = r.get("cost")
            if c is None:
                missing += 1
                continue
            known += 1
            cur = r.get("currency") or "¥"
            costs_by_cur.setdefault(cur, []).append(float(c))
        costs = {cur: {"known_total": round(sum(vals), 10), "known_runs": len(vals),
                       "avg": round(sum(vals) / len(vals), 10)}
                 for cur, vals in costs_by_cur.items()}
        # 兼容字段：只有一个币种有值且没有缺失时，才给单值 avg_cost。
        single_avg = None
        single_cur = None
        if len(costs_by_cur) == 1 and missing == 0:
            cur = next(iter(costs_by_cur))
            single_avg = round(sum(costs_by_cur[cur]) / len(costs_by_cur[cur]), 10)
            single_cur = cur
        out[prof] = {
            "runs": len(rs),
            "pass_rate": sum(1 for r in rs if r.get("passed")) / len(rs),
            "avg_steps": _avg([r.get("steps") for r in rs]),
            "avg_tool_calls": _avg([r.get("tool_calls") for r in rs]),
            "avg_prompt": prompt / len(rs),
            "avg_completion": _avg([r.get("completion_tokens") for r in rs]),
            "hit_rate": hit / prompt if prompt else None,
            "avg_cost": single_avg,
            "costs": costs,
            "cost_known_runs": known,
            "cost_missing_runs": missing,
            "currency": single_cur or next((r.get("currency") for r in rs if r.get("currency")), "¥"),
            "avg_duration": _avg([r.get("duration_s") for r in rs]),
            # 工具耗时单独统计：avg_duration 是整回合墙钟，两者相减才是模型+启动开销
            "avg_tool_s": _avg([r.get("tool_s") for r in rs]),
            "tool_timed_out": sum(r.get("tool_timed_out", 0) for r in rs),
            "slowest_tool": max((r.get("slowest_tool") for r in rs if r.get("slowest_tool")),
                                key=lambda t: t["duration_ms"], default=None),
            "edit_failures": sum(r.get("edit_failures", 0) for r in rs),
        }
    return out


def _f(v, fmt="{:.1f}"):
    return "—" if v is None else fmt.format(v)


def render(results: list[dict], tasks: list[Task], preset: dict | None = None) -> str:
    agg = aggregate(results)
    lines = []
    if preset:
        lines += [f"# 改前/改后对比：{preset['title']}", "",
                  f"- base = 基线 {preset['base']}（{preset.get('sha', '')}），cur = 当前工作区代码", ""]
    lines += ["## 总体", "",
             "| 模式 | 次数 | 完成率 | 平均步数 | 平均工具调用 | 平均输入 token | 平均输出 token | 缓存命中率 | 平均花费 | 平均耗时(s) | 其中工具(s) | 超时工具 | 最慢单次 | edit 失败 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for prof in sorted(agg):
        a = agg[prof]
        cost = "—" if a["avg_cost"] is None else f"{a['currency']}{a['avg_cost']:.4f}"
        slow = a.get("slowest_tool")
        slow_s = (f"{slow['name']} {slow['duration_ms'] / 1000:.1f}s"
                  + (f" `{slow.get('what', '')[:60]}`" if slow.get("what") else "")) if slow else "—"
        lines.append(f"| {prof} | {a['runs']} | {a['pass_rate']:.0%} | {_f(a['avg_steps'])} | {_f(a['avg_tool_calls'])} | "
                     f"{a['avg_prompt']:.0f} | {_f(a['avg_completion'], '{:.0f}')} | {_f(a['hit_rate'], '{:.0%}')} | "
                     f"{cost} | {_f(a['avg_duration'])} | {_f(a.get('avg_tool_s'))} | {a.get('tool_timed_out', 0)} | "
                     f"{slow_s} | {a['edit_failures']} |")
    profs = sorted(agg)
    lines += ["", "## 按任务（完成次数 / 总次数，平均步数）", "", "| 任务 | " + " | ".join(profs) + " |",
              "|---|" + "---|" * len(profs)]
    for t in tasks:
        cells = []
        for p in profs:
            rs = [r for r in results if r["task"] == t.id and (r.get("group") or r["profile"]) == p]
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
    if preset and "base" in agg and "cur" in agg:
        b, c = agg["base"], agg["cur"]
        lines += ["", "## 判定（cur 对比 base）", ""]
        lines.append(f"- 完成率：base {b['pass_rate']:.0%}，cur {c['pass_rate']:.0%}（差 {c['pass_rate'] - b['pass_rate']:+.0%}）")
        if b["avg_prompt"]:
            d = 1 - c["avg_prompt"] / b["avg_prompt"]
            lines.append(f"- 平均输入 token：cur 比 base {'少' if d >= 0 else '多'} {abs(d):.0%}")
        lines.append(f"- edit 失败：base {b['edit_failures']} 次，cur {c['edit_failures']} 次")
        if b["avg_steps"] is not None and c["avg_steps"] is not None:
            lines.append(f"- 平均步数：base {b['avg_steps']:.1f}，cur {c['avg_steps']:.1f}")
        # 工具耗时单列：墙钟变慢时先看落在这边还是模型那边
        if b.get("avg_tool_s") is not None and c.get("avg_tool_s") is not None:
            lines.append(f"- 平均工具耗时：base {b['avg_tool_s']:.1f}s，cur {c['avg_tool_s']:.1f}s")
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
            _rmtree(tmp)
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
    p.add_argument("--max-tool-timeout", type=int, default=180,
                   help="单次命令的硬上限秒数（默认 180）。设 0 关闭——不建议：一条跑不完的命令"
                        "能占满 shell 自带的 600 秒上限，整轮耗时会全被它盖住")
    p.add_argument("--timeout", type=float, default=900, help="单次 ee exec 超时秒数")
    p.add_argument("--check-timeout", type=float, default=300)
    p.add_argument("--ee", dest="ee_cmd", default=None,
                   help="调用 ee 的命令（默认当前 Python 的 -m ee；可写成 \"ee\"、exe 路径或 .py 脚本）")
    p.add_argument("--key-env", default="DEEPSEEK_API_KEY", help="检查哪个环境变量里有 key（不打印）")
    p.add_argument("--out", help="结果目录：results.jsonl + summary.md")
    p.add_argument("--keep", action="store_true", help="保留每次运行的仓库副本（排查用）")
    p.add_argument("--selfcheck", action="store_true", help="不调模型，只检查任务文件是否有效")
    p.add_argument("--preset", choices=sorted(PRESETS),
                   help="改前/改后对比：a2 = 工具描述压缩前（a6e2b22~1），a3 = edit 改动前（4cc28dc~1）；默认只跑 full")
    p.add_argument("--base", help="配合 --preset 自定义基线提交（cmd.exe 里写 ~1 别写 ^）")
    return p


def main(argv=None) -> int:
    safe_console()
    args = build_parser().parse_args(argv)
    if args.preset and args.ee_cmd:
        print("--preset 用 git worktree 里的源码跑 ee，不能同时给 --ee。", file=sys.stderr)
        return 2
    args.ee_cmd = resolve_ee_cmd(args.ee_cmd)
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
    profiles_arg = args.profiles
    if args.preset and "--profiles" not in (argv if argv is not None else sys.argv[1:]):
        profiles_arg = "full"           # 基线版本未必有 lean，默认只对比 full
    args.profile_list = [p.strip() for p in profiles_arg.split(",") if p.strip()]
    preset = None
    wt_root = None
    engines: list[Engine] = [Engine("", list(args.ee_cmd))]
    try:
        if args.preset:
            if not (repo / ".git").exists():
                print(f"--preset 需要 git 仓库：{repo}", file=sys.stderr)
                return 2
            preset = dict(PRESETS[args.preset])
            if args.base:
                preset["base"] = args.base
            wt_root = Path(tempfile.mkdtemp(prefix="ee-eval-base-"))
            wt = wt_root / "base"
            preset["sha"] = add_worktree(repo, preset["base"], wt)
            print(f"基线 {preset['base']}（{preset['sha']}）检出到 {wt}", file=sys.stderr, flush=True)
            engines = [engine_from_src("base", wt, f"基线 {preset['base']}"),
                       engine_from_src("cur", repo, "当前")]
        return _run_all(args, repo, tasks, engines, preset)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2
    finally:
        if wt_root is not None:
            remove_worktree(repo, wt_root / "base")
            _rmtree(wt_root)


def _run_all(args, repo: Path, tasks: list[Task], engines: list[Engine], preset: dict | None) -> int:
    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    total = len(tasks) * len(args.profile_list) * args.runs * len(engines)
    n = 0
    for i in range(args.runs):                 # 交错跑：几种配置受同一时段的服务端波动影响
        for t in tasks:
            for prof in args.profile_list:
                for eng in engines:
                    n += 1
                    who = f"{eng.variant} · {prof}" if eng.variant else prof
                    print(f"[{n}/{total}] {t.id} · {who} · 第 {i + 1} 次 …", file=sys.stderr, flush=True)
                    rec = run_once(t, prof, i + 1, args, repo, eng)
                    results.append(rec)
                    print(f"    {'完成' if rec.get('passed') else '未完成'} · {rec.get('reason')} · "
                          f"{rec.get('steps', 0)} 步 · {rec.get('duration_s', 0)}s"
                          + (f" · {rec['error']}" if rec.get("error") else ""), file=sys.stderr, flush=True)
                    if out_dir:
                        with open(out_dir / "results.jsonl", "a", encoding="utf-8") as f:
                            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    text = render(results, tasks, preset)
    print(text)
    if out_dir:
        (out_dir / "summary.md").write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
