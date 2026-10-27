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
import re
import threading
from pathlib import Path

from ee.proc import run_capture
from ee.tools.textdecode import smart_decode

log = logging.getLogger("ee.snapshot")

MAX_UNTRACKED_BYTES = 2 * 1024 * 1024
CAPTURE_TIMEOUT = 60.0
DEFAULT_EXCLUDES = ["node_modules/", ".venv/", "venv/", "__pycache__/", ".mypy_cache/", ".pytest_cache/",
                    ".ruff_cache/", ".tox/", ".gradle/", ".idea/", ".vs/", "*.pyc"]
_BEGIN, _END = "# >>> ee 自动排除（大文件）", "# <<< ee"


def repo_key(worktree) -> str:
    """工作区对应的影子仓库目录名（真实路径小写的 sha1 前 16 位）。"""
    return hashlib.sha1(str(os.path.realpath(worktree)).lower().encode("utf-8")).hexdigest()[:16]


class SnapshotError(Exception):
    pass


class RestoreBlocked(SnapshotError):
    """预检没过：这些文件写不了（被占用/只读…），一个文件都没动。blocked = [(相对路径, 原因)]。"""

    def __init__(self, blocked: list[tuple[str, str]]):
        self.blocked = list(blocked)
        super().__init__("这些文件现在写不了（可能被其他程序占用），没有恢复任何文件：\n"
                         + "\n".join(f"{p}（{why}）" for p, why in self.blocked[:20])
                         + (f"\n……等 {len(self.blocked)} 个" if len(self.blocked) > 20 else ""))


class RestorePartial(SnapshotError):
    """恢复到一半有文件失败：restored = 已恢复的绝对路径，failed = [(相对路径, 原因)]。"""

    def __init__(self, restored: list[str], failed: list[tuple[str, str]]):
        self.restored = list(restored)
        self.failed = list(failed)
        super().__init__(f"只恢复了 {len(self.restored)} 个文件，这些没恢复：\n"
                         + "\n".join(f"{p}（{why}）" for p, why in self.failed[:20])
                         + (f"\n……等 {len(self.failed)} 个" if len(self.failed) > 20 else ""))


def _git() -> str | None:
    from ee.core.gitinfo import git_exe
    return git_exe()


