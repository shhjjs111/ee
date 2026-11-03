"""压缩的可测指标：摘要覆盖率、压缩后重读、配对步数（roadmap §7 的三个指标）。

这一层存在的理由：压缩是唯一会让模型「变傻」的机制，之前只有一次性手工实验
（压缩 2 次后答对 24/24），改坏 `retained_suffix` 或调小 `keep_tools` 测试不会响。
"""
from conftest import FakeClient, collect, finish, run, text_chunks, tool_chunks

from ee.core import compaction_audit as A
from ee.core.store import SessionStore


def read_call(cid, path, offset=None, limit=None):
    args = {"path": path}
    if offset is not None:
        args["offset"] = offset
    if limit is not None:
        args["limit"] = limit
    return tool_chunks(0, cid, "read", args)


def reads_of(msgs):
    return A._reads(msgs)


# ---------------- 标识符抽取与覆盖率 ----------------

def test_identifiers_keeps_code_specific_tokens():
    text = ("改了 ee/core/session.py 里的 _retain_budget，"
            "还在 test_prune.py 见到 ContextLengthError 和 camelCaseName。")
    got = A.identifiers(text)
    for want in ("ee/core/session.py", "_retain_budget", "test_prune.py",
                 "ContextLengthError", "camelCaseName"):
        assert want in got, f"{want} 应当被抽出来：{got}"


def test_identifiers_drops_generic_words():
    """泛词（true/file/path）出现在任何摘要里都不算「记住了细节」。"""
    got = A.identifiers("true false none file path data value error ok")
    assert not [x for x in got if x.lower() in A._STOP]


def test_identifier_coverage_counts_kept_and_missed():
    before = "改了 ee/core/session.py 和 _retain_budget 两处"
    summary = "## 改动\n只提到 ee/core/session.py"
    c = A.identifier_coverage(before, summary)
    assert c["total"] >= 2
    assert "ee/core/session.py" not in c["missed"]
    assert "_retain_budget" in c["missed"]
    assert 0 < c["rate"] < 1


def test_identifier_coverage_empty_input_is_none_not_zero():
    """没有可比的标识符时要给 None（「算不出」），不能给 0（看着像「全丢了」）。"""
    c = A.identifier_coverage("", "## 空")
    assert c["total"] == 0 and c["rate"] is None


def test_ident_digest_is_two_ints_only():
    """落盘只留两个整数：压缩前原文很大且可能含私密内容，不能进账本。"""
    d = A.ident_digest("edit ee/core/session.py", "## 目标\n没提文件")
    assert set(d) == {"total", "kept"}
    assert all(isinstance(v, int) for v in d.values())


# ---------------- 压缩后重读（核心指标） ----------------

def test_reads_track_offsets_so_segments_are_distinct():
    """同一文件的不同区间是**不同的读取**：编辑时逐段读是正常推进，不是重读。"""
    msgs = [
        {"role": "assistant", "tool_calls": [
            {"id": "1", "function": {"name": "read", "arguments": '{"path":"a.py","offset":10,"limit":20}'}}]},
        {"role": "assistant", "tool_calls": [
            {"id": "2", "function": {"name": "read", "arguments": '{"path":"a.py","offset":30,"limit":20}'}}]},
    ]
    regs = reads_of(msgs)
    assert len(regs) == 2 and regs[0] != regs[1]


def test_reread_only_counts_content_that_was_there_before_compaction():
    msgs = [
        {"role": "user", "content": "改 a.py"},
        {"role": "assistant", "tool_calls": [
            {"id": "1", "function": {"name": "read", "arguments": '{"path":"a.py"}'}}]},
        {"role": "tool", "tool_call_id": "1", "content": "..."},
        {"role": "user", "content": "摘要", "_kind": "summary"},
        # 压缩后重读同一块 —— 这是损失
        {"role": "user", "content": "继续"},
        {"role": "assistant", "tool_calls": [
            {"id": "2", "function": {"name": "read", "arguments": '{"path":"a.py"}'}}]},
        # 压缩前没读过的东西，读了不算重读
        {"role": "assistant", "tool_calls": [
            {"id": "3", "function": {"name": "read", "arguments": '{"path":"b.py"}'}}]},
    ]
    r = A.reread_after_compaction(msgs)
    assert r["rereads"] == 1, r
    assert r["detail"][0]["files"] == ["a.py"]


def test_reread_zero_when_retained_segment_covers_what_was_read():
    """保留段把刚读过的内容原样留着时，模型不该重读——这是健康值。"""
    msgs = [
        {"role": "user", "content": "改 a.py"},
        {"role": "assistant", "tool_calls": [
            {"id": "1", "function": {"name": "read", "arguments": '{"path":"a.py"}'}}]},
        {"role": "user", "content": "摘要", "_kind": "summary"},
        {"role": "user", "content": "继续"},
        {"role": "assistant", "tool_calls": [
            {"id": "2", "function": {"name": "write", "arguments": '{"path":"a.py"}'}}]},
    ]
    assert A.reread_after_compaction(msgs)["rereads"] == 0


