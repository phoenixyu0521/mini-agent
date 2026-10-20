"""把远端工具「伪装」成本地工具。

## 为什么必须伪装

    Agent 的循环（`loop.py`）只认 `Tool` 这个抽象：拿到 `schema()` 塞给模型，
    拿到 `run(**kwargs)` 执行。

    它**不需要**知道工具是在本进程里读文件，还是在一个 node 子进程里干活。
    这就是抽象的价值：**接入新东西时，改动落在最外层，内核一行不动。**

    这个类就是那层「最外层」。加完它，`loop.py` / `registry.py` / `server.py`
    全都不用改。

## 为什么名字要加前缀

    远端工具叫 `read_file`，本地也有一个叫 `read_file` 的。
    `ToolRegistry.register()` 遇到重名会直接抛异常（这是对的，见 registry.py）。

    但真正危险的不是抛异常，是**你以为你调的是本地那个**。
    模型说「读一下 config.json」，结果去了 MCP server 那边读它自己的文件系统 ——
    **不报错，答案却是错的**。这类 bug 查起来极其费劲。

    所以远端工具一律加前缀：`mcp_<server>_<原工具名>`。
"""

from __future__ import annotations

from typing import Any

from agent.mcp.client import MCPClient
from agent.tools.base import EMPTY_PARAMS, Tool


class RemoteTool(Tool):
    """一个住在别的进程里的工具。"""

    # MCP client 内部有一把大锁，同一时刻只能处理一个调用（见 client.py 说明）。
    # 所以这里**必须**是 False —— 不是保守，是如实反映。
    concurrency_safe = False

    def __init__(
        self,
        client: MCPClient,
        remote_name: str,
        spec: dict[str, Any],
        public_name: str,
    ) -> None:
        self._client = client
        self._remote_name = remote_name
        self.name = public_name
        self.description = (
            str(spec.get("description") or "").strip()
            or f"（来自 MCP server '{client.name}' 的工具 {remote_name}，没有描述）"
        )
        self.parameters = _normalize_schema(spec.get("inputSchema"))

    def run(self, **kwargs: Any) -> str:
        return self._client.call_tool(self._remote_name, kwargs)

    def __repr__(self) -> str:
        return f"RemoteTool({self.name} -> {self._client.name}.{self._remote_name})"


def _normalize_schema(raw: object) -> dict[str, Any]:
    """补全 JSON Schema 的三个必备字段。

    为什么要补：很多服务商看到 `parameters` 缺 `type` 或 `required`
    会直接 400，而报错信息是「invalid tools format」这种，指不到这里。
    MCP server 一般会给全，但「一般」不值得赌。
    """
    if not isinstance(raw, dict) or not raw:
        return EMPTY_PARAMS

    schema = dict(raw)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    schema.setdefault("required", [])
    return schema


def make_tools(
    client: MCPClient,
    *,
    only: list[str] | None = None,
    prefix: str | None = None,
) -> list[RemoteTool]:
    """把一个 client 的工具全变成本地 Tool。

    `only` 是**白名单**：只暴露点名的工具。
    这不是可选项 —— MCP server 动辄暴露 20+ 个工具，而工具的说明书
    **每一轮请求都要重发一遍**（实测 1 个工具 +260 prompt tokens）。
    接 5 个 server 就能把上下文吃光大半。

    另外一条：暴露面越小越安全。server 给你 `delete_file` 的时候，
    你不会希望模型「顺手」看见它。
    """
    prefix = prefix if prefix is not None else f"mcp_{client.name}_"
    wanted = set(only) if only else None

    tools: list[RemoteTool] = []
    for spec in client.list_tools():
        remote_name = str(spec.get("name") or "").strip()
        if not remote_name:
            continue
        if wanted is not None and remote_name not in wanted:
            continue
        tools.append(RemoteTool(client, remote_name, spec, f"{prefix}{remote_name}"))
    return tools
