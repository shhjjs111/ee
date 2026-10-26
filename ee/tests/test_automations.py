"""D4-10 自动化 / 定时任务：调度规则（cron / 每天 / 间隔）、持久化与原子写、调度循环（可注入时钟）、不并发、
错过补跑一次、运行产生 source=automation 的会话、只读拦写、结果进历史与未读、RPC 增删改查 / run_now / mark_read。"""
import asyncio
import json
import shutil
import subprocess
from datetime import datetime

import pytest
from conftest import FakeClient, finish, text_chunks, tool_chunks

from ee.core import schedule as SC
from ee.core.providers.openai_chat import ChatClient
from ee.core.store import SessionStore
from ee.server.app import Backend, RpcError
from ee.server.automations import AutomationStore


def ts(*a) -> float:
    return datetime(*a).timestamp()


def dt(t: float) -> datetime:
    return datetime.fromtimestamp(t)


# ------------------------------------------------------------------ 调度规则
def test_cron_next_times():
    c = SC.Cron("0 9 * * 1-5")
    assert c.next_after(datetime(2026, 10, 30, 10, 0)) == datetime(2026, 11, 2, 9, 0)     # 周五 10 点 → 下周一
    assert c.next_after(datetime(2026, 10, 29, 8, 59, 30)) == datetime(2026, 10, 29, 9, 0)
    assert c.next_after(datetime(2026, 10, 29, 9, 0)) == datetime(2026, 10, 30, 9, 0)      # 严格晚于
    assert SC.Cron("*/15 * * * *").next_after(datetime(2026, 1, 1, 10, 7)) == datetime(2026, 1, 1, 10, 15)
    assert SC.Cron("*/15 * * * *").next_after(datetime(2026, 1, 1, 23, 50)) == datetime(2026, 1, 2, 0, 0)
    assert SC.Cron("0 0 1 * *").next_after(datetime(2026, 12, 15, 3, 0)) == datetime(2027, 1, 1, 0, 0)   # 跨年
    assert SC.Cron("30 23 31 * *").next_after(datetime(2026, 11, 1)) == datetime(2026, 12, 31, 23, 30)   # 跳过 11 月
    assert SC.Cron("0 12 29 2 *").next_after(datetime(2026, 3, 1)) == datetime(2028, 2, 29, 12, 0)        # 闰年
    # 日和星期都限定：任一满足（2026-11-06 是周五，早于 13 号）
    assert SC.Cron("0 12 13 * 5").next_after(datetime(2026, 11, 1)) == datetime(2026, 11, 6, 12, 0)
    # 名字、7=周日、列表、范围步长、a/n
    assert SC.Cron("0 8 * jan,feb mon").next_after(datetime(2026, 10, 1)) == datetime(2027, 1, 4, 8, 0)
    assert SC.Cron("0 10 * * 7").next_after(datetime(2026, 10, 26)) == datetime(2026, 11, 1, 10, 0)
    assert SC.Cron("0 10 * * sun").next_after(datetime(2026, 10, 26)) == datetime(2026, 11, 1, 10, 0)
    assert SC.Cron("0 0-12/6 * * *").hours == {0, 6, 12}
    assert SC.Cron("5/20 * * * *").minutes == {5, 25, 45}
    assert SC.Cron("1,2,3 * * * *").minutes == {1, 2, 3}


@pytest.mark.parametrize("expr", ["", "* * * *", "* * * * * *", "60 * * * *", "a * * * *", "*/0 * * * *",
                                  "5-1 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *", "* * * * 8", "1,,2 * * * *"])
def test_cron_invalid(expr):
    with pytest.raises(SC.ScheduleError):
        SC.Cron(expr)


def test_normalize_and_never_firing():
    with pytest.raises(SC.ScheduleError, match="不会触发"):
        SC.normalize({"kind": "cron", "expr": "0 0 30 2 *"})
    with pytest.raises(SC.ScheduleError):
        SC.normalize({"kind": "daily", "time": "25:00"})
    with pytest.raises(SC.ScheduleError):
        SC.normalize({"kind": "daily", "time": "9点"})
    with pytest.raises(SC.ScheduleError):
        SC.normalize({"kind": "interval", "minutes": 0})
    with pytest.raises(SC.ScheduleError):
        SC.normalize({"kind": "weekly"})
    with pytest.raises(SC.ScheduleError):
        SC.normalize(None)
    assert SC.normalize({"kind": "daily", "time": "9:05", "weekdays": [5, 1, 7]}) == \
        {"kind": "daily", "time": "09:05", "weekdays": [0, 1, 5]}
    assert SC.normalize({"kind": "daily", "time": "09:00", "weekdays": list(range(7))})["weekdays"] == []
    assert SC.normalize({"kind": "cron", "expr": " 0  9 * * 1-5 "})["expr"] == "0 9 * * 1-5"
    assert SC.normalize({"kind": "interval", "minutes": "30"}) == {"kind": "interval", "minutes": 30}


