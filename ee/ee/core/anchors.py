"""排除项锚点：推理被清空之后，把「已经排除的方向」留在上下文里。

**为什么需要**：ee 在消息第一次发送前就把旧推理清成空串（`prune.sync_reasoning`，
DeepSeek 带 tools 时必须回传 reasoning，但回传的是 `""`）。这省掉了长会话里最大的一块
可裁内容，也顺手丢掉了「哪条路试过、为什么不行」——模型可能在新回合里重走一遍。

**这里不恢复推理**：只从 assistant 的 `reasoning` 里抽出否定/排除结论，做成几条短锚点，
挂进 `<context-update>` 附录。成本是几百 token（且随最新用户消息一起缓存），
不改变推理回传的字节口径，也不会因为压缩而丢失。

**默认关（2026-11 试点后）**：3 个单回合任务 + 1 个两回合探针里，锚点没有提升完成率，
两回合探针反而多花 18% token（多 1 步、多 1 次请求）。抽取精度还不够高，
所以先用 `[prune] exclusion_anchors = true` 显式开启做实验，不默认影响所有会话。

**为什么是这些模式**：只认明确的否定结论（不可行 / 行不通 / 排除 / doesn't work …），
普通描述不抽；宁可少一条，也不要让锚点变成噪声。抽取是纯函数、对同一段历史完全确定，
所以附录只在真的多了一条排除时变化。
"""
from __future__ import annotations

import re

# 只认“结论性的否定”，不把“不是/不能/不要”这类口语否定当锚点：
# 实测它们在推理里大量出现在自我对话里（“不要动手改代码”“我不能确认”），噪声远大于信号。
_EXCLUDE = re.compile(
    r"不可行|行不通|不可取|不适合|不成立|排除|不能用|不要用|不应该|没法用|"
    r"\bwon't work\b|\bdoesn't work\b|\bwill not work\b|\bnot feasible\b|"
    r"\brules? out\b|\bruled out\b|\breject\b",
    re.IGNORECASE,
)
# 句子/分句切分：中文按标点，英文按句末标点 + 空白；换行也算。
_SENT = re.compile(r"[。！？；\n]+|(?<=[.!?])\s+")
_CLAUSE = re.compile(r"[，,、;；]+")

MAX_ANCHORS = 6          # 只留最近几条；锚点不是日志，多了会挤占注意力
MAX_CHARS = 160          # 单条上限，超过就截断（结论通常在前半句）
MAX_TOTAL = 900          # 整个块的上限，防止长 Reasoning 把附录撑大
MIN_CHARS = 5            # 太短的片段（“不是”本身 / “这个不是”）没有信息量；中文 5 字已能成义
# 这些是在“讨论候选/自我对话/模糊猜测”，不是“已经排除”的结论。
_META = re.compile(
    r"候选|看似|另一个|再一个|列为|举例|例如|比如|假设|如果|假如|"
    r"\blet me\b|\bi'll\b|\bi will\b|\bi would\b|\bi should\b|"
    r"让我|我会|我先|我需要|需要确认|确认一下|看看|"
    r"unlikely|probably|maybe|也许|大概|可能",
    re.IGNORECASE,
)
_DANGLING = ("不可行", "行不通", "不可取", "不适合", "不成立", "排除", "不能用", "不要用")
_QUOTED = ('"不可行"', "“不可行”", "'不可行'", "‘不可行’")


def _usable(claim: str) -> bool:
    """过滤“在讨论/假设排除”的片段，只留能当作结论用的。"""
    c = claim.strip("\"'“”‘’ ")
    if not c:
        return False
    if c.endswith(("：", ":", "？", "?")):      # 列表标题 / 问句不是结论
        return False
    if c.startswith(_DANGLING):                    # 只有否定词、没有对象
        return False
    if _META.search(c):                             # “另一个看似可行……”“Let me …”
        return False
    # 纯英文片段至少要像一句话："Not feasible" 这种没有主语的碎片当锚点毫无用处。
    if not re.search(r"[\u4e00-\u9fff]", c) and len(re.findall(r"[A-Za-z0-9_]+", c)) < 3:
        return False
    return not any(q in c for q in _QUOTED)        # 引号里的是被提及，不是被断言


def enabled(data: dict | None) -> bool:
    """是否开启排除项锚点。**默认关**，`[prune] exclusion_anchors = true` 才开。

    2026-11 试点（3 个单回合任务 + 1 个两回合探针）：锚点没有带来完成率提升，
    两回合探针反而多花 18% token（多 1 步、多 1 次请求）。在抽取精度和收益被证明前
    不默认开——推理被清空本身已经把成本压到最低，锚点是“用 token 换记忆”，换不到就不加。

    推理本来就没被清（strip_reasoning=false）时也不叠加：模型能看到完整推理，
    再加一遍锚点只是重复。只认真正的布尔 true，配置里写错字符串不会悄悄打开。
    """
    from ee.core import prune as P

    if not P.settings(data).get("strip_reasoning", True):
        return False
    cfg = (data or {}).get("prune") or {}
    return cfg.get("exclusion_anchors") is True


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().strip("-*•· ").rstrip(".").strip()


def extract(text: str) -> list[str]:
    """从一段推理里抽出排除结论（按出现顺序，可能重复）。"""
    if not isinstance(text, str) or not text:
        return []
    out: list[str] = []
    for sentence in _SENT.split(text):
        for clause in _CLAUSE.split(sentence):
            c = _normalize(clause)
            if len(c) < MIN_CHARS or not _EXCLUDE.search(c) or not _usable(c):
                continue
            out.append(c[:MAX_CHARS].rstrip())
    return out


def from_history(history: list[dict]) -> list[str]:
    """从历史里的 assistant 推理抽锚点，按「最近说过」排序，去重并限量。

    只看 `reasoning`（被清空的那部分），不看 content / 用户消息：后者会被正常发送，
    再抽一遍只会重复。重复的结论以后一次为准（同一个坑又提了一次，说明它更相关）。
    """
    if not history:
        return []
    # dict 保留插入顺序：重复时先删再插，等于“移到最近”。
    latest: dict[str, str] = {}
    for msg in history:
        if msg.get("role") != "assistant":
            continue
        for claim in extract(msg.get("reasoning") or ""):
            key = claim.casefold()
            latest.pop(key, None)
            latest[key] = claim
    picked = list(latest.values())[-MAX_ANCHORS:]
    # 总量上限：从最旧的开始丢（越新的结论越可能和当前任务相关）。
    while picked and sum(len(x) + 3 for x in picked) > MAX_TOTAL:
        picked.pop(0)
    return picked


def render(anchors: list[str]) -> str:
    """把锚点渲染成附录子块；空列表返回空串（调用方据此不挂这一块）。"""
    if not anchors:
        return ""
    body = "\n".join(f"- {a}" for a in anchors)
    return ("<excluded>\n此前推理里已排除的方向（没有新证据不要重复尝试）：\n"
            + body + "\n</excluded>")
