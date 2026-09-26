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
from agent.tools import registry


DEFAULT_SYSTEM = "你是简洁的中文助手，回答不超过三句话。"


class AgentLoop:
    """一次会话。self.messages 就是这次会话的全部「记忆」。"""

    def __init__(self, system_prompt: str = DEFAULT_SYSTEM, with_tools: bool = False) -> None:
        # 全部记忆就是一个列表。
        # 第 0 条是「规矩」，它定义它是谁、怎么说话 —— 这一条永不改变，
        # 所以它放在最前面，且 reset() 时保留。
        # （这就是后面 SOUL.md / AGENTS.md 的雏形）
        #
        # 第 2 周起，这个列表里还会出现两种新面孔：
        #   {"role": "assistant", "tool_calls": [...]}   ← 模型点名要调函数
        #   {"role": "tool", "tool_call_id": "...", ...} ← 你回填的执行结果
        # 所以类型放宽成 Any —— 内容不再全是字符串了。
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
        ]
        # token 账本。每轮结束后更新，供 cli 打印。
        # 「每次请求都要记账」是第 1 周就开始的质量线 —— 没有它，成本失控你都不知道。
        self.last_usage: dict[str, Any] = {}

        # 第 2 周：只有明说要工具时才去扫描工具目录。
        # 为什么默认关着？因为工具说明书每一轮都要重发 ——
        # 实测 1 个工具就让 prompt_tokens 从 17 涨到 276。用不上就别付这笔钱。
        self.tools = registry.discover() if with_tools else None

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
        self.messages.append({"role": "user", "content": user_text})

        reply = llm.chat(self.messages)

        self.messages.append({"role": "assistant", "content": reply.text})
        self.last_usage = reply.usage
        return reply.text

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
        self.messages.append({"role": "user", "content": user_text})

        pieces: list[str] = []
        for piece in llm.stream_chat(self.messages):
            pieces.append(piece)
            print(piece, end="", flush=True)
        print()

        reply_text = "".join(pieces)
        self.messages.append({"role": "assistant", "content": reply_text})
        return reply_text

    def reset(self) -> None:
        """清空对话内容，保留开头的 system。"""
        self.messages = self.messages[:1]

    # ══════════════════════════════════════════════════════════════════
    #  以下进入第 2 周内容：让模型动手
    # ══════════════════════════════════════════════════════════════════

    def run_with_tools(self, user_text: str, max_iterations: int = 5) -> str:
        """带工具的一轮 —— 第 2 周的全部内容都在这个方法里。

        和 run() 的区别只有一处：

            run()       问一次 → 答一次 → 结束
            run_with_tools 问一次 → 模型说「我要调 get_time」
                           → 你替它调 → 把结果塞回去 → **再问一次**
                           → 这次它才说出「现在是 2026年9月26日 17:41」

        所以一次工具调用要发**两次**请求。实测消耗 714 tokens，
        是普通一问一答的十倍左右。

        max_iterations 是刹车：模型可能停不下来（一直调工具、或者工具结果它一直
        不满意），没有上限就会一直烧钱。5 是个保守值。

        注意本方法**不做流式**。工具调用需要先拿到完整的 tool_calls 才能决定下一步，
        边收边打印会把逻辑搞乱。流式留给普通对话（run_stream）就够了。
        """
        if self.tools is None:
            raise RuntimeError("没有启用工具。构造时要用 AgentLoop(with_tools=True)")

        # 第 1 件事和第 1 周完全一样：把用户这句话追加进去。
        self.messages.append({"role": "user", "content": user_text})

        # 说明书只取一次 —— 它在这一轮里不会变。
        schemas = self.tools.schema_list()

        for round_no in range(1, max_iterations + 1):
            # 每一轮都把**整个** self.messages 重新发一遍。
            # 和第 1 周是同一件事，只是列表里现在会多出 tool_calls 和 tool 两种消息。
            reply = llm.chat(self.messages, tools=schemas)

            # ─────────────────────────────────────────────────────────
            # TODO ① —— 它没要求调工具，说明这轮聊完了
            #
            #   写三行：
            #     1) 把它的回复存进 self.messages
            #        用 reply.to_message()。这个方法会顺手处理好 tool_calls 字段，
            #        也顺手帮你挡住了服务商不认识的私有字段。
            #     2) self.last_usage = reply.usage     （记账，不然 CLI 打不出 token）
            #     3) return reply.text                 （把正文交出去）
            #
            #   第 1 步为什么不能省？第 1 周你已经答过这个问题了：
            #   不存，下一轮发出去的历史里就没有它自己说过的话。
            # ─────────────────────────────────────────────────────────
            if not reply.wants_tools:
                raise NotImplementedError("TODO ① 还没补")

            # ─────────────────────────────────────────────────────────
            # TODO ② —— 它要求调工具，你替它办事
            #
            #   步骤比 ① 多，但顺序只有一条，别乱：
            #
            #     1) 先把这条 assistant 消息存进 self.messages
            #        还是 reply.to_message()
            #
            #        ⚠️ 顺序是硬要求：它必须排在下面那条 role="tool" 的**前面**。
            #           协议规定「先声明要调，再给结果」，反了服务商直接报错。
            #
            #     2) 逐个处理 reply.tool_calls（用 for 循环），每个做三件事：
            #
            #          a. 取参数：args = tc.parsed_arguments()
            #             记住 arguments 原样是**字符串**，别直接当 dict 用。
            #             （这个方法内部已经兜住了坏 JSON —— 为什么这么设计，
            #               去看 llm.py 里 ToolCall.parsed_arguments 的注释）
            #
            #          b. 执行：result = self.tools.call(tc.name, args)
            #             返回的一定是字符串，出错也会返回 "[错误] ..." 而不是抛异常。
            #
            #          c. 追加一条消息：
            #             {"role": "tool", "tool_call_id": tc.id, "content": result}
            #             这是第 1 周没见过的第四种 role。它的作用只有一个：
            #             告诉模型「这是你刚才点名的那个函数的结果」，靠 tool_call_id 对号。
            #
            #     3) 这一步**不要 return，也不要打印**。
            #        什么都不做，让 for 循环进入下一轮 —— 把已经变长的 messages
            #        再发一次。下一轮模型看到工具结果，才会说出人话。
            # ─────────────────────────────────────────────────────────
            raise NotImplementedError("TODO ② 还没补")

        # ─────────────────────────────────────────────────────────
        # TODO ③ —— 循环跑满 max_iterations 还没结束
        #
        #   走到这里说明模型停不下来：要么一直在调工具，要么工具给的结果它一直不满意。
        #   这就是 ROADMAP 里「为什么必须有 max_iterations」那个问题的答案 ——
        #   没有刹车，它会一直调，而每次调用都是两次请求，**一直在烧钱**。
        #
        #   要写的是：返回一句人话，说明出了什么事。
        #   别让它静默返回空（那样 CLI 会打印出 None，用户会以为程序坏了）。
        # ─────────────────────────────────────────────────────────
        raise NotImplementedError("TODO ③ 还没补")
