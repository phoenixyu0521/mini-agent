"""命令行入口 —— 每周的验收工具。

用法：

    uv run mini-agent "你好"              单轮：问一句就退出（验 v0.1）
    uv run mini-agent                     交互模式，连续对话（验多轮记忆）
    uv run mini-agent --stream            交互模式 + 流式输出（验 v0.2）
    uv run mini-agent --tools "现在几点"   带工具：模型能自己调函数（验 v0.3）

    uv run mini-agent --sessions          第 3 周：列出最近的会话然后退出
    uv run mini-agent --resume 20261010-140032    恢复指定会话
    uv run mini-agent --new-session       强制开一个新会话
    uv run mini-agent --no-compact        关闭上下文自动压缩（看 token 一直涨）
    uv run mini-agent --no-memory         关闭长期记忆注入（对比有没有记忆的差别）

    uv run mini-agent --profiles          第 3 周第四块：列出 config.json 里的配置档案
    uv run mini-agent --profile deepseek  换一套配置跑（模型 / 超时 / max_tokens 全变）

第 2 周新增的是 `--tools`。它做两件事：
    1. 让 AgentLoop 去扫描 agent/tools/ 目录，把工具收进注册表
    2. 走 run_with_tools() 而不是 run()，也就是那套「两次请求」的循环

第 3 周新增的是会话持久化。默认行为：
    不带任何参数启动 → **自动沿用最近一个会话**，
    所以连敲两次 `python cli.py`，第二次会接着第一次聊。
    想断开历史用 `--new-session`；想接回更早的某次用 `--resume <编号>`。

第 3 周第四块新增的是配置化。模型名 / 超时 / max_tokens 搬去了 config.json：
    `--profiles` 看有哪些档案，`--profile <名字>` 换一套。
    API Key 仍然只在 .env 里（它是凭据，不进 Git）。

交互模式里还有这些内置命令：
    /reset             清空上下文（用来亲眼验证「记忆是 messages 列表」这件事）
    /history           查看当前会话的完整消息列表
    /tokens            看全量条数 vs 下一轮实际发送条数（看裁剪是否生效）
    /compact           看压缩过几次、最近一次省了多少
    /memory            查看长期记忆文件内容
    /remember <内容>   往长期记忆里写一条（立刻生效）
    /config            查看当前生效的配置（不显示 API Key）
    /exit              退出
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from agent import config as config_mod
from agent import context, llm
from agent.loop import DEFAULT_SYSTEM, AgentLoop
from agent.memory import Memory
from agent.session import Session


VERSION = "v0.7"


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
    parser.add_argument(
        "--no-compact",
        action="store_true",
        help="关闭上下文自动压缩（用来看 token 一路涨上去的样子）",
    )
    # ── 第 3 周后半段新增：长期记忆 ─────────────────────
    parser.add_argument(
        "--no-memory",
        action="store_true",
        help="关闭长期记忆注入（用来对比「有没有记忆」的差别）",
    )
    # ── 第 3 周第四块新增：配置化 ───────────────────────
    parser.add_argument(
        "--profile",
        metavar="NAME",
        default=None,
        help="用 config.json 里的哪套配置（见 --profiles）；不填则用 default",
    )
    parser.add_argument(
        "--profiles",
        action="store_true",
        help="列出 config.json 里所有可用的配置档案然后退出",
    )
    return parser.parse_args()


def _load_mcp_servers(profile: str | None) -> dict[str, Any]:
    """读 config.json 顶层的 mcpServers。

    配错了就当「没有 MCP」，而不是让 CLI 起不来 ——
    MCP 是可选能力，配置文件里一个逗号写错不该导致整个对话功能不可用。
    """
    try:
        return config_mod.resolve(profile).mcp_servers
    except (config_mod.ConfigError, llm.ConfigError):
        return {}


def main() -> None:
    args = parse_args()

    # ── --profiles：列完就退，也不需要 Key ────────────────
    # 和 --sessions 同一个道理：看「有哪些配置档案」是纯本地操作。
    if args.profiles:
        names = config_mod.list_profiles()
        if not names:
            print("config.json 里还没有任何 profile（或文件格式有问题）。")
            print(f"路径：{config_mod.CONFIG_PATH}")
            return
        try:
            raw = config_mod.load_raw()
            default_name = raw.get("default") or config_mod.DEFAULT_PROFILE
        except config_mod.ConfigError:
            default_name = "?"
        print(f"config.json 里的配置档案（默认：{default_name}）：")
        for name in names:
            mark = " ←默认" if name == default_name else ""
            print(f"  {name}{mark}")
        print("\n切换方式：uv run mini-agent --profile <名字>")
        return

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
        print("\n恢复某个会话：uv run mini-agent --resume <编号>")
        return

    # 开头就把配置验一遍。
    # 为什么不等到第一次提问才验？因为那时你已经敲完一句话，
    # 报错信息会和你的输入混在一起，看着像代码坏了。
    #
    # 第 3 周第四块：这里同时捕获两个 ConfigError ——
    #   config_mod.ConfigError 来自 config.json（profile 名拼错、JSON 格式坏）
    #   llm.ConfigError        来自 llm.py（历史遗留的同名类，见 config.py 注释）
    # 保留两个 try 是因为它们语义不同，合并要动 llm.py 的公开接口（第 4 周再统一）。
    try:
        llm.load_config(args.profile)
    except (config_mod.ConfigError, llm.ConfigError) as exc:
        print("[配置错误]", exc)
        sys.exit(1)
    except NotImplementedError as exc:
        # TODO ① / ② 还没补时走到这里。
        # 为什么不让它直接崩？因为那会甩出一大段 traceback，
        # 看起来像「整个项目坏了」，而其实只差两个函数没写。
        print("[配置模块还没写完]", exc)
        print("  提示：去补 agent/config.py 的两处 TODO，说明都写在注释里了。")
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
        auto_compact=not args.no_compact,
        memory=None if args.no_memory else Memory(),
        mcp_servers=_load_mcp_servers(args.profile) if args.tools else None,
    )

    # 恢复还是新建？只有读完才知道 —— 新建的 session 文件还不存在。
    restored = agent.load_session()
    if not restored:
        # 只有「没恢复成功」时才写开场行。
        # 刚 load 成功却再 start() 一次，会把文件清空覆盖掉 —— 这是个容易犯的错。
        session.start(agent.messages[0]["content"])

    # ── 第 3 周后半段：读长期记忆 ─────────────────────────
    # 它**不放进 self.messages**，而是单独存在 _memory_message 里，
    # 每次发请求时由 _view() 临时插进去。
    #
    # 为什么不直接 append 进 self.messages？
    #   因为 self.messages 是要落盘的会话历史。记忆是**背景资料**，
    #   不是这次对话说过的话 —— 混进去会让 session 文件变得很奇怪
    #   （打开一看第一行是记忆、后面才是对话）。
    #   而且 reset() 时也说不清该不该保留它。
    #   **让两个东西各归各位。**
    memory_on = agent.memory is not None
    if memory_on:
        agent.memory.ensure_exists()
        has_memory = agent.load_memory()
    else:
        has_memory = False

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
    # 第 3 周第四块：显示当前用的是哪套配置（不显示 Key）
    try:
        banner += "\n" + config_mod.resolve(args.profile).summary()
    except (config_mod.ConfigError, llm.ConfigError, NotImplementedError):
        pass  # 上面已经验过一次，理论上到不了这里
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
    if memory_on:
        st = agent.memory.stats()
        if has_memory:
            banner += f"\n长期记忆已加载：{st['lines']} 行（约 {st['est_tokens']} tokens）"
        else:
            banner += "\n长期记忆为空（用 /remember <内容> 写第一条）"
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
            est = context.estimate_tokens(agent.messages)
            view = agent._view()
            print(f"[{session.session_id}] 共 {len(agent.messages)} 条消息（约 {est} tokens）")
            if len(view) != len(agent.messages):
                print(f"  下一轮实际发送：{len(view)} 条（已裁剪）")
            for i, m in enumerate(agent.messages):
                c = (m.get("content") or "")[:50].replace("\n", " ")
                tag = " [tool_calls]" if m.get("tool_calls") else ""
                print(f"  {i:2d}. {m['role']:9s}{tag} {c}")
            continue
        if text == "/compact":
            if agent.compact_count == 0:
                print("[还没压缩过] 消息数未到水位线，或压缩失败了。")
            else:
                print(
                    f"[压缩过 {agent.compact_count} 次] "
                    f"最近一次省了约 {agent.last_compact_saving} tokens"
                )
            continue
        if text == "/tokens":
            est = context.estimate_tokens(agent.messages)
            print(f"全量 {len(agent.messages)} 条 ≈ {est} tokens；下一轮发送 {len(agent._view())} 条")
            continue
        # 第 3 周后半段：长期记忆的手动入口
        if text == "/memory":
            if not memory_on:
                print("[长期记忆已关闭]（启动时带了 --no-memory）")
                continue
            st = agent.memory.stats()
            if not st["exists"] or not st["lines"]:
                print("[记忆为空] 用 /remember <内容> 写第一条")
                continue
            print(f"[{st['path']}] {st['lines']} 行，{st['chars']} 字（约 {st['est_tokens']} tokens）")
            print(agent.memory.path.read_text(encoding="utf-8").strip())
            continue
        if text.startswith("/remember "):
            if not memory_on:
                print("[长期记忆已关闭]（启动时带了 --no-memory）")
                continue
            content = text[len("/remember "):].strip()
            agent.remember("- " + content)
            print(f"[已记入长期记忆] {content}")
            continue
        # 第 3 周第四块：看看现在生效的配置（安全 —— 不打印 Key）
        if text == "/config":
            try:
                conf = config_mod.resolve(args.profile)
                print(f"[当前配置] {conf.summary()}")
                print(f"  配置文件：{config_mod.CONFIG_PATH}")
                names = config_mod.list_profiles()
                print(f"  可用档案：{'、'.join(names)}（用 --profile <名字> 切换）")
            except (config_mod.ConfigError, llm.ConfigError) as exc:
                print(f"[配置错误] {exc}")
            except NotImplementedError as exc:
                print(f"[配置模块还没写完] {exc}")
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
