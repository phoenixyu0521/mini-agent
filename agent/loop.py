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

from agent import context, llm
from agent.memory import Memory
from agent.session import Session
from agent.tools import registry


DEFAULT_SYSTEM = "你是简洁的中文助手，回答不超过三句话。"


class AgentLoop:
    """一次会话。self.messages 就是这次会话的全部「记忆」。"""

    def __init__(
        self,
        system_prompt: str = DEFAULT_SYSTEM,
        with_tools: bool = False,
        session: Session | None = None,
        auto_compact: bool = True,
        memory: Memory | None = None,
    ) -> None:
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

        # ── 第 3 周：会话持久化 ──────────────────────────────
        # 有了它，程序重启后能接着上次聊。
        # 注意它**不改变** self.messages 的用法 —— 只是多了一进一出：
        #     每轮结束 flush 一次，启动时 load 一次顶替 self.messages。
        # 记忆的载体始终是那个列表，磁盘只是它的备份。
        self.session = session

        # 「已经落盘到第几条消息了」。load 成功后会被设成 len(restored)，
        # 新建会话时是 1（因为第 0 条 system 由 session.start() 单独写，不走 append）。
        # 详见 flush_session() 里的解释。
        self._flushed = 1 if session is not None else 0

        # ── 第 3 周：上下文裁剪 ──────────────────────────────
        # auto_compact=False 时永远不压缩（调试用：想亲眼看 token 涨到爆就用它）。
        self.auto_compact = auto_compact
        # 摘要函数。**注意它默认是 None，在第一次需要时才造** ——
        # 因为造它要 load_config()（读 .env），而 cli 的单轮模式可能压根不需要它。
        # 这个手法叫「惰性初始化」（lazy init）。
        self._summarizer: Any = None
        # 记录压缩过几次、最近一次省了多少 —— 第 4 周做可观测性时会正式用。
        self.compact_count = 0
        self.last_compact_saving = 0

        # ── 第 3 周：长期记忆 ────────────────────────────────
        # 它和 session 是两个不同的东西：
        #   session  这次对话说了什么（流水账，会被压缩）
        #   memory   关于用户「不变的」事实（档案，永不裁剪）
        self.memory = memory
        # 本轮的长期记忆消息（还没注入时是 None）
        self._memory_message: dict[str, str] | None = None

    def load_memory(self) -> bool:
        """读长期记忆。成功读到内容返回 True。

        为什么不在 __init__ 里直接读？
            为了和 load_session() 保持一致的形状，也让 cli 能决定
            「要不要检查一下 memory 文件是否该创建」。
        """
        if self.memory is None:
            return False
        self._memory_message = self.memory.load()
        return self._memory_message is not None

    def remember(self, line: str) -> None:
        """往长期记忆里追加一条，并让本轮立刻生效。

        为什么写完要顺手更新 self._memory_message？
            否则这一轮问它「你记住了吗」，它还说不知道 —— 因为你只写了磁盘，
            没改当前这次请求要发的东西。写完要立刻反映到现场。
        """
        if self.memory is None:
            return
        self.memory.append(line)
        self._memory_message = self.memory.load()

    def _get_summarizer(self) -> Any:
        """惰性造摘要器。只在真的需要压缩时才会被调到。"""
        if self._summarizer is None:
            self._summarizer = llm.make_summarizer()
        return self._summarizer

    def _view(self) -> list[dict[str, Any]]:
        """返回「本轮要发给模型的视图」——可能比 self.messages 短。

        ⚠️ 这是第 3 周最关键的一个概念区分：

            self.messages  是**全量历史**（真相，永不删减，要落盘）
            self._view()   是**本次请求的视图**（可能是裁剪过的）

        两者从这一刻起就不再是同一个东西了。之前三周它们一直是同一个，
        所以你可以直接 self.messages 发出去。现在不行。

        为什么不让 compact() 直接改 self.messages？
            因为用户可能用 /history 看全部，而且 session 文件里应该存完整历史。
            裁剪是「每次请求临时算一遍」，不是「把数据删了」。

        第 3 周后半段它又多了一件事：**注入长期记忆**。
        注入的位置很关键 —— 见下面 inject_memory 的注释。
        """
        if not self._should_compact():
            return self._inject_memory(self.messages)

        before = context.estimate_tokens(self.messages)
        try:
            view = context.compact(self.messages, self._get_summarizer())
        except Exception as exc:  # noqa: BLE001
            # 压缩失败不能拖垮对话 —— 退化成「不压缩，原样发」。
            # 代价是可能撞窗口，但那总比当场崩掉好。
            # 这是第 2 周 registry.call() 就确立的模式：局部失败不该拖垮全局。
            print(f"[警告] 上下文压缩失败，本轮降级为全量发送：{exc}")
            return self._inject_memory(self.messages)

        after = context.estimate_tokens(view)
        if len(view) < len(self.messages):
            self.compact_count += 1
            self.last_compact_saving = before - after
            print(
                f"[已压缩上下文] {len(self.messages)} 条 → {len(view)} 条，"
                f"约省 {self.last_compact_saving} tokens"
            )
        return self._inject_memory(view)

    def _should_compact(self) -> bool:
        """该不该压缩。抽出来是为了让逻辑读起来清楚：先问「要不要压」，再压。"""
        return self.auto_compact and context.needs_compaction(self.messages)

    def _inject_memory(self, view: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把长期记忆插进视图。

        ⚠️ **插入位置是这一小段的核心。**

        长期记忆是一条 role="system" 的消息，它必须排在**所有对话之前**，
        也就是紧跟原始的 system 提示词：

            正确的（历史顺序不会被割裂）：
                [ 原始 system ]
                [ 长期记忆 system ]   ← 插在这里
                [ 摘要 system ]        ← 压缩产生的
                [ user / assistant ... ]

            错误的（记忆被当成「刚才发生的事」）：
                [ 原始 system ]
                [ 摘要 system ]
                [ user / assistant ... ]
                [ 长期记忆 system ]    ← 塞到最后 → 模型以为这是刚收到的指令

        为什么这么在意顺序？
            因为 messages 列表是**时间正序**的对话记录。模型读它就像读聊天记录，
            越靠后越「新」。长期记忆是背景资料，不是新发生的事，
            所以它必须在最前面。
        """
        if self._memory_message is None:
            return view
        if not view:
            return view

        # 不修改传进来的 view —— 用切片拼新的。
        # 虽然这里 view 通常已经是临时对象了，但保持「不改参数」的习惯，
        # 能避免以后有人把 self.messages 直接传进来时踩雷。
        return [view[0], self._memory_message, *view[1:]]

    # ── 第 3 周：会话持久化（脚手架，已写完整）───────────────
    # 这两个方法是「接线」，不含 Agent 原理，所以直接给全。
    # 你要写的 TODO ① ② 都在 session.py 里。

    def load_session(self) -> bool:
        """从磁盘读回上一次的历史。成功返回 True，没有历史返回 False。

        为什么要有返回值？
            因为「没有旧会话」是**正常情况**（第一次用）而不是错误。
            cli 要靠它决定打不打印「已恢复上次对话」。
        """
        if self.session is None:
            return False
        restored = self.session.messages()
        if not restored:
            return False
        # 直接顶替。list 赋值是「换掉整个列表」，不是合并 ——
        # 这正是我们要的：磁盘上的历史就是全部真相，不要和当前内存里的混在一起。
        self.messages = restored
        # 已经落盘到第几条了。flush_session() 靠它知道该从哪继续写。
        self._flushed = len(restored)
        return True

    def flush_session(self) -> None:
        """把「从上次落盘之后新增的消息」追加进 session 文件。

        为什么要单独一个 flush，而不是每轮都全量重写？
            因为 JSONL 的全部意义就是**只追加**：
                不用重写历史 → 中途崩了最多丢最后一行，前面完好
                全量重写 → 崩一次就毁掉整个会话
            这不是性能考虑，是**数据安全**考虑。

        怎么知道哪些是新的？
            用 self._flushed 记住「上次写到第几条了」——
            这是本项目第一次出现「用一个小计数器跟踪状态」，
            记住这个套路：self.messages 只追加不改写，
            所以「已处理的条数」就等于「下次的起始下标」。
        """
        if self.session is None:
            return
        pending = self.messages[self._flushed:]
        for message in pending:
            self.session.append(message)
        self._flushed = len(self.messages)

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

        # 第 3 周：发出去的是「视图」不是「全量」。
        # 之前三周这两个是同一个东西，现在可能不同了 —— 见 _view() 的注释。
        reply = llm.chat(self._view())

        self.messages.append({"role": "assistant", "content": reply.text})
        self.last_usage = reply.usage
        self.flush_session()   # 第 3 周：这一轮的对话落盘
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
        for piece in llm.stream_chat(self._view()):
            pieces.append(piece)
            print(piece, end="", flush=True)
        print()

        reply_text = "".join(pieces)
        self.messages.append({"role": "assistant", "content": reply_text})
        return reply_text

    def reset(self) -> None:
        """清空对话内容，保留开头的 system。

        第 3 周加了一个问题：reset 之后磁盘上的文件要不要跟着清？
        **不清。** 理由：
            文件是「历史流水账」，已经发生的事不会被 reset 抹掉。
            就像清空聊天窗口不等于删掉聊天记录。
        所以这里只清内存，flush_session() 下次会把 system 之后的新内容
        继续追加到旧文件后面 —— 这是**有意的**：旧记录保留，
        `/resume <id>` 仍然能看到全部历史。
        """
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
            # 每一轮都把**整个** messages 重新发一遍。
            # 和第 1 周是同一件事，只是列表里现在会多出 tool_calls 和 tool 两种消息。
            # ⚠️ 注意这里每轮都重算 _view() —— 因为工具结果刚追加进去，
            #    列表变长了，视图也该跟着变。（压缩判断是幂等的，代价很小）
            reply = llm.chat(self._view(), tools=schemas)

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
                self.messages.append(reply.to_message())
                self.last_usage = reply.usage
                self.flush_session()   # 第 3 周：工具调用的中间消息也要存
                return reply.text

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
            self.messages.append(reply.to_message())

            for tc in reply.tool_calls:
                args = tc.parsed_arguments()
                result = self.tools.call(tc.name, args)
                self.messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": result}
                )

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
        return f"已达到最大工具调用轮数（{max_iterations}），我先停在这里。"
