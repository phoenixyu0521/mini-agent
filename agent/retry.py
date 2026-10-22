"""重试 —— 一次失败值不值得再试一次。

## 这个文件存在的原因

在它出现之前，`llm.chat()` 的逻辑是「发一次，不是 200 就抛」。
于是一个 503、一次 800ms 的连接抖动，整轮对话就废了 ——
而这类失败**重发一次大概率就好了**。

真实世界里，一次用户请求背后可能是 3~6 次模型往返（工具轮一多就这样）。
每一次都是独立的一次「可能抖动」，所以整轮的失败率是**乘起来**的：

    单次失败率 1% → 6 次请求的整轮成功率只有 0.99^6 ≈ 94%
    单次失败率 5% → 0.95^6 ≈ 74%

也就是说「偶尔抖一下」在 Agent 里会被放大成「经常失败」。
这就是为什么可靠性四件套里必须有它。

## 什么该重试，什么不该

这不是「出错就重试」，那会更糟：

    值得重试   5xx（服务端临时故障）、429（限流）、408、超时、连接断开
    不该重试   400/401/403/404/422 —— 请求本身就是错的，重发一百次也一样错

为什么 401 尤其不能重试？
    它通常意味着 Key 过期/写错。重试三次只是让你多等几秒，
    然后拿到同一个错误 —— 而且日志里会多出三条，让人以为是网络问题。
    **把「不可恢复」误判成「可恢复」，比不重试更浪费时间。**

## 为什么指数退避 + 抖动

    固定间隔重试：如果服务端正忙，所有客户端会**同时**再打一次 —— 雪崩。
    指数退避：0.8s → 1.6s → 3.2s，给对方喘气的机会。
    抖动：同一时刻失败的一百个客户端，等待时间各不相同，不会形成新的尖峰。

## 为什么 429 要看 Retry-After

限流响应通常会带 `Retry-After: 5`。这时候自己算 0.8s 是没用的 ——
对方明确告诉你「5 秒后再来」，太早去只会再吃一个 429。
"""

from __future__ import annotations

import os
import random
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

import httpx

T = TypeVar("T")

# 只重试这些状态码。
#
# 为什么用**白名单**而不是「5xx + 几个特例」的黑名单写法？
#   4xx 里哪些是临时的（408 请求超时、425 太早、429 限流），
#   这个判断只能靠列举。漏掉一个 4xx 的代价是「重试了一个注定失败的错误」，
#   而漏掉一个 5xx 的代价是「明明能救的请求白死了」——
#   两边都疼，但白名单让「默认不重试」成为默认姿势，符合 fail-closed。
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

ENV_ATTEMPTS = "MINI_AGENT_RETRY_ATTEMPTS"


class APIError(RuntimeError):
    """接口返回了非 200。

    为什么不用 `RuntimeError` 了？
        因为重试需要**看状态码**决定要不要再试，而 `RuntimeError` 只有一个字符串。
        从字符串里正则抠 "HTTP 503" 也能做，但那是在解析自己刚拼出来的文案 ——
        改一次报错格式，重试逻辑就悄悄失效了。
        **结构化信息就该用结构化字段传。**

    为什么仍然继承 RuntimeError？
        因为以前所有调用方都在 `except RuntimeError`。改成新类型不算破坏兼容，
        但继承它能保证「老的兜底逻辑」继续生效 —— 升级时不该让别处的 try/except 失效。
    """

    def __init__(
        self,
        status: int,
        body: str = "",
        retry_after: float | None = None,
        url: str = "",
    ) -> None:
        super().__init__(f"请求失败 HTTP {status}\n{body}")
        self.status = status
        self.body = body
        self.retry_after = retry_after
        self.url = url


@dataclass(frozen=True)
class RetryPolicy:
    """重试策略。三个数字，每个都有理由。

    attempts    总尝试次数（含第一次）。3 次 = 1 次首发 + 2 次重试。
                为什么不是 5 次？—— 每次重试都要等，用户能忍的是「几秒」，
                不是「半分钟」。真连续三次 5xx，多半不是抖动，是对方挂了。
    base_delay  第一次重试前等多久。0.8s 是经验值：
                比一次网络往返长，又不至于让人明显感到「卡住了」。
    max_delay   单次等待的上限。没有上限的话，attempts 一多就是指数爆炸。
    """

    attempts: int = 3
    base_delay: float = 0.8
    max_delay: float = 8.0
    jitter: float = 0.25

    def delay_for(self, attempt: int, retry_after: float | None = None) -> float:
        """第 `attempt` 次失败之后该等多久（attempt 从 1 开始）。

        注意参数是「已经失败了几次」，不是「第几次尝试」——
        差一位的错误会让退避从 1.6s 起步，看起来没坏，只是每次都多等一倍。
        """
        if retry_after is not None:
            # 对方明确说了等多久，就听它的（但仍受上限约束）
            return max(0.0, min(float(retry_after), self.max_delay))

        backoff = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        # 抖动是「往上抖」而不是「上下抖」：下限保持理论值，
        # 免得在服务端正好缓过来的那个瞬间又去撞它。
        return backoff * (1 + random.uniform(0, self.jitter))


