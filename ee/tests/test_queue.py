"""两条消息队列：插话（steer）/ 完成后追加（follow）的送入时机、顺序、one/all、撤回、退回、审批、计划模式、无人值守。"""
import asyncio
import io
import json

import pytest
from conftest import FakeClient, finish, make_session, run, text_chunks, tool_chunks
from test_server import make_backend, wait_for

from ee.core import queue as Q
from ee.core.events import (ApprovalDecision, ApprovalRequest, QueueDelivered, QueueReturned, TextDelta, ToolStart,
                            TurnDone)
from ee.core.store import SessionStore
from ee.server.app import RpcError


def done(events):
    return [e for e in events if isinstance(e, TurnDone)][-1]


def users(call):
    return [m["content"].split("\n\n<context-update")[0] for m in call["messages"] if m["role"] == "user"
            and not str(m.get("content", "")).startswith("[")]


def last_user(call):
    return users(call)[-1]


async def drive(s, text, on_event):
    """跑一个回合，每个事件交给 on_event(ev, events)；审批事件需要 on_event 自己处理。"""
    events = []
    async for ev in s.run_turn(text):
        events.append(ev)
        r = on_event(ev, events)
        if asyncio.iscoroutine(r):
            await r
    return events


def set_modes(s, steer=None, follow=None):
    q = s.config.data.setdefault("queue", {})
    if steer:
        q["steering_mode"] = steer
    if follow:
        q["follow_up_mode"] = follow


# ------------------------------------------------------------------ 纯函数
def test_queue_basics():
    q = Q.InputQueue()
    a = q.add("插1", Q.STEER, "a")
    q.add("追1", Q.FOLLOW, "b")
    q.add("插2", Q.STEER, "c")
    assert q.has(Q.STEER) and q.has(Q.FOLLOW) and len(q) == 3
    assert [x.id for x in q.take(Q.STEER, "one")] == ["a"]
    assert [x.id for x in q.take(Q.STEER, "all")] == ["c"]
    assert q.withdraw("b").text == "追1" and q.withdraw("b") is None
    assert not q
    q.add("x", Q.FOLLOW, "d")
    q.add("y", Q.STEER, "e")
    assert [x.to_dict() for x in q.drain()] == [{"id": "d", "text": "x", "queue": "follow"},
                                                {"id": "e", "text": "y", "queue": "steer"}]
    assert a.kind == "steer"
    with pytest.raises(ValueError):
        q.add("z", "later")


def test_mode_settings_defaults_and_validation():
    assert Q.mode_settings({}) == {"steering_mode": "all", "follow_up_mode": "one"}
    assert Q.mode_settings({"queue": {"steering_mode": "ONE", "follow_up_mode": "bogus"}}) == \
        {"steering_mode": "one", "follow_up_mode": "one"}
    out = {}
    Q.settings_in({"queue": {"steering_mode": "one", "follow_up_mode": "all"}}, out)
    assert out == {"queue": {"steering_mode": "one", "follow_up_mode": "all"}}
    with pytest.raises(ValueError):
        Q.settings_in({"queue": {"steering_mode": "some"}}, {})


# ------------------------------------------------------------------ 送入时机与顺序
def test_steer_after_tool_step_follow_only_at_end(ee_home, workspace):
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        tool_chunks(0, "c2", "read", {"path": "a.txt", "limit": 1}) + finish("tool_calls"),
        text_chunks("第一件做完") + finish("stop"),
        text_chunks("追加的也做完") + finish("stop"),
    ])
    ids = {}

    def on(ev, _):
        if isinstance(ev, ToolStart) and ev.call_id == "c1":
            ids["f"] = s.queue_input("完成后再写个总结", Q.FOLLOW)     # 先到的是追加
            ids["s"] = s.queue_input("顺便看看 b.txt")                 # 后到的插话反而先送

    ev = run(drive(s, "读 a.txt", on))
    assert done(ev).reason == "completed" and len(fake.calls) == 4
    assert last_user(fake.calls[1]) == "顺便看看 b.txt"                # 工具步之后：只送插话
    assert all("总结" not in u for u in users(fake.calls[1]) + users(fake.calls[2]))   # 追加没提前送
    assert last_user(fake.calls[3]) == "完成后再写个总结"             # 模型不再调工具时才送追加
    delivered = [(e.ids, e.kind) for e in ev if isinstance(e, QueueDelivered)]
    assert delivered == [([ids["s"]], "steer"), ([ids["f"]], "follow")]
    assert not any(isinstance(e, QueueReturned) for e in ev)


