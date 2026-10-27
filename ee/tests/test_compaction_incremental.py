"""A9：仅增量摘要。已有上次摘要时，压缩只把摘要之后的新内容与旧摘要合并成新摘要，不重读全部历史。

验证发送给模型的内容：
- 主路（同模型）：请求 = 冻结前缀 + 当前历史（上次请求是它的字节前缀，缓存稳定）+ 增量指令；
  当前历史里只有「保留的用户原话 + 旧摘要 + 之后的新内容」，第一次压缩前的内容不再出现；
- 侧路（主路失败）：只渲染旧摘要之后的新内容，旧摘要放在 <previous-summary> 里；
- 换了压缩模型（缓存本来就不共享）：只发 AGENTS.md 等固定消息 + 旧摘要 + 之后的新内容；
- 摘要之后没有新内容：不压缩。
"""
import json

from conftest import FakeError, collect, finish, run, text_chunks, tool_chunks

from ee.core import compaction as C
from ee.core.events import Compacted
from test_compaction import make, summary_chunks

OLD = "OLD_FILE_CONTENT_42"
NEW = "NEW_FILE_CONTENT_77"


def _two_rounds(workspace, third):
    (workspace / "old.txt").write_text(OLD + "\n", encoding="utf-8")
    (workspace / "new.txt").write_text(NEW + "\n", encoding="utf-8")
    return make(workspace, [
        tool_chunks(0, "c1", "read", {"path": "old.txt"}) + finish("tool_calls"),
        text_chunks("读完旧文件") + finish("stop"),
        summary_chunks("## 用户目标\n第一版摘要 S1\n## 下一步\n读新文件"),
        tool_chunks(0, "c2", "read", {"path": "new.txt"}) + finish("tool_calls"),
        text_chunks("读完新文件") + finish("stop"),
        *third,
    ])


def test_last_summary_extracts_text():
    hist = [{"role": "user", "content": "a"},
            {"role": "user", "_kind": "summary",
             "content": C.summary_prefix() + "\n\n<summary>\n旧摘要内容\n</summary>\n\n<context-update>…"}]
    assert C.last_summary(hist) == (1, "旧摘要内容")
    assert C.last_summary([{"role": "user", "content": "a"}]) is None


def test_second_compaction_is_incremental_main_path(ee_home, workspace):
    s, fake = _two_rounds(workspace, [summary_chunks("## 用户目标\n第二版摘要 S2")])
    run(collect(s, "先读旧文件"))
    first = run(s.compact())
    assert first["ok"] and not first.get("incremental")
    assert "上下文检查点压缩" in fake.calls[2]["messages"][-1]["content"]
    run(collect(s, "再读新文件"))
    evs = []
    res = run(s.compact(evs.append))
    assert res["ok"] and res["incremental"] is True
    req = fake.calls[5]["messages"]
    prev = fake.calls[4]["messages"]
    # 前缀缓存：上一次请求是这次压缩请求的字节前缀；tools 不变
    assert req[:len(prev)] == prev and fake.calls[5]["tools"] == fake.calls[4]["tools"]
    wire = json.dumps(req, ensure_ascii=False)
    assert "第一版摘要 S1" in wire and NEW in wire          # 旧摘要 + 新内容
    assert OLD not in wire                                   # 第一次压缩前的内容不重读
    instr = req[-1]["content"]
    assert "增量" in instr and "上下文检查点压缩" not in instr
    # 新历史只带新摘要
    assert "第二版摘要 S2" in s.history[-1]["content"] and "第一版摘要 S1" not in s.history[-1]["content"]
    comp = [e for e in evs if isinstance(e, Compacted)][0]
    assert comp.incremental is True


def test_incremental_side_path_only_renders_new_content(ee_home, workspace):
    s, fake = _two_rounds(workspace, [FakeError(400, "context length exceeded"), summary_chunks("合并后 S2")])
    run(collect(s, "先读旧文件"))
    run(s.compact())
    run(collect(s, "再读新文件"))
    res = run(s.compact())
    assert res["ok"] and res["incremental"]
    side = fake.calls[6]["messages"]
    assert [m["role"] for m in side] == ["system", "user"]
    assert "增量" in side[0]["content"]
    body = side[1]["content"]
    assert "<previous-summary>\n## 用户目标\n第一版摘要 S1" in body
    assert NEW in body and "【用户】再读新文件" in body
    assert OLD not in body and "【用户】先读旧文件" not in body    # 摘要之前（含保留的原话）不渲染
    assert "合并后 S2" in s.history[-1]["content"]


def test_incremental_with_other_compact_model_sends_only_new(ee_home, workspace):
    s, fake = _two_rounds(workspace, [summary_chunks("S2")])
    (workspace / "AGENTS.md").write_text("项目规则 RULE_X\n", encoding="utf-8")
    run(collect(s, "先读旧文件"))
    run(s.compact())
    run(collect(s, "再读新文件"))
    s.config.data["roles"]["compact"] = "deepseek-pro"
    assert run(s.compact())["ok"]
    call = fake.calls[5]
    assert call["model"] == "deepseek-v4-pro"
    msgs = call["messages"]
    assert msgs[0] == fake.calls[4]["messages"][0]                # 同一个 system
    users = [m for m in msgs if m["role"] == "user"]
    assert "RULE_X" in users[0]["content"]                         # AGENTS.md 等固定消息照发
    assert users[1]["content"].startswith(C.summary_prefix())      # 紧接着就是旧摘要
    assert not any(m["content"] == "先读旧文件" for m in users)     # 保留的原话已在摘要之前，不再发
    assert NEW in json.dumps(msgs, ensure_ascii=False)


def test_no_new_content_after_summary_does_not_compact(ee_home, workspace):
    s, fake = make(workspace, [text_chunks("答一") + finish("stop"), summary_chunks()])
    run(collect(s, "一"))
    assert run(s.compact())["ok"]
    n = len(fake.calls)
    res = run(s.compact())
    assert not res["ok"] and "没有新的内容" in res["message"] and len(fake.calls) == n


def test_compaction_savings_tracked(ee_home, workspace):
    s, fake = make(workspace, [text_chunks("答一" * 200) + finish("stop", prompt=50000), summary_chunks()])
    run(collect(s, "一"))
    res = run(s.compact())
    assert s.stats.compacted_tokens == res["tokens_before"] - res["tokens_after"] > 0
    assert s.stats.totals()["compacted"] == s.stats.compacted_tokens
