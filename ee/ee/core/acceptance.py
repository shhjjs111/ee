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
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > MAX_FILE_BYTES:
        # 超大配置不静默跳过：记长度与大小，说明"只比了元数据"
        return f"size:{len(data)}:{hashlib.sha256(data[:4096]).hexdigest()[:16]}"
    return hashlib.sha256(data).hexdigest()[:32]


def _tree_candidates(cwd: Path) -> list[str]:
    """常见子目录里的 conftest / 测试配置（只扫两层，避免全树遍历）。"""
    out: list[str] = []
    for pattern in ("*/conftest.py", "*/pytest.ini", "*/pyproject.toml", "*/package.json"):
        try:
            found = sorted(str(p.relative_to(cwd)).replace("\\", "/") for p in cwd.glob(pattern) if p.is_file())
        except OSError:
            found = []
        out.extend(found[:MAX_TREE_FILES // 4])
    return out


def inputs(cwd: Path | str | None) -> list[str]:
    """这一次验收实际依赖的依据文件（相对路径，已存在且可读的才有条目）。"""
    if not cwd:
        return []
    root = Path(cwd)
    rels = [n for n in ACCEPTANCE_FILES if (root / n).is_file()]
    rels += [r for r in _tree_candidates(root) if r not in rels]
    return rels


def fingerprint(cwd: Path | str | None, command: str) -> dict:
    """验收依据指纹：命令 + 依据文件内容 + 执行目录。

    `files` 是 {相对路径: 内容哈希}；后续比对用它点名"到底哪个文件变了"。
    """
    root = Path(cwd) if cwd else None
    files: dict[str, str] = {}
    for rel in inputs(root) if root is not None else []:
        h = _sha(root / rel) if root is not None else None
        if h is not None:
            files[rel] = h
    return {
        "command": hashlib.sha256((command or "").encode("utf-8")).hexdigest()[:32],
        "files": files,
        "cwd": os.path.normcase(str(root.resolve())) if root is not None else "",
    }


def diff(before: dict, after: dict) -> dict:
    """两份指纹的差异。`changed` 列出内容变了的文件，`added/removed` 单列，`command` 是命令变了。"""
    b_files = dict(before.get("files") or {})
    a_files = dict(after.get("files") or {})
    changed = sorted(k for k in (set(b_files) & set(a_files)) if b_files[k] != a_files[k])
    added = sorted(set(a_files) - set(b_files))
    removed = sorted(set(b_files) - set(a_files))
    return {
        "command": bool(before.get("command") != after.get("command")),
        "cwd": bool(before.get("cwd") != after.get("cwd")),
        "changed": changed,
        "added": added,
        "removed": removed,
    }


def changed(before: dict, after: dict) -> bool:
    d = diff(before, after)
    return bool(d["command"] or d["cwd"] or d["changed"] or d["added"] or d["removed"])


def describe(d: dict) -> str:
    """一句话说明变了什么（进小结，别让用户自己拼）。"""
    parts: list[str] = []
    if d.get("command"):
        parts.append("判据命令")
    if d.get("cwd"):
        parts.append("执行目录")
    for key, label in (("changed", "内容变化"), ("added", "新增"), ("removed", "消失")):
        names = list(d.get(key) or [])[:6]
        if names:
            more = "…" if len(d.get(key) or []) > len(names) else ""
            parts.append(f"{label}：{'、'.join(names)}{more}")
    return "；".join(parts) if parts else ""
