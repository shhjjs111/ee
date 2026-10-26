"""D5-2 子代理并行 / 工具读写闸门。

假模型按“谁在请求”分路：子代理请求里带 "[子代理任务]" 和任务名，其余是主会话。
每一路有自己的脚本和延迟，并记录每次流式输出的开始/结束时间，用来判断是否真的并发。
"""
import asyncio
import json
import shutil
import subprocess
import time

import pytest
from conftest import FakeError, finish, text_chunks, tool_chunks

from ee.core import parallel
from ee.core.events import (ApprovalDecision, ApprovalRequest, Notice, SubagentEnd, SubagentStart, SubagentUpdate,
                            ToolEnd, ToolStart, TurnDone)
from ee.tools.base import Tool, ToolResult


class RouterClient:
    def __init__(self, routes: dict, delays: dict | None = None):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.delays = delays or {}
        self.calls: list[tuple[str, dict]] = []
        self.spans: dict[str, list[list[float]]] = {}
        self.active = 0
        self.peak = 0
        outer = self

        class _Completions:
            async def create(self, **params):
                key = outer.route(params)
                outer.calls.append((key, json.loads(json.dumps(params, ensure_ascii=False, default=str))))
                if not outer.routes.get(key):
                    raise AssertionError(f"路由 {key} 的脚本用完了")
                item = outer.routes[key].pop(0)
                if isinstance(item, Exception):
                    raise item
                return _Stream(outer, key, item, outer.delays.get(key, 0.0))

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()

    def route(self, params) -> str:
        for m in params["messages"]:
            if m.get("role") == "user" and isinstance(m.get("content"), str) and "[子代理任务]" in m["content"]:
                for k in self.routes:
                    if k != "parent" and f"## 子任务\n{k}" in m["content"]:
                        return k
        return "parent"


