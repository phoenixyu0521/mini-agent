"""上下文组装与裁剪 —— 第 3 周最难的一块。

## 先算一笔账，你就知道为什么非做不可

从第 1 周开始，`llm.chat(self.messages)` 每一轮都把**整个 messages** 重发一遍。
所以对话越久，每轮越贵。不是线性增长，是**每轮都重新付一遍全部历史**。

假设平均每轮对话产生 100 token，聊到第 50 轮：

    第 1 轮   发 100    第 20 轮  发 2000
    第 5 轮   发 500    第 50 轮  发 5000
    第 10 轮  发 1000   ─────────────────
                        累计发送 = 100 × (1+2+...+50) = 127,500 token

而你实际上「说了 5000 token 的话」。**成本是平方级的。**

## 更要命的是硬上限

模型有上下文窗口（context window），比如 128k token。
它**不是「贵」的问题，是「直接报错」的问题**。撞上去之后：

    HTTP 400: This model's maximum context length is 131072 tokens...

程序当场死。这就是 ROADMAP 第 3 周那句「聊了 200 轮，超出模型窗口怎么办」的真身。

## 核心决策：什么东西必须留，什么东西可以丢

三种处理方式，对着三类的不同内容：

| 内容 | 处理 | 为什么 |
|---|---|---|
| system 提示词 | **永不裁剪** | 它是身份和规矩。丢了模型就变性格 |
| 最近 N 轮对话 | **原样保留** | 用户刚说的话，一个字都不能改，否则答非所问 |
| 更早的对话 | **压成一段摘要** | 细节不再重要，但「聊过什么」这个事实要留着 |

注意第三行不是「删掉」，是**压缩**。这个区别很重要：

    删掉早期对话   → 模型完全忘记「我叫旭、我在做建筑项目」→ 变得像失忆
    压成摘要       → 模型知道「用户叫旭，是做建筑项目的学生」→ 细节丢了但人还在

这就是 ROADMAP 说的「保留最近 N 条 + 把早期内容总结成一段」。

## 为什么摘要要用「另一次模型调用」来做

直觉上你会想用规则来压缩，比如「取每句话前 20 个字」。
但那样出来的东西是**留头去尾的残句**，模型读了一头雾水。

摘要必须是**人话**，而把一大段话变成人话，正是语言模型最擅长的事。
所以要再花一次（便宜的）调用去生成摘要。

这次调用的特点是：输入很大（要压缩的全部历史）、输出很小（几百字摘要）。
**一次性的成本，换来后面每一轮都省。**

## 触发时机：不能每轮都压缩

每轮都压缩的话，每轮都要多付一次摘要调用，反而更贵。
所以要设一个**水位线**（threshold）：

    超过水位线 → 压缩一次 → 掉回水位线以下 → 等下次再涨上来

本项目先做最简单的版本：**按 token 预算**算水位线。
为什么不是按条数？因为条数只是 token 的**代理指标**，两者会严重脱节 ——
实测过两个极端：11 条消息 10 万 token（条数少、体积大，完全不触发），
41 条消息 41 token（条数多、体积小，反而触发）。

模型真正受限的东西是 **token 窗口**，所以判据必须用 token。
估算器见 `estimate_tokens()`，精确的 tokenizer 属于后续优化。

## 这个文件里不出现的东西

**不调用模型。** 摘要函数由调用方传进来（`summarize` 参数）。

为什么？因为 `llm.py` 是唯一与厂商耦合的文件（铁律）。
如果 context.py 直接 `import llm` 去调模型，那它就也耦合了 —— 换厂商要改两个文件。
把「怎么概括」这件事当作**参数**传进来，context.py 就变成纯逻辑，可以单独测试。

这个手法叫**依赖注入**（dependency injection）：**需要别人干活时，别自己去 new，
让别人把工具递给你。** 第 4 周写单测时你会直接受益 —— 传一个假函数进去就能测，
不用真调 API。
"""

from __future__ import annotations

from typing import Any, Callable

