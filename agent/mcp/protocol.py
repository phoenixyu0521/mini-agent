"""MCP 的传输层 —— 在子进程的 stdin/stdout 上收发「一行一条 JSON」。

这一层只管搬运字节，不懂 MCP 的语义（不知道什么是 tools/list）。
拆出来的理由和 `llm.py` 那条铁律一样：**协议细节关在一个文件里**。

## 三个必须解决的现实问题

### 1. `readline()` 没有超时

    server 卡住不回消息时，`proc.stdout.readline()` 会**永久阻塞**。
    整个 Agent 就停在那里，用户看到的是「它不说话」。

    所以不能用 readline 直读：开一个后台线程把行塞进 `queue.Queue`，
    主线程用 `queue.get(timeout=...)` —— 这才是真的超时。

### 2. stderr 必须有人读

    `Popen(stderr=PIPE)` 之后如果没人读，管道缓冲区（Linux 64KB / Windows 类似）
    写满的那一刻，**子进程会在写 stderr 时永久阻塞**。
    表现是「server 莫名其妙不动了」，而且你用调试器看它就是在 write 上等着。

    所以也要开线程读 stderr，只留最后几行用于报错。

### 3. Windows 上 `npx` 其实是个 `.cmd`

    `shutil.which("npx")` 返回 `...\\npx.CMD`，而 `Popen` 不能直接 spawn `.CMD`
    （它不是可执行文件，是给 cmd.exe 的批处理）。
    直接 spawn 会 `FileNotFoundError` 或 `[WinError 193] 不是有效的 Win32 应用`。

    解法是用 `cmd /d /c` 包一层。
    nanobot 在 `_normalize_windows_stdio_command()` 里干了同一件事 ——
    **这不是谁抄谁，是这个坑谁都得踩一遍。**
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from typing import Any


class MCPError(RuntimeError):
    """传输层出问题：进程起不来、管道断了、响应超时。"""


# 读线程发现管道关闭时塞进队列的哨兵对象。
# 用 object() 而不是 None，因为 None 可能是合法的 JSON 值（null）。
_EOF = object()


def _prepare_command(
    command: str,
    args: list[str],
    env: dict[str, str] | None,
) -> tuple[str, list[str], dict[str, str] | None]:
    """把「用户写的命令」翻译成「Popen 真能执行的东西」。

    只处理 Windows 的 `.cmd` / `.bat` 情况，其他平台原样返回。
    """
    if os.name != "nt":
        return command, args, env

    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)

    resolved = shutil.which(command, path=merged_env.get("PATH")) or command
    if resolved.lower().endswith((".cmd", ".bat")):
        comspec = merged_env.get("COMSPEC") or "cmd.exe"
        # /d 跳过 AutoRun，/c 执行完就退 —— 少这两条行为会不一致
        return comspec, ["/d", "/c", resolved, *args], env
    return resolved, args, env


class StdioTransport:
    """一个 MCP server 子进程 + 它的三条管道。"""

    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self.name = name
        self._queue: queue.Queue[Any] = queue.Queue()
        self._stderr_tail: list[str] = []
        self._closed = False

        command, args, env = _prepare_command(command, list(args or []), env)

        try:
            # 二进制模式 + 显式 utf-8：中文 Windows 的子进程默认编码可能是 GBK，
            # 文本模式会按系统编码解码，遇到中文就 UnicodeDecodeError。
            self._proc = subprocess.Popen(
                [command, *args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**os.environ, **env} if env else None,
                cwd=cwd,
                bufsize=0,
            )
        except (OSError, ValueError) as exc:
            raise MCPError(f"MCP server '{name}' 启动失败：{type(exc).__name__}: {exc}") from exc

        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    # ── 后台读线程 ──────────────────────────────────────────

    def _pump_stdout(self) -> None:
        assert self._proc.stdout is not None
        try:
            for raw in self._proc.stdout:
                self._queue.put(raw)
        finally:
            self._queue.put(_EOF)

    def _pump_stderr(self) -> None:
        assert self._proc.stderr is not None
        for raw in self._proc.stderr:
            text = raw.decode("utf-8", errors="replace").rstrip()
            if text:
                self._stderr_tail.append(text)
                # 只留最后 40 行：这玩意是给报错用的，不是日志系统
                del self._stderr_tail[:-40]

    def stderr_tail(self, limit: int = 10) -> str:
        return "\n".join(self._stderr_tail[-limit:])

    # ── 读写 ────────────────────────────────────────────────

    def write(self, message: dict[str, Any]) -> None:
        if self._closed or self._proc.stdin is None:
            raise MCPError(f"MCP server '{self.name}' 已关闭，不能再写")
        data = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()   # 不 flush 就永远发不出去
        except (BrokenPipeError, OSError) as exc:
            raise MCPError(
                f"MCP server '{self.name}' 管道已断：{exc}\n它最后的输出：\n{self.stderr_tail()}"
            ) from exc

    def read(self, timeout: float = 20.0) -> dict[str, Any]:
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            raise MCPError(
                f"MCP server '{self.name}' {timeout}s 内没有响应。\n"
                f"它最后的输出：\n{self.stderr_tail() or '（无）'}"
            ) from None

        if item is _EOF:
            raise MCPError(
                f"MCP server '{self.name}' 提前退出（退出码 {self._proc.poll()}）。\n"
                f"它最后的输出：\n{self.stderr_tail() or '（无）'}"
            )

        try:
            return json.loads(item.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            # 典型原因：server 往 stdout 打了 print()
            raise MCPError(
                f"MCP server '{self.name}' 输出了非法 JSON（是不是往 stdout 打了日志？）：\n"
                f"{item[:300]!r}"
            ) from exc

    # ── 收尾 ────────────────────────────────────────────────

    def close(self) -> None:
        """关掉子进程。可以重复调用。"""
        if self._closed:
            return
        self._closed = True

        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()   # 关 stdin = 告诉 server「没活了」
        except OSError:
            pass

        try:
            self._proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            pass

        # 温和地请它走，再不走就强杀
        self._proc.terminate()
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=2)
