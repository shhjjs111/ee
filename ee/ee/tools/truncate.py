"""输出截断：超 2000 行或 50KB → 全文存 ~/.ee/tool-output/，返回头尾预览 + 路径。"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

MAX_LINES = 2000
MAX_BYTES = 50 * 1024
HEAD_LINES = 200
TAIL_LINES = 100
PREVIEW_BYTES = 24 * 1024


def truncate_output(text: str, out_dir: Path | None, tool: str = "tool",
                    max_lines: int = MAX_LINES, max_bytes: int = MAX_BYTES) -> tuple[str, bool]:
    out, info = truncate_output_info(text, out_dir, tool, max_lines, max_bytes)
    return out, info is not None


def truncate_output_info(text: str, out_dir: Path | None, tool: str = "tool",
                         max_lines: int = MAX_LINES, max_bytes: int = MAX_BYTES) -> tuple[str, dict | None]:
    """同 truncate_output，第二项是截断信息 {lines, bytes, path}（没截断为 None），给界面的 details 用。"""
    lines = text.split("\n")
    size = len(text.encode("utf-8", errors="replace"))
    if len(lines) <= max_lines and size <= max_bytes:
        return text, None
    path_note = ""
    full_path = None
    if out_dir is not None:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            f = out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{tool}-{uuid.uuid4().hex[:6]}.txt"
            f.write_text(text, encoding="utf-8", errors="replace")
            full_path = str(f)
            path_note = f"\n完整输出已保存到：{f}（可用 read 工具带 offset 分段读，或用 grep 搜索）"
        except OSError as e:
            path_note = f"\n（保存完整输出失败：{e}）"
    head = _cap("\n".join(lines[:HEAD_LINES]), PREVIEW_BYTES * 2 // 3, keep="head")
    tail = _cap("\n".join(lines[-TAIL_LINES:]), PREVIEW_BYTES // 3, keep="tail")
    omitted = max(0, len(lines) - HEAD_LINES - TAIL_LINES)
    msg = (f"[输出过长已截断：共 {len(lines)} 行 / {size} 字节，下面是开头和结尾]\n"
           f"{head}\n\n… 省略中间约 {omitted} 行 …\n\n{tail}{path_note}")
    return msg, {"lines": len(lines), "bytes": size, "path": full_path}


def _cap(s: str, n: int, keep: str) -> str:
    b = s.encode("utf-8", errors="replace")
    if len(b) <= n:
        return s
    if keep == "head":
        return b[:n].decode("utf-8", errors="ignore") + " …"
    return "… " + b[-n:].decode("utf-8", errors="ignore")