# 摘要函数的签名：吃一串消息，吐一段字符串。
# 用 Callable 而不是具体类型，是因为它只是个「约定」——
# 谁的实现都行，只要符合这个形状。
Summarizer = Callable[[list[dict[str, Any]]], str]


# ── 水位线参数 ──────────────────────────────────────────────
# 「最近 N 条」是保留区。注意它包含 user/assistant/tool 各类消息，
# 不是「N 轮」——一轮对话通常产生 2 条（user + assistant），带工具时更多。
#
# KEEP_RECENT=8  ≈ 保留最近 4 轮普通对话。
# 为什么是 4 轮而不是 10 轮？
#   保留区越大，每轮要发的 token 越多 —— 这正是我们要省的东西。
#   而 4 轮足够模型理解「现在在聊什么」。
KEEP_RECENT = 8

# 超过这个预算就触发压缩。
#
# 2026-10-20 改：判据从「条数」换成「token 预算」。
#   旧写法 `len(messages) - 1 > 12` 有个致命缺口：
#   一条 30 万字符的工具结果就能绕过它，直接撞窗口。
#   实测：5 轮共 10 万 token（11 条）不触发压缩；20 轮共 41 token（41 条）反而触发。
#
# 这三个常数是**推导预算**用的，不是拍脑袋的水位线：
#   预算 = 窗口 - 给输出留的额度 - 安全余量
# 为什么三项都要减？
#   - 窗口是「输入 + 输出」共用，不留输出额度，模型会没有空间说话；
#   - 安全余量吸收估算误差（1 token ≈ 2 字符是经验值，中文会偏）。
DEFAULT_CONTEXT_WINDOW = 128_000
DEFAULT_RESERVED_OUTPUT = 8_192
SAFETY_BUFFER_TOKENS = 2_000

# 兼容旧名字：调用方如果有写死的 COMPACT_AT，语义已经变了，这里保留成预算。
COMPACT_AT = DEFAULT_CONTEXT_WINDOW - DEFAULT_RESERVED_OUTPUT - SAFETY_BUFFER_TOKENS


def compact_budget(
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    max_tokens: int = DEFAULT_RESERVED_OUTPUT,
) -> int:
    """算出「历史消息最多能占多少 token」。
    """
    budget = int(context_window) - int(max_tokens) - SAFETY_BUFFER_TOKENS
    return max(4_000, budget)


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """粗略估一下这堆消息值多少 token。

    **这是估算，不是精确计数。** 真正算 token 要用模型自己的 tokenizer
    （tiktoken 之类），那是个额外依赖，而且不同厂商的规则不一样。

    经验值（中英混排）：1 个 token ≈ 2 个字符。
    所以字符数 / 2 就是量级上够用的估计。

    这个函数现在**直接参与裁剪判断**（`needs_compaction` 用它）。
    它还负责打日志 —— 看着数字涨，你才知道成本在涨。

    脚手架，已写好。
    """
    chars = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            chars += len(content)
        # 工具调用消息的「体积」主要在 arguments 里
        for tc in m.get("tool_calls") or []:
            function = tc.get("function") or {}
            chars += len(str(function.get("arguments") or ""))
        chars += len(str(m.get("tool_call_id") or ""))
    return chars // 2


def needs_compaction(
    messages: list[dict[str, Any]],
    budget: int | None = None,
) -> bool:
    """判断要不要压缩 —— **按 token 预算**，不是按条数。

    为什么必须用 token？
        约束模型的是窗口大小（比如 128k token），不是「几条消息」。
        条数只是它的代理指标，两者会脱节：
            11 条 × 4 万字符 ≈ 10 万 token  → 旧判据说「不用压」，然后撞窗口
            41 条 × 2 字符   ≈ 41 token      → 旧判据说「要压」，白花一次摘要钱

    budget=None 时用默认推导值（128k 窗口 / 8k 输出 / 2k 余量）。
    真实调用应该由入口按当前 profile 传进来 —— 换模型时窗口可能不同。
    """
    if budget is None:
        budget = compact_budget()
    return estimate_tokens(messages) > budget