class _Stream:
    def __init__(self, outer, key, chunks, delay):
        self.outer, self.key, self.chunks, self.delay = outer, key, chunks, delay

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        o = self.outer
        span = [time.monotonic(), 0.0]
        o.spans.setdefault(self.key, []).append(span)
        o.active += 1
        o.peak = max(o.peak, o.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            for c in self.chunks:
                yield c
        finally:
            o.active -= 1
            span[1] = time.monotonic()

    async def close(self):
        pass


def session(workspace, client, mode="default", subagent=None):
    from ee.config import load_config
    from ee.core.session import Session
    cfg = load_config(workspace)
    if subagent:
        cfg.data.setdefault("subagent", {}).update(subagent)
    return Session(cfg, cwd=workspace, mode=mode, client=client, persist=False)


async def collect(s, text, approve=None):
    evs = []
    async for ev in s.run_turn(text):
        evs.append(ev)
        if isinstance(ev, ApprovalRequest):
            ev.future.set_result(approve(ev) if approve else ApprovalDecision("once"))
    return evs


def run(c, timeout=30):
    return asyncio.run(asyncio.wait_for(c, timeout))


def task_call(i, cid, name, agent="explore", **kw):
    return tool_chunks(i, cid, "task", {"description": name, "prompt": name, "agent": agent, **kw})


def report(text):
    return text_chunks(text) + finish("stop")


def overlap(a, b) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def tool_results(s):
    return [(m["tool_call_id"], m["content"]) for m in s.history if m["role"] == "tool"]


# ------------------------------------------------------------------ 读写闸门（纯单元）

def test_rwgate_fifo_read_write_order():
    log = []

    async def worker(gate, kind, name, dur):
        async with gate.hold(kind):
            log.append(("start", name))
            await asyncio.sleep(dur)
            log.append(("end", name))

    async def go():
        gate = parallel.RWGate()
        ts = [asyncio.ensure_future(worker(gate, k, n, d)) for k, n, d in
              [("read", "r1", 0.05), ("read", "r2", 0.08), ("write", "w1", 0.03), ("read", "r3", 0.01),
               ("write", "w2", 0.01), ("free", "f1", 0.01)]]
        await asyncio.gather(*ts)
        return gate
    gate = asyncio.run(go())
    pos = {e: i for i, e in enumerate(log)}
    # r1、r2 并发；w1 等两个读完才开始；r3 在 w1 之后（先到的写挡住后到的读）；w2 在 r3 之后；free 不排队
    assert pos[("start", "r2")] < pos[("end", "r1")]
    assert pos[("start", "w1")] > max(pos[("end", "r1")], pos[("end", "r2")])
    assert pos[("start", "r3")] > pos[("end", "w1")]
    assert pos[("start", "w2")] > pos[("end", "r3")]
    assert pos[("start", "f1")] < pos[("end", "r1")]
    assert gate.readers == 0 and not gate.writer


def test_rwgate_cancel_while_waiting_releases_queue():
    async def go():
        gate = parallel.RWGate()
        await gate.acquire("write")
        waiter = asyncio.ensure_future(gate.acquire("write"))
        reader = asyncio.ensure_future(gate.acquire("read"))
        await asyncio.sleep(0.01)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        gate.release("write")
        await asyncio.wait_for(reader, 1)       # 被取消的写不再挡住后面的读
        assert gate.readers == 1
        gate.release("read")
        assert gate.readers == 0 and not gate.writer
    asyncio.run(go())


# ------------------------------------------------------------------ 普通工具：只读并发、写独占且保序

class _Slow(Tool):
    def __init__(self, name, read_only, log, dur=0.2):
        self.name, self.read_only, self.log, self.dur = name, read_only, log, dur
        self.description = name
        self.parameters = {"type": "object", "properties": {"n": {"type": "string"}}}

    def summary(self, args):
        return f"{self.name} {args.get('n')}"

    async def run(self, args, ctx):
        self.log.append(("start", args["n"], time.monotonic()))
        await asyncio.sleep(self.dur)
        self.log.append(("end", args["n"], time.monotonic()))
        return ToolResult(f"done {args['n']}")


def test_plain_tools_read_parallel_write_exclusive(ee_home, workspace):
    log = []
    calls = []
    order = [("slow_r", "r1"), ("slow_r", "r2"), ("slow_w", "w1"), ("slow_r", "r3"), ("slow_w", "w2"),
             ("slow_r", "r4")]
    for i, (name, n) in enumerate(order):
        calls += tool_chunks(i, f"c{i}", name, {"n": n})
    client = RouterClient({"parent": [calls + finish("tool_calls"), report("好")]})
    s = session(workspace, client, mode="auto")
    for t in (_Slow("slow_r", True, log), _Slow("slow_w", False, log)):
        s.tools.append(t)
        s.tool_map[t.name] = t
    ev = run(collect(s, "go"))
    assert [e for e in ev if isinstance(e, TurnDone)][-1].reason == "completed"
    span = {}
    for kind, n, ts in log:
        span.setdefault(n, [0, 0])[0 if kind == "start" else 1] = ts
    assert overlap(span["r1"], span["r2"])                       # 读并发
    assert span["w1"][0] >= max(span["r1"][1], span["r2"][1])    # 写等前面的读
    assert span["r3"][0] >= span["w1"][1]                        # 后面的读等写
    assert span["w2"][0] >= span["r3"][1] and span["r4"][0] >= span["w2"][1]
    # 结果按原调用顺序回填
    assert [cid for cid, _ in tool_results(s)] == [f"c{i}" for i in range(len(order))]
    assert [c for _, c in tool_results(s)] == [f"done {n}" for _, n in order]


def test_read_write_read_in_one_step_sees_write(ee_home, workspace):
    (workspace / "a.txt").write_text("old\n", encoding="utf-8")
    calls = (tool_chunks(0, "c0", "read", {"path": "a.txt"}) +
             tool_chunks(1, "c1", "write", {"path": "a.txt", "content": "new\n"}) +
             tool_chunks(2, "c2", "read", {"path": "a.txt"}))
    client = RouterClient({"parent": [calls + finish("tool_calls"), report("好")]})
    s = session(workspace, client, mode="auto")
    run(collect(s, "go"))
    res = dict(tool_results(s))
    assert "old" in res["c0"] and "new" in res["c2"]


# ------------------------------------------------------------------ 只读子代理并发

def test_readonly_subagents_run_concurrently_and_keep_order(ee_home, workspace):
    names = ["甲", "乙", "丙"]
    parent = sum((task_call(i, f"p{i}", n) for i, n in enumerate(names)), []) + finish("tool_calls")
    client = RouterClient({"parent": [parent, report("汇总完毕")],
                           **{n: [report(f"{n}的报告")] for n in names}},
                          delays={n: 0.8 for n in names})
    s = session(workspace, client)
    t0 = time.monotonic()
    ev = run(collect(s, "go"))
    total = time.monotonic() - t0
    sp = [client.spans[n][0] for n in names]
    assert overlap(sp[0], sp[1]) and overlap(sp[1], sp[2])
    assert total < 2.0                       # 串行要 2.4s 以上
    res = tool_results(s)
    assert [cid for cid, _ in res] == ["p0", "p1", "p2"]
    for (cid, out), n in zip(res, names):
        assert out.startswith('<task_result agent="explore" status="completed"') and f"{n}的报告" in out
    starts = [e for e in ev if isinstance(e, SubagentStart)]
    assert {e.call_id for e in starts} == {"p0", "p1", "p2"} and all(e.mode == "read-only" for e in starts)
    # 每个子代理的事件都带着自己的 call_id；用量实时并入主会话
    ends = {e.call_id: e for e in ev if isinstance(e, SubagentEnd)}
    assert all(ends[c].status == "completed" and ends[c].summary.endswith(f"{n}的报告")
               for c, n in zip(["p0", "p1", "p2"], names))
    ups = [e for e in ev if isinstance(e, SubagentUpdate) and e.kind == "usage"]
    assert {u.call_id for u in ups} == {"p0", "p1", "p2"} and ups[-1].totals["requests"] >= 4
    assert s.stats.requests == 5


def test_max_parallel_limits_and_queues(ee_home, workspace):
    names = ["一", "二", "三", "四"]
    parent = sum((task_call(i, f"p{i}", n) for i, n in enumerate(names)), []) + finish("tool_calls")
    client = RouterClient({"parent": [parent, report("好")], **{n: [report(n)] for n in names}},
                          delays={n: 0.25 for n in names})
    s = session(workspace, client, subagent={"max_parallel": 2})
    ev = run(collect(s, "go"))
    assert client.peak == 2
    queued = [e for e in ev if isinstance(e, SubagentStart) and e.status == "queued"]
    assert len(queued) == 2
    assert all(e.status == "completed" for e in ev if isinstance(e, SubagentEnd))
    assert [cid for cid, _ in tool_results(s)] == ["p0", "p1", "p2", "p3"]


def test_interrupt_reaches_all_parallel_subagents(ee_home, workspace):
    names = ["慢一", "慢二", "慢三"]
    parent = sum((task_call(i, f"p{i}", n) for i, n in enumerate(names)), []) + finish("tool_calls")
    client = RouterClient({"parent": [parent], **{n: [report(n)] for n in names}},
                          delays={n: 30 for n in names})
    s = session(workspace, client, subagent={"max_parallel": 2})

    async def go():
        evs = []
        running = set()
        async for e in s.run_turn("go"):
            evs.append(e)
            if isinstance(e, SubagentStart) and e.status == "running":
                running.add(e.call_id)
                if len(running) == 2:
                    await asyncio.sleep(0.1)
                    s.interrupt()
        return evs
    t0 = time.monotonic()
    ev = run(go(), timeout=20)
    assert time.monotonic() - t0 < 10
    assert [e for e in ev if isinstance(e, TurnDone)][-1].reason == "interrupted"
    ends = {e.call_id: e.status for e in ev if isinstance(e, SubagentEnd)}
    assert ends == {"p0": "interrupted", "p1": "interrupted", "p2": "interrupted"}   # 排队中的也结束
    ids = [tc["id"] for m in s.history if m["role"] == "assistant" for tc in m.get("tool_calls", [])]
    assert sorted(ids) == sorted(cid for cid, _ in tool_results(s))


def test_one_subagent_failure_does_not_break_others(ee_home, workspace):
    parent = task_call(0, "p0", "坏的") + task_call(1, "p1", "好的") + finish("tool_calls")
    client = RouterClient({"parent": [parent, report("好")],
                           "坏的": [FakeError(400, "bad request boom")],
                           "好的": [report("好的报告")]}, delays={"好的": 0.2})
    s = session(workspace, client)
    ev = run(collect(s, "go"))
    res = dict(tool_results(s))
    assert 'status="error"' in res["p0"]
    assert 'status="completed"' in res["p1"] and "好的报告" in res["p1"]
    assert [e for e in ev if isinstance(e, TurnDone)][-1].reason == "completed"


def test_hooks_run_for_every_parallel_call(ee_home, workspace, monkeypatch):
    from ee.core import hooks
    seen = []
    orig = hooks.pre_tool_use

    async def spy(sess, cid, tool, args, emit):
        seen.append((tool.name, cid))
        return await orig(sess, cid, tool, args, emit)
    monkeypatch.setattr(hooks, "pre_tool_use", spy)
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    parent = task_call(0, "p0", "读一") + task_call(1, "p1", "读二") + finish("tool_calls")
    child = tool_chunks(0, "k1", "read", {"path": "a.txt"}) + finish("tool_calls")
    client = RouterClient({"parent": [parent, report("好")],
                           "读一": [child, report("1")], "读二": [child, report("2")]})
    s = session(workspace, client)
    run(collect(s, "go"))
    assert sorted(seen) == [("read", "k1"), ("read", "k1"), ("task", "p0"), ("task", "p1")]


# ------------------------------------------------------------------ 写型子代理

def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


def _repo(workspace):
    _git(workspace, "init", "-q", "-b", "main")
    _git(workspace, "config", "core.autocrlf", "false")
    (workspace / "base.txt").write_bytes(b"base\n")
    (workspace / "edit.txt").write_bytes(b"v1\n")
    (workspace / "gone.txt").write_bytes(b"bye\n")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "init")


