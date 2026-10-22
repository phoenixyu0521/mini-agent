"""撞上 max_iterations 之后的收尾（第 6 周 · 可靠性 ②）。

之前的行为：跑满 5 轮就 `return "已达到最大工具调用轮数（5），我先停在这里。"`
问题不是文案难看，是 **messages 里已经躺着 5 条工具结果，用户一个字都看不到**。

现在的行为：再发一次**不带 tools** 的请求，逼它把已经拿到的东西说出来。

这些测试全离线：`llm.chat` 被换成假函数，不联网、不花钱。
"""

from __future__ import annotations

from typing import Any

import pytest

from agent import llm
from agent.loop import AgentLoop
from agent.tools import registry
from agent.tools.base import Tool


class EchoTool(Tool):
    name = "echo"
    description = "把输入原样回显（测试用）"
    concurrency_safe = True

    def run(self, text: str = "hi", **_: Any) -> str:
        return f"echo:{text}"


def _build_loop() -> AgentLoop:
    loop = AgentLoop(with_tools=False, auto_compact=False)
    loop.tools = registry.ToolRegistry()
    loop.tools.register(EchoTool())
    return loop


class FakeChat:
    """冒充 llm.chat。

    行为：前 `rounds` 次都「要求调工具」，之后返回一段正文。
    这正是「模型停不下来」的形状 —— 而且把每一次调用的
    `tools` 参数都记下来，因为「最后一次有没有带 tools」正是本节的验收点。

    ⚠️ `rounds` 必须给 `max_iterations + 1` 才能真的撞上上限。
       因为一次请求总数是「循环外 1 次首请求 + 循环内 max_iterations 轮」——
       这一点本身就是个发现：**`max_iterations=5` 实际最多打 6 次模型**。
    """

    def __init__(self, rounds: int, final_text: str = "查到两件事：A 和 B。") -> None:
        self.rounds = rounds
        self.final_text = final_text
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **_: Any,
    ) -> llm.Reply:
        # 存副本：调用方后面会改 messages，存引用会看到「未来」的内容
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        if len(self.calls) <= self.rounds:
            index = len(self.calls)
            return llm.Reply(
                text="",
                usage={},
                tool_calls=[
                    llm.ToolCall(id=f"c{index}", name="echo", arguments='{"text":"x"}')
                ],
            )
        return llm.Reply(text=self.final_text, usage={})


def _exhausted(max_iterations: int) -> FakeChat:
    """造一个「一路调到上限」的假模型。"""
    return FakeChat(rounds=max_iterations + 1)


def test_final_request_is_sent_without_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """撞上限后必须再发一次请求，而且这次**不带 tools**。

    为什么「不带」是核心？因为带了它还会继续点工具 ——
    那正是它刚被拦下的行为。去掉这个选项，它只剩说话一条路。
    """
    fake = _exhausted(3)
    monkeypatch.setattr(llm, "chat", fake)

    answer = _build_loop().run_with_tools("帮我查点东西", max_iterations=3)

    assert fake.final_text in answer
    # 1 次首请求 + 3 轮 = 4 次带工具，再加 1 次收尾
    assert len(fake.calls) == 5
    assert all(c["tools"] for c in fake.calls[:-1])
    assert fake.calls[-1]["tools"] is None


def test_tool_results_are_still_visible_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """收尾请求能看到前面几轮的工具结果。

    如果这一条不成立，整个改动就没有意义 ——
    模型看不到结果，它只能凭空编一段总结，比固定文案更糟。
    """
    fake = _exhausted(2)
    monkeypatch.setattr(llm, "chat", fake)

    _build_loop().run_with_tools("查", max_iterations=2)

    final_messages = fake.calls[-1]["messages"]
    tool_msgs = [m for m in final_messages if m.get("role") == "tool"]
    # 只该有 round 数那么多条：最后一轮末尾拿到的那个 tool_call
    # **还没被执行就撞上刹车了**，它连消息都没进 —— 所以历史里不会有
    # 「声明了调用却没有结果」的半截记录（这一点很关键，否则收尾请求会 400）。
    assert len(tool_msgs) == 2
    assert tool_msgs[0]["content"] == "echo:x"
    # 收尾那次请求真的发出去了（否则上面看到的是倒数第二次调用的快照）
    assert len(fake.calls) == 4


def test_pending_answer_is_reused_instead_of_asking_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """循环结束时手里的那条回复已经答完了 —— 不该再花钱问一遍。

    这个漏洞是写测试时**数请求次数**才发现的：
    `FakeChat(rounds=max_iterations)` 时，循环内最后一次请求返回的是正文
    （模型刚好在一轮末尾收住了），而旧代码会把这条正文丢掉，
    再发一次请求 —— 白花一次模型调用，还多等好几秒。

    光看「答案对不对」是看不出来的（答案一样）；只有数请求次数才看得见。
    """
    fake = FakeChat(rounds=1)          # call1 工具，round1 末尾 call2 给正文
    monkeypatch.setattr(llm, "chat", fake)

    loop = _build_loop()
    answer = loop.run_with_tools("查", max_iterations=1)

    assert len(fake.calls) == 2        # 没有第三次请求
    assert fake.final_text in answer
    assert "达到上限" in answer
    assert loop.messages[-1] == {"role": "assistant", "content": fake.final_text}


