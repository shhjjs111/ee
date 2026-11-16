"""allout 多路**定向集成**验收（可复用驱动，真实模型，隔离临时仓库与 home）。

它验证的是生产函数而不是一个"演示"：
  `Thread._plan_allout` / `_allout_fanout` / `_allout_rank` / `_cleanup_allout_losers`
  `allout.candidate_patch` / `apply_patch`、`Thread._delivery_gate`、`goal.judge`

明确不覆盖（不要把它当端到端）：
- 没有"自然首轮失败 → 批评修复 → 分叉"这条真实链路；这里直接进分叉。
- 走的是生产规划函数（`_plan_allout` → `allout.derive`），**不覆盖** plan 的候选数和
  `selection_grace_seconds`；硬件/上下文不允许 2 路时如实记录 1 路并停止。
- A/B 用真实 `side_complete`，两次位置对调；模型产出相同补丁时按设计跳过（另有
  一组"不同补丁"的候选专门验证位置对调与 `comparisons`）。
- 不 mock 任何响应、不改验收文件、不为通过而放大时间窗。

隔离：所有仓库、worktree、会话存储都在临时目录；配置沿用当前默认模型（凭据由
`load_config` 解析，脚本不打印、不落盘任何密钥）。用法：

    $env:PYTHONUTF8='1'
    ./.venv/Scripts/python.exe ee/scripts/allout_multilane_probe.py [--json 路径]

退出码 0 = 关键断言全过；1 = 有断言失败（附精确证据）；2 = 环境/前置不满足。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TMP = Path(tempfile.mkdtemp(prefix="ee-allout-ml-"))
OVERALL_TIMEOUT = int(os.environ.get("EE_PROBE_TIMEOUT", "900"))
LF = chr(10)   # 显式换行：Windows 上 write_text 默认写 CRLF，会让 worktree 与主目录字节哈希不一致


# ---------------------------------------------------------------- 工具

def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{out.stderr[:400]}")
    return out.stdout.strip()


def tree_digest(root: Path) -> dict[str, str]:
    """目录内容的指纹（验证"验收测试在运行前后没变"）。"""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix in (".py", ".ini", ".toml", ".cfg"):
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    return out


SLUGIFY_TESTS = """import pytest
from slugify import slugify


def test_basic():
    assert slugify("Hello World") == "hello-world"


def test_punct():
    assert slugify("A, b!") == "a-b"
    assert slugify("a---b___c") == "a-b-c"


def test_edge():
    assert slugify("--edge--") == "edge"
    assert slugify("") == ""
    assert slugify("   ") == ""


def test_translit():
    assert slugify("Über Straße") == "uber-strasse"
    assert slugify("smørrebrød") == "smorrebrod"


def test_unicode():
    assert slugify("中文 标题") == "中文-标题"


def test_truncate():
    text = "a" * 30 + "-" + "b" * 40
    assert slugify(text) == "a" * 30
    assert len(slugify("x" * 80)) == 60
