"""第一个工具 —— 故意做到最简单。

为什么必须是它排在第一位？

    它零依赖、几乎不会失败，存在的目的**不是「有用」，而是「跑通协议」**。
    这样一旦出问题，你可以百分之百确定：问题在协议层，不在工具本身。
    直接拿 read_file 或 run_shell 开刀的话，出错了你会分不清是协议错还是工具错。

    ROADMAP 原话：「这是最陡的坡。先把 get_time 跑通，别一上来写 run_shell」。

注意 description 的写法：

    差：「返回当前时间」          —— 模型不知道什么时候该用它
    好：「用户问现在几点、今天几号时必须调用，不要自己猜」

    模型没有时钟，它确实会「猜」一个时间给你（往往还挺像真的）。
    所以 description 里要明确写「必须调用」，否则它会觉得自己能直接答。
"""

from __future__ import annotations

import datetime
from typing import Any

from agent.tools.base import Tool


class GetTimeTool(Tool):
    name = "get_time"

    description = (
        "获取当前的日期和时间。"
        "当用户询问现在几点、今天几号、明天是什么日子时，必须调用这个工具，"
        "不要凭记忆猜测时间。"
    )

    # 这就是那份「机器可校验的契约」。
    # 每个字段的作用：
    #   type        —— 参数整体是 object（字典）
    #   properties  —— 里面每个参数的定义
    #   required    —— 哪些是必填。空列表表示全都可以不传
    parameters = {
        "type": "object",
        "properties": {
            "offset_days": {
                "type": "integer",
                "description": "相对今天的天数偏移。0=今天（默认），1=明天，-1=昨天",
            }
        },
        "required": [],
    }

    def run(self, offset_days: int = 0, **_: Any) -> str:
        # **kwargs 是为了忽略模型多传的参数 —— 模型偶尔会自作主张加字段，
        # 不加这个它会直接 TypeError。
        offset = int(offset_days or 0)
        now = datetime.datetime.now() + datetime.timedelta(days=offset)
        return now.strftime("%Y-%m-%d %H:%M:%S")
