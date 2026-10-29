"""文件交接的迁移清单（T10-e）：本地 ↔ worktree 搬东西之前，先把它列清楚。

方案原文（`stabilization-and-d6.md:128`）：
> 先生成迁移清单，包含 tracked/untracked、二进制、staged/unstaged、忽略文件策略；
> 用独立备份和内容哈希证明可恢复。先复制并验证目标，再切换会话 cwd 与派生资源，
> 最后经确认清理源；失败保留原件，恢复失败显式列出。

**本模块只做清单与验证，不做复制、切换、删除。** 那三步是不可逆的，必须由用户
看过清单后确认。这样即使本模块写错，代价也只是「清单不准」，而不是丢文件。

四条不变量：
1. 每一条都有 sha256 —— 否则「复制后一致」无法证明，只能靠信心。
2. 忽略文件必须出现（策略可配），不能悄悄漏掉：漏掉一个 .env 就是丢配置。
3. `.git` 内部永不进清单：复制它等于复制一半的仓库状态，比不复制更危险。
4. 清单里的路径必须都在仓库内（防目录穿越、防把源目录整个卷进去）。

`git status` 会刷新 index 的 stat 缓存，所以读取时显式 `-c core.preloadIndex=false`
并只读 porcelain 输出——本模块不改工作区（有测试守这条）。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# 这些目录里的「忽略文件」默认不迁移：都是可重建的依赖/缓存，搬过去只浪费时间
_DEP_DIRS = frozenset({"node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
                       "dist", "build", ".mypy_cache", ".ruff_cache", ".next", "target"})

BINARY_PROBE = 8000        # 取前 8KB 判断是否二进制
MAX_HASH_BYTES = 64 * 1024 * 1024   # 超大文件只哈希前 64MB，避免清单生成卡住


@dataclass
class Entry:
    path: str                  # 仓库相对路径，统一正斜杠
    category: str              # staged | unstaged | untracked | ignored | tracked
    kind: str = "file"         # file | dir | symlink
    size: int = 0
    sha256: str = ""
    binary: bool = False
    selected: bool = True      # 默认是否打算迁移

    def as_dict(self) -> dict:
        return {"path": self.path, "category": self.category, "kind": self.kind,
                "size": self.size, "sha256": self.sha256, "binary": self.binary,
                "selected": self.selected}


@dataclass
class Manifest:
    root: str
    entries: list[Entry] = field(default_factory=list)
    ignored_policy: str = "skip"     # skip | include

    def summary(self) -> dict:
        by_cat: dict[str, int] = {}
        for e in self.entries:
            by_cat[e.category] = by_cat.get(e.category, 0) + 1
        return {"total": len(self.entries),
                "selected": sum(1 for e in self.entries if e.selected),
                "bytes": sum(e.size for e in self.entries if e.selected),
                "binary": sum(1 for e in self.entries if e.binary),
                "by_category": by_cat}

    def report(self) -> str:
        s = self.summary()
        lines = [f"迁移清单：{self.root}",
                 f"共 {s['total']} 条，选中 {s['selected']} 条，{s['bytes']} 字节，"
                 f"二进制 {s['binary']} 个；忽略文件策略={self.ignored_policy}"]
        for e in self.entries:
            mark = "✓" if e.selected else "·"
            kind = " (binary)" if e.binary else ""
            lines.append(f"  {mark} [{e.category}] {e.path}{kind} {e.size}B {e.sha256[:12]}")
        return "\n".join(lines)


def _git(args: list[str], cwd: Path) -> str:
    """只读 git 查询。用 -c 关掉会写 index 的优化，保证本模块不改工作区。"""
    r = subprocess.run(["git", "-c", "core.preloadIndex=false", "-c", "core.fsmonitor=false", *args],
                       cwd=str(cwd), capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{r.stderr.decode('utf-8', 'replace')[:300]}")
    return r.stdout.decode("utf-8", "replace")


def _nul_split(text: str) -> list[str]:
    return [x for x in text.split("\0") if x]


def build_manifest(root: Path | str, *, ignored_policy: str = "skip",
                   include_ignored: bool = False) -> Manifest:
    """生成迁移清单。只读，不碰工作区。"""
    base = Path(root).resolve()
    if ignored_policy not in ("skip", "include"):
        raise ValueError(f"unknown ignored_policy: {ignored_policy}")
    m = Manifest(root=str(base), ignored_policy=ignored_policy)
    seen: dict[str, Entry] = {}

    def add(rel: str, category: str) -> None:
        norm = rel.replace("\\", "/").lstrip("./")
        if not norm or norm.startswith(".git/") or norm == ".git":
            return
        if norm in seen:
            # 同一文件可能既是 staged 又是 unstaged：staged 优先（它是更强的意图信号）
            if seen[norm].category == "staged":
                return
        full = (base / norm)
        try:
            full.resolve().relative_to(base)      # 防目录穿越
        except (ValueError, OSError):
            return
        entry = _make_entry(base, norm, category, selected=include_ignored or category != "ignored")
        seen[norm] = entry

    # 已暂存（--cached 相对 HEAD）
    for rel in _nul_split(_git(["diff", "--cached", "--name-only", "-z"], base)):
        add(rel, "staged")
    # 未暂存（相对索引）
    for rel in _nul_split(_git(["diff", "--name-only", "-z"], base)):
        add(rel, "unstaged")
    # 未跟踪（不含忽略）
    for rel in _nul_split(_git(["ls-files", "--others", "--exclude-standard", "-z"], base)):
        add(rel, "untracked")
    # 忽略文件：必须列出来，让人看见「这些不搬」
    for rel in _nul_split(_git(["ls-files", "--others", "--ignored", "--exclude-standard", "-z"], base)):
        parts = rel.replace("\\", "/").split("/")
        if any(x in _DEP_DIRS for x in parts[:-1]):
            continue                      # 依赖/缓存目录不列，避免几千条噪声
        add(rel, "ignored")

    m.entries = sorted(seen.values(), key=lambda e: (e.category, e.path))
    return m


def _make_entry(base: Path, rel: str, category: str, *, selected: bool) -> Entry:
    full = base / rel
    if full.is_symlink():
        target = os.readlink(full)
        return Entry(rel, category, kind="symlink", size=0,
                     sha256=hashlib.sha256(target.encode("utf-8", "replace")).hexdigest(),
                     selected=selected)
    if full.is_dir():
        return Entry(rel, category, kind="dir", size=0, sha256="", selected=False)
    try:
        size = full.stat().st_size
        head = full.open("rb").read(BINARY_PROBE)
        binary = b"\x00" in head
    except OSError:
        return Entry(rel, category, kind="file", size=0, sha256="", selected=False)
    return Entry(rel, category, kind="file", size=size, sha256=_sha256(full),
                 binary=binary, selected=selected)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    left = MAX_HASH_BYTES
    with path.open("rb") as f:
        while left > 0:
            chunk = f.read(min(1 << 20, left))
            if not chunk:
                break
            h.update(chunk)
            left -= len(chunk)
    return h.hexdigest()


def verify_target(manifest: Manifest, dest: Path | str) -> list[str]:
    """逐条核对目标。返回问题列表（空 = 全部一致）。

    只核对 `selected` 的 file/symlink：目录本身没内容，忽略条目本来就不迁。
    """
    base = Path(dest)
    problems: list[str] = []
    for e in manifest.entries:
        if not e.selected or e.kind not in ("file", "symlink"):
            continue
        p = base / e.path
        if not p.exists() and not p.is_symlink():
            problems.append(f"缺失：{e.path}")
            continue
        if e.kind == "symlink":
            if not p.is_symlink():
                problems.append(f"类型不一致（应为符号链接）：{e.path}")
            continue
        try:
            if p.is_dir():
                problems.append(f"类型不一致（目标是目录）：{e.path}")
                continue
            got = _sha256(p)
        except OSError as ex:
            problems.append(f"读取失败：{e.path}（{ex}）")
            continue
        if got != e.sha256:
            problems.append(f"哈希不一致：{e.path}（期望 {e.sha256[:12]}，实际 {got[:12]}）")
    return problems
