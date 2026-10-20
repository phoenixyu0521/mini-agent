"""启动入口：`uv run python -m mcp_servers`

这是一个**独立的 MCP server 进程**，不依赖 mini-agent。
任何支持 MCP 的客户端（mini-agent / Claude Desktop / Cursor / WorkBuddy）
都能通过 stdio 连它。

测试它不必写客户端：
    echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | uv run python -m mcp_servers
"""

from __future__ import annotations

from mcp_servers.docs_tools import (
    get_outline,
    get_section,
    list_chapters,
    search_docs,
)
from mcp_servers.protocol import MCPServer, serve, tool

TOOLS = [
    tool(
        "list_chapters",
        "列出这本教程的全部章节（共 16 章）以及每章的节数。"
        "第一次回答与教程内容相关的问题时先调它，能确定该往哪一章找。",
        handler=list_chapters,
        properties={},
    ),
    tool(
        "search_docs",
        "在教程正文里检索小节。返回编号、所在章节和一小段上下文。"
        "**这是主要的检索入口**：先 search 找到候选，再用 get_section 取全文。"
        "查询用具体的技术词（如「记忆系统」「MCP 协议」「评估基准」），不要用整句问句。",
        handler=search_docs,
        properties={
            "query": {"type": "string", "description": "检索词，2–20 字的技术关键词"},
            "limit": {
                "type": "integer",
                "description": "返回条数，默认 5，最多 10",
            },
        },
        required=["query"],
    ),
    tool(
        "get_section",
        "按编号取一小节的完整内容。编号必须来自 search_docs 或 get_outline 的结果。",
        handler=get_section,
        properties={
            "section_id": {"type": "integer", "description": "小节编号（search_docs 结果里的 id）"},
        },
        required=["section_id"],
    ),
    tool(
        "get_outline",
        "列出某一章的所有小节标题（只有标题，没有正文）。"
        "当你知道该看哪一章但不确定是哪一节时用它。",
        handler=get_outline,
        properties={
            "chapter": {
                "type": "string",
                "description": "章号，写 '8' 或 'chapter8' 都可以",
            },
        },
        required=["chapter"],
    ),
]


def main() -> None:
    server = MCPServer(
        name="hello-agents-docs",
        version="0.1.0",
        tools=TOOLS,
    )
    serve(server)


if __name__ == "__main__":
    main()
