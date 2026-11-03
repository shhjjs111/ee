"""estimate_tokens 的准确性回归：系数由真实 API 标定，这里守住"别退化"。

标定方法见 research/2026-11-03-cache-hit-rate.md §2.7：把待测文本作为唯一一条 user
消息发给真实模型（max_tokens=1），读回 usage.prompt_tokens 并扣掉单条消息的结构开销。
下面的 real 值全部来自这样的实测，不是估算出来的。

为什么值得单独守：`estimate_tokens` 不只用于统计，它还决定压缩保留段的大小、修剪阈值、
水位判断。旧公式（ASCII 4 字符 1 token）在 JSON / 路径 / ls / diff 上低估近一倍，而那些
正是长对话里占比最大的工具输出。
"""
from __future__ import annotations

import json

import pytest

from ee.core.compaction import estimate_tokens

# (名称, 文本, 真实 token 数)——真实值由 opencode 网关上的 deepseek-v4.1-flash 实测。
# 保留原始文本，这样改了公式就能立刻看到每一类偏成什么样。
CASES = [
    ("纯英文散文",
     "In software engineering, cache invalidation is one of the hardest problems you will "
     "face, alongside naming things. " * 6, 133),
    ("Python 代码",
     "def process(items, limit=10):\n    result = []\n    for item in items:\n"
     "        if item is None:\n            continue\n        result.append(item * 2)\n"
     "    return result[:limit]\n" * 6, 246),
    ("JSON",
     json.dumps([{"path": f"src/mod{i}.py", "size": i * 100, "ok": True} for i in range(25)],
                indent=1), 642),
    ("中文",
     "上下文压缩是一个权衡问题，保留得越多，每次请求的输入成本就越高。" * 8, 144),
    ("路径列表", "\n".join(f"E:/ee/core/module_{i}.py" for i in range(40)), 399),
    ("ls 输出",
     "".join(f"drwxr-xr-x 1 user 197609 0 Nov  3 12:0{i} .\n" for i in range(10)), 220),
    ("git diff",
     "diff --git a/foo.py b/foo.py\nindex 1234567..89abcde 100644\n--- a/foo.py\n"
     "+++ b/foo.py\n@@ -1,5 +1,6 @@\n-def old(a, b):\n+def new(a, b, c):\n"
     "     return a + b\n" * 5, 350),
    ("表格 markdown", "| 项 | 值 |\n|---|---|\n" + "| foo | bar |\n" * 30, 159),
    ("日志行",
     "2026-11-03 12:00:00 INFO ee.session 回合开始：会话 abc 第 1 回合\n" * 20, 560),
    ("随机 hex", "".join(f"{i:08x}" for i in range(100)), 311),
]


def test_empty_and_tiny():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") >= 1


# 逐字符加权有天花板：下面几类的 BPE 切分取决于词的组合方式，而不是字符类别本身，
# 单靠统计字符数无论如何都估不准。把它们单独列出来放宽，而不是调系数去讨好它们——
# 调了只会让其它样本变差（试过：改成"每段连续同类字符至少 1 token"的段模型后，
# 这几类从低估 0.5 倍变成高估 1.3–1.8 倍，整体平均误差从 0.254 恶化到 0.603）。
HARD_CASES = {"ls 输出"}


@pytest.mark.parametrize("name,text,real", CASES, ids=[c[0] for c in CASES])
def test_within_tolerance_of_real_api(name, text, real):
    """每一类都要落在真实值的合理区间内。

    常规样本守 ±40%：按字符类别加权只能接近、不可能精确——同样的字符在不同上下文里
    会被切成不同的 token。旧公式在 JSON / 路径 / ls / diff 上实测偏差 1.68–2.08 倍
    （即超出 ±68%~108%），这一条就是拦住那种退化。

    `ls` 输出守 ±60%：`drwxr-xr-x 1 user 197609 0 Nov 3` 这种"极短词 + 数字 + 空格"
    交替的结构，BPE 会把 `drwxr`、`197609` 各自切碎，token 数主要由此决定；逐字符
    统计只能给出下界（0.52–0.56 倍）。这是方法的边界，不是待修的 bug。
    """
    est = estimate_tokens(text)
    assert est > 0
    ratio = est / real
    lo, hi = (0.4, 1.6) if name in HARD_CASES else (0.6, 1.4)
    assert lo <= ratio <= hi, f"{name}: 估算 {est} vs 真实 {real} = {ratio:.2f}，超出 ±{int((hi - 1) * 100)}%"


def test_average_error_is_better_than_the_old_formula():
    """整体平均相对误差要明显优于旧公式（ASCII/4 + 其余/1）。

    旧公式实测平均误差 0.340。守住 0.25，避免有人"顺手简化"回一刀切。
    """
    def old(text: str) -> int:
        ascii_n = sum(1 for ch in text if ord(ch) < 128)
        return ascii_n // 4 + (len(text) - ascii_n) + 1

    new_err = sum(abs(estimate_tokens(t) / r - 1) for _, t, r in CASES) / len(CASES)
    old_err = sum(abs(old(t) / r - 1) for _, t, r in CASES) / len(CASES)
    assert new_err < 0.25, f"新公式平均误差 {new_err:.3f} 偏高"
    assert new_err < old_err * 0.75, f"新 {new_err:.3f} 没有明显优于旧 {old_err:.3f}"


def test_denser_content_estimates_more_tokens():
    """同样的字符数，标点/数字密度高的内容要估出更多 token——这是新公式的核心。"""
    prose = "the quick brown fox jumps over the lazy dog " * 3     # 全字母+空格
    jsonish = '{"a":1,"b":2,"c":3,"d":4,"e":5,"f":6,"g":7,"h":8}' * 3
    assert len(jsonish) <= len(prose) * 1.5
    assert estimate_tokens(jsonish) > estimate_tokens(prose)