def test_daily_and_interval_next():
    d = {"kind": "daily", "time": "23:30", "weekdays": []}
    assert dt(SC.next_run(d, ts(2026, 10, 31, 23, 45))) == datetime(2026, 11, 1, 23, 30)    # 跨日跨月
    assert dt(SC.next_run(d, ts(2026, 12, 31, 23, 31))) == datetime(2027, 1, 1, 23, 30)     # 跨年
    sat = {"kind": "daily", "time": "08:00", "weekdays": [6]}
    assert dt(SC.next_run(sat, ts(2026, 10, 26, 9))) == datetime(2026, 10, 31, 8, 0)
    work = {"kind": "daily", "time": "09:00", "weekdays": [1, 2, 3, 4, 5]}
    assert [dt(t).day for t in SC.next_runs(work, ts(2026, 10, 30, 10), 3)] == [2, 3, 4]
    iv = {"kind": "interval", "minutes": 30}
    base = ts(2026, 10, 26, 10, 0)
    assert SC.next_run(iv, base, base) == base + 1800
    assert SC.next_run(iv, base + 1799, base) == base + 1800
    assert SC.next_run(iv, base + 1800, base) == base + 3600                 # 严格晚于
    assert SC.next_run(iv, base + 3 * 3600 + 5, base) == base + 3 * 3600 + 1800   # 错过的格子跳过
    assert SC.describe(iv) == "每 30 分钟"
    assert SC.describe({"kind": "interval", "minutes": 120}) == "每 2 小时"
    assert SC.describe(work) == "工作日 09:00"
    assert SC.describe(sat) == "每周六 08:00"
    assert SC.describe(d) == "每天 23:30"


# ------------------------------------------------------------------ 持久化
def test_store_atomic_write_and_corruption(ee_home, monkeypatch):
    st = AutomationStore(ee_home)
    assert st.load()["automations"] == []
    st.update(lambda d: d["automations"].append({"id": "au_1", "name": "测试"}))
    raw = json.loads((ee_home / "automations.json").read_text(encoding="utf-8"))
    assert raw["automations"][0]["name"] == "测试"
    assert not list(ee_home.glob(".automations.json.*.tmp"))
    # 写入失败（replace 抛错）：原文件保持不变
    import os as _os

    def boom(*a, **k):
        raise OSError("磁盘满")
    monkeypatch.setattr(_os, "replace", boom)
    with pytest.raises(OSError):
        st.update(lambda d: d["automations"].append({"id": "au_2", "name": "x"}))
    monkeypatch.undo()
    assert [a["id"] for a in st.load()["automations"]] == ["au_1"]
    # 历史上限：每个自动化最多 100 条
    st.update(lambda d: d["runs"].extend({"id": f"r{i}", "automation_id": "au_1", "started": i} for i in range(130)))
    runs = st.load()["runs"]
    assert len(runs) == 100 and runs[0]["id"] == "r30" and runs[-1]["id"] == "r129"
    # 坏文件：按空处理并另存 .bad
    (ee_home / "automations.json").write_text("{坏的", encoding="utf-8")
    assert st.load()["automations"] == []
    assert (ee_home / "automations.json.bad").exists()


# ------------------------------------------------------------------ 调度 + 执行
class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make_backend(home, script, delay=0.0, t0=None):
    fake = FakeClient(script, delay=delay)
    b = Backend(home=home, client_factory=lambda spec: ChatClient(spec, client=fake))
    clock = Clock(t0 or ts(2026, 10, 26, 10, 0))
    b.automations.clock = clock
    return b, fake, clock


def new_auto(b, workspace, **kw):
    data = {"name": "每日检查", "cwd": str(workspace), "prompt": "看看项目有没有问题",
            "schedule": {"kind": "interval", "minutes": 10}}
    data.update(kw)
    return data