@needs_git
def test_write_subagents_run_in_parallel_worktrees(ee_home, workspace):
    _repo(workspace)
    # 主目录有未提交的改动：改一个、删一个、新建一个未跟踪文件 → 子代理的 worktree 里都应该看到
    (workspace / "edit.txt").write_bytes(b"v2-dirty\n")
    (workspace / "gone.txt").unlink()
    (workspace / "new.txt").write_bytes(b"untracked\n")
    parent = (task_call(0, "p0", "写甲", agent="general") + task_call(1, "p1", "写乙", agent="general") +
              task_call(2, "p2", "不改", agent="general") + finish("tool_calls"))
    client = RouterClient({
        "parent": [parent, report("好")],
        "写甲": [tool_chunks(0, "k1", "write", {"path": "a.txt", "content": "A\n"}) + finish("tool_calls"),
               report("写了 a")],
        "写乙": [tool_chunks(0, "k1", "read", {"path": "edit.txt"}) + finish("tool_calls"),
               tool_chunks(0, "k2", "write", {"path": "b.txt", "content": "B\n"}) + finish("tool_calls"),
               report("写了 b")],
        "不改": [report("没改")],
    }, delays={"写甲": 0.3, "写乙": 0.3, "不改": 0.3})
    s = session(workspace, client, mode="auto")
    ev = run(collect(s, "go"), timeout=60)
    assert [e for e in ev if isinstance(e, TurnDone)][-1].reason == "completed"
    starts = {e.call_id: e for e in ev if isinstance(e, SubagentStart) and e.status == "running"}
    assert all(starts[c].worktree.get("branch", "").startswith("ee/agent-") for c in ("p0", "p1", "p2"))
    assert overlap(client.spans["写甲"][0], client.spans["写乙"][0])          # 真的并行
    # 主目录没被子代理碰过
    assert not (workspace / "a.txt").exists() and not (workspace / "b.txt").exists()
    assert (workspace / "edit.txt").read_bytes() == b"v2-dirty\n"
    res = dict(tool_results(s))
    bra = starts["p0"].worktree["branch"]
    brb = starts["p1"].worktree["branch"]
    assert "[worktree]" in res["p0"] and bra in res["p0"] and "a.txt" in res["p0"]
    assert "a.txt" not in res["p1"] and "b.txt" in res["p1"]
    assert "没有改任何文件" in res["p2"]
    # 改动提交在各自分支上；分支里也带着主目录未提交的改动（基线），统计只算子代理自己的
    assert _git(workspace, "show", f"{bra}:a.txt") == "A\n"
    assert _git(workspace, "show", f"{brb}:b.txt") == "B\n"
    assert _git(workspace, "show", f"{brb}:edit.txt") == "v2-dirty\n"
    assert _git(workspace, "show", f"{brb}:new.txt") == "untracked\n"
    assert "gone.txt" not in _git(workspace, "ls-tree", "--name-only", brb)
    # 子代理在 worktree 里读到的是主目录未提交的版本
    child_req = [c for k, c in client.calls if k == "写乙"][1]
    assert any("v2-dirty" in str(m.get("content")) for m in child_req["messages"] if m["role"] == "tool")
    end = [e for e in ev if isinstance(e, ToolEnd) and e.call_id == "p1"][0]
    wt = end.meta["subagent"]["worktree"]
    assert wt["file_count"] == 1 and wt["files"][0]["path"] == "b.txt" and wt["synced_dirty"]
    # 没改动的那个：worktree 和分支已删
    br_none = starts["p2"].worktree["branch"]
    assert not _git(workspace, "branch", "--list", br_none).strip()
    # worktree 列表里能看到两个有改动的（用户可在 Worktree 对话框里合并/删除）
    from ee.core import worktree as wtm
    listed = {x["branch"] for x in wtm.list_for(workspace, with_status=False)}
    assert {bra, brb} <= listed and br_none not in listed
    # 主会话 /undo 不会动 worktree 里的东西，也没有可撤的
    assert (workspace / "edit.txt").read_bytes() == b"v2-dirty\n"


