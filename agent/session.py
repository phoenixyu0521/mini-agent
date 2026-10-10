"""会话持久化 —— 第 3 周的第一个文件。

## 先想清要解决什么问题

第 1 周你踩过一次这个坑：每次执行 `uv run python cli.py "..."` 都是**一个新进程**，
`self.messages` 从空列表开始，所以模型每次都不记得你。

「记忆」现在只活在 Python 进程的内存里，进程一退出就没了。
第 3 周要做的第一件事，就是把它**写到磁盘上**，让下次启动能读回来。

## 为什么选 JSONL（一行一个 JSON 对象），而不是一个大 JSON 数组？

想象两种文件长什么样：

    方案 A：一个 JSON 数组          方案 B：JSONL，一行一个
    [                              {"type":"session",...}
     {"role":"user",...}           {"type":"message",...}
     {"role":"assistant",...}      {"type":"message",...}
     {"role":"assistant",...}      {"type":"message",...}
    ]                              ...

差别在**怎么写入**：

    数组方案：追加一句话要把整个文件读出来 → 加一条 → 整个写回去。
              文件越大越慢，而且中途崩了就把**整个历史**一起弄坏。

    JSONL  ：只用「追加」模式打开（mode="a"），把新的一行写到文件末尾。
              永远不用重写历史，崩了最多丢最后一行，前面的完好无损。

这是**日志型数据**的通用做法 —— 追加便宜、损坏范围小、能按行读。
ROADMAP 说「一行一个事件」，就是这个意思。

## 为什么每行要包一层 {"type": ..., ...}，而不是直接写 message？

因为 session 文件里不只有消息。以后还会有别的东西要往同一个文件里记：

    {"type": "session", ...}   ← 开场：什么时候开的、system 提示词是什么
    {"type": "message", ...}   ← 一条对话消息
    {"type": "compact", ...}   ← （第 3 周后半段做上下文压缩时会用到）
                                ← 「我在第 40 条消息处做过一次裁剪，摘要是什么」

如果直接写裸 message，你就分不清「这行是消息」还是「这行是别的什么」。
有了 type 字段，读的时候 `if row["type"] == "message"` 就能筛出来，
而且**以后加新类型不用改老文件的格式**。

这叫 append-only（只追加）的设计思路，跟 git 的思路是一样的。

## 这个文件里不出现的东西

**不存 API Key，不存 system 之外的东西。**
system 提示词存了（它是要恢复对话必需的），但它不是机密；
`.env` 里的 Key 跟会话历史无关，永远不要写进 session 文件。

## 安全

session 文件里是**你和模型的对话内容**，可能包含私人信息。
所以：
    - 存在项目内的 `data/sessions/`，并加进 .gitignore（绝不能进 Git）
    - 文件名不带任何用户信息，只有时间戳
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


# session 文件放在哪。放在项目内而不是用户主目录 ——
# 理由和 .env 一样：项目边界清晰，不会污染用户的其他目录。
SESSIONS_DIR = Path(__file__).resolve().parents[1] / "data" / "sessions"


def new_session_id() -> str:
    """生成一个会话编号。

    格式：`20261010-140032` —— 年月日-时分秒。

    为什么不加随机后缀？
        一天开两个会话就会撞名。但那真的很少发生，而撞名的后果只是
        第二次覆盖第一次（不至于报错）。用时间戳的好处是**排序天然正确**，
        文件名按字典序排 = 按时间排，不用查数据库。
    """
    return time.strftime("%Y%m%d-%H%M%S")


class Session:
    """一个会话的落盘与读回。

    用法（三步，顺序固定）：
        s = Session.new()          # 或 Session.load("20261010-140032")
        s.start(system_prompt)     # 只在建新会话时调
        s.append(user_message)
        s.append(assistant_message)
        ...
        s.messages()               # 读回全部消息

    注意 Session 不负责「什么时候存」——那是 AgentLoop 的事。
    这里只管「怎么写、怎么读」，一件事一件事来。
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.path = SESSIONS_DIR / f"{session_id}.jsonl"

    # ── 建 / 读 ──────────────────────────────────────────────

    @classmethod
    def new(cls) -> "Session":
        """开一个新会话（还不落盘，等 start() 才创建文件）。"""
        return cls(new_session_id())

    @classmethod
    def load(cls, session_id: str) -> "Session":
        """打开一个已存在的会话（如果编号不存在也不报错，读的时候才处理）。"""
        return cls(session_id)

    @classmethod
    def list_sessions(cls, limit: int = 20) -> list[str]:
        """列出最近的会话编号，最新的在前。

        这个方法是**脚手架**，已经写好了 —— 它只是「读目录 + 按文件名倒序」，
        没有任何 Agent 原理在里面。你的第 3 周 TODO 不包括它。

        为什么按文件名排序就等于按时间排序？
            因为 new_session_id() 用的是 %Y%m%d-%H%M%S，
            这个格式的字典序和真实时间顺序**完全一致**
            （位数固定、先比年月再比日时分秒）。
        """
        if not SESSIONS_DIR.exists():
            return []
        ids = [p.stem for p in SESSIONS_DIR.glob("*.jsonl")]
        return sorted(ids, reverse=True)[:limit]

    # ── 写 ──────────────────────────────────────────────────

    def start(self, system_prompt: str) -> None:
        """写入开场行，并创建文件。

        为什么开场行要单独存 system？
            因为恢复对话时必须知道当时用的是哪个 system。
            现在你改了 DEFAULT_SYSTEM 再去读旧会话，如果硬用新的，
            恢复出来的对话就不是「原来那次」了。

        ⚠️ 这里会覆盖同名文件。如果 session_id 撞了名，上一次的记录就没了。
        这是可接受的（第 1 周说过：一秒内开两个会话的概率极低）。
        """
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        header = {
            "type": "session",
            "id": self.session_id,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "system": system_prompt,
        }
        # 编码必须写 utf-8：Windows 默认是 GBK，中文对话会写出一堆乱码，
        # 读回来就是一堆 U+FFFD 替换字符。这是本机已踩过的坑。
        self.path.write_text(
            json.dumps(header, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    # ─────────────────────────────────────────────────────────
    # TODO ① —— 追加一条消息
    #
    #   要做的事只有一句：**用追加模式打开文件，把一行 JSON 写进去。**
    #
    #   骨架已经给你了文件对象和内容，重点是「追加」这两个字：
    #
    #   1) 打开文件，模式是 "a"（append，追加）。
    #      对比一下三种模式，你应该能说出区别：
    #          "w" —— 清空后重写（用这个会**丢掉全部历史**）
    #          "a" —— 在末尾追加（**这个才对**）
    #          "r" —— 只读
    #
    #   2) 这条记录长这样：
    #          {"type": "message", "at": <时间戳字符串>, "message": <那条消息>}
    #
    #      为什么 message 要包在 "message" 这个键里？
    #          因为外层是信封（type / at），内层才是内容。
    #          将来可能有 {"type":"compact", ...} 这样的行混在同一个文件里，
    #          结构一致才好筛。
    #
    #   3) json.dumps(..., ensure_ascii=False) + "\n" 写出去。
    #      ensure_ascii=False 的作用是让中文原样写进文件而不是变成
    #      \uXXXX 转义（那样文件能读，但人眼完全没法看）。
    #      那个 "\n" 别忘 —— **没有换行，下一条会和这条粘成一行，整个文件就废了。**
    #
    #   要动手前先回答一个问题：
    #   Q：为什么这里用 with open(...) as f:，写完不用管关闭的事？
    #       提示：with 块结束时 Python 会自动关闭文件句柄。
    #       如果不用 with，程序中途崩了就可能不关，Windows 上文件会一直被占着。
    # ─────────────────────────────────────────────────────────
    def append(self, message: dict[str, Any]) -> None:
        record = {
            "type": "message",
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "message": message,
        }
        raise NotImplementedError("TODO ① 还没补：append() 还没实现")

    # ── 读 ──────────────────────────────────────────────────

    # ─────────────────────────────────────────────────────────
    # TODO ② —— 把文件读回成一个 messages 列表
    #
    #   返回值是 list[dict]，形状必须和 AgentLoop 里的 self.messages 完全一致，
    #   这样才能直接 self.messages = session.messages() 顶上。
    #
    #   要做的四件事：
    #
    #   1) 文件可能不存在（比如 Session.load 传了个错编号）。
    #      不存在时返回**空列表**并让调用方知道。
    #      —— 这里不抛异常，理由和 registry.call() 一样：持久化是辅助功能，
    #         读不到旧会话不该让整个程序崩掉，退化成「新会话」就行。
    #
    #   2) 用 open(..., encoding="utf-8") 逐行读。**encoding 绝对不能省**
    #      （Windows 默认 GBK，读 utf-8 文件会直接 UnicodeDecodeError）。
    #
    #   3) 每行 json.loads()，然后**只收 type == "message" 的那些**，
    #      取它们的 row["message"]。
    #      为什么筛？因为同一文件里还有 type == "session" 的开场行
    #      （以及以后可能的 compact 行），它们不是消息，不能进 messages。
    #
    #      ⚠️ 坏行怎么办？某一行 json.loads 抛异常时，**跳过它继续读**，
    #         不要整个函数失败。理由：JSONL 的好处就是崩了只坏一行。
    #         （这正是第 2 周反复出现的同一个模式：局部失败不该拖垮全局）
    #
    #   4) ⚠️ 最关键的一步：**返回的第一条要补上 system 消息。**
    #
    #      你可能会困惑：system 不是已经写在文件第一行了吗，为什么还要补？
    #      想清楚这一点，你就真的理解「messages 列表」了：
    #
    #          文件里的开场行 system 是「存档」，是给人看的；
    #          self.messages[0] 的 system 是「现场」，是要发给模型的。
    #          **模型只认它收到的 messages 列表，它看不到磁盘上的任何文件。**
    #
    #      所以正确做法是：读文件时从第 2 行（type=="session"）取出 system 字段，
    #      拼成 {"role": "system", "content": 那个字符串} 放在列表最前面。
    #
    #   动手前先回答：
    #   Q：假设你偷懒不补 system 这条，会发生什么现象？
    #       （提示：模型仍然会记得你说过的话，因为它能看到 user/assistant；
    #         但它的「身份」和「说话风格」会突然变成默认值）
    # ─────────────────────────────────────────────────────────
    def messages(self) -> list[dict[str, Any]]:
        raise NotImplementedError("TODO ② 还没补：messages() 还没实现")

    # ── 调试用 ──────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        """给 /history 命令用的一行摘要。脚手架，已写好。"""
        if not self.path.exists():
            return {"id": self.session_id, "exists": False}

        rows: list[dict[str, Any]] = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        messages = [r["message"] for r in rows if r.get("type") == "message"]
        roles: dict[str, int] = {}
        for m in messages:
            roles[m.get("role", "?")] = roles.get(m.get("role", "?"), 0) + 1

        return {
            "id": self.session_id,
            "exists": True,
            "created_at": next(
                (r.get("created_at") for r in rows if r.get("type") == "session"), "?"
            ),
            "message_count": len(messages),
            "roles": roles,
            "path": str(self.path),
            "size_bytes": self.path.stat().st_size,
        }