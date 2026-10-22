"""工具结果落盘（第 6 周 · 可靠性 ①）的测试。

这些测试全离线：不联网、不调模型、不依赖 .env。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent import llm, tool_results
from agent.loop import AgentLoop
from agent.tools import registry
from agent.tools.base import Tool
from agent.tools.read_file import PROJECT_ROOT, ReadFileTool


# ── 一个会吐出巨大结果的假工具 ──────────────────────────────
class BigTool(Tool):
    """模拟「读了一个 30 万字符的日志」这种工具。

    为什么用假工具而不是真去读一个大文件？
        因为要测的是**循环层的策略**（结果要不要落盘），
        不是文件系统。假工具让「结果有多长」这个变量完全可控。
    """

    name = "big_tool"
    description = "返回一大段内容"
    concurrency_safe = True

    def __init__(self, size: int = 20_000) -> None:
        self.size = size

    def run(self, **_: object) -> str:
        # 用带编号的内容，才能断言「头尾留住了、中间确实没进上下文」
        return "".join(f"行{i:05d}\n" for i in range(self.size // 8))


# ── 基础行为 ────────────────────────────────────────────────
def test_small_result_passes_through(tmp_path: Path) -> None:
    """短结果原样返回，而且**不建目录**。

    为什么要断言「不建目录」？
        因为「落盘」是有代价的（一次写盘 + 一个文件）。
        小结果也落盘会让 data/ 里堆满几百个几百字的碎片文件，
        而那些本来就不占上下文 —— 优化只该作用在真的有问题的地方。
    """
    text = "现在是 2026年10月22日"
    out = tool_results.maybe_persist("get_time", text, base_dir=tmp_path / "store")
    assert out == text
    assert not (tmp_path / "store").exists()


def test_large_result_is_persisted(tmp_path: Path) -> None:
    """大结果落盘，且**文件内容与原文逐字节一致**。

    这是整个设计的地基：如果存下来的东西和模型看到的不一样，
    「可读回」就是假的。
    """
    text = "甲" * 30_000
    out = tool_results.maybe_persist("big_tool", text, base_dir=tmp_path, max_inline=8000)

    path = tool_results.store_path("big_tool", text, base_dir=tmp_path)
    assert path.exists()
    assert path.read_text(encoding="utf-8") == text
    assert out != text


def test_preview_keeps_head_and_tail(tmp_path: Path) -> None:
    """预览必须同时留住开头和结尾，并说清「中间没进来」。"""
    text = "头" * 1500 + "中" * 8000 + "尾" * 1500
    out = tool_results.maybe_persist("big_tool", text, base_dir=tmp_path, max_inline=8000)

    assert "头" * 100 in out
    assert "尾" * 100 in out
    assert "中" * 100 not in out          # 中间真的没进上下文
    assert "11000" in out                  # 原始长度写清楚了
    assert "read_file" in out              # 告诉它怎么取回来


def test_exactly_at_limit_passes_through(tmp_path: Path) -> None:
    """正好等于阈值时不落盘。

    这条锁的是边界语义：判据是 `len(text) <= limit`，不是 `<`。
    写反了不会报错，只会在「刚好 8000 字符」这个点上多存一个文件 ——
    而这类差一错误，只有边界用例抓得住。
    """
    text = "x" * 8000
    store = tmp_path / "store"
    assert tool_results.maybe_persist("big_tool", text, base_dir=store, max_inline=8000) == text
    assert not store.exists()


def test_same_content_writes_once(tmp_path: Path) -> None:
    """同内容 → 同文件名，写两次只有一个文件。

    为什么等价于「幂等」？
        模型重试一次工具是很常见的（超时、它自己不满意）。
        如果每次落盘都新建一个 30 万字符的文件，
        跑一天数据目录就能涨到几个 G，而里面全是同一份内容。
    """
    text = "x" * 20_000
    tool_results.maybe_persist("big_tool", text, base_dir=tmp_path, max_inline=8000)
    tool_results.maybe_persist("big_tool", text, base_dir=tmp_path, max_inline=8000)

    files = list(tmp_path.glob("*.txt"))
    assert len(files) == 1


def test_write_failure_degrades_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写盘失败不能崩，要退化并**明确告知中间丢了**。

    为什么这条比「正常落盘」更重要？
        落盘是优化。优化失败却让整轮对话崩掉，是把收益和代价搞反了。
        但也不能静默 —— 模型以为自己在看全文，就会基于残片编。
    """

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    out = tool_results.maybe_persist(
        "big_tool", "y" * 20_000, base_dir=tmp_path, max_inline=8000
    )

    assert "落盘失败" in out
    assert "已丢失" in out
    assert len(out) < 20_000


def test_tool_name_cannot_escape_directory(tmp_path: Path) -> None:
    """工具名里的 `../` 不能变成路径穿越。

    这条不是洁癖：工具名可以来自 MCP server 的配置，也就是**外部输入**。
    和第 2 周 read_file 的路径校验是同一条铁律 —— 参数可能有恶意。
    """
    path = tool_results.store_path("../../evil", "内容", base_dir=tmp_path)
    assert path.parent == tmp_path
    assert ".." not in path.name


