"""apply_patch（GPT 家族专用）：Codex 的 *** Begin Patch 格式，参数是 JSON 字符串 {"patch": "..."}。

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


def seek(lines: list[str], pattern: list[str], start: int, eof: bool = False) -> int:
    if not pattern:
        return start
    if len(pattern) > len(lines):
        return -1
    norms = [lambda s: s, str.rstrip, str.strip]
    rng = range(len(lines) - len(pattern), start - 1, -1) if eof else range(start, len(lines) - len(pattern) + 1)
    for norm in norms:
        pn = [norm(p) for p in pattern]
        for i in (list(rng) if eof else rng):
            if all(norm(lines[i + j]) == pn[j] for j in range(len(pattern))):
                return i
    return -1


def apply_hunks(text: str, hunks: list[Hunk], path: str) -> str:
    trailing_nl = text.endswith("\n") or text == ""
    lines = text.split("\n")
    if trailing_nl and lines and lines[-1] == "":
        lines = lines[:-1]
    cursor = 0
    for h in hunks:
        if h.header:
            idx = seek(lines, [h.header], cursor)
            if idx < 0:
                raise ToolError(f"{path}：找不到 @@ 定位行：{h.header}")
            cursor = idx + 1
        old, new = list(h.old), list(h.new)
        if not old:
            pos = len(lines) if h.eof or not h.header else cursor
            lines[pos:pos] = new
            cursor = pos + len(new)
            continue
        idx = seek(lines, old, cursor, h.eof)
        if idx < 0 and old and old[-1] == "":
            old, new = old[:-1], (new[:-1] if new and new[-1] == "" else new)
            idx = seek(lines, old, cursor, h.eof)
        if idx < 0:
            raise ToolError(f"{path}：找不到要替换的上下文：\n" + "\n".join(old[:8]) +
                            "\n请重新读取文件，确保 ' ' 和 '-' 行与原文一致。")
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
