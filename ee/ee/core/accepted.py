"""改动审阅里“已接受”的块（D5-7）。

接受 = 用户看过、认可这块改动，审阅列表里把它收起来；不改文件、不碰 git 暂存区。
按工作区记在 ~/.ee/review/accepted.json：{工作区键: {相对路径: [块指纹]}}。
块指纹只由块内容决定（hunks.Hunk.fingerprint），前面的块变了也不失效；改动没了的指纹在下次审阅时清掉。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from ee.core.snapshot import repo_key

_lock = threading.Lock()


def _file(home: Path) -> Path:
    return Path(home) / "review" / "accepted.json"


def _load(home: Path) -> dict:
    try:
        d = json.loads(_file(home).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(home: Path, data: dict) -> None:
    f = _file(home)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, f)


def get(home: Path, root) -> dict[str, set[str]]:
    with _lock:
        d = _load(home).get(repo_key(root)) or {}
    return {k: set(v) for k, v in d.items() if isinstance(v, list)}


def mark(home: Path, root, rel: str, fp: str, on: bool) -> None:
    with _lock:
        data = _load(home)
        ws = data.setdefault(repo_key(root), {})
        cur = set(ws.get(rel) or [])
        cur = cur | {fp} if on else cur - {fp}
        if cur:
            ws[rel] = sorted(cur)
        else:
            ws.pop(rel, None)
        if not ws:
            data.pop(repo_key(root), None)
        _save(home, data)


def prune(home: Path, root, present: dict[str, set[str]]) -> dict[str, set[str]]:
    """只留还在 diff 里的指纹；返回清理后的 {路径: 已接受指纹}。"""
    with _lock:
        data = _load(home)
        key = repo_key(root)
        ws = data.get(key) or {}
        new = {}
        for rel, fps in ws.items():
            keep = set(fps) & present.get(rel, set())
            if keep:
                new[rel] = sorted(keep)
        if new != ws:
            if new:
                data[key] = new
            else:
                data.pop(key, None)
            _save(home, data)
    return {k: set(v) for k, v in new.items()}
