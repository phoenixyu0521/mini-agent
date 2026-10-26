"""自己写的 MCP server 的行为测试。

用项目里的 MCP client 连它 —— 客户端和服务端都是本项目的代码，
哪一层出问题都能直接定位，没有第三方黑盒。
"""

from __future__ import annotations

import os
import sys

import pytest

from agent.mcp import MCPClient, StdioTransport
from mcp_servers.docs_tools import DB_PATH, build_match, clean_query
from mcp_servers.protocol import MCPServer, ToolSpec

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

needs_index = pytest.mark.skipif(
    not DB_PATH.exists(),
    reason="索引不存在，先跑 `uv run python -m mcp_servers.build_index`",
)


@pytest.fixture(scope="module")
def docs():
    client = MCPClient(
        "helloagents",
        StdioTransport("helloagents", sys.executable, ["-m", "mcp_servers"], cwd=PROJ),
        timeout=30.0,
    ).start()
    yield client
    client.close()


# ══════════════════════════════════════════════════════════
#  查询构造（纯函数，不依赖索引）
# ══════════════════════════════════════════════════════════


def test_stopwords_are_removed():
    assert clean_query("记忆系统怎么实现") == "记忆系统 实现"
    assert "如何" not in clean_query("如何评估智能体")


def test_short_terms_fall_back_to_none():
    """全是 1–2 字的词 → 返回 None，让调用方走 LIKE。

    这条守的是 trigram 的硬限制：它只索引 3 字符 token，
    把「反思」送进 MATCH 会得到「语法正确但永远 0 命中」的查询。
    """
    assert build_match("反思") is None
    assert build_match("记忆") is None
    assert build_match("评估") is None


def test_long_terms_become_or_expression():
    expr = build_match("记忆系统 上下文工程")
    assert expr is not None
    assert '"记忆系统"' in expr
    assert '"上下文工程"' in expr
    assert " OR " in expr


def test_short_words_dropped_from_long_query():
    """「实现」这类 2 字词在 trigram 索引里永远匹配不到，
    留在 MATCH 里会让整个表达式变成 0 命中 —— 所以必须丢掉，
    哪怕它看起来是个有用的词。"""
    assert build_match("记忆系统 实现") == '"记忆系统"'


def test_phrase_is_not_used():
    """整串短语会要求连续出现，召回极差（实测 10 个问题几乎全 0）。"""
    expr = build_match("记忆系统怎么实现")
    assert expr is not None
    assert "记忆系统怎么实现" not in expr


# ══════════════════════════════════════════════════════════
#  协议层
# ══════════════════════════════════════════════════════════


def test_handshake(docs):
    assert docs.alive
    assert docs.server_info["name"] == "hello-agents-docs"


def test_four_tools_listed(docs):
    names = [t["name"] for t in docs.list_tools()]
    assert names == ["list_chapters", "search_docs", "get_section", "get_outline"]


def test_unknown_method_returns_jsonrpc_error():
    """服务端遇到不认识的方法要回 -32601，而不是崩掉。"""
    server = MCPServer("t", "0.0.1", [])
    resp = server.handle({"jsonrpc": "2.0", "id": 7, "method": "no/such"})
    assert resp["id"] == 7
    assert resp["error"]["code"] == -32601


def test_notification_gets_no_response():
    server = MCPServer("t", "0.0.1", [])
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tool_exception_becomes_is_error():
    """工具抛异常 → isError=true，进程继续活着。

    这条很重要：如果异常直接冒出去，server 会死，
    客户端后续所有调用都失败 —— 一个工具的 bug 拖垮整个服务。
    """
    def boom(**kwargs: object) -> str:
        raise ValueError("故意炸")

    server = MCPServer("t", "0.0.1", [
        ToolSpec("boom", "总是失败", {"type": "object", "properties": {}, "required": []}, boom)
    ])
    resp = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "boom", "arguments": {}},
    })
    assert resp["result"]["isError"] is True
    assert "ValueError" in resp["result"]["content"][0]["text"]

    # 出过错之后还能继续服务
    ok = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert ok["result"]["tools"][0]["name"] == "boom"


def test_extra_arguments_are_ignored():
    """模型经常多给一两个字段。直接 **kwargs 会 TypeError，
    而那是个和工具逻辑毫无关系的报错。"""
    def echo(text: str) -> str:
        return text

    server = MCPServer("t", "0.0.1", [
        ToolSpec("echo", "回声", {"type": "object", "properties": {}, "required": []}, echo)
    ])
    resp = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "echo", "arguments": {"text": "hi", "unexpected": 1}},
    })
    assert resp["result"]["content"][0]["text"] == "hi"


# ══════════════════════════════════════════════════════════
#  业务层（需要索引）
# ══════════════════════════════════════════════════════════


@needs_index
def test_list_chapters(docs):
    text = docs.call_tool("list_chapters", {})
    assert "第 8 章" in text
    assert "第 16 章" in text


@needs_index
def test_search_finds_right_chapter(docs):
    """中文检索的关键回归测试：这两个查询过去是 0 命中。"""
    rag = docs.call_tool("search_docs", {"query": "RAG 检索增强", "limit": 3})
    assert "第八章" in rag

    mem = docs.call_tool("search_docs", {"query": "记忆系统怎么实现", "limit": 3})
    assert "第八" in mem or "记忆" in mem


@needs_index
def test_two_char_query_still_works(docs):
    """2 字词走 LIKE 兜底。不能因为 trigram 不支持就直接返回空。"""
    text = docs.call_tool("search_docs", {"query": "反思", "limit": 3})
    assert "[id=" in text


