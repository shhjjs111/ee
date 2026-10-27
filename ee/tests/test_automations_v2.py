"""D5-3 自动化 v2：worktree 运行（命名带自动化名+时间、结束后保留并给改动摘要、合并/丢弃、运行中不许删、
失败保留日志与 worktree、无改动自动清理）、运行历史（开始/结束/用时/状态/用量花费/会话）。"""
import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import FakeError, finish, text_chunks, tool_chunks
from test_automations import make_backend, new_auto

from ee.core import worktree as W
from ee.server.app import RpcError

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


def git(cwd, *a) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(workspace):
    git(workspace, "init", "-q", "-b", "main")
    (workspace / "a.txt").write_text("a\n", encoding="utf-8")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-qm", "init")
    return workspace


def write_script(name="new.txt", content="hello\n"):
    return [
        tool_chunks(0, "c1", "write", {"path": name, "content": content}) + finish("tool_calls"),
        text_chunks("写好了 " + name) + finish("stop"),
    ]


async def run_once(b, a):
    run = (await b.call("automations/run_now", {"id": a["id"]}))["run"]
    await b.automations.wait_idle()
    return next(r for r in (await b.call("automations/runs", {}))["runs"] if r["id"] == run["id"])


def test_worktree_run_summary_and_merge(ee_home, repo):
    async def main():
        b, fake, clock = make_backend(ee_home, write_script())
        a = (await b.call("automations/create", {"automation": new_auto(b, repo, name="Nightly 检查", mode="auto",
                                                                         worktree=True)}))["automation"]
        r = await run_once(b, a)
        assert r["status"] == "completed"
        wt = r["worktree"]
        # 命名：自动化名 + 运行时间
        assert wt["branch"].startswith("ee/auto-Nightly-检查-20261026-1000")
        assert Path(wt["path"]).is_dir() and wt["state"] == "pending"
        assert [f["path"] for f in wt["files"]] == ["new.txt"] and wt["additions"] == 1 and wt["file_count"] == 1
        assert not (repo / "new.txt").exists()                       # 主目录不动
        # 运行历史：开始/结束/用时/用量/会话
        assert r["finished"] >= r["started"] and r["duration_ms"] >= 0 and r["thread_id"]
        assert r["usage"]["prompt"] == 200 and "cost" in r["usage"]
        # 合并：主目录拿到文件，worktree 目录和分支都清掉
        res = await b.call("automations/worktree", {"run_id": r["id"], "action": "merge"})
        assert res["run"]["worktree"]["state"] == "merged"
        assert (repo / "new.txt").read_text(encoding="utf-8") == "hello\n"
        assert not Path(wt["path"]).exists()
        assert wt["branch"] not in git(repo, "branch", "--list", wt["branch"])
        with pytest.raises(RpcError, match="已经"):
            await b.call("automations/worktree", {"run_id": r["id"], "action": "discard"})
    asyncio.run(main())


def test_worktree_discard_and_squash(ee_home, repo):
    async def main():
        b, fake, clock = make_backend(ee_home, write_script("x.txt") + write_script("y.txt"))
        a = (await b.call("automations/create", {"automation": new_auto(b, repo, mode="auto", worktree=True)}))["automation"]
        r1 = await run_once(b, a)
        clock.t += 1
        r2 = await run_once(b, a)
        assert r1["worktree"]["branch"] != r2["worktree"]["branch"]
        res = await b.call("automations/worktree", {"run_id": r1["id"], "action": "discard"})
        assert res["run"]["worktree"]["state"] == "discarded"
        assert not Path(r1["worktree"]["path"]).exists() and not (repo / "x.txt").exists()
        assert not git(repo, "branch", "--list", r1["worktree"]["branch"]).strip()
        res = await b.call("automations/worktree", {"run_id": r2["id"], "action": "squash"})
        assert res["run"]["worktree"]["state"] == "merged"
        assert (repo / "y.txt").exists()
        assert "y.txt" in git(repo, "diff", "--cached", "--name-only")       # 压成一份：放进暂存区由用户提交
        with pytest.raises(RpcError, match="未知"):
            await b.call("automations/worktree", {"run_id": r2["id"], "action": "zap"})
    asyncio.run(main())


