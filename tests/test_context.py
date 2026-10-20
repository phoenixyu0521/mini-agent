"""上下文裁剪的单测。

## 这块为什么最该测

裁剪错了不会崩 —— 程序照跑，token 照花，只是模型**答得很怪**。
你可能会怀疑模型不行、怀疑提示词写得差，唯独不会怀疑裁剪。
所以这里每一条结论都必须用断言钉死，不能靠「跑一次看着还行」。

## 两个水位线（回顾）

    KEEP_RECENT = 8            保留区：最近 8 条原样发（按条数）
    compact_budget(...)        预算：历史最多占多少 token（按 token）

2026-10-20 改：触发判据从「条数」换成「token 预算」。
所以下面测「切分逻辑」的用例都显式传 `budget=FORCE_COMPACT`，
把「要不要压」和「怎么压」解耦 —— 否则改判据会连带改一堆无关测试。
"""

from __future__ import annotations

from agent import context

# 强制压缩用的极小预算：只要不是空列表就一定超。
# 为什么不用 `budget=0`？—— `compact_budget()` 有 4000 的下限，
# 但 `needs_compaction` 直接收 budget，不经过那个下限。用 1 语义更明确。
FORCE_COMPACT = 1


# ══════════════════════════════════════════════════════════
#  水位线判定：现在判的是 token，不是条数
# ══════════════════════════════════════════════════════════


def test_needs_compaction_by_tokens(make_history):
    """⭐ 判据是 token 预算。

    这条用例锁住的是「用真实约束而不是代理指标」：
        30 条短消息（每条 ~4 字符）→ 几十 token → 不压
        少量但很长的消息 → 超预算 → 压

    如果哪天有人把 `needs_compaction` 改回 `len(messages) > N`，
    第二次断言会立刻红 —— 因为 4 条消息不可能触发条数水位线。
    """
    short = make_history(30)
    assert context.estimate_tokens(short) < 1000
    assert context.needs_compaction(short, budget=10_000) is False

    long_msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "x" * 40_000},
        {"role": "assistant", "content": "y" * 40_000},
        {"role": "user", "content": "z" * 40_000},
    ]
    assert context.estimate_tokens(long_msgs) > 50_000
    assert context.needs_compaction(long_msgs, budget=10_000) is True


def test_compact_budget_derivation():
    """预算 = 窗口 - 输出额度 - 安全余量，且不会算出非正数。"""
    assert context.compact_budget(128_000, 8_192) == 128_000 - 8_192 - context.SAFETY_BUFFER_TOKENS
    # 极端配置（窗口比输出还小）不能返回负数，否则「永远超预算」
    assert context.compact_budget(1_000, 8_192) == 4_000


def test_compaction_never_splits_tool_call_pair():
    """⭐ 切点不能落在 assistant(tool_calls) 和它的 tool 结果之间。

    否则保留区的第一条是一条「没有声明者的 tool 消息」——
    服务商直接 400（tool_call_id 找不到对应声明），
    而且报错发生在**下一轮**，很难联想到是裁剪造成的。

    构造：让 tool 消息正好落在 `len - KEEP_RECENT` 的位置上。
    """
    messages = [{"role": "system", "content": "sys"}]
    for i in range(10):
        messages.append({"role": "user", "content": f"u{i}"})
        messages.append({"role": "assistant", "content": f"a{i}"})
    # 现在 21 条。紧接着放一对「声明 + 结果」，让 tool 正好落在倒数第 8 条
    messages.append({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "get_time", "arguments": "{}"}}],
    })
    messages.append({"role": "tool", "tool_call_id": "c1", "content": "12:00"})
    for i in range(7):
        messages.append({"role": "assistant" if i % 2 == 0 else "user",
                         "content": f"t{i}"})

    # 切点原本会压在 tool 上
    assert messages[len(messages) - context.KEEP_RECENT]["role"] == "tool"

    safe = context.keep_from_for(messages)
    assert messages[safe]["role"] != "tool"
    # 往前挪一格，正好落在声明它的 assistant 上
    assert messages[safe].get("tool_calls")
    assert messages[safe + 1]["role"] == "tool"


# ══════════════════════════════════════════════════════════
#  裁剪结果的结构
# ══════════════════════════════════════════════════════════


