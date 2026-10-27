"""grep / glob：优先调 rg，没有 rg（Windows 常见）时用纯 Python 回退。结果按修改时间排序。"""
from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
from pathlib import Path

from ee.proc import popen_kwargs
from ee.tools.base import Tool, ToolContext, ToolError, ToolResult, rel
from ee.tools.truncate import truncate_output_info

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
             ".tox", "dist", "build", ".idea", ".vscode", "target", ".next"}
MAX_FILES = 200
MAX_MATCHES = 300


async def _rg(args: list[str], cwd: Path) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(shutil.which("rg") or "rg", *args, cwd=str(cwd),
                                                stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                **popen_kwargs())
    out, err = await proc.communicate()
    if proc.returncode not in (0, 1):
        raise ToolError(f"rg 出错：{err.decode('utf-8', 'replace').strip()[:500]}")
    return proc.returncode, out.decode("utf-8", errors="replace")


def _walk(root: Path):
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
        for f in fns:
            yield Path(dp) / f


def _glob_match(relpath: str, pattern: str) -> bool:
    relpath = relpath.replace("\\", "/")
    pats = _expand_braces(pattern.replace("\\", "/"))
    for p in pats:
        if "/" not in p:
            if fnmatch.fnmatch(relpath.rsplit("/", 1)[-1], p):
                return True
        else:
            if fnmatch.fnmatch(relpath, p) or (p.startswith("**/") and fnmatch.fnmatch(relpath, p[3:])):
                return True
    return False


def _expand_braces(p: str) -> list[str]:
    m = re.search(r"\{([^{}]*)\}", p)
    if not m:
        return [p]
    return [x for opt in m.group(1).split(",") for x in _expand_braces(p[:m.start()] + opt + p[m.end():])]


def _ignore(ctx: ToolContext):
    """.eeignore（D4-8）：没有规则时返回 None。"""
    from ee.core.eeignore import for_dir
    try:
        ig = for_dir(ctx.cwd)
    except Exception:  # noqa: BLE001
        return None
    return None if ig.empty else ig


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


class GlobTool(Tool):
    name = "glob"
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "glob 模式"},
            "path": {"type": "string", "description": "搜索根目录，默认工作目录"},
        },
        "required": ["pattern"],
    }

    def summary(self, args: dict) -> str:
        return f"List {args.get('pattern', '')}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        pattern = args.get("pattern") or ""
        if not pattern:
            raise ToolError("pattern 为空")
        root = ctx.resolve(args.get("path") or ".")
        if not root.is_dir():
            raise ToolError(f"目录不存在：{root}")
        files: list[Path] = []
        if shutil.which("rg"):
            _, out = await _rg(["--files", "--hidden", "-g", "!.git", "-g", pattern], root)
            files = [root / l for l in out.splitlines() if l]
        else:
            files = [p for p in _walk(root) if _glob_match(str(p.relative_to(root)), pattern)]
        ig = _ignore(ctx)
        if ig is not None:
            files = [f for f in files if not ig.ignored(f, False)]
        files.sort(key=_mtime, reverse=True)
        total = len(files)
        shown = files[:MAX_FILES]
        lines = [rel(p, ctx.cwd) for p in shown]
        if not lines:
            return ToolResult("没有匹配的文件", summary=f"List {pattern}", meta={"kind": "list", "files": [], "total": 0})
        if total > MAX_FILES:
            lines.append(f"\n（共 {total} 个，只显示最近修改的 {MAX_FILES} 个；请缩小模式或路径）")
        return ToolResult("\n".join(lines), summary=f"List {pattern}",
                          meta={"kind": "list", "path": rel(root, ctx.cwd), "files": [rel(p, ctx.cwd) for p in shown[:200]],
                                "total": total})


