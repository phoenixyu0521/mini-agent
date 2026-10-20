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
          "base_url": "https://maas.qianwenaiapi.com/compatible-mode/v1",
          "model": "deepseek-v4.1-flash",
          "max_tokens": 2048,
          "timeout": 60
        },
        "smart": {
          "base_url": "https://maas.qianwenaiapi.com/compatible-mode/v1",
          "model": "deepseek-v4-pro",
          "max_tokens": 8192,
          "timeout": 90
        }
      }
    }

`profiles` 就是「几套预设」，`default` 指定不带参数时用哪套。
**注意里面没有 API Key** —— Key 仍然从 .env 读。

## 优先级（这个顺序要想清楚）

    命令行 --profile smart       ← 最高，本次启动说了算
    环境变量 LLM_PROFILE         ← 次之，可以写进 shell 脚本
    config.json 的 default       ← 兜底
    内置常量 DEFAULT_PROFILE     ← 最后兜底（config.json 里连 default 都没写）

## ⚠️ 已知局限（2026-10-11 真实验证暴露）

**一个 profile 只有 base_url，没有 Key —— 所以一个 Key 只能配一家厂商。**

现在 `api_key` 是从 `LLM_API_KEY` 全局读的（见 resolve() 的 TODO ②），
而 `base_url` 是每个 profile 一份。这带来一个说不通的组合：

    想放「中转站」和「智谱官方」两个档案 → 做不到，因为只有一个 Key

更要命的是**失败信息具有误导性**：地址指向 A 家、Key 是 B 家的，
报错是 401「令牌已过期或验证不正确」。你会以为是 Key 过期了，
去后台翻半天，其实是自己把两家的东西拼在了一起。

**修法（第 4 周做）**：profile 里加 `api_key_env` 字段，指向不同的环境变量名：

    "zhipu": { "base_url": "...", "api_key_env": "ZHIPU_API_KEY", ... }

## 边界

    ✅ 一个 config.json 存多套预设，命令行切换
    ✅ 配置进 Git，凭据留在 .env
    ❌ 一个 Key 只能配一家厂商（见上面的已知局限）
    ❌ 不做「运行时热切换」—— 换 profile 要重启程序
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# ⚠️ 这一行不是多余的。
# 为什么 config.py 自己也要 load_dotenv()，明明 llm.py 已经调过了？
#     因为「模块不该依赖别的模块碰巧做过什么初始化」。
#     之前它跑得通，只是因为 cli.py 恰好先 import 了 llm —— 一旦有人
#     单独 `from agent import config`（比如写单测），就取不到 .env 里的 Key。
#     这类「靠运气成立的隐式依赖」是最难查的一类 bug。
#     load_dotenv() 是幂等的，重复调用没有副作用。
load_dotenv()

# config.json 放项目根目录（和 README 同级）——
# 因为它「属于项目」，应该能进 Git，也应该让人一眼看到。
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.json"

# 内置兜底。为什么需要它？
#     如果 config.json 不存在（比如刚 clone 下来还没建），程序不该直接崩 ——
#     它会退回这套最保守的默认值，至少能跑起来。
DEFAULT_PROFILE = "fast"
DEFAULT_MAX_TOKENS = 2048
DEFAULT_TIMEOUT = 60

# 模型能吃多大的上下文。它决定「历史消息最多占多少 token」——
# 即 context.compact_budget() 的输入。
# ⚠️ 这个值必须和模型真实窗口一致：填大了会撞 HTTP 400（窗口不够），
#    填小了会过早压缩、白丢上下文。换模型时记得一起改。
DEFAULT_CONTEXT_WINDOW = 128_000

# 内置的默认接口地址和模型。
# 为什么这两个可以写进代码，而 API Key 不行？
#     因为它们不是秘密 —— 地址和模型名是公开的。
#     写进来能让「config.json 还没建」时程序依然可用。
#
# ⚠️ 2026-10-11 教训：这里的值必须和 config.json 里的**实际可用组合**一致。
#     初版写的是智谱官方地址 + glm-4.7-flash，但本项目用的 Key 是中转站的，
#     两者不配套 → 401「令牌已过期或验证不正确」。
#     另外 deepseek-v4-flash 这个名字已从中转站下线，要用 deepseek-v4.1-flash。
#     **地址、模型名、Key 三者必须来自同一家厂商** —— 这是配置化最容易踩的坑。
DEFAULT_BASE_URL = "https://maas.qianwenaiapi.com/compatible-mode/v1"
DEFAULT_MODEL = "deepseek-v4.1-flash"

