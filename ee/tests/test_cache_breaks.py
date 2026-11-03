"""断点归因的端到端验证（P0-1）：真实 Session 走一遍，看 record 上的 wire/new/gap/break 对不对。

单元层面 UsageStats 的判定已在 test_cache_attribution.py 覆盖；这里守的是接线——
note_wire() 有没有在每次主对话发送前被调用、_compact() 有没有把 break_reason 传下去。
两条线任一断了，界面上看到的「断点」就永远是 0，用户也就永远不知道命中率为什么掉了。
"""
import pytest

from conftest import FakeClient, collect, finish, run, text_chunks

from ee.core.events import Compacted


def make(workspace, script, window=None):
    from ee.config import load_config
    from ee.core.session import Session
    cfg = load_config(workspace)
    if window:
        cfg.data["models"]["deepseek-flash"]["context_window"] = window
    return Session(cfg, cwd=workspace, client=FakeClient(script), persist=False)


def main_records(s):
    return [r for r in s.stats.records if (r.get("role") or "main") == "main"]


def test_plain_turn_reports_growth_not_a_break(ee_home, workspace):
    """普通追加回合：第二条请求的未命中只该是这次新增的那点量，不该标断点。"""
    s = make(workspace, [
        text_chunks("答一" + "扩" * 200) + finish("stop", prompt=2000, completion=50, hit=0),
        text_chunks("答二") + finish("stop", prompt=2400, completion=50, hit=1700),
    ])
    run(collect(s, "问一" + "长" * 200))
    run(collect(s, "问二" + "长" * 200))
    recs = main_records(s)
    assert len(recs) == 2
    first, second = recs
    # 首条：服务端什么都没命中（冷启动），wire 只被记下，没有上一次可比
    assert first["wire"] > 0 and first["new"] == first["wire"]
    assert "break" not in first
    assert second["new"] > 0
    assert second["gap"] == 2400 - 1700
    assert second["gap"] <= second["new"] * 1.5 + 1000, "正常追加不该被判成断点"
    assert "break" not in second


def test_compaction_marks_the_next_request_as_a_break(ee_home, workspace):
    """压缩重建历史后，下一次主对话请求的未命中远超新增 → 必须标成 break=compact。"""
    s = make(workspace, [
        text_chunks("答一" + "扩" * 400) + finish("stop", prompt=8000, completion=50, hit=7500),
        # 压缩请求自己：命中大部分历史，返回一份摘要
        text_chunks("## 用户目标\n继续\n## 下一步\n干活") + finish("stop", prompt=8200, completion=50),
        # 压缩后的第一条主对话请求：历史被换掉，服务端只能命中没变的那一小段
        text_chunks("答二") + finish("stop", prompt=5000, completion=50, hit=800),
    ])
    run(collect(s, "问一" + "长" * 3000))
    events = []
    res = run(s.compact(events.append))
    assert res["ok"]
    assert any(isinstance(e, Compacted) for e in events)

    run(collect(s, "问二"))
    recs = main_records(s)
    assert len(recs) == 2, "压缩请求本身是 role=compact，不该混进主对话口径"
    after = recs[-1]
    assert after["gap"] == 5000 - 800
    assert after["gap"] > after["new"] * 1.5 + 1000, "历史被整段换掉了，gap 必然远超新增"
    assert after["break"] == "compact"


def test_compact_request_is_not_counted_as_main(ee_home, workspace):
    """压缩 / 标题这类侧路请求命中恒 0，进主对话分母会把用户看到的命中率无端拉低。"""
    s = make(workspace, [
        text_chunks("答一") + finish("stop", prompt=10_000, completion=50, hit=9_900),
        text_chunks("## 用户目标\n继续") + finish("stop", prompt=10_200, completion=50, hit=0),
    ])
    run(collect(s, "问一"))
    assert run(s.compact())["ok"]
    roles = [r.get("role") or "main" for r in s.stats.records]
    assert roles.count("compact") == 1
    totals = s.stats.totals()
    assert totals["main_requests"] == 1
    assert totals["main_hit_rate"] == pytest.approx(0.99)
    assert totals["hit_rate"] < totals["main_hit_rate"]