def test_scheduled_run_creates_automation_session(ee_home, workspace):
    async def main():
        b, fake, clock = make_backend(ee_home, [text_chunks("一切正常，没有发现问题。") + finish("stop")])
        seen = []
        b.listeners.append(lambda m, p: seen.append((m, p)))
        a = (await b.call("automations/create", {"automation": new_auto(b, workspace)}))["automation"]
        assert a["mode"] == "read-only" and a["approve"] == "deny" and a["enabled"]
        assert a["next_run"] == clock.t + 600 and a["schedule_text"] == "每 10 分钟"
        clock.t += 300
        assert await b.automations.tick_once() == []                   # 还没到点
        clock.t += 300
        runs = await b.automations.tick_once()
        assert len(runs) == 1 and runs[0]["trigger"] == "schedule" and runs[0]["status"] == "running"
        await b.automations.wait_idle()
        r = (await b.call("automations/runs", {}))
        assert r["unread"] == 1
        run = r["runs"][0]
        assert run["status"] == "completed" and run["unread"] is True and run["summary"].startswith("一切正常")
        # 会话：source=automation、automation_id、标题=自动化名字、只读
        d = SessionStore.load(ee_home / "sessions" / f"{run['thread_id']}.jsonl")
        assert d["meta"]["source"] == "automation" and d["meta"]["automation_id"] == a["id"]
        assert d["meta"]["title"] == "每日检查" and d["meta"]["mode"] == "read-only"
        assert d["items"][0]["type"] == "user" and d["items"][0]["text"] == "看看项目有没有问题"
        # 无人值守：没有 ask_user 工具
        names = [t["function"]["name"] for t in fake.calls[0].get("tools") or []]
        assert "ask_user" not in names
        # 桌面端能看到会话；通知事件齐全
        lst = (await b.call("thread/list", {}))["threads"]
        assert lst[0]["source"] == "automation"
        methods = [m for m, _ in seen]
        assert "automation/run_done" in methods and "automation/updated" in methods and "thread/updated" in methods
        done = next(p for m, p in seen if m == "automation/run_done")
        assert done["unread"] == 1 and done["run"]["thread_id"] == run["thread_id"]
        al = (await b.call("automations/list", {}))
        assert al["unread"] == 1 and al["automations"][0]["last_status"] == "completed"
        assert al["automations"][0]["next_run"] == clock.t + 600 and al["automations"][0]["running"] is False
        # 标已读（按会话 id：桌面端打开会话时）
        res = await b.call("automations/mark_read", {"thread_id": run["thread_id"]})
        assert res["unread"] == 0 and res["changed"] == [run["id"]]
    asyncio.run(main())


def test_no_concurrent_runs_skip_recorded(ee_home, workspace):
    async def main():
        b, fake, clock = make_backend(ee_home, [text_chunks("慢慢完成了这次检查", n=2) + finish("stop")], delay=0.15)
        a = (await b.call("automations/create", {"automation": new_auto(b, workspace, schedule={"kind": "interval", "minutes": 1})}))["automation"]
        clock.t += 60
        first = await b.automations.tick_once()
        assert first[0]["status"] == "running"
        await asyncio.sleep(0.2)
        with pytest.raises(RpcError, match="正在运行"):
            await b.call("automations/run_now", {"id": a["id"]})
        clock.t += 60
        second = await b.automations.tick_once()
        assert second[0]["status"] == "skipped" and second[0]["unread"] is False
        await b.automations.wait_idle()
        runs = (await b.call("automations/runs", {"automation_id": a["id"]}))["runs"]
        assert sorted(r["status"] for r in runs) == ["completed", "skipped"]
        assert len(fake.calls) == 1                                    # 只跑了一次
    asyncio.run(main())


def test_missed_runs_catch_up_once_after_restart(ee_home, workspace):
    async def main():
        b, fake, clock = make_backend(ee_home, [])
        a = (await b.call("automations/create", {"automation": new_auto(
            b, workspace, schedule={"kind": "daily", "time": "09:00", "weekdays": []})}))["automation"]
        assert dt(a["next_run"]) == datetime(2026, 10, 27, 9, 0)
        # “关机”三天后重启：错过了 27、28、29 号三次，只补跑一次
        b2, fake2, clock2 = make_backend(ee_home, [text_chunks("补跑完成") + finish("stop")],
                                         t0=ts(2026, 10, 29, 20, 0))
        runs = await b2.automations.tick_once()
        assert len(runs) == 1 and runs[0]["trigger"] == "catchup"
        await b2.automations.wait_idle()
        assert await b2.automations.tick_once() == []
        al = (await b2.call("automations/list", {}))["automations"][0]
        assert dt(al["next_run"]) == datetime(2026, 10, 30, 9, 0)
        assert len(fake2.calls) == 1
        # 准点触发（不算补跑）
        clock2.t = ts(2026, 10, 30, 9, 0, 10)
        fake2.script.append(text_chunks("准点") + finish("stop"))
        r = await b2.automations.tick_once()
        assert r[0]["trigger"] == "schedule"
        await b2.automations.wait_idle()
    asyncio.run(main())


