"""渠道层：把「消息从哪来」和「Agent 怎么想」分开。

## 为什么现在才需要这一层

    mini-agent 原来只有一个入口（CLI），后来加了 Web，一共两个。
    两个的时候不需要抽象 —— `cli.py` 建一个 AgentLoop，`server.py` 建一个，
    各写各的，看不出问题。

    加上微信就是第三个。第三个入口出现时，三份「建 agent / 读历史 / 存会话」
    的代码会立刻长出三种细微的不一致，而这类 bug 只在某个渠道上复现。

    这就是 nanobot 为什么要单独做 `channels/`（18 个子包）：
    **渠道会越来越多，而内核不该跟着变。**

## 这一层只解决两件事

    1. 把各家的原始消息，归一成一个 `Inbound`
    2. 给每个「人」算出一个**稳定的**会话 id

    它不做「怎么回复」（各家协议不同），也不做「怎么想」（那是 loop 的事）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Inbound:
    """一条归一化后的入站消息。

    `msg_id` 不是好看用的，是**幂等键**：
        微信在 5 秒内收不到响应会重发同一条消息，最多 3 次。
        没有幂等键，同一个问题会被回答三遍，工具被执行三次。
    """

    channel: str                 # "wechat" / "cli" / "web"
    user_id: str                 # 谁说的（微信是 openid）
    text: str                    # 说的话
    msg_id: str                  # 幂等键
    raw: dict[str, Any] = field(default_factory=dict)   # 原始字段，调试用


_UNSAFE = re.compile(r"[^0-9A-Za-z_\-]")


def session_id_for(channel: str, user_id: str) -> str:
    """给「某渠道的某个人」算一个**稳定**的会话 id。

    为什么不能用 `new_session_id()`（时间戳）：
        那个函数回答的是「这次对话什么时候开始的」，不是「这是谁」。
        在 IM 里它有两个硬伤：
          ① 两个人同一秒发消息 → 撞名 → 混进同一个会话
          ② 同一个人隔天再发 → 新 id → **它忘了昨天聊过什么**

    为什么要哈希：
        openid 是 28 位大小写混合串，直接当文件名没问题，但它会出现在
        文件列表、URL、日志里。哈希之后既保持稳定，又不会把用户的
        微信标识散落到各处。取 16 位足够避免碰撞（生日界远大于实际用户量）。
    """
    digest = hashlib.sha1(f"{channel}:{user_id}".encode("utf-8")).hexdigest()[:16]
    return f"{channel}-{digest}"


def safe_user_id(raw: str) -> str:
    """只保留可安全写进文件名/日志的字符（防御性，理论上 openid 本来就干净）。"""
    return _UNSAFE.sub("", raw or "")[:64]
