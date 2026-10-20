"""MCP client 的行为测试 —— 全部用本地假 server，不联网、不装 npx。

这些测试锁住的是**协议层的行为**，不是「能不能连上某个具体 server」。
好消息是假 server 可以故意制造麻烦（比如在响应前插一条通知），
真 server 不会配合你测这个。
"""

from __future__ import annotations

import os
import sys

import pytest

from agent.mcp import MCPClient, MCPError, StdioTransport, make_tools

FAKE = os.path.join(os.path.dirname(__file__), "fake_mcp_server.py")


@pytest.fixture(scope="module")
def client():
    """整个模块共用一个连接 —— 起子进程约 400ms，每条测试都起太浪费。"""
    c = MCPClient(
        "fake",
        StdioTransport("fake", sys.executable, [FAKE]),
        timeout=15.0,
    ).start()
    yield c
    c.close()


# ══════════════════════════════════════════════════════════
#  握手
# ══════════════════════════════════════════════════════════


def test_handshake_records_server_info(client):
    assert client.alive
    assert client.protocol_version == "2024-11-05"
    assert client.server_info["name"] == "fake-server"


def test_list_tools(client):
    names = [t["name"] for t in client.list_tools()]
    assert names == ["echo", "add"]


def test_call_before_handshake_is_rejected():
    """没握手就调，必须当场报错，而不是发一个半成品请求出去。"""
    transport = StdioTransport("fake", sys.executable, [FAKE])
    try:
        raw = MCPClient("fake", transport, timeout=15.0)
        with pytest.raises(MCPError, match="握手"):
            raw.call_tool("echo", {"text": "x"})
    finally:
        transport.close()


# ══════════════════════════════════════════════════════════
#  ★ 核心：响应和通知混在一条流里
# ══════════════════════════════════════════════════════════


def test_notification_before_response_does_not_break_pairing(client):
    """假 server 在每次 tools/call 的响应**之前**先推一条 notifications/progress。

    如果 `_read_matching` 退化成「发一个请求，读一条消息当响应」，
    这里拿到的会是那条通知，然后 `message["result"]` 直接 KeyError。

    所以这条测试看起来只是「调成功了」，实际在锁死 id 配对逻辑。
    """
    result = client.call_tool("echo", {"text": "插队通知"})
    assert result == "回声：插队通知"


def test_multiple_calls_in_a_row(client):
    """连着调，每次都带一条插队通知 —— 不能出现「错位一次，之后全错」。"""
    for i in range(3):
        assert client.call_tool("add", {"a": i, "b": 10}) == str(i + 10)


# ══════════════════════════════════════════════════════════
#  错误一律变成字符串，不外抛
# ══════════════════════════════════════════════════════════


def test_unknown_tool_becomes_error_string(client):
    out = client.call_tool("no_such_tool", {})
    assert out.startswith("[错误]")
    assert "no_such_tool" in out


def test_remote_error_flag_becomes_error_string(client):
    """远端 isError=true 也要转成 [错误]，不能当成正常结果回给模型。"""
    out = client.call_tool("add", {"a": "不是数字"})
    assert out.startswith("[错误]")
    assert "参数不对" in out


def test_bad_command_raises_mcp_error_not_filenotfound():
    """命令根本不存在时，要抛出我们自己的错误类型，并带上 server 名字。

    为什么要包一层？因为上层（host）只捕获 MCPError 做降级，
    裸的 FileNotFoundError 会一路冒到 user 脸上。
    """
    with pytest.raises(MCPError, match="启动失败"):
        StdioTransport("nosuch", "definitely-not-a-real-command-xyz", [])


# ══════════════════════════════════════════════════════════
#  伪装成本地 Tool
# ══════════════════════════════════════════════════════════


def test_tool_names_are_prefixed(client):
    names = [t.name for t in make_tools(client)]
    assert names == ["mcp_fake_echo", "mcp_fake_add"]


def test_remote_tool_is_not_concurrency_safe(client):
    """client 内部一把大锁，同一时刻只能一个调用 —— 必须如实申报 False。"""
    for tool in make_tools(client):
        assert tool.concurrency_safe is False


def test_whitelist_limits_exposure(client):
    tools = make_tools(client, only=["echo"])
    assert [t.name for t in tools] == ["mcp_fake_echo"]


def test_whitelist_with_unknown_name_yields_nothing(client):
    """白名单写错名字 → 什么都不暴露。宁可少给，不能多给。"""
    assert make_tools(client, only=["no_such_tool"]) == []


def test_schema_has_three_required_fields(client):
    """缺 type/properties/required 任意一个，部分服务商会直接 400。"""
    for tool in make_tools(client):
        params = tool.schema()["function"]["parameters"]
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict)
        assert isinstance(params["required"], list)


def test_remote_tool_run_goes_through_client(client):
    tool = next(t for t in make_tools(client) if t.name.endswith("_add"))
    assert tool.run(a=2, b=40) == "42"
