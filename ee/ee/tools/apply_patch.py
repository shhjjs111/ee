"""apply_patch（GPT 家族专用）：*** Begin Patch 格式，参数是 JSON 字符串 {"patch": "..."}。

定位上下文逐级放宽：精确 → 忽略行尾空白 → 忽略首尾空白（同 codex seek_sequence）。
底层复用 fileops 的读写（保留换行/BOM）与快照。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ee.tools.base import Tool, ToolContext, ToolError, ToolResult, rel
from ee.tools.fileops import make_diff, read_text, write_text


@dataclass
class Hunk:
    header: str = ""
    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    eof: bool = False


@dataclass
class FileOp:
    kind: str                     # add | delete | update
    path: str
    move_to: str | None = None
    add_lines: list[str] = field(default_factory=list)
    hunks: list[Hunk] = field(default_factory=list)


def parse_patch(text: str) -> list[FileOp]:
    lines = text.replace("\r\n", "\n").strip("\n").split("\n")
    # 容错：模型有时包了 ``` 或 heredoc
    while lines and (lines[0].startswith("```") or lines[0].strip() in ("apply_patch <<'EOF'", "<<'EOF'")):
        lines = lines[1:]
    while lines and (lines[-1].startswith("```") or lines[-1].strip() == "EOF"):
        lines = lines[:-1]
    if not lines or lines[0].strip() != "*** Begin Patch":
        raise ToolError("补丁必须以 '*** Begin Patch' 开头")
    if lines[-1].strip() != "*** End Patch":
        raise ToolError("补丁必须以 '*** End Patch' 结尾")
    ops: list[FileOp] = []
    cur: FileOp | None = None
    hunk: Hunk | None = None
    for raw in lines[1:-1]:
        if raw.startswith("*** Add File: "):
            cur = FileOp("add", raw[len("*** Add File: "):].strip()); ops.append(cur); hunk = None
        elif raw.startswith("*** Delete File: "):
            cur = FileOp("delete", raw[len("*** Delete File: "):].strip()); ops.append(cur); hunk = None
        elif raw.startswith("*** Update File: "):
            cur = FileOp("update", raw[len("*** Update File: "):].strip()); ops.append(cur); hunk = None
        elif raw.startswith("*** Move to: "):
            if not cur or cur.kind != "update":
                raise ToolError("'*** Move to:' 只能跟在 Update File 后面")
            cur.move_to = raw[len("*** Move to: "):].strip()
        elif raw.strip() == "*** End of File":
            if hunk:
                hunk.eof = True
        elif cur is None:
            if raw.strip():
                raise ToolError(f"无法识别的行（缺少文件头）：{raw[:80]}")
        elif cur.kind == "add":
            if not raw.startswith("+"):
                raise ToolError(f"Add File 的每一行都必须以 + 开头：{raw[:80]}")
            cur.add_lines.append(raw[1:])
        elif cur.kind == "delete":
            if raw.strip():
                raise ToolError("Delete File 后面不应该有内容")
        else:
            if raw.startswith("@@"):
                hunk = Hunk(header=raw[2:].strip()); cur.hunks.append(hunk)
                continue
            if hunk is None:
                hunk = Hunk(); cur.hunks.append(hunk)
            if raw.startswith("+"):
                hunk.new.append(raw[1:])
            elif raw.startswith("-"):
                hunk.old.append(raw[1:])
            elif raw.startswith(" "):
                hunk.old.append(raw[1:]); hunk.new.append(raw[1:])
            elif raw == "":
                hunk.old.append(""); hunk.new.append("")
            else:
                raise ToolError(f"补丁行必须以 ' '、'+'、'-' 或 '@@' 开头：{raw[:80]}")
    for op in ops:
        if op.kind == "update" and not op.hunks and not op.move_to:
            raise ToolError(f"Update File {op.path} 没有任何改动块")
    if not ops:
        raise ToolError("补丁里没有任何文件操作")
    return ops


def _lead(line: str) -> str:
    return line[:len(line) - len(line.lstrip(" \t"))]


def _base_indent(lines: list[str]) -> str:
    """非空行里缩进最短的那行的前导空白。"""
    ind = [_lead(l) for l in lines if l.strip()]
    return min(ind, key=len) if ind else ""


def _reindent_new(old: list[str], new: list[str], o: str, r: str) -> list[str]:
    """把 new 的每一行从「以 old 的基准缩进 o 为基准」平移成「以原文的基准缩进 r 为基准」。

    只做整块统一平移（与 edit 的 indent 策略同一条规则）：old 里每个非空行都必须以 o 开头，
    这样才能确定 new 该加多少空白。做不到就报错——宁可报错，不能误改缩进。
    """
    if o == r:
        return new
    chars = set(o + r)
    out: list[str] = []
    for line in new:
        if not line.strip():
            out.append("")
            continue
        if line.startswith(o):
            out.append(r + line[len(o):])
            continue
        lead = _lead(line)
        if len(chars) == 1 and set(lead) <= chars:
            k = len(lead) + len(r) - len(o)
            if k >= 0:
                out.append(next(iter(chars)) * k + line[len(lead):])
                continue
        raise ToolError("补丁的 '+' 行缩进比 '-' 行的基准缩进还浅，无法按原文缩进对齐；"
                        "请照原文缩进重写补丁（或先 read 再逐字复制）。")
    return out


def _indent_hits(lines: list[str], pattern: list[str], rng: list[int]) -> list[int]:
    """整体缩进平移：block 每行都是「原文基准缩进 R + 余下部分」对「old 基准缩进 O + 同样的余下部分」。

    与 edit 的 indent 策略同一条规则——只接受整块统一平移（相对缩进与缩进字符都和原文一致），
    这样 _reindent_new 才能把 new 按同一偏移安全对齐回原文。
    """
    if not pattern or not any(l.strip() for l in pattern):
        return []
    o = _base_indent(pattern)
    if any(l.strip() and not l.startswith(o) for l in pattern):
        return []
    rest = [l[len(o):].rstrip() if l.strip() else "" for l in pattern]
    n = len(pattern)
    out: list[int] = []
    for i in rng:
        win = lines[i:i + n]
        if len(win) != n:
            continue
        if any(bool(w.strip()) != bool(f) for w, f in zip(win, rest)):
            continue                      # 空行/非空行的位置必须对得上
        r = _base_indent(win)
        if all((not w.strip()) or (w.startswith(r) and w[len(r):].rstrip() == f)
               for w, f in zip(win, rest)):
            out.append(i)
    return out


def seek_hits(lines: list[str], pattern: list[str], start: int, eof: bool = False) -> tuple[list[int], str]:
    """所有命中位置 + 用的匹配级别。

    **不再有 strip 级**（首尾空白都不看会把不相关的行当成命中，而且会吞掉缩进）。
    三级都是安全的：精确 → 忽略行尾空白 → 整体缩进平移。
    多处命中交给调用方报歧义——静默取第一处就是误改（重复代码很常见）。
    """
    if not pattern:
        return [start], "empty"
    if len(pattern) > len(lines):
        return [], ""
    rng = list(range(len(lines) - len(pattern), start - 1, -1)) if eof else list(range(start, len(lines) - len(pattern) + 1))
    for name, norm in (("exact", lambda s: s), ("rstrip", str.rstrip)):
        pn = [norm(p) for p in pattern]
        hits = [i for i in rng if all(norm(lines[i + j]) == pn[j] for j in range(len(pattern)))]
        if hits:
            return hits, name
    hits = _indent_hits(lines, pattern, rng)
    if hits:
        return hits, "indent"
    return [], ""


def seek_all(lines: list[str], pattern: list[str], start: int, eof: bool = False) -> tuple[int, int, str]:
    """兼容旧调用：(第一处位置, 命中次数, 匹配级别)。"""
    hits, how = seek_hits(lines, pattern, start, eof)
    return (hits[0] if hits else -1), len(hits), how


def _ambiguous(what: str, hits: list[int]) -> str:
    """多处命中：列出全部行号，让模型给更长的上下文重试。"""
    where = "、".join(str(i + 1) for i in hits[:10])
    more = f"（共 {len(hits)} 处，只列前 10 处）" if len(hits) > 10 else ""
    return (f"{what}有多处命中（第 {where} 行{more}），无法确定要改哪一处。"
            "请把上下文写长一点（包含上下的 ' ' 行）重试，让它只匹配一处。")


def seek(lines: list[str], pattern: list[str], start: int, eof: bool = False, what: str = "") -> int:
    """唯一命中的位置；找不到或多处命中都报错。"""
    hits, _ = seek_hits(lines, pattern, start, eof)
    if not hits:
        raise ToolError(f"找不到要替换的上下文：\n" + "\n".join(pattern[:8]) +
                        "\n请重新读取文件，确保 ' ' 和 '-' 行与原文一致。")
    if len(hits) > 1:
        raise ToolError(_ambiguous(what or "补丁上下文", hits))
    return hits[0]


def apply_hunks(text: str, hunks: list[Hunk], path: str) -> str:
    trailing_nl = text.endswith("\n") or text == ""
    lines = text.split("\n")
    if trailing_nl and lines and lines[-1] == "":
        lines = lines[:-1]
    cursor = 0
    for h in hunks:
        if h.header:
            # @@ 定位行同样要唯一；多处在同名的 def/class 里很常见
            idx = seek(lines, [h.header], cursor, what=f"{path}：@@ 定位行 '{h.header}' ")
            cursor = idx + 1
        old, new = list(h.old), list(h.new)
        if not old:
            pos = len(lines) if h.eof or not h.header else cursor
            lines[pos:pos] = new
            cursor = pos + len(new)
            continue
        hits, _ = seek_hits(lines, old, cursor, h.eof)
        if not hits and old and old[-1] == "":
            # 补丁块末尾的空行常是分隔符，不是要删的行
            old, new = old[:-1], (new[:-1] if new and new[-1] == "" else new)
            hits, _ = seek_hits(lines, old, cursor, h.eof)
        if not hits:
            raise ToolError(f"{path}：找不到要替换的上下文：\n" + "\n".join(old[:8]) +
                            "\n请重新读取文件，确保 ' ' 和 '-' 行与原文一致。")
        if len(hits) > 1:
            raise ToolError(_ambiguous(f"{path}：", hits))
        idx = hits[0]
        # 缩进按原文对齐：old 的基准缩进 → 原文窗口的基准缩进（不再把 new 原样写入）
        win = lines[idx: idx + len(old)]
        o, r = _base_indent(old), _base_indent(win)
        new = _reindent_new(old, new, o, r)
        lines[idx: idx + len(old)] = new
        cursor = idx + len(new)
    return "\n".join(lines) + ("\n" if trailing_nl else "")


class ApplyPatchTool(Tool):
    name = "apply_patch"
    parameters = {
        "type": "object",
        "properties": {"patch": {"type": "string", "description": "完整补丁文本，从 *** Begin Patch 到 *** End Patch"}},
        "required": ["patch"],
    }

    def summary(self, args: dict) -> str:
        return "Edited (patch)"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        patch = args.get("patch") or args.get("input") or ""
        ops = parse_patch(patch)
        # 先全部演算，任何一处失败都不落盘
        plan: list[tuple[str, Path, Path | None, str, str, object]] = []
        for op in ops:
            p = ctx.resolve(op.path)
            if op.kind == "add":
                if p.exists():
                    raise ToolError(f"Add File 目标已存在：{p}（要改它请用 Update File）")
                content = "\n".join(op.add_lines) + "\n"
                plan.append(("Added", p, None, "", content, read_text(p)))
            elif op.kind == "delete":
                if not p.exists():
                    raise ToolError(f"Delete File 目标不存在：{p}")
                tf = read_text(p)
                plan.append(("Deleted", p, None, tf.text, "", tf))
            else:
                if not p.exists():
                    raise ToolError(f"Update File 目标不存在：{p}")
                tf = read_text(p)
                new = apply_hunks(tf.text, op.hunks, op.path) if op.hunks else tf.text
                dest = ctx.resolve(op.move_to) if op.move_to else None
                plan.append(("Edited", p, dest, tf.text, new, tf))
        diffs, summaries, meta_files = [], [], []
        for verb, p, dest, old, new, tf in plan:
            ctx.state.snapshot(p)
            name = rel(dest or p, ctx.cwd)
            if verb == "Deleted":
                p.unlink()
            else:
                target = dest or p
                if dest:
                    ctx.state.snapshot(dest)
                write_text(target, tf, new)
                if dest and dest != p:
                    p.unlink()
                ctx.state.mark_read(target)
            d, a, r = make_diff(old, new, name)
            diffs.append(d)
            summaries.append(f"{verb} {name} (+{a} -{r})")
            meta_files.append({"verb": verb, "path": name, "added": a, "removed": r})
        return ToolResult("补丁已应用：\n" + "\n".join(summaries), summary="; ".join(summaries),
                          diff="\n".join(diffs), meta={"kind": "edit", "files": meta_files,
                                                       "verb": meta_files[0]["verb"], "path": meta_files[0]["path"],
                                                       "added": sum(f["added"] for f in meta_files),
                                                       "removed": sum(f["removed"] for f in meta_files)})
