"""把 hello-agents 的 docs/ 抓下来，切成小节，建成 SQLite 全文索引。

用法：
    uv run python -m mcp_servers.build_index                # 从 GitHub 抓
    uv run python -m mcp_servers.build_index --from-local D:\\some\\docs

产物：mcp_servers/data/docs.db

## 为什么不做成「自动更新」

索引是构建时产物，不是运行时行为。如果 server 每次启动都去抓一遍 GitHub：
1. 启动会慢几秒（还没算上下载）
2. 网络不通就整个 server 起不来
3. 上游一改，你的检索结果就会变，但你不知道

**把「数据准备」和「提供服务」分开**，是这类工具的通用做法。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.parse
from pathlib import Path

import httpx

REPO = "datawhalechina/hello-agents"
DOCS_PREFIX = "docs/"
DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = DATA_DIR / "docs.db"
CACHE_DIR = DATA_DIR / "raw"

MAX_CHARS = 1800


# ══════════════════════════════════════════════════════════
#  一、取数据
# ══════════════════════════════════════════════════════════

def list_doc_files() -> list[str]:
    """列出 docs/ 下要用的 markdown。只要中文正文，英文版是逐句翻译，重复度高。"""
    headers = {"User-Agent": "mini-agent", "Accept": "application/vnd.github+json"}
    repo = "https://api.github.com/repos/" + REPO
    r = httpx.get(f"{repo}/git/trees/main?recursive=1", headers=headers, timeout=60)
    r.raise_for_status()

    paths = [
        t["path"] for t in r.json()["tree"]
        if t["type"] == "blob"
        and t["path"].startswith(DOCS_PREFIX)
        and t["path"].endswith(".md")
    ]
    # 跳过英文版：文件名全 ASCII 的一律不要（README_EN / _sidebar_en / ChapterX-...）
    # 顶层的 README.md 也跳过 —— 它整篇是 HTML 导航和徽章，检索出来是纯噪音。
    keep = []
    for p in paths:
        name = p.rsplit("/", 1)[-1]
        if name in {"README.md", "_sidebar.md"}:
            continue
        if re.search(r"[^\x00-\x7f]", name):     # 含非 ASCII 字符 → 中文版
            keep.append(p)
    return sorted(keep)


def fetch_all(files: list[str]) -> dict[str, str]:
    """下载并缓存。

    ⚠️ 本机实测：raw.githubusercontent.com 的 SSL 会被中断
       （UNEXPECTED_EOF_WHILE_READING / WinError 10054）。
       而 api.github.com 的 contents 接口加 `Accept: application/vnd.github.raw`
       是通的，且直接返回原文（不用 base64 解码）。
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "mini-agent", "Accept": "application/vnd.github.raw"}
    out: dict[str, str] = {}

    for i, rel in enumerate(files, 1):
        cache = CACHE_DIR / (rel.replace("/", "__"))
        if cache.exists() and cache.stat().st_size > 0:
            out[rel] = cache.read_text(encoding="utf-8")
            continue

        url = (
            f"https://api.github.com/repos/{REPO}/contents/"
            + urllib.parse.quote(rel, safe="/")
        )
        r = httpx.get(url, headers=headers, timeout=60, follow_redirects=True)
        if r.status_code != 200:
            print(f"  [{i}/{len(files)}] {r.status_code} {rel}", file=sys.stderr)
            continue
        text = r.content.decode("utf-8", errors="replace")
        cache.write_text(text, encoding="utf-8")
        out[rel] = text
        print(f"  [{i}/{len(files)}] {len(text)/1024:.0f} KB  {rel}", file=sys.stderr)

    return out


