"""长期记忆 —— 第 3 周的第三个文件，也是「记得住」这件事的终点。

## 先看清它和 session 的区别

你已经在第 3 周做过 session 持久化了。既然历史都能存进文件了，
为什么还要另一个 memory？——因为它们解决的是**两个不同的问题**：

| | session（你已经做的） | memory（现在要做的） |
|---|---|---|
| 是什么 | 原始流水账 —— 每一句话都记 | 提炼后的结论 —— 只记「不变的」 |
| 大小 | 大、精确、会长到几万 token | 小、抽象、几十行 |
| 生命周期 | 一次会话 | **跨所有会话**，永远活着 |
| 会被裁剪吗 | 会（context.py 会把早期压成摘要） | **不会，永远常驻** |
| 类比 | 聊天记录 | 你给朋友画的「个人档案卡」 |

**核心问题在这里**：压缩摘要能救你，但不可靠。

    「我叫旭」这句话在第 12 条时会被压掉 → 只剩摘要里可能提到
    摘要写得粗糙 → 这条信息在当前请求里就没了 → 模型答「你没告诉过我名字」

而「用户叫旭」这种东西，**它是不会变的**。不该靠每次压缩去赌它有没有被保留下来 ——
它应该写进一个**永久档案**，每次对话开头都塞进去。

这就是 ROADMAP 说的：「跨会话的偏好怎么记住？→ 单独维护 MEMORY.md，只注入相关片段」。

## 两个层次：读和写

    load()   读出 MEMORY.md 的内容 → 拼成一条 system 消息 → 注入对话开头
    append() 把新学到的事实追加进 MEMORY.md

## 为什么格式要长成「- 键: 值」这样

因为**两个用户**都要读它：

    人（你）      打开 MEMORY.md 一眼扫过去，得能看懂
    模型         注入的时候是一整段文本，得能读明白

用 Markdown 列表是最省事的方案：人看着舒服，模型也认。
ROADMAP 提到的 nanobot 用 `agent/memory.py` 管这件事，
但它内部也是存成你写的那种人类可读的笔记文件。

## 为什么是「追加」而不是「覆盖」

因为记忆是**只增不减**的：
    学到新东西 → 追加一行
    发现旧的可能错了 → 也追加一行（或者手工去改文件）

如果每次覆盖整个文件，两条路：
    a. 先读全文件 → 改 → 全写回 —— 崩了毁全部记忆
    b. 只写新内容 —— 但那样会丢掉旧记忆

**方案：追加。** 和 session 的 JSONL 是同一个思路 —— 追加便宜、损坏范围小。
而且人可以随时手工编辑这个文件，改错了也不会让程序崩
（因为下面 load() 的设计就是：读不懂的行直接跳过）。

## 边界（先说清楚，避免过度期待）

第 3 周的 memory 是**最小可用版**：

    ✅ 手工写：你在 MEMORY.md 里写一行，下次对话它就知道了
    ✅ 程序追加：对话中明确说的「记住 X」，可以写进去
    ❌ 不会自动判断什么值得记住 —— 那是 nanobot 的 Dream 任务干的，
       需要额外一次模型调用去「复盘今天的对话」
"""

from __future__ import annotations

from pathlib import Path

# 记忆文件放哪。放在项目根目录（和 README 同级），
# 理由：这是「给人看的档案」，不是程序内部数据 —— 你要能方便地打开编辑它。
# （对比：data/sessions/ 是程序用的，放 data/ 下面。）
MEMORY_PATH = Path(__file__).resolve().parents[1] / "MEMORY.md"

# 注入对话时给这段内容加的前缀。
# 为什么要前缀？不加的话，模型看到一堆「- 名字: 旭」会以为这是什么任务数据。
# 加了前缀它才知道「这是我的长期背景资料」。
INJECTION_HEADER = "以下是你对这个用户的长期记忆（跨会话保存，可能不完整）："

# MEMORY.md 文件的初始内容。第一次运行时会自动创建。
TEMPLATE = """# 长期记忆

> 这个文件由 `agent/memory.py` 管理。你可以直接用手编辑它 —— 结果立刻生效。
> 格式随意，但建议每行写成 `- 键: 值`，例如 `- 名字: 旭`。
> 读不懂的行会被自动跳过，不会让程序报错。

## 关于用户
- 名字: 旭
"""


