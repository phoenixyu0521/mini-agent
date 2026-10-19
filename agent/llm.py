"""模型调用封装 —— 全项目唯一与模型厂商耦合的文件。

为什么单独关进一个文件？
    将来从这家换到那家，只改这一个文件，其余代码一行不动。
    这正是 nanobot 把 providers/ 单独成目录的原因。

对外提供五样东西：
    load_config()      读配置（.env 的 Key + config.json 的模型参数），缺项就报错
    chat()             一次性拿回完整回复（v0.1）
    stream_chat()      一小段一小段地拿（v0.2）
    format_usage()     把 token 用量排成人话（成本控制，第 1 周就要开始记）
    make_summarizer()  造一个「把旧对话压成摘要」的函数（给 context.py 用）

第 3 周第四块（配置化）的改动：
    load_config() 内部改成调用 config.resolve()。**函数签名和返回值都不变** ——
    因为 chat() / stream_chat() / make_summarizer() 三处都在用它，
    改签名会连锁改动。这就是「保持接口稳定」的实际价值。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx
from dotenv import load_dotenv

from agent import config as _config

load_dotenv()


# ── 第 4 周：共享 HTTP 连接 ──────────────────────────────────
# 改之前 chat() / stream_chat() / _summarize() 三处都用 httpx.post(...) 这种
# 「一次性」写法 —— httpx 会在内部悄悄新建一个 Client，请求结束就把它关掉。
#
# 本机实测（2026-10-15）：
#     每次新建 Client 耗时  918 ~ 1027 ms   ← 纯开销，跟模型快慢无关
#     其中绝大部分是 SSLContext 初始化（读 ca-bundle 证书链）
#
# 为什么这个开销在本机特别大？httpx 默认会加载 certifi 的证书包做 TLS 校验。
# Windows 上这一步很慢（不是 httpx 的 bug，是平台差异）。
#
# 修法：**一个进程共用一个 Client**，让底层连接池复用 TCP + TLS 握手。
# 这一步只改 llm.py 一个文件 —— 铁律的真正价值就在这里，
# 换厂商 / 换传输方式时，只有这一个文件需要重新审阅。
#
# 为什么用函数懒加载，而不是直接写模块级变量？
#     import llm 是「加载代码」，不是「准备发请求」。
#     如果写成模块级 client = httpx.Client()，那么**任何** import 到 llm 的地方
#     （包括 cli.py 打印 --help、包括跑测试）都会白白付这 900ms。
#     懒加载把成本推迟到「真的要发请求的那一刻」。
#
# 线程安全：
#     httpx.Client 是线程安全的，server.py 用 run_in_executor 多线程调它没问题。
#     但**不要**把 Client 传给多个不同的 base_url 之外的地方去乱改它的属性。
_client: httpx.Client | None = None


def get_client() -> httpx.Client:
    """拿进程级共享的 HTTP 客户端，没有就造一个。

    注意 timeout 不在这里设：
        因为 timeout 来自 config.json，不同 profile 可能不一样。
        设成 Client 的默认值就得在换 profile 时重建 Client，
        而「每次请求传 timeout」是 httpx 官方支持的做法。
    """
    global _client
    if _client is None:
        _client = httpx.Client(
            # 连接池参数全部塞在 Limits 里（httpx 0.28 的位置，不是 Client 的）。
            #   max_connections           最多 20 条并发连接
            #   max_keepalive_connections 最多留 5 条空闲连接备用
            #   keepalive_expiry          空闲连接最多留 30 秒
            #
            # 为什么 max_connections 要从默认的 100 降下来？
            #   默认 100 是给「代理服务器转发大量用户」设计的。
            #   单机 Agent 一个用户，同时最多也就一两个请求，
            #   100 条连接在服务端全是占着内存不干活的僵尸。
            #
            # 为什么 keepalive_expiry 要从默认的 5 秒提到 30？
            #   推理模型思考时可能十几秒不吐字。默认 5 秒就把空闲连接掐了，
            #   下一个请求又要重新握手 —— 连接池等于白建。
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=5,
                keepalive_expiry=30.0,
            ),
        )
    return _client


def close_client() -> None:
    """主动关掉共享 Client。

    什么时候需要调？
        正常跑 CLI / server 时**不需要** —— 进程退出时 socket 由操作系统回收。
        但两种情况必须调：
          1) 跑测试：同一进程里要换 base_url / mock 掉网络，不关会串味。
          2) 写长期驻留的服务，想在收到退出信号时优雅关闭（释放服务端连接）。
    """
    global _client
    if _client is not None:
        _client.close()
        _client = None


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


def load_config(profile: str | None = None) -> dict[str, Any]:
    """读配置，返回一个**扁平**的 dict。缺 Key 就抛 ConfigError。

    第 3 周第四块之后，真正的解析逻辑搬去了 agent/config.py，
    这里只做一层「翻译」：把 Config 对象摊平成原来的 dict 形状。

    为什么不干脆让所有调用方改用 Config 对象？
        因为 chat() / stream_chat() / make_summarizer() 都在用这个 dict，
        而且它们关心的是 cfg["url"] / cfg["timeout"] 这几个具体 key。
        把它摊平成 dict，**三个调用方一行都不用改** —— 这就是「保持接口稳定」。

    为什么不给缺项兜底默认值？
        带着半个配置去调接口，报的错会离真正原因很远（401 / 404 / 模型不存在），
        你会白排查半小时。宁可在门口就拦下。
    """
    conf = _config.resolve(profile)
    return {
        "LLM_BASE_URL": conf.base_url,
        "LLM_API_KEY": conf.api_key,
        "LLM_MODEL_ID": conf.model,
        "max_tokens": conf.max_tokens,
        "timeout": conf.timeout,
        "url": conf.url,
        # 顺带把 profile 名也带上 —— cli.py 的横幅要显示它
        "profile": conf.profile,
    }


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
    resp = get_client().post(
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
            # ⚠️ 这里必须写 config.json，不能写 .env ——
            #    第 3 周配置化之后 max_tokens 已经搬进config.json 了。
            #    报错文案指向一个已经没有这个配置项的文件，
            #    会让人白翻半天 —— **报错信息指错地方比报错本身更误导**。
            f"最常见原因是 max_tokens（当前 {cfg['max_tokens']}）被思考 token 吃满"
            " —— 改 config.json 里对应 profile 的 max_tokens。\n"
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
    # 流式用的是 client.stream()，它返回一个上下文管理器。
    # ⚠️ 一个容易踩的坑：如果写成 get_client().post(..., stream=True)，
    #    响应体会一次性读完 —— 那就不叫流式了，打字机效果会消失。
    with get_client().stream(
        "POST",
        cfg["url"],
        headers=_headers(cfg),
        json=_payload(cfg, messages, stream=True),
        timeout=cfg["timeout"],
    ) as resp:
        if resp.status_code != 200:
            resp.read()
            raise RuntimeError(f"请求失败 HTTP {resp.status_code}\n{resp.text}")

        # ─────────────────────────────────────────────────────────
        # 这里要盯两个量，因为**流式下的失败模式是静默的**。
        #
        # 背景：推理模型会先吐 reasoning_content（思考），思考也算 max_tokens。
        # 如果思考就把额度吃满，正文（content）会**一帧都没有**。
        # 非流式那条路（chat()）有检查会抛错；
        # 流式这条路如果不检查，结果就是「函数正常返回、一个片段都没有」——
        # 调用方以为成功，界面上只是出现一个空气泡。
        #
        # **静默失败是最坏的失败形态**：没有 traceback、没有错误消息、
        # 日志上一切正常，只有用户看见「它不说话」。
        #
        # 所以这里记两个量，在流结束时统一判断。
        # ─────────────────────────────────────────────────────────
        finish_reason: str | None = None
        got_content = False
        reasoning_chars = 0

        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            chunk = line[len("data:") :].strip()
            if chunk == "[DONE]":
                break
            try:
                frame = json.loads(chunk)
            except (json.JSONDecodeError, TypeError):
                # 坏片段跳过就行。为了一个字符崩掉整轮对话，不值得。
                continue

            # 最后一帧的 choices 可能是**空数组**（只带 usage）。
            # 这里必须先判空再下标 —— 否则 IndexError 会把整轮打断。
            choices = frame.get("choices") or []
            if not choices:
                continue

            choice = choices[0]
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}

            # 思考内容：只在计数，不产出（见下方注释）。
            if delta.get("reasoning_content"):
                reasoning_chars += len(delta["reasoning_content"])

            piece = delta.get("content")
            if piece:
                got_content = True
                yield piece

            # 推理模型还会吐 "reasoning_content"（思考过程），这里刻意丢掉它。
            # 被丢掉的副作用是：你会看到开头有几秒停顿，然后文字才开始蹦。
            # 想观察它在想什么，把下面两行取消注释：
            # if delta.get("reasoning_content"):
            #     yield "·"

        # ── 流结束了，一个片段都没给出来 → 必须报错，不能静默返回 ──
        # 这段检查和 chat() 里那段是同一个道理，只是触发条件不同：
        #   chat()   —— 有 reasoning 但 content 为空
        #   这里     —— 一个 content 帧都没收到过
        if not got_content:
            if finish_reason == "length":
                raise RuntimeError(
                    "模型的思考把 max_tokens 吃满了，一个字正文都没输出。\n"
                    f"（思考了 {reasoning_chars} 字，finish_reason=length）\n"
                    "两个办法：把 config.json 里该profile 的 max_tokens 调大；"
                    "或者换个不用长思考的问题。"
                )
            raise RuntimeError(
                f"模型没有输出任何正文（finish_reason={finish_reason!r}）。\n"
                f"（思考了 {reasoning_chars} 字）\n"
                "常见原因：max_tokens 太小，或上游返回了空响应。"
            )


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
        resp = get_client().post(
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
