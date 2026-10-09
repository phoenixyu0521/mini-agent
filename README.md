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
│   ├── loop.py         # 核心循环 —— 记忆在这里
│   └── tools/          # 【第 2 周】工具层
│       ├── __init__.py
│       ├── base.py       # 工具基类 + Schema 生成
│       ├── registry.py   # 注册表 + 自动发现
│       ├── get_time.py   # 工具 1：读系统时间（无副作用）
│       ├── read_file.py  # 工具 2：读项目内文件（必须做路径校验）
│       └── run_shell.py  # 工具 3：执行只读命令（必须做白名单）
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
uv run python cli.py --tools     # 【第 2 周】交互模式 + 工具
```

交互模式内置命令：`/reset` 清空上下文，`/exit` 退出。

## 加一个新工具要做什么

**只做一件事：在 `agent/tools/` 下新建一个 .py 文件**，里面写 `Tool` 的子类，
填好 `name` / `description` / `parameters` 三个类属性，实现 `run()`。

注册表（`registry.py`）会自动扫描这个目录并收走它，**别处一行都不用改**。

## 第 2 周的代价：工具说明书是固定开销

`tools` 字段**每一轮请求都要重发一遍**。实测 1 个工具会让 `prompt_tokens` 从 17 涨到 276。

一次工具调用要发两次请求（先拿 `tool_calls`，回填结果后再拿答案），实测约 714 tokens ——
是普通一问一答的十倍。所以 `max_iterations` 不是可选项。

## 三条必须记住的规则

1. **Key 绝不进代码**：只放在 `.env` 里，`.env` 已在 `.gitignore` 中。
2. **不对公网暴露 Key**：不要把 `.env` 内容贴到任何聊天窗口或截图里。
3. **每次请求都要有上限**：`max_tokens` 管钱，`timeout` 管时间。

## 四层安全边界（第 2 周真正的内容）

工具 `parameters` 里的每个值都是**模型生成的字符串**，不是你敲的。所以工具层必须假设
「每个参数都可能有恶意」。

| 层 | 手段 | 挡住什么 |
|---|---|---|
| 1 | 路径限制在项目目录内（`is_relative_to`） | `read_file("../../.env")` 跑到项目外 |
| 2 | 敏感目标拒绝（`.env` / `.git/` / `.venv/` / `.key`） | `.env` **就在项目里**，第 1 层挡不住 |
| 3 | 命令白名单（不是黑名单） | `del /s /q`、`curl xxx \| iex` |
| 4 | 超时 + 固定工作目录 | 死循环命令、在 `C:\Windows` 里乱跑 |

第 2 层是验收时才补上的 —— 教训是：**第 1 层只回答「能不能出项目」，
不回答「项目里什么是敏感的」**。两个问题要分开想。

## 本项目最该记住的两条默认值原则

1. **fail-closed（默认拒绝）** —— 安全逻辑没写出来就一律拒绝。
   `read_file` 最初的版本是 fail-open 的：校验为空，代码直接落到
   `target = PROJECT_ROOT / path`，模型一句 `read_file("../.env")` 就能读出你的 Key。
2. **白名单优于黑名单** —— 「允许什么」永远列不全（`ALLOWED_COMMANDS` 6 个 vs 危险命令无穷），
   所以只列安全的；反过来关键凭据要有一份显式的 `DENIED_*` 清单。

**还有一条容易忘的**：安全不能依赖模型自觉。实测模型有时会主动拒绝危险请求，
但那只是它「觉得」不安全 —— 万一它觉得安全，工具层就必须自己拦住。

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
- [x] **第 2 周**：`agent/tools/`，模型能自主调工具（v0.3）
  - [x] `agent/tools/base.py` —— 工具基类 + Schema 生成
  - [x] `agent/tools/registry.py` —— 注册表 + 自动发现
  - [x] `agent/tools/get_time.py` —— 第一个工具
  - [x] `llm.py` 支持 `tools` 与 `tool_calls`
  - [x] `cli.py` 加 `--tools` 开关
  - [x] `loop.py` 的 `run_with_tools()`（3 处 TODO 已补齐）
  - [x] 问「现在几点」能自动调 `get_time`
  - [x] 坏 JSON 不崩溃（`ToolCall.parsed_arguments()` 兜底）
  - [x] `max_iterations` 能拦住死循环（实测调用 3 次即停）
  - [x] `read_file` 工具 —— 路径校验 + 敏感文件拒绝（TODO ① 已补）
  - [x] `run_shell` 工具 —— 白名单 + 超时 + 固定工作目录（TODO ② 已补）
  - [x] 路径逃逸全部被拒（`../.env` / `..\..\.env` / `/etc/passwd` / `C:\Windows\...`）
  - [x] 项目内敏感文件被拒（`.env` / `.git/` / `.venv/`）—— 路径限制挡不住这层
  - [x] 危险命令全部被拒（`del` / `rm -rf` / `curl|iex` / `format` / `reg` / `powershell`）
  - [x] 白名单大小写与 `.exe` 归一化（`ECHO` / `dir.exe`）

  > 上面两条「被拒」是**工具层强制**的，与模型自己愿不愿意调无关。
  > 实测模型有时会主动拒绝危险请求，但那是它「觉得」不安全，不是「不能」——
  > **安全必须由工具层兜底，不能指望模型自觉。**
- [ ] 第 3 周：会话持久化 + 上下文裁剪 + 长期记忆
- [ ] 第 4 周：HTTP 接口 + 测试 + 打包
