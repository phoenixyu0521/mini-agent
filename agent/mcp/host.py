"""MCP 连接的进程级管理 —— 一个进程只连一次，所有人共用。

## 为什么不能每个 AgentLoop 各连一份

    Web 模式下，每个会话一个 `AgentLoop`（`server.py` 的会话池）。
    如果每个 loop 都 `start()` 一遍，10 个会话就是 10 个 node 子进程，
    每个占几十 MB，而且它们各自持有一份 server 状态 ——
    模型在 A 会话里设的东西，B 会话看不见。

    所以这里跟 `llm.get_client()` 是同一个思路：**进程级共享 + 懒加载**。
    第二次 `attach()` 直接复用已经连好的工具，不再起子进程。

## 为什么失败不抛异常

    某个 MCP server 起不来（没装 npx、命令写错、端口被占），
    不应该让整个 Agent 启动失败 —— 那等于「一个可选插件坏了，主程序不能用」。

    所以每个 server 独立 try/except：失败的记进 `host.errors`，
    其余照常加载。这是第 2 周 `registry.call()` 就定下的规矩：
    **局部失败不该拖垮全局。**

    但「不抛」不等于「不说」—— 启动时会打印警告，把 stderr 尾巴带出来。
    静默降级比崩溃更难查。
"""

from __future__ import annotations

import atexit
import threading
from typing import Any

from agent.mcp.client import MCPClient
from agent.mcp.protocol import MCPError, StdioTransport
from agent.mcp.tool import RemoteTool, make_tools


class MCPHost:
    """一组 MCP server 的连接 + 它们提供的工具。"""

    def __init__(self, servers: dict[str, Any], timeout: float = 20.0) -> None:
        self.servers = servers
        self.timeout = timeout
        self.errors: dict[str, str] = {}
        self._clients: list[MCPClient] = []
        self._tools: list[RemoteTool] = []
        self._loaded = False
        self._lock = threading.Lock()

    # ── 对外 ────────────────────────────────────────────────

    def ensure(self) -> list[RemoteTool]:
        """加载（第一次）并返回工具列表。可以反复调用。"""
        with self._lock:
            if not self._loaded:
                self._load()
                self._loaded = True
            return list(self._tools)

    def close(self) -> None:
        with self._lock:
            for client in self._clients:
                try:
                    client.close()
                except MCPError:
                    pass   # 关的时候出错没什么可做的
            self._clients.clear()
            self._loaded = False

    def __repr__(self) -> str:
        return (
            f"MCPHost(servers={list(self.servers)}, "
            f"tools={len(self._tools)}, errors={list(self.errors)})"
        )

    # ── 加载 ────────────────────────────────────────────────

    def _load(self) -> None:
        for name, raw_spec in self.servers.items():
            if not isinstance(raw_spec, dict):
                self.errors[name] = "配置必须是一个对象"
                continue

            if raw_spec.get("enabled", True) is False:
                continue

            command = str(raw_spec.get("command") or "").strip()
            if not command:
                self.errors[name] = "缺少 command"
                continue

            try:
                client = MCPClient(
                    name,
                    StdioTransport(
                        name,
                        command,
                        [str(a) for a in (raw_spec.get("args") or [])],
                        raw_spec.get("env") or None,
                        raw_spec.get("cwd") or None,
                    ),
                    timeout=float(raw_spec.get("toolTimeout") or self.timeout),
                ).start()

                tools = make_tools(
                    client,
                    only=raw_spec.get("enabledTools") or None,
                )
            except MCPError as exc:
                self.errors[name] = str(exc)
                print(f"[警告] MCP server '{name}' 未加载：{exc}")
                continue

            self._clients.append(client)
            self._tools.extend(tools)
            print(
                f"[MCP] {name} 已连接（{client.server_info.get('name', '?')}），"
                f"暴露 {len(tools)} 个工具：{'、'.join(t.name for t in tools) or '（无）'}"
            )


# ── 进程级单例 ──────────────────────────────────────────────

_host: MCPHost | None = None
_host_signature: str = ""
_host_lock = threading.Lock()


def get_host(servers: dict[str, Any], timeout: float = 20.0) -> MCPHost:
    """拿到进程级唯一的 host。配置变了就重连。

    为什么用 `repr(servers)` 当签名？
        配置是一个嵌套很浅的 dict，`repr` 够用且不用引 json。
        真正的要求只是「配置改动能被感知」，不需要稳定哈希。
    """
    global _host, _host_signature

    signature = repr(servers)
    with _host_lock:
        if _host is None or _host_signature != signature:
            if _host is not None:
                _host.close()
            _host = MCPHost(servers, timeout=timeout)
            _host_signature = signature
            atexit.register(_host.close)   # 进程退出时别把 node 子进程留成孤儿
        return _host


def attach(registry: Any, servers: dict[str, Any], timeout: float = 20.0) -> MCPHost | None:
    """把 MCP 工具接进一个 ToolRegistry。没有配置就什么都不做。"""
    if not servers:
        return None

    host = get_host(servers, timeout=timeout)
    for tool in host.ensure():
        if registry.get(tool.name) is not None:
            continue          # 已经注册过（同一个 registry 被 attach 两次）
        registry.register(tool)
    return host


def close_all() -> None:
    """测试用：断开所有连接并清空单例。"""
    global _host, _host_signature
    with _host_lock:
        if _host is not None:
            _host.close()
        _host = None
        _host_signature = ""