def keep_from_for(
    messages: list[dict[str, Any]],
    keep_recent: int = KEEP_RECENT,
) -> int:
    """算出保留区的起点，**并保证切点不落在工具调用对中间**。

    这是个容易被忽略、但一定会踩的坑：

        assistant(tool_calls=[c1])   ← 声明
        tool(tool_call_id="c1")      ← 结果

    协议要求这两条必须成对出现。如果切点正好落在它们之间，
    保留区的第一条就是一条「没有声明者的 tool 消息」，
    服务商直接报 400（tool_call_id 没有对应的 tool_call）。

    所以切点如果压在 tool 上，就往前挪到它的 assistant 声明之前。
    代价是保留区多一条，不会出错。

    返回 1 表示「整段都得留，压不了」。
    """
    keep_from = len(messages) - keep_recent
    if keep_from <= 1:
        return 1
    # tool 消息永远跟在它的 assistant 声明之后，所以往前挪一步就能配对
    while keep_from > 1 and messages[keep_from].get("role") == "tool":
        keep_from -= 1
    return keep_from


# ─────────────────────────────────────────────────────────
# TODO ① —— 把早期消息压成摘要，返回要注入的那条「摘要消息」
#
#   输入：
#       early —— 要被压缩掉的那些旧消息（list[dict]）
#       summarize —— 一个函数，调用方式就是 summarize(early)，
#                    它会返回一段摘要字符串（调用方负责提供一个真的模型调用，
#                    或者测试时提供一个假的）
#
#   输出：
#       一条**看起来像普通消息**的 dict，形如：
#           {"role": "system", "content": "以下是早先对话的摘要：\n<摘要内容>"}
#
#   要做的三件事：
#
#   1) 先把 early 里的内容拼成**一段文本**，好交给 summarize。
#      为什么不能直接把 early 传进去？——其实能（summarize 的签名收的就是
#      list[dict]）。但这有个坑：early 里可能有 tool_calls / tool 这类
#      结构复杂的消息，直接塞给 summarizer 会很难处理。
#
#      **更稳的做法是在这里先转成人话**，比如：
#          user: 我叫旭
#          assistant: 你好，旭。
#          tool: 2026-10-10 14:21:00
#      每行「角色: 内容」。这样 summarize 只需要处理纯文本。
#
#      爬虫一般的写法（列表推导 + join）：
#          lines = [f"{m.get('role')}: {(m.get('content') or '')}" for m in early]
#          blob = "\n".join(lines)
#
#      ⚠️ 注意：有的消息 content 是 None（比如纯 tool_calls 的 assistant 消息），
#         所以要用 `or ""` 兜底，否则会拼出字符串 "None"。
#
#   2) 调 summarize(early) 拿到摘要文字。
#      等一下 —— 你要传的是 early（原始列表）还是 blob（拼好的文本）？
#      这取决于 Summarizer 的签名：`Callable[[list[dict]], str]`。
#      所以**要传 early**。那第 1 步拼的 blob 怎么用？
#      留着 —— 如果早期消息里结构太乱（tool_calls 之类），
#      你可以在拼 blob 时把它们过滤掉，让摘要更干净。
#      两种都行，先选简单的：直接传 early。
#
#   3) 把摘要包成一条消息返回。**role 用 "system"**，不用 "assistant"。
#      为什么？
#          role="assistant" 意味着「这是模型说过的话」——模型会以为
#          自己真的讲过这些。role="system" 才是「这是给你的背景资料」。
#          用错 role，模型可能把摘要当成自己的承诺去执行。
#
#      内容建议的前缀（帮模型区分「这是二手信息」）：
#          "以下是早先对话的摘要（更早的原始内容已省略）：\n" + 摘要
#
#   动手前先回答：
#   Q：这条摘要消息放在 messages 列表的**什么位置**？
#       （先别急着写 —— TODO ② 会处理这个，但你现在就该想清楚。
#        提示：它代表的是「更早发生的事」，那它应该排在保留区的前面还是后面？）
# ─────────────────────────────────────────────────────────
def summarize_early(
    early: list[dict[str, Any]],
    summarize: Summarizer,
) -> dict[str, Any]:
    lines = []
    for m in early:
        content = m.get("content")
        if isinstance(content, str) and content.strip():
            lines.append(f"{m.get('role')}: {content}")
        for tc in m.get("tool_calls") or []:
            name = (tc.get("function") or {}).get("name")
            lines.append(f"{m.get('role')}: (调用了工具 {name})")

    summary = summarize(early)
    return {
        "role": "system",
        "content": "以下是早先对话的摘要（更早的原始内容已省略）：\n" + summary,
    }


