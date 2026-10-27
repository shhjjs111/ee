"""改动审阅：把整段 diff 拆成按文件的列表；单文件撤销；按块接受/拒绝（D5-7）。

两种来源：
- git 项目：工作区相对 HEAD（含未跟踪文件），同 gitinfo.diff。
- 非 git 项目：磁盘快照，会话开始时的 tree → 当前工作区（需要传 thread 的磁盘快照和起点 tree）。
每个文件带 hunks: [{id, header, additions, deletions, accepted}]，id 是块指纹（见 core/hunks.py）。
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path

from ee.core import accepted, gitinfo, hunks
from ee.proc import run_capture

_HEADER = re.compile(r"^diff --git a/(.*?) b/(.*)$")
MAX_FILE_PATCH = 512 * 1024          # 单个文件 diff 超过这么大就不显示内容（界面会卡）


def split_diff(text: str) -> list[dict]:
    files: list[dict] = []
    cur: dict | None = None
    lines: list[str] = []

    def flush():
        if cur is None:
            return
        cur["patch"] = "\n".join(lines)
        if len(cur["patch"]) > MAX_FILE_PATCH:
            cur["patch"], cur["binary"], cur["too_large"] = "\n".join(lines[:4]), True, True
        files.append(cur)

    # 只按 \n 切（str.splitlines 会把 \u2028、\x0b 之类也当换行，块对不上），行尾 \r 去掉只为显示
    if text.endswith("\n"):
        text = text[:-1]
    for line in text.split("\n"):
        line = line[:-1] if line.endswith("\r") else line
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


def annotate(files: list[dict], home: Path | None, root) -> list[dict]:
    """给每个文件加 hunks 列表和已接受标记；顺手清掉已经不在 diff 里的接受记录。"""
    present: dict[str, set[str]] = {}
    for f in files:
        hs = [] if f.get("binary") else hunks.parse_hunks(f.get("patch") or "")
        f["hunks"] = [{"id": h.fingerprint, "header": h.header, "additions": h.additions,
                       "deletions": h.deletions, "accepted": False} for h in hs]
        present[f["path"]] = {h["id"] for h in f["hunks"]}
    if home is None:
        return files
    acc = accepted.prune(home, root, present)
    for f in files:
        mine = acc.get(f["path"], set())
        for h in f["hunks"]:
            h["accepted"] = h["id"] in mine
    return files


def _totals(files: list[dict]) -> dict:
    return {"additions": sum(f["additions"] for f in files), "deletions": sum(f["deletions"] for f in files)}


def review(cwd: Path, home: Path | None = None) -> dict:
    st = gitinfo.status(cwd)
    if not st.is_repo:
        return {"is_repo": False, "git_available": gitinfo.git_exe() is not None, "files": []}
    files = annotate(split_diff(gitinfo.diff(cwd)), home, Path(st.root))
    return {"is_repo": True, "git_available": True, "branch": st.branch, "root": st.root,
            "ahead_behind": st.ahead_behind, "files": files, **_totals(files)}


def snapshot_review(disk, base: str, home: Path | None = None) -> dict:
    """非 git 项目：会话开始的 tree → 当前工作区。"""
    cur = disk.capture()
    if not cur:
        raise ValueError("抓取当前工作区快照失败")
    text = hunks.display(disk.diff_text(base, cur))
    files = annotate(split_diff(text), home, disk.worktree)
    return {"files": files, "base": base, "current": cur, **_totals(files)}


def _git_root(cwd: Path, rel: str) -> tuple[Path, Path, str]:
    st = gitinfo.status(cwd)
    if not st.is_repo:
        raise ValueError("不是 git 仓库，无法按文件撤销")
    root = Path(st.root).resolve()
    target = (root / rel).resolve()
    if root != target and root not in target.parents:
        raise ValueError(f"路径不在仓库内：{rel}")
    return root, target, target.relative_to(root).as_posix()


def revert_file(cwd: Path, rel: str) -> dict:
    """把一个文件恢复成 HEAD 的版本；HEAD 里没有的（新文件）直接删除。路径必须在仓库内。"""
    root, target, rel_posix = _git_root(cwd, rel)
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


# ---------------------------------------------------------------- 按块（D5-7）

def _git_file_patch(root: Path, rel: str) -> str:
    """单个文件相对 HEAD 的原始 diff（保留 \\r、任意字节）；未跟踪文件按 gitinfo.diff 的方式当成新增。"""
    exe = gitinfo.git_exe()
    if not exe:
        raise ValueError("没有找到 git")
    base = [exe, "-c", "core.quotepath=false", "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false",
            "--literal-pathspecs"]
    has_head = run_capture([*base, "rev-parse", "--verify", "-q", "HEAD"], cwd=root)[0] == 0
    args = ["diff", "--no-color", "--no-ext-diff", "--no-textconv"] + (["HEAD"] if has_head else ["--cached"])
    code, out, err = run_capture([*base, *args, "--", rel], cwd=root, timeout=30)
    if code != 0:
        raise ValueError(f"git diff 失败：{err.decode('utf-8', 'replace').strip()[:200]}")
    if out.strip():
        return hunks.decode(out)
    code, out, _ = run_capture([*base, "ls-files", "--others", "--exclude-standard", "-z", "--", rel], cwd=root)
    if rel not in out.decode("utf-8", "replace").split("\0"):
        return ""
    raw = (root / rel).read_bytes()
    text = hunks.decode(raw).replace("\r\n", "\n")
    d = difflib.unified_diff([], text.splitlines(), fromfile="/dev/null", tofile=f"b/{rel}", lineterm="")
    return "\n".join(d) + "\n"


def _find(patch: str, hunk_id: str) -> hunks.Hunk:
    for h in hunks.parse_hunks(patch):
        if h.fingerprint == hunk_id:
            return h
    raise ValueError("这块改动已经变了（文件被改过），请刷新后再试")


def _reject(target: Path, h: hunks.Hunk) -> str:
    try:
        content = hunks.decode(target.read_bytes())
        existed = True
    except FileNotFoundError:
        content, existed = "", False
    new = hunks.reverse_apply(content, h)
    if new == "" and h.old_len == 0 and h.old_start == 0:      # 拒绝的是新文件的全部内容：删掉文件
        if existed:
            target.unlink()
        return "deleted"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(hunks.encode(new))
    return "rejected"


def hunk_git(cwd: Path, rel: str, hunk_id: str, action: str, home: Path) -> dict:
    root, target, rel_posix = _git_root(cwd, rel)
    return _hunk(root, target, rel_posix, _git_file_patch(root, rel_posix), hunk_id, action, home)


def hunk_snapshot(disk, base: str, rel: str, hunk_id: str, action: str, home: Path) -> dict:
    root = disk.worktree
    target = (root / rel).resolve()
    if root != target and root not in target.parents:
        raise ValueError(f"路径不在工作区内：{rel}")
    rel_posix = target.relative_to(root).as_posix()
    patch = ""
    if action == "reject":
        cur = disk.capture()
        if not cur:
            raise ValueError("抓取当前工作区快照失败")
        patch = disk.diff_text(base, cur, [rel_posix])
    return _hunk(root, target, rel_posix, patch, hunk_id, action, home)


def _hunk(root: Path, target: Path, rel: str, patch: str, hunk_id: str, action: str, home: Path) -> dict:
    if action in ("accept", "unaccept"):
        accepted.mark(home, root, rel, hunk_id, action == "accept")
        return {"path": rel, "action": action}
    if action != "reject":
        raise ValueError(f"未知操作：{action}")
    h = _find(patch, hunk_id)
    try:
        res = _reject(target, h)
    except hunks.HunkError as e:
        raise ValueError(str(e))
    except OSError as e:
        raise ValueError(f"写文件失败：{e}")
    accepted.mark(home, root, rel, hunk_id, False)
    return {"path": rel, "action": res}
