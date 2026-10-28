"""项目层不能放宽预算；自动化隔离失败不能把写操作落到主目录。

两处都属"看起来更安全，实际被绕过"的族：

1. `sanitize_project_layer` 过滤了权限、MCP、沙箱，但**没有过滤 budget**。项目 .ee/config.toml
   写 `session = 0`（0 = 不限）或把 `action = "pause"` 改成 `warn`，用户在用户级设的花费上限
   就被静默取消了。无人值守标志补不回已被清零的上限。
2. 自动化选择 `worktree = true`（本次运行在独立目录里改代码）时，如果建 worktree 失败，
   原实现只记一条 note 就**在项目目录里继续跑**。对会写文件的任务，这等于隔离失败后扩大成
   直接改主目录。写型任务必须 fail-closed；只读任务的回退要明示。
"""
import asyncio

import pytest
from conftest import finish, text_chunks

from ee.config import load_config
from ee.server.app import RpcError
from test_automations import make_backend, new_auto


def _proj(workspace, text: str) -> None:
    (workspace / ".git").mkdir(exist_ok=True)
    (workspace / ".ee").mkdir(exist_ok=True)
    (workspace / ".ee" / "config.toml").write_text(text, encoding="utf-8")


def _user(ee_home, text: str) -> None:
    (ee_home / "config.toml").write_text("config_version = 3\n" + text, encoding="utf-8")


# ---------------------------------------------------------------- 1) 项目只能收紧预算


def test_project_cannot_zero_user_budget(ee_home, workspace):
    _user(ee_home, '[budget]\nsession = 5.0\ndaily = 20.0\naction = "pause"\n')
    _proj(workspace, '[budget]\nsession = 0\ndaily = 0\n')
    b = load_config(workspace).data["budget"]
    assert b["session"] == 5.0, "项目把用户设的会话上限清零了"
    assert b["daily"] == 20.0


def test_project_cannot_raise_user_budget(ee_home, workspace):
    _user(ee_home, '[budget]\nsession = 3.0\n')
    _proj(workspace, '[budget]\nsession = 999\n')
    assert load_config(workspace).data["budget"]["session"] == 3.0


def test_project_can_tighten_budget(ee_home, workspace):
    _user(ee_home, '[budget]\nsession = 10.0\n')
    _proj(workspace, '[budget]\nsession = 2.0\n')
    assert load_config(workspace).data["budget"]["session"] == 2.0


def test_project_cannot_downgrade_pause_to_warn(ee_home, workspace):
    _user(ee_home, '[budget]\nsession = 5.0\naction = "pause"\n')
    _proj(workspace, '[budget]\naction = "warn"\n')
    assert load_config(workspace).data["budget"]["action"] == "pause"


def test_project_cannot_flip_currency_or_warn_ratio(ee_home, workspace):
    _user(ee_home, '[budget]\nsession = 5.0\ncurrency = "¥"\nwarn_ratio = 0.8\n')
    _proj(workspace, '[budget]\ncurrency = "$"\nwarn_ratio = 0.1\n')
    b = load_config(workspace).data["budget"]
    assert b["currency"] == "¥" and b["warn_ratio"] == 0.8


def test_effective_budget_still_stops_the_run(ee_home, workspace):
    """不只是配置值对——真正的 BudgetGuard 拿到收紧后的值要能停。"""
    _user(ee_home, '[budget]\nsession = 1.0\naction = "pause"\n')
    _proj(workspace, '[budget]\nsession = 0\naction = "warn"\n')
    from ee.core.budget import BudgetGuard

    g = BudgetGuard(load_config(workspace).data, ee_home)
    assert g.active is True
    assert g.cfg["session"] == 1.0 and g.cfg["action"] == "pause"


def test_missing_budget_section_is_fine(ee_home, workspace):
    """用户没设预算时，项目设一个上限是允许的（收紧不成问题）。"""
    _proj(workspace, '[budget]\nsession = 2.0\n')
    assert load_config(workspace).data["budget"]["session"] == 2.0


# ---------------------------------------------------------------- 2) 隔离失败不降级


async def run_once(b, a):
    run = (await b.call("automations/run_now", {"id": a["id"]}))["run"]
    await b.automations.wait_idle()
    return next(r for r in (await b.call("automations/runs", {}))["runs"] if r["id"] == run["id"])


def test_write_task_does_not_fall_back_to_main_dir(ee_home, workspace):
    """mode=auto（会改文件）+ worktree=true，建 worktree 失败 → 本次取消，主目录一个字都不改。"""
    async def main():
        b, fake, clock = make_backend(ee_home, [text_chunks("不该跑到这里") + finish("stop")])
        a = (await b.call("automations/create", {
            "automation": new_auto(b, workspace, mode="auto", worktree=True)}))["automation"]
        r = await run_once(b, a)

        assert r["status"] == "failed", r
        assert "worktree" in (r.get("log") or ""), r.get("log")
        assert list(workspace.iterdir()) == [], "隔离失败后在项目目录里继续跑了"
        assert not fake.calls, "隔离失败后仍调用了模型"

    asyncio.run(main())


def test_readonly_task_may_fall_back_with_visible_note(ee_home, workspace):
    """只读任务允许回退（它改不了文件），但运行记录要写明这次没隔离。"""
    async def main():
        b, fake, clock = make_backend(ee_home, [text_chunks("只读看过了") + finish("stop")])
        a = (await b.call("automations/create", {
            "automation": new_auto(b, workspace, mode="read-only", worktree=True)}))["automation"]
        r = await run_once(b, a)

        assert r["status"] == "completed", r
        assert "worktree" in (r.get("log") or ""), "回退没有在运行记录里说明"

    asyncio.run(main())


def test_worktree_failure_on_git_repo_is_not_masked(ee_home, workspace):
    """git 仓库但建 worktree 失败（例如分支重名）也一样：写任务不降级。"""
    import subprocess
    import shutil

    if shutil.which("git") is None:
        pytest.skip("需要 git")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=workspace, check=True,
                   capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"],
                   cwd=workspace, check=True, capture_output=True)
    (workspace / "a.txt").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"],
                   cwd=workspace, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
                   cwd=workspace, check=True, capture_output=True)

    async def main():
        b, fake, clock = make_backend(ee_home, [])
        a = (await b.call("automations/create", {
            "automation": new_auto(b, workspace, mode="auto", worktree=True)}))["automation"]
        # 用后端同一个时钟算 worktree 名字（运行记录里的 started 来自 clock），
        # 把 9 个候选名全占住，逼 _create_worktree 抛错。
        stamp = __import__("time").strftime("%Y%m%d-%H%M%S", __import__("time").localtime(clock.t))
        from ee.core import worktree as W
        for i in range(1, 10):
            name = f"auto-每日检查-{stamp}" + ("" if i == 1 else f"-{i}")
            try:
                W.create(workspace, name, None, ee_home)
            except W.WorktreeError:
                pass
        r = await run_once(b, a)
        assert r["status"] == "failed", r
        assert "worktree" in (r.get("log") or "")
        assert not (workspace / "new.txt").exists()

    asyncio.run(main())
