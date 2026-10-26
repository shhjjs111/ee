from __future__ import annotations

import asyncio

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
            "pages": {"type": "string", "description": "PDF / PPTX 读哪些页，如 \"3\"、\"2-5\"、\"1,3,7-9\"；默认 PDF 前 50 页、PPTX 全部"},
            "sheet": {"type": "string", "description": "Excel 工作表名或序号（1 起），默认第一个"},
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
        from ee.core.docread import OLD_FORMATS, doc_kind
        if doc_kind(path) or path.suffix.lower() in OLD_FORMATS:
            return await self._read_doc(path, args, offset, limit, ctx)
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

    async def _read_doc(self, path, args: dict, offset: int, limit: int, ctx: ToolContext) -> ToolResult:
        """PDF / Word / Excel / PPT：先转文字（D4-7），再按行号分段返回，和普通文本一样可用 offset/limit 续读。"""
        from ee.core.docread import DocError, extract
        try:
            text, info = await asyncio.to_thread(extract, path, args.get("pages"), args.get("sheet"))
        except DocError as e:
            raise ToolError(str(e))
        ctx.state.mark_read(path)
        kind = info.get("kind", "")
        head = {"pdf": f"[PDF，共 {info.get('pages')} 页" + (f"，显示第 {_ranges(info.get('shown', []))} 页" if info.get("shown") else "") + "]",
                "docx": f"[Word 文档，{info.get('tables', 0)} 个表格]",
                "xlsx": f"[Excel，当前工作表「{info.get('sheet', '')}」；换表用 sheet 参数]",
                "pptx": f"[PowerPoint，共 {info.get('slides')} 张" + (f"，显示第 {_ranges(info.get('shown', []))} 张" if info.get("shown") else "") + "]"}.get(kind, "")
        if kind == "pdf" and info.get("empty_pages"):
            head += f"\n[第 {_ranges(info['empty_pages'])} 页没有文字层，可能是扫描件]"
        if kind == "pdf" and info.get("pages", 0) > len(info.get("shown", [])) and not args.get("pages"):
            head += "\n[只显示了前面几页：用 pages 参数读其余页]"
        lines = text.split("\n") if text else []
        total = len(lines)
        summary = f"Read {rel(path, ctx.cwd)}"
        meta = {"kind": "read", "doc": kind, "doc_info": {k: v for k, v in info.items() if k in ("pages", "sheet", "slides", "tables")}}
        if total == 0:
            return ToolResult(head + "\n<没有可提取的文字>", summary=summary, meta=meta)
        if offset > total:
            raise ToolError(f"offset={offset} 超过转换后的总行数 {total}")
        chunk = lines[offset - 1: offset - 1 + limit]
        width = len(str(offset + len(chunk)))
        out = [head]
        for i, line in enumerate(chunk, start=offset):
            if len(line) > MAX_LINE:
                line = line[:MAX_LINE] + f" …（本行过长，截断，原长 {len(line)} 字符）"
            out.append(f"{i:>{width}}\t{line}")
        end = offset + len(chunk) - 1
        if end < total:
            out.append(f"\n（显示转换后文字的第 {offset}-{end} 行，共 {total} 行。用 offset={end + 1} 继续读）")
        return ToolResult("\n".join(out), summary=summary, meta=meta)


def _ranges(nums: list[int]) -> str:
    """[1,2,3,5,7,8] → '1-3、5、7-8'"""
    if not nums:
        return ""
    nums = sorted(nums)
    out, a, b = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == b + 1:
            b = n
            continue
        out.append(f"{a}-{b}" if a != b else str(a))
        a = b = n
    out.append(f"{a}-{b}" if a != b else str(a))
    return "、".join(out)
