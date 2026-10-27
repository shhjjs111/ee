"""检查点时间线（D5-7）：在磁盘快照（影子 git 仓库）之上列回合检查点、算 diff、恢复、清理。

检查点 = 会话里每条有 tree 的用户条目（回合前 tree / 回合后 tree_after），
加上恢复前自动存的“恢复点”（thread.meta["restore_points"]，能反悔）。
清理（gc）：每个会话保留最近 keep_days 天内的、以及最近 keep_turns 个回合的 tree，其余从影子仓库删除；
工作区目录已不存在、也没有会话再引用的影子仓库整个删掉。
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path

from ee.core import snapshot

log = logging.getLogger("ee.checkpoints")

DEFAULT_KEEP_DAYS = 14
DEFAULT_KEEP_TURNS = 100
GC_INTERVAL = 24 * 3600
_STAMP = ".last_gc"


def keep_settings(cfg_data: dict) -> tuple[float, int]:
    sn = (cfg_data or {}).get("snapshot") or {}
    try:
        days = max(0.0, float(sn.get("keep_days", DEFAULT_KEEP_DAYS)))
    except (TypeError, ValueError):
        days = DEFAULT_KEEP_DAYS
    try:
        turns = max(1, int(sn.get("keep_turns", DEFAULT_KEEP_TURNS)))
    except (TypeError, ValueError):
        turns = DEFAULT_KEEP_TURNS
    return days, turns


def summarize(text: str, n: int = 80) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def session_trees(items: list[dict], meta: dict, keep_days: float, keep_turns: int, now: float) -> set[str]:
    """一个会话里要保留的 tree。"""
    users = [it for it in items if it.get("type") == "user" and it.get("tree")]
    keep: set[str] = set()
    cutoff = now - keep_days * 86400
    for i, it in enumerate(users):
        recent = i >= len(users) - keep_turns or float(it.get("ts") or now) >= cutoff
        if recent:
            keep.add(it["tree"])
            if it.get("tree_after"):
                keep.add(it["tree_after"])
    for rp in meta.get("restore_points") or []:
        if rp.get("tree") and float(rp.get("ts") or now) >= cutoff:
            keep.add(rp["tree"])
    rps = [rp for rp in meta.get("restore_points") or [] if rp.get("tree")]
    keep |= {rp["tree"] for rp in rps[-keep_turns:]}
    return keep


def collect_keep(home: Path, keep_days: float, keep_turns: int, now: float | None = None) -> dict[str, set[str]]:
    """{影子仓库目录名: 要保留的 tree}。读 ~/.ee/sessions/*.jsonl。"""
    from ee.core.store import SessionStore
    now = time.time() if now is None else now
    out: dict[str, set[str]] = {}
    for p in SessionStore.list_sessions(home):
        try:
            d = SessionStore.load(p)
        except Exception as e:  # noqa: BLE001
            log.info("读取会话 %s 失败，跳过：%s", p.name, e)
            continue
        cwd = (d.get("meta") or {}).get("cwd")
        if not cwd:
            continue
        key = snapshot.repo_key(cwd)
        out.setdefault(key, set()).update(session_trees(d.get("items") or [], d["meta"], keep_days, keep_turns, now))
    return out


def gc_all(home: Path, keep_days: float = DEFAULT_KEEP_DAYS, keep_turns: int = DEFAULT_KEEP_TURNS,
           force: bool = False, now: float | None = None, skip: set[str] | None = None) -> dict:
    """清理所有影子仓库。force=False 时距上次清理不到一天就跳过。返回统计。"""
    root = Path(home) / "snapshot"
    now = time.time() if now is None else now
    stamp = root / _STAMP
    if not root.is_dir():
        return {"skipped": True, "repos": 0}
    if not force:
        try:
            if now - stamp.stat().st_mtime < GC_INTERVAL:
                return {"skipped": True, "repos": 0}
        except OSError:
            pass
    keep = collect_keep(Path(home), keep_days, keep_turns, now)
    stats = {"skipped": False, "repos": 0, "removed_repos": 0, "kept_trees": 0, "errors": []}
    for gd in sorted(p for p in root.iterdir() if p.is_dir()):
        if skip and gd.name in skip:
            continue
        try:
            wt = (gd / "ee-worktree.txt").read_text(encoding="utf-8").strip()
        except OSError:
            wt = ""
        if not wt or not Path(wt).is_dir():
            if not keep.get(gd.name):
                shutil.rmtree(gd, ignore_errors=True)      # 工作区没了、也没有会话引用：整个删
                stats["removed_repos"] += 1
            continue
        ds = snapshot.for_dir(Path(wt), Path(home))
        if not ds.available:
            continue
        try:
            r = ds.gc(keep.get(gd.name, set()))
            stats["repos"] += 1
            stats["kept_trees"] += r["kept"]
        except Exception as e:  # noqa: BLE001
            log.warning("清理影子仓库 %s 失败：%s", gd, e)
            stats["errors"].append(f"{wt}：{e}")
    try:
        stamp.write_text(json.dumps({"t": now}), encoding="utf-8")
    except OSError:
        pass
    return stats
