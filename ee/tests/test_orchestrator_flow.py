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