def test_same_second_runs_get_distinct_worktrees(ee_home, repo):
    async def main():
        b, fake, clock = make_backend(ee_home, [text_chunks("看过了") + finish("stop")] * 2)
        a = (await b.call("automations/create", {"automation": new_auto(b, repo, worktree=True)}))["automation"]
        # 第一次运行没有改动 → 完成后自动清理（不留一堆空 worktree）
        r1 = await run_once(b, a)
        assert r1["worktree"]["state"] == "clean" and not Path(r1["worktree"]["path"]).exists()
        assert not git(repo, "branch", "--list", r1["worktree"]["branch"]).strip()
        # 同一秒内再建一个同名 worktree：手动先占住名字，名字自动加后缀
        W.create(repo, Path(r1["worktree"]["path"]).name, None, ee_home)
        r2 = await run_once(b, a)
        assert r2["status"] == "completed" and r2["worktree"]["branch"] != r1["worktree"]["branch"]
    asyncio.run(main())


def test_failed_run_keeps_worktree_and_log(ee_home, repo):
    async def main():
        b, fake, clock = make_backend(ee_home, [FakeError(400, "请求不合法：参数 foo 错误" + "x" * 400)])
        a = (await b.call("automations/create", {"automation": new_auto(b, repo, worktree=True)}))["automation"]
        r = await run_once(b, a)
        assert r["status"] == "error"
        assert r["worktree"]["state"] == "pending" and Path(r["worktree"]["path"]).is_dir()   # 失败不清理
        assert "参数 foo" in r["log"] and len(r["log"]) > len(r["summary"])                  # 完整错误留在日志里
        assert r["thread_id"]                                                                 # 会话保留
    asyncio.run(main())


def test_start_failure_keeps_log(ee_home, workspace):
    async def main():
        b, fake, clock = make_backend(ee_home, [])
        a = (await b.call("automations/create", {"automation": new_auto(b, workspace)}))["automation"]
        shutil.rmtree(workspace)
        r = await run_once(b, a)
        assert r["status"] == "failed" and "项目目录不存在" in r["log"]
    asyncio.run(main())


def test_cannot_delete_worktree_while_running(ee_home, repo):
    async def main():
        b, fake, clock = make_backend(ee_home, [text_chunks("慢慢地检查完了", n=1) + finish("stop")], delay=0.05)
        a = (await b.call("automations/create", {"automation": new_auto(b, repo, worktree=True)}))["automation"]
        run = (await b.call("automations/run_now", {"id": a["id"]}))["run"]
        path = None
        for _ in range(200):
            await asyncio.sleep(0.02)
            cur = next(r for r in (await b.call("automations/runs", {}))["runs"] if r["id"] == run["id"])
            if cur.get("worktree"):
                path = cur["worktree"]["path"]
                break
        assert path
        lst = (await b.call("worktree/list", {"cwd": str(repo)}))["worktrees"]
        assert next(w for w in lst if W._norm(w["path"]) == W._norm(path))["in_use"].startswith("自动化")
        with pytest.raises(RpcError, match="自动化"):
            await b.call("worktree/remove", {"path": path, "force": True, "delete_branch": True})
        with pytest.raises(RpcError, match="运行"):
            await b.call("automations/worktree", {"run_id": run["id"], "action": "discard"})
        await b.automations.wait_idle()
    asyncio.run(main())


def test_merge_conflicting_dirty_main_reports_error(ee_home, repo):
    async def main():
        b, fake, clock = make_backend(ee_home, write_script())
        a = (await b.call("automations/create", {"automation": new_auto(b, repo, mode="auto", worktree=True)}))["automation"]
        r = await run_once(b, a)
        (repo / "a.txt").write_text("dirty\n", encoding="utf-8")
        with pytest.raises(RpcError, match="未提交"):
            await b.call("automations/worktree", {"run_id": r["id"], "action": "merge"})
        cur = next(x for x in (await b.call("automations/runs", {}))["runs"] if x["id"] == r["id"])
        assert cur["worktree"]["state"] == "pending"                 # 合并失败：保持待处理
        # worktree 被别处删掉：标记为 gone
        W.remove(r["worktree"]["path"], True, True, ee_home)
        res = await b.call("automations/worktree", {"run_id": r["id"], "action": "discard"})
        assert res["run"]["worktree"]["state"] == "gone"
    asyncio.run(main())
