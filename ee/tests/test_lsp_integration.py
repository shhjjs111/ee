"""LSP 接线与真实服务器的集成用例（D6-2 / T9 的验收补充）。

**为什么单独一个文件**：`test_lsp.py` 用假服务器守住协议层语义，但它测不到两件事，
而这两件恰好是「用户能不能用上」的分界线：

1. **URI 盘符大小写。** 真实 pyright 把我们发的 `file:///E%3A/...` 回报成
   `file:///e%3A/...`。`DiagStore` 用 uri 做键，不归一就整条丢弃——
   现象是装好了服务器却永远 `not_open`。假服务器回原样 uri，永远测不出来。
2. **工具与缓存真的连在一起。** 生产代码里 `LspTool` 是无参构造，store 与 manager
   由会话接线补进去。假服务器用例手工 `LspTool(manager=..., store=...)`，
   所以「接线漏了」在那边全绿。

真实服务器用例默认跳过（拿不到 `pyright-langserver` 时），不自动安装任何东西。
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ee.core.lsp import diagnostics as D


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- URI 归一（无需服务器）

def test_norm_uri_treats_drive_letter_case_as_same_storage():
    """pyright 小写化盘符：`E%3A` 与 `e%3a` 必须指向同一条缓存。"""
    upper = "file:///E%3A/proj/a.py"
    lower = "file:///e%3a/proj/a.py"
    assert D.norm_uri(upper) == D.norm_uri(lower)


def test_norm_uri_keeps_posix_case_sensitive():
    """POSIX 下路径大小写有意义，只有盘符可以被归一。"""
    assert D.norm_uri("file:///home/A.py") != D.norm_uri("file:///home/a.py")


def test_publish_with_lowercase_drive_still_accepted():
    """端到端语义：我们记大写、服务器回小写，诊断必须被采纳而不是丢弃。

    这是 RED 用例——没有 `norm_uri` 时 `on_publish` 返回 False，状态停在 not_open。
    """
    st = D.DiagStore()
    ours = "file:///E%3A/proj/a.py"
    st.open(D.Doc(uri=ours, version=1, text="x: int = 'bad'\n"))
    theirs = "file:///e%3a/proj/a.py"
    accepted = st.on_publish({"uri": theirs, "version": 1,
                              "diagnostics": [{"message": "类型错误", "severity": 1}]})
    assert accepted is True
    res = st.get(ours)
    assert res.status == D.FRESH
    assert res.diagnostics and res.diagnostics[0]["message"] == "类型错误"


def test_lsp_tool_reads_store_injected_after_construction():
    """工具构造时没有 store，接线后补上的 store 必须被用上。

    生产路径是 `LspTool()`（无参）+ 会话接线；这里复现那个顺序，
    防止「测试全绿但工具永远报没有启用」再次发生。
    """
    from ee.tools.lsp import LspTool, uri_for
    from ee.tools.base import ToolContext, ToolState

    tmp = Path(__file__).parent / ".lsp-tool-wire-tmp"
    tmp.mkdir(exist_ok=True)
    try:
        f = tmp / "wire.py"
        f.write_text("x: int = 1\n", encoding="utf-8")
        store = D.DiagStore()
        u = uri_for(f)
        store.open(D.Doc(uri=u, version=1, text="x: int = 1\n"))
        store.on_publish({"uri": u, "version": 1, "diagnostics": [
            {"message": "接线后的诊断", "severity": 1,
             "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}}]})

        tool = LspTool()                      # 和 tools/__init__.py 里一样：无参
        assert tool.store is not store
        tool.store = store                    # 会话接线做的事
        tool.root = str(tmp)
        ctx = ToolContext(cwd=tmp, state=ToolState(), output_dir=tmp / ".out",
                          extra={"session": SimpleNamespace(lsp_store=store)})
        r = run(tool.run({"path": str(f)}, ctx))
        assert r.meta.get("status") == "fresh", r.output
        assert "接线后的诊断" in r.output
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 真实服务器（默认跳过）

def _find_pyright() -> list[str] | None:
    """找一个能启动 pyright-langserver 的 argv；找不到返回 None。

    优先 PATH 上的真实命令，其次 npx（不预装到项目里）。
    """
    exe = shutil.which("pyright-langserver")
    if exe:
        from ee.core.lsp.servers import spawn_argv
        return spawn_argv(exe, ["--stdio"])
    npx = shutil.which("npx")
    if npx:
        from ee.core.lsp.servers import spawn_argv
        return spawn_argv(npx, ["--yes", "--package", "pyright",
                                "pyright-langserver", "--stdio"])
    return None


PYRIGHT = _find_pyright()
needs_pyright = pytest.mark.skipif(
    PYRIGHT is None,
    reason="没有可用的 pyright-langserver（不自动安装；装了 pyright 或 npx 后会自动跑）")


@needs_pyright
def test_real_pyright_returns_fresh_diagnostics_for_current_version(tmp_path):
    """计划原文的唯一验收标准：写入确有类型错误的文件，模型收到该版本诊断。

    用中文目录 + 中文文件名：这是方案点名要在 Windows 原生验的维度，
    而且正是它暴露了盘符大小写的 uri 差异。
    """
    from ee.core.lsp import manager as M
    from ee.core.lsp import notify as N
    from ee.core.lsp.servers import Spec
    from ee.tools.lsp import LspTool, uri_for
    from ee.tools.base import ToolContext, ToolState

    d = tmp_path / "中文目录"
    d.mkdir()
    f = d / "有错.py"
    f.write_text("x: int = 'hello'\ny = undefined_name\n", encoding="utf-8")

    async def go():
        mgr = M.LspManager()
        store = D.DiagStore()
        notifier = N.Notifier(store)
        spec = Spec("python", command=list(PYRIGHT), available=True)
        handle = await mgr.acquire(str(d), "python", spec.identity, spec=spec)
        try:
            # 这一行是整条链的接口：没有它永远收不到诊断。
            handle.entry.client.on_notification["textDocument/publishDiagnostics"] = (
                lambda p: store.on_publish(p))
            notifier.default_client = handle.entry.client
            uri = uri_for(f)
            await notifier.opened(str(f), uri, f.read_text(encoding="utf-8"),
                                  language="python")

            deadline = D.Deadline(120)      # 首次 npx 拉包慢，给足预算
            while not deadline.expired() and store.get(uri).status != D.FRESH:
                await asyncio.sleep(0.3)
            res = store.get(uri)
            assert res.status == D.FRESH, (
                f"未取得真实诊断（status={res.status} reason={res.reason} "
                f"stderr={handle.entry.client.stderr_tail()[:500]}）")
            # pyright 报的是类型不符与未定义变量，两条都要在
            blob = " ".join(str(x.get("message") or "") for x in res.diagnostics)
            assert "not assignable" in blob or "not defined" in blob, blob

            tool = LspTool(manager=mgr, store=store, root=str(d))
            ctx = ToolContext(cwd=d, state=ToolState(), output_dir=d / ".out",
                              extra={"session": SimpleNamespace(lsp_store=store)})
            r = await tool.run({"path": str(f)}, ctx)
            assert r.meta.get("status") == "fresh", r.output
            assert r.meta.get("clean") is False
            assert "未取得诊断" not in r.output
        finally:
            await mgr.shutdown()

    run(go())
