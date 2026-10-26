from __future__ import annotations

from ee.tools.base import Tool, ToolContext, ToolError, ToolResult, rel
from ee.tools.fileops import make_diff, read_text, replace, write_text


class EditTool(Tool):
    name = "edit"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要修改的文件路径"},
            "old_string": {"type": "string", "description": "要被替换的原文（逐字复制，不含行号前缀）"},
            "new_string": {"type": "string", "description": "替换成的新文本（必须与 old_string 不同）"},
            "replace_all": {"type": "boolean", "description": "替换所有匹配处，默认 false"},
        },
        "required": ["path", "old_string", "new_string"],
    }

    def summary(self, args: dict) -> str:
        return f"Edit {args.get('path', '')}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(args.get("path", ""))
        if not path.exists():
            raise ToolError(f"文件不存在：{path}。新建文件请用 write。")
        if not ctx.state.was_read(path):
            raise ToolError(f"编辑前必须先用 read 读过 {path}。")
        tf = read_text(path)
        new_text, strategy, count = replace(tf.text, args.get("old_string", ""), args.get("new_string", ""),
                                            bool(args.get("replace_all")))
        st = path.stat()
        rec = ctx.state.files.get(str(path))
        stale = rec is not None and (rec.mtime_ns != st.st_mtime_ns or rec.size != st.st_size)
        ctx.state.snapshot(path)
        write_text(path, tf, new_text)
        ctx.state.mark_read(path)
        name = rel(path, ctx.cwd)
        diff, add, rem = make_diff(tf.text, new_text, name)
        notes = []
        if strategy != "exact":
            notes.append(f"（old_string 未精确匹配，已按「{strategy}」容错策略定位）")
        if stale:
            notes.append("（注意：文件在你读取后被外部修改过，但按当前内容仍唯一匹配，已编辑）")
        msg = f"已编辑 {path}：替换 {count} 处，+{add} -{rem} 行。" + "".join(notes)
        return ToolResult(msg, summary=f"Edited {name} (+{add} -{rem})", diff=diff,
                          meta={"kind": "edit", "verb": "Edited", "path": name, "added": add, "removed": rem,
                                "strategy": strategy})
