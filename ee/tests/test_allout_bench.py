"""全力以赴基准脚本（第三批）：只测夹具加载、dry-run 和判分，不调用模型。"""
from __future__ import annotations

import importlib.util
import io
import json
import shutil
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "research" / "bench" / "allout"


def _bench():
    spec = importlib.util.spec_from_file_location("allout_bench", ROOT / "scripts" / "allout_bench.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # 注册到 sys.modules：脚本里有 @dataclass，dataclasses 会按 cls.__module__ 回查模块，
    # 不注册会报 "NoneType has no attribute __dict__"。
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_fixtures_are_complete():
    mod = _bench()
    tasks = mod.load_tasks()
    assert [t.slug for t in tasks][:4] == ["01_first_pass", "02_small_repair",
                                           "03_wrong_approach", "04_green_but_missing"]
    slugs = {t.slug for t in tasks}
    assert {"99_fanout_smoke", "90_two_winners"} <= slugs, "两个冒烟夹具都要在（机器路径，不是质量样本）"
    smoke = next(t for t in tasks if t.slug == "99_fanout_smoke")
    assert "raise SystemExit(1)" in smoke.criterion, "冒烟夹具靠恒失败判据稳定触发分叉"
    two = next(t for t in tasks if t.slug == "90_two_winners")
    assert "criterion_runner.py" in two.criterion, "双候选夹具用计数器让前 3 次判据失败"
    for t in tasks:
        assert t.seed.is_dir() and t.public.is_dir() and t.hidden.is_dir()
        assert list(t.seed.glob("*.py")), f"{t.slug} 的 seed 里没有模块"


def test_dry_run_lists_samples_without_model_calls():
    mod = _bench()
    out = io.StringIO()
    with redirect_stdout(out):
        assert mod.main(["--dry-run"]) == 0
    payload = json.loads(out.getvalue())
    assert payload["dry_run"] is True
    assert payload["samples"] == 12, "6 个夹具 × 2 种模式（含 2 个冒烟夹具）"
    assert {p["candidates"] for p in payload["plan"] if p["mode"] == "allout"} == {1}
    assert {p["mode"] for p in payload["plan"]} == {"plain", "allout"}
    # 普通模式没有判据，火力全开有：这是两组唯一的差别
    assert all(p["criterion"] for p in payload["plan"] if p["mode"] == "allout")
    assert all(not p["criterion"] for p in payload["plan"] if p["mode"] == "plain")


def test_run_requires_explicit_confirmation():
    mod = _bench()
    with pytest.raises(SystemExit, match="--run --yes"):
        mod.main(["--run"])


@pytest.mark.parametrize("body,passed", [
    ('def shout(text):\n    return " ".join(text.split()).upper()\n', True),
    ('def shout(text):\n    return text\n', False),
])
def test_hidden_scoring_uses_a_separate_acceptance_dir(tmp_path, body, passed):
    """隐藏用例从独立目录跑：模型改不到它，只能影响被交付的代码。"""
    mod = _bench()
    task = next(t for t in mod.load_tasks() if t.slug == "01_first_pass")
    project = tmp_path / task.slug
    shutil.copytree(task.seed, project)
    (project / "shout.py").write_text(body, encoding="utf-8")
    got = mod.score(task, project)
    assert got["passed"] is passed, got["tail"]


def test_aggregate_separates_timeouts_unverified_and_cost():
    mod = _bench()
    rows = [
        {"mode": "allout", "task": "a", "reason": "met", "delivery": "verified", "passed": True,
         "cost": 0.2, "cost_complete": True, "timed_out": False, "duration_s": 10},
        {"mode": "allout", "task": "b", "reason": "met", "delivery": "verified", "passed": False,
         "cost": 0.2, "cost_complete": True, "timed_out": False, "duration_s": 10},
        {"mode": "allout", "task": "c", "reason": "completed", "delivery": "unverified", "passed": False,
         "cost": None, "cost_complete": False, "timed_out": False, "duration_s": 10},
        {"mode": "plain", "task": "a", "reason": "completed", "delivery": "", "passed": True,
         "cost": 0.1, "cost_complete": True, "timed_out": False, "duration_s": 10},
        {"mode": "plain", "task": "b", "reason": "error", "delivery": "", "passed": False,
         "cost": None, "cost_complete": False, "timed_out": True, "duration_s": 10},
    ]
    got = mod.aggregate(rows)["allout"]
    assert got["samples"] == 3 and got["solved"] == 1
    assert got["consistency_error_rate"] == 0.5, "报 met 但隐藏用例没过 = 不一致"
    assert got["unverified"] == 1
    assert got["cost_total"] == 0.4 and got["cost_complete"] is False
    assert got["cost_per_solved"] == 0.4
    assert mod.aggregate(rows)["plain"]["timeouts"] == 1


def test_public_tamper_signal_ignores_pycache(tmp_path):
    """跑过 pytest 生成的 pyc 不算"公开测试被改动"：只看源码。"""
    mod = _bench()
    public = tmp_path / "public"
    (public / "__pycache__").mkdir(parents=True)
    test = public / "test_public.py"
    test.write_text("def test_a(): pass\n", encoding="utf-8")
    (public / "__pycache__" / "test_public.cpython-312.pyc").write_bytes(b"pyc")

    sources = mod.public_sources(tmp_path)
    assert [p.name for p in sources] == ["test_public.py"], sources

    before = mod.fingerprint(sources)
    (public / "__pycache__" / "test_public.cpython-312.pyc").write_bytes(b"changed")
    assert mod.fingerprint(mod.public_sources(tmp_path)) == before
    test.write_text("def test_a(): assert True\n", encoding="utf-8")
    assert mod.fingerprint(mod.public_sources(tmp_path)) != before
