"""验收依据的指纹与变化检测（2026-11 交付验证，第一批）。

**为什么需要它**：全力以赴的判据命令是用户写的，但"验收依据"不只包含命令——
`conftest.py`、`pytest.ini`、`package.json` 的 test 脚本、`tox.ini` 这些文件同样决定
什么算通过。执行中的模型可以把断言改松、把 `-k` 选择范围改小，让同一条命令变绿。
只看"命令跑通了"会得到假成功。

**这一版的边界（不要当成完整防护）**：只做**固定清单**的字节指纹 + 命令指纹。
脚本里动态加载的输入、依赖版本、环境变量都不在里面——那些属于第三批的逐用例结果与
独立性检查。检测到变化就**阻止自动 met**，把"变了什么、缺哪项证据"如实写进小结，
而不是假装知道了新依据是否仍然满足需求。
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

# 固定清单：不是穷举，而是覆盖最常见的"验收口径"载体。
# 顺序固定，便于指纹与展示稳定；缺失的文件不参与比对（没有就是没有）。
ACCEPTANCE_FILES = (
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "pyproject.toml",
    "conftest.py",
    "package.json",
    "vitest.config.ts",
    "vitest.config.js",
    "vitest.config.mts",
    "jest.config.js",
    "jest.config.ts",
    "jest.config.cjs",
    "vite.config.ts",
    "vite.config.js",
    "makefile",
    "Makefile",
    "go.mod",
    "go.sum",
)
# 子目录里的 conftest / 测试配置也算依据；上限防止在巨型仓库里扫整棵树。
MAX_TREE_FILES = 40
MAX_FILE_BYTES = 1_000_000


def _sha(path: Path) -> str | None:
    """完整内容哈希；读不到或超过上限返回 None（由调用方记 unverified，不能假装完整）。"""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > MAX_FILE_BYTES:
        return None
    return hashlib.sha256(data).hexdigest()[:32]


_TREE_NAMES = {"conftest.py", "pytest.ini", "pyproject.toml", "package.json",
               "setup.cfg", "tox.ini"}
_TREE_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
              "target", ".ee", ".cursor", ".claude", "vendor", "release", "test-results"}


def _tree_candidates_ex(cwd: Path) -> tuple[list[str], bool]:
    """有界递归找嵌套 conftest/测试配置；真实路径去重、不越 root、超时/超量不完整。"""
    out: list[str] = []
    complete = True
    try:
        root = Path(cwd).resolve()
        root_real = Path(os.path.realpath(cwd))
    except OSError:
        return [], False
    stack = [(root, 0)]
    seen: set[str] = set()
    entries = 0
    deadline = time.monotonic() + 1.5
    while stack:
        if time.monotonic() > deadline or entries > 50_000:
            complete = False
            break
        d, depth = stack.pop()
        try:
            real = Path(os.path.realpath(d))
            key = str(real).lower()
            if key in seen:
                continue
            if not real.is_relative_to(root_real):
                continue
            seen.add(key)
            children = list(d.iterdir())
        except (OSError, ValueError):
            complete = False
            continue
        for child in children:
            entries += 1
            if entries > 50_000 or len(out) >= MAX_TREE_FILES:
                complete = False
                break
            try:
                if child.is_dir():
                    if depth < 3 and child.name not in _TREE_SKIP and not child.name.startswith("."):
                        child_real = Path(os.path.realpath(child))
                        if child_real.is_relative_to(root_real):
                            stack.append((child, depth + 1))
                    continue
                if not child.is_file() or child.name not in _TREE_NAMES:
                    continue
                child_real = Path(os.path.realpath(child))
                if not child_real.is_relative_to(root_real):
                    continue
            except (OSError, ValueError):
                complete = False
                continue
            try:
                out.append(child.relative_to(root).as_posix())
            except ValueError:
                continue
    if stack:
        complete = False
    return sorted(dict.fromkeys(out)), complete


def _tree_candidates(cwd: Path) -> list[str]:
    return _tree_candidates_ex(cwd)[0]


def inputs_ex(cwd: Path | str | None) -> tuple[list[str], bool]:
    """这一次验收实际依赖的依据文件，以及目录扫描是否完整。"""
    if not cwd:
        return [], True
    root = Path(cwd)
    rels = [n for n in ACCEPTANCE_FILES if (root / n).is_file()]
    tree, complete = _tree_candidates_ex(root)
    rels += [r for r in tree if r not in rels]
    return rels, complete


def inputs(cwd: Path | str | None) -> list[str]:
    """这一次验收实际依赖的依据文件（相对路径，已存在且可读的才有条目）。"""
    return inputs_ex(cwd)[0]


def fingerprint(cwd: Path | str | None, command: str, extra_files: tuple[str, ...] = (),
                protected_tests: tuple[str, ...] = (), *,
                protected_tests_complete: bool = True) -> dict:
    """验收依据指纹：命令 + 依据文件内容 + 既有验收测试 + 执行目录。

    `files` 是 {相对路径: 内容哈希}，用于配置/显式验收文件；
    `tests` 是既有测试文件的单独指纹。两者分开，是因为**新增**测试通常是合法的，
    但改写/删除既有测试会改变验收口径，必须让交付闸门看见。
    `unverified_files` / `tests_unverified` 是读不到或超过哈希上限的文件；
    存在它们时 `tests_complete=False`，交付闸门必须按「无法完整验证」处理。
    """
    root = Path(cwd) if cwd else None

    def _hash_many(paths):
        out: dict[str, str] = {}
        unverified: list[str] = []
        for rel in dict.fromkeys(paths) if root is not None else ():
            path = root / rel
            try:
                inside = path.resolve().is_relative_to(root.resolve())
            except (OSError, RuntimeError):
                inside = False
            if not inside:
                continue
            h = _sha(path)
            if h is not None:
                out[rel] = h
            elif path.is_file():
                unverified.append(rel)
        return out, unverified

    base_files, base_complete = inputs_ex(root)
    files, unverified_files = _hash_many([*base_files, *extra_files])
    out = {
        "command": hashlib.sha256((command or "").encode("utf-8")).hexdigest()[:32],
        "files": files,
        "cwd": os.path.normcase(str(root.resolve())) if root is not None else "",
    }
    if not base_complete:
        out["files_complete"] = False
    if unverified_files:
        out["unverified_files"] = unverified_files
    if protected_tests or not protected_tests_complete:
        tests, tests_unverified = _hash_many(protected_tests)
        out["tests"] = tests
        if tests_unverified:
            out["tests_unverified"] = tests_unverified
        out["tests_complete"] = bool(protected_tests_complete and not tests_unverified)
    return out


def diff(before: dict, after: dict) -> dict:
    """两份指纹的差异。`changed` 列出内容变了的文件，`added/removed` 单列，`command` 是命令变了。

    `tests_changed/tests_removed` 是既有验收测试被改写/删除；`tests_added` 只作信息，
    不参与 `changed()`——正常新增测试不应阻断交付。`unverified_*` / `tests_complete=False`
    表示无法完整验证，`changed()` 必须把它当成变化（宁可不自动 met）。
    """
    b_files = dict(before.get("files") or {})
    a_files = dict(after.get("files") or {})
    changed = sorted(k for k in (set(b_files) & set(a_files)) if b_files[k] != a_files[k])
    added = sorted(set(a_files) - set(b_files))
    removed = sorted(set(b_files) - set(a_files))
    b_tests = dict(before.get("tests") or {})
    a_tests = dict(after.get("tests") or {})
    tests_changed = sorted(k for k in (set(b_tests) & set(a_tests)) if b_tests[k] != a_tests[k])
    tests_removed = sorted(set(b_tests) - set(a_tests))
    tests_added = sorted(set(a_tests) - set(b_tests))
    return {
        "command": bool(before.get("command") != after.get("command")),
        "cwd": bool(before.get("cwd") != after.get("cwd")),
        "changed": changed,
        "added": added,
        "removed": removed,
        "tests_changed": tests_changed,
        "tests_removed": tests_removed,
        "tests_added": tests_added,
        "unverified_files": sorted(set(before.get("unverified_files") or [])
                                   | set(after.get("unverified_files") or [])),
        "files_incomplete": bool(before.get("files_complete") is False
                                 or after.get("files_complete") is False),
        "tests_unverified": sorted(set(before.get("tests_unverified") or [])
                                   | set(after.get("tests_unverified") or [])),
        "tests_incomplete": bool(before.get("tests_complete") is False
                                 or after.get("tests_complete") is False),
    }


def changed(before: dict, after: dict) -> bool:
    d = diff(before, after)
    return bool(d["command"] or d["cwd"] or d["changed"] or d["added"] or d["removed"]
                or d["tests_changed"] or d["tests_removed"] or d["unverified_files"]
                or d["files_incomplete"] or d["tests_unverified"] or d["tests_incomplete"])


def protected_tests_intact(before: dict, after: dict) -> bool:
    """既有测试没有改写/删除；新增测试不算破坏。无法完整哈希时不能宣称完整。"""
    d = diff(before, after)
    return not (d["tests_changed"] or d["tests_removed"] or d["tests_unverified"]
                or d["tests_incomplete"])


def baseline_problem(base: dict, *, validation_files: tuple[str, ...] = (),
                     protected_tests: tuple[str, ...] = ()) -> str:
    """候选自己的验收基线能不能用来做逐字节比对；能用返回 ""，否则返回原因。

    为什么需要它：候选在 Windows 上被 git 检出时，字节可能与主工作区不同
    （主工作区 CRLF / git blob 与 checkout 是 LF，`git status` 仍然干净）。
    所以候选的比对基线必须在**候选自己的目录里、模型还没跑时**采集，
    不能拿主工作区的 `_accept0` 直接比，否则未修改的测试会被误判成篡改。

    但"采集到了"不等于"采集完整"：文件缺失、读不到、超过哈希上限、扫描被截断时
    基线不完整。这时宁可不合格，也不能把"没看见"当成"没改"。
    """
    if not base:
        return "候选验收基线为空（没有采集到指纹）"
    if base.get("files_complete") is False:
        return "候选的验收配置文件扫描不完整"
    if base.get("tests_complete") is False:
        return "候选的既有测试清单不完整（扫描截断或文件读不到）"
    missing_files = [f for f in validation_files if f not in (base.get("files") or {})]
    if missing_files:
        return "验收文件在候选里缺失或不可读：" + "、".join(missing_files[:5])
    missing_tests = [t for t in protected_tests if t not in (base.get("tests") or {})]
    if missing_tests:
        return "既有测试在候选里缺失或不可读：" + "、".join(missing_tests[:5])
    unverified = list(base.get("unverified_files") or []) + list(base.get("tests_unverified") or [])
    if unverified:
        return "候选里这些文件无法完整哈希：" + "、".join(unverified[:5])
    return ""


def describe(d: dict) -> str:
    """一句话说明变了什么（进小结，别让用户自己拼）。"""
    parts: list[str] = []
    if d.get("command"):
        parts.append("判据命令")
    if d.get("cwd"):
        parts.append("执行目录")
    if d.get("files_incomplete"):
        parts.append("验收配置文件扫描不完整")
    if d.get("tests_incomplete"):
        parts.append("既有测试清单不完整（扫描截断或文件过大/不可读）")
    for key, label in (("changed", "内容变化"), ("added", "新增"), ("removed", "消失"),
                       ("tests_changed", "验收测试变化"), ("tests_removed", "验收测试消失"),
                       ("unverified_files", "无法完整哈希"), ("tests_unverified", "验收测试无法完整哈希")):
        names = list(d.get(key) or [])[:6]
        if names:
            more = "…" if len(d.get(key) or []) > len(names) else ""
            parts.append(f"{label}：{'、'.join(names)}{more}")
    return "；".join(parts) if parts else ""
