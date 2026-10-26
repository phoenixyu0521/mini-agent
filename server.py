"""HTTP 接口层 —— 第 4 周：从「脚本」变成「项目」。

CLI 与 Web 的关系：

    cli.py        终端入口  ┐
                            ├── 都调用同一个 AgentLoop，内核一行不改
    server.py     HTTP 入口 ┘

为什么要「会话池」？
    AgentLoop 把一次对话的全部记忆放在 self.messages 里 —— 它是**实例状态**，
    不是全局状态。所以：

        两个用户同时聊   →  必须有两个 AgentLoop 实例
        共用一个实例     →  A 会看到 B 说的话（串话）

    这个「按 id 管理多个会话」的小字典，就是 nanobot 里 runner.py（56 KB）的起点。
    你现在写的这几行，它用 5 万行把它做成了产品。

跑起来：
    uv run python server.py
    然后浏览器打开 http://127.0.0.1:8000
"""

from __future__ import annotations

import asyncio
import json
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any, Iterator

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import config as config_mod
from agent import llm
from agent import mcp
from agent import metrics as metrics_mod
from agent.loop import AgentLoop
from agent.memory import Memory
from agent.session import Session, new_session_id
from agent.tools import registry as registry_mod

WEB_DIR = Path(__file__).resolve().parent / "web"

# 版本号只留**一个**来源：pyproject.toml 的 version。
#
# 为什么值得专门写三行来做这件事？
#     因为打包这一步让「两份版本号」第一次真正撞车了 ——
#     pyproject 已改成 0.11.0，而这里还手写着 v0.10，
#     于是 /api/info 对外报的是旧版本。**这种错不会报错，只会骗人。**
#
#     和「Key 只能有一个来源」是同一条原则：能推导的东西，不要抄写。
try:
    VERSION = "v" + pkg_version("mini-agent")
except PackageNotFoundError:
    # 没装进环境（例如裸 `python server.py`）时的降级：宁可显示 dev，也不编一个号
    VERSION = "dev"

app = FastAPI(title="mini-agent", version=VERSION)


@app.exception_handler(NotImplementedError)
async def _todo_not_done(request: Request, exc: NotImplementedError) -> JSONResponse:
    """TODO 还没补时，返回人话而不是一大段 traceback。

    为什么要专门处理这个？
        骨架交付时两处 TODO 是空的，用户可能还没补就先跑起来看看。
        此时甩 traceback 会让人以为「整个项目是坏的」，
        而其实只差两个函数 —— 和第 3 周 config.py 那次是同一个教训。
    """
    return JSONResponse(
        status_code=503,
        content={"detail": f"内核还没写完 —— {exc}"},
    )


# ══════════════════════════════════════════════════════════════
#  会话池 —— 第 4 周的第一个坑
# ══════════════════════════════════════════════════════════════

_agents: dict[str, AgentLoop] = {}

# MCP 配置只读一次。为什么缓存？
#   config.json 在进程生命周期内不会变 —— 改了要重启服务（既有约定）。
#   而 _build_agent() 每建一个会话都会调它，不缓存就是每次读一遍磁盘。
_mcp_servers_cache: dict[str, Any] | None = None


def _mcp_servers() -> dict[str, Any]:
    global _mcp_servers_cache
    if _mcp_servers_cache is None:
        try:
            _mcp_servers_cache = config_mod.resolve().mcp_servers
        except (config_mod.ConfigError, llm.ConfigError):
            # 配置有问题时不要连累工具加载 —— /api/info 会单独把配置错误报出来
            _mcp_servers_cache = {}
    return _mcp_servers_cache


def _build_agent(session_id: str) -> AgentLoop:
    """造一个接好 session / memory 的 AgentLoop。脚手架，已写好。

    里面的顺序和 cli.py 完全一样（复用同一套约定）：
        load_session() 成功 → 什么都不做（磁盘历史就是真相）
        失败（新会话）      → 才写开场行 start()
    这个「先读、读不到才写」的顺序不能反 —— 反了会把已有会话清空。
    """
    session = Session.load(session_id)
    agent = AgentLoop(
        with_tools=True,
        session=session,
        memory=Memory(),
        mcp_servers=_mcp_servers(),
    )
    if not agent.load_session():
        session.start(agent.messages[0]["content"])
    agent.load_memory()
    return agent


