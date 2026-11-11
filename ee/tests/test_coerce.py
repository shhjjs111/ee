"""不可信数值的转换（`ee/core/coerce.py`）与它的两个真实回归点。

为什么单独守这一层：`int()` / `float()` 遇到 `"abc"` / `None` 会抛异常，而这些值
有时候来自**用户手写的配置**或**会话文件**——一条坏值不该让整个 `thread/get` 崩掉
或让压缩水位算不出来。pi-lens 的 `unchecked-numeric-parse-python` 规则找的正是
裸 `int()`；集中用 `as_int` / `as_float` 之后调用点不再有这个问题。
"""
from __future__ import annotations

import asyncio

from ee.core.coerce import as_float, as_int


def test_as_int_keeps_valid_and_falls_back_on_garbage():
    assert as_int(7) == 7
    assert as_int("7") == 7          # 和 int("7") 一致：字符串数字照常认
    assert as_int(3.9) == 3          # 截断，与 int(3.9) 一致
    assert as_int("abc") == 0
    assert as_int(None) == 0
    assert as_int([], -1) == -1      # 给了默认值就用它
    assert as_int(float("inf"), 5) == 5   # 溢出也归默认值，不抛


def test_as_float_keeps_valid_and_falls_back_on_garbage():
    assert as_float(0.5) == 0.5
    assert as_float("0.25") == 0.25
    assert as_float("abc", 1.5) == 1.5
    assert as_float(None) == 0.0
    assert as_float([1], 2.0) == 2.0


def test_corrupt_seq_in_session_file_does_not_break_thread_get(ee_home, workspace):
    """会话文件里的 seq / hist 被写坏（老版本 / 手改 / 半行修复）时，打开会话不能 500。

    `Thread.__init__` 用 `max(int(it["seq"]) ...)` 算下一个序号，`detail(tail=...)` 也用
    `int(seq)` 做游标——一个 `"abc"` 就让整个 thread/get 抛 ValueError，用户看到的是
    「打不开这个对话」，而会话本身只是有个坏字段。
    """
    from ee.core.store import SessionStore
    from ee.server.app import Backend

    sid = "20261112-120000-abcdef"
    st = SessionStore(ee_home, sid)
    st.meta({"cwd": str(workspace), "title": "坏的 seq"})
    st.append("item", {"item": {"id": "us_1", "type": "user", "text": "hi", "seq": "abc"}})
    st.append("item", {"item": {"id": "as_2", "type": "assistant", "text": "yo", "seq": 2}})

    async def go():
        b = Backend(home=ee_home, persist=True)
        d = (await b.call("thread/get", {"thread_id": sid}))["thread"]
        assert [it["id"] for it in d["items"]] == ["us_1", "as_2"]
        # 分页游标走的是另一条 int(seq) 路径，也要能吃下坏值
        page = (await b.call("thread/get", {"thread_id": sid, "tail": 1}))["thread"]
        assert [it["id"] for it in page["items"]] == ["as_2"]

    asyncio.run(go())


def test_bad_numeric_config_value_does_not_break_compaction_limit(workspace):
    """配置里写错类型（`threshold = "abc"`）时按默认值算，而不是在压缩水位上抛异常。

    压缩水位在 `needs_compaction()` 里被每次采样前调用；它抛异常等于整个回合挂掉，
    而用户的错误只是一个手滑的配置值。
    """
    from ee.config import load_config
    from ee.core.session import Session

    cfg = load_config(workspace)
    cfg.data.setdefault("compact", {}).update({"auto": True, "threshold": "abc", "max_tokens": "0"})
    s = Session(cfg, cwd=workspace, persist=False)
    s._fresh_usage = True
    limit = s._compact_limit()
    assert limit > 0, "坏 threshold 不该让压缩水位变成 0（那会永远不压缩）"
    assert limit == int(0.8 * s.profile.context_window), f"没退回默认阈值：{limit}"
