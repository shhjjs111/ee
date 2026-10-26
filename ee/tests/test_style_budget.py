"""D5-9 回答风格（注入位置稳定、切换只追加）与花费上限（80% / 100%、仅提醒 / 暂停询问、跨日跨月、
并发子代理累计、exec 与自动化超限停止）。"""
import asyncio
import json
import threading
from datetime import datetime

import pytest
from conftest import FakeClient, finish, make_session, run, text_chunks

from ee.core import styles as ST
from ee.core.budget import BudgetGuard, Ledger, settings as budget_settings
from ee.core.context import system_prompt
from ee.core.events import ApprovalDecision, ApprovalRequest, Notice, TurnDone

M = 1_000_000          # flash：未命中 2 元/百万 → 一次 prompt=M 的请求花 ¥2


@pytest.fixture(autouse=True)
def _no_auto_compact(monkeypatch):
    """prompt=M 会顶满上下文窗口触发自动压缩；这里只关心花费。"""
    from ee import config
    monkeypatch.setitem(config.DEFAULTS, "compact", {**config.DEFAULTS["compact"], "auto": False})


def answer(text="好", prompt=M):
    return text_chunks(text) + finish("stop", prompt=prompt, completion=0)


async def turn(s, text, decide=None):
    evs = []
    async for ev in s.run_turn(text):
        evs.append(ev)
        if isinstance(ev, ApprovalRequest):
            ev.future.set_result(ApprovalDecision(decide(ev) if decide else "once"))
    return evs


def done(evs):
    return [e for e in evs if isinstance(e, TurnDone)][-1]


def notices(evs):
    return [e.text for e in evs if isinstance(e, Notice)]


# ------------------------------------------------------------------ 回答风格

def _user_styles(home):
    """内置只有 默认/简洁；切换机制用两个用户风格文件来测。"""
    d = home / "styles"
    d.mkdir(exist_ok=True)
    (d / "tutor.md").write_text("# 作业辅导\n引导思考，不直接给出完整答案。\n", encoding="utf-8")
    (d / "explain.md").write_text("# 讲解\n边做边解释原理。\n", encoding="utf-8")


def test_builtin_styles_listed_and_user_style(ee_home):
    ids = [s.id for s in ST.available(ee_home)]
    assert ids == ["default", "concise"]
    assert ST.get(ee_home, "concise").label == "简洁"
    (ee_home / "styles").mkdir()
    (ee_home / "styles" / "pirate.md").write_text("# 海盗腔\n用海盗的口吻回答。\n", encoding="utf-8")
    assert ST.get(ee_home, "pirate").label == "海盗腔"
    assert ST.get(ee_home, "nope").id == "default"


def test_default_style_keeps_system_prompt_bytes(ee_home, workspace):
    s, fake = make_session(workspace, [answer()])
    run(turn(s, "hi"))
    assert fake.calls[0]["messages"][0]["content"] == system_prompt(s.profile, "", None, s.tools, s.agent_profile)
    assert "<response-style" not in json.dumps(fake.calls[0]["messages"], ensure_ascii=False)


def test_style_frozen_in_system_and_switch_appends_only(ee_home, workspace):
    _user_styles(ee_home)
    s, fake = make_session(workspace, [answer("1"), answer("2"), answer("3"), answer("4")])
    s.set_style("tutor")
    run(turn(s, "第一题怎么做"))
    run(turn(s, "然后呢"))
    sys0 = fake.calls[0]["messages"][0]["content"]
    assert sys0.endswith(ST.system_segment(ST.get(ee_home, "tutor")) + "\n")
    assert fake.calls[1]["messages"][0]["content"] == sys0                 # 每次请求 system 字节不变
    assert "<response-style" not in json.dumps(fake.calls[1]["messages"], ensure_ascii=False)
    s.set_style("explain")                                                 # 会话中途切换
    run(turn(s, "讲讲原理"))
    c2 = fake.calls[2]["messages"]
    assert c2[0]["content"] == sys0                                         # system 不动 → 缓存不失效
    prev = fake.calls[1]["messages"]
    assert c2[:len(prev)] == prev                                           # 之前的消息原样是前缀（只追加）
    last_user = c2[-1]["content"]
    assert "<response-style>" in last_user and "讲解" in last_user and 'mode="delta"' in last_user
    run(turn(s, "好的"))
    c3 = fake.calls[3]["messages"][-1]["content"]
    assert "<response-style" not in c3                                      # 之后没变化就不再发
    assert s.current_prefix().sha256 == s.prefix.sha256                    # 切风格不触发“前缀变了”的警告


