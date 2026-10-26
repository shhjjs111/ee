"""D4-2：/review（审查对象、只读回合、结果解析）与 /commit（生成提交信息、提交）。"""
import asyncio
import shutil
import subprocess

import pytest
from conftest import finish, text_chunks, tool_chunks
from test_server import make_backend, wait_for

from ee.core import review as R
from ee.server.app import RpcError

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="需要 git")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(workspace):
    git(workspace, "init", "-q", "-b", "main")
    git(workspace, "config", "user.email", "t@example.com")
    git(workspace, "config", "user.name", "t")
    (workspace / "a.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-qm", "初始提交")
    return workspace


def test_targets_and_collect(repo):
    git(repo, "checkout", "-qb", "feat")
    (repo / "a.py").write_text("def f(x):\n    return x + 1\n", encoding="utf-8")
    git(repo, "commit", "-qam", "加一")
    (repo / "new.py").write_text("print(1)\n", encoding="utf-8")
    t = R.targets(repo)
    assert t["is_repo"] and t["branch"] == "feat" and t["base"] == "main" and t["dirty"]
    assert set(t["branches"]) == {"main", "feat"} and t["commits"][0]["subject"] == "加一"

    u = R.collect(repo, "uncommitted")
    assert "new.py" in u.diff and "return x + 1" not in u.diff
    b = R.collect(repo, "branch", "main")
    assert "return x + 1" in b.diff and "相对 main" in b.label
    c = R.collect(repo, "commit", t["commits"][0]["sha"])
    assert "return x + 1" in c.diff and "加一" in c.diff
    p = R.build_prompt(b, "重点看边界")
    assert "ee-review" in p and "重点看边界" in p and "return x + 1" in p
    with pytest.raises(R.ReviewError):
        R.collect(repo, "branch", "--output=x")
    with pytest.raises(R.ReviewError):
        R.collect(repo, "commit", "nope123")


def test_collect_errors(workspace, repo):
    with pytest.raises(R.ReviewError, match="没有未提交"):
        R.collect(repo, "uncommitted")
    with pytest.raises(R.ReviewError, match="相对 main 没有改动"):
        R.collect(repo, "branch", "main")
    assert R.collect(repo, "custom").diff == ""


def test_parse_findings():
    text = ("总评：有问题。\n```ee-review\n"
            '{"overall": "incorrect", "summary": "有一个 bug", "findings": ['
            '{"priority": 3, "title": "小问题", "body": "b", "file": "a.py", "line_start": 1, "line_end": 1},'
            '{"priority": 1, "title": "越界", "body": "x", "file": "a.py", "line_start": 2, "line_end": 2, "confidence": 0.9},'
            '{"body": "无标题被丢掉"}]}\n```')
    r = R.parse_findings(text)
    assert r["overall"] == "incorrect" and [f["title"] for f in r["findings"]] == ["越界", "小问题"]
    assert R.parse_findings("没有块") is None and R.parse_findings("```ee-review\n不是json\n```") is None


def test_review_turn_is_readonly_and_restores_mode(ee_home, repo):
    (repo / "a.py").write_text("def f(x):\n    return x[0]\n", encoding="utf-8")
    # 模型在审查回合里试图写文件：只读模式下被拒
    script = [tool_chunks(0, "w1", "write", {"path": "a.py", "content": "hacked"}) + finish("tool_calls"),
              text_chunks("有问题\n```ee-review\n{\"overall\":\"incorrect\",\"findings\":[]}\n```") + finish("stop")]

    async def go():
        b, fake, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(repo), "mode": "auto"}))["thread"]["id"]
        tg = await b.call("review/targets", {"cwd": str(repo)})
        assert tg["dirty"] and tg["branch"] == "main"
        await b.call("review/start", {"thread_id": tid, "kind": "uncommitted", "instructions": "看看越界"})
        await wait_for(ev, "turn/completed")
        assert (repo / "a.py").read_text() == "def f(x):\n    return x[0]\n"
        d = (await b.call("thread/get", {"thread_id": tid}))["thread"]
        u = [it for it in d["items"] if it["type"] == "user"][0]
        assert u["kind"] == "review" and u["text"].startswith("/review 未提交的改动") and "看看越界" in u["text"]
        sent = "\n".join(str(m.get("content")) for m in fake.calls[0]["messages"])
        assert "return x[0]" in sent and "ee-review" in sent
        assert b.threads[tid].session.policy.mode == "auto" and d["mode"] == "auto"
        with pytest.raises(RpcError, match="需要写明"):
            await b.call("review/start", {"thread_id": tid, "kind": "custom"})
    asyncio.run(go())


def test_commit_message_and_commit(ee_home, repo):
    (repo / "a.py").write_text("def f(x):\n    return x * 2\n", encoding="utf-8")
    (repo / "b.py").write_text("y = 1\n", encoding="utf-8")
    script = [text_chunks("```\n翻倍返回值\n\n- 新增 b.py\n```") + finish("stop")]

    async def go():
        b, fake, ev = make_backend(ee_home, script)
        r = await b.call("git/commit_message", {"cwd": str(repo)})
        assert r["message"] == "翻倍返回值\n\n- 新增 b.py" and r["stage_all"] is True
        assert set(r["files"]) == {"a.py", "b.py"}
        assert "初始提交" in str(fake.calls[0]["messages"])
        c = await b.call("git/commit", {"cwd": str(repo), "message": r["message"], "stage_all": True})
        assert c["ok"] and c["sha"]
        assert git(repo, "log", "-1", "--format=%B").strip() == "翻倍返回值\n\n- 新增 b.py"
        assert git(repo, "status", "--porcelain").strip() == ""
        with pytest.raises(RpcError, match="没有可提交"):
            await b.call("git/commit_message", {"cwd": str(repo)})
        with pytest.raises(RpcError, match="不能为空"):
            await b.call("git/commit", {"cwd": str(repo), "message": "  "})
    asyncio.run(go())


def test_commit_only_staged(ee_home, repo):
    (repo / "a.py").write_text("changed\n", encoding="utf-8")
    (repo / "c.py").write_text("c\n", encoding="utf-8")
    git(repo, "add", "c.py")
    script = [text_chunks("加 c.py") + finish("stop")]

    async def go():
        b, fake, _ = make_backend(ee_home, script)
        r = await b.call("git/commit_message", {"cwd": str(repo)})
        assert r["stage_all"] is False and r["files"] == ["c.py"]
        await b.call("git/commit", {"cwd": str(repo), "message": r["message"], "stage_all": False})
        assert "a.py" in git(repo, "status", "--porcelain")
    asyncio.run(go())
