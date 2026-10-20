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

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from functools import partial
from typing import Any, Iterator

from agent import config, context, llm, mcp, metrics
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
        mcp_servers: dict[str, Any] | None = None,
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
        #
        # 第 4 周补 MCP：本地工具扫完之后，再把「住在别的进程里」的工具接进来。
        # 注意 loop 这一层**完全不知道 MCP 的存在** —— 它只是往同一个
        # ToolRegistry 里多注册几个 Tool。协议细节全在 agent/mcp/ 里。
        if with_tools:
            self.tools = registry.discover()
            mcp.attach(self.tools, mcp_servers or {})
        else:
            self.tools = None

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

        # 裁剪预算（token）。惰性算，第一次问的时候才读 config.json。
        # 为什么不在这里直接读？—— 构造 AgentLoop 不该依赖「配置文件可读」，
        # 单测里要能凭空造一个实例。
        self._context_budget: int | None = None

        # 上一次压缩产生的摘要，以及它覆盖到 messages 的哪一条。
        # 为什么需要缓存？
        #   压缩判据换成 token 后，可能出现「压完仍然超预算」（保留区里有巨型
        #   工具结果）。若不缓存，下一轮会**再花一次摘要请求**去压同一段历史，
        #   每轮都白花一次钱。摘要只依赖被砍掉的那段历史，那段没变就不用重算。
        self._summary: dict[str, Any] | None = None
        self._summary_upto = 0

        # ── 第 3 周：长期记忆 ────────────────────────────────
        # 它和 session 是两个不同的东西：
        #   session  这次对话说了什么（流水账，会被压缩）
        #   memory   关于用户「不变的」事实（档案，永不裁剪）
        self.memory = memory
        # 本轮的长期记忆消息（还没注入时是 None）
        self._memory_message: dict[str, str] | None = None
        # 第 4 周：v0.9 可观测性。造哪台由入口决定（CLI / HTTP），
        # 所以这里默认是「不记」—— 记不记是一种策略，不该硬编码在核心里。
        self.meter: metrics.Meter | None = None

        # ── 第 4 周 v0.10：本轮调了几次工具 ──────────────────
        # 它必须由**内核**给，入口层自己算不出来：
        #   server.py 拿到的是一个**字符串**（回复正文），
        #   字符串里没有「调了几次工具」这个信息。
        # 如果让入口层去数 agent.messages 里 role=="tool" 的条数，
        # 数到的是**这个会话累计**的，不是这一轮的 —— 会越聊越离谱。
        #
        # 归零位置是「一轮对话开始时，只归零一次」（在 stream() 和
        # _tools_loop() 的入口处），不是每次工具调用前 ——
        # 次数要累加，不是要记住最后一次。
        self.last_tool_calls = 0

    # ── 第 4 周：可观测性接线（脚手架，已写好）──────────────

    def _new_meter(self, entry: str) -> metrics.Meter:
        """造这一轮的计时器。

        为什么单独抽一个方法？
            因为 run() / stream() / run_with_tools() 三个入口都要造，
            而「session_id 从哪来」对它们都一样：
                有 session → 用它；没有（单轮模式）→ 用 "-" 占位。
            抽出来就只写一遍。
        """
        sid = self.session.session_id if self.session is not None else "-"
        return metrics.Meter(sid, entry)

    def _close_meter(self, error: str | None = None) -> None:
        """收尾：算总耗时、写盘。出错时把错误类型也记进去。

        为什么错误只记**类型和消息**、不记 traceback？
            因为 traceback 里可能带着文件路径、配置内容。
            而日志是要长期留在磁盘上的 —— 越详细越容易在半年后成为泄露源。
        """
        if self.meter is None:
            return
        self.meter.finish(error)
        self.meter = None

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

    def _compaction_budget(self) -> int:
        """历史消息最多能占多少 token —— 从当前 profile 推导。

        ⚠️ 为什么要 try/except？
            压缩判断每轮都会调用它。如果这里因为「没有 .env / 配置坏了」抛异常，
            一句普通对话都会崩 —— 而配置问题应该在**发请求时**报错，
            不该在「算个数字」的时候把整个循环拦下来。

            降级成默认预算：宁可压得不准，也不能因为算预算而聊不了天。
        """
        if self._context_budget is not None:
            return self._context_budget
        try:
            cfg = config.resolve()
            self._context_budget = context.compact_budget(
                cfg.context_window, cfg.max_tokens
            )
        except Exception:  # noqa: BLE001
            self._context_budget = context.compact_budget()
        return self._context_budget

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

        budget = self._compaction_budget()
        before = context.estimate_tokens(self.messages)

        # 切点要先算出来，它同时是「摘要覆盖到哪」的坐标。
        # ⚠️ 两个地方必须用同一个函数算：这里和 compact() 内部。
        #    自己另写一遍 `len - 8` 会绕过「不许切断工具调用对」的对齐逻辑。
        keep_from = context.keep_from_for(self.messages)

        # 摘要能复用多久？
        #   _summary_upto 是上次摘要覆盖到的位置。只要之后还没积累够
        #   KEEP_RECENT 条新历史，复用旧摘要更划算：
        #   中间这段原样发出去，等攒够一批再花一次摘要请求。
        #
        #   为什么不用「_summary_upto == keep_from」这种精确相等？
        #   因为 keep_from = len(messages) - 8，每聊一轮它就 +2，
        #   精确相等意味着**每轮都要重新摘要一次**，比不缓存还贵。
        #   这正好对应原来「8 → 12 留缓冲」的设计意图：压一次撑 1~2 轮。
        cache_fresh = (
            self._summary is not None
            and self._summary_upto <= keep_from
            and keep_from - self._summary_upto < context.KEEP_RECENT
        )

        if cache_fresh:
            view = [
                self.messages[0],
                self._summary,
                *self.messages[self._summary_upto:],
            ]
        else:
            try:
                view = context.compact(
                    self.messages, self._get_summarizer(), budget=budget
                )
            except Exception as exc:  # noqa: BLE001
                # 压缩失败不能拖垮对话 —— 退化成「不压缩，原样发」。
                # 代价是可能撞窗口，但那总比当场崩掉好。
                # 这是第 2 周 registry.call() 就确立的模式：局部失败不该拖垮全局。
                print(f"[警告] 上下文压缩失败，本轮降级为全量发送：{exc}")
                return self._inject_memory(self.messages)

            # 返回的不是原对象 = 真的压了，第二个位置就是摘要消息
            if view is not self.messages and len(view) > 1:
                self._summary = view[1]
                self._summary_upto = keep_from
                # ⚠️ 计数只在这里 +1（缓存命中不算第二次压缩）。
                #    否则指标会虚高：连续两轮看同一个摘要，日志会报「压了两次」。
                self.compact_count += 1
                after_now = context.estimate_tokens(view)
                self.last_compact_saving = before - after_now
                print(
                    f"[已压缩上下文] {len(self.messages)} 条 → {len(view)} 条，"
                    f"约省 {self.last_compact_saving} tokens"
                )

        after = context.estimate_tokens(view)

        # ⚠️ 压完仍然超预算：说明「保留区自己就太大了」。
        #    这不是压缩的问题，是某一条消息（通常是工具结果）没有体积上限。
        #    这里只能明确告警 —— 静默超发会变成「偶尔一个 HTTP 400」，
        #    那种错最难查，因为它不发生在出错的那一轮。
        if after > budget:
            print(
                f"[警告] 压缩后仍约 {after} tokens，超过预算 {budget}；"
                f"保留区本身过大（单条工具结果未设上限）"
            )

        return self._inject_memory(view)

    def _should_compact(self) -> bool:
        """该不该压缩。抽出来是为了让逻辑读起来清楚：先问「要不要压」，再压。

        判据是 **token 预算**，不是消息条数 —— 见 context.needs_compaction 的注释。
        """
        return self.auto_compact and context.needs_compaction(
            self.messages, self._compaction_budget()
        )

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
        # 换了整段历史 → 摘要缓存失效（它对应的是上一份 messages 的切点）
        self._summary = None
        self._summary_upto = 0
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
        """走完一轮，返回模型回复。

        第 4 周加了可观测性。这里新增的五行都是**脚手架**，直接给：
            self.meter = self._new_meter("chat")   开工
            with self.meter.phase("prepare"): ...  分段计时
            self.meter.count_request(reply.usage) 记一次上游请求
            self._close_meter()                    收尾（必须在 finally 里）

        ⚠️ 唯一需要你想明白的是**为什么 _close_meter 放在 finally**：
            模型请求失败（401、超时）时如果不收尾，这一轮的耗时和错误就丢了 ——
            而**失败的那些轮恰恰是你最想看的**。
            这就是「异常路径也要留痕」：try/finally 里的 finally 就是干这个的。
        """
        self.meter = self._new_meter("chat")
        try:
            self.messages.append({"role": "user", "content": user_text})

            # prepare 段：算视图（可能触发压缩 → 压缩自己也要调一次模型）
            with self.meter.phase("prepare"):
                view = self._view()
                if self.compact_count and "compact_saving" not in self.meter.m.extra:
                    self.meter.note_compress(self.last_compact_saving)

            # model 段：真正等网络的地方
            with self.meter.phase("model"):
                reply = llm.chat(view)
            self.meter.count_request(reply.usage)

            self.messages.append({"role": "assistant", "content": reply.text})
            self.last_usage = reply.usage
            self.flush_session()   # 第 3 周：这一轮的对话落盘
        except Exception as exc:  # noqa: BLE001
            self._close_meter(f"{type(exc).__name__}: {exc}")
            raise
        else:
            self._close_meter()
        return reply.text

    def stream(self, user_text: str, use_tools: bool = False) -> Iterator[dict[str, Any]]:
        """和 run() 做同一件事，区别是「一小段一小段地交出来」而不是一次给完。

        ⭐ 为什么第 4 周才把它从 run_stream() 里拆出来？（这是本周的第一个设计课）
            因为以前只有终端一个使用者 —— 让 run_stream() 自己 print 就够了。
            现在多了 Web：它**不能 print**，它要把片段打包成 SSE 发出去。
            同一个能力，两种展示方式 → 「产出」和「展示」必须分开。

            这就是那句话：**内核提供能力，入口决定怎么显示。**

            拆完之后：
                stream()      只负责「产出」（纯逻辑，不知道自己在哪被用）
                run_stream()  只负责「打印」（终端用）
                将来 Web 版写第三个消费者（打包成 SSE），内核一行不改

        ⚠️ 顺带修掉一个第 3 周的遗漏：
            原来的 run_stream() **从来没调过 flush_session()** ——
            也就是说 `--stream` 模式聊完，对话**根本没落盘**，关掉就没了。
            run() 有这个调用，run_stream() 漏了。
            现在补上（在本方法末尾），CLI 和 Web 两条路都能正确存盘。

        ⭐⭐ v0.10 第 2 步：yield 的东西从「裸字符串」变成了「事件 dict」

            为什么必须变？
                现在一轮里可能出现三种东西：一段文字、一次工具调用、一次工具结果。
                如果还只 yield 字符串，调用方根本分不清
                「这段字是模型说的话」还是「这段字是工具的输出」——
                而这两者在界面上要长得完全不一样。

            **事件是传输格式，不是领域对象**，所以这里用 dict 而不是 dataclass：
                入口层拿到就能 json.dumps 成 SSE 帧，零转换；
                用 dataclass 则每个消费者都要多写一次 asdict()。

            事件格式（就三种，前端按 type 分派）：
                {"type": "text", "text": "你"}
                {"type": "tool", "name": "get_time", "arguments": "{...}",
                 "result": "[错误] ...", "ok": false}
                （"done" 事件不由内核发 —— 那是传输层的结束标记，见 server.py）

        ⭐⭐⭐ 这个方法承载了第 2 步的全部难点：切换

            流式负责「说话」，非流式负责「干活」。
            具体流程：
                1. 用流式发第一轮请求（带上工具说明书）
                2. 如果这一轮没有 tool_calls → 就是一次普通对话，说话结束
                3. 如果**有** tool_calls → 在当前位置切换：
                   把已收到的正文存成 assistant 消息（必须带上 tool_calls 字段），
                   然后转非流式，把工具循环跑完，最终正文再作为 text 事件下发

            前端感知到的仍然是一条连续的流，只是中间多了一张工具卡片。

        ⚠️ 第 4 周加的计时器**只包住模型等待那一段**，
            故意不包 `self._view()`：
            压缩要额外调一次模型，把它算进「流式耗时」会误导 ——
            你会以为打字机本身变慢了，其实是压缩在花时间。
        """
        self.messages.append({"role": "user", "content": user_text})
        self.last_tool_calls = 0   # 一轮开始，计数归零（只归零这一次）

        # 视图单独算（可能触发压缩）。**不计时** —— 见下面的注释。
        view = self._view()

        # 说明书只取一次 —— 它在这一轮里不会变。
        schemas = None
        if use_tools and self.tools is not None:
            schemas = self.tools.schema_list()

        # stream() 自己造计时器（而不是让调用方造）——
        # 因为它是**生成器**：函数体在第一次 next() 时才执行，
        # 如果计时器由调用方在 for 之前造，中间任何异常都会绕过收尾。
        # 生成器里 self-close 更难保证，所以选择「造+收都在里面」。
        self.meter = self._new_meter("stream")
        pieces: list[str] = []
        outcome = llm.StreamOutcome()   # 流结束后，结论会装在这里
        try:
            # 流式的计时段只包住「收到片段」这段时间。
            # ⚠️ 这里**故意不包 self._view()**：
            #     压缩要额外调一次模型，把它算进「流式耗时」会误导 ——
            #     你会以为打字机本身变慢了，其实是压缩在花那几秒。
            #     两件事必须分开归因，否则指标会骗人。
            with self.meter.phase("model"):
                for piece in llm.stream_chat(view, tools=schemas, outcome=outcome):
                    pieces.append(piece)
                    yield {"type": "text", "text": piece}
            self.meter.count_request(outcome.usage)
            self.last_usage = outcome.usage

            # ═══════════════════════════════════════════════════════
            # 切换点：模型要调工具时，从这里转到非流式干活
            #
            #   三步，缺一不可：
            #
            #     1) 把这轮已收到的东西包装成一条 Reply —— 关键是 text 也要带上。
            #        ⚠️ 为什么不能只把 tool_calls 传下去、把 text 丢掉？
            #           实测过 tool_calls 和 content 可以同时存在 ——
            #           模型可能说的是「我来帮你查一下」，然后才调工具。
            #           丢掉它，模型下一轮会以为自己没说过那句话。
            #
            #     2) 跑工具循环，把产出的事件**原样转发**出去（前端靠它画卡片）。
            #        必须 for + yield，不能只调用 —— 它是生成器，
            #        **不迭代就一行都不会执行，而且不会报错**。
            #
            #     3) 最终正文也作为 text 事件交出去。
            #        用户真正想看的答案在这一段，前面那段只是「我去查一下」。
            #
            #   ⚠️⚠️ 这个 if 里**绝对不要**再 append reply.to_message()。
            #       保存那条带 tool_calls 的消息是 _run_tool_rounds 的活（它内部会做）。
            #       在这里再存一次，模型会看到两条一模一样的调用声明 ——
            #       轻则重复浪费 token，重则服务商直接报「tool_calls 消息格式错误」。
            #       （写参考实现时正是踩了这一脚：落盘多出一条空 assistant）
            # ═══════════════════════════════════════════════════════
            if outcome.tool_calls:
                reply = llm.Reply(text="".join(pieces), usage=outcome.usage,
                                  tool_calls=outcome.tool_calls)
                sink: dict[str, Any] = {}
                for event in self._run_tool_rounds(reply, schemas, 5, sink):
                    yield event
                yield {"type": "text", "text": sink["text"]}

            else:
                # ── 模型没要工具 —— 和 v0.8 完全一样 ──
                # 这就是第 3 周「碎片收集 + 拼完整再存」那段代码挪了个位置。
                #
                # 必须拼成**一整条**消息再存：下一轮会把 messages 里每一项重新发一遍，
                # 每一项都必须是一条完整消息 —— 存 100 个碎片进去，格式就坏了。
                self.messages.append(
                    {"role": "assistant", "content": "".join(pieces)}
                )

            self.flush_session()   # ← 第 3 周漏掉的那一行
        except Exception as exc:  # noqa: BLE001
            self._close_meter(f"{type(exc).__name__}: {exc}")
            raise
        else:
            self._close_meter()

    def run_stream(self, user_text: str, use_tools: bool = False) -> str:
        """终端用：消费 stream()，把事件显示成人能看的样子。

        注意它自己**不含任何逻辑** —— 只是把 stream() 产出的事件显示出来。
        这正是拆分的好处：Web 版只要写另一个消费者（打包成 SSE），内核一行不改。

        v0.10 第 2 步之后，它要处理的不只是文字了：
        工具调用在终端里用一行 `[调工具] get_time` 提示即可 ——
        **同一个事件，不同入口有不同的展示方式**，这就是分开的理由。
        """
        pieces: list[str] = []
        for event in self.stream(user_text, use_tools=use_tools):
            if event["type"] == "text":
                pieces.append(event["text"])
                print(event["text"], end="", flush=True)
            elif event["type"] == "tool":
                # 终端不适合画卡片，一行摘要足够
                flag = "" if event.get("ok", True) else " ✗"
                print(f"\n  · 调工具 {event['name']}{flag}", flush=True)
        print()
        return "".join(pieces)

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
        # 摘要缓存必须跟着失效 —— 它绑定的是「被砍掉的那段历史」，
        # 历史都清空了还留着摘要，下次压缩会拼出一条「摘要 + 全新对话」的怪视图。
        self._summary = None
        self._summary_upto = 0

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

        # 第 4 周：这一轮是「多步」的，所以它的账比run() 复杂得多 ——
        # requests 会是2、3、4…（每调一次工具就多一次模型往返）。
        # 这正是要观测的数字：**一次用户请求内部到底打了几次模型**。
        self.meter = self._new_meter("tools")
        try:
            return self._tools_loop(user_text, max_iterations)
        except Exception as exc:  # noqa: BLE001
            self._close_meter(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._close_meter()

    def _tools_loop(self, user_text: str, max_iterations: int) -> str:
        """工具循环的入口（非流式路径）。计时在 run_with_tools() 里管，这里只管逻辑。

        ⭐ v0.10 第 2 步把它拆成了三块：
            _tools_loop         —— 追加用户消息、打第一次请求，然后交给下面
            _request_with_tools —— 「打一次带工具的请求 + 计时 + 记 token 账」
            _run_tool_rounds    —— 工具循环本体，**流式和非流式共用同一份**

        为什么非要共用？
            因为流式那条路在检测到 tool_calls 之后，要跑的是**一模一样**的循环。
            各写一份的话，两边迟早走偏 ——
            「改了一边忘了另一边」是真实项目里最常见的 bug 来源之一。

        ⭐ 顺带修掉一个计数 bug：
            原来的 `self.last_tool_calls = 0` 写在「模型要调工具」那个分支**里面**，
            也就是 for 循环每转一圈都归零一次。
            一次对话调两轮工具（第 1 轮 2 个、第 2 轮 1 个），结果只会报 1 而不是 3。
            正确的位置是「一轮对话开始时归零**一次**」——
            归零是「清空本轮」，不是「清空这次工具调用」。
        """
        # 第 1 件事和第 1 周完全一样：把用户这句话追加进去。
        self.messages.append({"role": "user", "content": user_text})

        # 说明书只取一次 —— 它在这一轮里不会变。
        schemas = self.tools.schema_list()

        # 一轮开始，计数归零。**只归零这一次**，后面的累加都发生在 _run_tool_rounds 里。
        self.last_tool_calls = 0

        reply = self._request_with_tools(schemas)

        # ⚠️ _run_tool_rounds 是**生成器**：不迭代就一行都不会执行，而且不报错。
        #    非流式这条路不需要事件，所以空跑一遍把它跑完 ——
        #    结果通过 sink 带出来（为什么不用 return 值，见 _run_tool_rounds 的注释）。
        sink: dict[str, Any] = {}
        for _event in self._run_tool_rounds(reply, schemas, max_iterations, sink):
            pass
        return sink.get("text", "")

    def _request_with_tools(self, schemas: list[dict[str, Any]]) -> llm.Reply:
        """打一次「带工具说明书」的请求，顺带计时 + 记 token 账。

        抽出来的理由很实际：一次工具轮里这个动作会重复 N 次，
        而计时/记账的写法每次都必须一模一样 ——
        复制粘贴五遍，迟早漏掉一处 `count_request`，然后指标就开始骗人。
        """
        with self.meter.phase("model") if self.meter else nullcontext():
            reply = llm.chat(self._view(), tools=schemas)
        if self.meter is not None:
            self.meter.count_request(reply.usage)
        return reply

    def _execute_tool_calls(self, tool_calls: list[llm.ToolCall]) -> list[str]:
        """执行一批工具调用，返回**与入参同序**的结果字符串列表。

        ⚠️ 为什么返回值必须同序，不能按「谁先跑完」收集？
            模型是按下标顺序想事的：「先建目录，再写文件」。
            它的下一条回复要靠 tool 消息的顺序来对应自己的意图。
            顺序一乱，模型看到的结果就像被洗过牌 —— 它可能把 A 的结果
            当成 B 的，然后基于错误前提继续推理。**不会报错，只会答得很怪。**

        ⭐⭐ 这一节的核心问题：并行。

        但先看清一个前提，否则会白改一通：

            现在所有工具都是 `def run()` —— **同步阻塞**函数
            （subprocess.run、open、time 计算）。

            对同步函数，`async def` + `asyncio.gather` **不会**让它并行：
            函数体里没有 await 点，gather 只是把它们一个接一个地调用完。
            更糟的是，把阻塞函数塞进事件循环会把它整个卡住 ——
            并发的目的是「别互相等」，结果变成了「全都等这一个」。

            所以这里用**线程池**：每个工具在自己的线程里阻塞，互不等待。
            （想真正用 async，得把工具改成 `await asyncio.create_subprocess_shell`
             这类真异步 I/O —— 那是另一件事，收益和代价都要重新算。）

        ⭐ 第三个问题：不是所有工具都能并发。
            `Tool.concurrency_safe` 默认是 False，只有显式声明过的才进池子。
            判断标准是「有没有副作用、依不依赖顺序」：
                read_file 读两个不同文件 → 随便并发
                run_shell  可能写文件、可能「先 A 后 B」 → 必须串行
        """
        if not tool_calls:
            return []

        # 只有「全部都是只读工具」才并行。
        #
        # 为什么是「全部」而不是「把安全的挑出来并行、不安全的排后面」？
        #   因为后者会**改变模型的意图顺序**。模型说「先跑命令，再读结果文件」，
        #   你把 read_file 提到前面并行，读到的就是旧内容。
        #   宁可整批退化成串行 —— 慢是可见的，顺序错是不可见的。
        all_safe = all(
            getattr(self.tools.get(tc.name), "concurrency_safe", False)
            for tc in tool_calls
        )

        # 工具执行单独计时：它是**本地**耗时，和模型等待（秒级）差几个数量级。
        # 混在一起平均就看不出问题了。整批套一层，量到的就是这批的真实墙钟时间。
        with self.meter.phase("tool") if self.meter else nullcontext():
            if len(tool_calls) == 1 or not all_safe:
                # 串行：只有一个工具，或有副作用，没有并行的空间/资格
                return [
                    self.tools.call(tc.name, tc.parsed_arguments())
                    for tc in tool_calls
                ]

            # ── 并行分支：这批全是只读工具，可以同时跑 ──────────────
            #
            # 三个问题的答案：
            #
            #   Q1 max_workers 填多少？
            #      就填这批的条数（len(jobs)）。一轮能点几个工具受 max_iterations
            #      约束，本来就不多；开等量线程刚好够用。
            #      写死 8 只是让空闲线程白占内存 —— 线程的代价不是「慢」，是内存。
            #
            #   Q2 怎么「并行跑、按原顺序拿回」？
            #      `pool.map()` 的返回值**天然按输入顺序**给出，和谁先跑完无关。
            #      它只吃一个位置参数，所以先用 partial 把 (name, args) 绑成
            #      「待调用」，再用 lambda 把它调起来。
            #
            #   Q3 某个工具抛异常会拖垮同批吗？
            #      不会。`self.tools.call()` 已经把一切异常吞成 "[错误] ..."
            #      字符串了，线程里根本不会冒出异常。
            #      ⚠️ 这个安全性来自 call()，**不是来自线程池** ——
            #         哪天有人绕过 call() 直接 submit(tool.run)，一个异常就躺在
            #         future 里，等你 .result() 时才炸。
            #
            # `with` 退出时会等所有线程结束，所以 return 写在 with 里面是安全的：
            # 能返回，就说明结果已经收齐了。
            jobs = [
                partial(self.tools.call, tc.name, tc.parsed_arguments())
                for tc in tool_calls
            ]
            with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                return list(pool.map(lambda job: job(), jobs))

    def _run_tool_rounds(
        self,
        reply: llm.Reply,
        schemas: list[dict[str, Any]],
        max_iterations: int,
        sink: dict[str, Any],
    ) -> Iterator[dict[str, Any]]:
        """工具循环本体。**流式和非流式共用**。

        为什么是生成器？因为流式那条路需要在工具执行完的**那一刻**
        立刻把「调了哪个工具、结果是什么」推给前端 ——
        不能等整轮跑完再一起给，那样用户会盯着空白屏幕好几秒。

        为什么结果走 sink 而不是 return？
            生成器的 return 值被 StopIteration 包着，两个调用方都得写 try/except
            才拿得到。用一个「调用方自己造的字典」简单得多 ——
            和 llm.StreamOutcome 是同一个套路。

        参数 reply 是**已经拿到手的那条**模型回复：
            流式路径  —— 传「流式收完拼出来的那条」（可能已经带了 tool_calls）
            非流式路径 —— 传「chat() 返回的那条」
        """
        for round_no in range(1, max_iterations + 1):
            # ─────────────────────────────────────────────────────
            # 它没要求调工具，说明这轮聊完了
            # ─────────────────────────────────────────────────────
            if not reply.wants_tools:
                self.messages.append(reply.to_message())
                self.last_usage = reply.usage
                self.flush_session()   # 第 3 周：工具调用的中间消息也要存
                sink["text"] = reply.text
                return

            # ─────────────────────────────────────────────────────
            # 它要求调工具，你替它办事
            #
            # ⚠️ 顺序是硬要求：这条 assistant 必须排在下面 role="tool" 的**前面**。
            #    协议规定「先声明要调，再给结果」，反了服务商直接报错。
            # ─────────────────────────────────────────────────────
            self.messages.append(reply.to_message())

            # 执行（可能并行）。返回的结果**与入参同序**，所以下面能直接 zip。
            results = self._execute_tool_calls(reply.tool_calls)

            for tc, result in zip(reply.tool_calls, results):
                if self.meter is not None:
                    self.meter.m.tool_calls += 1
                self.last_tool_calls += 1

                # 追加第四条 role 的消息。它的作用只有一个：
                # 告诉模型「这是你刚才点名的那个函数的结果」，靠 tool_call_id 对号。
                self.messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": result}
                )

                # 把「刚才发生了什么」交出去。
                # ok 的判据直接复用工具层的约定：registry.call() 出错时返回
                # "[错误] ..." 而不是抛异常 —— 所以看前缀就够了，
                # 不需要在这里另造一套错误码。
                yield {
                    "type": "tool",
                    "name": tc.name,
                    "arguments": tc.arguments,
                    "result": result,
                    "ok": not result.startswith("[错误]"),
                }

            # 这里**不要 return** —— 让循环进入下一轮，
            # 把已经变长的 messages 再发一次。下一轮模型看到工具结果，才会说出人话。
            reply = self._request_with_tools(schemas)

        # ─────────────────────────────────────────────────────────
        # 循环跑满 max_iterations 还没结束
        #
        # 走到这里说明模型停不下来：要么一直在调工具，要么工具给的结果它一直不满意。
        # 这就是 ROADMAP 里「为什么必须有 max_iterations」那个问题的答案 ——
        # 没有刹车，它会一直调，而每次调用都是两次请求，**一直在烧钱**。
        # ─────────────────────────────────────────────────────────
        sink["text"] = f"已达到最大工具调用轮数（{max_iterations}），我先停在这里。"