def get_agent(session_id: str) -> AgentLoop:
    """按 session_id 取会话；池里没有就新建一个并放进去。

    ─────────────────────────────────────────────────────────
    TODO ② —— 三行就够，但它是「多用户不串话」的全部秘密

        if session_id not in _agents:
            _agents[session_id] = _build_agent(session_id)
        return _agents[session_id]

    想一想再动手：
        Q1：为什么必须「一个 session_id 一个 AgentLoop」，不能只用一个全局实例？
            提示：self.messages 是**实例属性**，不是类属性。
                  共用一个实例时，第二个用户的消息会被 append 到
                  第一个用户的对话里 —— 模型会以为「A 刚才说了 B 的话」。
        Q2：如果服务器跑一个星期，这个字典会越来越大。这是好是坏？
            提示：这就是常说的「内存泄漏」。第 4 周的取舍是
                  「先不管，但要知道它在那里」——
                  真实系统会给它加 TTL 过期，或者只在内存里放「活跃会话」。
    ─────────────────────────────────────────────────────────
    """
    if session_id not in _agents:
        _agents[session_id] = _build_agent(session_id)
    return _agents[session_id]


# ══════════════════════════════════════════════════════════════
#  请求体
# ══════════════════════════════════════════════════════════════


class ChatRequest(BaseModel):
    session_id: str
    message: str
    # v0.10：要不要带工具。默认 True —— 这是 Agent，不是聊天机器人。
    # 前端会在两种模式之间切（见 web/index.html 的 mode 选择）。
    use_tools: bool = True


class MemoryRequest(BaseModel):
    session_id: str
    line: str


# ══════════════════════════════════════════════════════════════
#  页面
# ══════════════════════════════════════════════════════════════


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


# 前端用的第三方库（marked / highlight.js）存了本地一份，不走 CDN。
#
# 为什么不用 <script src="https://cdn.jsdelivr.net/...">：
#   ① 这是个**本地工具**，断网也要能打开；
#   ② 国内访问 jsdelivr 时通时不通，引 CDN 会让「界面白屏」
#      变成一个随机故障 —— 而且它偏偏在网络好的时候不出现。
#
# StaticFiles 自己处理路径穿越（`/vendor/../../.env` 会被拒），
# 比手写 FileResponse + 字符串拼接安全。
try:
    app.mount("/vendor", StaticFiles(directory=WEB_DIR / "vendor"), name="vendor")
except RuntimeError:  # 目录不存在（比如只 clone 了部分文件）时不要让服务起不来
    pass


@app.get("/api/info")
async def info() -> dict[str, Any]:
    """给前端显示状态用。脚手架。

    注意 Config 是**扁平属性**（conf.model / conf.profile），
    不是 conf.data["model"] —— 它是值对象，构造时就把 dict 摊平了。
    """
    try:
        conf = config_mod.resolve()
        model, profile = conf.model, conf.profile
        max_tokens, timeout = conf.max_tokens, conf.timeout
    except Exception as exc:  # noqa: BLE001
        model, profile, max_tokens, timeout = f"(配置有问题：{exc})", "?", 0, 0

    return {
        "version": VERSION,
        "profile": profile,
        "model": model,
        "max_tokens": max_tokens,
        "timeout": timeout,
        "sessions": [
            {"id": sid, **{k: v for k, v in Session(sid).stats().items()
                           if k in ("created_at", "message_count")}}
            for sid in Session.list_sessions(limit=30)
        ],
    }


# ══════════════════════════════════════════════════════════════
#  对话 —— 非流式
# ══════════════════════════════════════════════════════════════


