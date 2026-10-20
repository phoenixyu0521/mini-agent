"""load_skill —— 让模型按需读取技能说明书。

## 为什么要做成「工具」而不是「直接塞进提示词」

    塞进去 = 每轮都付全文的钱，而 90% 的轮次用不到任何一个技能。
    做成工具 = 模型自己判断「这题该查说明书」，只在那一刻付钱。

    代价是多一次工具调用往返（约 1-2 秒 + 几百 token）。
    这个代价只在**真的要用技能**的那一轮付，划算。
"""

from __future__ import annotations

from typing import Any

from agent.skills import get_loader
from agent.tools.base import Tool


class LoadSkillTool(Tool):
    name = "load_skill"

    # 只读，不产生副作用
    concurrency_safe = True

    description = (
        "读取一个技能的完整说明。系统提示词里只列了技能的名字和一句话描述，"
        "**真正动手前必须先调它读全文**，照着里面的步骤做。"
        "如果不知道该用哪个技能，看系统提示词里的技能列表。"
    )

    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "技能名，例如 aihot / news-summary",
            },
        },
        "required": ["name"],
    }

    def run(self, name: str, **_: Any) -> str:
        return get_loader().load(name)