def test_steer_at_turn_end_goes_before_follow(ee_home, workspace):
    s, fake = make_session(workspace, [text_chunks("回答一", n=1) + finish("stop"),
                                       text_chunks("回答二") + finish("stop"),
                                       text_chunks("回答三") + finish("stop")], delay=0.005)

    def on(ev, events):
        if isinstance(ev, TextDelta) and not hasattr(on, "done"):
            on.done = True
            s.queue_input("追加", Q.FOLLOW)
            s.queue_input("插话")

    ev = run(drive(s, "问", on))
    assert done(ev).reason == "completed" and len(fake.calls) == 3
    assert last_user(fake.calls[1]) == "插话" and last_user(fake.calls[2]) == "追加"


@pytest.mark.parametrize("mode,expect_calls", [("one", 3), ("all", 2)])
def test_follow_modes(ee_home, workspace, mode, expect_calls):
    s, fake = make_session(workspace, [text_chunks(f"回答{i}", n=1) + finish("stop") for i in range(3)], delay=0.005)
    set_modes(s, follow=mode)

    def on(ev, events):
        if isinstance(ev, TextDelta) and not hasattr(on, "done"):
            on.done = True
            s.queue_input("任务A", Q.FOLLOW)
            s.queue_input("任务B", Q.FOLLOW)

    ev = run(drive(s, "开始", on))
    assert done(ev).reason == "completed" and len(fake.calls) == expect_calls
    if mode == "one":
        assert last_user(fake.calls[1]) == "任务A" and last_user(fake.calls[2]) == "任务B"
    else:
        assert last_user(fake.calls[1]) == "任务A\n\n任务B"
        assert [len(e.ids) for e in ev if isinstance(e, QueueDelivered)] == [2]


@pytest.mark.parametrize("mode", ["one", "all"])
def test_steer_modes(ee_home, workspace, mode):
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        text_chunks("好") + finish("stop"),
        text_chunks("好的") + finish("stop"),
    ])
    set_modes(s, steer=mode)

    def on(ev, _):
        if isinstance(ev, ToolStart):
            s.queue_input("纠正一")
            s.queue_input("纠正二")

    ev = run(drive(s, "go", on))
    assert done(ev).reason == "completed"
    if mode == "all":
        assert len(fake.calls) == 2 and last_user(fake.calls[1]) == "纠正一\n\n纠正二"
    else:   # 逐条：第二条等模型停下时再送
        assert len(fake.calls) == 3
        assert last_user(fake.calls[1]) == "纠正一" and last_user(fake.calls[2]) == "纠正二"


# ------------------------------------------------------------------ 中断退回 / 撤回 / 回合结束退回
def test_interrupt_returns_queued_in_order(ee_home, workspace):
    s, fake = make_session(workspace, [text_chunks("很长很长的回答" * 5, n=1) + finish("stop"),
                                       text_chunks("新回合") + finish("stop")], delay=0.01)

    def on(ev, _):
        if isinstance(ev, TextDelta) and not hasattr(on, "done"):
            on.done = True
            s.queue_input("追加X", Q.FOLLOW, qid="f1")
            s.queue_input("插话Y", qid="s1")
            s.interrupt()

    ev = run(drive(s, "写长文", on))
    assert done(ev).reason == "interrupted"
    ret = [e for e in ev if isinstance(e, QueueReturned)]
    assert len(ret) == 1 and ev.index(ret[0]) == len(ev) - 2      # 紧挨在 TurnDone 前
    assert ret[0].items == [{"id": "f1", "text": "追加X", "queue": "follow"},
                            {"id": "s1", "text": "插话Y", "queue": "steer"}]
    assert not s.queue and s.pending == []
    run(drive(s, "下一句", lambda *_: None))
    assert all(u not in ("追加X", "插话Y") for u in users(fake.calls[1]))   # 没被偷偷带进下一回合


