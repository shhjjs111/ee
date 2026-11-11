"""慢客户端不能把后端内存拖高（2026-11 审计 P2#17）。

每条连接一个 `asyncio.Queue()`，没有任何 maxsize，广播只有一个 `put_nowait`：
客户端读得慢（窗口失焦被浏览器节流、机器休眠、渲染进程卡住）时，队列会一直涨。
而一条 `thread/updated` 带着整个会话摘要，几十 KB 不稀奇——几百条积压就是几十 MB，
且这条连接永远不会被回收（叠加 P1#6 的保活缺口时更明显）。

处理策略：**队列满就断开这条连接**，让它重连后重新拉全量状态。
丢几条通知反而会让界面停在错误状态上；断开重连是自愈的。
"""
import asyncio

from ee.server.app import _enqueue_or_drop


def test_slow_connection_is_dropped_not_buffered():
    ws = object()
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    q.put_nowait("first")                 # 模拟客户端还没消费
    conns = {ws: q}

    slow = _enqueue_or_drop(conns, "second")

    assert slow == [ws], "队列满了应当判定为慢客户端"
    assert ws not in conns, "慢客户端要从连接表里移除，否则每轮广播都白试一次"


def test_fast_connection_keeps_receiving():
    ws = object()
    conns = {ws: asyncio.Queue(maxsize=8)}

    slow = _enqueue_or_drop(conns, "msg")

    assert slow == []
    assert ws in conns
    assert conns[ws].get_nowait() == "msg"


def test_one_slow_connection_does_not_block_others():
    slow_ws, fast_ws = object(), object()
    slow_q: asyncio.Queue = asyncio.Queue(maxsize=1)
    slow_q.put_nowait("pending")
    conns = {slow_ws: slow_q, fast_ws: asyncio.Queue(maxsize=8)}

    dropped = _enqueue_or_drop(conns, "msg")

    assert dropped == [slow_ws]
    assert list(conns) == [fast_ws], "快的连接不能受慢的牵连"

def test_handler_actually_uses_bounded_queue():
    """接线断言：handler 里必须建的是有界队列，否则上面三条守不住。

    只测 _enqueue_or_drop 是不够的——把 handler 里改回 `asyncio.Queue()`（无上限），
    QueueFull 永远不会发生，溢出分支就成了死代码。
    """
    import inspect

    from ee.server import app as A

    src = inspect.getsource(A.serve)
    assert "asyncio.Queue(maxsize=SEND_QUEUE_MAX)" in src
    assert A.SEND_QUEUE_MAX > 0


def test_response_queue_full_is_reported_not_raised():
    """响应队列满不能抛 QueueFull 在一个没人 await 的任务里。

    通知满了是丢通知（可接受），响应丢了客户端只能等 20 秒 RPC 超时，
    所以 `_put_response` 返回 False 让调用方断开这条连接。
    """
    from ee.server.app import _put_response

    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    assert _put_response(q, "ok") is True
    assert _put_response(q, "second") is False          # 满了：明确告知，不抛异常
    assert q.get_nowait() == "ok"


def test_single_response_path_uses_put_response():
    """接线断言：serve 里发响应必须走 `_put_response`（否则慢客户端会静默丢响应）。"""
    import inspect

    from ee.server import app as A

    src = inspect.getsource(A.serve)
    assert "_put_response(q, payload)" in src
    assert "q.put_nowait(json.dumps(out" not in src