def test_final_answer_is_stored_without_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """收尾那段回答要进历史，但**不能带 tool_calls**。

    为什么专门测这个？
        万一模型在没有 tools 的情况下仍然吐了 tool_calls（实测见过），
        存进去就是一条「声明了调用、却永远等不到结果」的消息，
        下一轮请求会因为 tool_call_id 配不上直接 400。
        这个雷埋在历史里，要到下一次对话才炸，最难查。
    """
    fake = _exhausted(1)
    monkeypatch.setattr(llm, "chat", fake)

    loop = _build_loop()
    loop.run_with_tools("查", max_iterations=1)

    last = loop.messages[-1]
    assert last["role"] == "assistant"
    assert last["content"] == fake.final_text      # 存的是干净正文，不带那句「说明」
    assert "tool_calls" not in last


def test_user_is_told_it_was_cut_short(monkeypatch: pytest.MonkeyPatch) -> None:
    """返回给用户的文本要说明「这是提前收尾的」。

    为什么不能静默补一句就完事？
        用户会以为任务真的做完了。工具跑了几轮是真花了钱的，
        这件事该让他知道，否则他会把「被刹车」当成「模型能力不行」。
    """
    fake = _exhausted(2)
    monkeypatch.setattr(llm, "chat", fake)

    answer = _build_loop().run_with_tools("查", max_iterations=2)
    assert "达到上限" in answer
    assert "提前收尾" in answer


def test_final_request_failure_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """收尾请求自己失败了，要退回固定文案，不能把整轮结果一起葬掉。

    收尾是补救措施；补救失败只该降级，不该二次伤害。
    这里只在「收尾那次」（即没有 tools 参数的那次）抛错 ——
    如果连工具轮都抛，那本来就该整轮失败，不是本节要测的东西。
    """
    class BoomChat(FakeChat):
        def __call__(self, messages, tools=None, **kwargs):  # type: ignore[override]
            if tools is None:
                raise RuntimeError("network down")
            return super().__call__(messages, tools, **kwargs)

    fake = BoomChat(rounds=2)
    monkeypatch.setattr(llm, "chat", fake)

    answer = _build_loop().run_with_tools("查", max_iterations=1)
    assert "已达到最大工具调用轮数（1）" in answer
    assert "RuntimeError" in answer


class TailChat(FakeChat):
    """前 `rounds` 次要求调工具，之后按 `tails` 依次返回。

    用来模拟「收尾请求会返回什么」——这是本节所有分叉的入口。
    """

    def __init__(self, rounds: int, tails: list[llm.Reply]) -> None:
        super().__init__(rounds)
        self.tails = tails

    def __call__(self, messages, tools=None, **_):  # type: ignore[override]
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        if len(self.calls) <= self.rounds:
            index = len(self.calls)
            return llm.Reply(
                text="",
                usage={},
                tool_calls=[
                    llm.ToolCall(id=f"c{index}", name="echo", arguments='{"text":"x"}')
                ],
            )
        index = min(len(self.calls) - self.rounds - 1, len(self.tails) - 1)
        return self.tails[index]


def _text(value: str, with_calls: bool = False) -> llm.Reply:
    calls = (
        [llm.ToolCall(id="zombie", name="read_file", arguments="{}")]
        if with_calls
        else []
    )
    return llm.Reply(text=value, usage={}, tool_calls=calls)


def test_final_reply_with_tool_calls_still_uses_its_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """收尾请求**仍然吐了 tool_calls** 时，只要它有正文，就该用那句正文。

    这是 2026-10-22 真机实测推翻的假设：
        我以为「请求里不传 tools，模型就只能说话」。
        实际它照样吐了 4 个 `read_file` 调用 —— 因为历史里全是这个模式。

    那些调用不会被执行（我们根本没把 tools 发给它，也没打算执行），
    但**正文是它自己写的答案**，丢掉就白丢了。
    """
    fake = TailChat(rounds=2, tails=[_text("名字是 mini-agent。", with_calls=True)])
    monkeypatch.setattr(llm, "chat", fake)

    loop = _build_loop()
    answer = loop.run_with_tools("查", max_iterations=1)

    assert "名字是 mini-agent。" in answer
    assert len(fake.calls) == 3          # 没有再重试
    # 存进历史的那条必须干净：带 tool_calls 就会变成「永远等不到结果」的悬空调用
    assert loop.messages[-1] == {"role": "assistant", "content": "名字是 mini-agent。"}


def test_second_attempt_adds_nudge(monkeypatch: pytest.MonkeyPatch) -> None:
    """正文空 → 加一句「不要再请求工具」再问一次。

    为什么需要这一步？因为「不传 tools」只是必要条件，不是充分条件。
    光靠工具清单缺席，模型不会意识到自己该收手。
    """
    fake = TailChat(
        rounds=2,
        tails=[_text("", with_calls=True), _text("两件事：A 和 B。")],
    )
    monkeypatch.setattr(llm, "chat", fake)

    loop = _build_loop()
    answer = loop.run_with_tools("查", max_iterations=1)

    assert "两件事：A 和 B。" in answer
    assert len(fake.calls) == 4
    # 劝告只出现在第二次收尾请求里，而且是**视图副本**，不进历史
    assert "不要再请求任何工具" in fake.calls[-1]["messages"][-1]["content"]
    assert all("不要再请求任何工具" not in str(m) for m in loop.messages)


def test_digest_fallback_when_all_attempts_are_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两次收尾都是空正文 → 直接把工具结果摊给用户。

    为什么不返回「模型没有给出总结」？
        用户这一轮已经付了 3 次模型调用的钱，手上握着真实的工具结果。
        告诉他「没总结」等于让他空手而归。
        失败时给**次优结果**，不给空结果。
    """
    fake = TailChat(rounds=2, tails=[_text("", with_calls=True)])
    monkeypatch.setattr(llm, "chat", fake)

    answer = _build_loop().run_with_tools("查", max_iterations=1)

    assert "模型没能给出总结" in answer
    assert "echo:x" in answer          # 工具结果真的被摊出来了
    assert "以下是最后" in answer
