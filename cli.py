"""命令行入口 —— 每周的验收工具。

用法：

    uv run python cli.py "你好"              单轮：问一句就退出（验 v0.1）
    uv run python cli.py                     交互模式，连续对话（验多轮记忆）
    uv run python cli.py --stream            交互模式 + 流式输出（验 v0.2）
    uv run python cli.py --tools "现在几点"   带工具：模型能自己调函数（验 v0.3）

`--tools` 是本文件第 2 周新增的唯一东西。它做两件事：
    1. 让 AgentLoop 去扫描 agent/tools/ 目录，把工具收进注册表
    2. 走 run_with_tools() 而不是 run()，也就是那套「两次请求」的循环

交互模式里还有两条内置命令：
    /reset   清空上下文（用来亲眼验证「记忆是 messages 列表」这件事）
    /exit    退出
"""

from __future__ import annotations

import argparse
import sys

from agent import llm
from agent.loop import DEFAULT_SYSTEM, AgentLoop


VERSION = "v0.3"


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
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # 开头就把配置验一遍。
    # 为什么不等到第一次提问才验？因为那时你已经敲完一句话，
    # 报错信息会和你的输入混在一起，看着像代码坏了。
    try:
        llm.load_config()
    except llm.ConfigError as exc:
        print("[配置错误]", exc)
        sys.exit(1)

    agent = AgentLoop(args.system or DEFAULT_SYSTEM, with_tools=args.tools)

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
        return

    # ---- 交互模式 ----
    banner = f"mini-agent {VERSION}  |  /reset 清空上下文  |  /exit 退出"
    if agent.tools is not None:
        names = agent.tools.names()
        banner += f"\n工具已启用（{len(names)} 个）：{'、'.join(names)}"
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
