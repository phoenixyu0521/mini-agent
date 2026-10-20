---
name: news-summary
description: 抓取国际 RSS 新闻源并生成中文简报。用户问「今天有什么新闻」「来份简报」「最近世界上发生了什么」时使用。
enabled: true
---

# 新闻简报

## 数据源（全部是 RSS，用 `fetch_url` 抓）

| 源 | URL | 什么时候用 |
|---|---|---|
| BBC World | `https://feeds.bbci.co.uk/news/world/rss.xml` | 默认，必抓 |
| BBC Business | `https://feeds.bbci.co.uk/news/business/rss.xml` | 用户问经济/市场 |
| BBC Technology | `https://feeds.bbci.co.uk/news/technology/rss.xml` | 用户问科技 |
| NPR | `https://feeds.npr.org/1001/rss.xml` | 需要美国视角 |
| Al Jazeera | `https://www.aljazeera.com/xml/rss/all.xml` | 需要非西方视角 |

## 流程

1. **先抓 BBC World**（一条就够开个头，不要一上来抓五个源）。
2. 用户明确问某个领域时，再补对应的源。
3. RSS 是 XML。从每个 `<item>` 里取 `<title>` 和 `<description>`。
   `<description>` 里常带 HTML 标签和 CDATA，要清掉标签只留文字。
4. 挑 **5–8 条**最重要的，按主题分组（世界 / 商业 / 科技）。
5. 用中文写，每条一到两句话。**不要逐字翻译原标题**，要讲清楚"发生了什么"。

## 输出格式

```markdown
## 今日要闻 · YYYY-MM-DD

### 世界
- **标题要点**：一到两句说明（来源：BBC）

### 商业
- ...

### 科技
- ...

---
来源：BBC World / BBC Business ｜ 时间：抓取时的UTC时间
```

## 硬规矩

- **只写抓到的内容。** 抓失败了就说抓失败了，不要用训练记忆补成"今日新闻"。
- **不编造细节。** RSS 的 description 通常只有一两句，不要扩写成三段。
- **标注来源。** 每条后面写清哪个源，用户要核对时有据可查。
- 用户要"语音播报"就直接说做不到 —— 本项目没有 TTS 能力。

## 反例（别这么做）

- ✗ 抓一个源，把同一条新闻写成五条
- ✗ 把 RSS 里的英文标题原样列出来当摘要
- ✗ 说"根据最新报道"但实际没抓任何东西
