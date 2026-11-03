"""`ee usage` 子命令：把落盘的逐请求账本变成可验收的结论。

**为什么必须有生产入口**：逐请求落盘只是数据，没有读它的地方就等于没落盘。
验收断言（主对话命中率 ≥ 99%、断点数 ≤ 压缩次数、正常回合未命中中位数 ≤ 1000）
只能从这里跑，否则每次验收都要手写临时脚本——那种东西不会留在仓库里，也就没人在跑。

守的边界：老会话（无账本）要明确说「没有账本」而不是报 0%；单个坏文件不能
让整条命令失败；--json 输出的字段要够脚本直接用。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ee.cli import build_parser, main


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("EE_HOME", str(tmp_path))
    (tmp_path / "sessions").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _seed(home: Path, tid: str, rows: list[dict]) -> str:
    from ee.core.store import SessionStore

    st = SessionStore(home, tid)
    for r in rows:
        st.usage(r)
    return tid


def _row(prompt, hit, **kw):
    return {"prompt": prompt, "completion": 10, "hit": hit, "role": "main", "model": "m", **kw}


def test_usage_subcommand_is_registered():
    assert "usage" in build_parser()._subparsers._group_actions[0].choices


def test_usage_reports_main_hit_rate_and_breaks(home, capsys):
    tid = _seed(home, "20261103-010101-aaaaaa", [
        _row(80_000, 79_200, turn=1),
        _row(90_000, 89_300, turn=2, gap=700, new=800),
        _row(60_000, 40_000, turn=3, gap=20_000, new=400, **{"break": "compact"}),
        {"prompt": 70_000, "completion": 50, "hit": 0, "role": "compact", "model": "m"},
    ])
    assert main(["usage", tid]) == 0
    out = capsys.readouterr().out
    assert "主对话缓存命中" in out
    assert "断点 1 次 / 压缩 1 次" in out
    assert tid in out


def test_usage_json_has_acceptance_fields(home, capsys):
    """--json 的字段要正好够写验收断言，脚本不用再自己算。"""
    tid = _seed(home, "20261103-010102-bbbbbb", [
        _row(80_000, 79_200, turn=1, gap=800, new=900),
        _row(90_000, 89_300, turn=2, gap=700, new=800),
    ])
    assert main(["usage", tid, "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["main"]["hit_rate"] == pytest.approx((79_200 + 89_300) / 170_000)
    assert d["main"]["hit_coverage"] == 1.0
    assert d["main"]["requests"] == 2
    assert d["breaks"]["count"] == 0
    # 最近秩：两个样本时取较小的那个，不插值出 750 这种账本里不存在的数
    assert d["normal_gap"]["median"] == 700
    assert d["normal_gap"]["max"] == 800


def test_usage_all_aggregates_sessions(home, capsys):
    _seed(home, "20261103-010103-cccccc", [_row(10_000, 9_900)])
    _seed(home, "20261103-010104-dddddd", [_row(10_000, 9_900)])
    assert main(["usage", "--all", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["main"]["requests"] == 2
    assert d["sessions"] == 2


def test_old_session_without_ledger_says_so(home, capsys):
    """老会话没有 usage 行：明确说「没有账本」，不能悄悄报 0% 让人以为命中率崩了。"""
    from ee.core.store import SessionStore

    SessionStore(home, "20261103-010105-eeeeee").message({"role": "user", "content": "老消息"})
    assert main(["usage", "20261103-010105-eeeeee"]) == 1
    err = capsys.readouterr().err
    assert "没有逐请求账本" in err


def test_missing_session_id_is_an_error(home, capsys):
    assert main(["usage", "不存在"]) == 1
    assert "会话不存在" in capsys.readouterr().err


def test_corrupt_file_does_not_break_aggregate(home, capsys):
    """一个坏文件不该让整条命令失败：其余会话的账本照样要能看。"""
    _seed(home, "20261103-010106-ffffff", [_row(10_000, 9_900)])
    (home / "sessions" / "20261103-010107-gggggg.jsonl").write_bytes(b"\xff\xfe not json\n")
    assert main(["usage", "--all", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["main"]["requests"] == 1
