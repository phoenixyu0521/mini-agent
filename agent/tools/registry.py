"""工具注册表 —— 名字 → 工具，外加「自动发现」。

## 为什么需要自动发现？

    如果每加一个工具，都要在另一个文件里手动登记一遍，你迟早会漏。
    手写登记表的项目，长到 20 个工具时一定会出现「明明写了却不生效」的怪事。

    注册表改成扫描目录：凡是 base.Tool 的子类就自动收进来。
    于是加工具 = 新建一个 .py 文件，**别处一行都不用改**。

    nanobot 的 `agent/tools/loader.py` 干的就是这件事，规模大一些而已。

## 为什么 call() 里要把异常吞掉？

    工具执行失败（文件不存在、命令超时、参数类型不对）时，有两种处理方式：

        A. 抛出异常 → 整轮对话中断，用户看到报错
        B. 把错误信息当成「工具返回的结果」交还给模型

    这里是 B。理由：模型拿到「[错误] 文件不存在」之后，通常能自己改主意
    （换个路径、或者直接告诉用户文件不在）。A 则让你什么都没得到。

    这条约定到第 3 周会变得更重要 —— 那时工具会开始真的碰文件系统。
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

from agent.tools.base import Tool

# 这两个文件是工具层的基础设施，不是工具，扫描时要跳过。
SKIP_MODULES = {"base", "registry"}


class ToolRegistry:
    """工具集合。一个实例装着一批工具。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError(f"{type(tool).__name__} 没有设置 name")
        if tool.name in self._tools:
            # 重名是危险信号：模型点名时会调到你没预期的那一个。
            # 宁可在启动时就炸，也别等线上出怪事。
            raise ValueError(f"工具名重复：{tool.name}")
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> Tool | None:
        """按名字取工具本体，没有就返回 None。

        第 4 周加它是因为「并发」需要**问工具一个问题**：
        「你能和同批其他工具同时跑吗？」（`Tool.concurrency_safe`）

        这件事 `call()` 做不到 —— call 只给结果字符串，不给工具本身；
        也不该为了这个问题就真去执行一次工具。

        为什么不返回 `_tools[name]` 让它 KeyError？
            因为调用方要处理的正是「模型点了个不存在的工具名」这种情况，
            那是**正常输入**不是异常。返回 None 让调用方自己决定怎么兜。
        """
        return self._tools.get(name)

    def schema_list(self) -> list[dict[str, Any]]:
        """所有工具的说明书，直接塞进请求的 tools 字段。

        注意代价：这些内容**每一轮请求都要重发一遍**。
        实测 1 个工具会让 prompt_tokens 从 17 涨到 276。
        所以工具不是越多越好 —— 说明书本身是固定开销。
        """
        return [tool.schema() for tool in self._tools.values()]

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        """执行一个工具，返回字符串结果。**不抛异常**（见模块开头说明）。"""
        tool = self._tools.get(name)
        if tool is None:
            available = "、".join(self.names()) or "（无）"
            return f"[错误] 没有名为 {name} 的工具。当前可用：{available}"

        try:
            return str(tool.run(**arguments))
        except Exception as exc:  # noqa: BLE001 —— 故意兜住一切，见模块说明
            return f"[错误] 工具 {name} 执行失败：{type(exc).__name__}: {exc}"


def discover() -> ToolRegistry:
    """扫描 agent/tools/ 目录，把找到的工具自动收进注册表。

    做法分三步：
        1. pkgutil.iter_modules 列出这个包里有哪些 .py 文件
        2. 逐个 import 进来
        3. 看模块里有没有 base.Tool 的子类，有就实例化并登记

    第 3 步用 vars(module).values() 把模块里的名字全过一遍，
    所以一个 .py 文件里放多个工具类也是可以的。
    """
    registry = ToolRegistry()

    package = importlib.import_module("agent.tools")
    for _, module_name, _ in pkgutil.iter_modules(package.__path__):
        if module_name in SKIP_MODULES or module_name.startswith("_"):
            continue

        module = importlib.import_module(f"agent.tools.{module_name}")
        for obj in vars(module).values():
            is_tool_class = (
                isinstance(obj, type)
                and issubclass(obj, Tool)
                and obj is not Tool
            )
            if is_tool_class:
                registry.register(obj())

    return registry