def test_compact_result_size(make_history, fake_summarize):
    """压缩后 = 1 条 system + 1 条摘要 + KEEP_RECENT 条。

    30 条对话是 31 条 messages → 压完应该只剩 10 条。
    这个数字同时验证了两件事：早期被合并了，保留区没被多留。
    """
    view = context.compact(make_history(30), fake_summarize, budget=FORCE_COMPACT)
    assert len(view) == 1 + 1 + context.KEEP_RECENT


def test_summary_message_is_system(make_history, fake_summarize):
    """摘要是 role="system"，不是 assistant。

    用错 role 的后果很具体：模型会以为自己**说过**摘要里那些话，
    把二手背景当成自己的承诺去执行。
    """
    view = context.compact(make_history(30), fake_summarize, budget=FORCE_COMPACT)
    assert view[0]["role"] == "system"                    # 原 system 保留
    assert view[1]["role"] == "system"                    # 摘要在第 2 位
    assert "摘要" in view[1]["content"]                    # 前缀能区分「二手信息」


def test_compact_does_not_mutate_original(make_history, fake_summarize):
    """⭐ 这条最重要：压缩**不能**改原列表。

    `self.messages` 是全量真相（要 /history 看、要落盘）；
    `compact()` 产出的只是「本次请求的视图」。
    如果这里用了 pop/remove，历史就真的少了 —— 下次再也找不回来。
    """
    history = make_history(30)
    snapshot = [dict(m) for m in history]      # 深一层的浅拷贝，够用（值都是字符串）

    context.compact(history, fake_summarize, budget=FORCE_COMPACT)

    assert len(history) == 31
    assert history == snapshot


def test_short_history_returned_as_is(make_history, fake_summarize):
    """没超水位线时，不压缩、也不调摘要器（调了就是白花钱）。"""
    history = make_history(3)
    assert context.compact(history, fake_summarize) == history
    assert fake_summarize.calls == []          # 一次都没被调用


def test_keep_recent_larger_than_history(make_history, fake_summarize):
    """保留区比总条数还大时，早期段是空的 → 原样返回，不调摘要器。

    这是 `keep_from <= 1` 那个边界的分身：
    水位线参数是**可调**的，调大了不能把程序弄崩。
    """
    history = make_history(20)
    assert context.compact(history, fake_summarize, keep_recent=100) == history
    assert fake_summarize.calls == []


# ══════════════════════════════════════════════════════════
#  TODO —— 两个真正的考点
#
#  现在跑 pytest 会看到这 2 条是红的。预期内。
# ══════════════════════════════════════════════════════════


def test_keeps_the_tail_not_the_head(make_history, fake_summarize):
    """TODO ①：保留的必须是**最后** KEEP_RECENT 条，不是前 N 条。

    30 条对话（内容 "第0条" … "第29条"）压完：
        view[2]  应该是 "第22条"   ← 保留区从第 22 条开始
        view[-1] 应该是 "第29条"   ← 最后一条原样在末尾

    为什么这条比 `len(view) == 10` 值钱？
        条数对了但取错了段（比如保留"第0条"开始的前 8 条），
        长度断言照样绿 —— 模型却会拿着最旧的 8 条回答当前问题。

    提示：断言写具体字符串，别写 `assert len(view) > 2`。
    """
    view = context.compact(make_history(30), fake_summarize, budget=FORCE_COMPACT)

    # 保留区 = 最后 8 条 = "第22条" … "第29条"
    assert view[2]["content"] == "第22条"
    assert view[-1]["content"] == "第29条"


def test_summarizer_gets_exactly_the_dropped_part(make_history, fake_summarize):
    """TODO ②：交给 summarize 的必须是「被砍掉的那段」，不多不少。

    30 条对话时，被砍掉的是 "第0条" … "第21条"，共 22 条。

    提示：fake_summarize.calls 里存着每次调用的入参（conftest.py 里定义的）。
        calls[0]      第一次调用收到的列表
        len(...)      → 22
        [0]["content"]   → "第0条"
        [-1]["content"]  → "第21条"

    想清楚再写：
        如果这里断言的长度是 21 或 23，说明切片的边界差了一条 ——
        那条消息要么被**既保留又摘要**（重复计入 token），
        要么被**既没保留也没摘要**（模型直接失忆）。
    """
    context.compact(make_history(30), fake_summarize, budget=FORCE_COMPACT)

    dropped = fake_summarize.calls[0]
    assert len(dropped) == 22
    assert dropped[0]["content"] == "第0条"
    assert dropped[-1]["content"] == "第21条"
