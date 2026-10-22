"""重试（第 6 周 · 可靠性 ③）的测试。

两条主线：
    1. `retry` 模块自己的判断 —— 什么该重试、退避怎么算
    2. 接进 `llm` 之后的行为 —— 5xx 会重试、401 不会、**流已开口就不再重试**

全离线：HTTP 客户端是假的，`time.sleep` 被换成一个「只记录不等待」的函数。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from agent import llm, retry


# ══════════════════════════════════════════════════════════
#  假 HTTP 客户端
# ══════════════════════════════════════════════════════════
class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        body: str = "",
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        lines: list[str] | None = None,
    ) -> None:
        self.status_code = status
        self.text = body
        self._data = data
        self.headers = httpx.Headers(headers or {})
        self._lines = lines or []

    def json(self) -> dict[str, Any]:
        return self._data or {}

    def read(self) -> bytes:
        return self.text.encode("utf-8")

    def iter_lines(self):
        for line in self._lines:
            yield line


class _StreamCtx:
    """冒充 `client.stream(...)` 返回的上下文管理器。"""

    def __init__(self, target: FakeResponse | Exception) -> None:
        self.target = target

    def __enter__(self) -> FakeResponse:
        # 连不上的失败发生在**进入上下文**这一刻，和 httpx 一致
        if isinstance(self.target, Exception):
            raise self.target
        return self.target

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeClient:
    """按脚本依次返回：Response 或 Exception。最后一项会被重复使用。"""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls = 0

    def _next(self) -> Any:
        self.calls += 1
        if len(self.script) > 1:
            return self.script.pop(0)
        return self.script[0]

    def post(self, *args: Any, **kwargs: Any) -> Any:
        step = self._next()
        if isinstance(step, Exception):
            raise step
        return step

    def stream(self, *args: Any, **kwargs: Any) -> _StreamCtx:
        return _StreamCtx(self._next())


CFG: dict[str, Any] = {
    "url": "https://example.invalid/chat/completions",
    "timeout": 5,
    "max_tokens": 8192,
    "LLM_API_KEY": "sk-test",
    "LLM_MODEL_ID": "test-model",
}


def _ok_reply(text: str = "你好") -> FakeResponse:
    return FakeResponse(
        data={
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }
    )


def _sse(*chunks: str) -> list[str]:
    lines = []
    for chunk in chunks:
        payload = {"choices": [{"delta": {"content": chunk}}]}
        lines.append("data: " + json.dumps(payload, ensure_ascii=False))
    lines.append("data: [DONE]")
    return lines


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """把真 sleep 换成记录器 —— 否则这些用例要白等好几秒。"""
    recorded: list[float] = []
    monkeypatch.setattr(retry.time, "sleep", lambda d: recorded.append(d))
    return recorded


# ══════════════════════════════════════════════════════════
#  一、什么该重试
# ══════════════════════════════════════════════════════════
def test_retryable_classification() -> None:
    """5xx / 429 / 超时 / 断连要重试；4xx 一律不重试。

    401 尤其重要：Key 写错了，重试三次只是多等几秒再拿到同一个错误，
    还让日志里多三条、看起来像网络问题。
    """
    assert retry.is_retryable(retry.APIError(503))
    assert retry.is_retryable(retry.APIError(429))
    assert retry.is_retryable(retry.APIError(408))
    assert not retry.is_retryable(retry.APIError(400))
    assert not retry.is_retryable(retry.APIError(401))
    assert not retry.is_retryable(retry.APIError(404))

    assert retry.is_retryable(httpx.ReadTimeout("slow"))
    assert retry.is_retryable(httpx.ConnectError("refused"))
    # 逻辑错误不该被拿去重试
    assert not retry.is_retryable(ValueError("bad json"))
    assert not retry.is_retryable(RuntimeError("别的东西"))


def test_backoff_is_exponential_and_capped() -> None:
    """退避必须指数增长，并且有上限。"""
    policy = retry.RetryPolicy(attempts=5, base_delay=1.0, max_delay=4.0, jitter=0.0)
    assert policy.delay_for(1) == 1.0
    assert policy.delay_for(2) == 2.0
    assert policy.delay_for(3) == 4.0
    assert policy.delay_for(4) == 4.0          # 撞上限
    assert policy.delay_for(9) == 4.0          # 不会爆炸


def test_retry_after_wins_over_backoff() -> None:
    """限流时听对方的，别自己算。"""
    policy = retry.RetryPolicy(attempts=3, base_delay=0.8, max_delay=8.0, jitter=0.0)
    assert policy.delay_for(1, retry_after=5.0) == 5.0
    assert policy.delay_for(1, retry_after=999.0) == 8.0   # 仍受上限约束
    assert policy.delay_for(1, retry_after=0.0) == 0.0


def test_jitter_never_goes_below_backoff() -> None:
    """抖动只往上抖 —— 下限保持理论值。"""
    policy = retry.RetryPolicy(attempts=3, base_delay=1.0, max_delay=8.0, jitter=0.5)
    for _ in range(50):
        delay = policy.delay_for(1)
        assert 1.0 <= delay <= 1.5


# ══════════════════════════════════════════════════════════
#  二、with_retry 本身
# ══════════════════════════════════════════════════════════
def test_with_retry_succeeds_after_transient_failure(sleeps: list[float]) -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise retry.APIError(503)
        return "ok"

    assert retry.with_retry(flaky) == "ok"
    assert calls["n"] == 3
    assert len(sleeps) == 2          # 两次重试各等一次


def test_with_retry_gives_up_and_raises_original(sleeps: list[float]) -> None:
    """次数用完就把**原异常**抛出去，不包一层。

    为什么要原样抛？
        调用方要判断「这是 429 还是 401」，包成 RetryError 就得再拆一层。
    """
    calls = {"n": 0}

    def always_500() -> str:
        calls["n"] += 1
        raise retry.APIError(500)

    with pytest.raises(retry.APIError) as excinfo:
        retry.with_retry(always_500)
    assert excinfo.value.status == 500
    assert calls["n"] == 3
    assert len(sleeps) == 2


def test_with_retry_does_not_retry_client_error(sleeps: list[float]) -> None:
    calls = {"n": 0}

    def unauthorized() -> str:
        calls["n"] += 1
        raise retry.APIError(401)

    with pytest.raises(retry.APIError):
        retry.with_retry(unauthorized)
    assert calls["n"] == 1
    assert sleeps == []


# ══════════════════════════════════════════════════════════
#  三、接进 llm 之后
# ══════════════════════════════════════════════════════════
def test_chat_retries_503(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    client = FakeClient([FakeResponse(503, "busy"), _ok_reply("重试之后成功了")])
    monkeypatch.setattr(llm, "get_client", lambda: client)

    reply = llm.chat([{"role": "user", "content": "hi"}], cfg=CFG)

    assert reply.text == "重试之后成功了"
    assert client.calls == 2
    assert len(sleeps) == 1


def test_chat_does_not_retry_401(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    client = FakeClient([FakeResponse(401, "invalid api key")])
    monkeypatch.setattr(llm, "get_client", lambda: client)

    with pytest.raises(retry.APIError) as excinfo:
        llm.chat([{"role": "user", "content": "hi"}], cfg=CFG)

    assert excinfo.value.status == 401
    assert client.calls == 1
    assert sleeps == []


def test_chat_honours_retry_after(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    client = FakeClient(
        [FakeResponse(429, "slow down", headers={"Retry-After": "3"}), _ok_reply()]
    )
    monkeypatch.setattr(llm, "get_client", lambda: client)

    llm.chat([{"role": "user", "content": "hi"}], cfg=CFG)
    assert sleeps == [3.0]


def test_stream_retries_before_first_chunk(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """还没开口就失败了 → 可以放心重试。"""
    client = FakeClient(
        [httpx.ConnectError("refused"), FakeResponse(lines=_sse("你", "好"))]
    )
    monkeypatch.setattr(llm, "get_client", lambda: client)

    outcome = llm.StreamOutcome()
    pieces = list(llm.stream_chat([{"role": "user", "content": "hi"}], cfg=CFG, outcome=outcome))

    assert "".join(pieces) == "你好"
    assert client.calls == 2
    assert outcome.tool_calls == []


def test_stream_does_not_retry_after_first_chunk(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """已经吐出字之后再断 → **绝不重试**。

    为什么这条是整个文件里最重要的一条？
        重试会让调用方把同一段文字再收一遍 —— 屏幕上出现「你好你好」。
        更糟的是前端按片段追加，用户会以为模型坏了。
        正确做法是把已收到的留下 + 明确报错。
    """

    def broken_lines():
        yield _sse("你")[0]              # 先给出一帧，让「已开口」成立
        raise httpx.ReadError("connection reset")

    client = FakeClient([FakeResponse(lines=broken_lines())])
    monkeypatch.setattr(llm, "get_client", lambda: client)

    received: list[str] = []
    with pytest.raises(httpx.ReadError):
        for piece in llm.stream_chat([{"role": "user", "content": "hi"}], cfg=CFG):
            received.append(piece)

    assert received == ["你"]        # 已收到的保留下来了
    assert client.calls == 1          # 关键断言：没有重试
    assert sleeps == []


def test_stream_does_not_retry_on_401(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    client = FakeClient([FakeResponse(401, "bad key")])
    monkeypatch.setattr(llm, "get_client", lambda: client)

    with pytest.raises(retry.APIError):
        list(llm.stream_chat([{"role": "user", "content": "hi"}], cfg=CFG))

    assert client.calls == 1
    assert sleeps == []
