"""给外部 MCP 客户端用的启动入口：**不依赖 cwd，也不依赖 PYTHONPATH**。

为什么需要这个文件（`python -m mcp_servers` 不够用）：

    MCP 客户端 spawn 子进程时，能不能找到 `mcp_servers` 这个包，
    取决于它给子进程设的工作目录。实测 Cursor 3.24 **忽略配置里的 `cwd` 字段**
    （`~/.cursor/mcp.json` 里写了 `"cwd"` 也没用），子进程从别处启动 → 抛
    `No module named mcp_servers` → 进程退出码 1 → 客户端只看到
    「MCP error -32000: Connection closed」。

    客户端不会把子进程的 stderr 当作错误显示出来，所以这个错**看起来像协议问题，
    实际是启动问题**。改用一个能自己算路径的入口，就绕开了整个问题。

用法（给客户端配置用）：

    <venv>/python.exe   <项目绝对路径>/mcp_servers/launch.py

命令行调试仍然可以用 `uv run python -m mcp_servers`。
"""

from __future__ import annotations

import os
import sys

# launch.py 在 mcp_servers/ 里，上一级才是项目根（也就是能 import mcp_servers 的目录）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from mcp_servers.__main__ import main  # noqa: E402 —— 必须在 sys.path 调整之后

if __name__ == "__main__":
    main()