@app.post("/api/chat")
async def chat(req: ChatRequest) -> dict[str, Any]:
    """非流式的一轮：一次性返回完整回复。

    v0.10 的改动：调用的方法从 agent.run 换成 agent.run_with_tools。

        改前  agent.run(req.message)              1 次模型往返，永远不调工具
        改后  agent.run_with_tools(req.message)   1~N 次模型往返，会调工具

    ⚠️ 第 2 步之后，**/api/stream 也能带工具了**。这个端点的价值变成了：
        需要「一次性拿到完整结果 + 工具次数」的场合（脚本、自动化、测试）。
        两个端点现在能力相同，区别只在传输方式。

    为什么工具调用**不能纯流式**？（这是本周最值得想清楚的一件事）
        stream() 是生成器，它 yield 的是一小段一小段的内容。
        而工具调用需要先拿到**完整的** tool_calls 列表才能决定下一步：
        你必须知道模型点了哪些函数、参数是什么，才能去执行。
        前 20 个小片到达时，你根本不知道这一轮会不会产生 tool_calls。

        所以做法是「流式说话 + 非流式干活」：
        一轮里先流式收，一旦发现这一轮要 tool_calls，
        就在那个位置切到非流式完成工具循环，前端仍感知为一条流。
        （实现见 AgentLoop.stream() 里的切换点）
    """
    agent = get_agent(req.session_id)

    # 把同步调用丢进线程池，别堵住事件循环。
    #
    # 为什么必须这样？agent.run() 内部是同步的 httpx.post —— 它一开跑就在
    # 当前线程上死等网络。而 FastAPI 只有**一个事件循环线程**，
    # 堵住它 = 所有请求排队（包括刷新页面），多用户时就是
    # 「一个人说话，所有人卡住」。
    #
    # run_in_executor 三个参数：线程池（None = 默认的）、要调的函数
    # （**只写函数名不加括号**）、它的位置参数。返回可等待对象，必须 await。
    # 效果：httpx.post 在另一个线程里等，事件循环那个线程空出来接待新请求。
    #
    # ⚠️ 工具调用让这件事**更重要**了：一次工具轮是 2 次串行的模型往返，
    #    实测要 4~8 秒。不丢线程池，这几秒内整个服务都是僵的。
    loop = asyncio.get_running_loop()

    if req.use_tools and agent.tools is not None:
        # run_with_tools 有第二个参数 max_iterations，这里用默认值 5。
        # run_in_executor 只传**一个**位置参数 —— 想改 max_iterations
        # 就得用 functools.partial 包一层，这是另一个值得记住的坑。
        reply = await loop.run_in_executor(None, agent.run_with_tools, req.message)
    else:
        reply = await loop.run_in_executor(None, agent.run, req.message)

    return {
        "session_id": req.session_id,
        "reply": reply,
        "usage": agent.last_usage,
        "compact_count": agent.compact_count,
        # v0.10：这两个数字只能由内核给。入口层拿到的是字符串，
        # 字符串里没有「调了几次工具」—— 让入口层数 messages 的话，
        # 数到的是**会话累计**而非这一轮，越聊越离谱。
        #
        # ⚠️ getattr 默认值不是「防御性编程」，而是**骨架期的必要兜底**：
        #    last_tool_calls 由 loop.py 的 TODO 提供，用户还没补时这一行会
        #    AttributeError 让整个端点 500 —— 而 reply 明明已经算出来了。
        #    一个纯装饰性的计数项不该有能力搞垮整个响应。
        "tool_calls": getattr(agent, "last_tool_calls", None),
        "streamed": False,
    }


# ══════════════════════════════════════════════════════════════
#  对话 —— 流式（前端默认走这条）
# ══════════════════════════════════════════════════════════════


