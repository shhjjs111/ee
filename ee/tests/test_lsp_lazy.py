"""LSP 服务器按需取用（不常驻）：启动时机、缓存可信度、引用不泄漏。

背景（`research/plans/2026-11-12-lsp-lazy-acquire.md`）：原实现让每个会话在**第一个回合**
就 acquire 一个语言服务器，并把句柄握到 `aclose`——manager 的 120 秒空闲回收永远等不到
refs 归零，会话缓存上限 40 就等于最多 40 个常驻进程，哪怕一次诊断都没要过。

现在的约定：
1. `_attach_lsp` 只接线，不启动进程；
2. `lsp` 工具每次调用自己 acquire → `didOpen`（磁盘当前内容）→ 等本版本诊断 → `release`；
3. 服务器没常驻，所以**缓存必须按内容指纹校验**：文件在别的途径被改过时，旧 fresh 不能充数
   （这一点是本节最重要的用例——拿旧诊断当新诊断，模型会照着过时的类型信息改代码）。
"""
from __future__ import annotations

import asyncio

import pytest

from ee.core.lsp import client as C
from ee.core.lsp import diagnostics as D
from ee.core.lsp import manager as M
from ee.core.lsp import servers as S
from ee.tools.base import ToolContext, ToolState


def run(coro):
    return asyncio.run(coro)