def test_style_switch_back_to_default_and_compaction_baseline(ee_home, workspace):
    s, fake = make_session(workspace, [answer("1"), answer("2"), answer("摘要"), answer("3")])
    s.set_style("concise")
    run(turn(s, "a"))
    s.set_style("default")
    run(turn(s, "b"))
    assert "回答风格已切换为：默认" in fake.calls[1]["messages"][-1]["content"]
    run(s.compact())
    summary = s.history[-1]["content"]
    assert "回答风格已切换为：默认" in summary                              # 压缩后的全量附录里仍说明当前风格
    run(turn(s, "c"))
    assert fake.calls[3]["messages"][0]["content"] == fake.calls[0]["messages"][0]["content"]


def test_style_default_from_config_and_subagent_has_no_style(ee_home, workspace):
    _user_styles(ee_home)
    (ee_home / "config.toml").write_text('[style]\ndefault = "explain"\n', encoding="utf-8")
    from test_subagent_parallel import RouterClient, report, session, task_call
    cli = RouterClient({"parent": [task_call(0, "t1", "查A") + finish("tool_calls"), report("好")],
                        "查A": [report("A")]})
    s = session(workspace, cli)
    assert s.style == "explain"
    run(turn(s, "开工"))
    sysmsgs = {k: p["messages"][0]["content"] for k, p in cli.calls}
    assert "回答风格：讲解" in sysmsgs["parent"] and "回答风格" not in sysmsgs["查A"]


# ------------------------------------------------------------------ 账本：跨日、跨月、原子、并发

def ts(*a):
    return datetime(*a).timestamp()


def test_ledger_day_and_month_rollover(ee_home):
    now = {"t": ts(2026, 10, 31, 23, 50)}
    led = Ledger(ee_home, clock=lambda: now["t"])
    led.add(1.5)
    led.add(0.5, "$")
    assert led.totals() == (1.5, 1.5) and led.totals("$") == (0.5, 0.5)
    now["t"] = ts(2026, 11, 1, 0, 10)                 # 跨日 + 跨月
    assert led.totals() == (0.0, 0.0)
    led.add(2.0)
    assert led.totals() == (2.0, 2.0)
    now["t"] = ts(2026, 11, 2, 9, 0)                  # 只跨日
    led.add(1.0)
    assert led.totals() == (1.0, 3.0)
    d = json.loads((ee_home / "state" / "spend.json").read_text(encoding="utf-8"))
    assert d["days"]["2026-10-31"]["¥"] == 1.5 and d["months"]["2026-11"]["¥"] == 3.0
    assert not list((ee_home / "state").glob("*.tmp")) and not (ee_home / "state" / "spend.json.lock").exists()
    # 另一个进程（新的 Ledger 对象）读到同样的数
    assert Ledger(ee_home, clock=lambda: now["t"]).totals() == (1.0, 3.0)


def test_ledger_prunes_old_days(ee_home):
    led = Ledger(ee_home)
    for day in range(1, 31):
        for month in (1, 2, 3):
            led.add(0.1, when=ts(2026, month, min(day, 28), 12))
    d = json.loads((ee_home / "state" / "spend.json").read_text(encoding="utf-8"))
    assert len(d["days"]) <= 62 and "2026-03-28" in d["days"] and "2026-01-01" not in d["days"]


