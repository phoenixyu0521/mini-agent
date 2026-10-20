"""fetch_url 的测试 —— 重点在安全边界，全部离线。"""

from __future__ import annotations

import pytest

from agent.tools.fetch_url import FetchUrlTool, _is_public_host


@pytest.fixture()
def tool():
    return FetchUrlTool()


# ══════════════════════════════════════════════════════════
#  安全边界 —— 这些必须全部拒绝，一条都不能漏
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize("url,label", [
    ("http://127.0.0.1:8000/api/info", "本机回环"),
    ("http://127.0.0.1/", "本机回环简写"),
    ("http://localhost:8080/", "localhost"),
    ("http://192.168.1.1/admin", "内网 C 段"),
    ("http://10.0.0.5/", "内网 A 段"),
    ("http://172.16.0.1/", "内网 B 段"),
    ("http://169.254.169.254/latest/meta-data/", "云 metadata"),
    ("http://[::1]/", "IPv6 回环"),
])
def test_blocks_internal_addresses(tool, url, label):
    out = tool.run(url)
    assert out.startswith("[错误]"), f"{label} 被放行了：{out}"


@pytest.mark.parametrize("url", [
    "file:///C:/Windows/win.ini",
    "ftp://example.com/x",
    "javascript:alert(1)",
    "data:text/html,<script>x</script>",
])
def test_blocks_non_http_scheme(tool, url):
    out = tool.run(url)
    assert out.startswith("[错误]")
    assert "http/https" in out


def test_blocks_missing_host(tool):
    assert tool.run("http://").startswith("[错误]")


def test_public_host_allowed():
    ok, why = _is_public_host("example.com")
    assert ok, why


def test_private_host_rejected_with_reason():
    ok, why = _is_public_host("192.168.0.1")
    assert not ok
    assert "内网" in why


# ══════════════════════════════════════════════════════════
#  工具声明
# ══════════════════════════════════════════════════════════


def test_tool_schema(tool):
    schema = tool.schema()["function"]
    assert schema["name"] == "fetch_url"
    assert "url" in schema["parameters"]["properties"]
    assert schema["parameters"]["required"] == ["url"]


def test_concurrency_safe(tool):
    """只读工具，可以和同批其他读工具并行。"""
    assert tool.concurrency_safe is True


def test_description_mentions_get_only(tool):
    """模型会照 description 行事，必须明确说「只能 GET」。"""
    assert "GET" in tool.description
    assert "POST" in tool.description      # 「不能发 POST」