class GrepTool(Tool):
    name = "grep"
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则（rg 语法）"},
            "path": {"type": "string", "description": "文件或目录，默认工作目录"},
            "include": {"type": "string", "description": "glob 过滤，如 *.py"},
            "output_mode": {"type": "string", "enum": ["content", "files", "count"],
                            "description": "默认 content"},
            "ignore_case": {"type": "boolean"},
        },
        "required": ["pattern"],
    }

    def summary(self, args: dict) -> str:
        p = args.get("path")
        return f"Search {args.get('pattern', '')}" + (f" in {p}" if p else "")

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        pattern = args.get("pattern") or ""
        if not pattern:
            raise ToolError("pattern 为空")
        root = ctx.resolve(args.get("path") or ".")
        if not root.exists():
            raise ToolError(f"路径不存在：{root}")
        mode = args.get("output_mode") or "content"
        include = args.get("include")
        icase = bool(args.get("ignore_case"))
        per_file: dict[str, list[str]] = {}
        if shutil.which("rg"):
            a = ["--line-number", "--no-heading", "--color", "never", "--hidden", "-g", "!.git",
                 "--max-columns", "400", "--max-columns-preview"]
            if icase:
                a.append("-i")
            if include:
                a += ["-g", include]
            a += ["-e", pattern, "--", str(root)]
            _, out = await _rg(a, ctx.cwd)
            for line in out.splitlines():
                m = re.match(r"^(.*?):(\d+):(.*)$", line) if not root.is_file() else re.match(r"^()(\d+):(.*)$", line)
                if not m:
                    continue
                f = m.group(1) or str(root)
                per_file.setdefault(f, []).append(f"{m.group(2)}: {m.group(3)}")
        else:
            try:
                rx = re.compile(pattern, re.I if icase else 0)
            except re.error as e:
                raise ToolError(f"正则无效：{e}")
            files = [root] if root.is_file() else _walk(root)
            for p in files:
                if include and not _glob_match(p.name, include):
                    continue
                try:
                    with open(p, encoding="utf-8", errors="strict") as fh:
                        for i, line in enumerate(fh, 1):
                            if rx.search(line):
                                per_file.setdefault(str(p), []).append(f"{i}: {line.rstrip()[:400]}")
                except (UnicodeDecodeError, OSError):
                    continue
        ig = _ignore(ctx)
        if ig is not None:
            per_file = {f: v for f, v in per_file.items() if not ig.ignored(Path(f) if Path(f).is_absolute() else ctx.cwd / f, False)}
        if not per_file:
            return ToolResult("没有匹配", summary=self.summary(args),
                              meta={"kind": "search", "pattern": pattern, "matches": 0, "files": []})
        order = sorted(per_file, key=lambda f: _mtime(Path(f)), reverse=True)
        out_lines: list[str] = []
        total = sum(len(v) for v in per_file.values())
        if mode == "files":
            out_lines = [rel(f, ctx.cwd) for f in order[:MAX_FILES]]
        elif mode == "count":
            out_lines = [f"{rel(f, ctx.cwd)}: {len(per_file[f])}" for f in order[:MAX_FILES]]
        else:
            n = 0
            for f in order:
                if n >= MAX_MATCHES:
                    break
                out_lines.append(rel(f, ctx.cwd))
                for m in per_file[f]:
                    if n >= MAX_MATCHES:
                        break
                    out_lines.append("  " + m)
                    n += 1
        if (mode == "content" and total > MAX_MATCHES) or len(order) > MAX_FILES:
            out_lines.append(f"\n（共 {total} 处匹配 / {len(order)} 个文件，已截断；请缩小 pattern、path 或 include）")
        text, trunc = truncate_output_info("\n".join(out_lines), ctx.output_dir, "grep")
        return ToolResult(text, summary=self.summary(args),
                          meta={"kind": "search", "pattern": pattern, "matches": total,
                                "files": [rel(f, ctx.cwd) for f in order[:200]],
                                **({"truncated": trunc} if trunc else {})})