class FakeLsp(C.LspClient):
    """假语言服务器：didOpen/didChange 后立刻发布「本版本」诊断。

    `bad` 出现在内容里就报一条错误，否则报干净——这样用例可以区分「拿的是新内容还是旧缓存」。
    """

    def __init__(self, argv, **kw):
        super().__init__(argv, **kw)
        self.notified: list[str] = []

    async def start(self):
        self.started_ok = True

    async def request(self, method, params=None, timeout=30):
        return {"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}

    async def notify(self, method, params=None):
        if method not in ("textDocument/didOpen", "textDocument/didChange") or not params:
            return
        self.notified.append(method)
        td = params["textDocument"]
        text = td.get("text") or (params.get("contentChanges") or [{}])[0].get("text", "")
        diags = []
        if "bad" in text:
            diags = [{"message": f"bad：{text.strip()}", "severity": 1,
                      "range": {"start": {"line": 0, "character": 0},
                                "end": {"line": 0, "character": 1}}}]
        cb = self.on_notification.get("textDocument/publishDiagnostics")
        if cb is not None:
            cb({"uri": td["uri"], "version": td.get("version"), "diagnostics": diags})

    async def close(self):
        self.closed = True


@pytest.fixture
def lsp_session(workspace, monkeypatch):
    """一个开了 LSP 的真会话（只有 LSP 部分是假的）。"""
    from ee.config import load_config
    from ee.core.session import Session

    started: list[FakeLsp] = []

    def factory(argv, **kw):
        c = FakeLsp(argv, **kw)
        started.append(c)
        return c

    class SpyManager(M.LspManager):
        instances: list = []

        def __init__(self, *a, **kw):
            kw.setdefault("client_factory", factory)
            kw.setdefault("idle_ttl", 0)
            super().__init__(*a, **kw)
            SpyManager.instances.append(self)

    monkeypatch.setattr(M, "LspManager", SpyManager)
    # 不让用例依赖本机装没装 pyright：只验证「什么时候取、什么时候还」
    monkeypatch.setattr(S, "resolve", lambda language, config=None, which=None: S.Spec(
        language, command=["fake-langserver", "--stdio"], available=True))

    cfg = load_config(workspace)
    cfg.data.setdefault("lsp", {})["enabled"] = True
    s = Session(cfg, cwd=workspace, persist=False)
    run(s._attach_lsp(lambda _e: None))
    return s, started, SpyManager.instances[-1]


def _ctx(session, workspace) -> ToolContext:
    return ToolContext(cwd=workspace, state=ToolState(), output_dir=workspace / ".out",
                       extra={"session": session})


def test_attach_wires_but_does_not_start_a_server(lsp_session):
    """接线不能启动进程：不查诊断的会话不该养一个语言服务器。"""
    s, started, mgr = lsp_session
    assert s.lsp_manager is mgr and s.lsp_store is not None and s.lsp_notifier is not None
    assert started == [], "attach 阶段就启动了语言服务器（会话常驻进程的老行为）"
    assert mgr.stats() == []
    tool = s.tool_map["lsp"]
    assert tool.manager is mgr and tool.store is s.lsp_store, "工具没被接线"
    assert tool.spec is not None, "工具拿不到启动规格，按需 acquire 无从谈起"


def test_tool_acquires_opens_waits_and_releases(lsp_session, workspace):
    """一次调用：取进程 → didOpen（磁盘内容）→ 拿到本版本诊断 → 归还。"""
    s, started, mgr = lsp_session
    (workspace / "a.py").write_text("x = bad\n", encoding="utf-8")

    r = run(s.tool_map["lsp"].run({"path": "a.py"}, _ctx(s, workspace)))

    assert r.meta.get("status") == D.FRESH, r.output
    assert "bad" in r.output
    assert len(started) == 1, f"应该正好启动一个服务器，实际 {len(started)}"
    # 夹具用 idle_ttl=0：归还即回收，所以池子应当空、进程确实被关掉
    assert mgr.stats() == [], "调用结束后服务器还挂在池里（没有归还）"
    assert started[0].closed is True, "归还后进程没有被关掉"


def test_unchanged_file_is_served_from_cache_without_a_new_process(lsp_session, workspace):
    """内容没变：缓存可直接用，不再取进程（第二次调用应该很快）。"""
    s, started, mgr = lsp_session
    f = workspace / "a.py"
    f.write_text("x = bad\n", encoding="utf-8")
    tool = s.tool_map["lsp"]

    run(tool.run({"path": "a.py"}, _ctx(s, workspace)))
    r2 = run(tool.run({"path": "a.py"}, _ctx(s, workspace)))

    assert r2.meta.get("status") == D.FRESH
    assert len(started) == 1, "内容没变却又启动了一个进程（缓存没命中）"
    assert started[0].closed is True, "第一个进程没有被回收"


def test_file_changed_on_disk_invalidates_the_cache(lsp_session, workspace):
    """文件在别的途径被改过：旧 fresh 必须作废，重新取一次。

    这是移出「写后同步」后唯一的安全性保证：服务器不常驻，`DiagStore` 里的 fresh 只
    属于当时那份内容。若这里不按指纹校验，模型会拿到上一版本的诊断并据此改代码。
    """
    s, started, mgr = lsp_session
    f = workspace / "a.py"
    f.write_text("x = bad\n", encoding="utf-8")
    tool = s.tool_map["lsp"]
    run(tool.run({"path": "a.py"}, _ctx(s, workspace)))

    f.write_text("x = 1\n", encoding="utf-8")          # 另一种途径改了文件（无 didChange）
    r = run(tool.run({"path": "a.py"}, _ctx(s, workspace)))

    assert len(started) == 2, "内容变了却直接返回了旧缓存"
    assert r.meta.get("status") == D.FRESH
    assert r.meta.get("clean") is True, f"新内容没有诊断，却报了旧结果：{r.output}"


def test_acquire_failure_reports_unavailable_and_does_not_leak(lsp_session, workspace, monkeypatch):
    """启动失败：如实报 unavailable，不抛、不留引用。"""
    s, started, mgr = lsp_session
    (workspace / "a.py").write_text("x = 1\n", encoding="utf-8")

    async def boom(*a, **kw):
        raise M.LspUnavailable("fake-langserver 起不来")

    monkeypatch.setattr(mgr, "acquire", boom)
    r = run(s.tool_map["lsp"].run({"path": "a.py"}, _ctx(s, workspace)))

    assert r.meta.get("status") == D.UNAVAILABLE
    assert "未取得诊断" in r.output and "起不来" in r.output
    assert started == [] and mgr.stats() == []
