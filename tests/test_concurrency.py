"""工具并发的单测。

## 这里要锁住的只有两件事

    1. 只读工具真的同时在跑（总耗时 ≈ 最长那个，不是相加）
    2. 结果**按入参顺序**返回，和谁先跑完无关

第 2 条比第 1 条重要得多：
    并发慢一点用户能感知（等得久），顺序错了用户感知不到 ——
    模型会把 A 的结果当成 B 的，然后基于错误前提继续推理，答得很怪但不报错。
    **慢是可见的，顺序错是不可见的。**

跑法：`uv run pytest tests/test_concurrency.py -v`
"""

from __future__ import annotations

import time

from agent.llm import ToolCall
from agent.loop import AgentLoop
from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry


class FakeTool(Tool):
    """睡一会儿再返回，用来量并行效果。

    返回值里带完成时刻 —— 这是证明「不按完成顺序收集」的唯一证据：
    光看耗时证明不了保序，必须看到「后面的工具先跑完」而结果顺序没变。
    """

    description = "测试用"

    def __init__(self, name: str, delay: float, tag: str, safe: bool = True) -> None:
        self.name = name
        self.delay = delay
        self.tag = tag
        self.concurrency_safe = safe

    def run(self, **kwargs: object) -> str:
        time.sleep(self.delay)
        return f"{self.tag}@{time.perf_counter():.6f}"


def _agent_with(*tools: Tool) -> AgentLoop:
    """造一个只装了指定工具的 AgentLoop（不扫描真实工具目录）。"""
    agent = AgentLoop()
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    agent.tools = registry
    return agent


def _run(agent: AgentLoop, calls: list[ToolCall]) -> tuple[float, list[str]]:
    start = time.perf_counter()
    results = agent._execute_tool_calls(calls)
    return time.perf_counter() - start, results


def test_safe_tools_run_in_parallel():
    """两个 0.4s 的只读工具：并行约 0.4s，串行会是 0.8s。

    阈值放在 0.7 而不是 0.45 —— CI / 老机器上留余量，
    只要明显小于两者之和，就说明它们真的重叠了。
    """
    agent = _agent_with(FakeTool("a", 0.4, "A"), FakeTool("b", 0.4, "B"))
    elapsed, _ = _run(agent, [ToolCall("1", "a", "{}"), ToolCall("2", "b", "{}")])
    assert elapsed < 0.7, f"耗时 {elapsed:.2f}s，看起来还是串行"


def test_unsafe_tools_stay_serial():
    """有副作用的工具不许并发 —— 0.4 + 0.4 必须接近 0.8。"""
    agent = _agent_with(
        FakeTool("a", 0.4, "A", safe=False),
        FakeTool("b", 0.4, "B", safe=False),
    )
    elapsed, _ = _run(agent, [ToolCall("1", "a", "{}"), ToolCall("2", "b", "{}")])
    assert elapsed >= 0.75, f"耗时 {elapsed:.2f}s，不该并发"


def test_order_preserved_even_if_second_finishes_first():
    """⭐ 核心：后一个工具先跑完，结果仍要按入参顺序给出。

    A 睡 0.35s，B 只睡 0.05s → B 的完成时刻一定更早。
    如果实现是按「谁先完成」收集的，结果就会变成 [B, A]。
    """
    agent = _agent_with(FakeTool("a", 0.35, "A"), FakeTool("b", 0.05, "B"))

    _, results = _run(agent, [ToolCall("1", "a", "{}"), ToolCall("2", "b", "{}")])

    assert [r.split("@")[0] for r in results] == ["A", "B"]

    # 再确认一次「B 真的先完成」—— 否则这条测试证明不了保序
    done_a = float(results[0].split("@")[1])
    done_b = float(results[1].split("@")[1])
    assert done_b < done_a, "B 没有先完成，这条测试失去了意义"


def test_one_tool_failure_does_not_break_the_batch():
    """同批里一个炸了，另一个结果照样拿回来 —— 这是 registry.call() 的契约。"""

    class Boom(Tool):
        name = "boom"
        description = "炸"
        concurrency_safe = True

        def run(self, **kwargs: object) -> str:
            raise ValueError("boom")

    agent = _agent_with(FakeTool("ok", 0.05, "OK"), Boom())
    _, results = _run(agent, [ToolCall("1", "ok", "{}"), ToolCall("2", "boom", "{}")])

    assert results[0].startswith("OK@")
    assert results[1].startswith("[错误]")
    assert "ValueError" in results[1]