def test_readonly_blocks_write_and_error_recorded(ee_home, workspace):
    async def main():
        script = [
            tool_chunks(0, "c1", "write", {"path": "x.txt", "content": "hi"}) + finish("tool_calls"),
            text_chunks("只读模式下写不了文件，需要你手动创建 x.txt") + finish("stop"),
        ]
        b, fake, clock = make_backend(ee_home, script)
        a = (await b.call("automations/create", {"automation": new_auto(b, workspace)}))["automation"]
        run = (await b.call("automations/run_now", {"id": a["id"]}))["run"]
        assert run["trigger"] == "manual"
        await b.automations.wait_idle()
        assert not (workspace / "x.txt").exists()
        r = (await b.call("automations/runs", {}))["runs"][0]
        assert r["status"] == "completed" and "手动" in r["summary"]
        d = SessionStore.load(ee_home / "sessions" / f"{r['thread_id']}.jsonl")
        tool = next(it for it in d["items"] if it["type"] == "tool")
        assert tool["status"] == "denied"
        # 模型出错：结果记 error 和错误信息
        from conftest import FakeError
        fake.script.append(FakeError(400, "请求不合法"))
        await b.call("automations/run_now", {"id": a["id"]})
        await b.automations.wait_idle()
        r = (await b.call("automations/runs", {}))["runs"][0]
        assert r["status"] == "error" and r["summary"] and r["unread"]
        assert (await b.call("automations/list", {}))["unread"] == 2
        await b.call("automations/mark_read", {"all": True})
        assert (await b.call("automations/list", {}))["unread"] == 0
    asyncio.run(main())


def test_default_mode_approval_auto_denied(ee_home, workspace, tmp_path):
    async def main():
        outside = tmp_path / "outside.txt"
        script = [
            tool_chunks(0, "c1", "write", {"path": str(outside), "content": "hi"}) + finish("tool_calls"),
            text_chunks("需要你手动写到外面") + finish("stop"),
        ]
        b, fake, clock = make_backend(ee_home, script)
        a = (await b.call("automations/create", {"automation": new_auto(b, workspace, mode="default")}))["automation"]
        await b.call("automations/run_now", {"id": a["id"]})
        await b.automations.wait_idle()
        assert not outside.exists()
        assert "无人值守的自动化" in json.dumps(fake.calls[1]["messages"], ensure_ascii=False)
    asyncio.run(main())


