"""hello-agents 文档的检索工具 —— 这是「作品」的业务层。

## 为什么是「两级检索」

    模型面对 80 万字，不可能一次读完。所以要分两步：
        第一步 search：只给「哪一节提到 + 一小段上下文」，很便宜
        第二步 get：模型确认这一节有用，才取全文

    这和人类查手册的方式一样：先翻目录，再读那一页。
    一步到位的话，要么返回太少（模型不知道全貌），要么返回太多（吃光上下文）。

## 检索策略的一个硬限制

    SQLite 的 trigram 分词器只索引**3 个字符**的 token。
    所以：
        · 「记忆系统」→ 能查（被切成 记忆系/忆系统 等 3-gram）
        · 「反思」    → 查不到（2 个字符，索引里没有这种东西）
    2 字查询必须退化成 LIKE 全表扫。150 条数据，扫一遍是毫秒级，不值得优化。

    这条限制不是可以绕过的「小问题」—— 它决定了「反思」「记忆」「工具」
    这类最常见的两字检索词必须走另一条路。
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "docs.db"

# 提问里的虚词。不去掉会污染排序 —— trigram 会把「如何」「怎么」也当成关键词去匹配。
STOPWORDS = [
    "请问", "帮我", "告诉我", "介绍一下", "介绍", "讲讲", "说说",
    "如何", "怎么", "怎样", "什么", "为什么", "哪些", "哪个", "是否",
    "可以", "需要", "应该", "一下",
    "的", "了", "是", "在", "和", "与", "有", "吗", "呢", "吧",
]


def clean_query(text: str) -> str:
    out = text
    for s in STOPWORDS:
        out = out.replace(s, " ")
    return " ".join(out.split())


def build_match(query: str) -> str | None:
    """把自然语言问题变成 FTS5 的 MATCH 表达式。

    返回 None 表示「所有词都太短」，调用方要退化成 LIKE。

    为什么用 `"词" OR "词"` 而不是整串短语？
        整串短语要求文档里**连续出现**这几个字。用户问「记忆系统怎么实现」，
        文档里写的是「记忆系统的工作流程」，短语匹配 → 0 命中（实测过）。
        拆开 OR 之后由 bm25 排序决定相关性，召回和排序都更好。
    """
    terms = [
        w for w in re.split(r"[\s，。、？！,.;:：()（）\[\]【】\"'“”]+", clean_query(query))
        if len(w) >= 3          # < 3 字符的 term 在 trigram 索引里永远匹配不到
    ]
    if not terms:
        return None
    return " OR ".join(f'"{t}"' for t in terms)


def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise RuntimeError(
            f"索引不存在：{DB_PATH}\n"
            "请先运行：uv run python -m mcp_servers.build_index"
        )
    return sqlite3.connect(DB_PATH)


def _like_fallback(con: sqlite3.Connection, raw_query: str, limit: int) -> list[tuple]:
    """短查询（1–2 字）专用。全表扫，但表只有一百多行。"""
    key = clean_query(raw_query) or raw_query
    rows = con.execute(
        """SELECT id, chapter, chapter_title, section,
                  substr(body, max(1, instr(body, ?) - 20), 120)
           FROM blocks WHERE body LIKE ?
           ORDER BY length(body) LIMIT ?""",
        (key, f"%{key}%", limit),
    ).fetchall()
    return rows


# ══════════════════════════════════════════════════════════
#  四个工具
# ══════════════════════════════════════════════════════════

def search_docs(query: str, limit: int = 5) -> str:
    """搜索文档小节，返回编号 + 位置 + 片段。用这些编号去调 get_section 取全文。"""
    limit = max(1, min(int(limit or 5), 10))
    con = _connect()
    try:
        match = build_match(query)
        rows: list[tuple] = []
        if match:
            try:
                rows = con.execute(
                    """SELECT b.id, b.chapter, b.chapter_title, b.section,
                              snippet(blocks_fts, 2, '【', '】', '…', 24)
                       FROM blocks_fts JOIN blocks b ON b.id = blocks_fts.rowid
                       WHERE blocks_fts MATCH ? ORDER BY rank LIMIT ?""",
                    (match, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            rows = _like_fallback(con, query, limit)
    finally:
        con.close()

    if not rows:
        return f"没有找到与「{query}」相关的小节。可以换个说法，或先用 list_chapters 看有哪些章节。"

    parts = [f"共 {len(rows)} 条结果（用 get_section 取全文）："]
    for rid, chapter, chapter_title, section, snippet in rows:
        snippet = re.sub(r"\s+", " ", str(snippet or "")).strip()
        parts.append(
            f"\n[id={rid}] 第 {chapter.replace('chapter', '')} 章 · {chapter_title}\n"
            f"  小节：{section}\n"
            f"  片段：{snippet}"
        )
    return "\n".join(parts)


def get_section(section_id: int) -> str:
    """按编号取一小节的完整内容。"""
    con = _connect()
    try:
        row = con.execute(
            "SELECT chapter, chapter_title, section, body FROM blocks WHERE id = ?",
            (int(section_id),),
        ).fetchone()
    finally:
        con.close()

    if not row:
        return f"没有 id={section_id} 的小节。id 来自 search_docs 的结果，不能凭猜。"
    chapter, chapter_title, section, body = row
    return f"《{chapter_title}》· {section}\n\n{body}"


def list_chapters() -> str:
    """列出全书章节和每章的节数，用于确定检索范围。"""
    con = _connect()
    try:
        rows = con.execute(
            """SELECT chapter, chapter_title, COUNT(*)
               FROM blocks GROUP BY chapter ORDER BY CAST(REPLACE(chapter,'chapter','') AS INTEGER)"""
        ).fetchall()
    finally:
        con.close()

    if not rows:
        return "索引是空的，请先运行 build_index。"

    lines = [f"共 {len(rows)} 个部分："]
    for chapter, title, n in rows:
        label = chapter.replace("chapter", "第 ") + " 章" if chapter.startswith("chapter") else chapter
        lines.append(f"  {label} · {title}（{n} 节）")
    return "\n".join(lines)


def get_outline(chapter: str) -> str:
    """列出某一章的所有小节标题（不含正文），用来在章内定位。

    chapter 参数接受 "8" 或 "chapter8"，两种都认得 —— 模型经常只给数字。
    """
    key = str(chapter).strip()
    key = key if key.startswith("chapter") else f"chapter{key}"

    con = _connect()
    try:
        rows = con.execute(
            "SELECT id, section FROM blocks WHERE chapter = ? ORDER BY id", (key,)
        ).fetchall()
    finally:
        con.close()

    if not rows:
        return f"没有 {key} 这一章。先用 list_chapters 看有哪些。"

    return f"{key} 的小节（{len(rows)} 个）：\n" + "\n".join(
        f"  [id={rid}] {section}" for rid, section in rows
    )
