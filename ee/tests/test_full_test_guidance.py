"""全量测试的引导与兜底（真实评测发现的必失败设计）。

实测根因（三次同任务 03-full 评测）：
- 本仓库全量 pytest 约 1064 秒（135 个测试文件），相关子集只要 31 秒；
- eval 的 `--max-tool-timeout` 默认 180 秒；shell 工具绝对上限 600 秒；
- 所以模型一旦跑全量，必然超时，还要再转后台轮询几十秒——纯浪费。

模型开局跑全量的两个原因：
1. **信息缺失**：仓库根没有 AGENTS.md，模型不知道本仓库测试怎么跑；
2. **无即时反馈**：跑了全量也只看到「超时」，不知道「这命令本来就会超时」。

修复（C 方案）：
- C1 治本：仓库根加 AGENTS.md，写明测试怎么跑（本文件 test_project_agents_md_*）；
- C2 兜底：shell 检测到无参数全量 pytest 时，在结果里即时警示（test_full_pytest_warning_*）。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from ee.tools.base import ToolContext, ToolState
from ee.tools.shell import ShellTool, full_pytest_hint

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- C1：项目说明

def test_project_agents_md_exists_and_guides_tests():
    """仓库根必须有 AGENTS.md，且写明全量测试很慢、要跑子集。"""
    f = REPO_ROOT / "AGENTS.md"
    assert f.is_file(), "仓库根缺少 AGENTS.md：模型看不到本仓库测试怎么跑"
    body = f.read_text(encoding="utf-8")
    assert "pytest" in body, "AGENTS.md 应给出 pytest 命令"
    assert ("子集" in body or "相关测试" in body or "-k" in body), (
        "AGENTS.md 应引导跑相关子集，而不是全量"
    )
    # 必须说明全量很慢/会超时，否则模型没有理由避开
    assert ("全量" in body) and any(w in body for w in ("慢", "分钟", "超时")), (
        "AGENTS.md 应说明全量测试很慢/会超时"
    )


# ---------------------------------------------------------------- C2：工具兜底

def test_full_pytest_hint_detects_bare_pytest():
    """无参数（或只带 -q 之类）的 pytest 调用要被识别为「全量」。"""
    assert full_pytest_hint("python -m pytest -q") is not None
    assert full_pytest_hint("pytest") is not None
    assert full_pytest_hint("python -m pytest -q 2>&1 | tail -15") is not None


def test_full_pytest_hint_allows_scoped_runs():
    """指定了测试文件或 -k 过滤的是「相关子集」，不该警示。"""
    assert full_pytest_hint("pytest tests/test_x.py -q") is None
    assert full_pytest_hint("python -m pytest -q tests/test_prefix_cache.py") is None
    assert full_pytest_hint("pytest -k truncate") is None
    assert full_pytest_hint("ls -la") is None
    assert full_pytest_hint("python -m pytest --co") is None      # 只收集不跑，不算全量执行


def test_shell_warns_on_full_pytest_before_running(tmp_path):
    """跑全量 pytest 时，工具结果里必须带即时警示（不能只等超时）。"""
    async def go():
        ctx = ToolContext(cwd=tmp_path, state=ToolState(), output_dir=tmp_path / ".out",
                          timeout_default=5, max_timeout=5)
        # 命令会很快失败（目录里没有测试），但警示要在结果里，与成败无关
        r = await ShellTool().run({"command": "python -m pytest -q", "timeout": 5}, ctx)
        return r.output

    out = asyncio.run(go())
    assert "全量" in out or "子集" in out, (
        f"跑全量 pytest 时必须即时提示改为子集，否则模型只能等到超时才后知后觉。实际：{out[:400]}"
    )
