"""工具参数解析的单测。

## 为什么挑这两块写测试

因为它们属于「**改坏了不会报错**」的地方：

    参数解析坏了 → 工具收到半截参数 → 返回一条错误字符串 → 模型自己圆过去
                   你看到的只是「它答得不太对」，不会看到任何异常
    裁剪逻辑坏了 → 模型收到乱序 / 错段的历史 → 答得很怪，但程序完整跑完

这类 bug 靠手测几乎发现不了，只能靠断言固定住。

## 跑法

在 `mini-agent` 目录下：

    uv run pytest                      # 全部
    uv run pytest tests/test_tool_args.py -v   # 只跑这个文件，显示每条用例名
    uv run pytest -k "bad_json"        # 只跑名字含 bad_json 的

⚠️ 别在 PyCharm 里点「Run」就算完 —— 命令行跑一次，才知道 `pythonpath` 配对了没有。
"""

from __future__ import annotations

import pytest

from agent.llm import ToolCall
from agent.tools.base import EMPTY_PARAMS, Tool
from agent.tools.registry import ToolRegistry


# ══════════════════════════════════════════════════════════
#  第一部分：ToolCall.parsed_arguments()
#  这是全项目最容易出静默 bug 的一处 —— arguments 是**字符串**不是 dict
# ══════════════════════════════════════════════════════════


def test_parsed_arguments_normal():
    """正常 JSON → dict。"""
    call = ToolCall(id="call_1", name="read_file", arguments='{"path": "a.txt"}')
    assert call.parsed_arguments() == {"path": "a.txt"}


def test_parsed_arguments_empty_is_empty_dict():
    """零参数工具下发的是空串（抓帧实测），不是 "{}"。"""
    call = ToolCall(id="call_2", name="get_time", arguments="")
    assert call.parsed_arguments() == {}


def test_parsed_arguments_bad_json_does_not_raise():
    """半截 JSON 是**必然会发生**的，必须容错成 {}，不能抛。

    注意这里断言的是「不抛异常 + 返回 {}」，不是「抛错」——
    抛错会让整轮对话断掉，而模型看到工具报错通常能自己纠正。
    """
    call = ToolCall(id="call_3", name="read_file", arguments='{"path": "a')
    assert call.parsed_arguments() == {}


def test_parsed_arguments_json_but_not_object():
    """JSON 合法但不是对象（数组/数字）→ 也要变 {}。

    因为工具是按关键字参数调用的，list 喂不进 run(**kwargs)。
    """
    assert ToolCall(id="c", name="x", arguments='["a", "b"]').parsed_arguments() == {}
    assert ToolCall(id="c", name="x", arguments="42").parsed_arguments() == {}


# ══════════════════════════════════════════════════════════
#  第二部分：Tool.schema() 的形状
#  形状错了服务商直接 400，而不是「勉强能跑」
# ══════════════════════════════════════════════════════════


class _Echo(Tool):
    """测试专用工具：把收到的参数原样吐回来。"""

    name = "echo"
    description = "回显参数"

    def run(self, **kwargs: object) -> str:
        return str(kwargs)


def test_schema_shape():
    schema = _Echo().schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "echo"
    assert schema["function"]["parameters"] == EMPTY_PARAMS


def test_duplicate_name_rejected():
    """重名必须在注册时就炸 —— 否则模型点名会调到你没预期的那一个。"""
    registry = ToolRegistry()
    registry.register(_Echo())
    with pytest.raises(ValueError):
        registry.register(_Echo())


# ══════════════════════════════════════════════════════════
#  TODO —— 错误路径（这两条是 registry.call() 的核心承诺）
#
#  现在跑 pytest 会看到这 2 条是红的。那是**预期内的红**：
#  它们存在的意思就是「这两个行为还没被验证过」。
# ══════════════════════════════════════════════════════════


def test_unknown_tool_returns_error_string():
    """TODO ①：模型点了个不存在的工具名。

    要求：`call()` 返回以 "[错误]" 开头的字符串，且**带上那个错误的名字**，
         并且这行代码本身不抛异常。

    提示：
        registry = ToolRegistry()
        result = registry.call("no_such_tool", {})
        assert result.startswith("[错误]")
        assert "no_such_tool" in result      ← 只断前缀不够，名字丢了模型无从纠正

    ⚠️ 别写成 `with pytest.raises(...)` —— 这里要证明的恰恰是「不抛」。
    """
    registry = ToolRegistry()
    result = registry.call("no_such_tool", {})

    # 两个断言缺一不可：
    #   前缀      → 证明它走的是「转字符串」这条路，而不是默默返回空串
    #   名字在里面 → 证明模型拿到了足够信息去纠正（否则它不知道该改什么）
    assert result.startswith("[错误]")
    assert "no_such_tool" in result


class _Boom(Tool):
    """专门用来炸的工具。"""

    name = "boom"
    description = "总是失败"

    def run(self, **kwargs: object) -> str:
        raise ValueError("boom")


def test_tool_exception_becomes_error_string():
    """TODO ②：工具内部抛异常时，call() 必须把它转成字符串交给模型。

    要求：返回值以 "[错误]" 开头，且包含异常类型名（ValueError）。

    提示：
        1) 先定义一个 run() 里直接 `raise ValueError("boom")` 的工具类
        2) 注册后 call 一次
        3) 断言前缀 + 类型名，**同时确认没有异常冒出来**

    想清楚再写：
        「异常被吞掉」这件事发生在 **registry.call()** 里，不是在线程池、
        也不是在 loop 里。所以这条测试其实在给 registry 的一个契约上锁：
        **局部失败不该拖垮整轮对话。**
    """
    registry = ToolRegistry()
    registry.register(_Boom())

    # 这一行如果抛异常，测试直接红 —— 那正是要防的事
    result = registry.call("boom", {})

    assert result.startswith("[错误]")
    # 异常类型名必须留下：模型看到 "ValueError" 才知道是「我参数给错了」
    # 还是「工具本身坏了」，两者的纠正策略完全不同
    assert "ValueError" in result
