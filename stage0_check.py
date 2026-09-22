"""阶段 0 验收脚本 —— 从 .env 读 Key，调一次模型接口，打印返回。

【你的任务】把下面 4 处 `TODO` 补完。这是 ROADMAP 阶段 0 的唯一交付物。

【写完怎么运行】
    1. 先装依赖（在 mini-agent 目录下）：
           uv add httpx python-dotenv
    2. 复制配置模板并填入你自己的 Key：
           copy .env.example .env
    3. 运行：
           uv run python stage0_check.py

【验收标准】终端打印出模型返回的中文，并且显示出 token 用量。

【提示】卡住了就查官方文档，不要搜博客：
    httpx POST     https://www.python-httpx.org/quickstart/
    dotenv         https://github.com/theskumar/python-dotenv
    异常处理       https://docs.python.org/zh-cn/3/library/exceptions.html
"""

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()


def get_config() -> dict:
    """从环境变量读配置。缺任何一项就直接退出。

    为什么宁可退出也不给默认值？
    因为带着半个配置去调接口，报的错会离真正的原因很远，你会白排查半小时。
    """
    required = ["LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL_ID"]
    cfg = {}
    for key in required:
        value = os.getenv(key)
        if not value or "请替换" in value:
            print(f"[配置缺失] 环境变量 {key} 没读到或还是示例值。")
            print("           检查两件事：① .env 文件在不在 ② Key 是否已替换")
            sys.exit(1)
        cfg[key] = value

    # TODO 1: 把 LLM_MAX_TOKENS 和 LLM_TIMEOUT 也读进 cfg，并转成 int
    #   这两个是「成本上限」和「时间上限」，ROADMAP 第 9 节把成本控制列为第一条质量线。
    #   int() 转换会抛 ValueError —— 想想要不要处理它。
    # 给 os.getenv 传第二个参数（默认值），它就不会返回 None，警告随之消失。
    # 这两个是「调优参数」而非身份信息，给默认值是合理的；Key 那种就不该给默认值。
    cfg["max_tokens"] = int(os.getenv("LLM_MAX_TOKENS", "2048"))
    cfg["timeout"] = int(os.getenv("LLM_TIMEOUT", "30"))

    return cfg


def build_payload(model: str, max_tokens: int, user_text: str) -> dict:
    """组装请求体。

    注意：所谓「对话」，在 API 层面就是一个 messages 列表。
    这就是第 1 周要彻底想清楚的东西。
    """
    messages = [{"role": "system", "content": "你是简洁的助手，回答不超过三句话。"},
    {"role": "user", "content": user_text},
        # TODO 2: 补两条消息（用 dict，键是 "role" 和 "content"）
        #   ① role="system"：定义它是谁、该怎么说话。自定，例如「你是简洁的助手，回答不超过三句话」
        #   ② role="user"：内容用 user_text
        #
        #   补完请自问：为什么 system 要单独一条，
        #   而不是把「你是简洁的助手」直接拼进 user 那句话的前面？
    ]
    return {"model": model, "messages": messages, "max_tokens": max_tokens}


def call(cfg: dict, payload: dict) -> httpx.Response:
    """发一次 POST。全项目唯一与厂商耦合的地方就是这里。

    为什么要把模型调用单独关进一个函数？
    因为将来换服务商时，改这一个函数就够了，其余代码不受影响。
    """
    url = cfg["LLM_BASE_URL"].rstrip("/") + "/chat/completions"

    headers = {
        "Authorization": "Bearer " + cfg["LLM_API_KEY"],
        "Content-Type": "application/json",
    }

    # TODO 4: 用 httpx 发 POST
    #   要点：json=payload（不要自己 json.dumps）、timeout=cfg["timeout"]
    #   拿到响应后判断 resp.status_code：
    #       不是 200 就把状态码和 resp.text 打出来，然后 sys.exit(1)
    #       别让错误静默通过 —— 这是新手最常踩的坑
    resp = httpx.post(url, headers=headers, json=payload, timeout=cfg["timeout"])

    if resp.status_code != 200:
        print("请求失败，状态码:", resp.status_code)
        print(resp.text)
        sys.exit(1)

    return resp


def main() -> None:
    cfg = get_config()
    user_text = sys.argv[1] if len(sys.argv) > 1 else "用一句话说明什么是 Agent。"
    payload = build_payload(cfg["LLM_MODEL_ID"], cfg["max_tokens"], user_text)

    resp = call(cfg, payload)
    data = resp.json()

    print("---- 模型回复 ----")
    print(data["choices"][0]["message"]["content"])
    print("---- token 用量 ----")
    print(data.get("usage"))


if __name__ == "__main__":
    main()
