"""输出截断：超 2000 行或 50KB → 全文存 ~/.ee/tool-output/，返回头尾预览 + 路径。

**为什么按工具调阈值**（对齐天枢 src/tools/artifact-threshold.ts 的思路）：

DeepSeek 的前缀缓存只在「用户输入末尾」和「模型输出末尾」落缓存单元，后续请求必须
完全匹配某个单元才命中。于是一个回合内每多一次工具调用，之前的工具结果就要重新计费
一次——**工具输出越大，后续每一步的重复成本越高**。

实测（~/.ee/sessions，9 个真实会话 / 2084 次请求 / 命中率 97.19%）：某回合 20 次工具
调用、输出合计 8 万字符，实际 miss 23.9 万 token，是新增量的 8.9 倍；按「每个工具输出
在其后每次请求都重算」建模得 36.2 万，落在同一量级，机制吻合。

所以给「单次输出大、后续极少逐字复用」的工具（网页正文）用更严的阈值：归档到磁盘，
历史里只留预览 + 路径，模型需要细节时按路径 grep/read。**不改变工具定义、系统提示、
请求构造**——那才是会让缓存失效的地方。
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import NamedTuple

MAX_LINES = 2000
MAX_BYTES = 50 * 1024
HEAD_LINES = 200
TAIL_LINES = 100
PREVIEW_BYTES = 24 * 1024

# 按工具缩小/放大的阈值（行数、字节）。键是工具名，未列出的用默认值。
# 只影响「多大才归档」，不影响归档后的格式。
_TOOL_LIMITS: dict[str, tuple[int, int]] = {
    # 网页正文：单次十几万字符，且几乎不会在后续步骤里被逐字引用。
    "web_fetch": (300, 12 * 1024),
    "web_search": (300, 12 * 1024),
    # repo_map / glob 的行数天然多但每行很短，按行数截容易误伤，只压字节。
    "repo_map": (MAX_LINES, 24 * 1024),
    "glob": (MAX_LINES, 24 * 1024),
}


class Limit(NamedTuple):
    lines: int
    bytes: int


def limit_for(tool: str) -> Limit:
    """某工具的输出归档阈值。未配置的工具用默认值（行为与以前一致）。"""
    lines, size = _TOOL_LIMITS.get((tool or "").strip(), (MAX_LINES, MAX_BYTES))
    return Limit(lines, size)


def truncate_output(text: str, out_dir: Path | None, tool: str = "tool",
                    max_lines: int = MAX_LINES, max_bytes: int = MAX_BYTES) -> tuple[str, bool]:
    out, info = truncate_output_info(text, out_dir, tool, max_lines, max_bytes)
    return out, info is not None


def truncate_output_info(text: str, out_dir: Path | None, tool: str = "tool",
                         max_lines: int | None = None, max_bytes: int | None = None) -> tuple[str, dict | None]:
    """同 truncate_output，第二项是截断信息 {lines, bytes, path}（没截断为 None），给界面的 details 用。

    max_lines / max_bytes 不传时按 tool 取阈值（见 limit_for），所以调用方只需给工具名。
    """
    lim = limit_for(tool)
    if max_lines is None:
        max_lines = lim.lines
    if max_bytes is None:
        max_bytes = lim.bytes
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