class DiskSnapshot:
    def __init__(self, worktree: Path, home: Path):
        self.worktree = Path(os.path.realpath(worktree))
        self.gitdir = Path(home) / "snapshot" / repo_key(self.worktree)
        self.exe = _git()
        self.disabled = ""            # 非空 = 不可用的原因
        self._lock = threading.Lock()
        self._ready = False
        self._numstat_cache: dict[tuple[str, str], dict] = {}
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
                "-c", "core.hooksPath=" + os.devnull,
                "--literal-pathspecs", *args]       # 路径一律按字面匹配：a[b].txt 不能顺带匹配到 ab.txt
        code, out, err = run_capture(argv, cwd=self.worktree, timeout=timeout, input=input, env=self._env())
        if code != 0:
            msg = smart_decode(err).strip()[:300] or f"退出码 {code}"
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
        # 影子仓库的 info/attributes 优先级最高：屏蔽项目 .gitattributes 里的 text/eol/filter（LFS）/编码转换，
        # 快照和恢复都按原始字节，否则 eol=crlf 之类会让恢复出来的文件换行符和快照时不一样
        attrs = self.gitdir / "info" / "attributes"
        want = "* -text -eol -filter -ident -working-tree-encoding\n"
        try:
            cur = attrs.read_text(encoding="utf-8")
        except OSError:
            cur = ""
        if cur != want:
            attrs.write_text(want, encoding="utf-8")
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
            lines += [_BEGIN, *("/" + _literal(p.replace("\\", "/")) for p in big), _END]
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
        """把 paths（相对路径）恢复成 tree 里的版本，tree 里没有的删除；返回实际处理过的绝对路径。

        先预检：所有要写/删的目标都能打开写（Windows 上被别的程序锁住的文件打不开），有一个不行就
        抛 RestoreBlocked，一个文件都不动。预检通过后逐个文件写（临时文件 + 替换），中途失败不停下，
        最后抛 RestorePartial，如实列出已恢复 / 没恢复的文件。"""
        paths = [p for p in dict.fromkeys(paths) if p and self._inside(p)]
        if not paths:
            return []
        _check_tree(tree)
        with self._lock:
            entries: dict[str, tuple[str, str]] = {}          # 相对路径 → (mode, blob sha)
            for chunk in _chunks(paths, 200):
                out = self._run("ls-tree", "-r", "-z", tree, "--", *chunk)
                for rec in out.decode("utf-8", "surrogateescape").split("\0"):
                    meta, _, rel = rec.partition("\t")
                    bits = meta.split()
                    if rel and len(bits) == 3 and bits[1] == "blob":
                        entries[rel] = (bits[0], bits[2])
            blocked: list[tuple[str, str]] = []
            for p in paths:
                full = self.worktree / p
                if p not in entries and not (full.is_file() or full.is_symlink()):
                    continue                                  # 要删的文件本来就不在了
                try:
                    _probe_writable(full)
                except OSError as e:
                    blocked.append((p, _why(e)))
            if blocked:
                raise RestoreBlocked(blocked)
            done: list[str] = []
            failed: list[tuple[str, str]] = []
            for chunk in _chunks(paths, 200):
                want = [entries[p][1] for p in chunk if p in entries]
                try:
                    blobs = self._blobs(want) if want else {}
                except SnapshotError as e:
                    failed += [(p, str(e)) for p in chunk]
                    continue
                for p in chunk:
                    full = self.worktree / p
                    try:
                        if p in entries:
                            mode, sha = entries[p]
                            _put(full, blobs[sha], mode)
                        elif full.is_file() or full.is_symlink():
                            full.unlink()
                            _prune_empty(full.parent, self.worktree)
                    except (OSError, KeyError) as e:
                        log.warning("恢复 %s 失败：%s", p, e)
                        failed.append((p, _why(e)))
                        continue
                    done.append(str(full))
        if failed:
            raise RestorePartial(done, failed)
        return done

    def _blobs(self, shas: list[str]) -> dict[str, bytes]:
        """cat-file --batch 一次取出这些 blob 的原始字节。"""
        out = self._run("cat-file", "--batch", input=("\n".join(dict.fromkeys(shas)) + "\n").encode(), timeout=120.0)
        res: dict[str, bytes] = {}
        i = 0
        while i < len(out):
            nl = out.index(b"\n", i)
            head = out[i:nl].decode("ascii", "replace").split()
            if len(head) < 3:                                 # "<sha> missing"
                raise SnapshotError(f"快照里缺少对象：{' '.join(head)}")
            size = int(head[2])
            res[head[0]] = out[nl + 1:nl + 1 + size]
            i = nl + 1 + size + 1
        return res

    # ------------------------------------------------------------ 检查点时间线（D5-7）
    _DIFF_CFG = ("-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false", "-c", "diff.relative=false",
                 "-c", "diff.external=", "-c", "color.ui=never")

    def diff_text(self, a: str, b: str, paths: list[str] | None = None, context: int = 3) -> str:
        """两个 tree 之间的 unified diff（字节按 utf-8 + surrogateescape 解码，\r 原样保留）。"""
        if not a or not b or a == b:
            return ""
        _check_tree(a), _check_tree(b)
        args = [*self._DIFF_CFG, "diff", "--no-color", "--no-ext-diff", "--no-renames", "--no-textconv",
                f"-U{int(context)}", a, b]
        if paths:
            args += ["--", *paths]
        with self._lock:
            out = self._run(*args, timeout=60.0)
        return out.decode("utf-8", "surrogateescape")

    def numstat(self, a: str, b: str) -> dict:
        """{files, additions, deletions}；tree 内容寻址，结果不变，缓存起来。"""
        if not a or not b or a == b:
            return {"files": 0, "additions": 0, "deletions": 0}
        key = (a, b)
        hit = self._numstat_cache.get(key)
        if hit is not None:
            return dict(hit)
        _check_tree(a), _check_tree(b)
        with self._lock:
            out = self._run(*self._DIFF_CFG, "diff", "--numstat", "-z", "--no-renames", "--no-ext-diff", a, b)
        files = adds = dels = 0
        for rec in out.decode("utf-8", "replace").split("\0"):
            parts = rec.split("\t", 2)
            if len(parts) < 3:
                continue
            files += 1
            adds += int(parts[0]) if parts[0].isdigit() else 0
            dels += int(parts[1]) if parts[1].isdigit() else 0
        res = {"files": files, "additions": adds, "deletions": dels}
        self._numstat_cache[key] = res
        return dict(res)

    def existing(self, trees: list[str]) -> set[str]:
        """这些 tree 里还在影子仓库里的（被清理掉的不在）。"""
        trees = [t for t in dict.fromkeys(trees) if t and _TREE_RE.fullmatch(t)]
        if not trees or not (self.gitdir / "HEAD").is_file():
            return set()
        with self._lock:
            try:
                out = self._run("cat-file", "--batch-check", input=("\n".join(trees) + "\n").encode())
            except SnapshotError:
                return set()
        ok = set()
        for line in out.decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "tree":
                ok.add(parts[0])
        return ok

    def gc(self, keep: set[str]) -> dict:
        """只保留 keep 里的 tree（及其内容）和最近 1 小时内新写的对象，其余删掉。"""
        if not (self.gitdir / "HEAD").is_file():
            return {"kept": 0}
        keep = {t for t in keep if _TREE_RE.fullmatch(t)}
        with self._lock:
            present = set()
            if keep:
                out = self._run("cat-file", "--batch-check", input=("\n".join(sorted(keep)) + "\n").encode())
                present = {ln.split()[0] for ln in out.decode("utf-8", "replace").splitlines()
                           if len(ln.split()) >= 2 and ln.split()[1] == "tree"}
            refs = self._run("for-each-ref", "--format=%(refname)", "refs/ee/keep/").decode().split()
            cmds = [f"delete {r}" for r in refs if r.rsplit("/", 1)[-1] not in present]
            cmds += [f"update {_ref(t)} {t}" for t in sorted(present)]
            if cmds:
                self._run("update-ref", "--stdin", input=("\n".join(cmds) + "\n").encode())
            self._run("reflog", "expire", "--expire=now", "--all", timeout=120.0)
            self._run("-c", "gc.reflogExpire=now", "gc", "-q", "--prune=1.hour.ago", timeout=600.0)
            self._numstat_cache.clear()
        return {"kept": len(present)}

    def _inside(self, rel: str) -> bool:
        full = os.path.realpath(self.worktree / rel)
        return full == str(self.worktree) or full.startswith(str(self.worktree) + os.sep)