def test_withdraw(ee_home, workspace):
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        text_chunks("好") + finish("stop"),
    ])
    got = {}

    def on(ev, _):
        if isinstance(ev, ToolStart):
            q1 = s.queue_input("算了不要这句")
            s.queue_input("留下这句", qid="keep")
            got["w"] = s.withdraw_input(q1)
        if isinstance(ev, QueueDelivered):
            got["late"] = s.withdraw_input("keep")     # 已送入：撤不回

    ev = run(drive(s, "go", on))
    assert got["w"].text == "算了不要这句" and got["late"] is None
    assert last_user(fake.calls[1]) == "留下这句"
    assert done(ev).reason == "completed"


def test_max_steps_returns_unsent(ee_home, workspace):
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        text_chunks("最后总结", n=1) + finish("stop"),
    ], max_steps=2, delay=0.005)

    def on(ev, events):
        if isinstance(ev, TextDelta) and not hasattr(on, "done"):     # 最后一步（只许总结）里排进来的
            on.done = True
            s.queue_input("再做一件", Q.FOLLOW, qid="f")
            s.queue_input("插一句", qid="s")

    ev = run(drive(s, "go", on))
    assert done(ev).reason == "max_steps" and len(fake.calls) == 2
    assert [x["id"] for x in [e for e in ev if isinstance(e, QueueReturned)][0].items] == ["f", "s"]


# ------------------------------------------------------------------ 审批期间
def test_steer_during_approval_does_not_bypass_it(ee_home, workspace):
    cmd = "git commit -m x"
    s, fake = make_session(workspace, [
        tool_chunks(0, "c1", "shell", {"command": cmd}) + finish("tool_calls"),
        text_chunks("好的，不提交") + finish("stop"),
    ])
    seen = {}

    async def on(ev, events):
        if isinstance(ev, ApprovalRequest):
            s.queue_input("别提交，先跑测试")
            await asyncio.sleep(0.05)                      # 审批挂着：插话不能提前送，也不能让命令跑
            seen["hist_during"] = [m["role"] for m in s.history]
            seen["calls_during"] = len(fake.calls)
            ev.future.set_result(ApprovalDecision("deny"))

    ev = run(drive(s, "提交", on))
    assert done(ev).reason == "completed"
    assert seen["calls_during"] == 1 and seen["hist_during"] == ["user", "assistant"]
    roles = [m["role"] for m in fake.calls[1]["messages"]][-3:]
    assert roles == ["assistant", "tool", "user"]                     # 工具结果（被拒）在前，插话在后
    tool_msg = [m for m in fake.calls[1]["messages"] if m["role"] == "tool"][0]
    assert "拒绝" in tool_msg["content"]
    assert last_user(fake.calls[1]) == "别提交，先跑测试"


# ------------------------------------------------------------------ 计划模式 present_plan
def _plan_script(extra=()):
    return [tool_chunks(0, "p1", "present_plan", {"title": "t", "plan": "## 步骤\n1. 做"}) + finish("tool_calls"),
            *extra]


def test_present_plan_ends_turn_and_returns_follow(ee_home, workspace):
    s, fake = make_session(workspace, _plan_script(), mode="plan")

    def on(ev, _):
        if isinstance(ev, ToolStart):
            s.queue_input("批准后顺便更新 README", Q.FOLLOW, qid="f")

    ev = run(drive(s, "规划", on))
    assert done(ev).reason == "completed" and len(fake.calls) == 1      # 方案提交后等用户批准，不送追加
    assert [e for e in ev if isinstance(e, QueueReturned)][0].items[0]["id"] == "f"


def test_present_plan_with_steer_continues(ee_home, workspace):
    s, fake = make_session(workspace, _plan_script([text_chunks("已按意见调整") + finish("stop")]), mode="plan")

    def on(ev, _):
        if isinstance(ev, ToolStart):
            s.queue_input("方案里别动数据库")

    ev = run(drive(s, "规划", on))
    assert done(ev).reason == "completed" and len(fake.calls) == 2
    assert last_user(fake.calls[1]) == "方案里别动数据库"


