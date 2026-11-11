"""grep / glob：优先调 rg，没有 rg（Windows 常见）时用纯 Python 回退。结果按修改时间排序。"""
from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
import stat
from pathlib import Path

from ee.proc import popen_kwargs
from ee.tools.base import Tool, ToolContext, ToolError, ToolResult, rel
from ee.tools.truncate import truncate_output_info

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
             ".tox", "dist", "build", ".idea", ".vscode", "target", ".next"}
MAX_FILES = 200
MAX_MATCHES = 300


RG_TIMEOUT = 120.0                  # 秒：rg 不返回时不能把整个回合挂住
_TEXT_MAX_BYTES = 8 * 1024 * 1024   # 回退搜索不把超大文件整份读进内存


def _read_text_any(p: Path) -> str | None:
    """按 UTF-8 → GBK 顺序读；两种都不是就返回 None（按二进制跳过）。

    原来直接用 `errors="strict"` 打开，中文 Windows 上的 GBK 文件（老项目源码、日志、
    导出的 CSV）会在 except 里整份跳过——用户搜不到内容，也没有任何提示。本产品的主
    平台默认编码就是 cp936，这类文件不是边角。
    """
    for enc in ("utf-8", "gbk"):
        try:
            return p.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
        except OSError:
            return None
    return None


async def _rg(args: list[str], cwd: Path) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(shutil.which("rg") or "rg", *args, cwd=str(cwd),
                                                stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                **popen_kwargs())
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=RG_TIMEOUT)
    except asyncio.TimeoutError:
        # rg 扫到一个巨大目录、或者卡在无响应的网络盘上时，不能让整个回合跟着挂住。
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise ToolError(f"rg {RG_TIMEOUT:.0f} 秒没有返回（可能在扫一个很大的目录），已结束它。"
                        "缩小路径或 include 范围再试。")
    except BaseException:          # 回合被中断（取消）：结束 rg，别让它留在后台
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except BaseException:  # noqa: BLE001
                pass
        raise
    if proc.returncode not in (0, 1):
        raise ToolError(f"rg 出错：{err.decode('utf-8', 'replace').strip()[:500]}")
    return proc.returncode, out.decode("utf-8", errors="replace")


def _walk(root: Path):
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
        for f in fns:
            yield Path(dp) / f


def _py_grep(root: Path, rx: re.Pattern, include: str | None) -> dict[str, list[str]]:
    """没有 rg 时的逐文件搜索。只读普通文件（管道 / 设备文件打开会一直阻塞）。"""
    per_file: dict[str, list[str]] = {}
    files = [root] if root.is_file() else _walk(root)
    for p in files:
        if include and not _glob_match(p.name, include):
            continue
        try:
            st = os.stat(p)
            if not stat.S_ISREG(st.st_mode) or st.st_size > _TEXT_MAX_BYTES:
                continue
            text = _read_text_any(p)
            if text is None:          # UTF-8 和 GBK 都读不出：按二进制跳过
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    per_file.setdefault(str(p), []).append(f"{i}: {line.rstrip()[:400]}")
        except OSError:
            continue
    return per_file


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
        files: list[Path] | None = []
        if shutil.which("rg"):
            _, out = await _rg(["--files", "--hidden", "-g", "!.git", "-g", pattern], root)
            files = [root / l for l in out.splitlines() if l]
        else:
            files = None

        def collect() -> list[Path]:     # 遍历 / .eeignore / stat 排序都是同步 IO：放线程里，不卡事件循环
            fs = files if files is not None else \
                [p for p in _walk(root) if _glob_match(str(p.relative_to(root)), pattern)]
            ig = _ignore(ctx)
            if ig is not None:
                fs = [f for f in fs if not ig.ignored(f, False)]
            fs.sort(key=_mtime, reverse=True)
            return fs

        files = await asyncio.to_thread(collect)
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
                raise ToolError(f"正则无效：{e}") from None
            # 纯 Python 回退（Windows 上常见）：逐个读文件是同步 IO，放线程里，不卡事件循环
            per_file = await asyncio.to_thread(_py_grep, root, rx, include)

        def order_files() -> tuple[dict[str, list[str]], list[str]]:
            pf = per_file
            ig = _ignore(ctx)
            if ig is not None:
                pf = {f: v for f, v in pf.items() if not ig.ignored(Path(f) if Path(f).is_absolute() else ctx.cwd / f, False)}
            return pf, sorted(pf, key=lambda f: _mtime(Path(f)), reverse=True)

        per_file, order = await asyncio.to_thread(order_files)
        if not per_file:
            return ToolResult("没有匹配", summary=self.summary(args),
                              meta={"kind": "search", "pattern": pattern, "matches": 0, "files": []})
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
