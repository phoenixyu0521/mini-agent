"""一个假的 MCP server —— 专门用来测 client，不需要联网、不需要 npx。

## 为什么要先写它

真正的 MCP server（比如 `@modelcontextprotocol/server-filesystem`）要 npx 下载，
而且它的行为不受你控制：想测「server 在响应前先插一条通知」，你没法让它配合。
自己写一个 60 行的桩，**想让它出什么怪毛病就出什么**。

## 它怎么跟 client 说话

MCP over stdio 的规矩只有三条：

    1. 一行一条 JSON，UTF-8 编码，`\\n` 结尾
    2. stdout **只能有 JSON** —— 你写一句 print("debug") 就会污染协议
    3. 想打日志只能走 stderr

第 2 条是新人最容易踩的：调试时随手加个 print，client 立刻 json 解析失败，
而且报错位置离你加的 print 很远。

## 故意制造的麻烦

`tools/call` 响应之前，它**先发一条 notification**（`notifications/progress`）。
如果 client 天真地「发一个请求就收下一条消息」，它就会把这条通知当成响应，
然后在 `result` 上 KeyError —— 这是 MCP client 最常见的 bug。
"""

from __future__ import annotations

import json
import sys


def send(message: dict) -> None:
    """往 stdout 写一条消息。注意必须 flush，否则 client 会一直等。"""
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def notify(method: str, params: dict | None = None) -> None:
    """发通知：没有 id。收了不需要回。"""
    send({"jsonrpc": "2.0", "method": method, "params": params or {}})


TOOLS = [
    {
        "name": "echo",
        "description": "原样返回你给的文本。用来验证链路通不通。",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要回显的文本"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "两个整数相加，返回和。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"},
            },
            "required": ["a", "b"],
        },
    },
]


def call_tool(name: str, arguments: dict) -> tuple[str, bool]:
    """返回 (结果文本, 是否出错)。"""
    if name == "echo":
        return f"回声：{arguments.get('text', '')}", False
    if name == "add":
        try:
            return str(int(arguments["a"]) + int(arguments["b"])), False
        except (KeyError, TypeError, ValueError) as exc:
            return f"参数不对：{exc}", True
    return f"没有这个工具：{name}", True


def main() -> None:
    # 和真 server（mcp_servers/protocol.py）保持一致：显式把 stdio 切到 UTF-8。
    #
    # 为什么这个桩也必须写这两行：
    #     中文 Windows 的 sys.stdout 默认是 GBK。桩用文本模式 write 中文 → 落进管道的
    #     是 GBK 字节；而 client（agent/mcp/protocol.py 的 read）明确按 **UTF-8** 解码，
    #     于是中文变乱码。表现很奇怪：协议全对、只有中文内容坏掉。
    #
    #     实测（2026-10-26，代码页 936）：不写这两行，test_mcp.py 红 2 条，
    #     报错是 `assert '参数不对' in "[错误] …\ufffd\ufffd\ufffd…"` ——
    #     看起来像 client 的 bug，其实是桩自己吐错了编码。
    #     **假 server 也是 server，规范要求 UTF-8，它就不能例外。**
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            # 真 server 也会遇到，但没法回（没有 id），只能记一行日志
            print(f"[桩] 收到非 JSON：{raw[:80]}", file=sys.stderr)
            continue

        method = msg.get("method")
        msg_id = msg.get("id")

        # ── 通知：没有 id，不回 ──
        if msg_id is None:
            if method == "notifications/initialized":
                print("[桩] 握手完成", file=sys.stderr)
            continue

        # ── initialize：握手第一步 ──
        if method == "initialize":
            send({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-server", "version": "0.0.1"},
                },
            })
            continue

        # ── tools/list ──
        if method == "tools/list":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
            continue

        # ── tools/call ──
        if method == "tools/call":
            params = msg.get("params") or {}

            # ★ 故意在响应前插一条通知，专门坑「发一个收一个」的写法
            notify("notifications/progress", {"progress": 50, "total": 100})

            text, is_error = call_tool(params.get("name", ""), params.get("arguments") or {})
            send({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": [{"type": "text", "text": text}], "isError": is_error},
            })
            continue

        # ── 未知方法 ──
        send({
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        })


if __name__ == "__main__":
    main()