# ------------------------------------------------------------------ Stop Hook：追加先于 Stop
def test_follow_delivered_before_stop_hook(ee_home, workspace, tmp_path):
    from test_hooks import HOOK_PY, cmd, read_log, write_user_hooks
    hook = tmp_path / "hook.py"
    hook.write_text(HOOK_PY, encoding="utf-8")
    log = tmp_path / "stop.log"
    write_user_hooks(ee_home, {"Stop": [{"command": cmd(hook, log, "stop-once", "还没跑测试")}]})
    s, fake = make_session(workspace, [text_chunks("A", n=1) + finish("stop"), text_chunks("B") + finish("stop"),
                                       text_chunks("C") + finish("stop")], delay=0.005)

    def on(ev, _):
        if isinstance(ev, TextDelta) and not hasattr(on, "done"):
            on.done = True
            s.queue_input("再加一件", Q.FOLLOW)

    ev = run(drive(s, "做事", on))
    assert done(ev).reason == "completed" and len(fake.calls) == 3
    assert last_user(fake.calls[1]) == "再加一件"
    rec = read_log(log)
    assert rec[0]["last_assistant_message"] == "B"      # Stop 只在追加的也做完后才跑


# ------------------------------------------------------------------ 服务端：条目、RPC、持久化
def test_server_queue_items_withdraw_and_return(ee_home, workspace):
    script = [text_chunks("第一段回答" * 4, n=1) + finish("stop"), text_chunks("追加完成") + finish("stop"),
              text_chunks("又一段很长的回答" * 4, n=1) + finish("stop")]

    async def go():
        b, fake, ev = make_backend(ee_home, script, delay=0.01)
        tid = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "你好"})
        await wait_for(ev, "item/delta")
        with pytest.raises(RpcError):
            await b.call("turn/start", {"thread_id": tid, "text": "x", "queue": "later"})
        rf = await b.call("turn/start", {"thread_id": tid, "text": "完成后总结一下", "queue": "follow"})
        rw = await b.call("turn/start", {"thread_id": tid, "text": "这句撤回"})
        assert rf["queued"] and rf["queue"] == "follow" and rw["queue"] == "steer"
        item = (await wait_for(ev, "item/started", lambda p: p["item"]["id"] == rf["item_id"]))["item"]
        assert item["queued"] is True and item["queue"] == "follow"
        w = await b.call("turn/withdraw", {"thread_id": tid, "item_id": rw["item_id"]})
        assert w == {"ok": True, "text": "这句撤回", "queue": "steer"}
        await wait_for(ev, "item/removed", lambda p: p["item_id"] == rw["item_id"])
        await wait_for(ev, "turn/completed")
        assert len(fake.calls) == 2 and last_user(fake.calls[1]) == "完成后总结一下"
        with pytest.raises(RpcError):      # 已送入
            await b.call("turn/withdraw", {"thread_id": tid, "item_id": rf["item_id"]})
        d = (await b.call("thread/get", {"thread_id": tid}))["thread"]
        kinds = [(it["type"], it.get("text"), it.get("queued"), it.get("queue")) for it in d["items"]
                 if it["type"] in ("user", "assistant")]
        # 送入后挪到第一段回答之后，不再标 queued，保留 queue 作标签；撤回的不见了
        assert kinds == [("user", "你好", None, None), ("assistant", "第一段回答" * 4, None, None),
                         ("user", "完成后总结一下", None, "follow"), ("assistant", "追加完成", None, None)]
        assert d["items"][0].get("hist") == 0

        # 中断：排队消息通过 turn/queue_returned 退回，条目移除
        ev.clear()
        await b.call("turn/start", {"thread_id": tid, "text": "再来"})
        await wait_for(ev, "item/delta")
        r2 = await b.call("turn/start", {"thread_id": tid, "text": "退回我", "queue": "follow"})
        await b.call("turn/interrupt", {"thread_id": tid})
        back = await wait_for(ev, "turn/queue_returned")
        assert back["items"] == [{"id": r2["item_id"], "text": "退回我", "queue": "follow"}]
        await wait_for(ev, "item/removed", lambda p: p["item_id"] == r2["item_id"])
        order = [m for m, _ in ev]
        assert order.index("turn/queue_returned") < order.index("turn/completed")
        d = (await b.call("thread/get", {"thread_id": tid}))["thread"]
        assert all(it.get("text") not in ("退回我", "这句撤回") for it in d["items"])
        # 落盘：撤回 / 退回的没留痕，送入的有
        saved = SessionStore.load(ee_home / "sessions" / f"{tid}.jsonl")["items"]
        texts = [it.get("text") for it in saved if it["type"] == "user"]
        assert texts == ["你好", "完成后总结一下", "再来"]
    asyncio.run(go())


