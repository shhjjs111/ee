from __future__ import annotations

from ee.tools.base import Tool, ToolContext, ToolError, ToolResult, rel
from ee.tools.fileops import read_text

DEFAULT_LIMIT = 2000
MAX_LINE = 2000
REF_MIN_BYTES = 2048


class ReadTool(Tool):
    name = "read"
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（绝对路径或相对工作目录）"},
            "offset": {"type": "integer", "description": "从第几行开始读（1 起），默认 1"},
            "limit": {"type": "integer", "description": "最多读多少行，默认 2000"},
        },
        "required": ["path"],
    }

    def summary(self, args: dict) -> str:
        return f"Read {args.get('path', '')}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(args.get("path", ""))
        if not path.exists():
            raise ToolError(f"文件不存在：{path}。不确定路径时先用 glob 查找。")
        if path.is_dir():
            items = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            lines = [p.name + ("/" if p.is_dir() else "") for p in items[:500]]
            more = f"\n…（共 {len(items)} 项，只列前 500）" if len(items) > 500 else ""
            return ToolResult("\n".join(lines) + more, summary=f"List {rel(path, ctx.cwd)}", meta={"kind": "list"})
        offset = max(1, int(args.get("offset") or 1))
        limit = max(1, int(args.get("limit") or DEFAULT_LIMIT))
        tf = read_text(path)
        rec = ctx.state.mark_read(path)
        key = (offset, limit)
        all_lines = tf.text.split("\n")
        if tf.text.endswith("\n"):
            all_lines = all_lines[:-1]
        total = len(all_lines)
        if total == 0:
            return ToolResult("<文件为空>", summary=f"Read {rel(path, ctx.cwd)}", meta={"kind": "read"})
        if offset > total:
            raise ToolError(f"offset={offset} 超过文件总行数 {total}")
        # 未变的重复读 → 引用（省 token）；同一引用第二次再要就给全文，避免死循环
        if key in rec.reads and rec.size >= REF_MIN_BYTES and rec.reads[key] == 0:
            rec.reads[key] = 1
            return ToolResult(f"[与上次读取相同] {path} 自上次读取后未改变（{total} 行），内容见上文同一次 read 的结果。"
                              "如果上文已被压缩看不到了，再调用一次 read 会返回全文。",
                              summary=f"Read {rel(path, ctx.cwd)}", meta={"kind": "read", "ref": True})
        rec.reads[key] = 0
        chunk = all_lines[offset - 1: offset - 1 + limit]
        width = len(str(offset + len(chunk)))
        out = []
        for i, line in enumerate(chunk, start=offset):
            if len(line) > MAX_LINE:
                line = line[:MAX_LINE] + f" …（本行过长，截断，原长 {len(line)} 字符）"
            out.append(f"{i:>{width}}\t{line}")
        end = offset + len(chunk) - 1
        if end < total:
            out.append(f"\n（显示第 {offset}-{end} 行，共 {total} 行。用 offset={end + 1} 继续读）")
        return ToolResult("\n".join(out), summary=f"Read {rel(path, ctx.cwd)}", meta={"kind": "read"})
