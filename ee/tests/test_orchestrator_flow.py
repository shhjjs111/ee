"""D6-1 U6：写工具与审批冒泡。

四条不变量在这里第一次有测试钉住：

* **I1** 子会话的父关系只能由服务端从可信身份写入 —— 工具传不了 parent，只能拿 ctx 里的调用者 id；
  别人的子会话、普通分叉都不能被当成本会话的子会话操作。
* **I3** 共享 cwd 的子会话只能只读 —— 会改文件就必须独立 worktree，否则隔离失败会直接改主目录。
* **I5** 名额在发起运行时预留 —— create 与 send 共用同一个控制器，先建空闲子会话再同时 send 绕不过上限。
* **子会话不能再建子会话** —— 工具表里没有 thread_*，服务层再兜一层。

另外两条是本轮补 U5 遗留接线的回归：工具必须真的能从 ctx 拿到句柄（少了这条，五个工具全部
报「没有启用指挥能力」），回合结束必须释放运行名额（靠 notify 释放会被 listener 的
`except Exception: pass` 吞掉，名额静默泄漏）。

U7 的三条重启/删除语义在同一文件末尾：**I6** 重启把遗留 running 标成 interrupted、
**I7** 删除父会话不级联删子会话 worktree、重启不重放回合。

全程假模型，不联网、不启真实模型；需要 git 的用例在无 git 环境自动跳过。
"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import FakeClient, finish, text_chunks, tool_chunks
from ee.core.providers.openai_chat import ChatClient
from ee.core.store import SessionStore
from ee.server.app import Backend, RpcError
from ee.server.orchestrator import Orchestrator, OrchestratorError
from ee.tools import build_tools

pytestmark = pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                                reason="需要 git")

SHELL_CMD = "touch made.txt" if sys.platform != "win32" else "New-Item made.txt"


def enable(home, allow=(), **kw):
    body = "\n".join(f"{k} = {json.dumps(v)}" for k, v in {"enabled": True, **kw}.items())
    extra = f"\n[permissions]\nallow = {json.dumps(list(allow))}\n" if allow else ""
    (home / "config.toml").write_text(f"[orchestrator]\n{body}\n{extra}", encoding="utf-8")


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("one\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


def make_backend(home, script, delay=0.0):
    fake = FakeClient(script, delay=delay)
    b = Backend(home=home, client_factory=lambda spec: ChatClient(spec, client=fake))
    events: list[tuple[str, dict]] = []
    b.listeners.append(lambda m, p: events.append((m, json.loads(json.dumps(p, default=str)))))
    return b, fake, events


async def wait_for(events, method, pred=lambda p: True, timeout=30.0):
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    while True:
        for m, p in events:
            if m == method and pred(p):
                return p
        if loop.time() - t0 > timeout:
            raise AssertionError(f"等不到 {method}；收到：{[m for m, _ in events]}")
        await asyncio.sleep(0.01)


async def _parent(b, cwd) -> str:
    return (await b.call("thread/start", {"cwd": str(cwd)}))["thread"]["id"]


# ---------------------------------------------------------------- 创建子会话


def test_create_child_creates_worktree_and_parent(ee_home, repo):
    """写型子会话必须在独立 worktree，父关系由服务端写进 meta（I1、I3）。"""
    enable(ee_home)

    async def go():
        b, _, _ = make_backend(ee_home, [])
        pid = await _parent(b, repo)
        orch = Orchestrator(b)

        res = await orch.create_child(pid, title="改个文档", mode="default")
        cid = res["thread"]["id"]
        t = b.threads[cid]

        assert t.meta["orchestrator_parent_id"] == pid
        assert t.meta["orchestrator_role"] == "child"
        wt = t.meta["worktree"]
        assert wt and wt["branch"].startswith("ee/"), f"worktree 分支不合规：{wt}"
        assert Path(t.meta["cwd"]) != repo.resolve(), "写型子会话不能直接用主目录"
        assert Path(t.meta["cwd"]) == Path(wt["path"]), "子会话 cwd 应落在 worktree 里"
        assert res["thread"]["orchestrator_parent_id"] == pid
        # 源目录没被碰过
        assert not (repo / "made.txt").exists()
        # 立刻能从父会话列出
        assert [x["id"] for x in orch.list_children(pid)] == [cid]

    asyncio.run(go())


def test_create_without_worktree_only_readonly(ee_home, repo):
    """共享 cwd 只允许只读；被拒绝的创建不能留下名额占用（I3、I5 的回滚面）。"""
    enable(ee_home)

    async def go():
        b, _, _ = make_backend(ee_home, [])
        pid = await _parent(b, repo)
        orch = Orchestrator(b)

        with pytest.raises(OrchestratorError, match="只读"):
            await orch.create_child(pid, title="写文件", mode="default", worktree=False)

        ok = await orch.create_child(pid, title="只看看", mode="read-only", worktree=False)
        assert ok["thread"]["cwd"] == str(repo.resolve())
        assert ok["thread"]["worktree"] is None
        # 失败那次没留下占用：总数只算成功的这一个
        assert b.slots is not None and b.slots.total == 1

    asyncio.run(go())


# ---------------------------------------------------------------- 名额共用


def test_send_uses_slot(ee_home, repo):
    """名额满了 send 也必须被拒 —— 只有 create 受限等于没限（I5）。"""
    enable(ee_home, max_children=1)

    async def go():
        b, _, _ = make_backend(ee_home, [text_chunks("做完了") + finish("stop")])
        pid = await _parent(b, repo)
        orch = Orchestrator(b)

        a = (await orch.create_child(pid, title="甲", mode="read-only", worktree=False))["thread"]["id"]
        # 名额控制器是惰性构造的（第一次派发时按父会话配置建），断言放在这之后
        assert b.slots is not None and b.slots.max_children == 1
        kid = (await orch.create_child(pid, title="乙", mode="read-only", worktree=False))["thread"]["id"]
        # 模拟「甲正在跑」：占掉唯一的名额
        await b.slots.reserve(a)

        with pytest.raises(OrchestratorError, match="上限"):
            await orch.send(pid, kid, "开始吧")

        # 名额腾出来之后同一句能发出去，并且跑完会释放
        b.slots.release(a)
        r = await orch.send(pid, kid, "开始吧")
        assert r["queued"] is False
        await b.threads[kid].task
        assert b.threads[kid].meta["runs"][-1]["terminal_reason"] == "completed"
        assert b.slots.active == 0

    asyncio.run(go())


def test_slot_released_after_turn(ee_home, repo):
    """回合结束释放运行名额；子会话还在，总数占用保留。"""
    enable(ee_home)

    async def go():
        b, _, _ = make_backend(ee_home, [text_chunks("做完了") + finish("stop")])
        pid = await _parent(b, repo)
        orch = Orchestrator(b)

        cid = (await orch.create_child(pid, title="跑一下", mode="read-only", worktree=False,
                                       prompt="做事"))["thread"]["id"]
        assert b.slots.active == 1, "带 prompt 的创建应立即占用运行名额"
        await b.threads[cid].task
        assert b.slots.active == 0
        assert b.slots.total == 1

    asyncio.run(go())


def test_delete_child_frees_total(ee_home, repo):
    """删掉子会话必须回收总数占用，否则 16 个之后再也建不出来。"""
    enable(ee_home)

    async def go():
        b, _, _ = make_backend(ee_home, [])
        pid = await _parent(b, repo)
        orch = Orchestrator(b)

        cid = (await orch.create_child(pid, title="临时", mode="read-only", worktree=False))["thread"]["id"]
        assert b.slots.total == 1
        await b.call("thread/delete", {"thread_id": cid})
        assert b.slots.total == 0
        assert orch.list_children(pid) == []

    asyncio.run(go())


# ---------------------------------------------------------------- 审批冒泡


def test_child_approval_reaches_parent_then_completes(ee_home, repo):
    """子会话要审批时父会话能看见；从父侧响应后子会话继续跑完。重复响应给明确错误（I7 的反面）。"""
    enable(ee_home)
    script = [
        tool_chunks(0, "c1", "shell", {"command": SHELL_CMD}) + finish("tool_calls"),
        text_chunks("创建好了") + finish("stop"),
    ]

    async def go():
        b, _, ev = make_backend(ee_home, script)
        pid = await _parent(b, repo)
        orch = Orchestrator(b)

        cid = (await orch.create_child(pid, title="建文件", mode="default",
                                       prompt="创建一个文件"))["thread"]["id"]
        note = await wait_for(ev, "thread/child_approval")
        assert note["parent_id"] == pid
        assert note["thread_id"] == cid
        assert note["tool"] == "shell" and note["call_id"]
        assert note["child_title"] == "建文件"
        assert "made.txt" in note["summary"]

        # 从父侧响应：thread_id 指向子会话
        await b.call("approval/respond", {"thread_id": cid, "call_id": note["call_id"], "choice": "once"})
        done = await wait_for(ev, "turn/completed", lambda p: p["thread_id"] == cid)
        assert done["reason"] == "completed"
        assert (Path(b.threads[cid].meta["cwd"]) / "made.txt").exists()

        # 重复响应：明确错误，不崩
        with pytest.raises(RpcError):
            await b.call("approval/respond", {"thread_id": cid, "call_id": note["call_id"], "choice": "once"})

    asyncio.run(go())


def test_child_question_reaches_parent(ee_home, repo):
    """ask_user 和审批一样是阻塞态：不冒泡就是子会话静默卡死到超时。"""
    enable(ee_home)
    script = [
        tool_chunks(0, "c1", "ask_user", {"question": "用哪个名字？", "options": ["a", "b"]}) + finish("tool_calls"),
        text_chunks("好") + finish("stop"),
    ]

    async def go():
        b, _, ev = make_backend(ee_home, script)
        pid = await _parent(b, repo)
        orch = Orchestrator(b)
        cid = (await orch.create_child(pid, title="问一下", mode="read-only", worktree=False,
                                       prompt="先问我"))["thread"]["id"]
        note = await wait_for(ev, "thread/child_approval")
        assert note["parent_id"] == pid and note["thread_id"] == cid
        assert note["kind"] == "question"
        await b.call("question/respond", {"thread_id": cid, "call_id": note["call_id"], "answer": "a"})
        await wait_for(ev, "turn/completed", lambda p: p["thread_id"] == cid)

    asyncio.run(go())


# ---------------------------------------------------------------- 子会话不能再建子会话


def test_child_cannot_create_grandchild(ee_home, repo):
    enable(ee_home)

    async def go():
        b, _, _ = make_backend(ee_home, [])
        pid = await _parent(b, repo)
        orch = Orchestrator(b)
        cid = (await orch.create_child(pid, title="子", mode="read-only", worktree=False))["thread"]["id"]

        # 工具表里没有编排工具（子会话没有子会话，给了只会让它误以为能再分派）
        s = b.threads[cid].ensure_session()
        names = {t.name for t in s.tools}
        assert not any(n.startswith("thread_") for n in names), names
        # 服务层兜底：就算工具被绕过，子会话也当不了父
        with pytest.raises(OrchestratorError):
            await orch.create_child(cid, title="孙", mode="read-only", worktree=False)

    asyncio.run(go())


# ---------------------------------------------------------------- 接线回归（U5 遗留）


def test_tool_context_gets_orchestrator_handle(ee_home, workspace):
    """工具必须真的从 ctx 拿到窄接口：少这条接线，五个工具全部报「没有启用指挥能力」。"""
    enable(ee_home)
    script = [
        tool_chunks(0, "c1", "threads_list", {}) + finish("tool_calls"),
        text_chunks("还没有子会话") + finish("stop"),
    ]

    async def go():
        b, _, ev = make_backend(ee_home, script)
        pid = await _parent(b, workspace)
        await b.call("turn/start", {"thread_id": pid, "text": "看看子会话"})
        await b.threads[pid].task
        tool = [p["item"] for m, p in ev if m == "item/updated" and p["item"]["type"] == "tool"][-1]
        assert tool["status"] == "ok", tool["output"]
        assert "指挥能力" not in (tool["output"] or ""), tool["output"]

    asyncio.run(go())


def test_tool_create_and_send_round_trip(ee_home, repo):
    """模型侧真的能用两个写工具派发：创建 → 子会话跑完 → send 追加一回合。

    写工具走"未知工具"分支（默认模式问一次），这里用 allow 规则模拟用户已经点过"以后都行"。
    """
    enable(ee_home, allow=["thread_create", "thread_send"])
    script = [
        # 父会话第一回合：建一个只读子会话（不派发，先建）
        tool_chunks(0, "c1", "thread_create",
                    {"title": "子任务", "mode": "read-only", "worktree": False}) + finish("tool_calls"),
        text_chunks("已经建好子会话") + finish("stop"),
        # 子会话第一回合
        text_chunks("子任务完成") + finish("stop"),
    ]

    async def go():
        b, _, ev = make_backend(ee_home, script)
        pid = await _parent(b, repo)
        await b.call("turn/start", {"thread_id": pid, "text": "建个子会话"})
        await b.threads[pid].task

        tool = [p["item"] for m, p in ev if m == "item/updated" and p["item"]["type"] == "tool"][-1]
        assert tool["status"] == "ok", tool["output"]
        kids = Orchestrator(b).list_children(pid)
        assert [k["title"] for k in kids] == ["子任务"]

        # 直接对子会话发一回合（send 的服务面），确认走了名额并能跑完
        r = await Orchestrator(b).send(pid, kids[0]["id"], "继续")
        assert r["queued"] is False
        await b.threads[kids[0]["id"]].task
        assert b.threads[kids[0]["id"]].meta["runs"][-1]["terminal_reason"] == "completed"

    asyncio.run(go())


def test_tools_include_create_and_send(ee_home, workspace):
    """启用了就要五个都在：只给读工具的话模型无法派发。"""
    enable(ee_home)
    from ee.config import load_config
    names = {t.name for t in build_tools("full", load_config(workspace))}
    assert {"threads_list", "thread_status", "thread_wait", "thread_create", "thread_send"} <= names


# ---------------------------------------------------------------- U7：重启与删除语义
#
# 三条语义都是「上一个进程死了」之后的状态，所以测试的构造方式都是：在 b1 里把状态写进磁盘，
# 再新建一个 Backend 当作重启后的新进程。不真的杀进程——那在测试里不可控，而磁盘上的残留
# 记录形状才是被断言的对象。

def _crashed_run(run_id="run_dead"):
    """进程在回合中途被杀时磁盘上留下的记录：start_turn → _new_run → save_meta 的产物。"""
    return {"id": run_id, "started": 1.0, "finished": None, "status": "running",
            "terminal_reason": None, "initiator": "user"}


def test_restart_marks_running_as_interrupted(ee_home, repo):
    """遗留 running 的回合在重启后必须是 interrupted，不能报成「空闲且没有终态」（I6）。

    `status` 只有 idle/running，靠它推不出「跑完了」还是「被杀」；`terminal_reason` 才是判据。
    这里同时钉住两条读路径：未加载会话的磁盘发现（list_children）与打开会话后的内存态。
    """
    enable(ee_home)

    async def go():
        b, _, _ = make_backend(ee_home, [])
        pid = await _parent(b, repo)
        cid = (await Orchestrator(b).create_child(pid, title="跑到一半", mode="read-only",
                                                  worktree=False))["thread"]["id"]
        return pid, cid

    pid, cid = asyncio.run(go())
    SessionStore(ee_home, session_id=cid).meta({"runs": [_crashed_run()]})
    path = ee_home / "sessions" / f"{cid}.jsonl"

    b2 = Backend(home=ee_home, persist=True)
    assert cid not in b2.threads, "确认是冷缓存，走的是磁盘路径"

    kid = Orchestrator(b2).list_children(pid)[0]
    assert kid["status"] == "idle"
    assert kid["terminal_reason"] == "interrupted", "重启后遗留 running 必须报成 interrupted"

    # 打开会话（内存路径）也要一致
    st = Orchestrator(b2).status(pid, cid)
    assert st["terminal_reason"] == "interrupted"
    assert SessionStore.summary(path)["runs"][-1]["terminal_reason"] == "interrupted"
    assert SessionStore.summary(path)["runs"][-1]["status"] == "done"


def test_restart_keeps_finished_runs_untouched(ee_home, repo):
    """只动遗留 running：正常结束的回合不能被改写（否则历史里的真实终态被抹掉）。"""
    enable(ee_home)

    async def go():
        b, _, _ = make_backend(ee_home, [])
        pid = await _parent(b, repo)
        cid = (await Orchestrator(b).create_child(pid, title="跑完了", mode="read-only",
                                                  worktree=False))["thread"]["id"]
        return pid, cid

    pid, cid = asyncio.run(go())
    SessionStore(ee_home, session_id=cid).meta({"runs": [
        {"id": "run_ok", "started": 1.0, "finished": 2.0, "status": "done",
         "terminal_reason": "budget", "initiator": "user"}]})

    b2 = Backend(home=ee_home, persist=True)
    kid = Orchestrator(b2).list_children(pid)[0]
    assert kid["terminal_reason"] == "budget"
    assert Orchestrator(b2).status(pid, cid)["terminal_reason"] == "budget"


def test_parent_delete_keeps_child_worktree(ee_home, repo):
    """删除父会话不级联删子会话：worktree 目录、分支、会话文件都还在，只解开父关系（I7）。

    级联删会直接毁掉子会话里未合并的改动；留着父指针则会让子会话变成一个指向已删会话的
    悬空子级（它既不能再被派发，也不再出现在任何父会话的列表里）。
    """
    enable(ee_home)

    async def go():
        b, _, _ = make_backend(ee_home, [])
        pid = await _parent(b, repo)
        cid = (await Orchestrator(b).create_child(pid, title="有改动的子任务", mode="default"))["thread"]["id"]
        wt = dict(b.threads[cid].meta["worktree"])
        # 子会话在 worktree 里留下未提交的改动：删除父会话绝不能把它一起带走
        (Path(wt["path"]) / "work.txt").write_text("还没合并\n", encoding="utf-8")

        await b.call("thread/delete", {"thread_id": pid})

        path = ee_home / "sessions" / f"{cid}.jsonl"
        assert path.is_file(), "子会话文件被连带删掉了"
        assert Path(wt["path"]).is_dir(), "子会话 worktree 目录被连带删掉了"
        assert (Path(wt["path"]) / "work.txt").is_file(), "子会话的改动被连带删掉了"
        assert git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{wt['branch']}").strip(), "子会话分支被删了"

        # 父关系解开：不再是任何会话的子级，也不再挂着一个已删除的父
        kept = b.threads[cid]
        assert kept.meta.get("orchestrator_parent_id") in (None, ""), kept.meta.get("orchestrator_parent_id")
        assert kept.meta.get("orchestrator_role") in (None, "")
        # 父已删：不能再从这个身份派发任何东西
        with pytest.raises(OrchestratorError):
            await Orchestrator(b).create_child(pid, title="不该建出来", mode="read-only", worktree=False)

    asyncio.run(go())


def test_parent_delete_detaches_unloaded_child(ee_home, repo):
    """重启后再删父会话：子会话不在内存里，照样要把磁盘上的父指针解开（否则永远是孤儿）。"""
    enable(ee_home)

    async def go():
        b, _, _ = make_backend(ee_home, [])
        pid = await _parent(b, repo)
        cid = (await Orchestrator(b).create_child(pid, title="不在内存", mode="read-only",
                                                 worktree=False))["thread"]["id"]
        b.threads.pop(cid)                      # 模拟重启后还没点开这个子会话
        await b.call("thread/delete", {"thread_id": pid})
        return cid

    cid = asyncio.run(go())
    path = ee_home / "sessions" / f"{cid}.jsonl"
    assert path.is_file(), "子会话文件被连带删掉了"
    s = SessionStore.summary(path)
    assert s.get("orchestrator_parent_id") in (None, "")
    assert s.get("orchestrator_role") in (None, "")


def test_restart_does_not_replay_turns(ee_home, repo):
    """重启只是把状态读出来，不能顺手再跑一回合（会自动花钱、还会改用户文件）。

    三个可观察点：不自动加载会话、不构造名额控制器（构造即代表准备派发）、run 记录不增加。
    """
    enable(ee_home)
    script = [text_chunks("做完了") + finish("stop")]

    async def go():
        b, _, _ = make_backend(ee_home, script)
        pid = await _parent(b, repo)
        cid = (await Orchestrator(b).create_child(pid, title="跑一回合", mode="read-only", worktree=False,
                                                  prompt="做事"))["thread"]["id"]
        await b.threads[cid].task
        return pid, cid

    pid, cid = asyncio.run(go())
    path = ee_home / "sessions" / f"{cid}.jsonl"
    before = len(SessionStore.summary(path)["runs"])

    b2 = Backend(home=ee_home, persist=True)
    assert b2.threads == {}, "新进程启动不该预先加载任何会话"
    assert b2.slots is None, "新进程启动不该为派发做准备（名额控制器是惰性构造的）"

    kids = Orchestrator(b2).list_children(pid)
    assert [k["status"] for k in kids] == ["idle"]
    assert b2.threads == {}, "列子会话触发了自动加载/运行"
    assert len(SessionStore.summary(path)["runs"]) == before, "重启后不该多出一次回合"
    assert Orchestrator(b2).status(pid, cid)["status"] == "idle"
    assert len(SessionStore.summary(path)["runs"]) == before


def test_detached_child_survives_as_standalone(ee_home, repo):
    """父被删后子会话还能自己继续用：打开、续跑，不再走名额（它已经没有父了）。"""
    enable(ee_home)
    script = [text_chunks("自己做完了") + finish("stop")]

    async def go():
        b, _, ev = make_backend(ee_home, script)
        pid = await _parent(b, repo)
        cid = (await Orchestrator(b).create_child(pid, title="独立了", mode="read-only",
                                                  worktree=False))["thread"]["id"]
        await b.call("thread/delete", {"thread_id": pid})
        await b.call("turn/start", {"thread_id": cid, "text": "自己继续"})
        await b.threads[cid].task
        return await wait_for(ev, "turn/completed", lambda p: p["thread_id"] == cid)

    done = asyncio.run(go())
    assert done["reason"] == "completed"


def test_running_run_in_this_process_is_not_rewritten(ee_home, workspace):
    """本进程正在跑的 run 不能被当成「遗留 running」（U7 的边界，别把判据写成"状态=running 就改"）。

    判据必须是 pid：`SessionStore.summary` 也被 `headless` / automations 这类"扫全部会话文件"
    的路径用，那些路径会读到本进程里**真的正在跑**的会话。只看 status 会把它标成 interrupted，
    界面就会在回合进行中显示"已中断"。
    """
    import os
    path = ee_home / "sessions"
    sid = "20261028-120000-live01"
    st = SessionStore(ee_home, session_id=sid)
    st.meta({"title": "正在跑", "cwd": str(workspace),
             "runs": [{"id": "run_live", "started": 1.0, "finished": None, "status": "running",
                       "terminal_reason": None, "initiator": "user", "pid": os.getpid()}]})
    got = SessionStore.summary(path / f"{sid}.jsonl")["runs"][-1]
    assert got["status"] == "running", "本进程正在跑的回合被误标成结束了"
    assert got["terminal_reason"] is None


# ---------------------------------------------------------------- 编排 RPC（D6-1 U9）
#
# 编排能力原先只以工具形式给模型用，服务面没有入口。桌面端因此拿不到子会话列表与
# 真实改动数（U8 卡片的 changed_count 只能靠模型自己调 thread_status 才有），
# 端到端脚本也无法从一个 RPC 客户端验证「派发 → 等终态」这条链路。


def test_thread_children_lists_only_own_children(ee_home, repo):
    """thread/children 只列本会话派发的子会话；普通分叉不是子会话。"""
    enable(ee_home)

    async def go():
        b, _, _ = make_backend(ee_home, [])
        pid = await _parent(b, repo)
        other = await _parent(b, repo)
        orch = Orchestrator(b)
        cid = (await orch.create_child(pid, title="甲", mode="read-only", worktree=False))["thread"]["id"]
        # 另一个父会话的子会话不该出现
        await orch.create_child(other, title="乙", mode="read-only", worktree=False)

        got = await b.call("thread/children", {"parent_id": pid})
        ids = [x["id"] for x in got["threads"]]
        assert ids == [cid]
        assert got["threads"][0]["orchestrator_parent_id"] == pid

    asyncio.run(go())


def test_thread_children_rejects_unknown_parent(ee_home, repo):
    """父会话不存在时必须报错，不能悄悄返回空列表（会被误读成"没有子会话"）。"""
    enable(ee_home)

    async def go():
        b, _, _ = make_backend(ee_home, [])
        with pytest.raises(RpcError):
            await b.call("thread/children", {"parent_id": "20260101-000000-nope00"})

    asyncio.run(go())


def test_thread_child_status_returns_changed_count(ee_home, repo):
    """thread/status 给界面用它需要的字段：终态与真实改动数。"""
    enable(ee_home)

    async def go():
        b, _, _ = make_backend(ee_home, [])
        pid = await _parent(b, repo)
        cid = (await Orchestrator(b).create_child(pid, title="改文件", mode="default"))["thread"]["id"]
        wt = Path(b.threads[cid].meta["cwd"])
        (wt / "new.txt").write_text("x\n", encoding="utf-8")

        got = (await b.call("thread/status", {"parent_id": pid, "child_id": cid}))["thread"]
        assert got["id"] == cid
        assert got["changed_count"] >= 1, got

    asyncio.run(go())


def test_thread_wait_returns_terminal_reason(ee_home, repo):
    """等子会话跑完，终态要如实带回来（wait 是编排闭环的最后一环）。"""
    enable(ee_home)

    async def go():
        b, _, _ = make_backend(ee_home, [text_chunks("做完了") + finish("stop")])
        pid = await _parent(b, repo)
        cid = (await Orchestrator(b).create_child(pid, title="跑一下", mode="read-only", worktree=False,
                                                  prompt="做事"))["thread"]["id"]
        got = await b.call("thread/wait", {"parent_id": pid, "child_ids": [cid], "timeout": 30},
                           )
        kids = got["threads"]
        assert [k["id"] for k in kids] == [cid]
        assert kids[0]["terminal_reason"] == "completed"
        assert got["timed_out"] is False

    asyncio.run(go())


def test_thread_wait_rejects_foreign_child(ee_home, repo):
    """不是自己的子会话一律拒绝：这条边界丢了好比把别人的会话交给调用方。"""
    enable(ee_home)

    async def go():
        b, _, _ = make_backend(ee_home, [])
        pid = await _parent(b, repo)
        other = await _parent(b, repo)
        cid = (await Orchestrator(b).create_child(other, title="别家的", mode="read-only",
                                                  worktree=False))["thread"]["id"]
        with pytest.raises(RpcError):
            await b.call("thread/wait", {"parent_id": pid, "child_ids": [cid]})

    asyncio.run(go())


def test_thread_create_child_rpc(ee_home, repo):
    """服务面能派发子会话，并且父关系由服务端写（工具参数里没有 parent 字段）。"""
    enable(ee_home)

    async def go():
        b, _, _ = make_backend(ee_home, [])
        pid = await _parent(b, repo)
        got = await b.call("thread/create_child", {"parent_id": pid, "title": "从 RPC 建的",
                                                   "mode": "read-only", "worktree": False})
        child = got["thread"]
        assert child["orchestrator_parent_id"] == pid
        assert b.threads[child["id"]].meta["orchestrator_role"] == "child"
        # 建完能立刻从父会话列出来
        listed = (await b.call("thread/children", {"parent_id": pid}))["threads"]
        assert [x["id"] for x in listed] == [child["id"]]

    asyncio.run(go())


def test_thread_create_child_rpc_rejects_non_readonly_shared_cwd(ee_home, repo):
    """共享 cwd 只能只读——这条不变量不能因为多了个 RPC 入口就放宽。"""
    enable(ee_home)

    async def go():
        b, _, _ = make_backend(ee_home, [])
        pid = await _parent(b, repo)
        with pytest.raises(RpcError):
            await b.call("thread/create_child", {"parent_id": pid, "title": "想写文件",
                                                 "mode": "default", "worktree": False})

    asyncio.run(go())
