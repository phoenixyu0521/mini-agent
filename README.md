# mini-agent

从零手写的 Agent 内核 —— 4 周学习项目的代码仓库。

完整计划见上层目录的 [`../ROADMAP.md`](../ROADMAP.md)。

## 目录说明

```
mini-agent/
├── pyproject.toml      # 项目定义与依赖清单（uv 读它）
├── .python-version     # 锁定 Python 版本
├── uv.lock             # 依赖锁定，保证换机器环境一致
├── .gitignore          # 含 .env 与 .venv/，防止 Key 泄露
├── .env.example        # 配置模板（进 Git）
├── .env                # 你的真实 Key（不进 Git，需自己从模板复制）
├── stage0_check.py     # 【阶段 0 交付物】调通一次模型接口
├── agent/
│   ├── __init__.py
│   ├── llm.py          # 模型调用封装 —— 唯一与厂商耦合的文件
│   └── loop.py         # 核心循环 —— 记忆在这里
├── cli.py              # 命令行入口
└── .venv/              # 虚拟环境（uv 自动创建，不进 Git）
```

## 常用命令

在本目录下执行（PyCharm 用 `Alt + F12` 打开内置 Terminal，它会自动停在本目录）：

```bash
uv add <包名>                    # 装依赖 —— .venv 在这一刻自动出现
uv sync                          # 按 uv.lock 把环境恢复成一致状态

uv run python stage0_check.py    # 阶段 0 验收：调通模型接口
uv run python cli.py "你好"       # 单轮提问
uv run python cli.py             # 交互模式（多轮对话）
uv run python cli.py --stream    # 交互模式 + 流式输出
```

交互模式内置命令：`/reset` 清空上下文，`/exit` 退出。

## 三条必须记住的规则

1. **Key 绝不进代码**：只放在 `.env` 里，`.env` 已在 `.gitignore` 中。
2. **不对公网暴露 Key**：不要把 `.env` 内容贴到任何聊天窗口或截图里。
3. **每次请求都要有上限**：`max_tokens` 管钱，`timeout` 管时间。

## 一个必须知道的坑：推理模型会「先思考再回答」

本项目用的模型带思考模式，会先产出一段 `reasoning_content`。**思考也算 token，也吃 `max_tokens`。**

症状：偶尔回复是空的。这时先看 `usage` 里的 `reasoning_tokens`，不要把锅甩给代码。

`agent/llm.py` 的 `chat()` 里已经加了针对这个的诊断报错。

## 进度

- [x] **阶段 0**：`stage0_check.py` 跑通，能打印模型回复与 token 用量
- [x] **第 1 周**：`agent/llm.py` + `agent/loop.py` + `cli.py`，多轮对话 + 流式
  - [x] `agent/llm.py` —— 模型调用封装
  - [x] `agent/loop.py` —— 主循环（两处 TODO 已补齐）
  - [x] `cli.py` —— 命令行入口
  - [x] 单轮对话跑通
  - [x] 多轮对话跑通（模型记得前两句）
  - [x] 流式输出跑通
  - [x] `/reset` 后确实失忆（验证「记忆 = messages 列表」）
- [ ] 第 2 周：`agent/tools/`，模型能自主调工具
- [ ] 第 3 周：会话持久化 + 上下文裁剪 + 长期记忆
- [ ] 第 4 周：HTTP 接口 + 测试 + 打包
