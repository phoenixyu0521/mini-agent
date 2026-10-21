"""一键启动 —— 双击 `start.bat` 最终跑的就是这个文件。

## 为什么启动逻辑放在 .py 里，而不是全写在 .bat 里

    .bat 是**字节级**解析的：cmd 用当前代码页（中文 Windows 是 GBK/936）
    去读文件的每一个字节。把 UTF-8 编码的中文写进 .bat，得到的不是乱码字面量，
    而是**命令本身被撑坏** —— 实测现象是一串
        '─────' 不是内部或外部命令
        'env.example' 不是内部或外部命令
    因为多字节字符里恰好含有 cmd 当成控制符的字节，一行被切成了好几段。

    所以：`.bat` 只写纯 ASCII（判断环境、拉起 Python），
    所有面向人的中文输出交给 Python —— 它对编码的处理是我们能控制的。

## 为什么路径全部相对本文件

    整个文件夹可以搬家、可以打包发给别人，双击照样能跑。
    写死 `D:\\WorkBuddy\\...` 的话，换台机器就废了。
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 端口跟 server.py 的 main() 保持一致。这里不单独引入可配置端口 ——
# 两个地方各存一份端口是「配置漂移」的经典来源，等真需要时再统一。
PORT = 8000
URL = f"http://127.0.0.1:{PORT}/"
QUIET = os.environ.get("MINI_AGENT_QUIET") == "1"

BANNER = r"""
  mini-agent
  ------------------------------------------------------------
"""


def _port_open(port: int, timeout: float = 0.3) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex(("127.0.0.1", port)) == 0
    finally:
        sock.close()


def _open_when_ready(deadline: float = 60.0) -> None:
    """轮询端口，服务真起来之后才开浏览器。

    为什么不用「睡 4 秒再打开」：
        机器慢的时候服务还没起，浏览器显示「无法连接」，
        用户的第一反应是「它坏了」，而不是「再等等」。
        轮询最多多等几百毫秒，却消掉了这个误判。
    """
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if _port_open(PORT):
            time.sleep(0.25)          # 让首页那次请求不撞在启动瞬间
            webbrowser.open(URL)
            return
        time.sleep(0.25)
    print(f"  [!] 等了 {deadline:.0f} 秒服务还没起来，请往上翻看报错")


def main() -> int:
    print(BANNER)

    if not (ROOT / ".env").exists():
        # 不 exit：界面还能打开，历史会话还能看，只是发消息会报缺 Key。
        # 直接拦住会让人以为「装坏了」。
        print("  [!] 没找到 .env —— 界面可以打开，但发消息会报「缺少 API Key」")
        print("      做法：把 .env.example 复制成 .env，填上接口地址 / 模型名 / Key")
        print()

    if _port_open(PORT):
        print(f"  [i] 端口 {PORT} 已被占用 —— 服务应该已经在运行了")
        print(f"      直接打开 {URL}")
        if not QUIET:
            webbrowser.open(URL)
        time.sleep(2)
        return 0

    print(f"  聊天界面   {URL}")
    print(f"  接口文档   {URL}docs")
    print()
    print("  关闭这个窗口 = 停止服务")
    print("  ------------------------------------------------------------")
    print()

    if not QUIET:
        # daemon 线程：主进程（服务）一结束它也跟着走，不会留下孤儿
        threading.Thread(target=_open_when_ready, daemon=True).start()

    import server
    server.main()          # 阻塞在这里，直到窗口被关 / Ctrl+C
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n  服务已停止。")