# 配置文件不存在时，自动创建这个内容。
# 它和仓库里的 config.json **刻意保持一致** —— 这样「删掉后自动重建」
# 得到的仍是一份能跑通的配置。
TEMPLATE: dict[str, Any] = {
    "default": "fast",
    "profiles": {
        "fast": {
            "base_url": DEFAULT_BASE_URL,
            "model": DEFAULT_MODEL,
            "max_tokens": 8192,
            "timeout": 120,
            "context_window": DEFAULT_CONTEXT_WINDOW,
        },
        "smart": {
            "base_url": DEFAULT_BASE_URL,
            "model": "deepseek-v4-pro",
            "max_tokens": 8192,
            "timeout": 180,
            "context_window": DEFAULT_CONTEXT_WINDOW,
        },
    },
    # MCP server 列表。空对象 = 不用 MCP。
    # 放在**顶层**而不是某个 profile 里，因为它跟「用哪个模型」无关 ——
    # 换模型不该导致工具消失。
    "mcpServers": {},
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
        # 窗口大小：老配置文件没有这一项，兜底成默认值（不该因为缺字段就崩）
        self.context_window = int(data.get("context_window") or DEFAULT_CONTEXT_WINDOW)
        # MCP server 配置。同样兜底成空 dict —— 老配置文件里没有这一项，
        # 但「没有 MCP」和「配置坏了」是两件事，前者不该报错。
        self.mcp_servers: dict[str, Any] = dict(data.get("mcp_servers") or {})
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
            f"context_window={self.context_window}, "
            f"url={self.url!r}, api_key={masked!r})"
        )

    def summary(self) -> str:
        """给 cli.py 横幅用的一行人话。同样不显示 Key。"""
        has_key = "已配置" if self.api_key else "缺失"
        return (
            f"配置档案 {self.profile}  |  模型 {self.model}  |  "
            f"max_tokens {self.max_tokens}  |  超时 {self.timeout}s  |  "
            f"窗口 {self.context_window // 1000}k  |  API Key {has_key}"
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
    # 1) 文件不存在就自动创建（用 TEMPLATE）
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(TEMPLATE, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    # 2) 读 + 解析。用户手编 JSON 时多一个逗号很常见
    text = CONFIG_PATH.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"config.json 格式不对（第 {exc.lineno} 行）：{exc.msg}\n"
            f"检查 {CONFIG_PATH}"
        ) from exc

    # 3) 校验最外层结构 —— 宁可在门口拦下
    if not isinstance(data, dict):
        raise ConfigError(f"config.json 的最外层必须是一个对象 {{...}}，实际是 {type(data).__name__}")
    if not isinstance(data.get("profiles"), dict):
        raise ConfigError('config.json 里必须有 "profiles" 字段，且它是个对象')

    return data



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
    data = load_raw()
    profiles = data["profiles"]

    # a) 决定用哪个 profile —— 优先级从高到低
    chosen = name or os.environ.get("LLM_PROFILE") or data.get("default") or DEFAULT_PROFILE

    # b) 找它。找不到要列出手上有哪些
    if chosen not in profiles:
        available = "、".join(sorted(profiles))
        raise ConfigError(
            f'config.json 里没有名为 "{chosen}" 的 profile。'
            f"可用的有：{available}。"
        )

    preset = profiles[chosen]
    if not isinstance(preset, dict):
        raise ConfigError(f'profile "{chosen}" 必须是一个对象，实际是 {type(preset).__name__}')

    # c) 四个值都给兜底
    preset_data = {
        "base_url": preset.get("base_url") or DEFAULT_BASE_URL,
        "model": preset.get("model") or DEFAULT_MODEL,
        "max_tokens": int(preset.get("max_tokens") or DEFAULT_MAX_TOKENS),
        "timeout": int(preset.get("timeout") or DEFAULT_TIMEOUT),
        "context_window": int(preset.get("context_window") or DEFAULT_CONTEXT_WINDOW),
        # MCP 是顶层配置，不属于 profile。这里透传下去，
        # Config 才是一个「自包含」的对象 —— 拿到 cfg 就知道该怎么连工具。
        "mcp_servers": data.get("mcpServers") or {},
    }

    # d) API Key —— 唯一绝不兜底的一项
    api_key = os.environ.get("LLM_API_KEY") or ""
    if not api_key.strip() or "请替换" in api_key:
        raise ConfigError(
            "缺少 LLM_API_KEY。请把 .env.example 复制成 .env，"
            "填上你自己的 Key（.env 不会进 Git）。"
        )

    # e) 组装
    return Config(profile=chosen, data=preset_data, api_key=api_key)



# ── 调试用 ──────────────────────────────────────────────────

def list_profiles() -> list[str]:
    """列出 config.json 里所有可用的 profile 名。脚手架，已写好。

    给 `cli.py --profiles` 用。为什么这个不用留 TODO？
        因为它只是「读文件 + 取 keys + 排序」，纯脚手架，没有原理。

    ⚠️ 为什么连 NotImplementedError 也一起捕？
        这是骨架阶段留下的防御 —— 当时 load_raw() 还是 TODO，未补时会抛它。
        现在两处 TODO 都补完了，理论上捕不到，**但留着不亏**：
        将来若又新增未实现的函数，这个入口仍不会崩，只会安静地列不出来。
    """
    try:
        data = load_raw()
    except (ConfigError, NotImplementedError):
        return []
    return sorted(data.get("profiles", {}).keys())
