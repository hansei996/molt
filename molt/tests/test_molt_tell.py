"""`tools/molt-tell.py` 的测试。

这个脚本是"以主人身份对一只壳说一句话"的通道，真机上踩过两个坑，
两个都表现为**人看见的和它说的不是一回事**：

1. 整段回答被打了两遍（流式打一次，`complete` 的汇总又打一次）——
   一个用于验证"它现在说了什么"的工具，自己先复读一遍，很难说清是它结巴
   还是工具坏了。
2. 工具调用的 JSON 被当成正文打了出来
   （`{"name": "memory_companion_recall", "args": …}`）——工具在内部干活
   的事，不该冒充它说的话。

所以这里测的不是"能不能连上"，而是**哪些事件算它说的话**。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "tools"
MOD_PATH = TOOLS / "molt-tell.py"


def _load():
    """按文件路径加载（`tools/` 不是包）。

    必须在 `exec_module` **之前**把模块塞进 `sys.modules`：文件头有
    `from __future__ import annotations`，dataclass 解析字符串注解时会
    `sys.modules.get(cls.__module__).__dict__`，拿不到就 `AttributeError`。
    """
    name = "molt_tell_tool"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, MOD_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tell = _load()


def _sse(**event) -> bytes:
    return ("data: " + json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")


def _collect(lines, **kwargs):
    return list(tell.iter_reply(lines, show_reasoning=False, verbose=False, **kwargs))


# ------------------------------------------------------------------ 正文


def test_plain_pieces_are_joined_in_order():
    lines = [
        _sse(type="run_started"),
        _sse(type="plain", data="在"),
        _sse(type="plain", data="的"),
        _sse(type="plain", data="。"),
        _sse(type="end"),
    ]
    assert "".join(_collect(lines)) == "在的。"


def test_plain_with_dict_payload_is_read():
    """有些事件把文本包在 `{"text": …}` 里，而不是裸字符串。"""
    lines = [_sse(type="plain", data={"text": "收到"})]
    assert _collect(lines) == ["收到"]


# -------------------------------------------------- complete：汇总，不是新话


def test_complete_is_skipped_when_text_already_streamed():
    """这就是"整句打两遍"的那个 bug。

    `complete` 带的是整段回答的汇总，流式已经一块块打过了，再打一遍
    就是复读机。
    """
    lines = [
        _sse(type="plain", data="在的。"),
        _sse(type="complete", data="在的。"),
    ]
    assert _collect(lines) == ["在的。"]


def test_complete_is_used_when_nothing_streamed():
    """兜底：万一某个壳只发汇总、不发增量，那它就是唯一的一份正文。

    删掉这条兜底会让"不流式的壳"变成一片空白，而空白看起来像"它什么都没说"。
    """
    lines = [_sse(type="complete", data="在的。")]
    assert _collect(lines) == ["在的。"]


# ------------------------------------------------------- 不是它说的话


def test_tool_call_events_are_not_speech():
    lines = [
        _sse(type="tool_call", data='{"name": "memory_companion_recall", "args": {}}'),
        _sse(
            type="tool_call_result",
            data={"result": '{"ok": true, "memories": []}'},
        ),
        _sse(type="plain", data="我这边没有存下记录。"),
    ]
    assert _collect(lines) == ["我这边没有存下记录。"]


def test_bookkeeping_events_are_dropped():
    lines = [
        _sse(type="session_id", data="abc"),
        _sse(type="user_message_saved", data="你好"),
        _sse(type="agent_stats", data="{}"),
        _sse(type="message_saved", data="在的"),
        _sse(type="end", data=""),
    ]
    assert _collect(lines) == []


# ------------------------------------------------------------------ 思考过程


def test_reasoning_is_dropped_by_default():
    lines = [
        _sse(type="plain", chain_type="reasoning", data="我先想一下……"),
        _sse(type="plain", data="在的。"),
    ]
    assert _collect(lines) == ["在的。"]


def test_reasoning_can_be_shown_on_request():
    lines = [
        _sse(type="plain", chain_type="reasoning", data="我先想一下……"),
        _sse(type="plain", data="在的。"),
    ]
    out = list(tell.iter_reply(lines, show_reasoning=True, verbose=False))
    assert out == ["我先想一下……", "在的。"]


# ------------------------------------------------------------------ 坏输入


def test_garbage_lines_are_ignored():
    lines = [
        b"\n",
        b"event: message\n",
        b"data: [DONE]\n",
        b"data: not-json\n",
        _sse(type="plain", data="在的。"),
    ]
    assert _collect(lines) == ["在的。"]


def test_bad_utf8_does_not_explode():
    lines = [b'data: {"type": "plain", "data": "\xff\xfe"}\n']
    assert "".join(_collect(lines)) == "\ufffd\ufffd"
