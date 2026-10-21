"""前端静态资源的测试。

为什么值得单独测：`/vendor/*` 是第 5 周新加的挂载点。
它坏了的表现是「界面打开一片白，控制台一条 404」——
而这类问题在 Python 侧的日志里**完全看不到**，只能靠这个测试拦住。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import server


def test_index_served():
    c = TestClient(server.app)
    r = c.get("/")
    assert r.status_code == 200
    body = r.text
    # 页面必须自己带着脚本引用，否则 markdown 渲染会静默失效（marked undefined）
    assert "/vendor/marked.min.js" in body
    assert "/vendor/highlight.min.js" in body
    # 停止按钮
    assert 'id="stop"' in body
    # favicon 用空 data URI，避免每次打开都 404
    assert 'rel="icon"' in body


def test_vendor_scripts_available():
    c = TestClient(server.app)
    for name, min_size in (("marked.min.js", 10_000), ("highlight.min.js", 50_000)):
        r = c.get(f"/vendor/{name}")
        assert r.status_code == 200, f"{name} 取不到，界面会白屏"
        assert len(r.content) > min_size


def test_vendor_blocks_path_traversal():
    """StaticFiles 必须挡住 `/vendor/../../.env` —— 那里面是 API Key。"""
    c = TestClient(server.app)
    for bad in ("/vendor/../index.html", "/vendor/../../.env", "/vendor/..%2f..%2f.env"):
        r = c.get(bad)
        assert r.status_code in (400, 403, 404), f"没挡住：{bad} -> {r.status_code}"