@needs_index
def test_outline_accepts_both_forms(docs):
    a = docs.call_tool("get_outline", {"chapter": "8"})
    b = docs.call_tool("get_outline", {"chapter": "chapter8"})
    assert a == b
    assert "[id=" in a


@needs_index
def test_get_section_roundtrip(docs):
    """从 search 拿 id，再用 id 取全文 —— 两级检索的完整链路。"""
    hits = docs.call_tool("search_docs", {"query": "MCP 协议", "limit": 1})
    first = next(line for line in hits.splitlines() if line.startswith("[id="))
    sid = int(first.split("id=")[1].split("]")[0])

    body = docs.call_tool("get_section", {"section_id": sid})
    assert len(body) > 200
    assert "[id=" not in body      # 取全文不该再返回检索格式


@needs_index
def test_unknown_section_returns_friendly_error(docs):
    text = docs.call_tool("get_section", {"section_id": 987654})
    assert "没有 id=" in text
    assert not text.startswith("[错误]")   # 是「查不到」，不是「工具坏了」


# ══════════════════════════════════════════════════════════
#  外部客户端兼容性（Cursor 红点事故的回归测试）
# ══════════════════════════════════════════════════════════

def test_starting_from_wrong_cwd_still_works():
    """`python -m mcp_servers` 换个工作目录就 `No module named`。

    这正是 Cursor 报「MCP error -32000: Connection closed」的原因：
    它 spawn 子进程时不认配置里的 cwd，进程 import 失败直接退出，
    而客户端不会把子进程的 stderr 给你看。

    launch.py 自己算路径，所以从任意目录启动都必须成功。
    这里故意用系统临时目录当 cwd —— 它不可能是项目目录。
    """
    import subprocess
    import tempfile

    launch = os.path.join(PROJ, "mcp_servers", "launch.py")
    assert os.path.exists(launch), "launch.py 是外部客户端的入口，不能删"

    msg = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}\n'
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            [sys.executable, launch],
            input=msg,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=tmp,                      # ← 关键：不是项目目录
            timeout=60,
        )
    assert proc.returncode == 0, f"从 {tmp} 启动失败，stderr={proc.stderr[:400]}"
    import json as _json
    reply = _json.loads(proc.stdout.strip().splitlines()[0])
    assert reply["result"]["serverInfo"]["name"] == "hello-agents-docs"


def test_protocol_version_is_negotiated():
    """客户端要的版本我们支持就回它要的，不能硬回老版本。

    新版客户端（Cursor 3.x）发 2025-06-18。硬回 2024-11-05 可能被判为不兼容，
    客户端报的还是「Connection closed」，查起来和启动失败长得一模一样。
    """
    from mcp_servers.protocol import PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS

    server = MCPServer(name="t", version="0", tools=[])

    for ver in SUPPORTED_PROTOCOL_VERSIONS:
        reply = server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": ver},
        })
        assert reply["result"]["protocolVersion"] == ver

    # 不认识的版本 → 回自己的默认值，让客户端自己决定（规范允许）
    reply = server.handle({
        "jsonrpc": "2.0", "id": 2, "method": "initialize",
        "params": {"protocolVersion": "2019-01-01"},
    })
    assert reply["result"]["protocolVersion"] == PROTOCOL_VERSION

    # 客户端没发版本（老客户端）→ 也不能崩
    reply = server.handle({"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}})
    assert reply["result"]["protocolVersion"] == PROTOCOL_VERSION


def test_stderr_is_valid_utf8_even_without_pythonutf8():
    """子进程的 stderr 必须是合法 UTF-8，哪怕环境里没有 PYTHONUTF8。

    为什么专门测 stderr？
        2026-10-26 的实测：`serve()` 里 reconfigure 了 stdout 和 stdin，
        **唯独漏了 stderr**。而紧接着第一行日志就是中文
        （「…已启动，等待客户端…」），GBK 环境下它按 cp936 编码发出去。
        客户端读 stderr 的线程按 UTF-8 解 → UnicodeDecodeError，
        而主线程因为 stdout 里的 JSON 是纯 ASCII，照样跑通 ——
        于是测试是绿的，bug 在日志线程里悄悄发生。

        MCP 的规矩是：stdout 是协议通道，日志只能走 stderr。
        所以 stderr 的编码和协议一样重要。

    为什么要在子进程里清掉 PYTHONUTF8？
        开发机上这个变量通常是 1，子进程继承它就自动 UTF-8，
        测试永远绿 —— 而用户拿到的中文 Windows 上并没有这个变量。
        不清掉就等于没测。
    """
    import json as _json
    import subprocess
    import tempfile

    launch = os.path.join(PROJ, "mcp_servers", "launch.py")
    msg = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}\n'

    # 还原成「中文 Windows 默认」：不带任何 UTF-8 开关
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PYTHONUTF8", "PYTHONIOENCODING", "PYTHONLEGACYWINDOWSSTDIO")
    }

    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            [sys.executable, launch],
            input=msg.encode("utf-8"),
            capture_output=True,          # 注意：不传 text/encoding，拿原始字节
            cwd=tmp,
            env=env,
            timeout=60,
        )

    assert proc.returncode == 0

    # 这两条是核心：stderr 一个字节都不能是非法 UTF-8。
    # decode 失败会抛 UnicodeDecodeError，测试直接红 —— 正是我们要的。
    stderr_text = proc.stderr.decode("utf-8")
    assert "已启动" in stderr_text, f"stderr 里应该有启动日志，实际是：{stderr_text[:200]!r}"

    # 顺带确认 stdout 也只含合法 UTF-8（协议通道，同样不能坏）
    reply = _json.loads(proc.stdout.decode("utf-8").strip().splitlines()[0])
    assert reply["result"]["serverInfo"]["name"] == "hello-agents-docs"