@app.post("/api/stream")
async def stream(req: ChatRequest) -> StreamingResponse:
    """SSE 流式：边收边推给浏览器，做出打字机效果。

    ⭐ v0.10 第 2 步之后，**这个端点也支持工具了**。
    机制是「流式说话 + 非流式干活」：内核先流式收第一轮，
    一旦发现这一轮有 tool_calls，就在那个位置切到非流式把工具循环跑完。
    所以前端感知到的仍然是一条连续的流。

    帧格式（v0.10 起，多了 type 字段 —— 前端按它分派）：
        data: {"type": "text",  "text": "你"}
        data: {"type": "tool",  "name": "get_time", "arguments": "{}",
                               "result": "...", "ok": true}
        data: [DONE]      ← 结束标记（**不是内核发的**，是这一层加的）

    ⚠️ 为什么 "done" 由这一层加，而不是内核 yield 一个 done 事件？
        因为「这条流结束了」是**传输层的概念**，不是 Agent 内核的概念。
        内核交出最后一个事件就算交完了，它不需要知道自己在被 SSE 打包。
        这仍然是那句话：**内核提供能力，入口决定怎么显示。**

    为什么这个端点不用 run_in_executor？
        因为 StreamingResponse 收到**同步生成器**时，Starlette 自己会把它
        放进线程池迭代（内部叫 iterate_in_threadpool）——
        所以这里的阻塞同样不会卡住事件循环。这是框架帮我们做掉的。

    注意 get_agent() 是放在 generate() **里面**调的：
        这样万一它抛错，错误会变成一条 SSE 消息
        送到前端显示，而不是变成一个干巴巴的 HTTP 503 ——
        前端只需要一套错误处理逻辑。
    """

    def generate() -> Iterator[str]:
        try:
            agent = get_agent(req.session_id)
            for event in agent.stream(req.message, use_tools=req.use_tools):
                # 事件本身就是 dict，直接序列化就行 —— 这就是选 dict 的好处，
                # 换到 dataclass 的话这里还得先 asdict() 一次。
                payload = json.dumps(event, ensure_ascii=False)
                yield f"data: {payload}\n\n"
            yield "data: [DONE]\n\n"
        except NotImplementedError as exc:
            payload = json.dumps({"error": f"内核还没写完：{exc}"}, ensure_ascii=False)
            yield f"data: {payload}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:  # noqa: BLE001
            payload = json.dumps({"error": f"{type(exc).__name__}: {exc}"},
                                 ensure_ascii=False)
            yield f"data: {payload}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ══════════════════════════════════════════════════════════════
#  会话与记忆
# ══════════════════════════════════════════════════════════════


@app.get("/api/sessions")
async def list_sessions() -> list[dict[str, Any]]:
    out = []
    for sid in Session.list_sessions(limit=30):
        st = Session(sid).stats()
        if st.get("exists"):
            out.append(st)
    return out


@app.post("/api/sessions/new")
async def new_session() -> dict[str, str]:
    """开一个新会话，返回它的编号。脚手架。

    注意这里**只发号不落盘** —— 文件要等第一次真正发消息、
    session.start() 被调到时才创建。和 cli.py --new-session 的行为一致。
    """
    return {"session_id": new_session_id()}


@app.get("/api/history")
async def history(session_id: str) -> dict[str, Any]:
    """读回某个会话的历史消息（给前端渲染）。脚手架。

    这里**故意不经过 get_agent()** —— 渲染历史不该顺手在内存里
    造一个 AgentLoop。只读磁盘就够了，也更省内存。
    """
    messages = Session(session_id).messages()
    return {
        "session_id": session_id,
        "messages": [
            {"role": m.get("role"), "content": m.get("content")}
            for m in messages
            if m.get("role") in ("user", "assistant") and m.get("content")
        ],
    }


@app.get("/api/tools")
async def list_tools() -> list[dict[str, Any]]:
    """列出可用的工具。给前端显示「我能干什么」用。

    为什么这个端点**故意不经过 get_agent()**？
        工具是从 agent/tools/ 目录扫出来的（registry.discover），
        跟哪个会话无关 —— 建一个 AgentLoop 实例只为拿工具名，
        会白白在内存里挂一整个会话。跟 /api/history /api/metrics 同一个理由：
        只读就别造状态。
    """
    reg = registry_mod.discover()
    # 把 MCP 工具也列出来 —— 否则「工具列表」和「模型实际能用的工具」会不一致，
    # 而界面上的列表是用户判断「为什么它不调工具」的唯一依据。
    mcp.attach(reg, _mcp_servers())
    out = []
    # 用 schema_list() 而不是遍历内部 dict：registry 的公开接口就是那几个，
    # 想加能力就加方法（get() 就是这么来的），别绕过封装去摸私有字段。
    for schema in reg.schema_list():
        fn = schema.get("function", {})
        props = (fn.get("parameters", {}) or {}).get("properties", {}) or {}
        out.append({
            "name": fn.get("name", ""),
            "description": (fn.get("description") or "").strip(),
            "params": list(props.keys()),
        })
    return sorted(out, key=lambda t: t["name"])


