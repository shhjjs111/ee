"""D5-3 会话内 /loop：解析（间隔 / 每天 / 工作日 / 默认 10 分钟 / 前后缀写法）、间隔型立即发一次、到点会话忙就等
这一回合结束再发（多次到点只补一次）、list/stop、删会话清掉循环、不持久化。"""
import asyncio
from datetime import datetime

import pytest
from conftest import finish, text_chunks
from test_automations import make_backend, ts

from ee.core import loopspec as LS
from ee.server.app import RpcError


# ------------------------------------------------------------------ 解析
@pytest.mark.parametrize("text,sched,prompt", [
    ("10m 检查CI是否通过", {"kind": "interval", "minutes": 10}, "检查CI是否通过"),
    ("每10分钟 检查CI", {"kind": "interval", "minutes": 10}, "检查CI"),
    ("每 2 小时 看看日志", {"kind": "interval", "minutes": 120}, "看看日志"),
    ("2h 看看日志", {"kind": "interval", "minutes": 120}, "看看日志"),
    ("every 5m run tests", {"kind": "interval", "minutes": 5}, "run tests"),
    ("1d 汇总", {"kind": "interval", "minutes": 1440}, "汇总"),
    ("检查部署状态 every 15m", {"kind": "interval", "minutes": 15}, "检查部署状态"),
    ("检查部署状态 每30分钟", {"kind": "interval", "minutes": 30}, "检查部署状态"),
    ("每天 9:00 总结昨天的提交", {"kind": "daily", "time": "09:00", "weekdays": []}, "总结昨天的提交"),
    ("每天9点 总结", {"kind": "daily", "time": "09:00", "weekdays": []}, "总结"),
    ("每天 21点30 总结", {"kind": "daily", "time": "21:30", "weekdays": []}, "总结"),
    ("每天 9：05 总结", {"kind": "daily", "time": "09:05", "weekdays": []}, "总结"),
    ("工作日 9:30 站会提醒", {"kind": "daily", "time": "09:30", "weekdays": [1, 2, 3, 4, 5]}, "站会提醒"),
    ("检查CI是否通过", {"kind": "interval", "minutes": 10}, "检查CI是否通过"),       # 不写间隔：默认 10 分钟
])
def test_parse(text, sched, prompt):
    spec = LS.parse(text)
    assert spec.schedule == sched and spec.prompt == prompt


def test_parse_seconds_round_up_and_errors():
    s = LS.parse("30s 看一眼")
    assert s.schedule == {"kind": "interval", "minutes": 1} and "1 分钟" in s.note
    s = LS.parse("90s 看一眼")
    assert s.schedule["minutes"] == 2
    for bad in ["", "10m", "0m 检查", "每天 25:00 检查", "每天 9:61 检查", "8d 检查"]:
        with pytest.raises(LS.LoopSpecError):
            LS.parse(bad)


# ------------------------------------------------------------------ 运行
class LoopClock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


async def setup(ee_home, workspace, script, delay=0.0):
    b, fake, _ = make_backend(ee_home, script, delay=delay)
    clock = LoopClock(ts(2026, 10, 27, 8, 0))
    b.loops.clock = clock
    th = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
    return b, fake, clock, th["id"]


async def idle(b, tid):
    t = b.threads[tid]
    for _ in range(400):
        if not t.running:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("回合没结束")


def user_texts(b, tid):
    return [it["text"] for it in b.threads[tid].items if it["type"] == "user"]


def test_interval_loop_fires_now_and_on_schedule(ee_home, workspace):
    async def main():
        b, fake, clock, tid = await setup(ee_home, workspace, [text_chunks("CI 还在跑") + finish("stop")] * 3)
        seen = []
        b.listeners.append(lambda m, p: seen.append((m, p)))
        res = await b.call("loop/create", {"thread_id": tid, "text": "10m 检查CI是否通过"})
        lp = res["loop"]
        assert lp["schedule_text"] == "每 10 分钟" and lp["prompt"] == "检查CI是否通过" and lp["id"] == "1"
        assert lp["next_run"] == clock.t + 600 and lp["runs"] == 1           # 立即发了一次
        await idle(b, tid)
        assert user_texts(b, tid) == ["检查CI是否通过"]
        assert b.threads[tid].items[0].get("kind") == "loop"
        clock.t += 300
        assert await b.loops.tick_once() == []
        clock.t += 300
        assert [x["id"] for x in await b.loops.tick_once()] == ["1"]
        await idle(b, tid)
        assert user_texts(b, tid) == ["检查CI是否通过"] * 2
        assert any(m == "loop/updated" for m, _ in seen)
    asyncio.run(main())