def test_rpc_crud_preview_and_validation(ee_home, workspace):
    async def main():
        b, fake, clock = make_backend(ee_home, [])
        with pytest.raises(RpcError, match="提示词"):
            await b.call("automations/create", {"automation": new_auto(b, workspace, prompt="  ")})
        with pytest.raises(RpcError, match="项目目录不存在"):
            await b.call("automations/create", {"automation": new_auto(b, workspace, cwd=str(workspace / "nope"))})
        with pytest.raises(RpcError, match="cron"):
            await b.call("automations/create", {"automation": new_auto(b, workspace, schedule={"kind": "cron", "expr": "* *"})})
        with pytest.raises(RpcError, match="权限模式"):
            await b.call("automations/create", {"automation": new_auto(b, workspace, mode="yolo")})
        a = (await b.call("automations/create", {"automation": new_auto(b, workspace, model="deepseek-flash", worktree=True)}))["automation"]
        assert a["model"] == "deepseek-flash" and a["worktree"] is True and a["timeout_minutes"] == 60
        u = (await b.call("automations/update", {"id": a["id"], "patch": {"enabled": False}}))["automation"]
        assert u["enabled"] is False and u["next_run"] is None
        assert await b.automations.tick_once() == []
        u = (await b.call("automations/update", {"id": a["id"], "patch": {
            "enabled": True, "schedule": {"kind": "cron", "expr": "0 9 * * 1-5"}, "name": "工作日早检"}}))["automation"]
        assert u["name"] == "工作日早检" and dt(u["next_run"]) == datetime(2026, 10, 27, 9, 0)
        assert u["schedule_text"] == "cron：0 9 * * 1-5"
        with pytest.raises(RpcError, match="未知字段"):
            await b.call("automations/update", {"id": a["id"], "patch": {"bogus": 1}})
        with pytest.raises(RpcError, match="不存在"):
            await b.call("automations/update", {"id": "au_nope", "patch": {"name": "x"}})
        p = await b.call("automations/preview", {"schedule": {"kind": "daily", "time": "08:30", "weekdays": [1]}})
        assert p["ok"] and p["text"] == "每周一 08:30" and dt(p["next_runs"][0]) == datetime(2026, 11, 2, 8, 30)
        assert len(p["next_runs"]) == 3
        bad = await b.call("automations/preview", {"schedule": {"kind": "cron", "expr": "99 * * * *"}})
        assert not bad["ok"] and "范围" in bad["error"]
        # 持久化：新 Backend 读得到
        b2, _, _ = make_backend(ee_home, [])
        assert (await b2.call("automations/list", {}))["automations"][0]["name"] == "工作日早检"
        # 删除连同运行历史
        b.automations.store.update(lambda d: d["runs"].append(
            {"id": "ru_x", "automation_id": a["id"], "started": 1, "unread": True}))
        res = await b.call("automations/delete", {"id": a["id"]})
        assert res["unread"] == 0
        assert (await b.call("automations/list", {}))["automations"] == []
        assert (await b.call("automations/runs", {}))["runs"] == []
        with pytest.raises(RpcError, match="不存在"):
            await b.call("automations/run_now", {"id": a["id"]})
    asyncio.run(main())


def test_manual_run_shifts_interval_and_start_failure(ee_home, workspace):
    async def main():
        b, fake, clock = make_backend(ee_home, [text_chunks("ok") + finish("stop")])
        a = (await b.call("automations/create", {"automation": new_auto(b, workspace)}))["automation"]
        clock.t += 120
        await b.call("automations/run_now", {"id": a["id"]})
        await b.automations.wait_idle()
        al = (await b.call("automations/list", {}))["automations"][0]
        assert al["next_run"] == clock.t + 600                          # 间隔从手动运行时刻重新计
        # 项目目录被删：运行记 failed，不抛
        shutil.rmtree(workspace)
        await b.call("automations/run_now", {"id": a["id"]})
        await b.automations.wait_idle()
        r = (await b.call("automations/runs", {}))["runs"][0]
        assert r["status"] == "failed" and "项目目录不存在" in r["summary"] and r["thread_id"] is None
    asyncio.run(main())


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_run_in_new_worktree(ee_home, workspace):
    def git(*a):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=workspace, check=True,
                       capture_output=True)
    git("init", "-q", "-b", "main")
    (workspace / "a.txt").write_text("a\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")

    async def main():
        b, fake, clock = make_backend(ee_home, [text_chunks("在 worktree 里看过了") + finish("stop")])
        a = (await b.call("automations/create", {"automation": new_auto(b, workspace, worktree=True)}))["automation"]
        await b.call("automations/run_now", {"id": a["id"]})
        await b.automations.wait_idle()
        r = (await b.call("automations/runs", {}))["runs"][0]
        assert r["status"] == "completed" and r["worktree"]["branch"].startswith("ee/auto-")
        d = SessionStore.load(ee_home / "sessions" / f"{r['thread_id']}.jsonl")
        assert d["meta"]["worktree"]["repo"] == str(workspace.resolve())
        assert d["meta"]["cwd"] != str(workspace.resolve())
    asyncio.run(main())


def test_scheduler_loop_starts_and_stops(ee_home, workspace):
    async def main():
        b, fake, clock = make_backend(ee_home, [text_chunks("循环触发") + finish("stop")])
        b.automations.tick = 0.05
        b.automations.startup_delay = 0
        a = (await b.call("automations/create", {"automation": new_auto(b, workspace)}))["automation"]
        b.automations.start()
        await asyncio.sleep(0.1)
        clock.t += 600
        b.automations._poke()
        for _ in range(100):
            await asyncio.sleep(0.05)
            if (await b.call("automations/runs", {}))["runs"][:1] and \
                    (await b.call("automations/runs", {}))["runs"][0]["status"] == "completed":
                break
        assert (await b.call("automations/runs", {}))["runs"][0]["status"] == "completed"
        await b.automations.stop()
        assert b.automations._loop_task is None
    asyncio.run(main())