# ─────────────────────────────────────────────────────────
# TODO ② —— 主入口：砍掉旧的、补上摘要、返回新的 messages
#
#   返回值就是下一轮要发给模型的完整列表。形状必须和原来一模一样。
#
#   步骤（顺序很重要，仔细看）：
#
#   1) 如果不需要压缩，**原样返回**。
#      怎么判断？上面 `needs_compaction(messages)` 已经写好了，直接用。
#      ⚠️ 注意「原样返回」不是 return 一个新列表 —— 想清楚返回什么更合适。
#
#   2) 切分。列表要切成三段，这个顺序是本文件的核心：
#
#          [0]        system      ← 永远保留
#          [1 .. k]   早期对话    ← 要压成摘要
#          [k+1 ..]   最近对话    ← 原样保留
#
#      k 怎么算？保留区是最后 KEEP_RECENT 条，所以：
#          keep_from = len(messages) - KEEP_RECENT
#      但要小心一个边界：如果列表很短，keep_from 可能算出 0 或负数，
#      那时候第 2 段就是空的 —— 空的就没什么可压的，直接返回原样。
#
#   3) 组装新列表。**这一步的顺序是考点**：
#
#          [ 老的 system ]
#          [ 摘要消息 ]      ← 注意它排在这里，不是排最后
#          [ 最近的那些消息 ]
#
#      为什么摘要在前？
#          因为时间顺序：摘要是「最早发生的事」，最近的消息是「刚发生的」。
#          对话历史必须是**时间正序**，模型才能理解因果。
#          把摘要塞到最后，模型会以为「哦刚才发生了这些事」，逻辑就反了。
#
#      为什么老的 system 要单独留？
#          摘要消息本身也是 role="system"。如果直接两条 system 挨着，
#          在大多数服务商那里是可以的，但为了清晰，保持一条系统提示 + 一条摘要。
#
#   4) 返回新列表。
#
#   ⚠️ 一个必须注意的点：**这个方法不能修改传进来的 messages。**
#      你在做的是「生成一个用于本次请求的视图」，原始历史（self.messages）
#      要**保持完整** —— 因为用户可能还要 /history 查看全部，
#      而且 session 文件里也要存全量。
#
#      所以别用 messages.pop() / messages.remove() 这类**原地修改**的方法。
#      用切片（messages[a:b]）生成新列表 —— 切片是**复制**，不改原列表。
#
#   动手前先回答：
#   Q：压缩之后，self.messages 里那 20 条还是完整的吗？
#      （答案：是。这就是「裁剪视图」和「删除历史」的区别 ——
#       前者是每次请求临时算一遍，后者是真的丢数据。）
# ─────────────────────────────────────────────────────────
def compact(
    messages: list[dict[str, Any]],
    summarize: Summarizer,
    keep_recent: int = KEEP_RECENT,
    budget: int | None = None,
) -> list[dict[str, Any]]:
    if not needs_compaction(messages, budget):
        return messages

    # ⚠️ 不能用 `len(messages) - keep_recent` —— 切点可能落在
    #    assistant(tool_calls) 和它的 tool 结果之间，那样保留区会以
    #    一条「没有声明者的 tool 消息」开头，服务商直接 400。
    keep_from = keep_from_for(messages, keep_recent)
    if keep_from <= 1:
        return messages

    head = messages[0]
    early = messages[1:keep_from]
    recent = messages[keep_from:]

    summary_message = summarize_early(early, summarize)

    return [head, summary_message, *recent]
