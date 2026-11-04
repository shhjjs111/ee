"""recall 工具：检索本会话（或其它会话）已压缩掉的旧对话原文。

**为什么需要它**：压缩会把 `history` 整段换成「摘要 + 最近保留段」，被压掉的原文
在 `SessionStore.load()` 里因为 `reset` 记录而消失——摘要的标识符覆盖率实测只有
8.6%（`ee usage` 自报 8.25%，目标 ≥35%）。而这段原文其实**完整留在磁盘上**：
`reset` 只清 messages，不动 `items`，所以工具输出、命令、报错都还在会话 JSONL 里，
也已进了 `~/.ee/index/search.db`（`core/search_index.py`，FTS5 trigram，中文可用）。
缺的只是一个「给模型用的检索入口」。

所以这里**不新建索引、不新建存档文件**：复用已有的 `SearchIndex`，只加一层
工具外壳。压缩掉 276k token 的会话，召回一次只要毫秒级。
"""
from __future__ import annotations

import asyncio

from ee.core.search_index import get_index
from ee.tools.base import Tool, ToolContext, ToolError, ToolResult

MAX_RESULTS = 8
DEFAULT_RESULTS = 5


class RecallTool(Tool):
    name = "recall"
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "原文里的关键词（函数名/路径/报错片段）"},
        },
        "required": ["query"],
    }

    def summary(self, args: dict) -> str:
        return f"Recall {args.get('query', '')}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolError("query 不能为空：给一个原文里出现过的词")
        # 参数只留 query：工具表是冻结前缀的一部分，剩给它的预算不到 100 token，
        # limit/scope 这两个旋钮不值得各花 30 token。
        limit = DEFAULT_RESULTS
        thread_id = (ctx.extra or {}).get("thread_id")
        home = (ctx.extra or {}).get("home")
        if home is None:
            from ee.config import ee_home
            home = ee_home()
        out = await asyncio.to_thread(_search, home, query, limit, thread_id)
        if not out:
            return ToolResult(f"没有找到包含「{query}」的旧对话。换个更短、更具体的词再试（如函数名、文件名、报错片段）。",
                              summary=self.summary(args),
                              meta={"kind": "recall", "query": query, "hits": 0,
                                    "scope": "thread" if thread_id else "all"})
        return ToolResult(out, summary=self.summary(args),
                          meta={"kind": "recall", "query": query,
                                "scope": "thread" if thread_id else "all"})


def _search(home, query: str, limit: int, session_id: str | None) -> str:
    """检索并渲染。同步函数，由 run() 放线程里跑（SQLite 与文件同步 IO）。"""
    try:
        idx = get_index(home)
        groups = idx.search(query, limit=limit, per_thread=limit, session_id=session_id)
    except Exception as e:  # noqa: BLE001  索引坏了不该让回合失败
        raise ToolError(f"检索失败：{type(e).__name__}: {e}") from None
    hits: list[dict] = []
    for g in groups:
        for h in g.get("hits") or []:
            hits.append({**h, "thread_id": g.get("thread_id")})
            if len(hits) >= limit:
                break
        if len(hits) >= limit:
            break
    if not hits:
        return ""
    lines = [f"在已压缩掉的旧对话里找到 {len(hits)} 段（原文片段，不是摘要）："]
    no_thread = any(h.get("thread_id") for h in hits) and not session_id
    for i, h in enumerate(hits, 1):
        where = f" · 会话 {h['thread_id']}" if no_thread else ""
        lines.append(f"\n[{i}] {h.get('kind') or '内容'}{where}\n    {h.get('snippet') or ''}")
    lines.append("\n需要更多上下文时，用更具体的关键词再 recall 一次；要整段原文可用 read 读对应会话。")
    return "\n".join(lines)
