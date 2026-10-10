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
│   ├── session.py      # 【第 3 周】会话持久化（JSONL）
│   ├── context.py      # 【第 3 周】上下文裁剪
│   ├── memory.py       # 【第 3 周】长期记忆（跨会话，防被裁剪）
│   └── tools/          # 【第 2 周】工具层
│       ├── __init__.py
│       ├── base.py       # 工具基类 + Schema 生成
│       ├── registry.py   # 注册表 + 自动发现
│       ├── get_time.py   # 工具 1：读系统时间（无副作用）
│       ├── read_file.py  # 工具 2：读项目内文件（必须做路径校验）
│       └── run_shell.py  # 工具 3：执行只读命令（必须做白名单）
├── cli.py              # 命令行入口
├── data/sessions/      # 【第 3 周】会话记录（JSONL，已在 .gitignore 中）
├── MEMORY.md           # 【第 3 周】长期记忆（你自己也能编辑，已在 .gitignore 中）
├── .venv/              # 虚拟环境（uv 自动创建，不进 Git）
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

uv run python cli.py --sessions                    # 【第 3 周】列出最近的会话
uv run python cli.py --resume 20261010-142117      # 恢复指定会话
uv run python cli.py --new-session                 # 强制开新会话
uv run python cli.py --no-compact                  # 【第 3 周】关掉上下文压缩
uv run python cli.py --no-memory                   # 【第 3 周】关掉长期记忆
```

交互模式内置命令：`/reset` 清空上下文，`/history` 查看消息列表，
`/tokens` 看裁剪是否生效，`/compact` 看压缩统计，
`/memory` 看长期记忆内容，`/remember <内容>` 写一条长期记忆，`/exit` 退出。

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
- [ ] **配置化 `agent/config.py`**

  > `reset()` 故意**不**清磁盘文件：文件是历史流水账，不是聊天窗口。
  > 清空窗口 ≠ 删除聊天记录 —— 所以 `/reset` 后接着写，旧记录仍在。
- [ ] 第 4 周：HTTP 接口 + 测试 + 打包
