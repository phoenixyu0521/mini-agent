"""skill 加载器 —— 用「渐进披露」把一大堆积压的说明书变成可用能力。

## skill 是什么

    一份 Markdown，告诉模型「遇到这类事该怎么做、用哪些工具、按什么顺序」。
    **它不改代码，只改模型的行为。**

    和「工具」的分工：
        工具 = 能做什么（能力边界）
        skill = 该怎么做（做事的方法）

    例：`fetch_url` 是工具，「抓 BBC 的 RSS 再按地区归纳」是 skill。

## 为什么必须分两层

    63 个 skill 的全文加起来 40 万字符，全塞进 system prompt 是不可能的。
    但只给名字，模型又不知道它们是干什么的。

    所以：
        第一层（每轮都发）   name + 一行 description，约 40 token/个
        第二层（按需读）     模型说「我要用 aihot」→ load_skill("aihot") 读全文

    实测 8 个 skill 的目录约 300 token/轮。63 个全装是 1763 token/轮，
    而且大部分你永远用不到 —— **所以别全装，挑。**

## 一个必须提醒的点

    WorkBuddy 那套 skill 是写给「能跑任意 bash 命令」的模型的，
    里面经常直接写 `curl ...`。搬到本项目**必须改写**：
    这里的 run_shell 只有 echo/date/dir/ls/pwd，curl 跑了会失败。

    正确的改写不是「把 curl 加进白名单」，而是**把意图翻译成本项目的工具**：
        curl -s "https://feeds.bbci.co.uk/..."   →   fetch_url(url="https://feeds.bbci.co.uk/...")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# 项目根下的 skills/ 目录。每个技能一个子目录，里面必须有 SKILL.md
SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"

FRONTMATTER = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n(.*)$", re.S)


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """拆出 YAML frontmatter 和正文。

    手写解析，不引 pyyaml —— 这里只需要 `key: value` 这一种形式。
    多行值（description 换行写）会解析不全，但现有的 skill 都写成一行，
    真遇到再补，不要为了「万一」多引一个依赖。
    """
    m = FRONTMATTER.match(text)
    if not m:
        return {}, text

    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        meta[key.strip()] = value
    return meta, m.group(2).strip()


@dataclass
class Skill:
    name: str
    description: str          # 中文优先，没有就英文
    body: str                 # 完整正文（含 frontmatter 之后的内容）
    path: Path
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def chars(self) -> int:
        return len(self.body)


class SkillLoader:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else SKILLS_DIR
        self._skills: dict[str, Skill] = {}
        self.reload()

    # ── 发现 ───────────────────────────────────────────────

    def reload(self) -> None:
        self._skills.clear()
        if not self.root.is_dir():
            return

        for d in sorted(self.root.iterdir()):
            f = d / "SKILL.md"
            if not f.is_file():
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue

            meta, body = parse_frontmatter(text)
            if str(meta.get("enabled", "true")).lower() == "false":
                continue

            name = meta.get("name") or d.name
            desc = (
                meta.get("description_zh")
                or meta.get("description")
                or meta.get("description_en")
                or "（没有描述）"
            )
            self._skills[name] = Skill(
                name=name,
                description=desc,
                body=body,
                path=f,
                meta=meta,
            )

    # ── 两层 ───────────────────────────────────────────────

    def names(self) -> list[str]:
        return sorted(self._skills)

    def summary(self) -> str:
        """第一层：给模型看的目录。每轮都随 system prompt 发出去。"""
        if not self._skills:
            return ""

        lines = [
            "【可用技能】",
            "遇到下列场景时，**先调 load_skill 把完整说明读出来，再按说明动手**。"
            "不要凭印象猜流程。",
        ]
        for s in self._skills.values():
            lines.append(f"- {s.name}：{s.description}")
        return "\n".join(lines)

    def load(self, name: str) -> str:
        """第二层：读全文。名字写错要给出可用列表，别让模型瞎猜。"""
        key = str(name or "").strip().lower()
        skill = self._skills.get(key) or next(
            (s for k, s in self._skills.items() if k.lower() == key), None
        )
        if skill is None:
            available = "、".join(self.names()) or "（没有安装任何技能）"
            return f"[错误] 没有名为 {name} 的技能。可用：{available}"

        return (
            f"# 技能：{skill.name}\n"
            f"（正文 {skill.chars} 字，已完整加载。按它的步骤做。）\n\n"
            f"{skill.body}"
        )

    def __repr__(self) -> str:
        return f"SkillLoader(root={self.root}, skills={self.names()})"


# ── 进程级单例 ─────────────────────────────────────────────
# 和 mcp.host / llm.get_client 同一个思路：扫描一次，全进程共用。

_loader: SkillLoader | None = None


def get_loader(root: Path | None = None) -> SkillLoader:
    global _loader
    if _loader is None or (root is not None and Path(root) != _loader.root):
        _loader = SkillLoader(root)
    return _loader
