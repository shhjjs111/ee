from __future__ import annotations

import asyncio

from ee.tools.base import Tool, ToolContext, ToolError, ToolResult, rel
from ee.tools.fileops import (STRATEGY_LABEL, apply_edits, make_diff, parse_edits, path_lock, read_text,
                              unencodable, write_text)


class EditTool(Tool):
    name = "edit"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要修改的文件路径"},
            "edits": {
                "type": "array",
                "description": "一处或多处替换，每处都对照原文件匹配，不能重叠",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string", "description": "要被替换的原文（逐字复制，不含行号前缀）"},
                        "new_string": {"type": "string", "description": "替换成的新文本"},
                        "replace_all": {"type": "boolean", "description": "替换所有匹配处，默认 false"},
                    },
                    "required": ["old_string", "new_string"],
                },
            },
        },
        "required": ["path", "edits"],
    }

    def summary(self, args: dict) -> str:
        return f"Edit {args.get('path', '')}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(args.get("path", ""))
        edits = parse_edits(args)
        async with path_lock(path):   # 同一文件的修改排队：读-算-写之间有 await
            if not path.exists():
                raise ToolError(f"文件不存在：{path}。新建文件请用 write。")
            if not ctx.state.was_read(path):
                raise ToolError(f"编辑前必须先用 read 读过 {path}。")
            tf = await asyncio.to_thread(read_text, path)
            if tf.lossy:
                raise ToolError(f"认不出 {path} 的编码（不是 UTF-8 也不是 GBK），局部编辑会损坏原内容。要改请用 write 整体重写。")
            # 匹配（尤其找不到时的相似度搜索）可能较慢，放到线程里，不卡事件循环
            new_text, hits = await asyncio.to_thread(apply_edits, tf.text, edits)
            bad = unencodable(tf, new_text)
            if bad is not None:
                raise ToolError(f"{path} 是 {tf.encoding.upper()} 编码，新内容里的「{bad}」无法用它保存。请换成别的字符。")
            st = path.stat()
            rec = ctx.state.files.get(str(path))
            stale = rec is not None and (rec.mtime_ns != st.st_mtime_ns or rec.size != st.st_size)
            ctx.state.snapshot(path)
            write_text(path, tf, new_text)
            ctx.state.mark_read(path)
        name = rel(path, ctx.cwd)
        diff, add, rem = make_diff(tf.text, new_text, name)
        count = sum(len(h.spans) for h in hits)
        notes = []
        fuzzy = [h for h in hits if h.strategy != "exact"]
        if fuzzy:
            if len(hits) == 1:
                notes.append(f"（old_string 未精确匹配，已按「{STRATEGY_LABEL[fuzzy[0].strategy]}」容错定位）")
            else:
                notes.append("（" + "；".join(f"edits[{h.index}] 按「{STRATEGY_LABEL[h.strategy]}」容错定位"
                                             for h in fuzzy) + "）")
        if stale:
            notes.append("（注意：文件在你读取后被外部修改过，但按当前内容仍能唯一匹配，已编辑）")
        msg = f"已编辑 {path}：替换 {count} 处，+{add} -{rem} 行。" + "".join(notes)
        strategies = [h.strategy for h in hits]
        strategy = next((s for s in strategies if s != "exact"), "exact")
        return ToolResult(msg, summary=f"Edited {name} (+{add} -{rem})", diff=diff,
                          meta={"kind": "edit", "verb": "Edited", "path": name, "added": add, "removed": rem,
                                "strategy": strategy, "edits": len(hits)})
