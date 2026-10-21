"""MCP server 的骨架 —— 一行一条 JSON 的服务端。

## 和 client 端是镜像关系

    客户端：写请求 → 读响应，按 id 配对
    服务端：读请求 → 写响应

协议本身完全一样，难点换了一边：

    客户端要防「响应流里混进了通知」
    服务端要防「一个工具炸了，把整个进程带走」

## 服务端的三个硬约束

### 1. stdout 只能有 JSON

    这是新手最常踩的：调试时随手 `print("收到请求")`，
    stdout 立刻多出一行非 JSON，客户端解析失败。
    而报错位置离你加的 print 很远 —— 它会说「server 返回了非法 JSON」。

    **所有日志走 stderr。** print(..., file=sys.stderr) 或者 logging 配 StreamHandler(stderr)。

### 2. Windows 上 stdout 默认编码不是 UTF-8

    中文 Windows 的 stdout 默认 GBK。客户端按 UTF-8 解码 → 乱码，
    严重时直接抛异常。所以在 main() 里第一件事就是
    `sys.stdout.reconfigure(encoding="utf-8")`。

### 3. 工具失败不能崩进程

    一个工具的 bug 不该让整个 server 死掉 —— 那意味着客户端所有
    后续调用都失败。所以调用点必须 try/except，把异常变成
    JSON-RPC 的错误响应（或者 isError=true 的结果）。
"""

from __future__ import annotations

import inspect
import json
import sys
import traceback
from dataclasses import dataclass
from typing import Any, Callable

PROTOCOL_VERSION = "2024-11-05"

# 我们能兼容的协议版本。这几个版本之间，initialize / tools/list / tools/call
# 的形状没有变化（新增的是 structuredContent、elicitation 等我们不用到的能力），
# 所以可以实现「客户端要哪个就回哪个」。
#
# 为什么要协商，而不是固定回 2024-11-05：
# 规范说服务器回自己的版本，客户端若不支持就断开。新版客户端（Cursor 3.x）
# 发的是 2025-06-18，硬回一个它不认识的老版本，可能直接被判为不兼容 → 连接关闭。
# 而客户端报出来的错是「Connection closed」，看不出是版本问题。
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")


@dataclass
class ToolSpec:
    """一个工具的三件套：名字、说明、参数 schema，外加执行体。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., str]

    def to_dict(self) -> dict[str, Any]:
        # 注意这里的 key 叫 inputSchema（驼峰），不是 parameters。
        # 这是 MCP 的规定；写错的话客户端拿不到参数定义，模型只能瞎猜。
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


def tool(
    name: str,
    description: str,
    handler: Callable[..., str],
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> ToolSpec:
    """声明一个工具。

    显式传 handler，不用装饰器 —— 少一层间接，调试时一眼看出
    「这个名字对应哪个函数」。schema 也显式传，不从类型注解推：
    模型看到的就是这里写的内容，含糊了它就调不对。
    """
    return ToolSpec(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": properties or {},
            "required": required or [],
        },
        handler=handler,
    )


class MCPServer:
    def __init__(self, name: str, version: str, tools: list[ToolSpec]) -> None:
        self.name = name
        self.version = version
        self._tools = {t.name: t for t in tools}

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """处理一条消息。返回 None 表示不需要回（通知）。"""
        method = message.get("method")
        msg_id = message.get("id")

        # 通知：没有 id，不回
        if msg_id is None:
            if method == "notifications/initialized":
                log("客户端握手完成")
            return None

        try:
            if method == "initialize":
                params = message.get("params") or {}
                wanted = str(params.get("protocolVersion") or "")
                # 客户端要的版本我们支持 → 回它要的；不认识 → 回自己的默认值，
                # 让客户端自己决定兼不兼容（这是规范允许的做法）。
                agreed = wanted if wanted in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
                if wanted and agreed != wanted:
                    log(f"客户端请求协议 {wanted}，我们回退到 {agreed}")
                return self._ok(msg_id, {
                    "protocolVersion": agreed,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.name, "version": self.version},
                })

            # 有些客户端会 ping 探活。不回会让它认为连接已死。
            if method == "ping":
                return self._ok(msg_id, {})

            if method == "tools/list":
                return self._ok(msg_id, {
                    "tools": [t.to_dict() for t in self._tools.values()]
                })

            if method == "tools/call":
                return self._call(msg_id, message.get("params") or {})

            return self._err(msg_id, -32601, f"Method not found: {method}")

        except Exception as exc:  # noqa: BLE001 —— 任何意外都要变成响应，不能崩
            log(f"[服务端内部错误] {type(exc).__name__}: {exc}")
            traceback.print_exc(file=sys.stderr)
            return self._err(msg_id, -32603, f"Internal error: {type(exc).__name__}: {exc}")

    def _call(self, msg_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}

        spec = self._tools.get(name)
        if spec is None:
            return self._err(msg_id, -32602, f"Unknown tool: {name}")

        if not isinstance(arguments, dict):
            arguments = {}

        # 只传函数真的接受的参数 —— 模型经常多给一两个字段，
        # 直接 **arguments 会 TypeError，而那是个和工具逻辑毫无关系的报错。
        accepted = set(inspect.signature(spec.handler).parameters)
        kwargs = {k: v for k, v in arguments.items() if k in accepted}

        try:
            text = spec.handler(**kwargs)
            is_error = False
        except Exception as exc:  # noqa: BLE001
            # 工具自己出错 → isError=true，客户端会把内容当成「工具说了句错误」
            # 交给模型，模型通常能自己改参数重试。比中断整轮对话好。
            text = f"{type(exc).__name__}: {exc}"
            is_error = True
            log(f"[工具失败] {name}: {text}")

        return self._ok(msg_id, {
            "content": [{"type": "text", "text": str(text)}],
            "isError": is_error,
        })

    @staticmethod
    def _ok(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _err(msg_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def log(text: str) -> None:
    """日志只能走 stderr。stdout 是协议通道。"""
    print(text, file=sys.stderr, flush=True)


def serve(server: MCPServer) -> None:
    """主循环：读一行 → 处理 → 写一行。"""
    # ⚠️ 中文 Windows 默认 GBK，不改成 UTF-8 客户端会乱码
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")

    log(f"{server.name} v{server.version} 已启动，等待客户端…")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            log(f"收到非法 JSON（跳过）：{line[:120]}")
            continue

        response = server.handle(message)
        if response is None:
            continue

        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()      # 不 flush，客户端会一直等

    log("stdin 已关闭，服务端退出")