def test_env_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """阈值可以用环境变量调；写错了退回默认值而不是崩。"""
    monkeypatch.setenv(tool_results.ENV_LIMIT, "1234")
    assert tool_results.inline_limit() == 1234

    monkeypatch.setenv(tool_results.ENV_LIMIT, "1234字符")
    assert tool_results.inline_limit() == tool_results.DEFAULT_INLINE_LIMIT

    monkeypatch.setenv(tool_results.ENV_LIMIT, "-5")
    assert tool_results.inline_limit() == tool_results.DEFAULT_INLINE_LIMIT


# ── 接进循环：这是真正会被线上走到的那条路 ──────────────────
def test_loop_persists_before_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """串行分支：工具结果进 messages 之前就被替换成预览。"""
    monkeypatch.setattr(tool_results, "STORE_DIR", tmp_path)

    loop = AgentLoop(with_tools=False)
    loop.tools = registry.ToolRegistry()
    loop.tools.register(BigTool())

    results = loop._execute_tool_calls(
        [llm.ToolCall(id="c1", name="big_tool", arguments="{}")]
    )

    assert len(results) == 1
    assert "已落盘" in results[0]
    assert len(results[0]) < 10_000
    assert len(list(tmp_path.glob("*.txt"))) == 1


def test_loop_persists_in_parallel_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并行分支也要落盘。

    为什么要单独测一遍？
        因为这里改的是**两个 return 路径**：串行那条和线程池那条。
        只测串行，并行分支的 return 万一漏改，测试仍然全绿 ——
        而那正是「一次点两个只读工具」的常见情形。
    """
    monkeypatch.setattr(tool_results, "STORE_DIR", tmp_path)

    loop = AgentLoop(with_tools=False)
    loop.tools = registry.ToolRegistry()
    loop.tools.register(BigTool())

    results = loop._execute_tool_calls(
        [
            llm.ToolCall(id="c1", name="big_tool", arguments="{}"),
            llm.ToolCall(id="c2", name="big_tool", arguments="{}"),
        ]
    )

    assert len(results) == 2
    assert all("已落盘" in r for r in results)
    # 两个调用的内容相同 → 哈希相同 → 只该有一个文件（幂等）
    assert len(list(tmp_path.glob("*.txt"))) == 1


# ── 读回来：落盘的另一半 ────────────────────────────────────
def test_read_file_offset_returns_middle_segment() -> None:
    """read_file 的 offset 能读到文件的**中段**，并提示下一段从哪继续。"""
    tool = ReadFileTool()
    raw = (PROJECT_ROOT / "README.md").read_bytes()
    out = tool.run(path="README.md", offset=100, max_bytes=50)

    header, _, body = out.partition("\n")
    assert "本次返回第" in header
    assert "继续读请用 offset=" in header
    # 关键断言：返回的文本**逐字节存在于原文件里**。
    # 如果段边界切坏了汉字、走了 GBK 分支，这里会解出一串原文里没有的乱码。
    assert body.encode("utf-8") in raw
    assert not raw.startswith(body.encode("utf-8")[:8])   # 确实不是从头读的


def test_read_file_offset_beyond_end() -> None:
    """offset 越界要给可执行建议，而不是抛 IndexError。"""
    out = ReadFileTool().run(path="README.md", offset=999_999_999, max_bytes=10)
    assert out.startswith("[错误]")
    assert "offset=" in out


def test_persisted_result_can_be_read_back() -> None:
    """落盘 → 用 read_file 按段读回 → 拼起来等于原文。

    这是唯一一条**真的走完整个闭环**的测试。
    只测「文件存在」是不够的：路径能不能被 read_file 接受、
    offset 语义对不对、段边界会不会切坏汉字 —— 任何一环错了，
    模型都取不回数据，而「落盘成功」的断言全都还是绿的。
    """
    import re

    text = "".join(f"第{i:05d}段：中文内容\n" for i in range(1200))   # 约 20K 字符
    out = tool_results.maybe_persist("big_tool", text, max_inline=8000)
    path = tool_results.store_path("big_tool", text)
    try:
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        assert rel in out          # 预览里给出的路径，模型照着抄就能用

        reader = ReadFileTool()
        parts: list[str] = []
        offset = 0
        for _ in range(100):       # 防死循环：万一 offset 不前进会立刻暴露
            chunk = reader.run(path=rel, offset=offset, max_bytes=4000)
            assert not chunk.startswith("[错误]"), chunk
            header, _, body = chunk.partition("\n")
            parts.append(body)
            match = re.search(r"offset=(\d+)", header)
            if not match:
                break
            offset = int(match.group(1))
        else:
            pytest.fail("分段读取没有终止 —— offset 没有前进")

        assert "".join(parts) == text
    finally:
        path.unlink(missing_ok=True)

