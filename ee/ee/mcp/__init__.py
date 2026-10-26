"""MCP（Model Context Protocol）客户端：stdio / streamable HTTP / 旧式 SSE 三种传输（D3-A）。

  config.py   读 [mcp] 配置与项目 .mcp.json
  client.py   传输与 JSON-RPC 会话（initialize → tools/list → tools/call）
  schema.py   把服务器给的 JSON Schema 规范化成各家模型都吃得下的样子
  manager.py  每个事件循环一个管理器：按配置缓存连接、并行连接、断线重连一次
  tool.py     McpTool：包成 ee 的 Tool（名字 mcp__<服务器>__<工具>）
"""
