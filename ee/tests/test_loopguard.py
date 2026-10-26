from conftest import collect, finish, make_session, run, text_chunks, tool_chunks

from ee.core.events import ToolEnd, TurnDone
from ee.core.loopguard import LoopGuard, signature


def done(ev):
    return [e for e in ev if isinstance(e, TurnDone)][-1]


def same_read(i):
    return tool_chunks(0, f"c{i}", "read", {"path": "a.txt"}) + finish("tool_calls")


def paired(s):
    ids = [tc["id"] for m in s.history if m["role"] == "assistant" for tc in m.get("tool_calls", [])]
    return sorted(ids) == sorted(m["tool_call_id"] for m in s.history if m["role"] == "tool")


def test_third_identical_call_blocked(ee_home, workspace):
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    s, fake = make_session(workspace, [same_read(1), same_read(2), same_read(3), text_chunks("换思路") + finish("stop")])
    ev = run(collect(s, "go"))
    ends = [e for e in ev if isinstance(e, ToolEnd)]
    assert [e.ok for e in ends] == [True, True, False]
    third = [m for m in s.history if m["role"] == "tool"][2]["content"]
    assert "连续 3 次" in third and "没有执行" in third
    assert done(ev).reason == "completed" and paired(s)


def test_hard_cap_stops_turn(ee_home, workspace):
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    s, fake = make_session(workspace, [same_read(i) for i in range(5)])
    ev = run(collect(s, "go"))
    assert done(ev).reason == "loop"
    assert len(fake.calls) == 5 and paired(s)
    # 下一回合重新计数，能正常对话
    fake.script.append(text_chunks("好") + finish("stop"))
    assert done(run(collect(s, "继续"))).reason == "completed"


def test_different_args_not_blocked(ee_home, workspace):
    (workspace / "a.txt").write_text("x\ny\n", encoding="utf-8")
    script = [tool_chunks(0, f"c{i}", "read", {"path": "a.txt", "offset": 1, "limit": i + 1}) + finish("tool_calls")
              for i in range(4)] + [text_chunks("ok") + finish("stop")]
    s, fake = make_session(workspace, script)
    ev = run(collect(s, "go"))
    assert all(e.ok for e in ev if isinstance(e, ToolEnd)) and done(ev).reason == "completed"


def test_no_progress_note_then_stop(ee_home, workspace):
    script = [tool_chunks(0, f"c{i}", "read", {"path": f"missing{i}.txt"}) + finish("tool_calls") for i in range(4)]
    s, fake = make_session(workspace, script)
    s.config.data["loop_guard"].update({"no_progress_steps": 2, "no_progress_hard": 4})
    ev = run(collect(s, "go"))
    notes = [m for m in s.history if m.get("_kind") == "note"]
    assert len(notes) == 1 and "没有新的进展" in notes[0]["content"]
    # 提醒在第 2 步之后、第 3 次请求之前送出
    assert any("没有新的进展" in (m.get("content") or "") for m in fake.calls[2]["messages"])
    assert not any("没有新的进展" in (m.get("content") or "") for m in fake.calls[1]["messages"])
    assert done(ev).reason == "loop" and len(fake.calls) == 4 and paired(s)


def test_guard_unit_oscillation():
    g = LoopGuard(no_progress_steps=3, no_progress_hard=0)
    a, b = signature("read", {"path": "a"}), signature("read", {"path": "b"})
    assert g.end_step([(a, True)]) is None
    assert g.end_step([(b, True)]) is None
    assert g.end_step([(a, True)]) is None and g.stale_steps == 1
    assert g.end_step([(b, True)]) is None and g.stale_steps == 2
    assert g.end_step([(a, True)]) is not None          # A↔B 摆动 3 步 → 提醒
    assert g.end_step([(signature("read", {"path": "c"}), True)]) is None and g.stale_steps == 0
    assert signature("x", {"b": 1, "a": 2}) == signature("x", {"a": 2, "b": 1})


def test_guard_from_config():
    g = LoopGuard.from_config({"repeat_limit": 2, "repeat_hard": 1})
    assert g.repeat_limit == 2 and g.repeat_hard == 2
    assert g.check("s") == ("ok", 1) and g.check("s")[0] == "stop"