def test_ledger_concurrent_threads_atomic(ee_home):
    led = Ledger(ee_home)

    def work():
        for _ in range(25):
            led.add(0.01)
    ths = [threading.Thread(target=work) for _ in range(8)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert led.totals()[0] == pytest.approx(2.0)


def test_ledger_corrupt_file_and_stale_lock(ee_home):
    p = ee_home / "state" / "spend.json"
    p.parent.mkdir(parents=True)
    p.write_text("{坏的", encoding="utf-8")
    lock = p.with_name("spend.json.lock")
    lock.write_text("1", encoding="utf-8")
    import os
    import time
    old = time.time() - 120
    os.utime(lock, (old, old))                         # 残留的旧锁会被清掉
    led = Ledger(ee_home)
    led.add(1.0)
    assert led.totals() == (1.0, 1.0)


def test_budget_settings_normalized():
    b = budget_settings({"budget": {"daily": "5", "action": "瞎写", "warn_ratio": 5, "session": -1}})
    assert b["daily"] == 5.0 and b["action"] == "warn" and b["warn_ratio"] == 0.99 and b["session"] == 0.0


# ------------------------------------------------------------------ 80% / 100%

def test_session_warn_at_80_and_full_with_warn_action(ee_home, workspace):
    s, fake = make_session(workspace, [answer() for _ in range(4)])
    s.config.data["budget"].update({"session": 5, "action": "warn"})
    e1 = run(turn(s, "1"))                    # 花 2（40%）
    assert not [n for n in notices(e1) if "花费" in n]
    e2 = run(turn(s, "2"))                    # 请求前 2 < 4；请求后 4
    assert not [n for n in notices(e2) if "花费" in n]
    e3 = run(turn(s, "3"))                    # 请求前 4 ≥ 80% → 提示一次
    warn = [n for n in notices(e3) if "花费" in n]
    assert len(warn) == 1 and "80%" in warn[0] and "本会话" in warn[0]
    e4 = run(turn(s, "4"))                    # 请求前 6 ≥ 100%，仅提醒 → 继续
    full = [n for n in notices(e4) if "花费" in n]
    assert len(full) == 1 and "仅提醒" in full[0]
    assert done(e4).reason == "completed" and len(fake.calls) == 4


def test_pause_asks_and_respects_choices(ee_home, workspace):
    s, fake = make_session(workspace, [answer(), answer(), answer(), answer()])
    s.config.data["budget"].update({"session": 1, "action": "pause"})
    run(turn(s, "1"))                                              # 花 2，超了
    asked = []
    e = run(turn(s, "2", lambda ev: asked.append(ev) or "deny"))
    assert done(e).reason == "budget" and len(fake.calls) == 1     # 拒绝：不发请求
    assert asked[0].tool == "budget" and asked[0].force and "是否继续" in asked[0].title
    assert any("已按你的选择停止" in n for n in notices(e))
    e = run(turn(s, "3", lambda ev: asked.append(ev) or "once"))   # 继续这一回合
    assert done(e).reason == "completed" and len(asked) == 2
    e = run(turn(s, "4", lambda ev: asked.append(ev) or "always"))  # 新回合再问；选本会话不再询问
    assert done(e).reason == "completed" and len(asked) == 3
    e = run(turn(s, "5"))
    assert done(e).reason == "completed" and len(asked) == 3 and len(fake.calls) == 4


def test_daily_and_monthly_limits_count_previous_sessions(ee_home, workspace):
    Ledger(ee_home).add(9.0)                                        # 今天别的会话已经花了 9
    s, fake = make_session(workspace, [answer()])
    s.config.data["budget"].update({"daily": 10, "monthly": 100, "action": "pause"})
    e = run(turn(s, "a"))
    assert any("今日" in n and "80%" in n for n in notices(e))
    assert Ledger(ee_home).totals() == (11.0, 11.0)                 # 本次的花费也记进账本
    s2, fake2 = make_session(workspace, [])
    s2.config.data["budget"].update({"daily": 10, "action": "pause"})
    e = run(turn(s2, "b", lambda ev: "deny"))
    assert done(e).reason == "budget" and fake2.calls == []


def test_month_limit_resets_next_month(ee_home, workspace):
    now = {"t": ts(2026, 10, 31, 22)}
    Ledger(ee_home, clock=lambda: now["t"]).add(50.0)
    s, fake = make_session(workspace, [answer()])
    s.config.data["budget"].update({"monthly": 50, "action": "pause"})
    s.budget = BudgetGuard(s.config.data, ee_home, clock=lambda: now["t"])
    e = run(turn(s, "a", lambda ev: "deny"))
    assert done(e).reason == "budget"
    now["t"] = ts(2026, 11, 1, 8)                                     # 下个月：从 0 开始
    e = run(turn(s, "b"))
    assert done(e).reason == "completed" and len(fake.calls) == 1


def test_unattended_stops_even_with_warn_action(ee_home, workspace):
    s, fake = make_session(workspace, [answer()])
    s.config.data["budget"].update({"session": 1, "action": "warn", "unattended": True})
    s.spent_before = {"¥": 1.0}                                      # 重启前已经花到上限
    s.budget.reload(s.config.data)
    e = run(turn(s, "a"))
    assert done(e).reason == "budget" and fake.calls == []
    assert "无人值守" in s.budget.stop_reason


def test_thread_restart_counts_previous_session_spend(ee_home, workspace):
    from ee.core.providers.openai_chat import ChatClient
    from ee.server.app import Backend
    (ee_home / "config.toml").write_text('[budget]\nsession = 3\naction = "pause"\n', encoding="utf-8")
    fake = FakeClient([answer(), answer()])

    async def main():
        b = Backend(home=ee_home, client_factory=lambda sp: ChatClient(sp, client=fake))
        t = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        await b.call("turn/start", {"thread_id": t["id"], "text": "1"})
        await b.threads[t["id"]].task
        st = await b.call("budget/status", {"thread_id": t["id"]})
        assert st["scopes"]["session"]["spent"] == pytest.approx(2.0) and st["scopes"]["daily"]["spent"] == pytest.approx(2.0)
        b2 = Backend(home=ee_home, client_factory=lambda sp: ChatClient(sp, client=fake))   # 重启
        await b2.call("turn/start", {"thread_id": t["id"], "text": "2"})
        th = b2.threads[t["id"]]
        await th.task
        await b2.call("turn/start", {"thread_id": t["id"], "text": "3"})     # 2+2 = 4 ≥ 3 → 卡片
        for _ in range(100):
            if th.approvals:
                break
            await asyncio.sleep(0.01)
        (cid, (fut, it)), = th.approvals.items()
        assert it["tool"] == "budget" and it["rule"] == ""
        th.respond(cid, "deny")
        await th.task
        return th
    th = asyncio.run(main())
    assert th.items[-1]["type"] == "turn_end" and th.items[-1]["reason"] == "budget"
    assert len(fake.calls) == 2


# ------------------------------------------------------------------ 并发子代理

def test_parallel_subagents_spend_accumulates_once(ee_home, workspace):
    from test_subagent_parallel import RouterClient, session, task_call
    big = lambda t: text_chunks(t) + finish("stop", prompt=M, completion=0)   # noqa: E731
    cli = RouterClient({
        "parent": [task_call(0, "t1", "A") + task_call(1, "t2", "B") + task_call(2, "t3", "C")
                   + finish("tool_calls", prompt=M, completion=0), big("汇总")],
        "A": [big("a")], "B": [big("b")], "C": [big("c")],
    }, delays={"A": 0.05, "B": 0.05, "C": 0.05})
    s = session(workspace, cli)
    s.config.data["budget"].update({"daily": 100})
    run(turn(s, "开工"))
    assert cli.peak >= 2                                             # 确实并发
    assert s.stats.cost == pytest.approx(10.0)                        # 主会话 2 次 + 3 个子代理各 1 次
    assert Ledger(ee_home).totals() == (pytest.approx(10.0), pytest.approx(10.0))   # 账本不重复记


def test_parallel_subagents_over_limit_ask_once(ee_home, workspace):
    from test_subagent_parallel import RouterClient, session, task_call
    big = lambda t: text_chunks(t) + finish("stop", prompt=M, completion=0)   # noqa: E731
    cli = RouterClient({
        "parent": [task_call(0, "t1", "A") + task_call(1, "t2", "B") + task_call(2, "t3", "C")
                   + finish("tool_calls", prompt=M, completion=0), big("汇总")],
        "A": [big("a")], "B": [big("b")], "C": [big("c")],
    })
    s = session(workspace, cli)
    s.config.data["budget"].update({"session": 1, "action": "pause"})
    asked = []
    e = run(turn(s, "开工", lambda ev: asked.append(ev) or "deny"))
    # 主会话第一次请求后已超（¥2 ≥ 1）：三个子代理同时到线只问一次；拒绝后都停，主会话也停
    assert len(asked) == 1
    assert [k for k, _ in cli.calls] == ["parent"]
    assert done(e).reason == "budget"
    results = [m["content"] for m in s.history if m["role"] == "tool"]
    assert len(results) == 3 and all('status="budget"' in r for r in results)


# ------------------------------------------------------------------ ee exec / 自动化

def test_exec_stops_when_over_budget(ee_home, workspace):
    from test_exec import events_of, run_exec
    Ledger(ee_home).add(5.0)
    (ee_home / "config.toml").write_text('[budget]\ndaily = 5\naction = "warn"\n', encoding="utf-8")
    code, out, err, fake = run_exec(ee_home, [answer()], ["-C", str(workspace), "--json", "干活"])
    assert code == 5 and fake.calls == []
    last = events_of(out)[-1]
    assert last["type"] == "turn_done" and last["reason"] == "budget" and last["exit_code"] == 5
    assert "今日花费" in last["stop_reason"] and "无人值守" in last["stop_reason"]
    code, out, err, _ = run_exec(ee_home, [answer()], ["-C", str(workspace), "干活"])
    assert code == 5 and "达到花费上限" in err


def test_exec_within_budget_records_spend(ee_home, workspace):
    from test_exec import run_exec
    (ee_home / "config.toml").write_text('[budget]\ndaily = 5\n', encoding="utf-8")
    code, out, err, fake = run_exec(ee_home, [answer("完成")], ["-C", str(workspace), "干活"])
    assert code == 0 and out == "完成\n"
    assert Ledger(ee_home).totals() == (2.0, 2.0)


def test_automation_run_stops_and_records_reason(ee_home, workspace):
    from test_automations import make_backend, new_auto
    Ledger(ee_home).add(100.0)
    (ee_home / "config.toml").write_text('[budget]\nmonthly = 50\naction = "pause"\n', encoding="utf-8")

    async def main():
        b, fake, clock = make_backend(ee_home, [answer()])
        a = (await b.call("automations/create", {"automation": new_auto(b, workspace)}))["automation"]
        await b.call("automations/run_now", {"id": a["id"]})
        await b.automations.wait_idle()
        return (await b.call("automations/runs", {}))["runs"][0], fake
    r, fake = asyncio.run(main())
    assert r["status"] == "budget" and fake.calls == []
    assert "本月花费" in r["summary"] and "无人值守" in r["summary"]


# ------------------------------------------------------------------ 设置

def test_settings_roundtrip_and_validation(ee_home):
    from ee.server import settings as S
    r = S.set_settings({"budget": {"session": 2, "daily": "10", "monthly": 0, "action": "pause", "warn_ratio": 0.9},
                        "roles": {"title": "deepseek-pro", "compact": ""}, "reasoning": {"effort": "off"},
                        "style": {"default": "concise"}, "title": {"ai": True}})["settings"]
    assert r["budget"]["daily"] == 10 and r["budget"]["action"] == "pause" and r["budget"]["warn_ratio"] == 0.9
    assert r["roles"]["title"] == "deepseek-pro" and r["reasoning"]["effort"] == "off"
    assert r["style"]["default"] == "concise" and r["title"]["ai"] is True
    for bad in ({"budget": {"daily": -1}}, {"budget": {"action": "explode"}}, {"roles": {"title": "没有的模型"}},
                {"roles": {"nope": ""}}, {"reasoning": {"effort": "超高"}}, {"style": {"default": "nope"}}):
        with pytest.raises(ValueError):
            S.set_settings(bad)
    choices = S.get_settings()["choices"]
    assert choices["efforts"] == ["", "low", "medium", "high", "off"] and [x["id"] for x in choices["styles"]] == ["default", "concise"]


def test_budget_status_rpc_without_thread(ee_home):
    from ee.server.app import Backend
    Ledger(ee_home).add(1.25)
    (ee_home / "config.toml").write_text('[budget]\ndaily = 5\n', encoding="utf-8")
    st = asyncio.run(Backend(home=ee_home).call("budget/status", {}))
    assert st["scopes"]["daily"] == {"spent": 1.25, "limit": 5.0} and st["currency"] == "¥"
