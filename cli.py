"""命令行入口 —— 每周的验收工具。

用法：

    uv run python cli.py "你好"              单轮：问一句就退出（验 v0.1）
    uv run python cli.py                     交互模式，连续对话（验多轮记忆）
    uv run python cli.py --stream            交互模式 + 流式输出（验 v0.2）
    uv run python cli.py --tools "现在几点"   带工具：模型能自己调函数（验 v0.3）

    uv run python cli.py --sessions          第 3 周：列出最近的会话然后退出
    uv run python cli.py --resume 20261010-140032    恢复指定会话
    uv run python cli.py --new-session       强制开一个新会话

第 2 周新增的是 `--tools`。它做两件事：
    1. 让 AgentLoop 去扫描 agent/tools/ 目录，把工具收进注册表
    2. 走 run_with_tools() 而不是 run()，也就是那套「两次请求」的循环

第 3 周新增的是会话持久化。默认行为：
    不带任何参数启动 → **自动沿用最近一个会话**，
    所以连敲两次 `python cli.py`，第二次会接着第一次聊。
    想断开历史用 `--new-session`；想接回更早的某次用 `--resume <编号>`。

交互模式里还有三条内置命令：
    /reset   清空上下文（用来亲眼验证「记忆是 messages 列表」这件事）
    /history 查看当前会话的完整消息列表
    /exit    退出
"""

from __future__ import annotations

import argparse
import sys

from agent import llm
from agent.loop import DEFAULT_SYSTEM, AgentLoop
from agent.session import Session


VERSION = "v0.4"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description=f"mini-agent {VERSION} —— 命令行 Agent",
    )
    # nargs="*" 表示「可以有零个或多个词」，不填就进入交互模式
    parser.add_argument("prompt", nargs="*", help="单次提问；不填则进入交互模式")
    parser.add_argument("--stream", action="store_true", help="流式输出，文字一个个蹦")
    parser.add_argument("--system", default=None, help="覆盖默认的 system 提示词")
    # 第 2 周新增。它和 --stream 不能一起用：
    # 工具调用必须先拿到完整的 tool_calls 才知道下一步干什么，没法边收边打印。
    parser.add_argument(
        "--tools",
        action="store_true",
        help="启用工具，模型能自己调函数（与 --stream 互斥，同时给以 --tools 为准）",
    )
    # ── 第 3 周新增 ─────────────────────────────────────
    parser.add_argument(
        "--new-session",
        action="store_true",
        help="强制开一个新会话（不读旧的历史）",
    )
    parser.add_argument(
        "--resume",
        metavar="ID",
        default=None,
        help="恢复指定编号的会话，编号见 --sessions",
    )
    parser.add_argument(
        "--sessions",
        action="store_true",
        help="列出最近的会话然后退出",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # ── --sessions：列完就退，不需要 Key，也不需要联网 ──────
    # 为什么放在 load_config() 之前？
    #   因为「看我有哪几次对话」是个纯本地操作，不该因为 Key 没配好就用不了。
    #   这类「不依赖外部资源的入口」应该最先响应 —— 这是很好的分层习惯。
    if args.sessions:
        ids = Session.list_sessions()
        if not ids:
            print("还没有任何会话记录。用不带 --sessions 的方式启动一次就有了。")
            return
        print(f"最近的会话（{len(ids)} 个）：")
        for sid in ids:
            info = Session(sid).stats()
            if not info["exists"]:
                continue
            detail = "、".join(f"{r} {n}" for r, n in sorted(info["roles"].items()))
            print(f"  {info['id']}  {info['created_at']}  {info['message_count']} 条（{detail}）")
        print("\n恢复某个会话：uv run python cli.py --resume <编号>")
        return

    # 开头就把配置验一遍。
    # 为什么不等到第一次提问才验？因为那时你已经敲完一句话，
    # 报错信息会和你的输入混在一起，看着像代码坏了。
    try:
        llm.load_config()
    except llm.ConfigError as exc:
        print("[配置错误]", exc)
        sys.exit(1)

    # ── 第 3 周：决定用哪个 session ───────────────────────
    # 三条路径，优先级从高到低：
    #   --resume <id>   打开指定的旧会话
    #   --new-session   强制开新的
    #   （都不给）      默认沿用最近的一个，没有就新建
    #
    # 「默认沿用最近一个」是刻意的：这样 `python cli.py` 连敲两次，
    # 第二次会自动接上第一次 —— 正是我们要演示的效果。
    if args.resume:
        session = Session.load(args.resume)
    elif args.new_session:
        session = Session.new()
    else:
        recent = Session.list_sessions(limit=1)
        session = Session.load(recent[0]) if recent else Session.new()

    agent = AgentLoop(
        args.system or DEFAULT_SYSTEM,
        with_tools=args.tools,
        session=session,
    )

    # 恢复还是新建？只有读完才知道 —— 新建的 session 文件还不存在。
    restored = agent.load_session()
    if not restored:
        # 只有「没恢复成功」时才写开场行。
        # 刚 load 成功却再 start() 一次，会把文件清空覆盖掉 —— 这是个容易犯的错。
        session.start(agent.messages[0]["content"])

    # ---- 单轮模式 ----
    if args.prompt:
        text = " ".join(args.prompt)
        if args.tools:
            print("AI >", agent.run_with_tools(text))
            print(llm.format_usage(agent.last_usage))
        elif args.stream:
            print("AI > ", end="", flush=True)
            agent.run_stream(text)
        else:
            reply = agent.run(text)
            print("AI >", reply)
            print(llm.format_usage(agent.last_usage))
        # 第 3 周：单轮也要落盘。否则 `cli.py "记住我"` 退出后什么都没留下，
        # 会话持久化就成了「只有交互模式能用」的功能，那是设计失误。
        agent.flush_session()
        print(f"[已存入会话 {session.session_id}]")
        return

    # ---- 交互模式 ----
    banner = f"mini-agent {VERSION}  |  /reset 清空上下文  |  /exit 退出"
    if restored:
        info = session.stats()
        banner += (
            f"\n已恢复上次对话：{info['message_count']} 条"
            f"（{info['created_at']} 开始）—— 它还记得你"
        )
    if agent.tools is not None:
        names = agent.tools.names()
        banner += f"\n工具已启用（{len(names)} 个）：{'、'.join(names)}"
    banner += f"\n本轮对话将存为：{session.session_id}"
    print(banner)

    while True:
        try:
            text = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not text:
            continue
        if text in ("/exit", "/quit"):
            break
        if text == "/reset":
            agent.reset()
            print("[已清空上下文 —— 现在它又不记得你了]")
            continue
        # 第 3 周新增：/history 在当前会话里问「我聊了些什么」
        if text == "/history":
            print(f"[{session.session_id}] 共 {len(agent.messages)} 条消息")
            for i, m in enumerate(agent.messages):
                c = (m.get("content") or "")[:50].replace("\n", " ")
                print(f"  {i:2d}. {m['role']:9s} {c}")
            continue

        try:
            if args.tools:
                print("AI >", agent.run_with_tools(text))
                print("    " + llm.format_usage(agent.last_usage))
            elif args.stream:
                print("AI > ", end="", flush=True)
                agent.run_stream(text)
            else:
                reply = agent.run(text)
                print("AI >", reply)
                print("    " + llm.format_usage(agent.last_usage))
        except Exception as exc:  # noqa: BLE001
            # 单轮失败不该让整个会话崩掉 —— 这一条到第 2 周会更重要，
            # 因为那时模型会开始返回坏 JSON、工具也可能会失败。
            print("\n[出错]", exc)


if __name__ == "__main__":
    main()
