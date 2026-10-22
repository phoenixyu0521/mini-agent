"""工具结果落盘 —— 大结果不进上下文，只留一段预览和一条路径。

## 这个文件解决什么问题

工具返回的是一个**字符串**，它会原样变成一条 `role="tool"` 消息进 `self.messages`。
在这个文件出现之前，它**没有长度上限**：

    模型想读一个 300KB 的日志 → 30 万字符整段进上下文
    下游压缩也救不了 —— KEEP_RECENT=8 的保留区原样发送，
    这条巨型消息只要还在最近 8 条里，就一条字符都压不掉
    实测 `compact()` 压 16 条只省 7 token

后果不是「慢一点」，是 HTTP 400 context_length_exceeded ——
而且报错的那一轮往往不是罪魁祸首的那一轮，最难查。

## 为什么是「落盘」而不是「截断」

    截断 = 把中间删了，模型永远拿不回来，它也不知道自己缺了什么
           → 它会基于残缺信息自信作答（编造的最爱）
    落盘 = 数据不在上下文里，但**在磁盘上**，模型知道缺什么，能自己去取

这就是 nanobot 里那个函数叫 `maybe_persist_tool_result` 而不是 `truncate` 的原因。
一字之差，是「数据丢了」和「数据在外面」的区别。

## 为什么文件名用内容哈希

同一个工具、同样的输出 → 同一个文件名 → 重复写等于没写。
这带来两个白拿的好处：
    1. 幂等：模型重试一次工具，不会在磁盘上堆出两份 30 万字符的文件
    2. 可去重：同一段内容被两个工具返回，只存一份

## 为什么放在这里、而不是放在 registry.call() 里

`registry.call()` 的职责是「执行并返回结果」，它对「这份结果要不要进上下文」一无所知 ——
那是循环层的决定。把落盘塞进 call()，等于让工具层开始关心上下文预算。

所以位置选在**结果进入 messages 之前的最后一站**：`AgentLoop._execute_tool_calls()`。
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

# 项目根 = mini-agent/。parents[0] 是 agent/，parents[1] 是 mini-agent/。
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 落盘目录。data/ 已经在 .gitignore 里 —— 这些是运行产物，不是源码。
STORE_DIR = PROJECT_ROOT / "data" / "tool_results"

# 超过这么多字符就不进上下文。
#
# 为什么是 8000？
#   经验值 1 token ≈ 2 字符 → 8000 字符 ≈ 4000 tokens，
#   约占预算（128k 窗口算下来 117808）的 3%。
#   一条工具结果吃掉 3% 是合理的；吃掉 50% 就是事故。
DEFAULT_INLINE_LIMIT = 8000

# 落盘后上下文里留下多少。
# 为什么是「头 + 尾」而不是「只留头」？
#   工具输出（尤其命令输出、网页）的有用信息常常在**末尾**：
#   报错在最后几行、结论在最后一段。只留头等于把答案扔了。
HEAD_CHARS = 1500
TAIL_CHARS = 1500

# 允许用环境变量临时改阈值 —— 调参不用改代码。
ENV_LIMIT = "MINI_AGENT_TOOL_RESULT_MAX_CHARS"


def inline_limit() -> int:
    """当前生效的阈值。

    为什么读取失败要静默退回默认值，而不是抛异常？
        这个函数在**每一次工具返回**时都会被调用。为一个环境变量写错
        （比如手滑写成 `8000字符`）让整轮对话崩掉，代价远大于收益。
        配置类的东西，能降级就降级 —— 和 config.py 里的预算推导同一个道理。
    """
    raw = os.getenv(ENV_LIMIT, "").strip()
    if not raw:
        return DEFAULT_INLINE_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_INLINE_LIMIT
    return value if value > 0 else DEFAULT_INLINE_LIMIT


def _slug(name: str) -> str:
    """把工具名变成安全的文件名片段。

    为什么要消毒？因为工具名可能来自 MCP server 的配置（外部输入），
    里面出现 `../` 或 `:` 就变成一个路径穿越洞 —— 而这正是本项目
    第 2 周那条铁律：**参数是模型/外部给的，必须假设它可能有恶意**。
    """
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", (name or "").strip())[:32].strip("_")
    return slug or "tool"


def store_path(tool_name: str, text: str, base_dir: Path | None = None) -> Path:
    """算出这份结果该存到哪。**同内容必然同路径**（见模块开头）。"""
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return (base_dir or STORE_DIR) / f"{_slug(tool_name)}-{digest}.txt"


def _display_path(path: Path) -> str:
    """给模型看的路径。

    为什么优先给**相对路径**？
        因为 read_file 只接受「相对项目根」的路径。给绝对路径，
        模型照着抄回来会被路径校验拦下，然后它就开始瞎猜别的路径 ——
        一个「能读回」的设计，会在这一步变成「读不回来」。
    """
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def _preview(text: str, path: Path) -> str:
    """落盘后在上下文里留的那段替代文本。

    三段信息缺一不可：
        1. 原始长度 —— 让模型知道自己看到的是残片，不是全文
        2. 文件路径 —— 让模型能自己决定要不要去取
        3. 怎么取     —— 不说清楚 offset 从哪开始，它多半会直接读一次全文，
                        然后又是一个 30 万字符，白落盘
    """
    head = text[:HEAD_CHARS]
    tail = text[-TAIL_CHARS:]
    omitted = len(text) - len(head) - len(tail)
    return (
        f"[工具结果过大，已落盘]\n"
        f"原始长度：{len(text)} 字符，其中中间 {omitted} 字符没有放进上下文。\n"
        f"完整内容：{_display_path(path)}\n\n"
        f"--- 开头 {len(head)} 字符 ---\n{head}\n"
        f"--- 结尾 {len(tail)} 字符 ---\n{tail}\n"
        f"[需要中间部分时，用 read_file 分段读取这个文件："
        f"path=\"{_display_path(path)}\"，offset 从 0 开始，每次读 4000 字节。]"
    )


def maybe_persist(
    tool_name: str,
    text: str,
    *,
    base_dir: Path | None = None,
    max_inline: int | None = None,
) -> str:
    """结果不长就原样返回；太长就落盘，返回预览。

    注意它**绝不抛异常**。理由和 `registry.call()` 吞异常是同一条：
    一个「省 token」的优化，不该有能力让整轮对话崩掉。
    写盘失败就退化成「头 + 尾 + 明确告知中间丢了」——
    仍然比无声地超发 30 万字符好。
    """
    if not isinstance(text, str):
        return text

    limit = max_inline if max_inline is not None else inline_limit()
    if len(text) <= limit + 10**9:
        return text

    path = store_path(tool_name, text, base_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 同内容已经在盘上就别再写一遍 —— 幂等在这里是白拿的。
        if not path.exists():
            # newline="" 关掉换行转换：Windows 上默认会把 \n 改成 \r\n，
            # 那样文件内容就和内存里的字符串不一致了（长度、哈希、读回全对不上）。
            path.write_text(text, encoding="utf-8", newline="")
    except OSError as exc:
        head = text[:HEAD_CHARS]
        tail = text[-TAIL_CHARS:]
        return (
            f"[工具结果过大，且落盘失败：{type(exc).__name__}: {exc}]\n"
            f"原始长度：{len(text)} 字符。\n\n"
            f"--- 开头 {len(head)} 字符 ---\n{head}\n"
            f"--- 结尾 {len(tail)} 字符 ---\n{tail}\n"
            f"[中间 {len(text) - len(head) - len(tail)} 字符已丢失，"
            f"如确需完整内容，请换一种方式（例如缩小查询范围）重新获取。]"
        )

    return _preview(text, path)
