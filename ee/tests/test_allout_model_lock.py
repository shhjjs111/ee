"""第二批：全力以赴的模型绑定与档位接线（2026-11 设计评审）。

守三件事：
1. 本轮锁定的模型身份覆盖角色配置（压缩 / A-B 评审 / 标题都用它）；
2. 子代理的 `model` 参数在本轮被忽略，并把这件事告诉模型（否则它会以为换成了便宜模型）；
3. 强度档位「已有会话也提到最高档、只在这一回合生效、收尾还原」。
"""
from __future__ import annotations

import asyncio

from conftest import finish, make_session, text_chunks


def _thread(home, workspace):
    from ee.server.threads import Thread

    return Thread("t-lock", home, lambda m, p: None, {"cwd": str(workspace)}, persist=False)


def _with_role_model(s, name: str = "deepseek/deepseek-chat") -> None:
    """把某个用途配到另一个模型上（provider/model 形式，resolve_model 能解析）。"""
    s.config.data.setdefault("roles", {})["compact"] = name
    s.config.data.setdefault("roles", {})["allout"] = name


def test_role_models_are_ignored_while_locked(workspace):
    """锁定期间：压缩 / 评审 / 标题都不许偷偷换模型；解锁后恢复原状。"""
    s, _ = make_session(workspace, [])
    _with_role_model(s)
    assert s.role_spec("compact").alias != s.spec.alias, "前提：角色配置确实指向了别的模型"

    s.lock_model()
    for role in ("compact", "allout", "title"):
        assert s.role_spec(role) is s.spec, f"锁定期内 {role} 不该换模型"
    s.unlock_model()
    assert s.role_spec("compact").alias != s.spec.alias, "解锁后要恢复角色配置"


def test_candidates_inherit_the_locked_effort(workspace):
    """候选会话按 `effort_for` 取档：模型相同就该继承本轮拉高的档位。"""
    from ee.core.allout import max_effort
    from ee.core.session import Session

    s, _ = make_session(workspace, [])
    s.lock_model()
    top = max_effort(s.profile)
    assert top, "测试环境的默认模型档案应支持调节强度"
    s.set_effort(top)
    child = Session(s.config, model=s.spec.alias, cwd=s.cwd, mode=s.policy.eff_mode,
                    client=s._client_override, persist=False, home=s.home)
    child.effort = s.effort_for(child.spec)
    assert child.effort == s.effort, "候选必须继承本轮档位（否则主路 max、候选默认）"


def test_subagent_model_override_ignored_under_lock(workspace):
    """子代理指定别的模型 → 忽略，用本轮锁定模型，并把这件事写进结果给模型看。"""
    from ee.core.subagent import run_subagent

    s, _fake = make_session(workspace, [text_chunks("子任务完成") + finish("stop")])
    s.lock_model()
    events: list = []
    out, _ok, rec = asyncio.run(run_subagent(
        s, "c1", {"agent": "general", "prompt": "做点事", "model": "some-cheap-model"}, events.append))

    assert rec["model"] == s.spec.alias, f"子代理必须用锁定模型，实际 {rec['model']}"
    assert "已锁定模型" in out and "some-cheap-model" in out, "要告诉模型参数被忽略了（否则它以为换了模型）"


def test_subagent_model_override_still_works_without_lock(workspace):
    """没有锁定时保持原行为：`model` 参数照旧生效（不把绑定做成全局）。"""
    from ee.core.subagent import run_subagent

    s, _fake = make_session(workspace, [text_chunks("ok") + finish("stop")])
    s.config.data.setdefault("models", {})["cheap"] = {"provider": "deepseek", "model": "deepseek-chat"}
    events: list = []
    _out, _ok, rec = asyncio.run(run_subagent(
        s, "c2", {"agent": "general", "prompt": "做点事", "model": "cheap"}, events.append))
    assert rec["model"] == "cheap"


def test_model_state_reports_lock_and_effort(workspace):
    """回合小结要能说清"实际用了哪个模型、哪一档"。"""
    s, _ = make_session(workspace, [])
    s.lock_model()
    st = s.model_state()
    assert st["locked"] is True and st["alias"] == s.spec.alias
    assert st["provider"] == s.spec.provider and st["model"] == s.spec.model
    assert "effort" in st and "effort_degraded" in st
    s.unlock_model()
    assert s.model_state()["locked"] is False


def test_model_state_reports_server_side_downgrade(workspace):
    """服务端拒了推理参数（400 后回退）时，小结要能说"已降级"，不能假装生效。"""
    s, _ = make_session(workspace, [])
    s.lock_model()

    class _Qs:
        data = {f"{s.spec.base_url}|{s.spec.model}": {"no_reasoning_effort": True}}

    class _Client:
        state = _Qs()
        key = f"{s.spec.base_url}|{s.spec.model}"

    s._client = _Client()
    assert s.model_state()["effort_degraded"] is True
    s._client = None
    assert s.model_state()["effort_degraded"] is False
