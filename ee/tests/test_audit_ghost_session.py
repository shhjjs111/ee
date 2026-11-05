"""删除会话后不能被迟到的收尾把 jsonl 写回来（2026-11 审计修复）。

thread_delete 会 t.interrupt() 取消在飞回合，然后 unlink 会话文件。但取消清理跑在后面：
_turn 的 finally 调 _fill_missing（给未完成的 tool_call 补结果），_sample 的
CancelledError 分支补写已流出的 assistant 片段——两者都走 _append → store.message，
Session 自己的 SessionStore 没有任何"已删除"标记，于是把 sessions/<id>.jsonl 重新建出来。
重建的文件里没有 meta，下次打开 cwd 退回用户主目录，变成一条来历不明的「新对话」。
"""
import asyncio

import pytest

from ee.core.session import Session
from ee.core.store import SessionStore
from ee.server.app import Backend


@pytest.fixture
def backend(tmp_path):
    return Backend(home=tmp_path, persist=False)


def _bare_session(home, tid):
    """只带 history + store 的 Session，用来直接验写入口。"""
    s = object.__new__(Session)
    s.history = []
    s.store = SessionStore(home, session_id=tid)
    return s


def test_append_writes_before_delete(tmp_path):
    """对照：没删之前 _append 照常落盘（否则下面的用例没有意义）。"""
    s = _bare_session(tmp_path, "20261029-040404-aaaaaa")
    s._append({"role": "assistant", "content": "正常记录"})
    assert (tmp_path / "sessions" / "20261029-040404-aaaaaa.jsonl").is_file()


def test_append_after_delete_does_not_recreate_the_file(tmp_path):
    tid = "20261029-040405-bbbbbb"
    s = _bare_session(tmp_path, tid)

    s.mark_deleted()
    s._append({"role": "tool", "tool_call_id": "c1", "content": "取消时补写的结果"})

    path = tmp_path / "sessions" / f"{tid}.jsonl"
    assert not path.exists(), "删除后的收尾把会话文件重建了（幽灵会话）"
    assert s.history, "内存里照常追加，只是不落盘"


def test_without_mark_deleted_append_would_recreate(tmp_path):
    """反证：不关门时 unlink 之后一次 _append 就会把文件重建出来。

    这条记录的是被修掉的那条路径本身——删除会话时取消清理正好在 unlink 之后跑。
    """
    tid = "20261029-040407-dddddd"
    s = _bare_session(tmp_path, tid)
    s._append({"role": "user", "content": "先写一条"})
    path = tmp_path / "sessions" / f"{tid}.jsonl"
    path.unlink()                                   # 模拟 thread_delete 的 unlink
    s._append({"role": "tool", "tool_call_id": "c1", "content": "取消时补写"})
    assert path.exists(), "机制变了：_append 不再重建文件，这条反证需要重写"


def test_thread_delete_closes_the_session_before_unlink(backend):
    """thread/delete 必须在删文件前把会话的写入口关掉。"""
    tid = "20261029-040406-cccccc"
    SessionStore(backend.home, session_id=tid).message({"role": "user", "content": "你好"})

    class _BG:
        async def close(self):
            pass

    class _Session:
        def __init__(self):
            self.bg = _BG()
            self.closed = False

        def mark_deleted(self):
            self.closed = True

        async def aclose(self):
            pass

    class _Thread:
        meta: dict = {}
        deleted = False
        running = False

        def __init__(self):
            self.session = _Session()

        def interrupt(self):
            pass

    async def go():
        t = _Thread()
        backend.threads[tid] = t
        await backend.call("thread/delete", {"thread_id": tid})
        for _ in range(10):
            await asyncio.sleep(0)
        return t

    t = asyncio.run(go())
    assert t.session.closed, "thread/delete 没有关掉会话的写入口"
    assert not (backend.home / "sessions" / f"{tid}.jsonl").exists()