DEFAULT_POLICY = RetryPolicy()


def policy_from_env() -> RetryPolicy:
    """允许用环境变量临时改次数（调试用）。

    只开放 attempts —— base_delay 这类参数调错了不会报错，
    只会让程序「感觉怪怪的」，不值得给一个外部入口。
    """
    raw = os.getenv(ENV_ATTEMPTS, "").strip()
    if not raw:
        return DEFAULT_POLICY
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_POLICY
    if value < 1:
        return DEFAULT_POLICY
    return RetryPolicy(
        attempts=value,
        base_delay=DEFAULT_POLICY.base_delay,
        max_delay=DEFAULT_POLICY.max_delay,
        jitter=DEFAULT_POLICY.jitter,
    )


def is_retryable(exc: BaseException) -> bool:
    """这个异常值得再试一次吗？"""
    if isinstance(exc, APIError):
        return exc.status in RETRYABLE_STATUS
    # httpx 的两类「临时故障」：
    #   TimeoutException  —— 连上了但对方不说话（读超时最典型）
    #   TransportError    —— 网络层直接断（ConnectError / ReadError /
    #                        RemoteProtocolError，对方中途关连接就是它）
    #
    # 为什么不用 `Exception` 一把兜住？
    #   因为那会把「参数写错」「JSON 解析失败」也拿去重试。
    #   重试一个逻辑错误，只会让错误晚三倍时间出现，还多花两次钱。
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


def retry_after_of(exc: BaseException) -> float | None:
    value = getattr(exc, "retry_after", None)
    return float(value) if value is not None else None


def print_notice(attempt: int, exc: BaseException, delay: float) -> None:
    """重试时在 stderr 说一声。

    为什么不能悄悄重试？
        对用户来说，悄悄重试 = 界面卡住几秒然后突然好了，
        他不知道发生了什么；真出问题时也没有任何线索。
        一行 `[重试]` 的成本极低，信息量极大。
        写 stderr 而不是 stdout，是为了不把 CLI 的正常输出搅乱。
    """
    status = f" HTTP {exc.status}" if isinstance(exc, APIError) else ""
    print(
        f"[重试] 第 {attempt} 次失败（{type(exc).__name__}{status}），"
        f"{delay:.1f}s 后再试一次",
        file=sys.stderr,
        flush=True,
    )


def with_retry(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy | None = None,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    sleeper: Callable[[float], Any] | None = None,
) -> T:
    """执行 fn()，失败且值得重试时就退避重试。

    sleeper 为什么是参数？
        测试里不能真 sleep —— 一个 3 次重试的用例会白等 2.4 秒，
        十几个用例就是一分钟。传一个「只记录不等待」的函数进去，
        还能顺带断言「退避确实是指数的」。
        （这是「依赖注入」在测试上的又一次回报。）

        ⚠️ 默认值写成 `sleeper=None` 再在函数体里取 `time.sleep`，
           而不是直接写 `sleeper=time.sleep`。区别很实际：
           默认参数在**函数定义时**就绑定了，之后 monkeypatch `time.sleep`
           对它无效 —— 测试里怎么打桩都还是会真睡。这类坑不会报错，
           只表现为「测试莫名其妙很慢」。

    返回最后一次的异常，而不是包一个新的：
        调用方需要知道原样发生了什么（401 还是超时），
        包成 RetryError 反而要求他再拆一层才能拿到状态码。
    """
    policy = policy or policy_from_env()
    attempts = max(1, policy.attempts)
    sleep = sleeper or time.sleep

    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 —— 要看具体类型再决定，不能在这里分类
            if attempt >= attempts or not is_retryable(exc):
                raise
            delay = policy.delay_for(attempt, retry_after_of(exc))
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            sleep(delay)

    # 走不到这里：循环里要么 return，要么 raise。
    raise AssertionError("with_retry 的逻辑有问题")  # pragma: no cover
