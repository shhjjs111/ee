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
