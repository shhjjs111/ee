"""全力以赴的边界行为（2026-11 第一批修复）。

守四件事：
1. 主线不被"候选只读"限制（写型子代理照旧可派），候选内部只能只读探索；
2. 判据审批有超时：超时=拒绝，卡片收尾、待处理记录清掉、迟到的点击不能改写结果；
3. 自建判据（`EE_VALIDATION`）的拒绝路径都挡住，合法的归一化路径仍然通过；
4. 没有独立判据的回合落 `delivery.status="unverified"`（完成 ≠ 验收通过）。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from conftest import finish, make_session, text_chunks

from ee.core.allout import proposed_validation
from ee.core.probe import ProbeResult
from test_allout_run import _thread


# ---------------------------------------------------------------- 子代理权限

def _run_subagent(parent, args):
    from ee.core.subagent import run_subagent

    events: list = []
    return asyncio.run(run_subagent(parent, "c1", args, events.append))


def test_main_line_keeps_write_subagents(workspace):
    """主线照旧可以派写型子代理：全力以赴只约束候选，不约束主线。"""
    parent, _fake = make_session(workspace, [text_chunks("子任务完成") + finish("stop")])
    assert getattr(parent, "_allout_explore_only", False) is False
    out, ok, rec = _run_subagent(parent, {"agent": "general", "prompt": "做点事"})
    assert ok and rec["model"], out


def test_candidates_are_read_only_explore_only(workspace):
    """候选内部只允许只读探索：写型子代理直接拒绝，且说清原因。"""
    parent, _fake = make_session(workspace, [text_chunks("不该跑") + finish("stop")])
    parent._allout_explore_only = True
    with pytest.raises(ValueError, match="只读探索"):
        _run_subagent(parent, {"agent": "general", "prompt": "写点东西"})


# ---------------------------------------------------------------- 审批超时

def test_probe_approval_times_out_as_denied(ee_home, workspace, monkeypatch):
    """审批没人点：按拒绝处理，卡片收尾、记录清掉、迟到的点击失效。"""
    import ee.core.session as session_mod

    monkeypatch.setattr(session_mod, "APPROVAL_TIMEOUT", 0.05)
    th = _thread(ee_home, workspace)
    th.ensure_session()
    notes: list[tuple[str, dict]] = []
    th.notify = lambda m, p: notes.append((m, p))

    proj = ProbeResult(command="pytest -q", source="pyproject.toml", confirmed=False)
    assert asyncio.run(th._confirm_probe_goal(proj)) is False

    assert th.approvals == {}, "超时后不能留着等不到回应的 future"
    approval = next(it for it in th.items if it.get("type") == "approval")
    assert approval["status"] == "cancelled"
    with pytest.raises(ValueError, match="已经处理过"):
        th.respond(approval["call_id"], "once")
    assert any(it.get("type") == "notice" and "按拒绝处理" in str(it.get("text")) for it in th.items)
    assert any(m == "thread/updated" for m, _ in notes)


def test_review_path_does_not_run_rejected_proposal(ee_home, workspace, monkeypatch):
    """提案没批准（含超时）：判据命令不执行，回合以「未独立验收」收尾。"""
    from ee.core.events import TurnDone

    th = _thread(ee_home, workspace)
    s = th.ensure_session()
    th.notify = lambda m, p: None

    async def implementation(text, images=None, inner=False):
        yield TurnDone("completed", 'EE_VALIDATION: {"command":"pytest -q","files":["t.py"]}')

    async def reject(proposal):
        return False

    called: list[str] = []

    async def judge(spec, **_kw):
        called.append(spec.command)
        raise AssertionError("提案没批准，判据不能执行")

    monkeypatch.setattr(s, "run_turn", implementation)
    monkeypatch.setattr(th, "_confirm_probe_goal", reject)
    monkeypatch.setattr("ee.core.goal.judge", judge)
    (workspace / "t.py").write_text("def test_x(): assert True\n", encoding="utf-8")

    reason = asyncio.run(th._run_allout_review("做点事", None))
    assert reason == "completed"
    assert called == [], "拒绝后仍然执行了判据"
    assert th._delivery is not None and th._delivery["status"] == "unverified"


# ---------------------------------------------------------------- 自建判据

def _payload(**data) -> str:
    return "实现的说明\nEE_VALIDATION: " + json.dumps(data, ensure_ascii=False)


@pytest.fixture
def proposal_ws(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "test_ok.py").write_text("def test_x(): assert True\n", encoding="utf-8")
    (ws / "sub").mkdir()
    return ws


@pytest.mark.parametrize("text", [
    "实现的说明\nEE_VALIDATION: {不是 json",
    _payload(command="  ", files=["test_ok.py"]),
    _payload(command="pytest -q\nrm -rf .", files=["test_ok.py"]),
    _payload(command="pytest -q", files=[]),
])
def test_invalid_proposals_are_rejected(proposal_ws, text):
    assert proposed_validation(text, proposal_ws) is None


def test_absolute_path_rejected(proposal_ws):
    absolute = str((proposal_ws / "test_ok.py").resolve())
    assert proposed_validation(_payload(command="pytest -q", files=[absolute]), proposal_ws) is None


def test_escaping_workspace_rejected(proposal_ws):
    outside = proposal_ws.parent / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    assert proposed_validation(_payload(command="pytest -q", files=["../outside.py"]), proposal_ws) is None


def test_git_internal_file_rejected(proposal_ws):
    (proposal_ws / ".git").mkdir()
    (proposal_ws / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    assert proposed_validation(_payload(command="pytest -q", files=[".git/config"]), proposal_ws) is None


def test_missing_file_and_directory_rejected(proposal_ws):
    assert proposed_validation(_payload(command="pytest -q", files=["nope.py"]), proposal_ws) is None
    assert proposed_validation(_payload(command="pytest -q", files=["sub"]), proposal_ws) is None


def test_oversized_and_too_many_files_rejected(proposal_ws):
    big = proposal_ws / "big.py"
    big.write_text("x = 1\n" + "#" * 1_100_000, encoding="utf-8")
    assert proposed_validation(_payload(command="pytest -q", files=["big.py"]), proposal_ws) is None
    many = []
    for i in range(13):
        (proposal_ws / f"t{i}.py").write_text("x = 1\n", encoding="utf-8")
        many.append(f"t{i}.py")
    assert proposed_validation(_payload(command="pytest -q", files=many), proposal_ws) is None


def test_normalized_path_inside_workspace_is_accepted(proposal_ws):
    got = proposed_validation(_payload(command="pytest -q", files=["./sub/../test_ok.py"]), proposal_ws)
    assert got == ("pytest -q", ("test_ok.py",))
