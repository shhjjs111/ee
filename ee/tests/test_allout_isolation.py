"""全力以赴候选的隔离（allout 阶段 5 的补丁）。

真模型分叉演练（2026-11-09）暴露：候选会话继承了主会话的 auto 模式，而
`_check_write` / `_check_shell` 在 auto 下对工作区外的路径**直接放行**——
模型于是用绝对路径改了主仓库的 calc.py，worktree 形同虚设，写回时补丁
对不上（`patch does not apply`）。

隔离必须靠 deny 规则（deny 判定在 auto 放行之前）：候选的 policy 里追加
主仓库路径的精确规则，拦住"写主仓库"与"cd 进主仓库执行命令"，同时不能
误伤候选自己的 worktree（它的路径里含仓库名）。
"""
from __future__ import annotations

from ee.core.permissions import PermissionPolicy


def _cand_policy(tmp_path):
    main = tmp_path / "repo"
    wt = tmp_path / "wt" / "agent-allout-1"
    main.mkdir(parents=True)
    wt.mkdir(parents=True)
    from ee.core import allout as A

    deny = A.candidate_deny_rules(str(main))
    p = PermissionPolicy(mode="auto", workspace=wt, deny=deny, shell="bash")
    return p, main, wt


def test_derived_rules_shape(tmp_path):
    from ee.core import allout as A

    rules = A.candidate_deny_rules(r"C:\Users\x\repo")
    assert any(r.startswith("shell(") for r in rules)
    assert any(r.startswith("write(") for r in rules)
    assert any("c:/users/x/repo" in r for r in rules), "路径要归一成正斜杠小写"


def test_drive_root_yields_no_rules():
    """盘符根（E:\\）不生成规则：写不出既拦根目录又不误伤一切的精确规则。

    这是有意的取舍——用户真在盘符根开仓库是极端情况；而 `write(e:/*)` 这类
    规则会连候选自己的 worktree（如果也在 E:）一起拦死，宁可少拦。
    """
    from ee.core import allout as A

    assert A.candidate_deny_rules("E:\\") == []
    assert A.candidate_deny_rules("E:/") == []


def test_empty_path_yields_no_rules():
    from ee.core import allout as A

    assert A.candidate_deny_rules("") == []


def test_writing_main_repo_is_denied_even_in_auto(tmp_path):
    p, main, _wt = _cand_policy(tmp_path)
    d = p.check("write", {"path": str(main / "calc.py")})
    assert d.action == "deny", f"auto 模式下候选也不能写主仓库：{d}"
    d = p.check("edit", {"path": str(main / "calc.py")})
    assert d.action == "deny"


def test_cd_into_main_repo_shell_is_denied_even_in_auto(tmp_path):
    p, main, _wt = _cand_policy(tmp_path)
    for cmd in (f'cd "{main}" && python -m pytest -q',
                f"cd {main} && ls",
                f"cd '{main}' && git status"):
        d = p.check("shell", {"command": cmd})
        assert d.action == "deny", f"auto 模式下候选也不能进主仓库执行：{d} | {cmd}"


def test_candidate_worktree_is_not_blocked(tmp_path):
    """worktree 路径里含仓库名，规则必须精确到主仓库全路径，不能误伤候选。"""
    p, _main, wt = _cand_policy(tmp_path)
    assert p.check("write", {"path": str(wt / "calc.py")}).action == "allow"
    assert p.check("shell", {"command": f'cd "{wt}" && python -m pytest -q'}).action == "allow"


def test_normal_commands_still_allowed(tmp_path):
    p, _main, _wt = _cand_policy(tmp_path)
    assert p.check("shell", {"command": "python -m pytest -q"}).action == "allow"


def test_rule_matches_are_precise(tmp_path):
    """主仓库是 /a/repo，兄弟目录 /a/repo2 不能被误伤。"""
    p, main, _wt = _cand_policy(tmp_path)
    sibling = tmp_path / "repo2"
    sibling.mkdir()
    assert p.check("write", {"path": str(sibling / "x.py")}).action == "allow"


# ---------------------------------------------------------------- 接线


def test_fanout_wires_deny_rules_and_worktree_prompt(ee_home, workspace, monkeypatch):
    """`_allout_fanout` 必须真的把隔离规则装进候选 policy，并让提示词指向候选目录。

    真跑抓到过「候选改主仓库」：payload 用主仓库 spec 构造、policy 又没有 deny，
    两层防线同时缺失。这条用例锁住接线，避免以后重构时静默退回。
    """
    import asyncio
    from types import SimpleNamespace

    import ee.core.session as S
    import ee.core.worktree as W
    from ee.core.goal import GoalSpec
    from ee.server.threads import Thread

    main = workspace
    wt_root = main.parent / "wt"
    wt_root.mkdir(exist_ok=True)
    captured: dict = {}

    class FakeSession:
        def __init__(self, cfg, **kw):
            self.config = cfg
            self.cwd = kw.get("cwd")
            self.spec = captured["parent"].spec
            self.stats = SimpleNamespace(records=[], totals=lambda: {})
            self.policy = None
            self.budget = None
            self.budget_owner = True
            self.budget_root = None
            self.disk_enabled = True
            self.effort = ""

        async def run_turn(self, prompt, images=None):
            captured["prompt"] = prompt
            captured["policy"] = self.policy
            if False:                     # 让它是 async generator
                yield None

    def fake_create(cwd, label="", home=None):
        p = wt_root / "agent-allout-1"
        p.mkdir(exist_ok=True)
        return {"path": str(p), "cwd": str(p), "agent_base": "base0", "branch": "ee/x",
                "repo": str(main), "name": "agent-allout-1"}

    async def fake_judge(spec, **_kw):
        return SimpleNamespace(met=False, tests=1, output="", reason="red", timed_out=False, min_tests=1)

    monkeypatch.setattr(S, "Session", FakeSession)
    monkeypatch.setattr(W, "create_for_agent", lambda *a, **k: fake_create(*a, **k))
    monkeypatch.setattr(W, "agent_eligible", lambda *a, **k: (True, ""))
    monkeypatch.setattr(W, "finish_agent", lambda *a, **k: {"file_count": 0, "additions": 0,
                                                           "deletions": 0, "files": [], "commits": [],
                                                           "removed": False, "kept": True,
                                                           "kept_reason": "有改动"})
    import ee.core.goal as G
    monkeypatch.setattr(G, "judge", fake_judge)

    th = Thread("t1", ee_home, lambda m, p: None, {"cwd": str(main)}, persist=False)
    th.notify = lambda m, p: None
    s = th.ensure_session()
    captured["parent"] = s
    th._allout_judge_lock = asyncio.Lock()
    spec = GoalSpec(command="python -m pytest -q", cwd=str(main), min_tests=1, timeout=30)
    plan = SimpleNamespace(candidates=1, temperature_ladder=(0.0,))

    asyncio.run(th._allout_fanout(spec, plan, "修好它", 1))

    policy = captured["policy"]
    assert policy is not None, "候选没跑起来"
    from ee.core import allout as A
    want = A.candidate_deny_rules(main)
    assert all(r in policy.deny for r in want), "主仓库 deny 规则没有装进候选 policy"
    prompt = captured["prompt"]
    assert str(wt_root / "agent-allout-1") in prompt, "提示词必须指向候选自己的 worktree"
    # 负向：提示词里的「判据执行目录」不能还是主仓库（这是第一次真跑的直接诱因）
    assert f"判据执行目录：{main}" not in prompt, "提示词仍指向主仓库"
