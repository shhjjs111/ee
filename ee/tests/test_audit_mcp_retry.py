"""MCP 连接失败后要能重试，不能被冻结前缀永久挡住（2026-11 审计 P2#33）。

`_attach_mcp` 的 docstring 写得很清楚：「失败会留下 _mcp_tried 供诊断，但下一回合仍会
重试」。但实现有两处把它堵死：

1. `if self._mcp_done or self.prefix is not None or ...: return` —— 前缀在第一次回合就会
   冻结，于是从第二回合起直接 return，永远不再尝试。
2. `self._mcp_done = True` 在连接流程末尾无条件置位，注释还写着「成功或部分失败才算完成」
   —— 全部服务器都连不上时也置真，等于认定「已经连过了」。

两者叠加：第一回合 MCP 服务器没起来（很常见：它比 ee 慢启动），这个会话此后就再也用不上
MCP，只能新开一个会话。

修法是把「这一回合要不要试」抽成纯函数，并把「已建立前缀但一个 MCP 工具都没加上」的
情形放行——那种情况重试只赚不亏；而已经加上了 MCP 工具就不再重试，否则每回合改工具表
会让冻结前缀失效、白丢一次缓存。
"""
from ee.core.session import _should_attach_mcp


def test_retries_when_nothing_was_attached():
    """前缀已冻结但一个 MCP 工具都没加上：必须还能重试（原实现正是被这里挡住的）。"""
    assert _should_attach_mcp(done=False, enabled=True, has_prefix=True, tool_names=["read", "write"]) is True


def test_does_not_retry_once_mcp_tools_are_present():
    """已经有 MCP 工具了：再连会改工具表、让冻结前缀失效，每回合白丢一次缓存。"""
    assert _should_attach_mcp(
        done=False, enabled=True, has_prefix=True, tool_names=["read", "mcp__fs__ls"],
    ) is False


def test_retries_before_any_prefix_exists():
    assert _should_attach_mcp(done=False, enabled=True, has_prefix=False, tool_names=[]) is True


def test_no_retry_after_success_or_when_disabled():
    assert _should_attach_mcp(done=True, enabled=True, has_prefix=False, tool_names=[]) is False
    assert _should_attach_mcp(done=False, enabled=False, has_prefix=False, tool_names=[]) is False


def test_late_mcp_attach_refreezes_prefix(ee_home, workspace, monkeypatch):
    """首回合连不上、次回合连上：新工具必须真的进入下一次请求的 tools。

    只修 `_should_attach_mcp` 不够：它让第二次连接发生了，但 `_sample()` 发的仍是
    第一回合冻结的 `prefix.tools`，模型收不到 mcp__* schema，这个会话等于白连。
    """
    from conftest import collect, finish, make_session, run, text_chunks

    class Cfg:
        name = "fake"
        trusted = True

    class Handle:
        server = "fake"
        conn_id = "fake:1"

    class State:
        def __init__(self, status, tools=None):
            self.status = status
            self.error = "down" if status == "error" else ""
            self.tools = tools or []
            self.handle = Handle()
            self.cfg = Cfg()

        def summary(self):
            return {"name": "fake", "status": self.status, "tools": [], "server_info": {},
                    "conn_id": self.handle.conn_id, "cwd": ""}

    class Mgr:
        def __init__(self):
            self.calls = 0

        async def connect_all(self, data, cwd):
            self.calls += 1
            if self.calls == 1:
                return [State("error")]
            return [State("ready", [{"name": "echo", "description": "回声",
                                      "inputSchema": {"type": "object", "properties": {}}}] )]

    mgr = Mgr()
    monkeypatch.setattr("ee.mcp.manager.get_manager", lambda: mgr)
    monkeypatch.setattr("ee.mcp.config.load_servers",
                        lambda data, cwd: ([type("S", (), {"enabled": True, "name": "fake", "trusted": True})()], []))

    s, fake = make_session(workspace, [
        text_chunks("一") + finish("stop", prompt=100_000, hit=0),
        text_chunks("二") + finish("stop", prompt=105_000, hit=0),
    ])
    run(collect(s, "一"))
    assert not any(n.startswith("mcp__") for n in s.tool_map)

    run(collect(s, "二"))
    names = [t["function"]["name"] for t in fake.calls[-1]["tools"]]
    assert "mcp__fake__echo" in names, "第二次请求仍发旧 prefix.tools，MCP 工具对模型不可用"
    assert "mcp__fake__echo" in [t["function"]["name"] for t in s.prefix.tools]
    rec = s.stats.records[-1]
    assert rec.get("break") == "mcp_attach" or rec.get("cause") == "mcp_attach"


def test_new_mcp_server_hot_plugs(ee_home, workspace, monkeypatch):
    """会话已有 MCP 工具后，设置里新增第二个服务器也应生效，不必新开对话。"""
    from conftest import collect, finish, make_session, run, text_chunks

    servers = ["first"]

    class Srv:
        def __init__(self, name):
            self.name = name
            self.key = name
            self.enabled = True
            self.trusted = True

    class Handle:
        def __init__(self, name):
            self.server = name
            self.conn_id = name + ":1"

    class State:
        def __init__(self, name):
            self.status = "ready"
            self.error = ""
            self.tools = [{"name": "echo", "description": name,
                           "inputSchema": {"type": "object", "properties": {}}}]
            self.handle = Handle(name)
            self.cfg = Srv(name)

        def summary(self):
            return {"name": self.cfg.name, "status": self.status, "tools": [], "server_info": {},
                    "conn_id": self.handle.conn_id, "cwd": ""}

    class Mgr:
        async def connect_all(self, data, cwd):
            return [State(name) for name in servers]

    monkeypatch.setattr("ee.mcp.manager.get_manager", lambda: Mgr())
    monkeypatch.setattr("ee.mcp.config.load_servers",
                        lambda data, cwd: ([Srv(name) for name in servers], []))

    s, fake = make_session(workspace, [
        text_chunks("一") + finish("stop", prompt=1_000, hit=0),
        text_chunks("二") + finish("stop", prompt=1_100, hit=900),
        text_chunks("三") + finish("stop", prompt=1_200, hit=1_000),
    ])
    run(collect(s, "一"))
    assert "mcp__first__echo" in s.tool_map

    servers.append("second")
    run(collect(s, "二"))
    names = [t["function"]["name"] for t in fake.calls[-1]["tools"]]
    assert "mcp__second__echo" in names
    assert "mcp__first__echo" in names, "热插拔第二个服务器不能把第一个丢掉"
    assert "mcp__second__echo" in [t["function"]["name"] for t in s.prefix.tools]
    assert "mcp__first__echo" in [t["function"]["name"] for t in s.prefix.tools]
    rec = s.stats.records[-1]
    assert rec.get("cause") == "mcp_change" or rec.get("break") == "mcp_change"
