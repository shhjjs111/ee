"""旧会话 JSON 的窄范围兼容回归（合成 fixture，不读取真实会话）。

覆盖：
- 旧 turn_end 没有 delivery 时，SessionStore.load / Thread 打开 / 继续建会话不崩溃；
- 旧 error / interrupted 不被交付指标或线程状态说成成功；
- 旧条目缺少新字段时仍保持原 reason，不伪造 delivery。
"""
from __future__ import annotations

import json
from pathlib import Path

from ee.core import delivery_metrics as M
from ee.core.store import SessionStore
from ee.server.threads import Thread


def _write_old_session(path: Path, *, workspace: Path) -> None:
    records = [
        {"kind": "meta", "meta": {"cwd": str(workspace), "title": "合成旧会话",
                                  "model": "old-model", "mode": "default",
                                  "created": 1.0, "updated": 2.0}},
        {"kind": "item", "item": {"id": "u1", "type": "user", "text": "合成旧任务",
                                  "seq": 1}},
        {"kind": "item", "item": {"id": "a1", "type": "assistant", "text": "旧回答",
                                  "reasoning": "", "seq": 2}},
        # 旧回合：没有新字段 delivery；第二个连 usage 都没有。
        {"kind": "item", "item": {"id": "e-error", "type": "turn_end",
                                  "reason": "error", "duration_ms": 10,
                                  "usage": {"requests": 1, "cost": 0.01}, "seq": 3}},
        {"kind": "item", "item": {"id": "e-stop", "type": "turn_end",
                                  "reason": "interrupted", "duration_ms": 5, "seq": 4}},
    ]
    path.write_text(
        "".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in records),
        encoding="utf-8",
    )


def test_old_turn_end_without_delivery_opens_and_does_not_become_success(ee_home, workspace):
    sid = "20260101-000000-old"
    path = ee_home / "sessions" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True)
    _write_old_session(path, workspace=workspace)

    loaded = SessionStore.load(path)
    assert [it["type"] for it in loaded["items"]] == ["user", "assistant", "turn_end", "turn_end"]
    ends = [it for it in loaded["items"] if it["type"] == "turn_end"]
    assert [it["reason"] for it in ends] == ["error", "interrupted"]
    assert all("delivery" not in it for it in ends), "合成 fixture 必须是旧格式"

    # 打开/恢复路径：Thread 构造 + 懒建会话 + detail 都不能因缺 delivery 崩溃。
    th = Thread(sid, ee_home, lambda _m, _p: None, loaded["meta"],
                messages=loaded["messages"], items=loaded["items"], persist=False)
    session = th.ensure_session()
    assert session is not None
    detail = th.detail()
    assert detail["status"] == "idle"
    assert [it["reason"] for it in detail["items"] if it["type"] == "turn_end"] == [
        "error", "interrupted"]

    # 旧回合没有 delivery，不能进入交付统计，更不能被算成成功。
    metrics = M.aggregate(loaded["items"])
    assert metrics["samples"] == 0
    assert metrics["reported_met"] == 0
    assert metrics["solved"] == 0
    assert metrics["verify_rate"] is None


def test_old_turn_end_reason_is_preserved_without_rewriting(ee_home, workspace):
    """读取只应归一化运行状态，不能把旧 error/interrupted 改写成 completed 或 met。"""
    sid = "20260101-000000-old2"
    path = ee_home / "sessions" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True)
    _write_old_session(path, workspace=workspace)
    before = path.read_bytes()

    loaded = SessionStore.load(path)
    ends = [it for it in loaded["items"] if it["type"] == "turn_end"]
    assert [it["reason"] for it in ends] == ["error", "interrupted"]
    assert path.read_bytes() == before, "load 不得迁移/重写旧会话文件"
