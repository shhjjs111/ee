"""LSP 客户端（D6-2 / T9）。用假服务器测协议层，不依赖真实 pyright。

这些用例守的都是「做错了不会报错、只会在真实项目里偶尔卡住」的地方：
Content-Length 按字节还是字符、响应乱序、stderr 不排空导致死锁、取消后 pending 悬挂。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

FAKE = Path(__file__).resolve().parent / "fake_lsp_server.py"


def _cmd(*args: str) -> list[str]:
    return [sys.executable, str(FAKE), *args]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def client_mod():
    """U1 之前模块不存在——这个 import 失败就是 RED。"""
    from ee.core.lsp import client as C
    return C


def test_content_length_uses_utf8_bytes(client_mod):
    """Content-Length 必须按 UTF-8 字节算。

    假服务器回显 chars 与 bytes；用一串中文让两者明显不同（4 字 = 4 字符 12 字节）。
    如果客户端按字符数读，会截断或卡住——这个用例就是钉住它。
    """
    text = "中文测试abcd"      # 4 汉字(12 字节) + 4 字母 = 8 字符 / 16 字节

    async def go():
        c = client_mod.LspClient(_cmd())
        await c.start()
        try:
            await c.request("initialize", {"capabilities": {}}, timeout=10)
            res = await c.request("test/echo", {"text": text}, timeout=10)
            assert res["result"]["chars"] == len(text)
            assert res["result"]["bytes"] == len(text.encode("utf-8"))
            assert res["result"]["echo"] == text
        finally:
            await c.close()

    run(go())


def test_response_matched_by_id_out_of_order(client_mod):
    """并发发多个请求，响应按 id 匹配，不能靠到达顺序。"""

    async def go():
        c = client_mod.LspClient(_cmd())
        await c.start()
        try:
            await c.request("initialize", {"capabilities": {}}, timeout=10)
            # 故意让先发的慢、后发的快，制造乱序返回
            slow = asyncio.create_task(c.request("test/echo", {"text": "slow", "delay": 0.4}, timeout=10))
            fast = asyncio.create_task(c.request("test/echo", {"text": "fast", "delay": 0.0}, timeout=10))
            a, b = await asyncio.gather(slow, fast)
            assert a["result"]["echo"] == "slow"
            assert b["result"]["echo"] == "fast"
        finally:
            await c.close()

    run(go())


def test_stderr_is_drained(client_mod):
    """服务器狂写 stderr 时客户端不能阻塞。

    不排空的话，管道缓冲区写满后子进程会卡在 write 上，紧接着客户端等响应就超时——
    症状是「偶尔卡住」，极难定位。
    """

    async def go():
        c = client_mod.LspClient(_cmd("--mode", "stderr-flood"))
        await c.start()
        try:
            # 洪水发生在启动阶段；这里只要请求还能通就说明没被堵死
            res = await c.request("initialize", {"capabilities": {}}, timeout=15)
            assert "capabilities" in res["result"]
        finally:
            await c.close()

    run(go())


def test_close_cancels_pending(client_mod):
    """关闭时未响应的请求要被明确失败，不能留下悬挂的 await。"""

    async def go():
        c = client_mod.LspClient(_cmd("--mode", "silent"))
        await c.start()
        task = asyncio.create_task(c.request("test/echo", {"text": "x"}, timeout=30))
        await asyncio.sleep(0.3)          # 让它真的发出去并挂住
        await c.close()
        with pytest.raises(Exception):
            await asyncio.wait_for(task, 5)

    run(go())


def test_request_timeout_covers_send(client_mod):
    """超时要覆盖「发送 + 等响应」全过程，不能只管等响应。"""

    async def go():
        c = client_mod.LspClient(_cmd("--mode", "slow"))
        await c.start()
        try:
            await c.request("initialize", {"capabilities": {}}, timeout=10)
            with pytest.raises(Exception) as ei:
                await c.request("test/echo", {"text": "x"}, timeout=0.5)
            assert "超时" in str(ei.value) or "timeout" in str(ei.value).lower()
        finally:
            await c.close()

    run(go())


def test_process_exit_fails_pending(client_mod):
    """进程中途退出时，pending 请求要以可解释的错误失败（带 stderr 尾部）。"""

    async def go():
        c = client_mod.LspClient(_cmd("--mode", "crash"))
        await c.start()
        try:
            with pytest.raises(Exception) as ei:
                await c.request("test/echo", {"text": "x"}, timeout=15)
            assert "退出" in str(ei.value) or "closed" in str(ei.value).lower()
        finally:
            await c.close()

    run(go())


# ---------------------------------------------------------------- U2 启动策略

def test_cmd_wrapper_is_not_exec_directly():
    """Windows 的 .cmd/.bat 不能直接 exec。

    npx 之类在 Windows 上是 .cmd，create_subprocess_exec 找不到可执行映像，
    直接失败。必须走 shell + list2cmdline。这里只验策略产出，不真启动。
    """
    from ee.core.lsp import servers as S

    argv = S.spawn_argv(r"C:\node\npx.cmd", ["pyright-langserver", "--stdio"], windows=True)
    assert argv[0].lower().endswith("cmd.exe") or argv[0].lower().endswith("cmd")
    assert "/c" in [a.lower() for a in argv]
    joined = " ".join(argv)
    assert "npx.cmd" in joined and "--stdio" in joined


def test_plain_exe_is_exec_directly():
    """普通 .exe 不加 shell 包装（避免多一层解释器）。"""
    from ee.core.lsp import servers as S

    argv = S.spawn_argv(r"C:\py\pyright-langserver.exe", ["--stdio"], windows=True)
    assert argv == [r"C:\py\pyright-langserver.exe", "--stdio"]


def test_project_config_cannot_set_executable():
    """项目级配置只能关闭或收紧，不能指定可执行文件。"""
    from ee.core.lsp import servers as S

    user = {"python": {"command": ["pyright-langserver", "--stdio"]}}
    project = {"python": {"command": [r"C:\evil\payload.exe"]}, "enabled": True}
    merged = S.merge_config(user, project)
    assert merged["python"]["command"] == ["pyright-langserver", "--stdio"], merged


def test_project_config_can_disable():
    """项目配置可以关闭 LSP（收紧方向是允许的）。"""
    from ee.core.lsp import servers as S

    assert S.merge_config({"python": {"command": ["x"]}}, {"enabled": False})["enabled"] is False


def test_missing_server_reports_unavailable():
    """找不到服务器时给 unavailable，不抛、不尝试安装。"""
    from ee.core.lsp import servers as S

    spec = S.resolve("python", config={"python": {"command": ["definitely-not-installed-xyz"]}},
                     which=lambda name: None)
    assert spec.available is False
    assert "未安装" in spec.reason or "找不到" in spec.reason


# ---------------------------------------------------------------- U3 生命周期

def test_two_sessions_share_one_server():
    """同一个 key 只启动一次进程。"""
    from ee.core.lsp import manager as M

    started = []

    async def go():
        mgr = M.LspManager(client_factory=_counting_factory(started))
        a = await mgr.acquire("root1", "python", "pyright", spec=_spec())
        b = await mgr.acquire("root1", "python", "pyright", spec=_spec())
        assert len(started) == 1, started
        await mgr.release(a)
        await mgr.release(b)
        await mgr.shutdown()

    run(go())


def test_closing_one_session_keeps_server_for_other():
    """关一个会话不影响另一个在用同一服务器。"""
    from ee.core.lsp import manager as M

    started = []

    async def go():
        mgr = M.LspManager(client_factory=_counting_factory(started), idle_ttl=0)
        a = await mgr.acquire("root1", "python", "pyright", spec=_spec())
        b = await mgr.acquire("root1", "python", "pyright", spec=_spec())
        await mgr.release(a)
        # a 释放后计数还有 1，服务器必须还活着
        assert len(mgr._pool) == 1
        assert started[0].closed is False
        await mgr.release(b)
        assert len(mgr._pool) == 0        # 计数归零且 ttl=0 → 立刻回收
        await mgr.shutdown()

    run(go())


def test_different_roots_do_not_share():
    """不同工作区不能共用服务器（pyright 的配置按项目走）。"""
    from ee.core.lsp import manager as M

    started = []

    async def go():
        mgr = M.LspManager(client_factory=_counting_factory(started))
        await mgr.acquire("root1", "python", "pyright", spec=_spec())
        await mgr.acquire("root2", "python", "pyright", spec=_spec())
        assert len(started) == 2
        await mgr.shutdown()

    run(go())


def test_crash_restarts_on_next_acquire():
    """服务器进程退出后，下一次 acquire 要重启，不能永久缓存失败状态。"""
    from ee.core.lsp import manager as M

    started = []

    async def go():
        mgr = M.LspManager(client_factory=_counting_factory(started))
        h = await mgr.acquire("root1", "python", "pyright", spec=_spec())
        started[0].closed = True                    # 模拟崩溃
        await mgr.release(h)
        await mgr.acquire("root1", "python", "pyright", spec=_spec())
        assert len(started) == 2, "崩溃后应重新启动"
        await mgr.shutdown()

    run(go())


def _counting_factory(started: list):
    """返回一个能记下每次启动的 client 工厂。"""
    from ee.core.lsp import client as C

    class Fake(C.LspClient):
        def __init__(self, argv, **kw):
            super().__init__(argv, **kw)
            started.append(self)

        async def start(self):
            self.started_ok = True

        async def request(self, method, params=None, timeout=30):
            if method == "initialize":
                return {"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}
            return {"jsonrpc": "2.0", "id": 2, "result": None}

        async def notify(self, method, params=None):
            return None

        async def close(self):
            self.closed = True

    return lambda argv, **kw: Fake(argv, **kw)


def _spec():
    """测试用：一个"可用"的启动规格（真实 spawn 被 Fake client 拦下）。"""
    from ee.core.lsp.servers import Spec
    return Spec("python", command=["fake-langserver", "--stdio"], available=True)


# ---------------------------------------------------------------- U4 诊断语义

def _doc(uri="file:///a.py", text="x = 1\n", version=1):
    from ee.core.lsp import diagnostics as D
    return D.Doc(uri=uri, version=version, text=text)


def test_old_version_diagnostics_dropped():
    """先到 v2 的诊断再到 v1，v1 必须被丢弃（旧诊断不能回流）。"""
    from ee.core.lsp import diagnostics as D

    st = D.DiagStore()
    d = _doc(version=2)
    st.open(d)
    st.on_publish({"uri": d.uri, "version": 2, "diagnostics": [{"message": "v2 错误"}]})
    # 迟到的旧版本通知
    st.on_publish({"uri": d.uri, "version": 1, "diagnostics": [{"message": "v1 旧错误"}]})
    res = st.get(d.uri)
    assert res.status == "fresh"
    assert [x["message"] for x in res.diagnostics] == ["v2 错误"]


def test_stale_fingerprint_dropped():
    """版本相同但内容指纹不符（客户端已改内容）时丢弃。"""
    from ee.core.lsp import diagnostics as D

    st = D.DiagStore()
    d = _doc(version=1, text="x = 'type error'\n")
    st.open(d)
    # 来自另一份内容的诊断（指纹不匹配）
    st.on_publish({"uri": d.uri, "version": 1, "diagnostics": [{"message": "别的文件"}]},
                  fingerprint="deadbeef")
    res = st.get(d.uri)
    assert res.diagnostics == []
    assert res.status in ("stale", "unavailable", "timeout")   # 总之不能当成 fresh


def test_unavailable_is_not_clean():
    """没有服务器时必须说 unavailable，不能伪装成「无错误」。"""
    from ee.core.lsp import diagnostics as D

    st = D.DiagStore()
    d = _doc()
    st.open(d)
    res = st.get(d.uri, default_status="unavailable")
    assert res.status == "unavailable"
    assert res.diagnostics == []
    assert res.clean is False        # 关键：clean 只在 fresh 且无诊断时为 True


def test_fresh_empty_is_clean():
    """fresh 且无诊断才算干净。"""
    from ee.core.lsp import diagnostics as D

    st = D.DiagStore()
    d = _doc(version=1)
    st.open(d)
    st.on_publish({"uri": d.uri, "version": 1, "diagnostics": []})
    res = st.get(d.uri)
    assert res.status == "fresh" and res.clean is True


def test_budget_is_single_deadline():
    """多个文件的等待共用一个截止时间，不是每文件串行累加。"""
    from ee.core.lsp import diagnostics as D

    now = [100.0]
    dl = D.Deadline(seconds=2.0, clock=lambda: now[0])
    assert dl.left() > 0
    now[0] += 1.5
    assert 0 < dl.left() <= 0.6
    now[0] += 1.0
    assert dl.left() == 0 and dl.expired()


def test_close_clears_diagnostics():
    """didClose 之后该 uri 的缓存要清空，不能残留旧诊断。"""
    from ee.core.lsp import diagnostics as D

    st = D.DiagStore()
    d = _doc(version=1)
    st.open(d)
    st.on_publish({"uri": d.uri, "version": 1, "diagnostics": [{"message": "err"}]})
    assert st.get(d.uri).diagnostics
    st.close(d.uri)
    assert st.get(d.uri).diagnostics == []
    assert st.get(d.uri).status == "closed"


def test_changed_content_bumps_version_and_fingerprint():
    """内容改了要更新版本与指纹，旧版本的诊断随之失效。"""
    from ee.core.lsp import diagnostics as D

    st = D.DiagStore()
    d = _doc(version=1, text="x = 1\n")
    st.open(d)
    st.on_publish({"uri": d.uri, "version": 1, "diagnostics": [{"message": "旧错误"}]})
    cur = st.change(d.uri, "x = 1  # 改了\n")
    assert cur.version == 2
    res = st.get(d.uri)
    # 内容变了但还没收到新诊断：不能把旧诊断当成本版本的
    assert res.status != "fresh" or res.diagnostics == []


# ---------------------------------------------------------------- U5 工具接入

@pytest.fixture
def lsp_env(tmp_path, monkeypatch):
    """一个带 LSP 能力的会话环境：Fake client + DiagStore。"""
    from ee.core.lsp import manager as M
    from ee.core.lsp import diagnostics as D

    started = []
    mgr = M.LspManager(client_factory=_counting_factory(started), idle_ttl=0)
    store = D.DiagStore()
    return {"mgr": mgr, "store": store, "started": started, "root": str(tmp_path)}


def test_lsp_tool_returns_fresh_diagnostics(lsp_env, tmp_path):
    """工具返回该文件当前版本的诊断。"""
    from ee.tools.lsp import LspTool
    from ee.core.lsp import diagnostics as D

    root = lsp_env["root"]
    f = tmp_path / "a.py"
    f.write_text("x: int = 'type error'\n", encoding="utf-8")

    async def go():
        from ee.tools.lsp import uri_for
        store: D.DiagStore = lsp_env["store"]
        u = uri_for(f)
        doc = store.open(D.Doc(uri=u, version=1, text=f.read_text(encoding="utf-8")))
        store.on_publish({"uri": doc.uri, "version": 1,
                          "diagnostics": [{"message": "类型错误", "severity": 1,
                                           "range": {"start": {"line": 0, "character": 0},
                                                     "end": {"line": 0, "character": 1}}}]})
        tool = LspTool(manager=lsp_env["mgr"], store=store, root=root)
        r = await tool.run({"path": str(f)}, _lsp_ctx(tmp_path))
        assert r.ok
        assert "类型错误" in r.output
        assert r.meta.get("status") == "fresh"

    run(go())


def test_lsp_tool_reports_unavailable_not_clean(lsp_env, tmp_path):
    """没有服务器时必须明确说「未取得诊断」，不能显示成没有错误。"""
    from ee.tools.lsp import LspTool
    from ee.core.lsp import diagnostics as D

    f = tmp_path / "b.py"
    f.write_text("y = 1\n", encoding="utf-8")

    async def go():
        from ee.tools.lsp import uri_for
        u = uri_for(f)
        store = D.DiagStore()
        store.open(D.Doc(uri=u, version=1, text="y = 1\n"))
        store.set_status(u, D.UNAVAILABLE, "语言服务器未安装")
        tool = LspTool(manager=lsp_env["mgr"], store=store, root=lsp_env["root"])
        r = await tool.run({"path": str(f)}, _lsp_ctx(tmp_path))
        assert r.ok                       # 取不到诊断不等于工具失败
        assert "未取得诊断" in r.output
        assert "未安装" in r.output or "不可用" in r.output
        assert r.meta.get("status") == "unavailable"
        assert r.meta.get("clean") is False

    run(go())


def test_write_success_triggers_notify(tmp_path):
    """写工具成功后要通知服务器（didChange），否则模型拿不到新诊断。"""
    from ee.core.lsp import notify as N
    from ee.core.lsp import diagnostics as D

    calls = []

    class FakeClient:
        closed = False

        async def notify(self, method, params=None):
            calls.append((method, params))

    store = D.DiagStore()
    store.open(D.Doc(uri="file:///c.py", version=1, text="v1"))
    hook = N.Notifier(store=store)
    hook.attach("file:///c.py", FakeClient())

    async def go():
        await hook.changed("/c.py", "file:///c.py", "v2 content")
        await hook.opened("/c.py", "file:///c.py", "v2 content")
        methods = [m for m, _ in calls]
        assert "textDocument/didChange" in methods or "textDocument/didOpen" in methods

    run(go())


def test_notify_failure_does_not_fail_write(tmp_path):
    """通知抛异常时，写工具必须仍然成功（诊断不能反噬编辑）。"""
    from ee.core.lsp import notify as N
    from ee.core.lsp import diagnostics as D

    class BadClient:
        closed = False

        async def notify(self, method, params=None):
            raise RuntimeError("pipe broken")

    store = D.DiagStore()
    store.open(D.Doc(uri="file:///d.py", version=1, text="v1"))
    hook = N.Notifier(store=store)
    hook.attach("file:///d.py", BadClient())

    async def go():
        # 不抛异常
        await hook.changed("/d.py", "file:///d.py", "v2")
        assert hook.last_error is not None   # 但要留下痕迹

    run(go())


def test_no_server_edit_still_succeeds(tmp_path):
    """完全没有 LSP 时，写文件本身必须成功。"""
    from ee.core.lsp import notify as N
    from ee.core.lsp import diagnostics as D

    store = D.DiagStore()
    hook = N.Notifier(store=store)        # 没有任何 attach

    async def go():
        await hook.changed("/e.py", "file:///e.py", "content")   # 不该抛
        assert hook.last_error is None

    run(go())


def _lsp_ctx(ws):
    from ee.tools.base import ToolContext, ToolState
    from types import SimpleNamespace
    return ToolContext(cwd=ws, state=ToolState(), output_dir=ws / ".out",
                       extra={"session": SimpleNamespace()})
