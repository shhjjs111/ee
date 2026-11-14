"""交付状态的预期构造与内容比对（2026-11 交付验证，第一批）。

**要解决的问题**：候选补丁写回用户工作区后，"写回成功"不等于"用户拿到的就是这份补丁"。
`git apply` 返回 0 只说明它能应用；写回期间外部编辑器可能改了同一个文件的别处，
或者只写了一半。只比"哪些路径变了"不够——同一路径被改了内容，路径集合依然相等。

**做法**：把写回前的快照（`pre_tree`）导出到临时目录，用**同一份补丁字节**应用一次，
得到"预期交付状态"；再与真实工作区的每个补丁路径逐字节比对。写回前就存在的用户独立
修改会自然进入预期状态（它们本来就在 `pre_tree` 里），写回过程中新增的修改则会被发现。

**这一版明确不承诺的事**：
- 符号链接目标与可执行位：只在 `unverified_mode` 里点名，不参与判定（Windows 上无法可靠比对）。
- 快照未覆盖的路径（被 .gitignore 排除、超过体积上限）：报 `uncovered`，**不**当成空文件。
- "内容比对"与"复验启动"之间仍有极小窗口，见 `threads._delivery_gate` 里的二次 capture。
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from ee.core.snapshot import SnapshotError

ABSENT = "absent"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


def _read(p: Path) -> bytes | None:
    try:
        return p.read_bytes()
    except OSError:
        return None


@dataclass
class Expected:
    """预期交付状态。`files` 是 {相对路径: 内容哈希 或 ABSENT}。"""
    ok: bool = False
    reason: str = ""
    files: dict[str, str] = field(default_factory=dict)
    uncovered: list[str] = field(default_factory=list)
    unverified_mode: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason, "uncovered": list(self.uncovered),
                "unverified_mode": list(self.unverified_mode)}


def _tree_entries(disk, tree: str, paths: list[str]) -> dict[str, str]:
    """{相对路径: mode}——只关心补丁涉及的路径，避免在巨型仓库里列全树。"""
    if not paths:
        return {}
    out = disk._run("ls-tree", "-r", "-z", tree, "--", *paths, timeout=60)  # noqa: SLF001 影子仓库内部接口
    entries: dict[str, str] = {}
    for rec in out.decode("utf-8", "surrogateescape").split("\0"):
        meta, _, rel = rec.partition("\t")
        bits = meta.split()
        if rel and len(bits) == 3:
            entries[rel] = bits[0]
    return entries


def build_expected(disk, tree: str | None, patch: bytes, paths: list[str],
                   worktree: Path) -> Expected:
    """用快照 + 补丁算出预期交付状态。

    `paths` 必须来自 `git diff --name-only`（含重命名的两侧），而不是从补丁文本正则解析——
    带空格的路径在 `diff --git` 行里会被引号包起来，自己解析迟早出错。
    """
    if not tree:
        return Expected(ok=False, reason="拿不到写回前的快照，无法构造预期状态")
    if not patch.strip():
        return Expected(ok=False, reason="补丁为空")
    try:
        entries = _tree_entries(disk, tree, paths)
    except SnapshotError as e:
        return Expected(ok=False, reason=f"读取快照失败：{e}")

    # 补丁路径分三类：快照里有（可比对）、快照里没有但工作区有（未覆盖）、两边都没有（补丁新增）
    covered = [p for p in paths if p in entries]
    not_in_tree = [p for p in paths if p not in entries]
    # 未覆盖：被 .gitignore 排除或超过体积上限。它的"写回前内容"我们不知道，不能当成空文件。
    uncovered = [p for p in not_in_tree if (worktree / p).exists()]

    unverified_mode = [p for p in covered if entries.get(p) in ("120000", "100755")]

    with tempfile.TemporaryDirectory(prefix="ee-deliver-") as tmp:
        base = Path(tmp) / "base"
        base.mkdir()
        if covered:
            zpath = Path(tmp) / "pre.zip"
            try:
                disk._run("archive", "--format=zip", "-o", str(zpath), tree, "--", *covered,  # noqa: SLF001
                          timeout=120)
                with zipfile.ZipFile(zpath) as zf:
                    zf.extractall(base)
            except (SnapshotError, OSError, zipfile.BadZipFile) as e:
                return Expected(ok=False, reason=f"导出写回前快照失败：{e}", uncovered=uncovered)

            # GPT 的第一条要求：先确认快照内容与工作区**应用前**的字节一致，否则预期状态无意义
            for p in covered:
                got = _read(worktree / p)
                want = _read(base / p)
                if got is None or want is None:
                    return Expected(ok=False, reason=f"写回前状态与快照不一致：{p}（读不到内容）",
                                    uncovered=uncovered)
                if digest(got) != digest(want):
                    return Expected(ok=False,
                                    reason=f"写回前状态与快照不一致：{p}（写回期间被外部修改？）",
                                    uncovered=uncovered)

        pf = Path(tmp) / "patch.bin"
        pf.write_bytes(patch)
        from ee.proc import run_capture
        from ee.core.snapshot import _git  # 同一个 git 可执行文件

        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
        code, _out, err = run_capture([_git() or "git", "apply", "--binary", "--whitespace=nowarn",
                                       str(pf)], cwd=base, timeout=120, env=env)
        if code != 0:
            return Expected(ok=False,
                            reason="在既有快照上应用补丁失败，无法构造预期状态："
                                   + (err.decode("utf-8", "replace")[:300] or f"exit {code}"),
                            uncovered=uncovered)

        files: dict[str, str] = {}
        for p in paths:
            data = _read(base / p)
            files[p] = ABSENT if data is None else digest(data)
    return Expected(ok=True, files=files, uncovered=uncovered,
                    unverified_mode=unverified_mode)


def compare(expected: Expected, worktree: Path, paths: list[str]) -> list[dict]:
    """把真实工作区与预期状态逐路径比对，返回不一致项（空 = 一致）。"""
    if not expected.ok:
        return []
    out: list[dict] = []
    for p in paths:
        want = expected.files.get(p, ABSENT)
        data = _read(worktree / p)
        got = ABSENT if data is None else digest(data)
        if got != want:
            kind = "exists" if (got == ABSENT or want == ABSENT) else "content"
            out.append({"path": p, "kind": kind, "expected": want, "actual": got})
    return out


def unexpected_changes(disk, before: str | None, after: str | None, allowed: set[str]) -> list[str]:
    """`before→after` 里补丁路径之外的改动（写回期间被别的程序改过）。"""
    if not before or not after or before == after:
        return []
    try:
        changed = disk.changed(before, after)
    except SnapshotError:
        return []
    return [p for p in changed if p not in allowed]
