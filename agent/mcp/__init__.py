"""MCP 支持：让 Agent 用上「不在本进程里」的工具。

分层和 llm.py 是一个道理 —— **协议细节只关在这个包里**：

    protocol.py   一行一条 JSON 的读写、子进程、超时
    client.py     握手 / tools/list / tools/call，id 配对
    tool.py       把远端工具伪装成本地 Tool（loop.py 完全不知道有 MCP）
    host.py       进程级连接池 + 从 config.json 读配置

对外只暴露这几样：

    from agent.mcp import attach, close_all
"""

from agent.mcp.client import MCPClient
from agent.mcp.host import MCPHost, attach, close_all, get_host
from agent.mcp.protocol import MCPError, StdioTransport
from agent.mcp.tool import RemoteTool, make_tools

__all__ = [
    "MCPClient",
    "MCPError",
    "MCPHost",
    "RemoteTool",
    "StdioTransport",
    "attach",
    "close_all",
    "get_host",
    "make_tools",
]
