"""流式工具调用拼接：同一 index 下发多个调用时，参数不能串到别的槽里。

缺陷（按代码核对 + 模拟流复现）：`_accumulate()` 里

    if idx is not None and idx < len(calls):
        slot = calls[idx]
        # 同一 index 塞多个完整调用：id 变了且旧参数已完整 → 当新调用
        if tid and slot.get("id") and tid != slot["id"] and _complete(slot["function"]["arguments"]):
            slot = None

"当新调用"的条件要求**旧参数已经完整**。但中转站常见的是：两个调用都放 index=0，
参数分多块流式传输、只有第一块带 id。此时：

  A 第 1 块（id=a, args='{"x":')     → 新建槽 calls[0]
  A 第 2 块（无 id, args='1}')       → calls[0]，A 完整
  B 第 1 块（id=b, args='{"y":')     → A 已完整 → 新槽（正确）
  ... 但如果 A 尚未完整时 B 就到了：
  B 第 1 块（id=b, args='{"y":')     → A 不完整 → slot 仍是 calls[0] → B 的参数拼进 A

结果 A、B 的 arguments 都不是合法 JSON，这一步两个调用全失败。
"""
import json

import pytest

from ee.core.providers.openai_chat import _accumulate


def tc(index, tid=None, name=None, args=None):
    """构造一个流式 tool_call 分块（None 的字段表示服务端没下发）。"""
    d = {"index": index}
    if tid is not None:
        d["id"] = tid
    fn = {}
    if name is not None:
        fn["name"] = name
    if args is not None:
        fn["arguments"] = args
    if fn:
        d["function"] = fn
    return d


def parsed(calls):
    return [json.loads(c["function"]["arguments"]) for c in calls]


# ---------------------------------------------------------------- 缺陷复现


def test_two_calls_same_index_second_takes_over():
    """A 还没传完 B 就开始（同一 index=0）→ B 开新槽并接管该 index。

    注：B 开始后，不带 id 的后续块在协议上已无法区分属于谁——实现按"接管"处理，
    即后续块全归 B。所以这里只要求：**B 的参数完整可解析，且 A 没被污染**。
    """
    calls: list[dict] = []
    active: dict[int, int] = {}
    _accumulate(calls, tc(0, "call_a", "read", '{"path": "a'), active)
    _accumulate(calls, tc(0, "call_b", "write", '{"path": "b'), active)   # A 尚未完整
    _accumulate(calls, tc(0, args='.txt"}'), active)                      # 接管后属于 B

    assert len(calls) == 2, calls
    assert calls[0]["function"]["arguments"] == '{"path": "a'      # A 原样保留，没被拼进 B 的分块
    assert json.loads(calls[1]["function"]["arguments"]) == {"path": "b.txt"}
    assert calls[0]["id"] == "call_a" and calls[1]["id"] == "call_b"


def test_second_call_start_does_not_pollute_first():
    """最小复现：B 的第一块到达时 A 不完整，B 的参数不能进 A。"""
    calls: list[dict] = []
    active: dict[int, int] = {}
    _accumulate(calls, tc(0, "a", "read", '{"p":'), active)
    _accumulate(calls, tc(0, "b", "write", '{"q":'), active)
    assert len(calls) == 2, f"B 没有进新槽，被拼进 A 了：{calls}"
    assert calls[0]["id"] == "a" and calls[1]["id"] == "b"


def test_followup_without_id_goes_to_active_slot():
    """不带 id 的后续块，要追加到"当前活跃"的那个槽（b），不是 calls[0]。"""
    calls: list[dict] = []
    active: dict[int, int] = {}
    _accumulate(calls, tc(0, "a", "read", '{"p":'), active)
    _accumulate(calls, tc(0, "b", "write", '{"q":'), active)
    _accumulate(calls, tc(0, args='1}'), active)            # 无 id → 属于 b（接管该 index 的那个）
    assert json.loads(calls[1]["function"]["arguments"]) == {"q": 1}
    assert calls[0]["function"]["arguments"] == '{"p":'      # a 没被污染


# ---------------------------------------------------------------- 不能修坏原有行为


def test_normal_single_call_streaming():
    """普通单个调用：id 在第一块，参数分多块 → 拼成一个合法 JSON。"""
    calls: list[dict] = []
    active: dict[int, int] = {}
    _accumulate(calls, tc(0, "c1", "read", '{"path":'), active)
    _accumulate(calls, tc(0, args='"a.txt"}'), active)
    assert len(calls) == 1
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "a.txt"}
    assert calls[0]["id"] == "c1" and calls[0]["function"]["name"] == "read"


def test_two_calls_different_indices():
    """标准情况：两个调用 index 分别是 0 / 1，各自拼好。"""
    calls: list[dict] = []
    active: dict[int, int] = {}
    _accumulate(calls, tc(0, "c1", "read", '{"path":'), active)
    _accumulate(calls, tc(1, "c2", "write", '{"path":'), active)
    _accumulate(calls, tc(0, args='"a.txt"}'), active)
    _accumulate(calls, tc(1, args='"b.txt"}'), active)
    assert len(calls) == 2
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "a.txt"}
    assert json.loads(calls[1]["function"]["arguments"]) == {"path": "b.txt"}


def test_sequential_calls_same_index_after_complete():
    """同一 index 顺序下发两个**完整**调用（常见的中转站降级形式）→ 两个槽。"""
    calls: list[dict] = []
    active: dict[int, int] = {}
    _accumulate(calls, tc(0, "a", "read", '{"p":1}'), active)
    _accumulate(calls, tc(0, "b", "write", '{"q":2}'), active)
    assert len(calls) == 2
    assert json.loads(calls[0]["function"]["arguments"]) == {"p": 1}
    assert json.loads(calls[1]["function"]["arguments"]) == {"q": 2}


def test_no_index_falls_back_to_last():
    """完全没有 index 也没 id 的分块 → 追加到最后一个槽（原有兜底行为）。"""
    calls: list[dict] = []
    active: dict[int, int] = {}
    _accumulate(calls, tc(None, "c1", "read", '{"p":'), active)
    _accumulate(calls, tc(None, args='1}'), active)
    assert len(calls) == 1
    assert json.loads(calls[0]["function"]["arguments"]) == {"p": 1}
