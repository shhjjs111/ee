"""增量压缩的摘要输入必须包含上次的保留段（2026-11 审计修复）。

压缩后的历史是 [AGENTS, 用户原话, 摘要, 保留段]：保留段是原文、**还没进过摘要**。
再压一次时输入起点必须是「摘要的下一条」；用 after_summary() 当起点会从「上次保留段
的末尾」开始，保留段既不在摘要里，又会被 build_history 的 floor 挡在新历史之外——
工具输出、命令结果就这样永久离开上下文（磁盘 items 与检索索引里还在，但不会再自动进上下文）。
after_summary() 的正确用途是判断「摘要之后到底有没有新内容」（has_new_content）。
"""
import asyncio
import inspect
from types import SimpleNamespace

from ee.core import compaction as C
from ee.core.session import Session


def _hist():
    return [
        {"_kind": "agents", "role": "system", "content": "AGENTS"},
        {"role": "user", "content": "用户原话"},
        {"_kind": "summary", "role": "user", "content": "上次的摘要", "_retained_to": 5},
        {"role": "user", "content": "保留段-1"},
        {"role": "assistant", "content": "保留段-2"},
        {"role": "user", "content": "新内容"},
    ]


def test_after_summary_points_past_the_retained_segment():
    """钉住 after_summary 的语义：它给的是「保留段末尾」，所以不能当输入起点。"""
    hist = _hist()[:5]
    assert C.after_summary(hist, 2) == 5
    assert hist[C.after_summary(hist, 2):] == []          # 从那里开始什么都拿不到
    assert [m["content"] for m in hist[3:]] == ["保留段-1", "保留段-2"]


def test_incremental_compaction_starts_right_after_the_summary():
    """主路（换模型重建输入）与侧路兜底都必须从摘要的下一条开始。"""
    for fn in (Session._summarize, Session._summarize_side):
        code = "\n".join(ln for ln in inspect.getsource(fn).splitlines()
                         if not ln.lstrip().startswith("#"))
        assert "after_summary(" not in code, (
            f"{fn.__name__} 又拿 after_summary() 当摘要输入起点了：它返回的是保留段末尾，"
            "会把保留段排除在摘要之外（原文随后被 floor 挡住，永久丢失）")


class _FakeSession(Session):
    """只跑 _summarize_side 需要的部分。"""

    def __init__(self, hist, sink):
        self.history = hist
        self._sink = sink

    @property
    def profile(self):
        return SimpleNamespace(context_window=200_000)

    def role_spec(self, role):
        return SimpleNamespace(alias="compact", profile=SimpleNamespace(context_window=200_000))

    async def side_complete(self, prompt, body, role=""):
        self._sink.append(body)
        return "新摘要"


def test_side_request_includes_retained_segment():
    """侧路兜底请求里必须能看到保留段的原文。"""
    sink: list[str] = []
    s = _FakeSession(_hist(), sink)

    asyncio.run(s._summarize_side((2, "上次的摘要")))

    body = sink[0]
    assert "保留段-1" in body and "保留段-2" in body, "保留段没进摘要请求，会永久丢失"
    assert "新内容" in body
