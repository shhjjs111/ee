"""git / gh 探测与 diff。Windows 下从 PATH 找 git.exe（shutil.which 会处理 PATHEXT）。"""
from __future__ import annotations

import difflib
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MAX_UNTRACKED_BYTES = 200_000


@dataclass
class GitStatus:
    is_repo: bool = False
    branch: str = ""
    changed: int = 0          # 已修改/暂存/删除（已跟踪）
    untracked: int = 0
    ahead_behind: str = ""
    root: str = ""
    files: list[str] = field(default_factory=list)

    @property
    def dirty(self) -> bool:
        return (self.changed + self.untracked) > 0


def git_exe() -> str | None:
    return shutil.which("git")


def gh_exe() -> str | None:
    return shutil.which("gh")


def _run(args: list[str], cwd: Path, timeout: float = 10) -> tuple[int, str]:
    exe = git_exe()
    if not exe:
        return 127, ""
    kw = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        p = subprocess.run([exe, *args], cwd=str(cwd), capture_output=True, timeout=timeout, **kw)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return p.returncode, p.stdout.decode("utf-8", errors="replace")


def status(cwd: Path) -> GitStatus:
    code, out = _run(["rev-parse", "--show-toplevel"], cwd)
    if code != 0:
        return GitStatus()
    st = GitStatus(is_repo=True, root=out.strip())
    code, out = _run(["status", "--porcelain=v1", "-b", "--untracked-files=normal"], cwd)
    for line in out.splitlines():
        if line.startswith("## "):
            head = line[3:]
            br = head.split("...")[0].split(" ")[0]
            if br.startswith("No commits yet on "):
                br = head.replace("No commits yet on ", "")
            st.branch = br
            if "[" in head:
                st.ahead_behind = head[head.index("[") + 1: head.rindex("]")] if "]" in head else ""
        elif line.startswith("??"):
            st.untracked += 1
            st.files.append(line[3:])
        elif line.strip():
            st.changed += 1
            st.files.append(line[3:])
    if st.branch in ("HEAD", "") :
        c, o = _run(["rev-parse", "--short", "HEAD"], cwd)
        st.branch = f"detached@{o.strip()}" if c == 0 and o.strip() else (st.branch or "HEAD")
    return st


def diff(cwd: Path, include_untracked: bool = True) -> str:
    """工作区相对 HEAD 的完整 diff（含暂存），加上未跟踪文件（当作新增）。"""
    code, has_head = _run(["rev-parse", "--verify", "-q", "HEAD"], cwd)
    if code == 0:
        _, out = _run(["diff", "--no-color", "--no-ext-diff", "HEAD"], cwd, timeout=30)
    else:
        _, out = _run(["diff", "--no-color", "--cached"], cwd, timeout=30)
    parts = [out.rstrip("\n")] if out.strip() else []
    if include_untracked:
        _, files = _run(["ls-files", "--others", "--exclude-standard"], cwd)
        root = Path(_run(["rev-parse", "--show-toplevel"], cwd)[1].strip() or cwd)
        for f in files.splitlines():
            p = root / f
            try:
                if p.stat().st_size > MAX_UNTRACKED_BYTES:
                    parts.append(f"diff --git a/{f} b/{f}\nnew file (过大，未显示内容)")
                    continue
                raw = p.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:8000]:
                parts.append(f"diff --git a/{f} b/{f}\nnew file (二进制)")
                continue
            text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
            d = difflib.unified_diff([], text.splitlines(), fromfile="/dev/null", tofile=f"b/{f}", lineterm="")
            parts.append(f"diff --git a/{f} b/{f}\nnew file mode 100644\n" + "\n".join(d))
    return "\n".join(parts)


def staged_diff(cwd: Path) -> str:
    return _run(["diff", "--no-color", "--cached"], cwd, timeout=30)[1]


def recent_log(cwd: Path, n: int = 10) -> str:
    return _run(["log", "--oneline", f"-{n}"], cwd)[1]


def commit(cwd: Path, message: str, stage_all: bool) -> tuple[bool, str]:
    if stage_all:
        c, o = _run(["add", "-A"], cwd)
        if c != 0:
            return False, o
    exe = git_exe()
    if not exe:
        return False, "找不到 git"
    p = subprocess.run([exe, "commit", "-F", "-"], cwd=str(cwd), input=message.encode("utf-8"),
                       capture_output=True, timeout=60)
    return p.returncode == 0, (p.stdout + p.stderr).decode("utf-8", errors="replace")
