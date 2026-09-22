"""核心循环 —— 第 1 周唯一必须自己想透的文件。

先想清一件事：

    大模型是一个无状态的函数。文本进，文本出。
    它不记得你上一句说了什么。
    它不能碰你的文件，也不知道任务做没做完。

    所以「多轮对话」这件事，模型没帮你做 —— 是你自己做出来的。
    做法只有一招：把之前所有的话，每一轮都重新发一遍。

这个文件就是干这件事的地方。它一点都不复杂，复杂的是后面 3 周。
"""

from __future__ import annotations

from typing import Any

from agent import llm

DEFAULT_SYSTEM = "你是简洁的中文助手，回答不超过三句话。"


class AgentLoop:
    """一次会话。self.messages 就是这次会话的全部「记忆」。"""

    def __init__(self, system_prompt: str = DEFAULT_SYSTEM) -> None:
        # 全部记忆就是一个列表。
        # 第 0 条是「规矩」，它定义它是谁、怎么说话 —— 这一条永不改变，
        # 所以它放在最前面，且 reset() 时保留。
        # （这就是后面 SOUL.md / AGENTS.md 的雏形）
        self.messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
        ]
        # token 账本。每轮结束后更新，供 cli 打印。
        # 「每次请求都要记账」是第 1 周就开始的质量线 —— 没有它，成本失控你都不知道。
        self.last_usage: dict[str, Any] = {}

    def run(self, user_text: str) -> str:
        """走完一轮，返回模型回复。"""
        # ─────────────────────────────────────────────────────────
        # TODO ① —— 补三行，然后删掉下面的 raise
        #
        #   1) 把用户这句话追加进 self.messages
        #        格式：{"role": "user", "content": user_text}
        #   2) 调 llm.chat(self.messages)，它返回一个 Reply 对象
        #        （正文在 .text，token 账在 .usage）
        #   3) 把模型的回复也追加进 self.messages，role 要用 "assistant"
        #   4) 把 reply.usage 存进 self.last_usage，再把正文返回出去
        #        （没有第 4 步，终端就打不出 token 用量）
        #
        #   动手前先回答两个问题 —— 答不上来就还不该动手：
        #
        #   Q1：第 3 步为什么必须做？只做 1、2 步会怎样？
        #       提示：第 2 步每一次发出去的，是整个 self.messages。
        #
        #   Q2：能不能在 __init__ 里就把 user 和 assistant 这两条也提前塞好？
        #       为什么不行？
        # ─────────────────────────────────────────────────────────
        raise NotImplementedError("TODO ① 还没补")

    def run_stream(self, user_text: str) -> str:
        """和 run() 做同一件事，只是边收边打印。"""
        # ─────────────────────────────────────────────────────────
        # TODO ② —— 同样是三步，难点在这个「拼」字
        #
        #   llm.stream_chat(self.messages) 是个「产出一小段、停一下、再产出一小段」的
        #   迭代器。你可以用一个 for 循环消费它：
        #
        #       for piece in llm.stream_chat(self.messages):
        #           ...
        #
        #   你需要做四件事：
        #     1) 先追加 user 消息（和 TODO ① 一样）
        #     2) 准备一个空列表碎片收集箱，比如 pieces = []
        #        每拿到一小段：存进收集箱，同时立刻打出去
        #            print(piece, end="", flush=True)
        #        end="" 让它别换行，flush=True 让它立刻显示而不是攒着
        #     3) 循环结束后，把收集箱拼成完整一句："".join(pieces)
        #     4) 把这一整句追加进 self.messages（role="assistant"），再返回
        #        （流式接口不返回 usage，所以 self.last_usage 这次留空就行）
        #
        #   为什么必须拼完整了再存？直接存 100 个小碎片不行吗？
        #     提示：messages 的每一项必须是「一条完整消息」，
        #           而且下一轮会把它们全部重新发出去，格式坏了模型就懵了。
        # ─────────────────────────────────────────────────────────
        raise NotImplementedError("TODO ② 还没补")

    def reset(self) -> None:
        """清空对话内容，保留开头的 system。"""
        self.messages = self.messages[:1]
