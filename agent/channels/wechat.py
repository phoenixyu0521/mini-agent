"""微信公众号的消息协议：验签、解析、构造回复。

## 只用协议，不需要企业资质

    这份代码同时适用三种账号：

        公众号**测试号**   免费、扫码即得，个人就能用 —— 先拿它把链路跑通
        个人主体订阅号     注册免费，只能在 5 秒内被动回复
        认证服务号         多一个「客服消息」接口，可以延迟推送

    三者协议完全一样，差别只在权限。所以先用测试号做，将来换正式号
    只需要改 appID / appSecret / token 三个值。

## 5 秒这道墙（本文件之外，但必须先知道）

    微信要求服务器在 **5 秒内**给它一个回复（可以是空串）。
    超时它会**重发同一条消息，最多 3 次**。

    而 mini-agent 回答一个问题要 10–16 秒。所以天真的写法是：

        微信发 → 你跑 15 秒 → 微信 5 秒超时重发 → 你又跑一遍
               → 用户收到 3 条一样的回复，工具被执行 3 次

    这就是 `Inbound.msg_id` 存在的理由：**重发必须能被识别出来并丢掉。**

## 安全

    任何知道 URL 的人都能 POST 到这里。所以：
      · 必须先验签（signature = sha1(sort(token, timestamp, nonce))）
      · 必须限制请求体大小（XML 实体膨胀是经典攻击）
      · 解析时不用外部实体
"""

from __future__ import annotations

import hashlib
import hmac
import time
import xml.etree.ElementTree as ET

from agent.channels import Inbound, safe_user_id

# 微信文本消息上限约 2048 字节（UTF-8）。超过会被截断或拒收，
# 所以我们主动切，并留出余量给「…（已截断）」这类提示。
MAX_REPLY_BYTES = 2000
MAX_BODY_BYTES = 64 * 1024      # 正常消息只有几百字节，64K 已经很宽松


def verify_signature(token: str, signature: str, timestamp: str, nonce: str) -> bool:
    """微信的验签：把 token/timestamp/nonce 排序后拼起来做 sha1。

    注意是**字典序排序**，不是按参数顺序拼 —— 少排这一步，永远对不上。
    """
    if not (token and signature and timestamp and nonce):
        return False
    raw = "".join(sorted([token, timestamp, nonce]))
    expect = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    # 用 compare_digest 而不是 ==：避免按字符比较泄露前缀信息（时序攻击）。
    # 注意它在 hmac 里，不在 hashlib 里 —— 写错是 AttributeError，不是逻辑错。
    return hmac.compare_digest(expect, signature.lower())


def parse_message(xml_text: str) -> Inbound | None:
    """把微信推来的 XML 变成 `Inbound`。

    返回 None 表示「这条不该交给 agent」：
      · 不是文本消息（图片/语音/位置/事件…）
      · 是事件里的「进入会话」这类噪音
    """
    if not xml_text or len(xml_text.encode("utf-8")) > MAX_BODY_BYTES:
        return None
    try:
        # ElementTree 不解析外部实体，但实体膨胀仍然存在 ——
        # 所以上面先卡了请求体大小。
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    def field(name: str) -> str:
        node = root.find(name)
        return (node.text or "").strip() if node is not None else ""

    msg_type = field("MsgType")
    if msg_type != "text":
        return None

    user = safe_user_id(field("FromUserName"))
    text = field("Content")
    if not user or not text:
        return None

    return Inbound(
        channel="wechat",
        user_id=user,
        text=text,
        # MsgId 是微信给每条消息的唯一编号，正是我们要的幂等键。
        # 没有就退回 CreateTime —— 至少同一条消息重发时它不变。
        msg_id=field("MsgId") or f"t{field('CreateTime')}",
        raw={"msg_type": msg_type, "to": field("ToUserName")},
    )


def build_text_reply(to_user: str, from_user: str, content: str) -> str:
    """构造被动回复的 XML。

    两个容易踩的点：
      ① 时间字段叫 `CreateTime`（不是 CreateAt），且是秒级整数
      ② 所有文本字段都要 CDATA 包裹，否则内容里的 `&` `<` 会破坏 XML
    """
    body = _clip(content)
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{_cdata(to_user)}]]></ToUserName>"
        f"<FromUserName><![CDATA[{_cdata(from_user)}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{_cdata(body)}]]></Content>"
        "</xml>"
    )


def build_empty_reply() -> str:
    """空回复：告诉微信「收到了，别再重发」，但不给用户任何内容。

    这是超时场景的正确应答 —— 比「返回一个『正在思考』」要好：
    后者会在用户那儿留下一条永远不更新的消息。
    """
    return "success"


TRUNC_SUFFIX = "\n…（已截断）"


def _clip(text: str) -> str:
    """按**字节**截断，不是按字符。

    中文一个字 3 个字节，按字符截会在长回答上超出微信的 2048 字节上限，
    表现是整条消息发送失败（用户什么都收不到）——**静默失败**，最难查。

    留的余量必须按**后缀的实际字节数**算，不能拍一个「差不多 12」：
    后缀 `\\n…（已截断）` 一个字符 1 字节、两个标点共 15 字节，合计 19 字节。
    实测拍 12 的时候，结果刚好超出 5 字节。
    """
    data = text.encode("utf-8")
    if len(data) <= MAX_REPLY_BYTES:
        return text
    budget = MAX_REPLY_BYTES - len(TRUNC_SUFFIX.encode("utf-8"))
    # errors="ignore" 是为了不把多字节字符切成半个（会显示成 �）
    clipped = data[:budget].decode("utf-8", errors="ignore")
    return clipped + TRUNC_SUFFIX


def _cdata(text: str) -> str:
    """CDATA 里的内容**不做 HTML 转义**。

    这里踩过一次：先写了 `escape(text)`，结果 `<` 变成 `&lt;` 原样显示给用户 ——
    因为 CDATA 里的一切都是字面量，转义等于把转义符号也写进了内容。

    CDATA 里唯一能出事的序列是 `]]>`（会提前结束 CDATA）。
    标准处理是把它拆成两段 CDATA：`]]>` → `]]]]><![CDATA[>`
    """
    return text.replace("]]>", "]]]]><![CDATA[>")