def test_busy_session_defers_until_turn_done_and_coalesces(ee_home, workspace):
    async def main():
        script = [text_chunks("用户的长任务做完了", n=1) + finish("stop"), text_chunks("CI 通过") + finish("stop")]
        b, fake, clock, tid = await setup(ee_home, workspace, script, delay=0.02)
        await b.call("turn/start", {"thread_id": tid, "text": "做个长任务"})
        lp = (await b.call("loop/create", {"thread_id": tid, "text": "每天 9:00 检查CI"}))["loop"]
        assert lp["runs"] == 0 and datetime.fromtimestamp(lp["next_run"]) == datetime(2026, 10, 27, 9, 0)
        clock.t = ts(2026, 10, 27, 9, 0, 1)
        await b.loops.tick_once()
        cur = (await b.call("loop/list", {"thread_id": tid}))["loops"][0]
        assert cur["pending"] is True
        clock.t = ts(2026, 10, 28, 9, 0, 1)                    # 还忙着又到点：不重复排
        await b.loops.tick_once()
        assert (await b.call("loop/list", {"thread_id": tid}))["loops"][0]["skipped"] == 1
        await idle(b, tid)
        for _ in range(100):
            await asyncio.sleep(0.02)
            if len(user_texts(b, tid)) == 2:
                break
        await idle(b, tid)
        assert user_texts(b, tid) == ["做个长任务", "检查CI"]
        cur = (await b.call("loop/list", {"thread_id": tid}))["loops"][0]
        assert cur["pending"] is False and cur["runs"] == 1
        assert not any(it.get("queued") for it in b.threads[tid].items)   # 不是插话，是新的一回合
    asyncio.run(main())


def test_list_stop_and_thread_delete(ee_home, workspace):
    async def main():
        b, fake, clock, tid = await setup(ee_home, workspace, [])
        await b.call("loop/create", {"thread_id": tid, "text": "每天 9:00 a"})
        await b.call("loop/create", {"thread_id": tid, "text": "工作日 18:00 b"})
        ls = (await b.call("loop/list", {"thread_id": tid}))["loops"]
        assert [x["id"] for x in ls] == ["1", "2"] and ls[1]["schedule_text"] == "工作日 18:00"
        assert (await b.call("loop/stop", {"thread_id": tid, "id": "1"}))["stopped"] == ["1"]
        with pytest.raises(RpcError, match="没有"):
            await b.call("loop/stop", {"thread_id": tid, "id": "9"})
        th2 = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
        with pytest.raises(RpcError, match="没有"):
            await b.call("loop/stop", {"thread_id": th2, "id": "2"})            # 别的会话的循环停不了
        await b.call("loop/create", {"thread_id": th2, "text": "每天 10:00 c"})
        await b.call("thread/delete", {"thread_id": tid})
        assert [x["thread_id"] for x in (await b.call("loop/list", {}))["loops"]] == [th2]
        assert (await b.call("loop/stop", {"thread_id": th2, "id": "all"}))["stopped"] == ["3"]
        with pytest.raises(RpcError, match="提示"):
            await b.call("loop/create", {"thread_id": th2, "text": "10m"})
        for i in range(10):
            await b.call("loop/create", {"thread_id": th2, "text": f"每天 10:{i:02d} x"})
        with pytest.raises(RpcError, match="最多"):
            await b.call("loop/create", {"thread_id": th2, "text": "每天 11:00 x"})
        # 不持久化：新后端里没有
        b2, _, _ = make_backend(ee_home, [])
        assert (await b2.call("loop/list", {}))["loops"] == []
    asyncio.run(main())


def test_loop_scheduler_task_runs(ee_home, workspace):
    async def main():
        b, fake, clock, tid = await setup(ee_home, workspace, [text_chunks("ok") + finish("stop")] * 2)
        b.loops.tick = 0.05
        b.loops.start()
        try:
            await b.call("loop/create", {"thread_id": tid, "text": "1m 看看"})
            await idle(b, tid)
            clock.t += 61
            b.loops.poke()
            for _ in range(100):
                await asyncio.sleep(0.03)
                if len(user_texts(b, tid)) == 2:
                    break
            assert len(user_texts(b, tid)) == 2
        finally:
            await b.loops.stop()
            await idle(b, tid)
    asyncio.run(main())
