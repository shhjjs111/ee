"""`DiagStore` 的文档上限（LRU）：长会话不能把看过的每个文件全文都攒着。

`Doc.text` 是整份文件内容。一次会话里模型可能查几十上百个文件的诊断；没有上限时
`_docs` 只增不减，等于给每个查过的文件留一份全文副本。淘汰后 `doc(uri)` 返回 None，
`LspTool` 会重新 didOpen——缓存因此永远只对应「还在手里」的文件，不会指向旧内容。
"""
from __future__ import annotations

import asyncio

import pytest

from ee.core.lsp import diagnostics as D

# 复用 test_lsp_lazy 的会话夹具（它自带假语言服务器与 S.resolve 打桩）。
# 不能直接把同名 fixture import 进来：测试函数的参数也叫 lsp_session，ruff 会报 F811
# （同一名字在模块里被重定义）。包一层同名 fixture，依赖名换个前缀就没这个问题。
from test_lsp_lazy import _ctx, lsp_session as _lazy_lsp_session  # noqa: F401  作为下面 fixture 的依赖


@pytest.fixture
def lsp_session(_lazy_lsp_session):  # noqa: F811  ruff 把 fixture 参数当成重定义
    return _lazy_lsp_session


def _open(st: D.DiagStore, uri: str, text: str = "x = 1\n") -> None:
    st.open(D.Doc(uri=uri, text=text))


def test_docs_are_capped_by_lru():
    st = D.DiagStore(max_docs=3)
    for u in ("file:///a.py", "file:///b.py", "file:///c.py"):
        _open(st, u)
    assert len(st._docs) == 3

    st.get("file:///a.py")            # 读一下 a：a 变成最近使用
    _open(st, "file:///d.py")         # 第 4 个文档 → 淘汰最久没用的 b

    assert st.doc("file:///b.py") is None, "最久没用的文档没有被淘汰"
    assert st.doc("file:///a.py") is not None and st.doc("file:///d.py") is not None
    assert len(st._docs) == 3


def test_evicted_doc_reads_as_not_open_and_drops_results():
    st = D.DiagStore(max_docs=1)
    st.open(D.Doc(uri="file:///a.py", text="x = 1\n"))
    st.on_publish({"uri": "file:///a.py", "version": 1,
                   "diagnostics": [{"message": "x", "severity": 1}]})
    assert st.get("file:///a.py").status == D.FRESH

    _open(st, "file:///b.py")

    assert st.doc("file:///a.py") is None
    res = st.get("file:///a.py")
    assert res.status == D.NOT_OPEN and res.diagnostics == [], "淘汰后还留着旧诊断，会被当成当前版本"
    assert st.get("file:///b.py").status == D.NOT_OPEN


def test_open_and_change_keep_the_most_recent_doc():
    st = D.DiagStore(max_docs=2)
    _open(st, "file:///a.py")
    _open(st, "file:///b.py")
    st.change("file:///a.py", "x = 2\n")      # 修改 a：a 变最近
    _open(st, "file:///c.py")

    assert st.doc("file:///a.py") is not None, "刚改过的文档被淘汰了"
    assert st.doc("file:///b.py") is None
    assert st.doc("file:///c.py") is not None


def test_default_cap_is_positive_and_sane():
    st = D.DiagStore()
    assert st.max_docs == D.DEFAULT_MAX_DOCS > 0
    assert D.DiagStore(max_docs=0).max_docs == 1, "上限为 0 会让每个文件都立刻被淘汰（等于不可用）"


def test_tool_reopens_an_evicted_file_instead_of_serving_stale(lsp_session, workspace):
    """工具层：文件被 LRU 淘汰后，再问它必须重新取，而不是返回旧结果。"""
    from ee.core.lsp import diagnostics as D2

    s, started, _mgr = lsp_session
    s.lsp_store.max_docs = 1                  # 只留一个文档，第二次调用就会挤掉第一个
    (workspace / "a.py").write_text("a = bad\n", encoding="utf-8")
    (workspace / "b.py").write_text("b = 1\n", encoding="utf-8")
    tool = s.tool_map["lsp"]

    async def go():
        r1 = await tool.run({"path": "a.py"}, _ctx(s, workspace))
        assert r1.meta.get("status") == D2.FRESH, r1.output
        r2 = await tool.run({"path": "b.py"}, _ctx(s, workspace))
        assert r2.meta.get("status") == D2.FRESH, r2.output
        assert s.lsp_store.doc("file:///" + str(workspace / "a.py").replace("\\", "/")) is None
        r3 = await tool.run({"path": "a.py"}, _ctx(s, workspace))
        assert r3.meta.get("status") == D2.FRESH, r3.output
        assert "bad" in r3.output, "被淘汰的文件没有重新取诊断"

    asyncio.run(go())
    assert len(started) >= 3, f"每次重新打开都该取一次服务器，实际 {len(started)}"
