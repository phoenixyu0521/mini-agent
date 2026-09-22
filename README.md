# mini-agent

从零手写的 Agent 内核 —— 4 周学习项目的代码仓库。

完整计划见上层目录的 [`../ROADMAP.md`](../ROADMAP.md)。

## 目录说明

```
mini-agent/
├── pyproject.toml      # 项目定义与依赖清单（uv 读它）
├── .python-version     # 锁定 Python 版本
├── .gitignore          # 含 .env，防止 Key 泄露
├── .env.example        # 配置模板（进 Git）
├── .env                # 你的真实 Key（不进 Git，需要自己从模板复制）
├── stage0_check.py     # 【阶段 0 交付物】调通一次模型接口
└── .venv/              # 虚拟环境（uv 自动创建，不进 Git）
```

## 常用命令

在本目录下执行：

```bash
uv add httpx python-dotenv   # 装依赖 —— .venv 会在这一刻自动出现
uv run python stage0_check.py # 运行脚本 —— 不用手动激活 .venv
uv sync                       # 按 uv.lock 把环境恢复成一致状态
```

## 三条必须记住的规则

1. **Key 绝不进代码**：只放在 `.env` 里，`.env` 已在 `.gitignore` 中。
2. **不对公网暴露 Key**：不要把 `.env` 内容贴到任何聊天窗口或截图里。
3. **每次请求都要有上限**：`max_tokens` 管钱，`timeout` 管时间。

## 进度

- [ ] 阶段 0：`stage0_check.py` 跑通，能打印模型回复与 token 用量
- [ ] 第 1 周：`agent/llm.py` + `agent/loop.py` + `cli.py`，多轮对话 + 流式
- [ ] 第 2 周：`agent/tools/`，模型能自主调工具
- [ ] 第 3 周：会话持久化 + 上下文裁剪 + 长期记忆
- [ ] 第 4 周：HTTP 接口 + 测试 + 打包
