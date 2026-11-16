"""并行验收的独立补测（bot1）。

范围：
- 正式 `derive` 资源矩阵：开关关闭、资源不足、资源充足的硬件夹具。
- 生产 `Thread._allout_fanout` + 真实 `goal.judge` 子进程；模型部分是离线替身，
  因此这里证明的是“模型替身 + 真实判据”的集成，不是真模型多路。
- 用事件文件证明判据的 start/end 与重叠，不只比较墙钟耗时。
- 判据失败、外层取消后共享信号量释放；取消时真实子进程退出。
- 探索与判据共用同一个槽位（`_allout_explore_sem is _allout_work_sem`）时不超额。

不修改产品代码，不访问真实模型/API。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from ee.core.allout import derive
from ee.core.goal import GoalSpec
from ee.core.hwprobe import Hardware
from ee.server.threads import Thread

PYTHON = sys.executable


# ---------------------------------------------------------------- 临时 toy 仓库

def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, f"git {' '.join(args)} 失败：{p.stderr[:500]}"
    return p.stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "toy"
    repo.mkdir()
    (repo / "test_serial.py").write_text('''
import os
import pathlib
import time


def _write(line):
    path = pathlib.Path(os.environ["EE_PARALLEL_EVENTS"])
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        os.write(fd, (line + "\\n").encode("utf-8"))
    finally:
        os.close(fd)


def test_serial():
    cwd = os.getcwd()
    _write(f"start|{os.getpid()}|{cwd}")
    time.sleep(float(os.environ.get("EE_PARALLEL_HOLD", "0.15")))
    _write(f"end|{os.getpid()}|{cwd}")
''', encoding="utf-8")
    (repo / "test_barrier.py").write_text('''
import os
import pathlib
import time


def _write(line):
    path = pathlib.Path(os.environ["EE_PARALLEL_EVENTS"])
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        os.write(fd, (line + "\\n").encode("utf-8"))
    finally:
        os.close(fd)


def test_barrier():
    cwd = os.getcwd()
    _write(f"start|{os.getpid()}|{cwd}")
    expected = int(os.environ.get("EE_PARALLEL_EXPECT", "2"))
    deadline = time.monotonic() + 5
    seen = 0
    while time.monotonic() < deadline:
        try:
            lines = pathlib.Path(os.environ["EE_PARALLEL_EVENTS"]).read_text(
                encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        seen = sum(1 for line in lines if line.startswith("start|"))
        if seen >= expected:
            _write(f"overlap|{os.getpid()}|{cwd}")
            break
        time.sleep(0.01)
    _write(f"end|{os.getpid()}|{cwd}")
''', encoding="utf-8")
    (repo / "test_fail.py").write_text(
        "def test_fail():\n    assert False, 'intentional judge failure'\n", encoding="utf-8")
    (repo / "slow_judge.py").write_text('''
import os
import pathlib
import time

pathlib.Path(os.environ["EE_PARALLEL_PIDFILE"]).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(30)
print("1 passed")
''', encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.pyc\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "parallel toy")
    return repo


def _spec(repo: Path, test_file: str, *, timeout: float = 30) -> GoalSpec:
    command = f'& "{PYTHON}" -m pytest -q -p no:cacheprovider {test_file}'
    return GoalSpec(command=command, cwd=str(repo), min_tests=1, timeout=timeout,
                    validation_files=(test_file,), protected_tests=(test_file,),
                    protected_tests_complete=True)


def _slow_spec(repo: Path) -> GoalSpec:
    return GoalSpec(command=f'& "{PYTHON}" slow_judge.py', cwd=str(repo),
                    min_tests=1, timeout=60)


# ---------------------------------------------------------------- 计划与 fanout

def _plan(hardware: Hardware, parallel: bool, candidates: int) -> object:
    """用正式 derive；profile 只提供计划需要的字段，不连接 provider。"""
    return derive(
        config={"max_steps": 50, "max_steps_segments": 1},
        profile=SimpleNamespace(context_window=1_000_000, effort_param="", temperature=None),
        has_goal=True,
        hardware=hardware,
        candidate_gate=6,
        max_candidates=candidates,
        parallel_validation=parallel,
    )


class TrackingSemaphore:
    """可观测的信号量替身；接口与 asyncio.Semaphore 的 async with 一致。"""

    def __init__(self, limit: int):
        self.limit = limit
        self._sem = asyncio.Semaphore(limit)
        self.active = 0
        self.max_active = 0
        self.entries = 0

    async def __aenter__(self):
        await self._sem.acquire()
        self.active += 1
        self.entries += 1
        self.max_active = max(self.max_active, self.active)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.active -= 1
        self._sem.release()
        return False


def _install_fake_session(monkeypatch, parent_spec, *, explore_hold: float = 0.0):
    import ee.core.session as S

    class FakeChild:
        def __init__(self, cfg, **kw):
            self.config = cfg
            self.cwd = Path(kw.get("cwd") or ".")
            self.spec = parent_spec
            self.stats = SimpleNamespace(records=[], totals=lambda: {"cost": 0.0})
            self.policy = None
            self.budget = None
            self.budget_owner = False
            self.budget_root = None
            self.disk_enabled = False
            self.shell_name = "pwsh"
            self.user_home = Path.home()
            self.effort = ""

        def lock_model(self):
            pass

        async def run_turn(self, prompt, images=None, **_kw):
            sem = getattr(self, "_allout_explore_sem", None)
            if sem is not None:
                async with sem:
                    if explore_hold:
                        await asyncio.sleep(explore_hold)
            if False:                 # 空异步生成器：模型部分不产出任何回合事件
                yield None

    monkeypatch.setattr(S, "Session", FakeChild)


def _thread(repo: Path, home: Path):
    notes: list[tuple[str, dict]] = []
    th = Thread("t-parallel", home, lambda m, p: notes.append((m, p)),
                {"cwd": str(repo)}, persist=False)
    s = th.ensure_session()
    return th, s, notes


async def _run_fanout(th: Thread, spec: GoalSpec, plan, cap: int, *,
                      tracker: TrackingSemaphore | None = None):
    th._allout_judge_lock = asyncio.Semaphore(plan.judge_slots)
    shared = tracker if tracker is not None else asyncio.Semaphore(
        max(plan.explore_slots, plan.judge_slots))
    th._allout_explore_sem = shared if plan.explore_slots else None
    th._allout_work_sem = shared
    return await th._allout_fanout(spec, plan, "并行验收补测", cap)


def _events(path: Path) -> list[tuple[str, str, str]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            out.append((parts[0], parts[1], parts[2]))
    return out


def _max_concurrent(events) -> int:
    active = 0
    peak = 0
    for kind, _pid, _cwd in events:
        if kind == "start":
            active += 1
            peak = max(peak, active)
        elif kind == "end":
            active -= 1
    return peak


def _norm_path(value: str) -> str:
    return os.path.normcase(str(Path(value).resolve()))


def _assert_candidate_dirs(recs, events):
    starts = [cwd for kind, _pid, cwd in events if kind == "start"]
    assert len(starts) == len(recs), (starts, recs)
    assert {_norm_path(x) for x in starts} == {_norm_path(str(Path(r["cwd"]).resolve())) for r in recs}


# ---------------------------------------------------------------- 测试

def test_switch_off_keeps_judges_serial(tmp_path, ee_home, monkeypatch):
    repo = _make_repo(tmp_path)
    events = tmp_path / "events.log"
    monkeypatch.setenv("EE_PARALLEL_EVENTS", str(events))
    monkeypatch.setenv("EE_PARALLEL_HOLD", "0.15")
    plan = _plan(Hardware(16, 16, 50), False, 2)
    assert plan.judge_slots == 1, "开关关闭必须是单槽"
    assert plan.explore_slots >= 1

    async def go():
        th, s, _notes = _thread(repo, ee_home)
        _install_fake_session(monkeypatch, s.spec)
        return await _run_fanout(th, _spec(repo, "test_serial.py"), plan, 2)

    recs = asyncio.run(go())
    ev = _events(events)
    assert len([x for x in ev if x[0] == "start"]) == 2
    assert len([x for x in ev if x[0] == "end"]) == 2
    assert _max_concurrent(ev) == 1
    _assert_candidate_dirs(recs, ev)
    assert all(r["verdict"] is not None and r["verdict"].met for r in recs)
    assert all(not r.get("baseline_error") and r.get("regressed") is False for r in recs)


def test_resource_shortage_stays_single_slot(tmp_path, ee_home, monkeypatch):
    """实际 16 核/约 8GB 的资源形态：parallel_validation 开启仍只能 1 路判据。"""
    repo = _make_repo(tmp_path)
    events = tmp_path / "events.log"
    monkeypatch.setenv("EE_PARALLEL_EVENTS", str(events))
    monkeypatch.setenv("EE_PARALLEL_HOLD", "0.12")
    plan = _plan(Hardware(16, 7.8, 50), True, 2)
    assert plan.judge_slots == 1
    assert plan.explore_slots == 0

    async def go():
        th, s, _notes = _thread(repo, ee_home)
        _install_fake_session(monkeypatch, s.spec)
        return await _run_fanout(th, _spec(repo, "test_serial.py"), plan, 2)

    recs = asyncio.run(go())
    ev = _events(events)
    assert len([x for x in ev if x[0] == "start"]) == 2
    assert _max_concurrent(ev) == 1
    _assert_candidate_dirs(recs, ev)
    assert all(r["verdict"] is not None and r["verdict"].met for r in recs)
    assert all(not r.get("baseline_error") and r.get("regressed") is False for r in recs)


def test_sufficient_resources_run_parallel_judges_within_slots(tmp_path, ee_home, monkeypatch):
    """资源充足硬件夹具：开关开启时两个真实判据子进程实际重叠，且不超过 judge_slots。"""
    repo = _make_repo(tmp_path)
    events = tmp_path / "events.log"
    monkeypatch.setenv("EE_PARALLEL_EVENTS", str(events))
    monkeypatch.setenv("EE_PARALLEL_EXPECT", "2")
    monkeypatch.setenv("EE_PARALLEL_HOLD", "0.15")
    plan = _plan(Hardware(16, 16, 50), True, 2)
    assert plan.judge_slots >= 2

    async def go():
        th, s, _notes = _thread(repo, ee_home)
        _install_fake_session(monkeypatch, s.spec)
        return await _run_fanout(th, _spec(repo, "test_barrier.py"), plan, 2)

    recs = asyncio.run(go())
    ev = _events(events)
    assert len([x for x in ev if x[0] == "start"]) == 2
    assert any(x[0] == "overlap" for x in ev), "没有观察到两个判据在结束前都已启动"
    assert _max_concurrent(ev) == 2
    assert _max_concurrent(ev) <= plan.judge_slots
    _assert_candidate_dirs(recs, ev)
    assert all(r["verdict"] is not None and r["verdict"].met for r in recs)
    assert all(not r.get("baseline_error") and r.get("regressed") is False for r in recs)


def test_explore_and_judge_share_one_slot_budget(tmp_path, ee_home, monkeypatch):
    """探索与判据共用同一个槽位：一个在跑时另一个不能越过 limit。"""
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("EE_PARALLEL_EVENTS", str(tmp_path / "events.log"))
    monkeypatch.setenv("EE_PARALLEL_HOLD", "0.12")
    plan = _plan(Hardware(16, 8, 50), True, 2)
    assert plan.judge_slots == 1
    assert plan.explore_slots == 1
    tracker = TrackingSemaphore(1)

    async def go():
        th, s, _notes = _thread(repo, ee_home)
        _install_fake_session(monkeypatch, s.spec, explore_hold=0.12)
        return await _run_fanout(
            th, _spec(repo, "test_serial.py"), plan, 2, tracker=tracker)

    recs = asyncio.run(go())
    assert tracker.limit == 1
    assert tracker.max_active == 1, "探索/判据共用的槽位被超额使用"
    assert tracker.entries >= 3, "探索和判据都应实际经过共享槽位"
    assert all(r["verdict"] is not None and r["verdict"].met for r in recs)


def test_judge_failure_releases_slots(tmp_path, ee_home, monkeypatch):
    repo = _make_repo(tmp_path)
    plan = _plan(Hardware(16, 7.8, 50), True, 2)
    assert plan.judge_slots == 1

    async def go():
        th, s, _notes = _thread(repo, ee_home)
        _install_fake_session(monkeypatch, s.spec)
        recs = await _run_fanout(th, _spec(repo, "test_fail.py"), plan, 2)
        # 失败后槽位必须可立即再次取得，不能把失败误当成永久占用
        await asyncio.wait_for(th._allout_judge_lock.acquire(), 1)
        th._allout_judge_lock.release()
        await asyncio.wait_for(th._allout_work_sem.acquire(), 1)
        th._allout_work_sem.release()
        return recs

    recs = asyncio.run(go())
    assert len(recs) == 2
    assert all(r["verdict"] is not None and not r["verdict"].met for r in recs)
    assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_cancel_kills_real_judge_and_releases_slots(tmp_path, ee_home, monkeypatch):
    """取消外层 fanout：真实判据子进程退出，信号量释放，不留下候选 worktree。"""
    repo = _make_repo(tmp_path)
    pidfile = tmp_path / "judge.pid"
    monkeypatch.setenv("EE_PARALLEL_PIDFILE", str(pidfile))
    plan = _plan(Hardware(16, 7.8, 50), True, 1)
    assert plan.judge_slots == 1

    async def _wait_file(path: Path, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while not path.exists():
            if time.monotonic() >= deadline:
                raise AssertionError(f"等待 {path} 超时")
            await asyncio.sleep(0.01)

    async def go():
        th, s, _notes = _thread(repo, ee_home)
        _install_fake_session(monkeypatch, s.spec)
        task = asyncio.ensure_future(_run_fanout(
            th, _slow_spec(repo), plan, 1))
        await _wait_file(pidfile, 10)
        task.cancel()
        cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True
        assert cancelled, "外层取消必须传播"
        await asyncio.wait_for(th._allout_judge_lock.acquire(), 1)
        th._allout_judge_lock.release()
        await asyncio.wait_for(th._allout_work_sem.acquire(), 1)
        th._allout_work_sem.release()
        return int(pidfile.read_text(encoding="utf-8").strip())

    pid = asyncio.run(go())

    deadline = time.monotonic() + 5
    while True:
        p = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                           capture_output=True, text=True, encoding="cp936", errors="replace",
                           timeout=20)
        if str(pid) not in (p.stdout or ""):
            break
        if time.monotonic() >= deadline:
            raise AssertionError(f"判据子进程 {pid} 取消后仍在运行：{p.stdout[:200]}")
        time.sleep(0.1)
    assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
