"""必然久等的 shell 命令自动转后台（实测：91 回合里 45 分钟纯阻塞）。

实测现象（~/.ee/sessions 全部真实会话，扣掉分段续跑上线前的）：
- 模型把 `sleep 540; gh run list` 这类「睡一会儿再查」的命令写在前台，一条就撞
  600s 超时，进程树被杀，什么都没拿到；
- `gh run watch` 单跑一次实测 600s；
- background=true 在 4442 次 shell 调用里只用了 56 次（1.3%）——提示词指望不上，
  必须在代码里识别并接管。

同时修 full_pytest_hint 的两个漏检（本机标准写法一次都没触发过警示）：
- 解释器带路径（`./.venv/Scripts/python.exe -m pytest`）识别不出来；
- `cd repo && pytest` 的 "cd" 被当成测试选择器，判成「有范围」；
- `-c ee/pyproject.toml` 的配置路径被当成测试选择器。
"""
from __future__ import annotations

import asyncio

from ee.core.bgproc import BgManager
from ee.tools.base import ToolContext, ToolState
from ee.tools.bgshell import MAX_WAIT, ShellOutputTool
from ee.tools.shell import ShellTool, full_pytest_hint, long_wait_reason


# ---------------------------------------------------------------- 检测今久等

def test_detects_sleep_then_poll():
    assert long_wait_reason("sleep 540; gh run list --limit 1") is not None
    assert long_wait_reason("sleep 300 && curl -s http://x") is not None


def test_ignores_short_sleep():
    """短 sleep 和「单纯睡一会儿」不算：前台等完比转后台再查更简单。

    阈值是 90s：历史实测 63 条 sleep 里 54 条 ≥90s，10～59s 的只有 9 条。
    阈值太低（比如 10s）会把本该前台等完的短等待也转后台，模型还得再发一次
    shell_output，反而多一轮模型往返。
    """
    assert long_wait_reason("sleep 3; echo hi") is None
    assert long_wait_reason("sleep 60; echo hi") is None
    assert long_wait_reason("sleep 30") is None
    assert long_wait_reason("echo hi") is None


def test_detects_ci_watch():
    assert long_wait_reason("gh run watch 12345 --exit-status") is not None
    assert long_wait_reason("cd repo && gh pr checks 7 --watch") is not None


def test_long_wait_runs_in_background(tmp_path):
    """命中检测时，即使模型没传 background，也要走后台并告知模型去 shell_output 等。"""
    from ee.tools.shellenv import resolve_shell

    sh = resolve_shell("auto")
    written = []
    # `sleep 120; echo done`：检测会命中，但这里不想真等——shell 一进后台就立刻返回
    cmd = "sleep 120; echo done"

    async def go():
        mgr = BgManager(tmp_path / "logs", owner="t")
        ctx = ToolContext(cwd=tmp_path, state=ToolState(), output_dir=tmp_path / ".out",
                          shell=sh, extra={"session": type("S", (), {"bg": mgr})()})
        ctx.emit = written.append
        r = await ShellTool().run({"command": cmd}, ctx)
        mgr.kill_all_sync()
        return r

    r = asyncio.run(go())
    assert r.meta.get("background") is True, f"应自动转后台，实际：{r.output[:300]}"
    assert r.meta.get("bg_id"), "转后台后应给出 bg id"
    assert "后台" in r.output, f"结果里应说明已转后台：{r.output[:300]}"
    assert any("后台" in str(n) for n in written), "应发出一条 Notice 告诉用户/模型已转后台"


def test_explicit_background_false_is_respected(tmp_path):
    """模型显式传 background=false：尊重它，不偷偷转后台。"""
    async def go():
        ctx = ToolContext(cwd=tmp_path, state=ToolState(), output_dir=tmp_path / ".out",
                          timeout_default=5, max_timeout=5)
        return await ShellTool().run({"command": "sleep 120; echo hi", "background": False}, ctx)

    r = asyncio.run(go())
    assert r.meta.get("background") is not True, "显式 background=false 时不该转后台"


def test_no_background_manager_falls_back_to_foreground(tmp_path):
    """没有 BgManager（精简模式/关掉后台）时退回前台，不能报错。"""
    async def go():
        ctx = ToolContext(cwd=tmp_path, state=ToolState(), output_dir=tmp_path / ".out",
                          timeout_default=5, max_timeout=5)
        return await ShellTool().run({"command": "sleep 120; echo hi"}, ctx)

    r = asyncio.run(go())
    assert r.meta.get("background") is not True


# ---------------------------------------------------------------- wait 上限

def test_shell_output_wait_allows_long_ci_waits():
    """等 CI 一次就能等几分钟，上限不能卡在 120s。

    实测 147 次 shell_output 里 61 次请求了 180～600 秒，全被静默截到 120，
    模型以为没等到又发一次，28 条轮询链多出 106 次徒劳请求。
    """
    assert MAX_WAIT >= 600, f"MAX_WAIT 太小（{MAX_WAIT}s）：长任务会被静默截断"


def test_shell_output_schema_advertises_real_cap():
    desc = ShellOutputTool.parameters["properties"]["wait"]["description"]
    assert str(MAX_WAIT) in desc, f"wait 描述应写明真实上限：{desc}"


# ---------------------------------------------------------------- pytest 漏检

def test_hint_detects_path_qualified_python():
    """本机标准写法带路径；原先只认裸 `python`，一次警示都没触发过。"""
    assert full_pytest_hint("./.venv/Scripts/python.exe -m pytest") is not None
    assert full_pytest_hint("./.venv/Scripts/python.exe -m pytest -c ee/pyproject.toml -q -n 8") is not None
    assert full_pytest_hint(r".\.venv\Scripts\python.exe -m pytest -q") is not None


def test_hint_sees_through_cd_prefix():
    """`cd repo && pytest` 不能因为第一个 token 是 cd 就被当成「有范围」。"""
    assert full_pytest_hint("cd repo && pytest -q") is not None


def test_hint_treats_test_dir_as_full_run():
    """`pytest tests` 跑的是整个目录，等同全量，不能算缩小。"""
    assert full_pytest_hint("pytest tests") is not None
    assert full_pytest_hint("./.venv/Scripts/python.exe -m pytest ee/tests -q") is not None


def test_hint_ignores_option_values():
    """端正 `-c ee/pyproject.toml` 的值不是测试范围。"""
    assert full_pytest_hint("pytest -c ee/pyproject.toml") is not None
    assert full_pytest_hint("pytest -c ee/pyproject.toml ee/tests/test_x.py") is None


def test_hint_treats_marker_filter_as_scoped():
    """`-m slow` / `-m "not network"` 是 marker 筛选，和 `-k` 一样缩小了范围。

    回归：把 `-m` 当成带值选项后，它的值被当成路径剩余参数，于是这两种写法
    都会误报「全量」。`--markers` 只是列出可用 marker，根本没跑测试。
    """
    assert full_pytest_hint("pytest -m slow") is None
    assert full_pytest_hint('pytest -m "not network"') is None
    assert full_pytest_hint("pytest --markers") is None
    assert full_pytest_hint("python -m pytest -m slow") is None
