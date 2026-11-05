"""跨进程共用会话时，别把对方正在跑的回合报成「已中断」（2026-11 审计修复）。

_close_stale_runs 的判据是 `pid != 本进程`，前提是「写这条 running 的不是当前进程，
就一定不是正在跑」。桌面端后端与 `ee exec --resume` 共用同一份 sessions/<id>.jsonl 时
这个前提不成立：任何一方 load 时，另一方正在跑的 run 都会被就地改成 done/interrupted。
接着任何一次 save_meta 把这份被污染的 meta 整份写回磁盘——假的 interrupted 被持久化，
用户在这期间改的会话名/模型/模式也被旧快照覆盖。

编排按 terminal_reason 判断子任务是否结束（orchestrator.py 的 _last_run），
把还在跑的子任务当已结束并据此收尾或重派，是这条最坏的后果。
仓库里早有 lifeline.pid_alive 可判 pid 是否真的活着。
"""
import os

from ee.core.store import _close_stale_runs
from ee.server.lifeline import pid_alive


def test_own_pid_is_left_alone():
    meta = {"runs": [{"id": "r1", "status": "running", "pid": os.getpid()}]}
    _close_stale_runs(meta)
    assert meta["runs"][0]["status"] == "running"


def test_live_other_process_run_is_not_marked_interrupted():
    """父进程一定活着，用它代表「另一个正在跑的 ee 进程」。"""
    parent = os.getppid()
    assert pid_alive(parent), "用例前提：父进程应当活着"
    meta = {"runs": [{"id": "r1", "status": "running", "pid": parent}]}

    _close_stale_runs(meta)

    assert meta["runs"][0]["status"] == "running", "把另一个活着的进程正在跑的回合判成了中断"
    assert meta["runs"][0].get("terminal_reason") is None


def test_dead_process_run_is_marked_interrupted():
    dead = 999_999
    assert not pid_alive(dead), "用例前提：这个 pid 不该存在"
    meta = {"runs": [{"id": "r1", "status": "running", "pid": dead}]}

    _close_stale_runs(meta)

    assert meta["runs"][0]["status"] == "done"
    assert meta["runs"][0]["terminal_reason"] == "interrupted"


def test_run_without_pid_is_treated_as_dead():
    """老记录没有 pid 字段：只能来自已经不在的进程。"""
    meta = {"runs": [{"id": "r1", "status": "running"}]}
    _close_stale_runs(meta)
    assert meta["runs"][0]["terminal_reason"] == "interrupted"


def test_finished_runs_are_untouched():
    meta = {"runs": [{"id": "r1", "status": "done", "terminal_reason": "completed", "pid": 999_999}]}
    _close_stale_runs(meta)
    assert meta["runs"][0]["terminal_reason"] == "completed"


def test_pending_approval_items_are_closed_on_load(tmp_path):
    """进程被杀时残留的 pending 审批/提问条目，读取时标成 cancelled。

    这些条目在创建时就落盘（threads.py 里 persist=True），而重启后 Thread.approvals
    是新进程里的空表 —— respond()/answer() 一定抛「这个审批已经处理过或已失效」，
    界面上却还是可点的卡片，看起来有一个永远批准不了的阻塞操作。
    """
    from ee.core.store import SessionStore

    tid = "20260101-000000-abcdef"
    s = SessionStore(tmp_path, session_id=tid)
    s.item({"type": "approval", "status": "pending", "call_id": "c1", "id": "ap1", "seq": 1, "tool": "shell"})
    s.item({"type": "question", "status": "pending", "call_id": "c2", "id": "q1", "seq": 2, "question": "选哪个"})
    s.item({"type": "approval", "status": "once", "call_id": "c3", "id": "ap2", "seq": 3, "tool": "write"})

    d = SessionStore.load(tmp_path / "sessions" / f"{tid}.jsonl")
    got = {it["id"]: it["status"] for it in d["items"]}

    assert got["ap1"] == "cancelled", "残留的 pending 审批没有归一"
    assert got["q1"] == "cancelled", "残留的 pending 提问没有归一"
    assert got["ap2"] == "once", "已经处理完的审批被改坏了"
