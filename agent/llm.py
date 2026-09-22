"""模型调用封装 —— 全项目唯一与模型厂商耦合的文件。

为什么单独关进一个文件？
    将来从这家换到那家，只改这一个文件，其余代码一行不动。
    这正是 nanobot 把 providers/ 单独成目录的原因。

对外提供四样东西：
    load_config()    读 .env，缺项就报错
    chat()           一次性拿回完整回复（v0.1）
    stream_chat()    一小段一小段地拿（v0.2）
    format_usage()   把 token 用量排成人话（成本控制，第 1 周就要开始记）
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Iterator

import httpx
from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """配置缺失，或者还是模板里的示例值。"""


@dataclass
class Reply:
    """一次回复。text 是正文，usage 是 token 账本。

    为什么用 dataclass 而不是直接返回字符串？
        因为「成本控制」是贯穿全程的质量线 —— 每次请求都必须留下 token 账。
        返回字符串就把账丢了。
    """

    text: str
    usage: dict[str, Any]
    reasoning_chars: int = 0


def load_config() -> dict[str, Any]:
    """从环境变量读配置。缺任何一项就抛 ConfigError。

    为什么不给缺项兜底默认值？
        带着半个配置去调接口，报的错会离真正原因很远（401 / 404 / 模型不存在），
        你会白排查半小时。宁可在门口就拦下。
    """
    required = ["LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL_ID"]
    cfg: dict[str, Any] = {}
    missing = []
    for key in required:
        value = os.getenv(key)
        if not value or "请替换" in value:
            missing.append(key)
        else:
            cfg[key] = value
    if missing:
        raise ConfigError("缺少配置：" + "、".join(missing) + "（检查 .env 是否已填好）")

    # os.getenv 的第二个参数是默认值。有了它，返回值就不可能是 None。
    # max_tokens / timeout 是「调优参数」，给默认值合理；
    # API Key 那种「身份信息」才绝不能给默认值。
    cfg["max_tokens"] = int(os.getenv("LLM_MAX_TOKENS", "2048"))
    cfg["timeout"] = int(os.getenv("LLM_TIMEOUT", "30"))
    cfg["url"] = cfg["LLM_BASE_URL"].rstrip("/") + "/chat/completions"
    return cfg


def _headers(cfg: dict[str, Any]) -> dict[str, str]:
    return {
        # "Bearer" 后面那个空格不能少
        "Authorization": "Bearer " + cfg["LLM_API_KEY"],
        "Content-Type": "application/json",
    }


def _payload(cfg: dict[str, Any], messages: list[dict[str, str]], stream: bool) -> dict:
    return {
        "model": cfg["LLM_MODEL_ID"],
        "messages": messages,
        "max_tokens": cfg["max_tokens"],
        "stream": stream,
    }


def chat(messages: list[dict[str, str]], cfg: dict[str, Any] | None = None) -> Reply:
    """把整个 messages 发出去，拿回完整回复。

    注意一个事实：每调用一次，messages 里**全部**内容都会被重新发一遍。
    这就是「多轮对话」的真实成本结构 —— 聊得越久，每轮越贵。第 3 周专门解决它。
    """
    cfg = cfg or load_config()
    resp = httpx.post(
        cfg["url"],
        headers=_headers(cfg),
        json=_payload(cfg, messages, stream=False),
        timeout=cfg["timeout"],
    )
    if resp.status_code != 200:
        raise RuntimeError(f"请求失败 HTTP {resp.status_code}\n{resp.text}")

    data = resp.json()
    message = data["choices"][0]["message"]
    text = (message.get("content") or "").strip()
    reasoning = message.get("reasoning_content") or ""

    # 推理模型（本项目当前用的就是）会先产出一段思考，思考也算 max_tokens。
    # 思考偶尔会把额度吃满，导致 content 是空字符串。
    # 这个检查就是为了让你一眼看出「回复为空」不是代码写错了。
    if not text:
        raise RuntimeError(
            "模型只产出了思考、没有正文。\n"
            "最常见原因是 max_tokens 被思考 token 吃满 —— 把 .env 里的 LLM_MAX_TOKENS 调大。\n"
            f"（本次思考 {len(reasoning)} 字）"
        )

    return Reply(text=text, usage=data.get("usage") or {}, reasoning_chars=len(reasoning))


def stream_chat(
    messages: list[dict[str, str]], cfg: dict[str, Any] | None = None
) -> Iterator[str]:
    """一小段一小段地拿回复。

    接口返回的是 SSE（Server-Sent Events）文本流，每一行长这样：

        data: {"choices":[{"delta":{"content":"你"}}]}

    末尾来一行 data: [DONE] 表示结束。所以解析规则是：
    按行读 → 剥掉 "data: " 前缀 → 剥掉的那行如果是 [DONE] 就收工 → 否则 json.loads。

    为什么这段解析也要留在这个文件里？
        因为 SSE 的格式是接口协议的一部分 —— 换服务商时它也可能要改。
        「与厂商耦合的代码只在这一个文件里」是铁律。
    """
    cfg = cfg or load_config()
    with httpx.stream(
        "POST",
        cfg["url"],
        headers=_headers(cfg),
        json=_payload(cfg, messages, stream=True),
        timeout=cfg["timeout"],
    ) as resp:
        if resp.status_code != 200:
            resp.read()
            raise RuntimeError(f"请求失败 HTTP {resp.status_code}\n{resp.text}")

        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            chunk = line[len("data:") :].strip()
            if chunk == "[DONE]":
                break
            try:
                delta = json.loads(chunk)["choices"][0].get("delta") or {}
            except (json.JSONDecodeError, IndexError, KeyError, TypeError):
                # 坏片段跳过就行。为了一个字符崩掉整轮对话，不值得。
                continue

            piece = delta.get("content")
            if piece:
                yield piece

            # 推理模型还会吐 "reasoning_content"（思考过程），这里刻意丢掉它。
            # 被丢掉的副作用是：你会看到开头有几秒停顿，然后文字才开始蹦。
            # 想观察它在想什么，把下面两行取消注释：
            # if delta.get("reasoning_content"):
            #     yield "·"


def format_usage(usage: dict[str, Any] | None) -> str:
    """把 usage 字典排成一行人话。"""
    if not usage:
        return "tokens: 无数据"
    prompt = usage.get("prompt_tokens", 0)
    completion = usage.get("completion_tokens", 0)
    total = usage.get("total_tokens", 0)
    details = usage.get("completion_tokens_details") or {}
    reasoning = details.get("reasoning_tokens", 0)
    line = f"tokens: 输入 {prompt} + 输出 {completion}"
    if reasoning:
        line += f"（其中思考 {reasoning}）"
    return line + f" = {total}"