def test_write_subagents_serial_without_git(ee_home, workspace):
    if shutil.which("git"):
        # 确保 workspace 不在某个 git 仓库里（tmp 目录一般不在）
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=workspace, capture_output=True)
        if r.returncode == 0:
            pytest.skip("临时目录在 git 仓库里")
    parent = (task_call(0, "p0", "写一", agent="general") + task_call(1, "p1", "写二", agent="general") +
              finish("tool_calls"))
    client = RouterClient({
        "parent": [parent, report("好")],
        "写一": [tool_chunks(0, "k1", "write", {"path": "x.txt", "content": "1\n"}) + finish("tool_calls"), report("1")],
        "写二": [tool_chunks(0, "k1", "write", {"path": "y.txt", "content": "2\n"}) + finish("tool_calls"), report("2")],
    }, delays={"写一": 0.2, "写二": 0.2})
    s = session(workspace, client, mode="auto")
    ev = run(collect(s, "go"))
    a = client.spans["写一"]
    b = client.spans["写二"]
    assert a[-1][1] <= b[0][0]                       # 第二个在第一个全部结束后才开始
    assert (workspace / "x.txt").read_text(encoding="utf-8") == "1\n"
    assert (workspace / "y.txt").read_text(encoding="utf-8") == "2\n"
    assert any(isinstance(e, Notice) and "按顺序执行" in e.text for e in ev)
    assert not any(e.worktree for e in ev if isinstance(e, SubagentStart))