class Memory:
    """长期记忆的读写。**没有继承、没有复杂结构，就是一个文件的两端。**"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or MEMORY_PATH

    # ── 读 ──────────────────────────────────────────────────

    def ensure_exists(self) -> bool:
        """确保记忆文件存在。新建了返回 True，本来就存在返回 False。

        为什么要「第一次自动创建」而不是直接报错？
            因为「还没写过记忆」是**正常状态**，不是错误 ——
            新用户第一次跑不该看到一个 FileNotFoundError。
            这和 session.load_session() 返回 bool 是同一个设计思路。
        """
        if self.path.exists():
            return False
        self.path.write_text(TEMPLATE, encoding="utf-8")
        return True

    # ─────────────────────────────────────────────────────────
    # TODO ① —— 读出记忆，拼成一条能注入 messages 的 system 消息
    #
    #   返回：
    #       有记忆  → 一条 dict，形如 {"role": "system", "content": "..."}
    #       没记忆/读不到/文件是空的 → **None**（要用 None 表示「不用注入」）
    #
    #   为什么「没记忆」要返回 None 而不是空字符串的消息？
    #       因为一条 content="" 的 system 消息对模型毫无意义，
    #       而且会白白占 token。**「没有」就该表示成「没有」。**
    #
    #   步骤（四步，都不难）：
    #
    #   1) 文件不存在 → 直接 return None。
    #      （别在这里偷偷创建它 —— 读的函数只负责读。创建是 ensure_exists 的事。
    #       这叫「单一职责」。）
    #
    #   2) 读文件内容。⚠️ 两个要点：
    #         - open(..., encoding="utf-8")，encoding 不能省（Windows 默认 GBK）
    #         - .strip() 一下，去掉开头结尾的空行
    #
    #   3) 如果 strip 之后是空字符串 → return None。
    #      为什么？因为一个只有空白的档案没有任何信息量，
    #      注入进去只是浪费 token。**空内容 = 没内容。**
    #
    #   4) 拼成消息返回：
    #         content = INJECTION_HEADER + "\n" + 文件内容
    #         return {"role": "system", "content": content}
    #
    #      为什么用 role="system" 而不是塞进已有的 system 消息里？
    #          两种都能跑。分开成两条 system 的好处是**边界清晰**：
    #          将来想「临时停用长期记忆」，只要不注入这一条就行，
    #          不用去已有的 system 字符串里做手术。
    #
    #   动手前先回答：
    #   Q：如果记忆文件特别长（比如你往里塞了 5000 字），会有什么后果？
    #       （提示：它每一轮都会被发出去）
    # ─────────────────────────────────────────────────────────
    def load(self) -> dict[str, str] | None:
        # 1) 文件不存在 → 没有记忆。注意：读的函数不负责创建文件
        if not self.path.exists():
            return None

        # 2) 读内容。encoding 不能省，Windows 默认 GBK 会把中文读成乱码
        text = self.path.read_text(encoding="utf-8").strip()

        # 3) 全是空白 = 没有内容。空消息对模型没意义，还白占 token
        if not text:
            return None

        # 4) 加上前缀，拼成一条独立的 system 消息
        return {
            "role": "system",
            "content": INJECTION_HEADER + "\n" + text,
        }

    # ── 写 ──────────────────────────────────────────────────

    # ─────────────────────────────────────────────────────────
    # TODO ② —— 把一条新记忆追加进文件
    #
    #   输入是已经拼好的**一行**文本，比如 "- 职业: 建筑学大三学生"。
    #   你的任务是把它安全地追加到文件末尾。
    #
    #   要做的三件事：
    #
    #   1) 先确保文件存在 —— 直接调 self.ensure_exists() 就行。
    #      为什么不直接 open(..., "a") 就完事？
    #          "a" 模式确实会**自动创建**不存在的文件，所以严格说不用调。
    #          但代价是：新建出来的文件**没有 TEMPLATE 那个标题和说明**，
    #          你会得到一个光秃秃只有一个 "- xxx" 的文件。所以还是先 ensure 一下。
    #
    #   2) 用追加模式打开，把 line 写进去，**末尾加换行 "\n"**。
    #      ⚠️ 那个 "\n" 和第 3 周 session 的 append() 是同一个坑：
    #         没有它，下一条记忆会和这条粘在同一行，人就看不明白了。
    #
    #      ⚠️ line 本身可能已经带了换行（调用方不确定），
    #         所以可以写 line.rstrip("\n") + "\n" 来规范化。
    #         这一手叫「防御性编程」：不假设调用方一定做对了。
    #
    #   3) 空的 line 怎么办？想一下：如果 line 是 "" 或空白，
    #      追加进去就是往文件里塞空行 —— 毫无意义，还会让文件越来越乱。
    #      所以**一开头就该拦掉**，直接 return，什么都不写。
    #
    #   动手前先回答：
    #   Q：为什么这里用追加而不用覆盖写？想想 memory 和 session 的共同点。
    # ─────────────────────────────────────────────────────────
    def append(self, line: str) -> None:
        # 1) 空行直接扔。写空行只会让文件越来越乱
        line = line.rstrip("\n")
        if not line.strip():
            return

        # 2) 先确保文件存在 —— 不然新建出来的是个没有标题说明的秃文件
        self.ensure_exists()

        # 3) 追加一行。mode="a" 只写末尾，不碰已有内容
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ── 调试用 ──────────────────────────────────────────────

    def stats(self) -> dict[str, object]:
        """给 /memory 命令用的摘要。脚手架，已写好。"""
        if not self.path.exists():
            return {"exists": False, "lines": 0, "chars": 0, "path": str(self.path)}
        text = self.path.read_text(encoding="utf-8")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return {
            "exists": True,
            "lines": len(lines),
            "chars": len(text),
            "path": str(self.path),
            # 粗估注入成本：字符数/2 ≈ token 数
            "est_tokens": len(text) // 2,
        }
