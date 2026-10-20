"""skill 加载器的测试 —— 全部离线，不联网。"""

from __future__ import annotations

import pytest

from agent import skills
from agent.loop import AgentLoop


@pytest.fixture()
def sample(tmp_path):
    """造两个假技能，不依赖真实 skills/ 目录。"""
    for name, desc, body in [
        ("alpha", "第一个技能", "正文 A"),
        ("beta", "第二个技能", "正文 B"),
    ]:
        d = tmp_path / name
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}\n", encoding="utf-8"
        )
    return tmp_path


# ══════════════════════════════════════════════════════════
#  frontmatter
# ══════════════════════════════════════════════════════════


def test_parse_frontmatter_basic():
    meta, body = skills.parse_frontmatter("---\nname: x\ndescription: 说明\n---\n\n正文\n")
    assert meta["name"] == "x"
    assert meta["description"] == "说明"
    assert body == "正文"


def test_parse_frontmatter_quoted_values():
    meta, _ = skills.parse_frontmatter('---\nname: "y z"\n---\n正文')
    assert meta["name"] == "y z"


def test_parse_frontmatter_missing():
    """没有 frontmatter 也不能崩 —— 返回空 meta + 全文。"""
    meta, body = skills.parse_frontmatter("就是一段文字")
    assert meta == {}
    assert body == "就是一段文字"


# ══════════════════════════════════════════════════════════
#  扫描与两层
# ══════════════════════════════════════════════════════════


def test_discover(sample):
    loader = skills.SkillLoader(sample)
    assert loader.names() == ["alpha", "beta"]


def test_summary_is_cheap(sample):
    """第一层只给名字和描述，不能把正文塞进去。"""
    loader = skills.SkillLoader(sample)
    text = loader.summary()
    assert "alpha" in text and "第一个技能" in text
    assert "正文 A" not in text          # ← 这条是关键
    assert len(text) < 300


def test_load_returns_full_body(sample):
    loader = skills.SkillLoader(sample)
    out = loader.load("alpha")
    assert "正文 A" in out
    assert "技能：alpha" in out


def test_load_unknown_lists_available(sample):
    out = skills.SkillLoader(sample).load("nope")
    assert out.startswith("[错误]")
    assert "alpha" in out and "beta" in out


def test_disabled_skill_is_skipped(tmp_path):
    d = tmp_path / "off"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: off\ndescription: 关掉的\nenabled: false\n---\n正文", encoding="utf-8"
    )
    assert skills.SkillLoader(tmp_path).names() == []


def test_empty_dir_is_safe(tmp_path):
    loader = skills.SkillLoader(tmp_path)
    assert loader.names() == []
    assert loader.summary() == ""


# ══════════════════════════════════════════════════════════
#  注入
# ══════════════════════════════════════════════════════════


def test_skill_block_injected_without_memory():
    """没有长期记忆时，技能目录也必须注入。

    这是实现时会踩的坑：原代码写的是
        if self._memory_message is None: return view
    加了技能之后，这条会把它一起漏掉。
    """
    agent = AgentLoop(with_tools=True, auto_compact=False, memory=None, session=None)
    agent._skill_loaded = False
    agent._skill_message = None

    view = agent._inject_memory(list(agent.messages) + [{"role": "user", "content": "hi"}])
    contents = [str(m.get("content", "")) for m in view]
    assert any("【可用技能】" in c for c in contents)


def test_skill_block_not_injected_without_tools():
    """没启用工具就不注入 —— 模型读不到全文，给了目录只会瞎编。"""
    agent = AgentLoop(with_tools=False, auto_compact=False, memory=None, session=None)
    view = agent._inject_memory(list(agent.messages) + [{"role": "user", "content": "hi"}])
    assert not any("【可用技能】" in str(m.get("content", "")) for m in view)


def test_injection_order():
    """system → 记忆 → 技能 → 对话。越靠后越「新」。"""
    agent = AgentLoop(with_tools=True, auto_compact=False, memory=None, session=None)
    agent._skill_loaded = False
    agent._skill_message = None
    agent._memory_message = {"role": "system", "content": "【长期记忆】"}

    view = agent._inject_memory([
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "U1"},
    ])
    assert view[0]["content"] == "SYS"
    assert view[1]["content"] == "【长期记忆】"
    assert "【可用技能】" in view[2]["content"]
    assert view[3]["content"] == "U1"