@app.get("/api/metrics")
async def metrics_summary() -> dict[str, Any]:
    """把 turns.jsonl 聚合成一份汇总。给前端的调试面板用。

    这里**故意不经过 get_agent()** —— 读指标不该在内存里造一个 AgentLoop。
    和 /api/history 同一个理由：只读磁盘就够。

    summarize() 目前还是骨架里的 NotImplementedError，
    全局异常处理器会把它转成 503 + 人话提示，不会甩 traceback。
    """
    return {"summary": metrics_mod.summarize(metrics_mod.read_all(limit=500))}


@app.get("/api/metrics/raw")
async def metrics_raw(limit: int = 50) -> dict[str, Any]:
    """最近 N 条原始账本。查单轮细节时用（比如「刚才那 17 秒花在哪」）。

    这个端点**故意保留原始行不做聚合** ——
    汇总会把三次工具调用平均成一个数字，而你想看的是「第三次调了多久」。
    指标系统的通病：只有平均值，诊断问题时不够用。留一个明细出口。
    """
    return {"rows": metrics_mod.read_all(limit=max(1, min(limit, 500)))}


@app.get("/api/memory")
async def read_memory() -> dict[str, Any]:
    mem = Memory()
    text = mem.load()
    return {"lines": len(text.splitlines()) if text else 0,
            "has_memory": text is not None}


@app.post("/api/memory")
async def write_memory(req: MemoryRequest) -> dict[str, Any]:
    agent = get_agent(req.session_id)
    if agent.memory is None:
        raise HTTPException(status_code=400, detail="这个会话没挂 long-term memory")
    agent.remember("- " + req.line)
    return {"ok": True, "line": req.line}


# ══════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════


def main() -> None:
    # 为什么要加 argparse？原来 `mini-agent-server --help` 会**直接启动服务器**，
    # 想看看这命令怎么用的人，得到的是一个跑起来的服务和一个占住的终端。
    # 2026-10-26 在 CI 上暴露出来：那条「两个入口命令都在」的检查
    # 用 `--help` 探测，结果卡到超时 —— 不是 CI 写错了，是这个命令本身的问题。
    import argparse

    parser = argparse.ArgumentParser(
        prog="mini-agent-server",
        description="mini-agent 的网页界面（FastAPI + 原生 JS），默认只监听本机。",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="监听地址，默认 127.0.0.1（只有本机能访问）",
    )
    parser.add_argument(
        "--port", type=int, default=8000,
        help="端口，默认 8000",
    )
    parser.add_argument("--version", action="version", version=f"mini-agent {VERSION}")
    args = parser.parse_args()

    # 先验配置 —— 和 cli.py 同一个理由：报错要在门口报，
    # 不要等用户敲完一句话才发现 Key 没配。
    try:
        llm.load_config()
    except (config_mod.ConfigError, llm.ConfigError) as exc:
        print("[配置错误]", exc)
        raise SystemExit(1)

    # 这个服务没有任何鉴权（见 README 的「已知局限」）。
    # 绑到非本机地址 = 把「能读你项目文件、能执行白名单命令」的 Agent
    # 开放给整个网络。默认值是安全的，改之前必须让人知道自己在做什么。
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("=" * 60)
        print("[警告] 这个服务没有任何鉴权。")
        print(f"       绑定到 {args.host} 意味着同网络内任何人都能用它")
        print("       读你项目里的文件、执行允许的命令。")
        print("       只在完全可信的网络里这么做。")
        print("=" * 60)

    print(f"mini-agent {VERSION}")
    print(f"  接口文档：http://{args.host}:{args.port}/docs")
    print(f"  聊天界面：http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
