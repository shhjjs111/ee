"""自动化 / 调度规则的边界回归测试：dom/dow 与 */n 的组合、夏令时回拨时下次时间必须严格晚于、
非 ASCII 数字报中文错误、删除正在运行的自动化不留幽灵记录、重启后清理遗留的“运行中”记录。"""
import asyncio
import json
import time
from datetime import datetime

import pytest
from conftest import text_chunks, finish
from test_automations import make_backend, new_auto, ts

from ee.core import schedule as SC


def test_cron_dom_step_with_dow_uses_and():
    # 日期字段以 * 开头（*/2）时按 cron 惯例不算“受限”，日和星期取“且”：
    # 2026-11 的周一是 2、9、16、23、30，其中单数日（*/2 = 1,3,5…）是 9 和 23
    c = SC.Cron("0 0 */2 * 1")
    assert c.next_after(datetime(2026, 11, 1)) == datetime(2026, 11, 9, 0, 0)
    assert c.next_after(datetime(2026, 11, 9, 0, 0)) == datetime(2026, 11, 23, 0, 0)
    # 星期字段 */2（周日、二、四、六）+ 日期 1：只有 1 号恰好是这些星期时才触发
    c2 = SC.Cron("0 0 1 * */2")
    got = c2.next_after(datetime(2026, 10, 26))
    assert got.day == 1 and (got.weekday() + 1) % 7 in {0, 2, 4, 6}
    # 纯 * 时语义不变
    assert SC.Cron("0 12 * * 5").next_after(datetime(2026, 11, 1)) == datetime(2026, 11, 6, 12, 0)
    assert SC.Cron("0 12 13 * *").next_after(datetime(2026, 11, 1)) == datetime(2026, 11, 13, 12, 0)


@pytest.mark.parametrize("expr", ["² * * * *", "*/² * * * *", "0 ³ * * *"])
def test_cron_non_ascii_digits_raise_schedule_error(expr):
    with pytest.raises(SC.ScheduleError):
        SC.Cron(expr)


@pytest.fixture
def new_york_tz(monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("需要 time.tzset（POSIX）")
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_next_run_strictly_after_during_dst_fall_back(new_york_tz):
    # 2026-11-01 美东 01:00–02:00 出现两次；after 落在第二次（EST）的 01:30
    first = datetime(2026, 11, 1, 1, 30).timestamp()
    second = first + 3600
    assert datetime.fromtimestamp(second).hour == 1
    for expr in ("* * * * *", "*/10 * * * *"):
        nxt = SC.next_run({"kind": "cron", "expr": expr}, second)
        assert nxt > second, expr
        assert nxt - second <= 600, expr
    runs = SC.next_runs({"kind": "cron", "expr": "*/10 * * * *"}, second, 4)
    assert all(b > a for a, b in zip([second] + runs, runs))
    # 每天 01:30：从第一次 01:30 起，下一次是第二天（不会算出比 after 更早的时间）
    nxt = SC.next_run({"kind": "daily", "time": "01:30", "weekdays": []}, first)
    assert nxt > first


def test_delete_running_automation_leaves_no_ghost_run(ee_home, workspace):
    async def main():
        b, fake, clock = make_backend(ee_home, [text_chunks("慢慢完成", n=3) + finish("stop")], delay=0.1)
        seen = []
        b.listeners.append(lambda m, p: seen.append((m, p)))
        a = (await b.call("automations/create", {"automation": new_auto(b, workspace)}))["automation"]
        await b.call("automations/run_now", {"id": a["id"]})
        await asyncio.sleep(0.05)
        res = await b.call("automations/delete", {"id": a["id"]})
        assert res["unread"] == 0
        await b.automations.wait_idle()
        r = await b.call("automations/runs", {})
        assert [x for x in r["runs"] if x.get("automation_id") == a["id"]] == []
        assert r["unread"] == 0
        assert (await b.call("automations/list", {}))["unread"] == 0
        # 删除之后不再推这次运行的结果（桌面端不会把它加回列表、也不弹通知）
        i = next(i for i, (m, _) in enumerate(seen) if m == "automation/deleted")
        assert not [m for m, _ in seen[i + 1:] if m in ("automation/run_updated", "automation/run_done")]
    asyncio.run(main())


def test_stale_running_runs_cleaned_on_start(ee_home, workspace):
    async def main():
        b, fake, clock = make_backend(ee_home, [])
        a = (await b.call("automations/create", {"automation": new_auto(b, workspace)}))["automation"]
        # 模拟上次进程被强杀：文件里留着 status=running 的记录
        def fn(d):
            d["runs"].append({"id": "ru_stale", "automation_id": a["id"], "name": a["name"], "trigger": "schedule",
                              "started": clock.t - 100, "finished": None, "status": "running", "thread_id": None,
                              "summary": "", "unread": False})
            for x in d["automations"]:
                x["last_status"], x["last_run_id"] = "running", "ru_stale"
        b.automations.store.update(fn)

        b2, _, _ = make_backend(ee_home, [], t0=clock.t)
        b2.automations.startup_delay = 3600
        b2.automations.start()
        try:
            d = json.loads((ee_home / "automations.json").read_text(encoding="utf-8"))
            run = next(r for r in d["runs"] if r["id"] == "ru_stale")
            assert run["status"] == "interrupted" and run["finished"] is not None
            assert d["automations"][0]["last_status"] == "interrupted"
            r = await b2.call("automations/runs", {})
            assert all(x["status"] != "running" for x in r["runs"])
        finally:
            await b2.automations.stop()
    asyncio.run(main())
