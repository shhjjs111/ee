"""D6-1 U5：窄接口与三个读工具。

三个约束：

1. **工具只在显式启用时注入**，lean 永远没有。多五个工具是每个会话的固定 token 开销，
   而且会改冻结前缀。
2. **查询不能有副作用**：`thread_status` 若复用 `W.diff()` 会执行 intent-to-add 改真实索引
   （T3 刚修过同类问题），所以改动统计只读 `git status` 的计数，不列文件、不碰索引。
3. **重启后能发现子会话**：只看 `backend.threads` 会漏掉未加载的子会话，
   必须能从磁盘元数据补出来。
"""
import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from conftest import FakeClient, finish, text_chunks
from ee.config import load_config
from ee.core.providers.openai_chat import ChatClient
from ee.server.app import Backend
from ee.tools import build_tools

pytestmark = pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                                reason="需要 git")


def enable(home, **kw):
    body = "\n".join(f"{k} = {json.dumps(v)}" for k, v in {"enabled": True, **kw}.items())
    (home / "config.toml").write_text(f"[orchestrator]\n{body}\n", encoding="utf-8")


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


# ---------------------------------------------------------------- 注入范围


def test_tools_absent_by_default(ee_home, workspace):
    names = [t.name for t in build_tools("full", load_config(workspace))]
    assert not any(n.startswith("thread_") for n in names)


def test_tools_present_when_enabled(ee_home, workspace):
    enable(ee_home)
    names = {t.name for t in build_tools("full", load_config(workspace))}
    assert {"threads_list", "thread_status", "thread_wait"} <= names


def test_lean_never_gets_orchestrator_tools(ee_home, workspace):
    enable(ee_home)
    names = {t.name for t in build_tools("full", load_config(workspace), agent="lean")}
    assert not any(n.startswith("thread_") for n in names)


# ---------------------------------------------------------------- 服务实现


def _orchestrator(home):
    from ee.server.orchestrator import Orchestrator
    b = Backend(home=home, persist=True,
                client_factory=lambda spec: ChatClient(spec, client=FakeClient([])))
    return b, Orchestrator(b)


def _child(b, parent_id: str, title="子") -> str:
    """直接建一个受控子会话（U5 阶段还没有 create 工具）。"""
    th = asyncio.run(b.call("thread/start", {"cwd": str(b.threads[parent_id].cwd)}))["thread"]
    t = b.threads[th["id"]]
    t.meta["orchestrator_parent_id"] = parent_id
    t.meta["orchestrator_role"] = "child"
    t.meta["title"] = title
    t.save_meta()
    b.notify("thread/updated", {"thread": t.summary()})
    return t.id


def test_list_children_only_returns_own(ee_home, workspace):
    b, orch = _orchestrator(ee_home)
    p1 = asyncio.run(b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
    p2 = asyncio.run(b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
    kid = _child(b, p1)

    mine = orch.list_children(p1)
    assert [x["id"] for x in mine] == [kid]
    assert orch.list_children(p2) == []


def test_list_discovers_children_after_restart(ee_home, workspace):
    """冷缓存（模拟重启）也要能列出——不能只扫内存里的 threads。"""
    b, orch = _orchestrator(ee_home)
    p1 = asyncio.run(b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
    kid = _child(b, p1)

    b2 = Backend(home=ee_home, persist=True)
    assert kid not in b2.threads               # 确认是冷缓存
    from ee.server.orchestrator import Orchestrator
    assert [x["id"] for x in Orchestrator(b2).list_children(p1)] == [kid]


def test_status_does_not_touch_index(ee_home, repo):
    b, orch = _orchestrator(ee_home)
    p1 = asyncio.run(b.call("thread/start", {"cwd": str(repo)}))["thread"]["id"]
    kid = _child(b, p1)
    wt = Path(b.threads[kid].meta["cwd"])
    (wt / "new.txt").write_text("x\n", encoding="utf-8")

    before = git(wt, "ls-files", "-s")
    orch.status(p1, kid)
    after = git(wt, "ls-files", "-s")
    assert after == before, "thread_status 改动了真实索引"


def test_status_reports_counts_and_terminal(ee_home, repo):
    b, orch = _orchestrator(ee_home)
    p1 = asyncio.run(b.call("thread/start", {"cwd": str(repo)}))["thread"]["id"]
    kid = _child(b, p1)
    t = b.threads[kid]
    t.meta["runs"] = [{"id": "run_1", "status": "done", "terminal_reason": "completed",
                       "started": 1, "finished": 2, "initiator": "user"}]
    t.save_meta()
    wt = Path(t.meta["cwd"])
    (wt / "new.txt").write_text("x\n", encoding="utf-8")

    st = orch.status(p1, kid)
    assert st["terminal_reason"] == "completed"
    assert st["changed_count"] >= 1
    assert st["status"] == "idle"


def test_status_rejects_foreign_child(ee_home, workspace):
    b, orch = _orchestrator(ee_home)
    p1 = asyncio.run(b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
    p2 = asyncio.run(b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
    kid = _child(b, p1)
    with pytest.raises(Exception, match="不是本会话"):
        orch.status(p2, kid)


def test_wait_returns_terminal_reason_not_idle(ee_home, workspace):
    b, orch = _orchestrator(ee_home)
    p1 = asyncio.run(b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
    kid = _child(b, p1)
    t = b.threads[kid]
    t.meta["runs"] = [{"id": "run_1", "status": "done", "terminal_reason": "budget",
                       "started": 1, "finished": 2, "initiator": "user"}]
    t.save_meta()

    async def go():
        return await orch.wait(p1, [kid], timeout=1)

    res = asyncio.run(go())
    assert res["timed_out"] is False
    assert res["threads"][0]["terminal_reason"] == "budget"


def test_wait_times_out_and_says_so(ee_home, workspace):
    b, orch = _orchestrator(ee_home)
    p1 = asyncio.run(b.call("thread/start", {"cwd": str(workspace)}))["thread"]["id"]
    kid = _child(b, p1)
    t = b.threads[kid]

    class _Running:
        def done(self):
            return False

    t.task = _Running()          # 伪造"还在跑"

    async def go():
        return await orch.wait(p1, [kid], timeout=0.2)

    res = asyncio.run(go())
    assert res["timed_out"] is True
