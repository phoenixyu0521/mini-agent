"""第二个工具 —— 读文件。

比 get_time 难在哪？

    get_time  就算错了，最多返回一个错的时间 —— 无所谓。
    read_file 一旦写错，模型就能读到你**本不该看到的东西**：

        ../../../../Users/你/.env      ← 你的 API Key 就在里面
        C:/Windows/System32/config/SAM

    所以这个工具真正的考点不是「怎么读文件」，是**怎么拒绝**。
    路径校验写对了，这个工具才敢上线；写错了，它就是个定时炸弹。

## 为什么路径要限制在项目目录内？

因为**路径是模型生成的，不是你敲的**。

    你敲 `read_file("README.md")`      → 你知道自己在读什么
    模型回 `read_file("../../.env")`   → 模型只是「觉得」你可能想看

    工具层必须假设：**每一个参数都可能有恶意**。这是安全的第一原则。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.tools.base import Tool


# 允许读取的根目录 = 项目根（mini-agent/）。
# parents[0] 是 agent/tools/，parents[1] 是 agent/，parents[2] 是 mini-agent/。
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 单次最多读多少字节。防止模型要读一个 500MB 的日志文件把内存撑爆、
# 或者把几万行内容全塞进对话把 token 烧光。
DEFAULT_MAX_BYTES = 4000

# ─────────────────────────────────────────────────────────────────
# 敏感目标：项目**里面**也不能读的东西。
#
# 为什么光有「限制在项目目录内」还不够？
# 因为 .env（你的 API Key）本来就在项目目录里。
# 路径校验回答的是「能不能跑到项目外」，但 .env 根本没跑出去 ——
# 它一直堂堂正正地待在 mini-agent/.env。模型只要 read_file(".env") 就拿到了。
#
# 所以要**两层**：
#   第一层  路径限制在项目内     → 防「往外跑」（你已经写好了）
#   第二层  敏感目标拒绝         → 防「项目内的敏感文件」（这一层）
#
# 又是同一个道理：白名单管「允许什么」永远列不全，
# 所以关键凭据要有一份**显式的拒绝清单**。
# ─────────────────────────────────────────────────────────────────
DENIED_DIR_NAMES = {
    ".git",        # 仓库历史与远程地址
    ".venv",       # 虚拟环境，几百个文件
    "__pycache__", # 编译缓存，无意义
    "node_modules",
}

DENIED_FILE_NAMES = {
    ".env",
    "id_rsa",
    "id_ed25519",
    "credentials",
    "secrets",
}

# 按后缀拦：不管文件名是什么，只要是这个类型就拒。
DENIED_SUFFIXES = {
    ".key",        # 私钥
    ".pem",
    ".pfx",
    ".p12",
}


class ReadFileTool(Tool):
    name = "read_file"

    description = (
        "读取项目目录内的文本文件并返回内容。"
        "当用户让你看某个文件、读某段配置、总结某个文档时调用。"
        "只能读取项目目录内的文件，不支持写文件。"
    )

    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径，相对项目根目录。例如 README.md 或 agent/llm.py",
            },
            "max_bytes": {
                "type": "integer",
                "description": f"最多读取的字节数，默认 {DEFAULT_MAX_BYTES}",
            },
        },
        # path 是必填的 —— 没有路径就不知道读什么。
        "required": ["path"],
    }

    def run(self, path: str, max_bytes: int = DEFAULT_MAX_BYTES, **_: Any) -> str:
        # ─────────────────────────────────────────────────────────
        # TODO ① —— 校验路径必须在项目目录内
        #
        #   为什么这是**安全**而不是「功能」？
        #   校验没写对 = 你的 API Key、你的私人文件对模型完全敞开。
        #   而 path 是模型生成的字符串，你不知道它会写出什么。
        #
        #   要做的事：把相对路径解析成绝对路径，然后判断它有没有**逃出**
        #            PROJECT_ROOT。逃出去了就返回一个错误字符串（不要抛异常，
        #            让模型看到错误后自己换个路径）。
        #
        #   提示：`Path.resolve()` 会把 ".." 消掉并变成绝对路径 ——
        #          也就是说 resolve 之后的路径里已经没有 ".." 了，
        #          但它可能指向项目外。所以要靠**前缀比较**来判断。
        #
        #   边界情况提醒：`str(p).startswith(str(ROOT))` 这种写法有个坑 ——
        #                  项目目录是 D:/proj 时，文件 D:/proj_secret/x 也会 "startswith" 通过。
        #                  想稳一点，用 Path 的相对路径关系来判断
        #                  （想想 `is_relative_to` 这个方法在哪个版本可用，
        #                    本项目 requires-python >= 3.11）。
        #
        #   别忘了再检查两件事：
        #     - 这个路径是不是目录？是目录的话读不了，要给出说明
        #     - 存不存在？不存在的话返回说明，不要让文件操作抛异常
        #
        #   ⚠️ 下面这行「未实现」的 return 是**故意留的安全闸**：
        #      校验逻辑没写出来之前，一律拒绝读任何文件。
        #      这叫 fail-closed（默认关闭）—— 反过来（默认放行）叫 fail-open，
        #      意味着你少写一行代码就直接造成信息泄露。
        #      **安全相关的默认值，永远选「拒绝」那一侧。**
        #      补完校验后，把这一行删掉。
        # ─────────────────────────────────────────────────────────
        # 1) 把模型给的相对路径，拼到项目根目录下，再"打平"成绝对路径
        raw_path = str(path).strip()
        if not raw_path:
            return "[错误] path 是空的，请告诉我读哪个文件（例如 README.md）。"

        target = (PROJECT_ROOT / raw_path).resolve()

        # 2) 关键一步：判断它有没有逃出项目目录
        if not target.is_relative_to(PROJECT_ROOT):
            return (
                f"[错误] 路径 {raw_path} 超出了项目目录，我只能读项目里的文件。"
                f"请给我一个项目内的相对路径（例如 README.md）。"
            )

        # 3) 顺带挡一下目录本身（目录读不了，当文件读会报错）
        if target.is_dir():
            return f"[错误] {raw_path} 是一个目录，不能直接读取。"

        # 4) 第二层防护：项目内的敏感目标一律拒绝。
        #    .env 就在项目里，路径校验挡不住它 —— 只能靠显式拒绝。
        #    先看路径的**任意一段目录名**（parts）是不是在拒绝目录里。
        for part in target.parts:
            if part.lower() in DENIED_DIR_NAMES:
                return f"[错误] {raw_path} 位于受保护目录 {part}/ 内，我不能读取。"

        # 再看文件名本身
        if target.name.lower() in DENIED_FILE_NAMES:
            return f"[错误] {raw_path} 是敏感文件（可能含凭据），我不能读取。"

        # 最后看后缀（.key / .pem / .pfx / .p12）
        if target.suffix.lower() in DENIED_SUFFIXES:
            return f"[错误] {raw_path} 是 {target.suffix} 类型的密钥文件，我不能读取。"

        # ---- 以下是校验通过之后的读文件逻辑，已写完整 ----

        if not target.exists():
            return f"[错误] 文件不存在：{path}"

        try:
            raw = target.read_bytes()
        except PermissionError:
            return f"[错误] 没有权限读取：{path}"
        except Exception as exc:  # noqa: BLE001
            return f"[错误] 读取失败：{type(exc).__name__}: {exc}"

        limit = int(max_bytes or DEFAULT_MAX_BYTES)
        truncated = len(raw) > limit
        chunk = raw[:limit]

        # 编码问题：Windows 上的中文文件可能是 GBK，也可能是 UTF-8。
        # 先试 UTF-8，失败再退到 GBK。都失败就用 errors="ignore" 硬解，
        # 宁可显示得难看，也别崩掉整轮对话。
        for encoding in ("utf-8", "gbk"):
            try:
                text = chunk.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = chunk.decode("utf-8", errors="ignore")

        header = f"[{path}] 共 {len(raw)} 字节"
        if truncated:
            header += f"，已截断到前 {limit} 字节"
        return f"{header}\n{text}"