def test_queue_before_turn_start_keeps_hist_on_real_message(ee_home, workspace):
    script = [text_chunks("答") + finish("stop"), text_chunks("答2") + finish("stop")]

    async def go():
        b, fake, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
        r1 = await b.call("turn/start", {"thread_id": tid, "text": "第一句"})
        r2 = await b.call("turn/start", {"thread_id": tid, "text": "马上补一句"})     # 回合还没真正开始就排队
        assert r2["queued"]
        await wait_for(ev, "turn/completed")
        d = (await b.call("thread/get", {"thread_id": tid}))["thread"]
        first = next(it for it in d["items"] if it["id"] == r1["item_id"])
        assert first.get("hist") == 0
    asyncio.run(go())


def test_old_persisted_queued_flag_is_cleared(ee_home, workspace):
    from ee.server.threads import Thread
    items = [{"id": "u1", "type": "user", "text": "a", "seq": 1},
             {"id": "u2", "type": "user", "text": "b", "seq": 2, "queued": True}]
    th = Thread("t1", ee_home, lambda m, p: None, {"cwd": str(workspace)}, items=items, persist=False)
    assert all("queued" not in it for it in th.items)


def test_settings_queue_modes(ee_home):
    from ee.server import settings as S
    assert S.get_settings()["settings"]["queue"] == {"steering_mode": "all", "follow_up_mode": "one"}
    S.set_settings({"queue": {"steering_mode": "one", "follow_up_mode": "all"}})
    assert S.get_settings()["settings"]["queue"] == {"steering_mode": "one", "follow_up_mode": "all"}
    with pytest.raises(ValueError):
        S.set_settings({"queue": {"follow_up_mode": "many"}})


# ------------------------------------------------------------------ 无人值守（ee exec 的线程类）
def test_exec_thread_with_queued_follow(ee_home, workspace):
    from ee import headless
    from ee.core.providers.openai_chat import ChatClient

    fake = FakeClient([tool_chunks(0, "c1", "shell", {"command": "git commit -m x"}) + finish("tool_calls"),
                       text_chunks("主任务完成") + finish("stop"),
                       text_chunks("追加任务完成") + finish("stop")])

    async def go():
        cls = headless._make_thread_class()
        meta = {"cwd": str(workspace), "model": "", "mode": "default", "title": "新对话", "source": "exec"}
        th = cls("exec-q", ee_home, lambda m, p: None, meta, persist=False,
                 client_factory=lambda spec: ChatClient(spec, client=fake),
                 configure=headless.unattended_configure(None, (), ()))
        out = io.StringIO()
        th.printer = headless.Printer(True, False, True, out, io.StringIO())
        th.approve = "deny"
        await th.ensure_session_async()
        th.start_turn("提交代码")
        th.start_turn("然后写说明", queue="follow")
        await th.task
        evs = [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]
        approvals = [e for e in evs if e["type"] == "approval"]
        assert approvals and approvals[0]["decision"] == "denied"   # 无人值守照样自动拒绝，不因排队绕过
        assert headless._last_text(th) == "追加任务完成"
        assert th.items[-1]["type"] == "turn_end" and th.items[-1]["reason"] == "completed"
        assert last_user(fake.calls[2]) == "然后写说明"
    asyncio.run(go())