def test_reread_respects_window():
    """只在压缩后 N 轮内算：更晚的重读已经不算「立刻重读」。"""
    def build(reads_per_turn):
        """每轮读几次 a.py；压缩前读过同一区间，所以每轮都算重读。"""
        msgs = [
            {"role": "user", "content": "起"},
            {"role": "assistant", "tool_calls": [
                {"id": "1", "function": {"name": "read", "arguments": '{"path":"a.py"}'}}]},
            {"role": "user", "content": "摘要", "_kind": "summary"},
        ]
        for i, n in enumerate(reads_per_turn):
            msgs.append({"role": "user", "content": f"第 {i} 轮"})
            for k in range(n):
                msgs.append({"role": "assistant", "tool_calls": [
                    {"id": f"r{i}_{k}", "function": {"name": "read", "arguments": '{"path":"a.py"}'}}]})
        return msgs

    # 前 3 轮各重读一次，第 4、5 轮也重读 —— 后面的不该被算进去
    r = A.reread_after_compaction(build([1, 1, 1, 1, 1]), window=3)
    assert r["rereads"] == 3, r
    # 窗口内同一轮读两次只算一次
    assert A.reread_after_compaction(build([2, 0, 0]), window=3)["rereads"] == 1
    # 窗口放大就能看到后两轮
    assert A.reread_after_compaction(build([1, 1, 1, 1, 1]), window=5)["rereads"] == 5


# ---------------- 配对步数 ----------------

def test_step_overhead_median_and_over_threshold():
    assert A.step_overhead([])["median"] is None
    r = A.step_overhead([(5, 5), (6, 7), (4, 8)])
    assert r["pairs"] == 3
    assert r["median"] == 1          # 差值 0, 1, 4 → 中位 1
    assert r["over"] == 1            # 只有 4 > 1


# ---------------- 端到端：压缩时把覆盖率落盘 ----------------

def test_compact_persists_coverage_digest(ee_home, workspace):
    """压缩会把原文整段换掉，覆盖率只能在这一刻算并落盘，否则事后永久算不出。"""
    from ee.config import load_config
    from ee.core.session import Session

    (workspace / "a.txt").write_text("hello\n", encoding="utf-8")
    cfg = load_config(workspace)
    fake = FakeClient([
        tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        text_chunks("读完了") + finish("stop"),
        text_chunks("## 用户目标\n改 ee/core/session.py 的 _retain_budget 与 test_prune.py")
        + finish("stop", prompt=500, completion=50),
        text_chunks("继续") + finish("stop"),
    ])
    s = Session(cfg, cwd=workspace, client=fake, persist=True)
    run(collect(s, "读 a.txt，然后改 ee/core/session.py"))
    run(collect(s, "再看 test_prune.py"))
    assert run(s.compact())["ok"]

    recs = SessionStore._records(s.store.path)
    cov = A.from_store(recs)
    assert cov["count"] == 1
    item = cov["items"][0]
    assert item["total"] > 0, "压缩前确实有可比的标识符"
    assert item["kept"] == 0, "摘要里一个标识符都没提起 —— 正是这个指标要暴露的问题"
    assert item["rate"] == 0.0


def test_from_store_tolerates_records_without_coverage():
    """老会话没有 coverage 字段：给 count=0 / rate=None，不能崩也不能编出 0%。"""
    cov = A.from_store([{"kind": "compact", "reason": "auto"}, {"kind": "message"}])
    assert cov["count"] == 1
    assert cov["items"][0]["rate"] is None


def test_audit_reports_none_when_nothing_measured():
    a = A.audit([{"role": "user", "content": "你好"}])
    assert a["identifier_coverage"] is None
    assert a["reread"]["rereads"] == 0

def test_retained_segment_keeps_reread_at_zero_end_to_end(ee_home, workspace):
    """端到端守「保留段有效」：压缩后重读同一文件，指标必须看得见。

    这是 §7 的核心指标，之前只有一次手工实验（压缩 2 次后答对 24/24）。现在它是
    一条会响的回归：谁把 keep_tools 调成 0、或让 retained_suffix 返回空，模型手里
    就没有刚读过的文件，这条就会从 0 变成 1。

    注意 `before_reads` 走落盘清单：压缩后历史里已经没有「压缩前读过什么」，
    从 `messages[:s]` 现算永远得 0（不是健康，是算不出来）。
    """
    from ee.config import load_config
    from ee.core.session import Session

    (workspace / "big.txt").write_text("x" * 4000, encoding="utf-8")
    cfg = load_config(workspace)
    cfg.data["models"]["deepseek-flash"]["context_window"] = 32_000
    fake = FakeClient([
        tool_chunks(0, "c1", "read", {"path": "big.txt"}) + finish("tool_calls"),
        text_chunks("读到了") + finish("stop"),
        text_chunks("## 用户目标 看 big.txt") + finish("stop", prompt=500, completion=50),
    ])
    s = Session(cfg, cwd=workspace, client=fake, persist=True)
    run(collect(s, "读 big.txt"))
    assert run(s.compact())["ok"]

    recs = SessionStore._records(s.store.path)
    comp = [r for r in recs if r.get("kind") == "compact"]
    assert comp, "压缩记录应当落盘"
    assert comp[0]["reads"], "压缩时应当把读过的区间记下来"
    assert any(m.get("role") == "tool" for m in s.history), "保留段里应当有原始工具往来"

    # 健康值：保留段盖住了刚读的内容，模型不用重读
    assert A.reread_after_compaction(s.history, before_reads=comp[0]["reads"])["rereads"] == 0

    # 故意制造「重读」：压缩后紧接着再读同一区间
    pretend = list(s.history) + [
        {"role": "user", "content": "继续"},
        {"role": "assistant", "tool_calls": [
            {"id": "c9", "function": {"name": "read", "arguments": '{"path":"big.txt"}'}}]},
    ]
    assert A.reread_after_compaction(pretend, before_reads=comp[0]["reads"])["rereads"] == 1