"""

SLUGIFY_TASK = (
    "实现 slugify.py 的 slugify(text)：小写；常见拉丁扩展字母转写（ä→a ö→o ü→u ß→ss æ→ae ø→o å→a，"
    "大小写都算）；其余非字母数字的连续片段压成一个 -；去掉首尾 -；空串返回空串；"
    "长度上限 60，超长时在 ≤60 的最后一个 - 处截断，没有 - 就硬截 60，截断后再去尾部 -。"
    + LF + "实现方式自由，只要 tests/ 全过；不要改 tests/ 下的文件。判据由外层统一执行。"
)

_SLUGIFY_SEED = [
    "def slugify(text):",
    '    return "-".join(text.strip().lower().split())',
    "",
]
_SLUGIFY_IGNORE = ["__pycache__/", "*.pyc", ".pytest_cache/", ""]


def make_toy_repo(root: Path, kind: str = "calc") -> Path:
    repo = root / "toy"
    if kind == "slugify":
        (repo / "tests").mkdir(parents=True)
        (repo / "slugify.py").write_text(LF.join(_SLUGIFY_SEED), encoding="utf-8", newline=LF)
        (repo / "tests" / "test_slugify.py").write_text(SLUGIFY_TESTS, encoding="utf-8", newline=LF)
        (repo / "tests" / "test_smoke.py").write_text(
            "def test_smoke():" + LF + "    assert True" + LF, encoding="utf-8", newline=LF)
        (repo / ".gitignore").write_text(LF.join(_SLUGIFY_IGNORE), encoding="utf-8", newline=LF)
        (repo / "README.md").write_text("toy repo for allout directed acceptance" + LF,
                                        encoding="utf-8", newline=LF)
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "probe@example.invalid")
        git(repo, "config", "user.name", "ee probe")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "init: slugify is naive on purpose")
        return repo
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8", newline=LF)
    (repo / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8", newline=LF)
    (repo / "tests" / "test_regression.py").write_text(
        "def test_unrelated_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8", newline=LF)
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n", encoding="utf-8", newline=LF)
    (repo / "README.md").write_text("toy repo for allout directed acceptance\n", encoding="utf-8", newline=LF)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "probe@example.invalid")
    git(repo, "config", "user.name", "ee probe")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init: add() is wrong on purpose")
    return repo


def write_candidate_fix(cwd: Path, body: str) -> None:
    (cwd / "calc.py").write_text(f"def add(a, b):\n    {body}\n", encoding="utf-8", newline=LF)


# ---------------------------------------------------------------- 主流程

async def main(kind: str = "calc", directed_ab: bool = True) -> tuple[dict, list[str]]:
    from ee.config import load_config
    from ee.core import acceptance, probe as P
    from ee.core import allout as A
    from ee.core import bestof as B
    from ee.core import delivery as D
    from ee.core import worktree as W
    from ee.core.goal import GoalSpec, judge
    from ee.core.session import Session
    from ee.server.threads import Thread

    failures: list[str] = []
    home = TMP / "home"
    home.mkdir()
    repo = make_toy_repo(TMP, kind)
    py = f'"{sys.executable}"' if " " in sys.executable else sys.executable
    cmd = f"{py} -m pytest -q"
    p2p_file = "tests/test_smoke.py" if kind == "slugify" else "tests/test_regression.py"
    p2p_cmd = f"{py} -m pytest -q {p2p_file}"
    report: dict = {"repo": str(repo), "home": str(home), "command": cmd, "p2p_command": p2p_cmd,
                    "task_kind": kind}
    events: list[dict] = []

    def notify(method: str, payload: dict) -> None:
        events.append({"method": method, "payload": {k: v for k, v in payload.items() if k != "thread_id"}})

    tests_before = tree_digest(repo / "tests")
    cfg = load_config(repo)                       # 真实配置；不做任何字段覆盖
    alias = cfg.default_model
    spec_profile = cfg.resolve_model(alias)
    profile = spec_profile.profile                # derive() 要的是 profile，不是 ModelSpec
    report["model"] = {
        "alias": spec_profile.alias, "provider": spec_profile.provider, "model": spec_profile.model,
        "profile_family": getattr(profile, "family", ""),
        "context_window": getattr(profile, "context_window", None),
        "context_source": "inherited profile (config 里未显式写 context_window)",
        "effort_param": bool(getattr(profile, "effort_param", "")),
    }
    if not getattr(profile, "context_window", None):
        failures.append("profile.context_window 为空，规划会退回单路（本机配置异常）")

    s = Session(cfg, model=alias, cwd=repo, mode="auto", home=home, persist=False,
                user_home=Path.home())
    s.thread_id = "probe-multilane"
    th = Thread("probe-multilane", home, notify, {"cwd": str(repo), "model": alias, "mode": "auto"})
    th.session = s

    goal = {"command": cmd, "max_rounds": 3, "pass_to_pass": p2p_cmd, "timeout": 180}
    # ---- 生产规划（不覆盖 candidates / grace）
    await th._plan_allout({"enabled": True, "candidates": 2}, goal)
    plan = th._allout_plan
    if plan is None:
        failures.append("_plan_allout 没有产出 plan")
        return report, failures
    base_win, hard_win, judge_s = th._allout_selection_window(plan)
    report["plan"] = {"candidates": plan.candidates, "limiting": plan.limiting,
                      "verified": plan.verified, "judge_slots": plan.judge_slots,
                      "explore_slots": plan.explore_slots, "effort": plan.effort or "",
                      "selection_grace_seconds": plan.selection_grace_seconds,
                      "window_base_s": round(base_win, 2), "window_hard_s": round(hard_win, 2),
                      "judge_history_s": judge_s, "reasons": list(plan.reasons)[-4:]}
    if plan.candidates < 2:
        report["note"] = ("实际硬件/上下文只支持 %d 路，按生产决策如实记录，不强推 2 路"
                          % plan.candidates)

    # ---- spec：与 _run_allout 同一口径（含既有测试保护）
    files, complete = await asyncio.to_thread(P.collect_test_files_ex, repo)
    spec = GoalSpec(command=cmd, cwd=str(repo), min_tests=1, timeout=180.0,
                    protected_tests=tuple(files), protected_tests_complete=complete)
    p2p_spec = GoalSpec(command=p2p_cmd, cwd=str(repo), min_tests=1, timeout=180.0)
    th._accept0 = acceptance.fingerprint(repo, spec.command, spec.validation_files,
                                         spec.protected_tests,
                                         protected_tests_complete=spec.protected_tests_complete)
    report["acceptance"] = {"protected_tests": len(spec.protected_tests),
                            "protected_complete": complete,
                            "accept0_keys": sorted(th._accept0.keys()),
                            "tests_digest_before": len(tests_before)}

    # ---- 基线判据：真实跑一次（失败是预期的），同时给择优窗提供真实耗时
    t0 = time.monotonic()
    v0 = await judge(spec)
    th._remember_judge_ms((time.monotonic() - t0) * 1000) if hasattr(th, "_remember_judge_ms") else None
    report["baseline_judge"] = {"met": bool(v0.met), "reason": v0.reason,
                                "seconds": round(time.monotonic() - t0, 2)}

    # ---- 分叉：生产函数，2 路
    shared = asyncio.Semaphore(max(plan.explore_slots, plan.judge_slots))
    th._allout_judge_lock = asyncio.Semaphore(plan.judge_slots)
    th._allout_explore_sem = shared if plan.explore_slots else None
    th._allout_work_sem = shared
    task = (SLUGIFY_TASK if kind == "slugify" else
            ("修复 calc.py 里的 add()：现在 add(2, 3) 返回 -1，需要返回 5。"
             "只改 calc.py，不要动 tests/ 下的任何文件。"
             f"判据命令是 `{cmd}`（外层统一执行，你不用重复跑整套测试）。"))
    t1 = time.monotonic()
    cands = await th._allout_fanout(spec, plan, task, plan.candidates,
                                    pass_to_pass=p2p_cmd, failure=None, images=None)
    fanout_s = time.monotonic() - t1
    report["fanout"] = {
        "seconds": round(fanout_s, 1),
        "planned": plan.candidates,
        "records": len(cands),
        "with_worktree": sum(1 for c in cands if c.get("worktree")),
        "with_verdict": sum(1 for c in cands if c.get("verdict") is not None),
        "cancelled": sum(1 for c in cands if c.get("cancelled")),
        "fanout_notified": next((e["payload"].get("started") for e in events
                                 if e["payload"].get("phase") == "fanout"), None),
    }
    report["candidates"] = [{
        "id": c.get("id"), "role": c.get("role"), "met": bool(getattr(c.get("verdict"), "met", False)),
        "tests": getattr(c.get("verdict"), "tests", None), "regressed": bool(c.get("regressed")),
        "error": (c.get("error") or "")[:160], "cost": round(float(c.get("cost") or 0.0), 4),
        "worktree": c.get("worktree"),
    } for c in cands]
    passed = [c for c in cands if A.candidate_ok(c)]
    report["passed"] = [c["id"] for c in passed]
    # B 样本要证明"补丁是模型产出的、而且不同"：直接对每个通过候选算生产补丁指纹。
    patch_shas: dict[str, str] = {}
    for c in passed:
        try:
            got = await asyncio.to_thread(A.candidate_patch, c["cwd"], c["base"])
            patch_shas[c["id"]] = got.get("sha", "") if got.get("ok") else ""
        except Exception as e:  # noqa: BLE001
            patch_shas[c["id"]] = f"error:{type(e).__name__}"
    report["candidate_patch_shas"] = patch_shas
    report["distinct_patches"] = len({v for v in patch_shas.values() if v}) == len(patch_shas) > 1
    # 没进判据的候选要说清为什么：验收文件被改（保护生效）还是别的。
    diag = []
    for c in cands:
        cwd = c.get("cwd")
        if not cwd:
            continue
        try:
            now = acceptance.fingerprint(cwd, spec.command, spec.validation_files,
                                         spec.protected_tests,
                                         protected_tests_complete=spec.protected_tests_complete)
            d = acceptance.diff(th._accept0, now)
            st = subprocess.run(["git", "status", "--porcelain"], cwd=str(cwd),
                                capture_output=True, text=True, timeout=60).stdout.strip().splitlines()
            diag.append({"id": c.get("id"), "verdict": bool(c.get("verdict")),
                         "acceptance": {k: v for k, v in d.items() if v},
                         "git_status": st[:6]})
        except Exception as e:  # noqa: BLE001
            diag.append({"id": c.get("id"), "error": f"{type(e).__name__}: {e}"})
    report["fanout_diagnostics"] = diag
    if len(passed) < 2:
        report["note"] = (report.get("note", "") +
                          f" 择优窗内只有 {len(passed)} 个候选跑完（窗口 base={base_win:.1f}s / "
                          f"hard={hard_win:.1f}s）；按生产语义早退取消其余，属正常但值得优化："
                          "可把窗口按本轮判据耗时分位再放宽一档或让已进入 testing 的路单独计窗口。").strip()

    # ---- A/B：不同补丁的真实候选（验证位置对调与 comparisons 计数）
    if directed_ab:
        ab, ab_made = await _directed_ab(th, W, judge, repo, home, py, cmd, acceptance)
    else:
        ab, ab_made = {"skipped": "定向 A/B 已关闭（本样本只看真实候选）"}, []
    report["ab_directed"] = ab
    if directed_ab:
        if ab.get("error"):
            failures.append(f"A/B 定向验证失败：{ab['error']}")
        else:
            if {c["comparisons"] for c in ab["candidates"]} != {1}:
                failures.append(f"A/B comparisons 计数异常：{ab['candidates']}")
            if sum(c["votes"] for c in ab["candidates"]) != 0:
                failures.append(f"A/B 净胜票应互相抵消：{ab['candidates']}")

    # ---- 写回 + 交付闸门
    pool = list(passed)
    if len(pool) >= 2:
        winner = await th._allout_rank(pool, task, getattr(v0, "output", "") or "")
        report["rank"] = {"source": "fanout",
                          "winner": winner.get("id") if winner else None,
                          "votes": {c["id"]: c.get("votes") for c in pool},
                          "comparisons": {c["id"]: c.get("comparisons") for c in pool},
                          "fair_pairs": [list(p) for p in B.fair_pairs([c["id"] for c in pool])]}
    elif ab.get("winner") and len(ab_made) >= 2 and not ab.get("error"):
        # fanout 被资源钳到 1 路（或唯一候选被判 regressed）时，写回/闸门/回收改在
        # **真实的两个 A/B 候选**上验证：它们是真 git worktree、真判据、真模型评审。
        pool = ab_made
        winner = next((c for c in pool if c["id"] == ab["winner"]), None)
        report["rank"] = {"source": "ab_directed", "winner": winner.get("id") if winner else None,
                          "votes": {c["id"]: c.get("votes") for c in pool},
                          "comparisons": {c["id"]: c.get("comparisons") for c in pool},
                          "fair_pairs": [list(p) for p in B.fair_pairs([c["id"] for c in pool])],
                          "note": "fanout 侧未产出 2 个通过候选，写回链路用定向 A/B 的真实候选验证"}
    else:
        winner = pool[0] if pool else None
        report["rank"] = {"source": "none", "winner": winner.get("id") if winner else None}
    if winner is None:
        failures.append("没有候选通过判据，无法验证写回链路")
        return report, failures

    patch = await asyncio.to_thread(A.candidate_patch, winner["cwd"], winner["base"])
    report["apply"] = {"patch_ok": bool(patch.get("ok")), "paths": patch.get("paths") or []}
    if not patch.get("ok"):
        failures.append(f"候选补丁不可用：{patch.get('error')}")
        return report, failures
    pre = await asyncio.to_thread(s.disk_capture)
    expected = await asyncio.to_thread(D.build_expected, s.disk(), pre, patch["patch"],
                                       patch["paths"], repo)
    res = await asyncio.to_thread(A.apply_patch, repo, patch["patch"])
    applied = await asyncio.to_thread(s.disk_capture)
    unexpected = await asyncio.to_thread(D.unexpected_changes, s.disk(), pre, applied,
                                         set(patch["paths"]))
    report["apply"].update({"applied": bool(res.get("applied")), "error": res.get("error", ""),
                            "expected_ok": expected.ok, "unexpected": unexpected})
    gate = await th._delivery_gate(s, spec, p2p_spec, source="apply",
                                   apply_info={"patch_sha": patch.get("sha", ""), "mismatches": [],
                                               "unexpected": unexpected,
                                               "expected": expected.as_dict()},
                                   delivered_tree=applied)
    deliv = th._delivery or {}
    report["delivery"] = {"gate": gate, "status": deliv.get("status"),
                          "target": (deliv.get("target") or {}).get("reason"),
                          "p2p_status": (deliv.get("regression") or {}).get("status"),
                          "p2p_reason": (deliv.get("regression") or {}).get("reason"),
                          "acceptance_changed": (deliv.get("acceptance") or {}).get("changed")}
    if gate != "met":
        failures.append(f"交付闸门未通过：{report['delivery']}")

    # ---- 落选候选安全回收（生产函数）
    cleanup = await th._cleanup_allout_losers(pool, winner["id"])
    losers = [c for c in pool if c.get("id") != winner["id"] and c.get("worktree")]
    branches_after = git(repo, "branch", "--list", "--format=%(refname:short)")
    report["losers_cleanup"] = {
        "removed": cleanup.get("removed", []), "kept": cleanup.get("kept", []),
        "loser_dirs": [{"id": c["id"], "exists": Path(str(c["worktree"])).exists(),
                        "branch_kept": bool(c.get("branch") and
                                            any(str(c.get("branch")) == b for b in branches_after.splitlines()))}
                       for c in losers],
        "winner_dir_exists": bool(winner.get("worktree") and Path(str(winner["worktree"])).exists()),
    }
    if losers and not report["losers_cleanup"]["removed"]:
        failures.append("落选候选一个都没被回收（收尾信息齐全时应能安全删除目录）")
    for item in report["losers_cleanup"]["loser_dirs"]:
        if item["exists"] and item["id"] in (cleanup.get("removed") or []):
            failures.append(f"落选目录声称已删但仍存在：{item['id']}")
        if not item["branch_kept"]:
            failures.append(f"落选分支没有保留（应可恢复）：{item['id']}")

    # ---- 验收测试未被改动 + 目标/回归真实通过
    tests_after = tree_digest(repo / "tests")
    report["tests_unchanged"] = tests_before == tests_after
    if not report["tests_unchanged"]:
        failures.append("运行前后既有测试文件发生变化")
    final = await judge(GoalSpec(command=cmd, cwd=str(repo), min_tests=1, timeout=180.0))
    reg = await judge(p2p_spec)
    report["final_judge"] = {"met": bool(final.met), "reason": final.reason}
    report["final_p2p"] = {"met": bool(reg.met), "reason": reg.reason}
    if not (final.met and reg.met):
        failures.append(f"写回后判据未通过：target={final.reason} / p2p={reg.reason}")

    usage = dict(s.stats.totals() if hasattr(s.stats, "totals") else {})
    report["usage"] = {"requests": usage.get("requests"), "prompt": usage.get("prompt"),
                       "completion": usage.get("completion"),
                       "cost_estimate": round(float(usage.get("cost") or 0.0), 4),
                       "currency": usage.get("currency", ""),
                       "note": "费用来自 provider 返回的 usage；未返回价格的请求不计入，属估算"}
    report["walltime_s"] = round(time.monotonic() - t0, 1)
    report["branches"] = branches_after.splitlines()
    return report, failures


async def _directed_ab(th, W, judge, repo: Path, home: Path, py: str, cmd: str,
                        acceptance) -> tuple[dict, list[dict]]:
    """两个**真实 git 候选 + 不同补丁**，跑生产 `_allout_rank`（真实 side_complete）。"""
    from ee.core import allout as A
    from ee.core.goal import GoalSpec

    out: dict = {"candidates": [], "counts": {}}
    made = []
    try:
        for i, body in enumerate(("return a + b", "return sum((a, b))  # 另一种写法")):
            info = await asyncio.to_thread(W.create_for_agent, repo, f"abprobe-{i + 1}", home)
            cwd = Path(info["cwd"])
            write_candidate_fix(cwd, body)
            v_out = await asyncio.to_thread(W.finish_agent, info, f"probe A/B 候选 {i + 1}", home)
            v = await judge(GoalSpec(command=cmd, cwd=str(cwd), min_tests=1, timeout=180.0))
            patch = await asyncio.to_thread(A.candidate_patch, cwd, info["agent_base"])
            made.append({
                "id": f"ab-{i + 1}", "cwd": str(cwd), "base": info["agent_base"],
                "worktree": info["path"], "branch": info["branch"],
                "head": await asyncio.to_thread(W.head_sha, Path(info["path"])),
                "finish": v_out, "verdict": v, "regressed": False,
                "touched_tests": False, "files": v_out.get("file_count", 0),
                "additions": v_out.get("additions", 0), "deletions": v_out.get("deletions", 0),
                "votes": 0, "comparisons": 0, "cost": 0.0,
                "patch_sha": patch.get("sha", ""), "patch_ok": bool(patch.get("ok")),
            })
        out["patch_shas"] = [c.pop("patch_sha") for c in made]
        out["patch_ok"] = all(bool(p.get("patch_ok")) for p in made)
        winner = await th._allout_rank(made, "修 add()", "")
        out["winner"] = winner.get("id") if winner else None
        out["candidates"] = [{"id": c["id"], "votes": c.get("votes"),
                              "comparisons": c.get("comparisons")} for c in made]
        out["counts"] = {c["id"]: c.get("comparisons") for c in made}
        out["distinct_patches"] = len({p for p in out["patch_shas"]}) == 2
        if not out["distinct_patches"]:
            out["error"] = "两个定向候选的补丁相同，A/B 会被跳过（不构成验证）"
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out, made


def _cleanup(path: str) -> str:
    try:
        shutil.rmtree(path, ignore_errors=True)
        return "removed"
    except Exception as e:  # noqa: BLE001
        return f"failed: {e}"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="", help="把脱敏报告写到这个路径")
    ap.add_argument("--task", default="calc", choices=("calc", "slugify"),
                    help="calc=单点修复（候选易同补丁）；slugify=有设计空间（候选易不同补丁）")
    ap.add_argument("--no-directed-ab", action="store_true",
                    help="跳过“手工补丁”的 A/B 定向验证（B 样本只看真实候选）")
    args = ap.parse_args()
    try:
        report, failures = asyncio.run(asyncio.wait_for(
            main(kind=args.task, directed_ab=not args.no_directed_ab), OVERALL_TIMEOUT))
    except (asyncio.TimeoutError, TimeoutError):
        report, failures = {"error": f"整体超时（{OVERALL_TIMEOUT}s），按失败记录，不重跑"}, ["timeout"]
    except Exception as e:  # noqa: BLE001
        report, failures = {"error": f"{type(e).__name__}: {e}"}, [f"{type(e).__name__}: {e}"]
    report["failures"] = failures
    report["ok"] = not failures
    report["cleanup"] = {"temp_dir": str(TMP), "result": _cleanup(str(TMP))}
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.json:
        Path(args.json).write_text(text + "\n", encoding="utf-8", newline=LF)
    print(text)
    sys.exit(0 if not failures else 1)
