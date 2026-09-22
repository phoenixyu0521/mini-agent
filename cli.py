"""命令行入口 —— 第 1 周的验收工具。

三种用法：

    uv run python cli.py "你好"        单轮：问一句就退出（验 v0.1）
    uv run python cli.py               交互模式，连续对话（验多轮记忆）
    uv run python cli.py --stream      交互模式 + 流式输出（验 v0.2）

交互模式里还有两条内置命令：
    /reset   清空上下文（用来亲眼验证「记忆是 messages 列表」这件事）
    /exit    退出
"""

from __future__ import annotations

import argparse
import sys

from agent import llm
from agent.loop import AgentLoop


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="mini-agent v0.2 —— 命令行多轮对话",
    )
    # nargs="*" 表示「可以有零个或多个词」，不填就进入交互模式
    parser.add_argument("prompt", nargs="*", help="单次提问；不填则进入交互模式")
    parser.add_argument("--stream", action="store_true", help="流式输出，文字一个个蹦")
    parser.add_argument("--system", default=None, help="覆盖默认的 system 提示词")
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

    agent = AgentLoop(args.system) if args.system else AgentLoop()

    # ---- 单轮模式 ----
    if args.prompt:
        text = " ".join(args.prompt)
        if args.stream:
            print("AI > ", end="", flush=True)
            agent.run_stream(text)
        else:
            reply = agent.run(text)
            print("AI >", reply)
            print(llm.format_usage(agent.last_usage))
        return

    # ---- 交互模式 ----
    print("mini-agent v0.2  |  /reset 清空上下文  |  /exit 退出")
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
            if args.stream:
                print("AI > ", end="", flush=True)
                agent.run_stream(text)
            else:
                reply = agent.run(text)
                print("AI >", reply)
                print("    " + llm.format_usage(agent.last_usage))
        except Exception as exc:  # noqa: BLE001
            # 单轮失败不该让整个会话崩掉 —— 这一条到第 2 周会更重要，
            # 因为那时模型会开始返回坏 JSON。
            print("\n[出错]", exc)


if __name__ == "__main__":
    main()
