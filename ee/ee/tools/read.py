from __future__ import annotations

import asyncio

from ee.tools.base import Tool, ToolContext, ToolError, ToolResult, rel
from ee.tools.fileops import read_text

DEFAULT_LIMIT = 2000
MAX_LINE = 2000
REF_MIN_BYTES = 2048
STREAM_BYTES = 8 * 1024 * 1024     # 超过这么大的文本文件逐行扫描，只留要显示的行，不整体读入内存
LINE_READ_CAP = 64 * 1024          # 逐行扫描时每行最多读入的字节，超出部分跳过

# read 工具能按「视觉内容」读取的图片扩展名（方案 A：as_image=true 登记到下一轮 image 上下文）
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
HEAD_BYTES = 16


def _describe_head(head: bytes) -> str:
    """文件头 → 人类可读的格式名。"""
    if head[:4] == b"PK\x03\x04":
        return "ZIP 容器（docx/xlsx/pptx，或 Word 导出的 XPS）"
    if head[:4] == b"%PDF":
        return "PDF 文档"
    if head[:2] == b"BM":
        return "BMP 位图"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "TIFF 图片"
    if head[4:8] == b"ftyp":
        return "HEIF/AVIF 图片"
    if head[:2] == b"\x1f\x8b":
        return "gzip 压缩包"
    if head[:4] == b"RIFF":
        return "RIFF 容器"
    return "无法识别的字节"


def _scan_window(path, offset: int, limit: int) -> tuple[list[str], int]:
    """逐行扫描大文件：返回 (第 offset 行起最多 limit 行, 总行数)。超长行只读前 LINE_READ_CAP 字节。"""
    from ee.tools.textdecode import _utf8_prefix
    out: list[str] = []
    total = 0
    with open(path, "rb") as f:
        head = f.read(65536)
        if b"\x00" in head[:8192]:
            raise ToolError(f"{path} 看起来是二进制文件，不能按文本读")
        enc = "utf-8"
        if _utf8_prefix(head) is None:     # 开头 64KB 不是 UTF-8：能按 GBK 解就当 GBK（Windows 中文老日志）
            for cand in (head, head[:-1]):        # 末字节可能是被切开的双字节字符的前半
                try:
                    cand.decode("gbk")
                    enc = "gbk"
                    break
                except UnicodeDecodeError:
                    continue
        f.seek(0)
        if f.read(3) != b"\xef\xbb\xbf":
            f.seek(0)
        while True:
            b = f.readline(LINE_READ_CAP)
            if not b:
                break
            extra = 0
            if not b.endswith(b"\n"):        # 超长行（或最后一行没有换行）：把这一行剩下的读掉
                while True:
                    c = f.readline(1 << 20)
                    if not c:
                        break
                    extra += len(c)
                    if c.endswith(b"\n"):
                        break
            total += 1
            if offset <= total < offset + limit:
                line = b.rstrip(b"\r\n").decode(enc, errors="replace")
                if extra and len(line) > MAX_LINE:
                    line = line[:MAX_LINE] + f" …（本行过长，截断，原长 {len(b) + extra} 字节）"
                out.append(line)
    return out, total


