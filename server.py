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
from pathlib import Path
from typing import Any, Iterator

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from agent import config as config_mod
from agent import llm
from agent.loop import AgentLoop
from agent.memory import Memory
from agent.session import Session, new_session_id

WEB_DIR = Path(__file__).resolve().parent / "web"
VERSION = "v0.8"

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


def _build_agent(session_id: str) -> AgentLoop:
    """造一个接好 session / memory 的 AgentLoop。脚手架，已写好。

    里面的顺序和 cli.py 完全一样（复用同一套约定）：
        load_session() 成功 → 什么都不做（磁盘历史就是真相）
        失败（新会话）      → 才写开场行 start()
    这个「先读、读不到才写」的顺序不能反 —— 反了会把已有会话清空。
    """
    session = Session.load(session_id)
    agent = AgentLoop(with_tools=True, session=session, memory=Memory())
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
    raise NotImplementedError("TODO ② 还没补：get_agent() 会话池还没实现")


# ══════════════════════════════════════════════════════════════
#  请求体
# ══════════════════════════════════════════════════════════════


class ChatRequest(BaseModel):
    session_id: str
    message: str


class MemoryRequest(BaseModel):
    session_id: str
    line: str


# ══════════════════════════════════════════════════════════════
#  页面
# ══════════════════════════════════════════════════════════════


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


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
    agent = get_agent(req.session_id)

    # ─────────────────────────────────────────────────────────
    # TODO ③ —— 让这次调用「不堵住事件循环」
    #
    # 直接写   reply = agent.run(req.message)   是能跑的，但有问题：
    #   agent.run() 内部是同步的 httpx.post —— 它会卡住当前线程。
    #   FastAPI 跑在**单个事件循环**上，卡住它 = 所有其他请求都得排队，
    #   包括刷新页面这种跟对话无关的请求。多用户时就是「一个人说话，
    #   所有人卡住」。
    #
    # 解法：把同步函数丢进**线程池**，让事件循环继续转：
    #
    #     loop = asyncio.get_running_loop()
    #     reply = await loop.run_in_executor(None, agent.run, req.message)
    #
    #   ↑ run_in_executor 的三个参数：线程池（None = 用默认的）、
    #     要调的函数、函数的位置参数。它返回一个**可等待对象**，必须 await。
    #
    # 想一想：为什么这样就「不堵」了？
    #     提示：httpx.post 在**另一个线程**里等网络，
    #           事件循环那个线程就被空出来接待新请求了。
    # ─────────────────────────────────────────────────────────

    return {
        "session_id": req.session_id,
        "reply": reply.text if hasattr(reply, "text") else str(reply),
        "usage": agent.last_usage,
        "compact_count": agent.compact_count,
    }


# ══════════════════════════════════════════════════════════════
#  对话 —— 流式（前端默认走这条）
# ══════════════════════════════════════════════════════════════


@app.post("/api/stream")
async def stream(req: ChatRequest) -> StreamingResponse:
    """SSE 流式：每收到一小段就推给浏览器，做出打字机效果。

    为什么这个端点不用 run_in_executor？
        因为 StreamingResponse 收到**同步生成器**时，Starlette 自己会把它
        放进线程池迭代（内部叫 iterate_in_threadpool）——
        所以这里的阻塞同样不会卡住事件循环。这是框架帮我们做掉的。

    帧格式是 SSE 规定的：每条消息以 `data: ` 开头，以**两个换行**结束。
         data: {"text": "你"}
         <空行>
         data: {"text": "好"}
         <空行>
         data: [DONE]      ← 自定义的结束标记，前端收到就停止读取

    注意 get_agent() 是放在 generate() **里面**调的：
        这样万一它抛错（比如 TODO ② 还没补），错误会变成一条 SSE 消息
        送到前端显示，而不是变成一个干巴巴的 HTTP 503 ——
        前端只需要一套错误处理逻辑。
    """

    def generate() -> Iterator[str]:
        try:
            agent = get_agent(req.session_id)
            for piece in agent.stream(req.message):
                payload = json.dumps({"text": piece}, ensure_ascii=False)
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
    # 先验配置 —— 和 cli.py 同一个理由：报错要在门口报，
    # 不要等用户敲完一句话才发现 Key 没配。
    try:
        llm.load_config()
    except (config_mod.ConfigError, llm.ConfigError) as exc:
        print("[配置错误]", exc)
        raise SystemExit(1)

    print(f"mini-agent {VERSION}")
    print("  接口文档：http://127.0.0.1:8000/docs")
    print("  聊天界面：http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
