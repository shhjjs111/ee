"""会话导出（T10-a / U1）：把 `SessionStore.load()` 的结果变成可留存的文本。

**纯函数**：这里没有任何 I/O——输入 `{items, meta}`，输出字符串。取数据与落盘
分别是 server 层的事，这样本模块能被单测、也能被 CLI 复用。

两个必须做对、做错了不会报错只会让人误以为完整的地方：

1. **工具参数是 repr 字符串，不是 JSON。** 会话文件里 `tool.args` 长这样：
   `"{'path': 'C:\\\\Users\\\\x\\\\calc.py'}"`。直接 `json.dumps` 会得到一堆转义
   反斜杠冒充结构；JSON 导出必须先用 `ast.literal_eval` 还原。
2. **未知条目不能静默丢弃。** 新版本加了条目类型、老版本的程序读它，跳过就等于
   「导出成功但少了东西」——用户没有任何线索。

**边界**：只导出 `items`。附件（`threads.py:start_turn(images, attachments)`）
不写入 items，所以导出天然不含附件；这是有意为之，不是漏了。
"""
from __future__ import annotations

import ast
import json
from typing import Any

FORMATS = ("markdown", "json")

# 未识别条目的占位前缀：既能在 Markdown 里看见，也便于 grep 定位
_UNKNOWN = "[未识别条目 type={}]"


def export(data: dict, fmt: str = "markdown", **opts) -> str:
    """唯一入口。`fmt` 只接受 markdown / json，别的明确报错。"""
    if fmt == "markdown":
        return to_markdown(data, **opts)
    if fmt == "json":
        return to_json(data, **opts)
    raise ValueError(f"未知导出格式：{fmt!r}（可选：{', '.join(FORMATS)}）")


# ---------------------------------------------------------------- Markdown

def to_markdown(data: dict, *, include_reasoning: bool = True,
                include_tool_output: bool = True) -> str:
    """人读格式：标题骨架 + 按 seq 排列的回合，工具调用挂在所属助手消息之后。"""
    meta = data.get("meta") or {}
    items = _ordered(data)

    lines: list[str] = []
    lines.append(f"# {meta.get('title') or '新对话'}")
    lines.append("")
    head = _meta_line(meta)
    if head:
        lines.append(head)
        lines.append("")

    if not items:
        lines.append("_（这个会话还没有内容。）_")
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    for it in items:
        t = it.get("type")
        if t == "user":
            lines.append("## 用户")
            lines.append("")
            lines.append(_text(it.get("text")))
            lines.append("")
        elif t == "assistant":
            lines.append("## 助手")
            lines.append("")
            if include_reasoning and (it.get("reasoning") or "").strip():
                lines.append("> **推理**")
                for ln in str(it["reasoning"]).strip().splitlines():
                    lines.append(f"> {ln}")
                lines.append("")
            lines.append(_text(it.get("text")))
            lines.append("")
        elif t == "tool":
            lines.append(f"### 工具 · {it.get('name') or '未知'}（{it.get('status') or '?'}）")
            lines.append("")
            if it.get("summary"):
                lines.append(f"_{it['summary']}_")
                lines.append("")
            if it.get("args"):
                lines.append("**参数**")
                lines.append("")
                lines.append("```")
                lines.append(str(it["args"]))
                lines.append("```")
                lines.append("")
            if include_tool_output and it.get("output"):
                lines.append("**输出**")
                lines.append("")
                lines.extend(_fence(str(it["output"])))
                lines.append("")
        elif t == "turn_end":
            # 回合结束原因要可见：用户才知道那轮是正常结束还是被打断
            reason = it.get("reason") or "?"
            dur = it.get("duration_ms")
            tail = f" · {dur}ms" if isinstance(dur, (int, float)) else ""
            lines.append(f"---\n\n_回合结束：{reason}{tail}_\n")
        else:
            lines.append(_UNKNOWN.format(t))
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _fence(body: str) -> list[str]:
    """把内容包进围栏。内容自己带 ``` 时用更长的围栏，否则块会提前闭合。"""
    fence = "```"
    while fence in body:
        fence += "`"
    return [fence, body, fence]


def _meta_line(meta: dict) -> str:
    bits = []
    if meta.get("model"):
        bits.append(f"模型：`{meta['model']}`")
    if meta.get("cwd"):
        bits.append(f"工作区：`{meta['cwd']}`")
    if meta.get("mode"):
        bits.append(f"模式：{meta['mode']}")
    if meta.get("created"):
        bits.append(f"创建：{_ts(meta['created'])}")
    if meta.get("updated"):
        bits.append(f"更新：{_ts(meta['updated'])}")
    return "  \n".join(bits)


def _ts(v: Any) -> str:
    import datetime
    try:
        return datetime.datetime.fromtimestamp(float(v)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return str(v)


def _text(v: Any) -> str:
    s = "" if v is None else str(v)
    return s if s.strip() else "_（空）_"


# ---------------------------------------------------------------- JSON

def to_json(data: dict, *, include_reasoning: bool = True,
            include_tool_output: bool = True) -> str:
    """机读格式：工具参数 / 详情还原成对象；还原不了就留原文并标记 `args_raw`。"""
    meta = dict(data.get("meta") or {})
    out_items = []
    for it in _ordered(data):
        new = _json_item(it, include_reasoning, include_tool_output)
        out_items.append(new)
    doc = {"meta": meta, "items": out_items}
    # ensure_ascii=False：中文不转义，导出文件是给人看的
    return json.dumps(doc, ensure_ascii=False, indent=2)


def _json_item(it: dict, include_reasoning: bool, include_tool_output: bool) -> dict:
    new = dict(it)
    if not include_reasoning:
        new.pop("reasoning", None)
    # usage 也是 repr 字符串（实测："{'requests': 1, 'prompt': 4146}"）
    if "usage" in new:
        parsed, ok = _maybe_literal(new["usage"])
        if ok:
            new["usage"] = parsed
    if new.get("type") == "tool":
        for key in ("args", "details"):
            if key not in new or new[key] is None:
                continue
            parsed, ok = _maybe_literal(new[key])
            if ok:
                new[key] = parsed
            else:
                # 留原文 + 标记：消费方能区分「结构」与「原文」
                new[f"{key}_raw"] = True
        if not include_tool_output:
            new.pop("output", None)
    return new


def _maybe_literal(v: Any) -> tuple[Any, bool]:
    """把 Python repr 字符串还原成对象。已经是对象就原样返回。

    `ast.literal_eval` 只接受字面量，比 `eval` 安全：不会执行任何代码。
    """
    if not isinstance(v, str):
        return v, True
    s = v.strip()
    if not s:
        return v, True
    try:
        return ast.literal_eval(s), True
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return v, False


# ---------------------------------------------------------------- 共用

def _ordered(data: dict) -> list[dict]:
    """按 seq 排序；缺 seq 的排在最后（稳定），不因排序丢条目。"""
    items = list(data.get("items") or [])
    return sorted(items, key=lambda it: (it.get("seq") is None, it.get("seq") or 0))
