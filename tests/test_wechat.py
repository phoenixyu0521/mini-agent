"""微信公众号协议层的测试。

这些函数不需要任何凭证、不需要公网、不需要真的连微信 ——
协议是确定性的，所以可以全部离线验证。

（真正连微信那一步只能靠测试号，但那是「凭证问题」，不是「逻辑问题」。
 把逻辑全部测死，剩下没验证的就只有「网络能不能通」这一件事。）
"""

from __future__ import annotations

import hashlib

import pytest

from agent.channels import Inbound, session_id_for
from agent.channels.wechat import (
    MAX_REPLY_BYTES,
    build_empty_reply,
    build_text_reply,
    parse_message,
    verify_signature,
)

TOKEN = "my_test_token"


def sign(token: str, timestamp: str, nonce: str) -> str:
    return hashlib.sha1("".join(sorted([token, timestamp, nonce])).encode()).hexdigest()


def text_xml(content: str = "你好", user: str = "oABC123", msg_id: str = "10001") -> str:
    return (
        "<xml><ToUserName><![CDATA[gh_abc]]></ToUserName>"
        f"<FromUserName><![CDATA[{user}]]></FromUserName>"
        "<CreateTime>1729500000</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        f"<MsgId>{msg_id}</MsgId></xml>"
    )


# ══════════════════════════════════════════════════════════
#  验签
# ══════════════════════════════════════════════════════════

def test_signature_accepts_correct():
    ts, nonce = "1729500000", "12345"
    assert verify_signature(TOKEN, sign(TOKEN, ts, nonce), ts, nonce)


def test_signature_rejects_wrong_token():
    ts, nonce = "1729500000", "12345"
    other = sign("attacker_token", ts, nonce)
    assert not verify_signature(TOKEN, other, ts, nonce)


def test_signature_rejects_missing_fields():
    """空参数不能通过 —— 否则构造一个空 signature 就能打进来。"""
    assert not verify_signature(TOKEN, "", "1", "2")
    assert not verify_signature("", "abc", "1", "2")


def test_signature_is_case_insensitive():
    """微信可能返回大写 hex，比较时不能吃这个亏。"""
    ts, nonce = "1729500000", "12345"
    assert verify_signature(TOKEN, sign(TOKEN, ts, nonce).upper(), ts, nonce)


def test_signature_order_matters():
    """必须按字典序排序后再拼 —— 直接按「token+timestamp+nonce」拼是错的。

    这条用反向构造来锁死：按未排序顺序算出的值，不能被接受。
    """
    ts, nonce = "1729500000", "12345"
    wrong = hashlib.sha1((TOKEN + ts + nonce).encode()).hexdigest()
    # 只有一种可能让它碰巧相等：排序后恰好等于这个顺序。这里选的三个值不满足。
    assert not verify_signature(TOKEN, wrong, ts, nonce)


# ══════════════════════════════════════════════════════════
#  解析
# ══════════════════════════════════════════════════════════

def test_parse_text_message():
    m = parse_message(text_xml("现在几点"))
    assert isinstance(m, Inbound)
    assert m.channel == "wechat"
    assert m.user_id == "oABC123"
    assert m.text == "现在几点"
    assert m.msg_id == "10001"


def test_parse_ignores_non_text():
    """图片/语音/事件都不该交给 agent —— 我们还没做多模态。"""
    for t in ("image", "voice", "event", "location"):
        xml = text_xml().replace("<![CDATA[text]]>", f"<![CDATA[{t}]]>")
        assert parse_message(xml) is None, t


def test_parse_rejects_broken_xml():
    assert parse_message("") is None
    assert parse_message("not xml at all") is None
    assert parse_message("<xml><unclosed>") is None


def test_parse_rejects_oversized_body():
    """实体膨胀攻击：几百字节的 XML 能展开成几个 G。按体积先拦掉。"""
    huge = "<xml>" + "<a>" * 40000 + "</xml>"
    assert parse_message(huge) is None


def test_parse_falls_back_to_create_time_when_no_msgid():
    """事件消息没有 MsgId，用 CreateTime 兜底 —— 至少重发时它不变。"""
    xml = text_xml().replace("<MsgId>10001</MsgId>", "")
    m = parse_message(xml)
    assert m is not None
    assert m.msg_id == "t1729500000"


def test_parse_empty_content_is_dropped():
    assert parse_message(text_xml(content="")) is None


# ══════════════════════════════════════════════════════════
#  构造回复
# ══════════════════════════════════════════════════════════

def test_reply_roundtrip():
    """构造出来能再解析回去，且文字一字不差。"""
    import xml.etree.ElementTree as ET

    xml = build_text_reply("oUSER", "gh_abc", "第一行\n第二行")
    root = ET.fromstring(xml)
    assert root.find("ToUserName").text == "oUSER"
    assert root.find("FromUserName").text == "gh_abc"
    assert root.find("MsgType").text == "text"
    assert root.find("Content").text == "第一行\n第二行"
    assert int(root.find("CreateTime").text) > 1_700_000_000


def test_reply_keeps_special_chars_literal():
    """CDATA 里不能做 HTML 转义。之前写成 escape()，用户会看到 &lt; 这种东西。"""
    import xml.etree.ElementTree as ET

    raw = "if a < b && c > d: 用 <xml> 举例"
    xml = build_text_reply("u", "g", raw)
    assert ET.fromstring(xml).find("Content").text == raw


def test_reply_survives_cdata_terminator():
    """内容里出现 ]]> 会提前结束 CDATA —— 必须拆段，不能直接塞。"""
    import xml.etree.ElementTree as ET

    raw = "危险序列 ]]> 结束"
    xml = build_text_reply("u", "g", raw)
    assert ET.fromstring(xml).find("Content").text == raw


def test_reply_clips_by_bytes_not_chars():
    """中文一个字 3 字节。按字符截会在 2048 字节上限处整条发送失败。"""
    long_text = "很长的中文回答。" * 200          # 1600 字符 / 4800 字节
    assert len(long_text) < MAX_REPLY_BYTES        # 字符数没超
    assert len(long_text.encode()) > MAX_REPLY_BYTES  # 但字节数超了

    xml = build_text_reply("u", "g", long_text)
    import xml.etree.ElementTree as ET

    got = ET.fromstring(xml).find("Content").text
    assert len(got.encode()) <= MAX_REPLY_BYTES
    assert got.endswith("…（已截断）")
    # 没有把多字节字符截断成半个（否则会出现 U+FFFD）
    assert "\ufffd" not in got


def test_empty_reply_is_success_string():
    """空回复必须正好是 `success` —— 微信据此判断「已收到，别重发」。"""
    assert build_empty_reply() == "success"


# ══════════════════════════════════════════════════════════
#  会话身份
# ══════════════════════════════════════════════════════════

def test_session_id_is_stable():
    """同一个人的 id 必须每次算出来一样，否则「它忘了昨天」."""
    a = session_id_for("wechat", "oABC123")
    b = session_id_for("wechat", "oABC123")
    assert a == b
    assert a.startswith("wechat-")
    assert len(a) == len("wechat-") + 16


def test_session_id_differs_per_user_and_channel():
    assert session_id_for("wechat", "oAAA") != session_id_for("wechat", "oBBB")
    # 同一个人在不同渠道是不同会话：微信里说的是私事，网页上可能是工作
    assert session_id_for("wechat", "oAAA") != session_id_for("web", "oAAA")


def test_session_id_has_no_unsafe_chars():
    sid = session_id_for("wechat", "../../etc/passwd")
    assert "/" not in sid and "\\" not in sid and ".." not in sid