_TREE_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _check_tree(t: str) -> None:
    if not _TREE_RE.fullmatch(t or ""):
        raise SnapshotError(f"不是有效的快照：{t!r}")


def _ref(tree: str) -> str:
    return f"refs/ee/keep/{tree}"


def _literal(rel: str) -> str:
    """把文件名转成只匹配它自己的 gitignore 规则（转义 * ? [ \\ 和行尾空格；调用方会在前面加 /）。"""
    s = re.sub(r"([\\*?\[])", r"\\\1", rel)
    n = len(s) - len(s.rstrip(" "))
    return s[:len(s) - n] + "\\ " * n if n else s


def _chunks(xs: list[str], n: int):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _why(e: BaseException) -> str:
    if isinstance(e, PermissionError):
        return "被其他程序占用或没有写权限"
    if isinstance(e, IsADirectoryError):
        return "同名的是一个文件夹"
    return getattr(e, "strerror", None) or str(e) or type(e).__name__


def _probe_writable(full: Path) -> None:
    """预检：能不能写/删这个文件。存在的文件以读写方式打开一下（Windows 上被锁的文件这里就会失败）；
    不存在的文件检查最近一级已存在的上级目录可写。不行就抛 OSError。"""
    if full.is_symlink():
        return
    if full.exists():
        if full.is_dir():
            raise IsADirectoryError(21, "同名的是一个文件夹", str(full))
        fd = os.open(full, os.O_RDWR | getattr(os, "O_BINARY", 0))
        os.close(fd)
        return
    d = full.parent
    while not d.exists() and d != d.parent:
        d = d.parent
    if not d.is_dir():
        raise NotADirectoryError(20, "上级路径不是文件夹", str(d))
    if not os.access(d, os.W_OK):
        raise PermissionError(13, "目录没有写权限", str(d))


def _put(full: Path, data: bytes, mode: str = "100644") -> None:
    """写回一个文件的原始字节：同目录临时文件 + 替换（替换失败时 Windows 上稍等重试）。"""
    from ee.core.atomic import write_bytes
    if full.is_symlink():
        full.unlink()
    write_bytes(full, data)
    if os.name != "nt" and mode == "100755":
        try:
            os.chmod(full, 0o755)
        except OSError:
            pass


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
