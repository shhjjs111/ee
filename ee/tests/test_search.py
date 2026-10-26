"""D4-4：会话全文搜索（SQLite FTS5 trigram 索引 + thread/search）。"""
import os
import time

from test_server import make_backend

from ee.core.search_index import SearchIndex, snippet
from ee.core.store import SessionStore


def _session(home, sid, cwd, items, title=None):
    s = SessionStore(home, session_id=sid)
    s.meta({"cwd": str(cwd), "title": title or ""})
    for i, it in enumerate(items):
        s.item({"seq": i, **it})
    return s


def test_index_and_search(ee_home, workspace):
    _session(ee_home, "a", workspace, [
        {"id": "u1", "type": "user", "text": "帮我写一个快速排序"},
        {"id": "a1", "type": "assistant", "text": "好的，下面是 quicksort 的实现"},
        {"id": "t1", "type": "tool", "name": "shell", "summary": "运行 pytest", "output": "3 passed in 0.1s"},
    ], title="排序")
    _session(ee_home, "b", workspace, [{"id": "u1", "type": "user", "text": "整理一下读书笔记"}])
    idx = SearchIndex(ee_home)
    assert idx.sync() == 2
    assert idx.sync() == 0                      # 没变化不重建
    r = idx.search("quicksort")
    assert [g["thread_id"] for g in r] == ["a"] and r[0]["hits"][0]["item_id"] == "a1"
    assert "quicksort" in r[0]["hits"][0]["snippet"]
    assert [g["thread_id"] for g in idx.search("笔记")] == ["b"]          # 两个汉字：LIKE 回退
    assert [g["thread_id"] for g in idx.search("快速排序")] == ["a"]       # 中文 trigram
    assert idx.search("3 passed")[0]["hits"][0]["kind"] == "tool"
    assert idx.search("不存在的词语") == []
    assert idx.search("  ") == []
    # 追加内容 → 增量重建该会话
    time.sleep(0.01)
    s = SessionStore(ee_home, session_id="b")
    s.item({"id": "a2", "type": "assistant", "text": "已生成 markdown 大纲", "seq": 5})
    os.utime(s.path, None)
    assert [g["thread_id"] for g in idx.search("markdown")] == ["b"]
    # 截断（编辑重发）后旧内容不再命中
    s.append("truncate", {"from_seq": 5})
    assert idx.search("markdown") == []
    # 删除文件 → 从索引移除
    (ee_home / "sessions" / "a.jsonl").unlink()
    assert idx.search("quicksort") == []
    idx.close()


def test_snippet():
    t = "前面很多很多文字" * 10 + "关键词在这里" + "后面也有很多文字" * 10
    s = snippet(t, "关键词", radius=5)
    assert "关键词" in s and s.startswith("…") and s.endswith("…")
    assert snippet("abc", "zzz") == "abc"


def test_thread_search_rpc(ee_home, workspace, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    _session(ee_home, "x1", workspace, [{"id": "u1", "type": "user", "text": "配置 webpack 打包"}])
    _session(ee_home, "x2", other, [{"id": "u1", "type": "user", "text": "webpack 和 vite 对比"}])
    b, _, events = make_backend(ee_home, [])
    r = b._thread_search("webpack")
    assert {x["thread"]["id"] for x in r["results"]} == {"x1", "x2"}
    assert all(x["hits"] and "webpack" in x["hits"][0]["snippet"] for x in r["results"])
    r = b._thread_search("webpack", cwd=str(workspace))
    assert [x["thread"]["id"] for x in r["results"]] == ["x1"]
    b.thread_delete("x1")
    assert [x["thread"]["id"] for x in b._thread_search("webpack")["results"]] == ["x2"]
