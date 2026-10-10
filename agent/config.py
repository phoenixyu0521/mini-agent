"""配置化 —— 第 3 周的第四个文件，也是「程序能给别人用」的第一步。

## 先问：现在这样有什么不好？

你现在换模型，要打开 `.env`，改 `LLM_MODEL_ID` 那一行，然后重启程序。

改一行就能跑，听起来还行。但有三件事会变得很难受：

    1. 想同时存着两套配置。比如「平时用便宜的快模型，做难活时切强模型」——
       你得把改过的那行记住，或者复制两份 .env 手动换来换去。

    2. 想把 .env 提交给别人。但 .env 里有你的 API Key，**永远不能进 Git**。
       结果就是：别人 clone 你的项目，看不到任何模型配置，只能翻文档猜。

    3. 参数和代码混在一起。`max_tokens=2048` 这个数写在哪合适？
       写在 .env 里，它和 `LLM_TIMEOUT` 挤在一堆；写在代码里，改它就要改代码。
       这两种都不好。

## 解法：把「配置」和「凭据」分开

这是这一块唯一需要真正理解的事：

    凭据（Credential）  = 证明「你是谁」的东西 —— API Key
                          → 属于**你个人**，绝不进 Git
                          → 只能从 .env 或环境变量读

    配置（Config）      = 描述「怎么干活」的东西 —— 模型名、超时、max_tokens
                          → 属于**项目**，应该进 Git
                          → 放 config.json，别人 clone 下来就能看到

**为什么这个区分重要？**
    因为它们的生命周期不一样。你换台电脑，凭据要重新配；但配置跟着项目走。
    混在一个文件里，就会出现「要么 Key 泄露，要么配置无法共享」的两难。

    这个思路不是我编的 —— 12-Factor App 的第 3 条就是这个原则。

## config.json 长什么样

    {
      "default": "fast",
      "profiles": {
        "fast": {
          "base_url": "https://open.bigmodel.cn/api/paas/v4",
          "model": "glm-4.7-flash",
          "max_tokens": 2048,
          "timeout": 30
        },
        "deepseek": {
          "base_url": "https://api.deepseek.com/v1",
          "model": "deepseek-v4-flash",
          "max_tokens": 8192,
          "timeout": 60
        }
      }
    }

`profiles` 就是「几套预设」，`default` 指定不带参数时用哪套。
**注意里面没有 API Key** —— Key 仍然从 .env 读。

## 优先级（这个顺序要想清楚）

    命令行 --profile deepseek   ← 最高，本次启动说了算
    环境变量 LLM_PROFILE         ← 次之，可以写进 shell 脚本
    config.json 的 default       ← 兜底
    内置常量 "default"           ← 最后兜底（config.json 里连 default 都没写）

## 边界

    ✅ 一个 config.json 存多套预设，命令行切换
    ✅ 配置进 Git，凭据留在 .env
    ❌ 不做「运行时热切换」—— 换 profile 要重启程序。这一块够用就行
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# config.json 放项目根目录（和 README 同级）——
# 因为它「属于项目」，应该能进 Git，也应该让人一眼看到。
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.json"

# 内置兜底。为什么需要它？
#     如果 config.json 不存在（比如刚 clone 下来还没建），程序不该直接崩 ——
#     它会退回这套最保守的默认值，至少能跑起来。
#     ⚠️ 但注意：这里**不放 base_url 和 model**，见下面的 DEFAULT_BASE_URL 说明。
DEFAULT_PROFILE = "default"
DEFAULT_MAX_TOKENS = 2048
DEFAULT_TIMEOUT = 30

# 内置的默认接口地址。
# 为什么这个可以写进代码，而 API Key 不行？
#     因为它不是秘密 —— 它是公开的、所有用智谱的人都知道的地址。
#     把它写进来，能让「config.json 还没建」的新用户开箱即用。
DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_MODEL = "glm-4.7-flash"

# 配置文件不存在时，自动创建这个内容（含两套示例预设）。
TEMPLATE: dict[str, Any] = {
    "default": "fast",
    "profiles": {
        "fast": {
            "base_url": DEFAULT_BASE_URL,
            "model": DEFAULT_MODEL,
            "max_tokens": 2048,
            "timeout": 30,
        },
        "deepseek": {
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-v4-flash",
            "max_tokens": 8192,
            "timeout": 60,
        },
    },
}


class ConfigError(RuntimeError):
    """配置有问题（文件格式错、profile 不存在、缺必填项）。

    注意：llm.py 里也有一个同名的 ConfigError。
    这是个**已知的重复**，本次故意保留 —— 合并它们要动 llm.py 的公开接口，
    风险大于收益。第 4 周重构时统一。
    """


class Config:
    """一份「解析好的、马上能用的」配置。

    和 Memory / Session 不同的是，它**不做磁盘读写循环** ——
    读一次、解析一次、之后只读不写。所以它更像一个「值对象」。

    为什么把 base_url 和 url 分开？
        base_url 是配置文件里的原始值（给人和别的代码看）
        url 是拼接好的完整接口地址（给 httpx 用）
        拼接口的活在这里干一次，llm.py 就不用每次都拼了。
    """

    def __init__(self, profile: str, data: dict[str, Any], api_key: str) -> None:
        self.profile = profile
        self.model = str(data["model"])
        self.max_tokens = int(data["max_tokens"])
        self.timeout = int(data["timeout"])
        self.base_url = str(data["base_url"]).rstrip("/")
        self.url = self.base_url + "/chat/completions"
        self.api_key = api_key

    def __repr__(self) -> str:
        """打印时**绝不显示 api_key**。

        为什么专门写这个方法？
            调试时经常 print(config)。如果它是 dataclass 默认行为，
            Key 会整串出现在终端里，然后可能被粘进 issue、截图、聊天记录。
            **凭据泄露最常见的路径就是日志。**
        """
        masked = (self.api_key[:6] + "..." + self.api_key[-4:]) if len(self.api_key) > 12 else "***"
        return (
            f"Config(profile={self.profile!r}, model={self.model!r}, "
            f"max_tokens={self.max_tokens}, timeout={self.timeout}, "
            f"url={self.url!r}, api_key={masked!r})"
        )

    def summary(self) -> str:
        """给 cli.py 横幅用的一行人话。同样不显示 Key。"""
        has_key = "已配置" if self.api_key else "缺失"
        return (
            f"配置档案 {self.profile}  |  模型 {self.model}  |  "
            f"max_tokens {self.max_tokens}  |  超时 {self.timeout}s  |  API Key {has_key}"
        )


# ─────────────────────────────────────────────────────────────
# TODO ① —— 读 config.json，返回原始 dict
#
#   输入：无（路径用模块常量 CONFIG_PATH）
#   返回：一个 dict，形如 {"default": "fast", "profiles": {...}}
#
#   要做的四件事：
#
#   1) 文件不存在 → **自动创建**，内容是 TEMPLATE。
#      为什么要自动创建？
#          因为 config.json 是**要进 Git** 的 —— 你把它 clone 下来时它就在，
#          根本不会走这条分支。但如果有人手动删了它，自动重建比报错友好。
#      怎么写？提示：json.dumps(TEMPLATE, ensure_ascii=False, indent=2)
#          · ensure_ascii=False → 中文原样写出，不是 \uXXXX（本机踩过的坑）
#          · indent=2         → 给人看的，缩进起来
#          · 别忘了 encoding="utf-8"
#
#   2) 读文件内容：CONFIG_PATH.read_text(encoding="utf-8")
#
#   3) json.loads() 解析。**要 try/except json.JSONDecodeError** ——
#      用户手编 JSON 时多一个逗号是常有的事，这时要抛 ConfigError 并带上原因，
#      而不是让一个原始的 JSONDecodeError 糊到用户脸上。
#      错误信息里要提「检查 config.json 的格式」。
#
#   4) 校验最外层结构：必须是 dict，且 **必须含 "profiles" 且它是 dict**。
#      为什么不校验就往下走？
#          因为写错的 config.json 会让后面 resolve() 报出莫名其妙的 KeyError，
#          错误信息离真正原因很远。**宁可在门口拦下**（和 load_config() 同一个思路）。
#      检查点：
#          · not isinstance(data, dict)          → 抛错
#          · not isinstance(data.get("profiles"), dict) → 抛错
#
#   先回答再动手：
#   Q：为什么这里要「自动创建」而 Memory.load() 却规定「读的函数不创建文件」？
#      （提示：想想这两个文件一个进 Git、一个不进 Git）
# ─────────────────────────────────────────────────────────────
def load_raw() -> dict[str, Any]:
    raise NotImplementedError("TODO ① 还没补：load_raw() 还没实现")


# ─────────────────────────────────────────────────────────────
# TODO ② —— 决定「这次用哪个 profile」，然后把它解析成 Config
#
#   参数 name：命令行传进来的 profile 名（可能是 None）
#
#   优先级（从上到下，谁先有值就听谁的）：
#
#       ① name 参数                       ← 命令行 --profile xxx
#       ② os.environ.get("LLM_PROFILE")   ← 环境变量
#       ③ data.get("default")             ← config.json 里的 default
#       ④ DEFAULT_PROFILE 常量            ← 最后兜底（"default"）
#
#   然后做这些检查（顺序重要，一条条来）：
#
#   a) 有了 profile 名之后，去 data["profiles"] 里找它 —— 找不到要抛 ConfigError，
#      而且**错误信息里要列出有哪些可用的**（用户才知道自己拼错了哪个）。
#      提示：", ".join(profiles.keys())
#
#   b) 找到的那份预设必须是 dict（有人可能把 profile 写成列表）。
#
#   c) 从预设里取四个值，**每个都给兜底**：
#          base_url   → 缺就用 DEFAULT_BASE_URL
#          model      → 缺就用 DEFAULT_MODEL
#          max_tokens → 缺就用 DEFAULT_MAX_TOKENS（注意 int() 转一下）
#          timeout    → 缺就用 DEFAULT_TIMEOUT（同样 int()）
#      为什么这四个给兜底，而 API Key 不给？
#          因为它们错了顶多「效果不好」；Key 错了是「根本用不了」。
#          **兜底只该给「有合理默认值」的东西。**
#
#   d) 读 API Key —— 这是**唯一**必须存在、绝不兜底的一项：
#          key = os.environ.get("LLM_API_KEY")
#          如果为空，或者包含 "请替换" → 抛 ConfigError，
#          错误信息要说明「请把 .env.example 复制成 .env 并填入 Key」。
#
#   e) 最后 return Config(profile=<用了哪个名字>, data=<组装好的 dict>, api_key=key)
#      注意 data 这个 dict 需要四个 key：base_url / model / max_tokens / timeout
#      （因为 Config.__init__ 就是这么取的，去看一眼它）
#
#   先回答再动手：
#   Q：为什么 (a)(b)(c) 都校验好了才读 API Key，而不是一开头就读？
#      （提示：想想如果用户只是把 profile 名字拼错了，应该先告诉他哪一个错）
# ─────────────────────────────────────────────────────────────
def resolve(name: str | None = None) -> Config:
    raise NotImplementedError("TODO ② 还没补：resolve() 还没实现")


# ── 调试用 ──────────────────────────────────────────────────

def list_profiles() -> list[str]:
    """列出 config.json 里所有可用的 profile 名。脚手架，已写好。

    给 `cli.py --profiles` 用。为什么这个不用留 TODO？
        因为它只是「读文件 + 取 keys + 排序」，纯脚手架，没有原理。

    ⚠️ 这里**故意连 NotImplementedError 一起捕**：
        因为 load_raw() 是 TODO ① —— 它还没被补上时，这个函数不能让程序崩。
        宁可返回空列表让上层打印「还没有任何 profile」，
        也不要让用户看到一个他没写过的异常。
        （同样地，抛错也不该外抛 —— 这个函数的语义就是「列不出来就列不出来」。）
    """
    try:
        data = load_raw()
    except (ConfigError, NotImplementedError):
        return []
    return sorted(data.get("profiles", {}).keys())