def load_local(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*.md")):
        name = p.name
        if not re.search(r"[^\x00-\x7f]", name):
            continue
        out[str(p.relative_to(root)).replace("\\", "/")] = p.read_text(encoding="utf-8")
    return out


# ══════════════════════════════════════════════════════════
#  二、切块
# ══════════════════════════════════════════════════════════

FENCE = re.compile(r"^\s*(```|~~~)")
H1 = re.compile(r"^#\s+(.+?)\s*$")
H2 = re.compile(r"^##\s+(.+?)\s*$")
H3 = re.compile(r"^###\s+(.+?)\s*$")


def _split_by_heading(lines: list[str], pattern: re.Pattern) -> list[tuple[str, list[str]]]:
    """按标题切。**代码块里的 '##' 不算标题**（见下面的 in_code）。

    这三行 if 是整个切块器最关键的地方：markdown 里代码块常含
    `## 注释`，不判断就会把一段代码劈成两块，模型看到没头没尾的片段。
    """
    out: list[tuple[str, list[str]]] = []
    title, body, in_code = "", [], False
    for line in lines:
        if FENCE.match(line):
            in_code = not in_code
        if not in_code:
            m = pattern.match(line)
            if m:
                if body:
                    out.append((title, body))
                title, body = m.group(1), []
                continue
        body.append(line)
    if body:
        out.append((title, body))
    return out


def _split_by_paragraph(lines: list[str]) -> list[list[str]]:
    chunks: list[list[str]] = []
    cur: list[str] = []
    size = 0
    for line in lines:
        cur.append(line)
        size += len(line) + 1
        if size >= MAX_CHARS and not line.strip():
            chunks.append(cur)
            cur, size = [], 0
    if cur:
        chunks.append(cur)
    return chunks


def build_blocks(chapter: str, text: str) -> list[dict]:
    lines = text.splitlines()
    # 章标题不一定在第一行（有些文件开头是空行或 HTML），往下扫一点再放弃。
    # 扫不到就退回目录名 —— 显示成 "chapter3" 虽然丑，但不会崩。
    chapter_title = chapter
    for line in lines[:30]:
        m = H1.match(line)
        if m:
            chapter_title = m.group(1)
            break

    blocks: list[dict] = []
    for h2, body2 in _split_by_heading(lines, H2):
        if len("\n".join(body2)) <= MAX_CHARS:
            blocks.append({"section": h2, "body": "\n".join(body2)})
            continue
        for h3, body3 in _split_by_heading(body2, H3):
            label = f"{h2} / {h3}" if h3 else h2
            if len("\n".join(body3)) <= MAX_CHARS:
                blocks.append({"section": label, "body": "\n".join(body3)})
                continue
            for i, part in enumerate(_split_by_paragraph(body3), 1):
                blocks.append({"section": f"{label} ({i})", "body": "\n".join(part)})

    result = []
    for b in blocks:
        body = b["body"].strip()
        if len(body) < 60:        # 目录、分隔线，没有检索价值
            continue
        result.append({
            "chapter": chapter,
            "chapter_title": chapter_title,
            "section": (b["section"] or chapter_title).strip(),
            "body": body,
        })
    return result


# ══════════════════════════════════════════════════════════
#  三、建库
# ══════════════════════════════════════════════════════════

def build_db(docs: dict[str, str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE blocks (
            id INTEGER PRIMARY KEY,
            chapter TEXT, chapter_title TEXT, section TEXT, body TEXT
        )
    """)
    # ⚠️ 中文必须 trigram。默认的 unicode61 按空格/标点切词，中文没有空格，
    #    整段变成一个 token —— 查什么都是 0 命中（本项目实测）。
    #    代价：trigram 只索引 3 字符 token，2 字查询要退化成 LIKE。
    con.execute("""
        CREATE VIRTUAL TABLE blocks_fts USING fts5(
            chapter_title, section, body,
            content='blocks', content_rowid='id',
            tokenize='trigram'
        )
    """)

    total = 0
    for rel, text in docs.items():
        parts = rel.split("/")
        chapter = parts[1] if len(parts) > 1 else "front"
        if chapter.endswith(".md"):        # docs/前言.md 这种，不是章
            chapter = "front"
        for b in build_blocks(chapter, text):
            cur = con.execute(
                "INSERT INTO blocks(chapter, chapter_title, section, body) VALUES (?,?,?,?)",
                (b["chapter"], b["chapter_title"], b["section"], b["body"]),
            )
            con.execute(
                "INSERT INTO blocks_fts(rowid, chapter_title, section, body) VALUES (?,?,?,?)",
                (cur.lastrowid, b["chapter_title"], b["section"], b["body"]),
            )
            total += 1

    con.commit()
    con.close()
    print(f"索引完成：{total} 块 → {DB_PATH} ({DB_PATH.stat().st_size/1024:.0f} KB)")


def main() -> None:
    ap = argparse.ArgumentParser(description="构建 hello-agents 文档索引")
    ap.add_argument("--from-local", type=Path, help="用本地已下载的 docs 目录，不联网")
    args = ap.parse_args()

    if args.from_local:
        print(f"从本地读取：{args.from_local}", file=sys.stderr)
        docs = load_local(args.from_local)
    else:
        files = list_doc_files()
        print(f"共 {len(files)} 个中文文档，开始下载…", file=sys.stderr)
        docs = fetch_all(files)

    if not docs:
        raise SystemExit("没有取到任何文档")
    build_db(docs)


if __name__ == "__main__":
    main()
