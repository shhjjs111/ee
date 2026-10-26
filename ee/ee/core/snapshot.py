"""磁盘快照（D4-1）：影子 git 仓库记录每回合前后的工作区。

- 独立 git 目录 ~/.ee/snapshot/<工作区真实路径的哈希>，work-tree 指向工作区；不碰项目自己的 .git。
- capture()：`add -A` + `write-tree`，返回 tree 哈希（内容寻址，重复内容不占空间）。
  遵守工作区里的 .gitignore（和项目 .git/info/exclude）；未跟踪且大于 2MB 的文件不收；
  非 git 项目额外排除 node_modules、.venv 等常见依赖/缓存目录。
- changed(a, b)：两个 tree 之间变了哪些文件（相对路径）。
- restore(tree, paths)：把这些文件恢复成 tree 里的版本，tree 里没有的删除；其他文件不动。
- 没装 git、工作区是用户目录/盘根、抓取失败或超时：返回 None，调用方回退内存快照。
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path

from ee.proc import run_capture

log = logging.getLogger("ee.snapshot")

MAX_UNTRACKED_BYTES = 2 * 1024 * 1024
CAPTURE_TIMEOUT = 60.0
DEFAULT_EXCLUDES = ["node_modules/", ".venv/", "venv/", "__pycache__/", ".mypy_cache/", ".pytest_cache/",
                    ".ruff_cache/", ".tox/", ".gradle/", ".idea/", ".vs/", "*.pyc"]
_BEGIN, _END = "# >>> ee 自动排除（大文件）", "# <<< ee"


class SnapshotError(Exception):
    pass


def _git() -> str | None:
    from ee.core.gitinfo import git_exe
    return git_exe()


class DiskSnapshot:
    def __init__(self, worktree: Path, home: Path):
        self.worktree = Path(os.path.realpath(worktree))
        key = hashlib.sha1(str(self.worktree).lower().encode("utf-8")).hexdigest()[:16]
        self.gitdir = Path(home) / "snapshot" / key
        self.exe = _git()
        self.disabled = ""            # 非空 = 不可用的原因
        self._lock = threading.Lock()
        self._ready = False
        if not self.exe:
            self.disabled = "没有找到 git"
        elif self._too_broad():
            self.disabled = "工作区是用户目录或盘根，不做磁盘快照"
        elif not self.worktree.is_dir():
            self.disabled = "工作区不存在"

    # ------------------------------------------------------------ 基础
    @property
    def available(self) -> bool:
        return not self.disabled

    def _too_broad(self) -> bool:
        w = self.worktree
        return w == Path(os.path.realpath(Path.home())) or w.parent == w

    def _env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_DIR=str(self.gitdir), GIT_WORK_TREE=str(self.worktree), GIT_TERMINAL_PROMPT="0",
                   GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
        return env

    def _run(self, *args: str, timeout: float = 30.0, input: bytes | None = None) -> bytes:
        argv = [self.exe or "git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false",
                "-c", "core.quotepath=false", "-c", "core.longpaths=true", "-c", "core.fsmonitor=false",
                "-c", "core.hooksPath=" + os.devnull, *args]
        code, out, err = run_capture(argv, cwd=self.worktree, timeout=timeout, input=input, env=self._env())
        if code != 0:
            msg = err.decode("utf-8", "replace").strip()[:300] or f"退出码 {code}"
            raise SnapshotError(f"git {args[0]} 失败：{'超时' if code == 124 else msg}")
        return out

    def _ensure(self) -> None:
        if self._ready:
            return
        if not (self.gitdir / "HEAD").is_file():
            self.gitdir.mkdir(parents=True, exist_ok=True)
            self._run("init", "-q")
            for k, v in (("core.autocrlf", "false"), ("core.symlinks", "false"), ("core.longpaths", "true"),
                         ("gc.auto", "0"), ("core.quotepath", "false")):
                self._run("config", k, v)
            (self.gitdir / "ee-worktree.txt").write_text(str(self.worktree), encoding="utf-8")
        self._write_excludes([])
        self._ready = True

    def _write_excludes(self, big: list[str]) -> None:
        lines: list[str] = []
        src = self.worktree / ".git"
        if src.is_dir():
            try:
                lines += (src / "info" / "exclude").read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                pass
        else:
            lines += DEFAULT_EXCLUDES
        if big:
            lines += [_BEGIN, *("/" + p.replace("\\", "/") for p in big), _END]
        f = self.gitdir / "info" / "exclude"
        f.parent.mkdir(parents=True, exist_ok=True)
        text = "\n".join(lines) + "\n"
        try:
            if f.read_text(encoding="utf-8") == text:
                return
        except OSError:
            pass
        f.write_text(text, encoding="utf-8")

    def _big_untracked(self) -> list[str]:
        out = self._run("ls-files", "-z", "--others", "--exclude-standard", timeout=CAPTURE_TIMEOUT)
        big = []
        for rel in out.decode("utf-8", "replace").split("\0"):
            if not rel:
                continue
            try:
                if (self.worktree / rel).stat().st_size > MAX_UNTRACKED_BYTES:
                    big.append(rel)
            except OSError:
                continue
        return big

    # ------------------------------------------------------------ 接口
    def capture(self) -> str | None:
        if self.disabled:
            return None
        with self._lock:
            try:
                self._ensure()
                self._write_excludes(self._big_untracked())
                self._run("add", "-A", "--ignore-errors", "--", ".", timeout=CAPTURE_TIMEOUT)
                tree = self._run("write-tree").decode().strip()
                return tree or None
            except Exception as e:  # noqa: BLE001
                log.warning("磁盘快照失败（%s）：%s", self.worktree, e)
                if "超时" in str(e):
                    self.disabled = "抓取快照超时（工作区太大），本次运行改用内存快照"
                return None

    def changed(self, a: str, b: str) -> list[str]:
        if not a or not b or a == b:
            return []
        with self._lock:
            out = self._run("diff", "--name-only", "-z", "--no-renames", a, b)
        return [p for p in out.decode("utf-8", "replace").split("\0") if p]

    def restore(self, tree: str, paths: list[str]) -> list[str]:
        """把 paths（相对路径）恢复成 tree 里的版本；返回实际处理过的绝对路径。"""
        paths = [p for p in dict.fromkeys(paths) if p and self._inside(p)]
        if not paths:
            return []
        done: list[str] = []
        with self._lock:
            present: set[str] = set()
            for chunk in _chunks(paths, 200):
                out = self._run("ls-tree", "-r", "-z", "--name-only", tree, "--", *chunk)
                present |= {p for p in out.decode("utf-8", "replace").split("\0") if p}
            keep = [p for p in paths if p in present]
            for chunk in _chunks(keep, 200):
                self._run("checkout", "-f", tree, "--", *chunk)
            for p in paths:
                full = self.worktree / p
                if p not in present:
                    try:
                        if full.is_file() or full.is_symlink():
                            full.unlink()
                            _prune_empty(full.parent, self.worktree)
                    except OSError as e:
                        log.warning("删除 %s 失败：%s", p, e)
                        continue
                done.append(str(full))
        return done

    def _inside(self, rel: str) -> bool:
        full = os.path.realpath(self.worktree / rel)
        return full == str(self.worktree) or full.startswith(str(self.worktree) + os.sep)


def _chunks(xs: list[str], n: int):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _prune_empty(d: Path, root: Path) -> None:
    while d != root and root in d.parents:
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent


_cache: dict[tuple[str, str], DiskSnapshot] = {}
_cache_lock = threading.Lock()


def for_dir(worktree: Path, home: Path) -> DiskSnapshot:
    """同一工作区共用一个实例（多个会话串行访问同一影子仓库）。"""
    key = (os.path.realpath(worktree), str(home))
    with _cache_lock:
        ds = _cache.get(key)
        if ds is None:
            ds = _cache[key] = DiskSnapshot(Path(worktree), Path(home))
        return ds
