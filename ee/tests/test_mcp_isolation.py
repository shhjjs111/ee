"""MCP 连接隔离、项目信任与调用截止时间。

背景（源码核对结果）：

1. `McpManager.connect` 的缓存键是 `(cfg.name, cfg.key)`，不含工作区；`call(server, ...)`
   又按服务器名挑“最近连接”。两个项目都用同名的 `.mcp.json` 服务器时，A 的旧工具可能
   落到 B 的连接上执行 —— 工具的 schema / read_only / trusted 来自原连接，实际执行却在别处。
2. `parse_server` 把项目 `.mcp.json` 里的 `trusted` 直接读进 ServerConfig，而 `allow_id`
   指纹排除了 `trusted`。项目把 `trusted` 从 false 改成 true 不会要求重新允许，
   却能在权限层换来“不再询问”。
3. `_Base.request` 先 `await self._send(...)` 再 `wait_for(fut, timeout)`。HTTP 传输时
   `_send` 自己就在等响应，读取超时是 300 秒 —— 工具调用配置的 `tool_timeout` 管不到发送阶段。

修复约定：
- 连接身份 = (服务器名, 配置指纹, 规范化工作区)。工具持有 `ConnectionHandle`，
  调用与重连都用它，不再按名字找“最近的那个”。
- 只给名字调用时，若同名连接不唯一则报错，不猜。
- 项目配置不能授予 trusted；用户侧 `mcp.trusted_project` 才能。
- 单次请求的 timeout 覆盖“发送 + 等响应”全过程。
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

from ee.mcp.client import McpError, _Base
from ee.mcp.config import load_servers, parse_server
from ee.mcp.manager import McpManager

FAKE = str(Path(__file__).with_name("fake_mcp_server.py"))


def stdio_cfg(name="fake", **kw):
    return parse_server(name, {"command": sys.executable, "args": [FAKE], **kw})


def norm(p: Path) -> str:
    return str(Path(p).resolve()).lower()


# ---------------------------------------------------------------- 1) 连接按工作区隔离


def test_same_name_two_workspaces_get_separate_connections(tmp_path):
    """同配置、同名、不同工作区：两条连接，各自返回自己的工作目录。"""
    a, b = tmp_path / "proj-a", tmp_path / "proj-b"
    a.mkdir()
    b.mkdir()

    async def go():
        m = McpManager()
        cfg = stdio_cfg()
        sa = await m.connect(cfg, timeout=20, cwd=a)
        sb = await m.connect(cfg, timeout=20, cwd=b)
        assert sa.status == "ready", sa.error
        assert sb.status == "ready", sb.error
        assert sa.handle != sb.handle, "不同工作区共用了同一个连接身份"

        ra = await m.call(sa.handle, "echo", {"text": "__cwd__"})
        rb = await m.call(sb.handle, "echo", {"text": "__cwd__"})
        text_a = ra["content"][0]["text"]
        text_b = rb["content"][0]["text"]
        assert norm(a) in text_a.lower(), f"A 的调用落到了别的工作区：{text_a}"
        assert norm(b) in text_b.lower(), f"B 的调用落到了别的工作区：{text_b}"
        await m.close_all()

    asyncio.run(go())


def test_bare_name_call_refuses_when_ambiguous(tmp_path):
    """只给名字、又有多条同名连接时不猜：报错让人显式指定，而不是串到最近的连接。"""
    a, b = tmp_path / "proj-a", tmp_path / "proj-b"
    a.mkdir()
    b.mkdir()

    async def go():
        m = McpManager()
        cfg = stdio_cfg()
        await m.connect(cfg, timeout=20, cwd=a)
        await m.connect(cfg, timeout=20, cwd=b)
        with pytest.raises(McpError, match="多个|不唯一|歧义"):
            await m.call("fake", "echo", {"text": "x"})
        await m.close_all()

    asyncio.run(go())


def test_bare_name_still_works_when_unique(tmp_path):
    """只有一条同名连接时，旧调用方式继续可用（别把兼容路径修坏）。"""
    async def go():
        m = McpManager()
        await m.connect(stdio_cfg(), timeout=20, cwd=tmp_path)
        r = await m.call("fake", "echo", {"text": "ok"})
        assert r["content"][0]["text"] == "echo:ok"
        await m.close_all()

    asyncio.run(go())


def test_reconnect_keeps_its_own_workspace(tmp_path, monkeypatch):
    """断线重连必须沿用原连接的工作区，不能借用别的同名连接。"""
    a, b = tmp_path / "proj-a", tmp_path / "proj-b"
    a.mkdir()
    b.mkdir()

    async def go():
        m = McpManager()
        cfg = stdio_cfg()
        sa = await m.connect(cfg, timeout=20, cwd=a)
        await m.connect(cfg, timeout=20, cwd=b)
        await sa.client.close()             # 模拟 A 断线
        r = await m.call(sa.handle, "echo", {"text": "__cwd__"})
        assert norm(a) in r["content"][0]["text"].lower(), "重连后跑到了别的工作区"
        await m.close_all()

    asyncio.run(go())


# ---------------------------------------------------------------- 2) 项目不能授予信任


def test_project_config_cannot_grant_trusted():
    sc = parse_server("p", {"command": "x", "trusted": True}, source="project")
    assert sc.trusted is False, "项目 .mcp.json 不能把服务器标成免询问"


def test_user_config_can_still_trust_its_own_server():
    sc = parse_server("u", {"command": "x", "trusted": True}, source="user")
    assert sc.trusted is True


def test_project_trust_requires_user_side_record(tmp_path, monkeypatch):
    """项目服务器要被信任，必须用户在 ~/.ee/config.toml 里记过 name:sha8。"""
    import ee.mcp.config as mc

    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)
    (ws / ".mcp.json").write_text(
        '{"mcpServers": {"p": {"command": "pp", "trusted": true}}}', encoding="utf-8")

    # 只允许连接、没有信任记录 → 仍然是 ask
    monkeypatch.setattr(mc, "_user_allowed", lambda: {"p:" + parse_server(
        "p", {"command": "pp"}, source="project").key})
    monkeypatch.setattr(mc, "_user_trusted", lambda: set())
    sc = {s.name: s for s in load_servers({}, ws)[0]}["p"]
    assert sc.enabled is True and sc.trusted is False

    # 用户侧同时记了信任 → 才生效
    monkeypatch.setattr(mc, "_user_trusted", lambda: {sc.allow_id})
    sc2 = {s.name: s for s in load_servers({}, ws)[0]}["p"]
    assert sc2.trusted is True


# ---------------------------------------------------------------- 3) 总截止时间


class _SlowSend(_Base):
    """发送阶段就卡住的客户端：模拟 HTTP POST 一直不返回。"""

    def __init__(self, cfg):
        super().__init__(cfg, [])

    async def start(self) -> None:  # pragma: no cover - 不启动子进程
        pass

    async def _send(self, obj: dict) -> None:
        await asyncio.sleep(30)

    async def close(self) -> None:
        self.closed = True


def test_request_timeout_covers_send_phase():
    cfg = parse_server("slow", {"command": "x"})

    async def go():
        c = _SlowSend(cfg)
        t0 = time.monotonic()
        with pytest.raises(McpError, match="超时"):
            await c.request("tools/call", {"name": "t"}, timeout=0.3)
        spent = time.monotonic() - t0
        assert spent < 2.0, f"超时没有覆盖发送阶段（耗时 {spent:.1f}s）"
        assert c._pending == {}, "超时后 pending 没清理"

    asyncio.run(go())


def test_manager_call_has_an_outer_deadline(tmp_path):
    """manager.call 也要有总截止：传输层不守时的时候不能无限等。"""
    async def go():
        m = McpManager()
        st = await m.connect(stdio_cfg(), timeout=20, cwd=tmp_path)
        assert st.status == "ready", st.error
        # 让底层请求永远不返回
        async def hang(*_a, **_kw):
            await asyncio.sleep(30)
        st.client.call_tool = hang           # type: ignore[assignment]
        t0 = time.monotonic()
        with pytest.raises(McpError, match="超时"):
            await m.call(st.handle, "echo", {"text": "x"}, timeout=0.3)
        assert time.monotonic() - t0 < 2.0
        await m.close_all()

    asyncio.run(go())
