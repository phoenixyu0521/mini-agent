"""上下文裁剪的单测。

## 这块为什么最该测

裁剪错了不会崩 —— 程序照跑，token 照花，只是模型**答得很怪**。
你可能会怀疑模型不行、怀疑提示词写得差，唯独不会怀疑裁剪。
所以这里每一条结论都必须用断言钉死，不能靠「跑一次看着还行」。

## 两个水位线（回顾）

    KEEP_RECENT = 8    保留区：最近 8 条原样发
    COMPACT_AT  = 12   超过 12 条对话就压

"条数" 不是 "轮数"：一轮普通对话 = 2 条（user + assistant），带工具时更多。
"""

from __future__ import annotations

from agent import context


# ══════════════════════════════════════════════════════════
#  水位线判定
# ══════════════════════════════════════════════════════════


def test_needs_compaction_boundary(make_history):
    """边界要精确，差一条就是「每轮都压」或「永远不压」。

    公式是 `len(messages) - 1 > COMPACT_AT`，减 1 是因为 system 不算对话。
        12 条对话 → 13 条 messages → 12 > 12 为假
        13 条对话 → 14 条 messages → 13 > 12 为真
    """
    assert context.needs_compaction(make_history(12)) is False
    assert context.needs_compaction(make_history(13)) is True


# ══════════════════════════════════════════════════════════
#  裁剪结果的结构
# ══════════════════════════════════════════════════════════


def test_compact_result_size(make_history, fake_summarize):
    """压缩后 = 1 条 system + 1 条摘要 + KEEP_RECENT 条。

    30 条对话是 31 条 messages → 压完应该只剩 10 条。
    这个数字同时验证了两件事：早期被合并了，保留区没被多留。
    """
    view = context.compact(make_history(30), fake_summarize)
    assert len(view) == 1 + 1 + context.KEEP_RECENT


def test_summary_message_is_system(make_history, fake_summarize):
    """摘要是 role="system"，不是 assistant。

    用错 role 的后果很具体：模型会以为自己**说过**摘要里那些话，
    把二手背景当成自己的承诺去执行。
    """
    view = context.compact(make_history(30), fake_summarize)
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

    context.compact(history, fake_summarize)

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
    view = context.compact(make_history(30), fake_summarize)

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
    context.compact(make_history(30), fake_summarize)

    dropped = fake_summarize.calls[0]
    assert len(dropped) == 22
    assert dropped[0]["content"] == "第0条"
    assert dropped[-1]["content"] == "第21条"
