from __future__ import annotations

from ee.tools.base import Tool, ToolContext, ToolError, ToolResult, rel
from ee.tools.fileops import make_diff, read_text, write_text


class WriteTool(Tool):
    name = "write"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（绝对路径或相对工作目录）"},
            "content": {"type": "string", "description": "完整文件内容"},
        },
        "required": ["path", "content"],
    }

    def summary(self, args: dict) -> str:
        return f"Write {args.get('path', '')}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(args.get("path", ""))
        content = args.get("content")
        if content is None:
            raise ToolError("缺少 content 参数")
        if path.is_dir():
            raise ToolError(f"{path} 是目录")
        existed = path.exists()
        if existed and not ctx.state.was_read(path):
            raise ToolError(f"{path} 已存在，覆盖前必须先用 read 读过它。若只是改一部分，请用 edit。")
        if existed:
            st = path.stat()
            rec = ctx.state.files.get(str(path))
            if rec and (rec.mtime_ns != st.st_mtime_ns or rec.size != st.st_size):
                raise ToolError(f"{path} 在你读取之后被修改过，请重新 read 再写。")
        tf = read_text(path)
        ctx.state.snapshot(path)
        write_text(path, tf, content)
        ctx.state.mark_read(path)
        name = rel(path, ctx.cwd)
        diff, add, rem = make_diff(tf.text, content.replace("\r\n", "\n"), name)
        verb = "Edited" if existed else "Added"
        return ToolResult(f"已{'覆盖' if existed else '新建'} {path}（{content.count(chr(10)) + 1} 行）",
                          summary=f"{verb} {name} (+{add} -{rem})", diff=diff,
                          meta={"kind": "edit", "verb": verb, "path": name, "added": add, "removed": rem})
