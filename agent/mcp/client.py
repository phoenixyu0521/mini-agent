"""MCP client —— 把「一行一条 JSON」变成「握手、列工具、调工具」三个方法。

## 这一层新增的唯一硬概念：请求和响应靠 `id` 配对

    MCP 不是 HTTP。HTTP 是「一个请求一个响应」的一问一答，
    你发出去之后收到的下一个响应，**必然**是这个请求的（除非有 pipeline）。

    MCP 的 stdout 是一条**双向杂流**：server 随时可以主动推消息过来
    （进度通知、日志通知、资源变更通知）。这些消息**没有 id**。

    所以「发一个请求，读一条消息，当成响应」这个写法**会坏**。
    坏的时机很具体：server 恰好在你的响应前推了一条通知，
    你拿到的是 `{"method": "notifications/progress"}`，
    再取 `message["result"]` 就是 KeyError —— 而且报错信息完全指不到真正的原因。

    正确做法是循环：读到的消息**没有 id** 就丢掉，**id 不匹配**也丢掉，
    直到 id 对上为止。

## 为什么这里要一把大锁

    工具是在线程池里并发跑的（第 4 周做的）。两个 MCP 工具同时调用时，
    如果各读各的，A 读到 B 的响应、B 读到 A 的，两个都错。

    最省事且正确的做法：**把「发请求 + 读到自己的响应」整段锁住**。
    代价是这个 client 同一时刻只能处理一个调用 —— 于是 MCP 工具
    一律声明 `concurrency_safe = False`，如实反映这个限制。

    nanobot 用 async + 每个请求一个 Future，能做到真并发。
    那是它的前提（全 async）允许的做法，我们这里不硬套。
"""

from __future__ import annotations

import threading
from typing import Any

from agent.mcp.protocol import MCPError, StdioTransport


class MCPClient:
    """一个已连接的 MCP server。"""

    def __init__(self, name: str, transport: StdioTransport, timeout: float = 20.0) -> None:
        self.name = name
        self.timeout = timeout
        self.server_info: dict[str, Any] = {}
        self.protocol_version = ""
        self._transport = transport
        self._lock = threading.Lock()
        self._next_id = 1
        self._started = False

    # ── 建立连接 ────────────────────────────────────────────

    def start(self) -> "MCPClient":
        """握手。必须**第一件事**做完，之后才能调任何方法。

        三步，顺序不能换：

            1. 发 initialize 请求          → server 回它支持的版本和能力
            2. 发 notifications/initialized → 告诉它「我准备好了」
            3. 之后才可以 tools/list

        为什么第 2 步不能省？
            协议规定的是「双方各发一次」。少了它，严格的 server 会认为
            握手没完成，后续请求一律拒绝。而宽松的 server 不报错 ——
            于是你换一个 server 就挂，属于「能跑但不对」的那类 bug。
        """
        result = self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mini-agent", "version": "0.12.0"},
        })

        self.protocol_version = str(result.get("protocolVersion") or "")
        self.server_info = dict(result.get("serverInfo") or {})

        self._notify("notifications/initialized")
        self._started = True
        return self

    # ── 两个业务方法 ────────────────────────────────────────

    def list_tools(self) -> list[dict[str, Any]]:
        """列出 server 提供了哪些工具。

        每个工具长这样（注意 `inputSchema`，不是 `parameters`）：

            {"name": "read_file",
             "description": "读一个文件",
             "inputSchema": {"type": "object", "properties": {...}, "required": [...]}}

        `inputSchema` 是**标准 JSON Schema**，而我们项目里 `Tool.parameters`
        也是 JSON Schema —— 看起来可以直接搬，实际不一定：
        MCP 允许用 `$ref` 复用定义、允许 `type: ["string","null"]` 这种写法，
        而 OpenAI 的 function calling 对这两者支持有限。
        nanobot 用 `_rewrite_local_schema_refs` / `_normalize_nullable_schema`
        专门处理这件事（约 150 行）。最小版本先不碰，遇到再说。
        """
        result = self._request("tools/list", {})
        tools = result.get("tools")
        return list(tools) if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """调一个远端工具，把结果拼成字符串。

        返回结构：
            {"content": [{"type": "text", "text": "..."}], "isError": false}

        `content` 是**数组**，因为一次调用可能返回多段（文本 + 图片）。
        我们只取文本，非文本的段落注明一下 —— 丢掉不提比报错更糟。
        """
        result = self._request("tools/call", {"name": name, "arguments": arguments})

        parts: list[str] = []
        for block in result.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            else:
                parts.append(f"[{block.get('type')} 内容，本客户端不支持展示]")

        text = "\n".join(p for p in parts if p) or "（工具没有返回任何内容）"
        if result.get("isError"):
            # 和 registry.call() 的约定保持一致：错误也走字符串，让模型自己看
            return f"[错误] MCP 工具 {name}：{text}"
        return text

    # ── 底下两个 ────────────────────────────────────────────

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """发通知：没有 id，不等回复。"""
        self._transport.write({
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        })

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """发请求，返回 result。失败一律抛 MCPError。

        注意这里是**唯一**跟 server 交互的入口 —— 所以锁加在这里，
        而不是散在各个方法里。
        """
        with self._lock:
            if not self._started and method != "initialize":
                # fail-closed：没握手就调，宁可当场报错也别发出半成品请求
                raise MCPError(f"MCP server '{self.name}' 尚未完成握手，不能调用 {method}")

            request_id = self._next_id
            self._next_id += 1

            self._transport.write({
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            })

            message = self._read_matching(request_id)

        if "error" in message:
            error = message["error"] or {}
            raise MCPError(
                f"MCP server '{self.name}' 的 {method} 失败："
                f"[{error.get('code')}] {error.get('message')}"
            )

        result = message.get("result")
        return result if isinstance(result, dict) else {}

    def _read_matching(self, request_id: int) -> dict[str, Any]:
        """一直读到「id 正好是这个请求」的那条消息为止。

        丢掉的两类：
            · 没有 id 的 → 是 server 主动推的通知
            · id 不是这个的 → 理论上不该出现（锁保证了串行），
                              但 server 乱序回也是合法行为，丢掉更稳
        """
        while True:
            message = self._transport.read(self.timeout)

            if "id" not in message:
                continue                      # 通知，跟这次请求无关

            if message.get("id") != request_id:
                continue                      # 别人的消息，扔掉

            return message

    # ── 收尾 ────────────────────────────────────────────────

    def close(self) -> None:
        with self._lock:
            self._transport.close()
            self._started = False

    @property
    def alive(self) -> bool:
        return self._started

    def __repr__(self) -> str:
        info = self.server_info.get("name", "?")
        return f"MCPClient(name={self.name!r}, server={info!r}, alive={self._started})"
