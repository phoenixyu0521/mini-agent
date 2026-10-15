"""可观测性 —— 第 4 周第二块（v0.9）。

## 先想清「可观测性」到底解决什么问题

到目前为止，这个项目出错时你能知道什么？

    程序崩了 → 看到 traceback
    程序没崩但答得不好 → **什么都看不到**

第二种才是真正折磨人的。「它刚才读了什么文件」「一共调了几次模型」
「这17 秒花在网络还是花在本地」「上下文压了几次」「token 花了多少」——
现在全是黑的。可观测性就是把这些黑箱变成能看的数据。

## 为什么是 JSONL（和第 3 周 session 一样）

因为它**根本就是同一类数据**：按时间追加、只写不改、崩了最多丢最后一行。
`data/sessions/*.jsonl` 记「说了什么」，`data/logs/turns.jsonl` 记「花了多久」。

不要为了记日志引入一个数据库或一个日志框架 —— 那是 nanobot 用 5 万行换来的东西，
你现在只需要「多写一行 JSON」。

## 三段耗时：这一块是本周的核心

一次工具调用的真实时间构成：

    [准备]  → [模型等待]  → [工具执行]  → [模型等待]  → ...
     _view()    httpx 等网络    本地跑命令      又一次网络等待
     可能触发压缩  可能是17 秒    可能 2 毫秒

如果只记一个total_ms，你只知道「慢」，不知道「慢在哪」。
但这两种慢要修的地方完全不同：

    模型等待慢  → 网络问题 / 模型问题 / 上下文太长 → 换模型、压上下文
    工具执行慢  → 你的工具写得烂（比如shell 不设超时）→ 改工具

所以 `TurnMetrics` 把时间拆成三段。**拆分不是为了好看，是为了能归因。**

## 埋点放在哪：这是本文件最需要想清楚的事

计时必须紧贴真正干活的那几行，不能包住整个 run()：

    ✅  for piece in llm.stream_chat(...)      ← 里面才是网络等待
    ❌  reply = self.run(user_text)             ← 这里还包住了压缩和落盘

原则：**计时器只包住「等别人的时间」和「自己干活的时间」两段。**
`flush_session()` 写磁盘很快（几毫秒），混进去会污染读数；
但如果哪天它变慢了，你就该知道 —— 所以它也该被单独记一段，只是本周先不记。
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator


# 日志目录。和 session 一样放项目内的 data/ 下，并在 .gitignore 里。
LOGS_DIR = Path(__file__).resolve().parents[1] / "data" / "logs"
TURN_LOG = LOGS_DIR / "turns.jsonl"


@dataclass
class TurnMetrics:
    """一轮对话（或一次 API 请求）的完整账本。

    字段按「三段耗时 + 四类计数」分组，不是随手加的：

        耗时   total_ms / prepare_ms / model_ms / tool_ms
        计数   requests / tool_calls / prompt_tokens / completion_tokens
        状态   compacted（这一轮有没有触发压缩）/ error（失败原因，None = 成功）

    为什么 compacted 值得单独记？
        因为它是**你主动花钱买的东西** —— 压缩本身要调一次模型。
        只记 token 总量的话，你会看到「压缩之后 token 反而涨了」，
        却不知道为什么。把它记下来才看得清这笔交易的账。
    """

    session_id: str
    entry: str                       # "chat" / "stream" / "tools" —— 哪个入口
    started_at: str = ""
    total_ms: float = 0.0
    prepare_ms: float = 0.0
    model_ms: float = 0.0
    tool_ms: float = 0.0
    requests: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    compacted: bool = False
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        """转成能写进 JSONL 的一行。脚手架，已写好。

        为什么把 extra 拍平进去而不是留成嵌套字典？
            因为 JSONL 后续要能被人用 grep 直接搜。
            嵌套三层就不好搜了 —— 「日志是给人看的」优先于「结构很优雅」。
        """
        row = asdict(self)
        extra = row.pop("extra") or {}
        row.update(extra)
        return row


def write(metrics: TurnMetrics) -> None:
    """把一条账本追加进 turns.jsonl。脚手架，已写好。

    为什么不在函数里吞掉异常？
        因为**记日志失败不该让对话失败**。写磁盘是辅助功能，
        磁盘满了、权限变了都不该让用户的对话崩掉 —— 和 registry.call()
        是同一个模式：局部失败不拖垮全局。
    """
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps(metrics.to_row(), ensure_ascii=False)
        with open(TURN_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # 写不了就算了，别影响主流程


class Meter:
    """一轮对话的计时器。用法见下面 run() 里的例子。

    它是**有状态的**（要记住已经过了多少时间），所以每轮 new 一个，
    不要跨轮复用 —— 复用会累积上一轮的耗时。

    为什么用 with（contextmanager）而不是 start()/stop() 两个方法？
        因为 start/stop 必须成对调用，中间任何一条 return 或异常都会漏掉 stop，
        计时器就永远停不下来。with 块结束时**自动**收尾，漏不掉。
    """

    def __init__(self, session_id: str, entry: str) -> None:
        self.m = TurnMetrics(
            session_id=session_id,
            entry=entry,
            started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        self._t0 = time.perf_counter()

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """量一段代码的耗时，name 取 "prepare" / "model" / "tool"。

        它会自动把 "model" 映射到字段 **model_ms**。
        为什么要这层映射，而不是让调用方自己写 "model_ms"？
            因为「段名」和「字段名」是两件事：段名是一个标识，
            字段名带了单位。让调用方自己拼，迟早会写错一次
            （写成 model_millis 就会静默新建一个字段，指标永远是 0）。
            **静默错比报错难发现得多** —— 这里宁可多一层。

        这里有**一个容易忽略的正确写法**：
        累加用的是 `+=` 而不是 `=`，并且不重置基准时间。

        为什么？
            因为一轮里可能有 5 次 model 阶段、3 次 tool 阶段。
            如果用 `=` 覆盖，最后一次会把前面的吃掉，total 就对不上。
            用 `+=` 才是「各段累加」。

        为什么用 time.perf_counter() 而不是 time.time()？
            perf_counter 专门量耗时，不受系统时钟调整影响（用户改时区、NTP 校时
            都可能让 time.time() 出现负数或跳变）。量耗时就该用它。

        名字不存在时会**立刻报错**（不静默新建字段）——
        拼错段名应该在第一次就炸掉，而不是让指标一直是 0 却没人发现。
        """
        field = f"{name}_ms"
        if not hasattr(self.m, field):
            raise AttributeError(
                f"没有这一段：{name!r}（TurnMetrics 里没有字段 {field!r}）"
            )
        t = time.perf_counter()
        try:
            yield
        finally:
            elapsed = (time.perf_counter() - t) * 1000
            setattr(self.m, field, getattr(self.m, field) + elapsed)

    def count_request(self, usage: dict[str, Any] | None) -> None:
        """记一次上游请求 + 它的 token 账。脚手架，已写好。

        ⚠️ 调用位置很重要：**每调一次 llm.chat / stream_chat 就要调一次**。
        漏掉的话 requests 会少算，而「一次用户请求内部调了几次模型」
        恰恰是 Agent 最重要的一个数字（第 2 周实测：一次工具调用 = 两次请求）。
        """
        self.m.requests += 1
        usage = usage or {}
        self.m.prompt_tokens += usage.get("prompt_tokens") or 0
        self.m.completion_tokens += usage.get("completion_tokens") or 0

    def note_compress(self, saving: int) -> None:
        """记一次上下文压缩。脚手架，已写好。"""
        self.m.compacted = True
        self.m.extra["compact_saving"] = saving

    def finish(self, error: str | None = None) -> TurnMetrics:
        """收尾：算总耗时、写盘、返回账本。脚手架，已写好。

        为什么总耗时是「从 new 到 finish」而不是三段相加？
            因为三段加起来未必等于总耗时 —— 中间还有 Python 自己的开销
            （构造 messages、序列化 JSON、拼字符串）。两个数都留着，
            你能看出「差值大」= 有没被归因的时间漏掉了。
        """
        self.m.total_ms = (time.perf_counter() - self._t0) * 1000
        self.m.error = error
        write(self.m)
        return self.m


# ══════════════════════════════════════════════════════════════
#  聚合：把一堆单轮账本变成「这个进程一共干了什么」
# ══════════════════════════════════════════════════════════════


def read_all(limit: int = 500) -> list[dict[str, Any]]:
    """读回最近 limit 条账本（原始 dict，坏行跳过）。脚手架，已写好。"""
    if not TURN_LOG.exists():
        return []
    rows: list[dict[str, Any]] = []
    with open(TURN_LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 坏行跳过，同 session.messages()
    return rows[-limit:]


# ─────────────────────────────────────────────────────────────
# TODO ① —— 聚合成一份汇总
#
#   要写一个 summarize(rows) -> dict[str, Any]，返回类似：
#
#       {
#         "turns": 12,                    # 一共几轮
#         "failed": 1,                   # 几轮出错
#         "requests": 20,                # 一共几次上游请求
#         "tool_calls": 8,
#         "prompt_tokens": 1240,
#         "completion_tokens": 860,
#         "avg_total_ms": 3100.5,        # 平均总耗时
#         "avg_model_ms": 2900.0,        # 平均模型等待
#         "avg_tool_ms": 12.0,           # 平均工具执行
#         "slowest_ms": 17400.0,         # 最慢的一轮
#       }
#
#   动手前先想清楚四件事，每一件都有坑：
#
#   Q1：rows 可能**是空的**（第一次运行、日志被删了）。
#       空列表时你的除法会ZeroDivisionError。
#       → 想想除了「报错」，还能返回什么？（提示：前端要显示「还没有数据」）
#
#   Q2：avg_tool_ms 的分母该是什么？
#       全部轮数？还是「调了工具的那些轮数」？
#       两种都讲得通，但**含义完全不同** ——
#       前者会告诉你「工具在整体里占多少」，后者告诉你「用工具时它多慢」。
#       你选哪个，为什么？（提示：看它是为了回答什么问题）
#
#   Q3：失败的那几轮，耗时还算不算？
#       比如一个 HTTP 500 是 0.3 秒快速失败，一个超时是 60 秒。
#       都算进平均，会不会被「快速失败」拉低，从而**掩盖**真正的慢问题？
#
#   Q4：avg_model_ms 到底该除以什么？
#       提示：requests 和 turns 不相等 —— 一次工具调用内部有2 次模型请求，
#       但对外只算 1 轮。用 turns 当分母算出来的「平均模型等待」
#       和 requests 当分母算出来的，**差了一倍多**。
#       先想清你要回答的是哪个问题，再选分母。
#
#   最后：avg_total_ms 该不该把失败轮排除掉？
#       这个问题没有标准答案，取决于你要回答什么 ——
#       但**你必须能说出你选了哪个，以及为什么**。
#
# ── 下面是本项目的取舍（2026-10-15 定）────────────────────
#   四个问题的答案都写在这里了。**重点不是这四个答案本身，
#   而是「先问清楚要回答什么问题，再选口径」这个做法。**
#   换一个要回答的问题，口径就该换 —— 指标是工具，不是标准答案。
#
#   Q1 空列表 → 返回 {"turns": 0, "empty": True}。
#       前端要能显示「还没有数据」，不能因为没数据就500。
#       注意别返回 {} —— 空 dict 在前端看起来像「加载中」，
#       显式的 empty 字段才能区分「无数据」和「请求失败」。
#
#   Q2 avg_tool_ms 分母 = **调过工具的那些轮数**。
#       问的是「用工具时它有多慢」，不是「工具在整体里占多少」。
#       两者差很多：20 轮里只有 2 轮用工具，除 20 会显得工具特别快，
#       掩盖掉「这个工具本身要3 秒」的事实。
#       顺带一提：一轮里可能调 3 次工具，平均的是**轮**不是**次**——
#       想看单次耗时得去 /api/metrics/raw。
#
#   Q3 **失败轮一律排除**（分母只算成功的）。
#       一次 HTTP 500 可能 0.3 秒就返回了，把这种快速失败算进平均，
#       会把数字拉低，**掩盖真正的慢问题**（比如 60 秒的超时）。
#       失败的轮不是「慢的轮」，是「坏掉的轮」，混在一起平均是错的。
#       它们的信息在 failed 计数和 /api/metrics/raw 里，不在平均耗时里。
#
#   Q4 avg_model_ms 分母 = **requests**，不是 turns。
#       一次工具调用对外算 1 轮，内部却打了 2 次模型请求。
#       除 turns 会系统性**低估一半**——你会以为模型比实际快一倍，
#       然后去优化一个根本不慢的地方。
#       注意：requests 也要只数成功轮里的（与 Q3 保持同一口径）。
#
#   口径一致性原则：Q3 排除了失败轮，所以**所有**平均值都只在
#   成功的轮上计算。分母混着来是最容易犯的错 —— 分子排除了、
#   分母没排除，算出来的东西连单位都对不上。
# ─────────────────────────────────────────────────────────────
def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """把一批单轮账本聚合成一份汇总。口径见上方注释。

    返回结构（前端 /api/metrics 面板直接吃这个 dict）：
        turns / failed              这批日志里有几轮、其中几轮出错
        requests / tool_calls       上游请求总数、工具调用总次数
        prompt_tokens / completion_tokens    token 总账
        avg_total_ms / avg_model_ms / avg_tool_ms    三个平均值（毫秒）
        slowest_ms                  最慢的一轮（诊断时最有用的一行）
    """
    if not rows:
        return {"turns": 0, "empty": True}

    total = len(rows)

    # ── 计数：这些是「事实」，不涉及口径，全部数 ──────────────
    failed = sum(1 for r in rows if r.get("error"))
    requests = sum(r.get("requests") or 0 for r in rows)
    tool_calls = sum(r.get("tool_calls") or 0 for r in rows)
    prompt_tokens = sum(r.get("prompt_tokens") or 0 for r in rows)
    completion_tokens = sum(r.get("completion_tokens") or 0 for r in rows)

    # ── 平均值：只在**成功的轮**上算（Q3）─────────────────────
    ok = [r for r in rows if not r.get("error")]
    n_ok = len(ok)

    # 每个平均值的分母：分母为 0 时给 0.0 而不是崩。
    # 为什么给 0.0 而不是 None？因为前端要直接显示，
    # None 会变成 JS 里的 "null" 粘在文字中间。
    def avg(key: str, denominator: int) -> float:
        if denominator <= 0:
            return 0.0
        return sum(r.get(key) or 0.0 for r in ok) / denominator

    # Q4：模型耗时除以请求数（不是轮数）
    #     而 requests 也要只数成功轮里的 —— 否则口径不一致。
    ok_requests = sum(r.get("requests") or 0 for r in ok)

    # Q2：工具耗时除以「调过工具的轮数」，不是全部轮数
    tool_turns = sum(1 for r in ok if (r.get("tool_calls") or 0) > 0)

    return {
        "turns": total,
        "failed": failed,
        "ok_turns": n_ok,
        "requests": requests,
        "tool_calls": tool_calls,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "avg_total_ms": round(avg("total_ms", n_ok), 1),
        "avg_model_ms": round(avg("model_ms", ok_requests), 1),
        "avg_tool_ms": round(avg("tool_ms", tool_turns), 1),
        # ⚠️ max() 在空序列上会 ValueError，**不是**返回 0。
        #    所以这里要单独判 —— 这是本函数里唯一一个不能靠 avg()兜底的点。
        "slowest_ms": round(max((r.get("total_ms") or 0.0 for r in ok), default=0.0), 1),
        "tool_turns": tool_turns,
    }
