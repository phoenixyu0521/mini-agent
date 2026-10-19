"""工具基类 —— 一个工具 = 一份说明书 + 一个执行体。

为什么一个工具要写成「类」，而不是一个普通函数？

    因为要交给模型的东西有三样，它们必须成组出现：

        1. 叫什么名字     → name
        2. 干什么用       → description
        3. 参数长什么样   → parameters（JSON Schema）

    模型只看这三样来决定「要不要调」和「怎么调」。
    漏了第 3 样，它只能瞎猜参数；description 写得含糊，它该调的时候不调。

第 2 周唯一要理解的新概念就在第 3 样：

    parameters 为什么必须是 JSON Schema，而不是一句自然语言描述？

        因为 Schema 是**机器可校验的契约**。
        模型是按结构化输出训练的，看到 Schema 就能产出可解析的参数；
        你收到后直接 json.loads() 就能用，不需要写正则去猜格式。

        自然语言描述（"参数是一个文件路径，可以带引号"）也能让模型勉强理解，
        但你拿到的字符串会五花八门，最终一定要靠正则去兜 —— 那就是崩溃的开始。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


# 零参数工具的 parameters 长这样。
# "type": "object" 表示参数整体是个对象（字典）；
# properties 是每个参数的定义；required 是必填项名单。
# 三者缺一不可 —— 很多服务商看到缺字段会直接 400。
EMPTY_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
}


class Tool(ABC):
    """所有工具的父类。子类只要填三个类属性 + 实现 run()。"""

    # 模型就是照着 name 来点名的，所以它要短、要像函数名。
    # 一旦上线就别改名 —— 改了会打断所有历史对话。
    name: str = ""

    # 这句是写给模型看的，不是写给人看的。
    # 写「什么情况下必须调它」，比写「它是什么」有效得多。
    description: str = ""

    parameters: dict[str, Any] = EMPTY_PARAMS

    # ── 第 4 周：这个工具能不能和同批的其他工具**同时**执行？ ──
    #
    # 默认 **False**，必须显式声明才能并发。这是 fail-closed：
    # 猜错的代价是不对称的 ——
    #   猜「安全」但实际不安全 → 两个 run_shell 同时改同一个文件，结果是随机的
    #   猜「不安全」但实际安全 → 只是慢一点
    #
    # 判断标准只有一条：**会不会产生副作用 / 依赖执行顺序**。
    #   get_time、read_file → 只读，随便并发
    #   run_shell           → 可能写文件、可能按顺序才有意义 → 不能并发
    #
    # 注意：这不只是「快不快」的问题。真实系统里（比如 Cline）也会把
    # 有副作用的工具单独串起来跑，否则模型以为的「先建目录再写文件」会失效。
    concurrency_safe: bool = False

    def schema(self) -> dict[str, Any]:
        """打包成模型能看懂的「说明书」。

        这个嵌套三层的结构不是我们发明的，是 OpenAI 定下的格式，
        别家（包括我们用的那个中转站）都跟着走。

        所以换服务商时，全项目要改的地方就在这个文件 —— 这正是
        「与厂商耦合的代码只关进一个文件」那条铁律的实际用处。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @abstractmethod
    def run(self, **kwargs: Any) -> str:
        """真正干活的地方。

        两条约定：

        1. 返回值**必须是字符串** —— 它会原样塞回对话给模型看。
           返回 dict 的话自己先 json.dumps，否则模型收到的是 Python 的 repr，
           它会看不懂。

        2. 参数是模型给的，未必靠谱。**别假设它一定传对**，
           该给默认值就给默认值，能忽略的多余参数就忽略。
        """