@needs_git
def test_single_write_subagent_stays_in_place(ee_home, workspace):
    _repo(workspace)
    client = RouterClient({
        "parent": [task_call(0, "p0", "单个", agent="general") + finish("tool_calls"), report("好")],
        "单个": [tool_chunks(0, "k1", "write", {"path": "c.txt", "content": "C\n"}) + finish("tool_calls"),
               report("ok")]})
    s = session(workspace, client, mode="auto")
    ev = run(collect(s, "go"))
    assert (workspace / "c.txt").read_text(encoding="utf-8") == "C\n"
    assert not [e for e in ev if isinstance(e, SubagentStart)][0].worktree


@pytest.mark.parametrize("mode", ["read-only", "plan"])
def test_readonly_modes_reject_write_subagent(ee_home, workspace, mode):
    parent = (task_call(0, "p0", "写", agent="general", isolation="worktree") +
              task_call(1, "p1", "普通", agent="general") + finish("tool_calls"))
    client = RouterClient({
        "parent": [parent, report("好")],
        "普通": [tool_chunks(0, "k1", "write", {"path": "z.txt", "content": "z"}) + finish("tool_calls"),
               report("写不了")]})
    s = session(workspace, client, mode=mode)
    ev = run(collect(s, "go"))
    res = dict(tool_results(s))
    assert "不能派会改文件的子代理" in res["p0"]
    st = [e for e in ev if isinstance(e, SubagentStart)]
    assert [e.call_id for e in st] == ["p1"] and st[0].mode == "read-only"
    assert not (workspace / "z.txt").exists()


