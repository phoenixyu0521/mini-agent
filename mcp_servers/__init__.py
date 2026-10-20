"""hello-agents 文档的 MCP server（作品本体）。

独立进程，通过 stdio 提供四个工具：
    list_chapters   有哪些章
    get_outline     某章有哪些节
    search_docs     检索小节（两级检索的第一级）
    get_section     取某节全文（第二级）

它和 mini-agent 的 `agent/mcp/` 是**镜像的两端**：
那个包是客户端（连别人），这个包是服务端（被别人连）。
"""
