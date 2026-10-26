"""改动审阅：把 gitinfo.diff 的整段 diff 拆成按文件的列表；单文件撤销。"""
from __future__ import annotations

import re
from pathlib import Path

from ee.core import gitinfo

_HEADER = re.compile(r"^diff --git a/(.*?) b/(.*)$")


def split_diff(text: str) -> list[dict]:
    files: list[dict] = []
    cur: dict | None = None
    lines: list[str] = []

    def flush():
        if cur is None:
            return
        cur["patch"] = "\n".join(lines)
        files.append(cur)

    for line in text.splitlines():
        m = _HEADER.match(line)
        if m:
            flush()
            cur = {"path": m.group(2), "old_path": m.group(1), "status": "modified", "additions": 0,
                   "deletions": 0, "binary": False}
            lines = [line]
            continue
        if cur is None:
            continue
        lines.append(line)
        if line.startswith("new file"):
            cur["status"] = "added"
            if "二进制" in line or "过大" in line:
                cur["binary"] = True
        elif line.startswith("deleted file"):
            cur["status"] = "deleted"
        elif line.startswith("rename from"):
            cur["status"] = "renamed"
        elif line.startswith("Binary files"):
            cur["binary"] = True
        elif line.startswith("+") and not line.startswith("+++"):
            cur["additions"] += 1
        elif line.startswith("-") and not line.startswith("---"):
            cur["deletions"] += 1
    flush()
    return files


def review(cwd: Path) -> dict:
    st = gitinfo.status(cwd)
    if not st.is_repo:
        return {"is_repo": False, "git_available": gitinfo.git_exe() is not None, "files": []}
    files = split_diff(gitinfo.diff(cwd))
    return {"is_repo": True, "git_available": True, "branch": st.branch, "root": st.root,
            "ahead_behind": st.ahead_behind, "files": files,
            "additions": sum(f["additions"] for f in files), "deletions": sum(f["deletions"] for f in files)}


def revert_file(cwd: Path, rel: str) -> dict:
    """把一个文件恢复成 HEAD 的版本；HEAD 里没有的（新文件）直接删除。路径必须在仓库内。"""
    st = gitinfo.status(cwd)
    if not st.is_repo:
        raise ValueError("不是 git 仓库，无法按文件撤销")
    root = Path(st.root).resolve()
    target = (root / rel).resolve()
    if root != target and root not in target.parents:
        raise ValueError(f"路径不在仓库内：{rel}")
    rel_posix = target.relative_to(root).as_posix()
    code, _ = gitinfo._run(["cat-file", "-e", f"HEAD:{rel_posix}"], root)
    if code == 0:
        c, out = gitinfo._run(["restore", "--source=HEAD", "--staged", "--worktree", "--", rel_posix], root)
        if c != 0:
            c, out = gitinfo._run(["checkout", "HEAD", "--", rel_posix], root)
        if c != 0:
            raise ValueError(f"git 恢复失败：{out.strip() or c}")
        return {"path": rel_posix, "action": "restored"}
    gitinfo._run(["rm", "-q", "--cached", "-f", "--", rel_posix], root)
    if target.is_file():
        target.unlink()
    return {"path": rel_posix, "action": "deleted"}
