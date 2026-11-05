"""事件循环不被同步 IO 卡住（2026-11 审计修复）。

thread/wait 收尾时对每个子会话调 status()，里面要跑 git status 子进程；
automations/run_now 直接在事件循环上读整份 automations.json 再整份写回
（同文件其它 RPC 全都走 asyncio.to_thread，只有它漏了）。
后果是等子会话结束时整个后端停一下：所有窗口的流式输出、审批、状态查询一起卡住。
"""
import asyncio
import time
from types import SimpleNamespace

from ee.server.orchestrator import Orchestrator


class _SlowStatusOrch(Orchestrator):
    """只跑 wait 需要的两个方法；status 故意慢，模拟 git 子进程。"""

    def __init__(self, delay: float = 0.15):
        self._delay = delay

    def _own_child(self, parent_id, child_id):
        return SimpleNamespace(id=child_id, running=False)

    def status(self, parent_id, child_id):
        time.sleep(self._delay)              # 同步阻塞，正是要测的东西
        return {"id": child_id}


async def _max_loop_gap(coro_factory, beat: float = 0.01) -> float:
    ticks: list[float] = []
    stop = asyncio.Event()

    async def heartbeat():
        while not stop.is_set():
            ticks.append(time.monotonic())
            await asyncio.sleep(beat)

    hb = asyncio.create_task(heartbeat())
    # 必须先让心跳真的跑起来几拍：否则 ticks 为空/只有一条，gaps 也算不出来，
    # 无论被阻塞多久都会返回 0.0 —— 那是假绿（第一版就是这么写的）。
    await asyncio.sleep(beat * 3)
    await coro_factory()
    # 阻塞期间心跳正卡在 sleep 里，醒来时 stop 已置位、不会记 tick —— 阻塞时长就漏掉了
    # （第二版就是这么假绿的）。这里补一拍收尾，把「被阻塞了多久」量进去。
    ticks.append(time.monotonic())
    stop.set()
    await hb
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert gaps, "心跳一次都没跑起来，这组测量无效"
    return max(gaps)


def test_wait_collects_child_status_off_the_event_loop():
    """等 3 个子会话、每个 status 慢 150ms：循环不该被卡住 450ms。"""
    orch = _SlowStatusOrch()

    gap = asyncio.run(_max_loop_gap(lambda: orch.wait("p1", ["c1", "c2", "c3"])))

    assert gap < 0.10, f"事件循环被同步 status() 卡住了 {gap:.2f}s"


# automations/run_now 同样在循环上做整份 JSON 读写，但**不能**简单套 to_thread：
# _launch 末尾要 asyncio.get_running_loop().create_task(...) 起执行任务，在工作线程里会
# RuntimeError（实测 14 个用例红）。要修得先把 _launch 拆成「落盘」与「调度」两段，
# 那属于重构，本次不做，已在 automations.run_now 处留了注释说明原因。