@needs_git
def test_parallel_approvals_are_distinguishable(ee_home, workspace):
    _repo(workspace)
    parent = (task_call(0, "p0", "命令甲", agent="general") + task_call(1, "p1", "命令乙", agent="general") +
              finish("tool_calls"))
    child = tool_chunks(0, "k1", "shell", {"command": "curl https://example.com"}) + finish("tool_calls")
    client = RouterClient({"parent": [parent, report("好")],
                           "命令甲": [child, report("甲完")], "命令乙": [child, report("乙完")]})
    s = session(workspace, client, mode="default")

    async def go():
        pending = []
        evs = []
        async for e in s.run_turn("go"):
            evs.append(e)
            if isinstance(e, ApprovalRequest):
                pending.append(e)
                if len(pending) == 2:      # 两个审批同时挂着，再逐个回答
                    for p in pending:
                        p.future.set_result(ApprovalDecision("deny"))
        return evs, pending
    ev, pending = run(go(), timeout=60)
    assert sorted(p.call_id for p in pending) == ["p0/k1", "p1/k1"]
    src = {p.call_id: p.source for p in pending}
    assert src["p0/k1"]["agent"] == "general" and src["p0/k1"]["description"] == "命令甲"
    assert src["p1/k1"]["call_id"] == "p1" and src["p1/k1"]["description"] == "命令乙"


def test_server_thread_parallel_items(ee_home, workspace):
    """服务端：两个并行子代理各自更新自己的 task 条目；子代理的审批条目带 source。"""
    import copy
    from ee.core.providers.openai_chat import ChatClient
    from ee.server.threads import Thread
    parent = task_call(0, "p0", "看甲") + task_call(1, "p1", "看乙") + finish("tool_calls")
    client = RouterClient({"parent": [parent, report("好")], "看甲": [report("甲")], "看乙": [report("乙")]},
                          delays={"看甲": 0.2, "看乙": 0.2})
    notes = []
    t = Thread("t1", ee_home, lambda m, p: notes.append((m, copy.deepcopy(p))), {"cwd": str(workspace)},
               client_factory=lambda spec: ChatClient(spec, client=client), persist=False)

    async def go():
        t.start_turn("go")
        await t.task
    run(go())
    tools = {it["call_id"]: it for it in t.items if it.get("type") == "tool"}
    assert tools["p0"]["meta"]["subagent"]["summary"] == "甲" and tools["p1"]["meta"]["subagent"]["summary"] == "乙"
    assert tools["p0"]["meta"]["subagent"]["id"] == "p0"
    running_both = False
    state = {}
    for m, p in notes:
        if m == "item/updated" and p["item"].get("type") == "tool":
            sub = p["item"].get("meta", {}).get("subagent") or {}
            state[p["item"]["call_id"]] = sub.get("status")
            if state.get("p0") == "running" and state.get("p1") == "running":
                running_both = True
    assert running_both
    assert any(m == "thread/usage" for m, _ in notes)


def test_subagent_parallel_settings(ee_home):
    from ee.core.subagent import subagent_config
    from ee.server.app import Backend
    b = Backend(home=ee_home, persist=False)
    st = run(b.call("settings/get", {}))["settings"]["subagent"]
    assert st["max_parallel"] == 3 and st["worktree"] is True
    st = run(b.call("settings/set", {"settings": {"subagent": {"max_parallel": 5, "worktree": False}}}))["settings"]
    assert st["subagent"]["max_parallel"] == 5 and st["subagent"]["worktree"] is False
    with pytest.raises(Exception, match="1～8"):
        run(b.call("settings/set", {"settings": {"subagent": {"max_parallel": 0}}}))
    assert subagent_config({"subagent": {"max_parallel": 99}})["max_parallel"] == 8
    assert subagent_config({"subagent": {"max_parallel": "x"}})["max_parallel"] == 3
