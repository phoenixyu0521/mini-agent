"""测试共享夹具（fixture）。

## 为什么需要这个文件

测试里会反复造同样的东西：一份 30 条的历史、一个假的摘要函数。
每处复制一遍，改一次就要改十处。fixture 就是「按名字要，pytest 帮你造好」。

## 为什么这里能放「假摘要器」

`context.py` 的 `compact()` 不认识模型 —— 摘要函数是**参数传进去的**。
这就是第 3 周「依赖注入」在测试上的回报：
    传一个固定返回字符串的普通函数，就能测裁剪逻辑，
    **不联网、不花钱、结果 100% 确定**。
"""

from __future__ import annotations

from typing import Any

import pytest


# ── 造历史 ──────────────────────────────────────────────────
# 不加下划线前缀，因为它会被 fixture 包一层再交给测试。
def build_history(n: int) -> list[dict[str, Any]]:
    """造一条 system + n 条对话。

    内容带编号（"第0条" / "第1条" …），这样测试能断言
    「保留的到底是哪一段」—— 光看条数分不出头和尾。

    角色交替 user / assistant，和真实对话的形状一致。
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": "你是助手"}]
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        messages.append({"role": role, "content": f"第{i}条"})
    return messages


@pytest.fixture
def make_history():
    """把 build_history 当工厂交给测试：`history = make_history(30)`。"""
    return build_history


# ── 假摘要器 ────────────────────────────────────────────────
class FakeSummarizer:
    """冒充 llm.make_summarizer() 的返回物。

    为什么要记 `calls`？
        因为「摘要器收到的是哪一段」这件事，只能从**它自己**身上看。
        真实实现里这是一次 HTTP 请求，看不见输入；
        换成假的以后，输入输出全在手里 —— 这就是可测试性。
    """

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    def __call__(self, messages: list[dict[str, Any]]) -> str:
        # 必须存副本：调用方后面可能改原列表，存引用会看到「未来」的内容
        self.calls.append(list(messages))
        return "用户自称旭，正在学做 Agent。"


@pytest.fixture
def fake_summarize() -> FakeSummarizer:
    return FakeSummarizer()