class ReadTool(Tool):
    name = "read"
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "起始行（从 1 起）"},
            "limit": {"type": "integer", "description": "最多读多少行，默认 2000"},
            "pages": {"type": "string", "description": "PDF/PPTX 页码，如 \"2-5\""},
            "sheet": {"type": "string", "description": "Excel 工作表名或序号"},
            "as_image": {"type": "boolean", "description": "读图片的视觉内容（下一轮可见）"},
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
            return ToolResult("\n".join(lines) + more, summary=f"List {rel(path, ctx.cwd)}",
                              meta={"kind": "list", "path": rel(path, ctx.cwd), "files": lines[:200], "total": len(items)})
        offset = max(1, int(args.get("offset") or 1))
        limit = max(1, int(args.get("limit") or DEFAULT_LIMIT))
        # 图片：默认提示用 as_image=true 读视觉内容；显式传参则登记进待看队列，
        # 下一轮作为 image_url 块注入 user 消息（tool 结果是字符串，无法本轮带图）。
        if path.is_file() and path.suffix.lower() in IMAGE_EXTS:
            if args.get("as_image") is True:
                # 扩展名是用户以为的格式，文件头才是真的。按扩展名登记的话，一个叫
                # _page1.png 的 XPS 会被当成 png 发出，上游回 400 拒整条请求，而这条
                # 消息随历史每轮重发，会话从此永久卡死。所以发送前按字节验一次。
                from ee.core.attachments import sniff
                with open(path, "rb") as fh:
                    head = fh.read(HEAD_BYTES)
                if sniff(head) is None:
                    raise ToolError(
                        f"{path} 不能作为图片发送：文件头是{_describe_head(head)}，"
                        f"不是 JPEG/PNG/GIF/WebP（扩展名 {path.suffix} 说明不了格式）。"
                        "要发图请先转成 PNG 或 JPEG；如果它其实是文档，直接用 read（不带 as_image）读文字。")
                # 快照到会话目录：这张图会成为历史中段，之后工作区里的源文件被替换/删除
                # 不能让消息字节跟着变（否则从那条起整段前缀失效）。
                sess = (ctx.extra or {}).get("session")
                snap = getattr(sess, "snapshot_image", None)
                ctx.state.pending_images.append(snap(str(path)) if snap is not None else str(path))
                return ToolResult(
                    f"[图片已登记] {path} 将在下一轮作为图片内容发送给模型。"
                    "（当前这一轮模型只能看到这段文字，下一轮才能真正看到图片。）",
                    summary=f"Read image {rel(path, ctx.cwd)}",
                    meta={"kind": "read", "image": True, "path": rel(path, ctx.cwd)})
            return ToolResult(
                f"[图片文件] {path} 是图片，直接按文本读会得到二进制乱码。"
                "如果你需要模型看到这张图（如截图、设计稿、验收图），请用 read(path=..., as_image=true) 读取。",
                summary=f"Read {rel(path, ctx.cwd)}",
                meta={"kind": "read", "image": True, "path": rel(path, ctx.cwd)})
        from ee.core.docread import OLD_FORMATS, doc_kind
        if doc_kind(path) or path.suffix.lower() in OLD_FORMATS:
            return await self._read_doc(path, args, offset, limit, ctx)
        if path.is_file() and path.stat().st_size > STREAM_BYTES:
            return await self._read_large(path, offset, limit, ctx)
        tf = await asyncio.to_thread(read_text, path)
        # 内容身份（T17）：用解码后的正文算哈希，作为"文件是否真的变了"的判据。
        # 用 tf.text 而不是磁盘原始字节：引用语义是"模型看到的正文没变"，
        # CRLF/BOM 差异对模型不可见，不该触发假失效（ACC-40）。
        from ee.core.evidence import content_identity
        identity, _ = content_identity(tf.text.encode("utf-8"))
        all_lines = tf.text.split("\n")
        if tf.text.endswith("\n"):
            all_lines = all_lines[:-1]
        total = len(all_lines)
        if total == 0:
            ctx.state.mark_read(path, identity)      # 空文件：读过就是读过，允许后续写入
            return ToolResult("<文件为空>", summary=f"Read {rel(path, ctx.cwd)}",
                              meta={"kind": "read", "path": rel(path, ctx.cwd), "total_lines": 0})
        if offset > total:
            # 越界检查必须在 mark_read **之前**：否则一次失败的读取也记成「读过」，
            # write 的「覆盖前必须先 read 过」护栏就被一次读错（模型猜 offset、文档越界）
            # 满足了，它随后覆盖的是自己从未看见过的内容。
            raise ToolError(f"offset={offset} 超过文件总行数 {total}")
        rec = ctx.state.mark_read(path, identity)
        key = (offset, limit)
        # 未变的重复读 → 引用（省 token）；同一引用第二次再要就给全文，避免死循环
        if key in rec.reads and rec.size >= REF_MIN_BYTES and rec.reads[key] == 0:
            rec.reads[key] = 1
            return ToolResult(f"[与上次读取相同] {path} 自上次读取后未改变（{total} 行），内容见上文同一次 read 的结果。"
                              "如果上文已被压缩看不到了，再调用一次 read 会返回全文。",
                              summary=f"Read {rel(path, ctx.cwd)}",
                              meta={"kind": "read", "ref": True, "path": rel(path, ctx.cwd), "total_lines": total})
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
        return ToolResult("\n".join(out), summary=f"Read {rel(path, ctx.cwd)}",
                          meta={"kind": "read", "path": rel(path, ctx.cwd), "start": offset, "end": end,
                                "total_lines": total})

    async def _read_large(self, path, offset: int, limit: int, ctx: ToolContext) -> ToolResult:
        chunk, total = await asyncio.to_thread(_scan_window, path, offset, limit)
        ctx.state.mark_read(path)
        summary = f"Read {rel(path, ctx.cwd)}"
        if total == 0:
            return ToolResult("<文件为空>", summary=summary, meta={"kind": "read"})
        if offset > total:
            raise ToolError(f"offset={offset} 超过文件总行数 {total}")
        width = len(str(offset + len(chunk)))
        out = []
        for i, line in enumerate(chunk, start=offset):
            if len(line) > MAX_LINE and "…（本行过长" not in line[MAX_LINE:]:
                line = line[:MAX_LINE] + f" …（本行过长，截断，原长 {len(line)} 字符）"
            out.append(f"{i:>{width}}\t{line}")
        end = offset + len(chunk) - 1
        if end < total:
            out.append(f"\n（显示第 {offset}-{end} 行，共 {total} 行。用 offset={end + 1} 继续读）")
        return ToolResult("\n".join(out), summary=summary, meta={"kind": "read", "large": True})

    async def _read_doc(self, path, args: dict, offset: int, limit: int, ctx: ToolContext) -> ToolResult:
        """PDF / Word / Excel / PPT：先转文字（D4-7），再按行号分段返回，和普通文本一样可用 offset/limit 续读。"""
        from ee.core.docread import DocError, extract_async
        try:
            text, info = await extract_async(path, args.get("pages"), args.get("sheet"))
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
