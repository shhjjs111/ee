"""候选验收基线：Windows 检出行尾差异不得被误判成"改测试"。

真跑暴露的问题（不是 toy 特有）：主工作区是 CRLF、git blob 与候选 checkout 是 LF 时，
`git status` 干净，但**字节哈希不同**。旧的候选校验拿主工作区的 `_accept0` 去比，
于是每个候选都被判 `regressed=True`（"改了既有测试"），多路根本跑不起来。

修复口径：候选在"共享 baseline 建好、模型还没跑"时采集**自己的**验收字节基线，
之后只对这份基线做逐字节比对；主工作区仍用 `_accept0` + 最终交付闸门守着。
这里同时守住反面：真改/真删测试仍然拒绝，基线不完整（缺失/读不到）时不假装完整。
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{out.stderr[:300]}")
    return out.stdout.strip()


def _make_crlf_repo(root: Path) -> Path:
    """主工作区 CRLF、blob LF、`git status` 干净——就是真跑里遇到的那个状态。

    `core.autocrlf=input`：add 时把 CRLF 归一成 LF 存进 blob，checkout 时**不**再转回来。
    于是工作区文件是 CRLF、仓库里是 LF，两边都"干净"，但字节不同。
    """
    repo = root / "proj"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_bytes(b"def add(a, b):\r\n    return a - b\r\n")
    (repo / "tests" / "test_x.py").write_bytes(b"def test_x():\r\n    assert True\r\n")
    (repo / ".gitignore").write_bytes(b"__pycache__/\r\n*.pyc\r\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "input")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _fanout(ee_home, repo: Path, monkeypatch, *, protected=("tests/test_x.py",),
            validation=(), action=None):
    """跑一次真实的 `_allout_fanout`（真 worktree、真 baseline；模型与判据是假的）。"""
    import ee.core.goal as G
    import ee.core.session as S
    import ee.core.worktree as W
    from ee.core import acceptance
    from ee.core.goal import GoalSpec
    from ee.server.threads import Thread

    class FakeChild:
        def __init__(self, cfg, **kw):
            self.config = cfg
            self.cwd = Path(kw.get("cwd") or ".")
            self.spec = captured["parent"].spec
            self.stats = SimpleNamespace(records=[], totals=lambda: {})
            self.policy = captured["parent"].policy
            self.budget = captured["parent"].budget
            self.budget_owner = False
            self.budget_root = None
            self.disk_enabled = False
            self.effort = ""
            self.shell_name = "bash"
            self.user_home = Path.home()

        async def run_turn(self, prompt, images=None, **_kw):
            if action is not None:
                action(self.cwd)
            if False:                     # async generator
                yield None

    judge_calls: list[str] = []

    async def fake_judge(spec, **_kw):
        judge_calls.append(str(spec.cwd))
        return SimpleNamespace(met=True, tests=1, output="", reason="ok", timed_out=False,
                               min_steps=1, min_tests=1, unstable=False, report=None)

    captured: dict = {}
    monkeypatch.setattr(S, "Session", FakeChild)
    monkeypatch.setattr(G, "judge", fake_judge)

    th = Thread("t-base", ee_home, lambda m, p: None, {"cwd": str(repo)}, persist=False)
    th.notify = lambda m, p: None
    s = th.ensure_session()
    captured["parent"] = s
    th._allout_judge_lock = asyncio.Lock()
    spec = GoalSpec(command="python -m pytest -q", cwd=str(repo), min_tests=0, timeout=30,
                    validation_files=tuple(validation), protected_tests=tuple(protected),
                    protected_tests_complete=True)
    th._accept0 = acceptance.fingerprint(repo, spec.command, spec.validation_files,
                                         spec.protected_tests,
                                         protected_tests_complete=spec.protected_tests_complete)
    plan = SimpleNamespace(candidates=1, temperature_ladder=(0.0,), max_steps=5,
                           repair_rounds=0, feedback_chars=4000, explore_slots=0, judge_slots=1,
                           selection_grace_seconds=30.0, verified=True)
    recs = asyncio.run(th._allout_fanout(spec, plan, "修好 add", 1))
    return th, spec, recs, judge_calls, W


# ---------------------------------------------------------------- 夹具本身

def test_crlf_worktree_against_lf_blob_is_git_clean_but_bytes_differ(tmp_path):
    """先钉住环境条件：这种仓库是真的"干净"，而且检出会改变字节。"""
    from ee.core import acceptance
    from ee.core import worktree as W

    repo = _make_crlf_repo(tmp_path)
    assert _git(repo, "status", "--porcelain") == "", "夹具要求 git 状态干净"
    assert (repo / "tests" / "test_x.py").read_bytes().startswith(b"def test_x():\r\n"), "主工作区应是 CRLF"

    home = tmp_path / "home"
    home.mkdir()
    info = W.create_for_agent(repo, "crlf-check", home)
    cand = Path(info["cwd"])
    cand_bytes = (cand / "tests" / "test_x.py").read_bytes()
    assert b"\r\n" not in cand_bytes, "候选检出应是 LF（autocrlf=input 不转回来）"
    assert _git(cand, "status", "--porcelain") == "", "候选自己也是干净的"

    files = ("tests/test_x.py",)
    main_fp = acceptance.fingerprint(repo, "python -m pytest -q", (), files)
    cand_fp = acceptance.fingerprint(cand, "python -m pytest -q", (), files)
    assert cand_fp["tests"] != main_fp["tests"], "字节确实不同（这就是旧实现误判的来源）"
    # 旧口径：拿主工作区的基线比候选 → 误报"改了测试"
    assert not acceptance.protected_tests_intact(main_fp, cand_fp)
    # 新口径：拿候选自己的基线比 → 干净
    assert acceptance.protected_tests_intact(cand_fp, cand_fp)


# ---------------------------------------------------------------- 修复后的行为

def test_untouched_candidate_is_not_flagged_as_test_tampering(tmp_path, ee_home, monkeypatch):
    """没动过测试行的候选：行尾差异不得让它被判 regressed。"""
    from ee.core import acceptance

    repo = _make_crlf_repo(tmp_path)
    th, _spec, recs, judge_calls, _W = _fanout(ee_home, repo, monkeypatch)

    assert len(recs) == 1
    rec = recs[0]
    assert rec.get("baseline_error") in (None, ""), f"候选自己的基线不该有问题：{rec.get('baseline_error')}"
    assert rec.get("regressed") is False, "未修改的候选被判成了改测试（行尾误报回归）"
    assert rec.get("verdict") is not None and judge_calls, "候选应进入判据"
    # 反向证据：同一份候选如果拿主工作区基线去比，就会被误判（说明这条用例不是空转）
    from pathlib import Path as _P

    cand = _P(str(rec["cwd"]))
    fp = acceptance.fingerprint(cand, _spec.command, _spec.validation_files, _spec.protected_tests)
    assert not acceptance.protected_tests_intact(th._accept0, fp)


def test_candidate_that_rewrites_a_test_is_still_rejected(tmp_path, ee_home, monkeypatch):
    def tamper(cwd: Path) -> None:
        (cwd / "tests" / "test_x.py").write_text("def test_x():\n    assert 1 == 1\n", encoding="utf-8")

    repo = _make_crlf_repo(tmp_path)
    _th, _spec, recs, judge_calls, _W = _fanout(ee_home, repo, monkeypatch, action=tamper)
    rec = recs[0]
    assert rec.get("regressed") is True, "真改了既有测试必须被拒绝"
    assert not judge_calls, "被判定篡改的候选不应继续跑判据"


def test_candidate_that_deletes_a_protected_test_is_still_rejected(tmp_path, ee_home, monkeypatch):
    def drop(cwd: Path) -> None:
        (cwd / "tests" / "test_x.py").unlink()

    repo = _make_crlf_repo(tmp_path)
    _th, _spec, recs, judge_calls, _W = _fanout(ee_home, repo, monkeypatch, action=drop)
    rec = recs[0]
    assert rec.get("regressed") is True, "删掉既有测试必须被拒绝"
    assert not judge_calls


def test_incomplete_candidate_baseline_fails_closed(tmp_path, ee_home, monkeypatch):
    """基线里根本没有这个测试（缺失/不可读）时，不能重设一个空基线假装完整。"""
    repo = _make_crlf_repo(tmp_path)
    _th, _spec, recs, judge_calls, _W = _fanout(
        ee_home, repo, monkeypatch, protected=("tests/test_x.py", "tests/does_not_exist.py"))
    rec = recs[0]
    assert rec.get("baseline_error"), "缺失的受保护测试应让基线判定为不可用"
    assert "does_not_exist.py" in rec["baseline_error"]
    assert rec.get("regressed") is True
    assert not judge_calls


def test_subdir_workspace_validation_files_keep_their_mapping(tmp_path, ee_home, monkeypatch):
    """工作区是仓库子目录时，验证文件仍按候选自己的 cwd 映射，既不会误报也不会漏报。"""
    repo = _make_crlf_repo(tmp_path)
    sub = repo / "sub"
    (sub / "tests").mkdir(parents=True)
    (sub / "tests" / "test_sub.py").write_bytes(b"def test_sub():\r\n    assert True\r\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add sub")

    th, spec, recs, judge_calls, _W = _fanout(
        ee_home, sub, monkeypatch, protected=("tests/test_sub.py",), validation=("tests/test_sub.py",))
    rec = recs[0]
    assert rec.get("baseline_error") in (None, ""), rec.get("baseline_error")
    assert rec.get("regressed") is False, "子目录工作区的未修改候选不应被判改测试"
    assert rec.get("verdict") is not None and judge_calls

    # 同样的子目录工作区，真改验证文件 → 仍要拒绝
    def tamper(cwd: Path) -> None:
        (cwd / "tests" / "test_sub.py").write_text("def test_sub():\n    assert 2 == 2\n", encoding="utf-8")

    _th2, _spec2, recs2, judge_calls2, _W2 = _fanout(
        ee_home, sub, monkeypatch, protected=("tests/test_sub.py",),
        validation=("tests/test_sub.py",), action=tamper)
    assert recs2[0].get("regressed") is True
    assert not judge_calls2
