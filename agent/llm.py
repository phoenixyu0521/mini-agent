"""模型调用封装 —— 全项目唯一与模型厂商耦合的文件。

为什么单独关进一个文件？
    将来从这家换到那家，只改这一个文件，其余代码一行不动。
    这正是 nanobot 把 providers/ 单独成目录的原因。

对外提供四样东西：
    load_config()      读 .env，缺项就报错
    chat()             一次性拿回完整回复（v0.1）
    stream_chat()      一小段一小段地拿（v0.2）
    format_usage()     把 token 用量排成人话（成本控制，第 1 周就要开始记）

第 3 周新增：
    make_summarizer()  造一个「把旧对话压成摘要」的函数（给 context.py 用）
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx
from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """配置缺失，或者还是模板里的示例值。"""


@dataclass
class ToolCall:
    """模型的一次「调用请求」。第 2 周新增。

    三个字段实测确认（2026-09-26，本项目中转站）：
        id        服务商给的编号，形如 "call_7dd76c91071b4358b42008d8"。
                  回填结果时必须原样带上，模型靠它对上号。
        name      要调哪个函数。长这样："get_time"
        arguments **字符串**，不是 dict！长这样："{}" 或 '{"path":"a.txt"}'
                  所以用之前必须 json.loads()。这是新手最容易踩的一脚。
    """

    id: str
    name: str
    arguments: str

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "ToolCall":
        """把服务商返回的那一坨拆成三个干净字段。

        嵌套结构（实测）：
            {"id": "call_xxx", "type": "function",
             "function": {"name": "get_time", "arguments": "{}"}}

        全用 .get() 取值并给兜底，是因为这些字段理论上都可能缺 ——
        缺了也不该崩，让它变成一个「名字是空的工具」，后面的报错会更友好。
        """
        function = raw.get("function") or {}
        return cls(
            id=raw.get("id") or "",
            name=function.get("name") or "",
            arguments=function.get("arguments") or "",
        )

    def parsed_arguments(self) -> dict[str, Any]:
        """把 arguments 解析成 dict。解析失败时返回空 dict，不抛异常。

        为什么容错而不是抛错？
            模型产出坏 JSON 是**必然会发生**的事（ROADMAP 第 2 周第 2 个设计问题）。
            为它崩掉整轮对话不值得。返回空 dict 后，工具会因为缺参数而报错，
            那条错误信息会回到模型手里，它通常能自己纠正过来。
        """
        if not self.arguments or not self.arguments.strip():
            return {}
        try:
            value = json.loads(self.arguments)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def to_raw(self) -> dict[str, Any]:
        """还原成服务商要的原始结构（回填时用）。"""
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass
class Reply:
    """一次回复。text 是正文，usage 是 token 账本。

    为什么用 dataclass 而不是直接返回字符串？
        因为「成本控制」是贯穿全程的质量线 —— 每次请求都必须留下 token 账。
        返回字符串就把账丢了。

    第 2 周多了 tool_calls：
        模型有两种「回复」方式 —— 说一段话，或者要求你调函数。
        **要求调函数时 text 是空字符串**，所以不能只看 text 判断成功与否。
    """

    text: str
    usage: dict[str, Any]
    reasoning_chars: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        """模型的真实意图是「先帮我办事」而不是「回答你」。"""
        return bool(self.tool_calls)

    def to_message(self) -> dict[str, Any]:
        """转成能塞回 messages 的 assistant 消息。

        为什么只挑三个字段，而不是把服务商返回的 message 整体存回去？
            因为原始 message 里带着 reasoning_content 这类私有字段。
            服务商对未知字段的容忍度不一致 —— 有的直接 400。
            只留协议规定的三个，才换得过服务商。
        """
        message: dict[str, Any] = {"role": "assistant", "content": self.text}
        if self.tool_calls:
            message["tool_calls"] = [tc.to_raw() for tc in self.tool_calls]
        return message


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


def _payload(
    cfg: dict[str, Any],
    messages: list[dict[str, Any]],
    stream: bool,
    tools: list[dict[str, Any]] | None = None,
) -> dict:
    payload: dict[str, Any] = {
        "model": cfg["LLM_MODEL_ID"],
        "messages": messages,
        "max_tokens": cfg["max_tokens"],
        "stream": stream,
    }
    # 只在真的带工具时才加这个字段。
    # 不传 tools 时，服务商根本不会返回 tool_calls —— 这也是 v0.1/v0.2 还能照常跑的原因。
    if tools:
        payload["tools"] = tools
    return payload


def chat(
    messages: list[dict[str, Any]],
    cfg: dict[str, Any] | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> Reply:
    """把整个 messages 发出去，拿回完整回复。

    注意一个事实：每调用一次，messages 里**全部**内容都会被重新发一遍。
    这就是「多轮对话」的真实成本结构 —— 聊得越久，每轮越贵。第 3 周专门解决它。

    tools 参数（第 2 周新增）：
        传了它就是「这些是我能帮你调的函数，这是它们的说明书」。
        模型有需要时会返回 tool_calls 而不是正文。
    """
    cfg = cfg or load_config()
    resp = httpx.post(
        cfg["url"],
        headers=_headers(cfg),
        json=_payload(cfg, messages, stream=False, tools=tools),
        timeout=cfg["timeout"],
    )
    if resp.status_code != 200:
        raise RuntimeError(f"请求失败 HTTP {resp.status_code}\n{resp.text}")

    data = resp.json()
    message = data["choices"][0]["message"]
    text = (message.get("content") or "").strip()
    reasoning = message.get("reasoning_content") or ""
    tool_calls = [ToolCall.from_raw(tc) for tc in (message.get("tool_calls") or [])]

    # 推理模型（本项目当前用的就是）会先产出一段思考，思考也算 max_tokens。
    # 思考偶尔会把额度吃满，导致 content 是空字符串。
    # 但注意加了个前提：**要求调工具时正文本来就该是空的**，那不是故障。
    if not text and not tool_calls:
        raise RuntimeError(
            "模型只产出了思考、没有正文。\n"
            "最常见原因是 max_tokens 被思考 token 吃满 —— 把 .env 里的 LLM_MAX_TOKENS 调大。\n"
            f"（本次思考 {len(reasoning)} 字）"
        )

    return Reply(
        text=text,
        usage=data.get("usage") or {},
        reasoning_chars=len(reasoning),
        tool_calls=tool_calls,
    )


def stream_chat(
    messages: list[dict[str, Any]], cfg: dict[str, Any] | None = None
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


# ── 第 3 周新增：摘要器 ─────────────────────────────────────
# 为什么放在 llm.py 而不是 context.py？
#   因为**它要调模型**。而「与厂商耦合的代码只允许出现在 llm.py」
#   是本项目的铁律。context.py 只定义「需要一个摘要函数」，
#   具体怎么调模型由这里提供 —— 这就是依赖注入的好处。

SUMMARIZE_PROMPT = (
    "以下是一段对话记录。请把它压缩成一段简洁的中文摘要，用于后续对话的背景。\n"
    "只保留这些：用户的身份与偏好、已确认的事实与结论、正在进行的任务、明确的承诺。\n"
    "不要保留：寒暄、重复内容、被否决的方案、工具调用的技术细节。\n"
    "直接输出摘要正文，不要加任何前言或标题。控制在 200 字以内。"
)


def make_summarizer(cfg: dict[str, Any] | None = None) -> Any:
    """造一个「把一串消息压成摘要」的函数。

    返回的函数签名是 `f(messages: list[dict]) -> str`，
    正好符合 context.Summarizer 的约定。

    为什么要「造一个函数」而不是直接提供一个函数？
        因为要先把 cfg 定下来（否则每次摘要都要重新读 .env）。
        这叫**闭包** —— 内层函数记住了外层的 cfg。
        你在 stage0_check.py 里其实见过类似的写法，只是当时没意识到。
    """
    cfg = cfg or load_config()

    def _summarize(early: list[dict[str, Any]]) -> str:
        # 把结构复杂的消息先抹平成纯文本。
        # 为什么不在 context.py 里做？因为「怎么转成文本」其实取决于
        # 厂商对消息格式的要求 —— 又是一件与协议相关的事，所以归 llm.py。
        lines: list[str] = []
        for m in early:
            role = m.get("role") or "?"
            content = m.get("content")
            if isinstance(content, str) and content.strip():
                lines.append(f"{role}: {content}")
            # tool_calls 只留个痕迹，不留完整参数 —— 摘要不需要它们
            for tc in m.get("tool_calls") or []:
                function = tc.get("function") or {}
                lines.append(f"{role}: (调用了工具 {function.get('name')})")
        blob = "\n".join(lines)

        # 摘要是一次独立的请求。它不带 tools —— 我们只要它说话，不要它调函数。
        resp = httpx.post(
            cfg["url"],
            headers=_headers(cfg),
            json={
                "model": cfg["LLM_MODEL_ID"],
                "messages": [
                    {"role": "system", "content": SUMMARIZE_PROMPT},
                    {"role": "user", "content": blob},
                ],
                "max_tokens": 512,   # 摘要很短，512 足够，别用 2048 浪费
                "stream": False,
            },
            timeout=cfg["timeout"],
        )
        if resp.status_code != 200:
            raise RuntimeError(f"摘要请求失败 HTTP {resp.status_code}\n{resp.text}")

        message = resp.json()["choices"][0]["message"]
        # 用 .get 并兜底空串：推理模型偶尔会把 512 全用在思考上，
        # content 就是空的。宁可返回空串（调用方能判断），也别在这里崩。
        return (message.get("content") or "").strip()

    return _summarize
