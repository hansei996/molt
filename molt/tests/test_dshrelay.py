"""代转层（`molt/dshrelay.py`）与账本那条 `/dsh` 路由。

这一层的失败方式很单调，所以测试也照着单调地写：

- **白名单在发请求之前就生效**（不是发出去再拒）
- **Host 一定被改写**（不改写，对端按回环校验会拒 —— 这正是"NAS 连不上"的真因）
- **对端说不行，就如实说不行**，不重试、不吞
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from molt import dshrelay


class _Response:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Opener:
    """记下每次请求，按队列回执。"""

    def __init__(self, *payloads: Dict[str, Any]) -> None:
        self.payloads = list(payloads)
        self.requests: List[Any] = []

    def __call__(self, request: Any, timeout: float = 0) -> _Response:
        self.requests.append(request)
        if not self.payloads:
            return _Response({"result": {"ok": True, "value": {}}})
        return _Response(self.payloads.pop(0))


def _ok(value: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "server-response", "rpcId": "x", "result": {"ok": True, "value": value}}


# --------------------------------------------------------------- resolve_base


def test_base_comes_from_the_ledger_address():
    status = {"shells": {"win": {"address": "192.168.50.63"}}}
    assert dshrelay.resolve_base(status, "win") == "http://192.168.50.63:3082"


def test_no_address_is_an_error_that_says_what_to_do():
    with pytest.raises(dshrelay.DshRelayError) as err:
        dshrelay.resolve_base({"shells": {"win": {}}}, "win")
    assert "molt declare" in str(err.value)


def test_an_unknown_shell_does_not_fall_back_to_localhost():
    with pytest.raises(dshrelay.DshRelayError):
        dshrelay.resolve_base({"shells": {}}, "ghost")


# --------------------------------------------------------------- 白名单


@pytest.mark.parametrize("method", ["session/delete", "shell/exec", "", "__import__"])
def test_only_three_methods_are_allowed_before_any_io(method):
    opener = _Opener()
    with pytest.raises(dshrelay.DshRelayError):
        dshrelay.call("http://127.0.0.1:3082", method, {}, opener=opener)
    assert opener.requests == [], "白名单必须在发请求之前生效"


# --------------------------------------------------------------- 请求形态


def test_the_host_header_is_rewritten_to_loopback():
    opener = _Opener(_ok({"items": []}))
    dshrelay.call("http://192.168.50.63:3082", "session/list", {"_request": {}}, opener=opener)
    headers = {k.lower(): v for k, v in opener.requests[0].header_items()}
    assert headers["host"] == "127.0.0.1:3082", "不改写 Host，对端的回环校验会直接拒"


def test_the_body_is_a_client_request_with_args():
    opener = _Opener(_ok({}))
    dshrelay.call("http://127.0.0.1:3082", "session/list", {"_request": {}}, opener=opener)
    body = json.loads(opener.requests[0].data.decode("utf-8"))
    assert body["type"] == "client-request"
    assert body["method"] == "session/list"
    assert body["payload"] == {"args": {"_request": {}}}


def test_a_gateway_refusal_is_reported_with_its_code():
    refusal = {
        "type": "server-response",
        "rpcId": "x",
        "result": {
            "ok": False,
            "error": {"code": "gateway/arguments-invalid", "message": "missing \"request\""},
        },
    }
    with pytest.raises(dshrelay.DshRelayError) as err:
        dshrelay.call("http://127.0.0.1:3082", "session/list", {}, opener=_Opener(refusal))
    assert "gateway/arguments-invalid" in str(err.value)


def test_a_connection_failure_says_which_base():
    def boom(request: Any, timeout: float = 0) -> Any:
        raise OSError("connection refused")

    with pytest.raises(dshrelay.DshRelayError) as err:
        dshrelay.call("http://192.168.50.63:3082", "session/list", {}, opener=boom)
    assert "192.168.50.63" in str(err.value)


# --------------------------------------------------------------- 三个便利封装


def test_send_prompt_builds_one_text_part_and_a_request_id():
    opener = _Opener(_ok({"accepted": True}))
    dshrelay.send_prompt("http://127.0.0.1:3082", "s-1", "在吗", opener=opener)
    body = json.loads(opener.requests[0].data.decode("utf-8"))
    request = body["payload"]["args"]["request"]
    assert request["sessionId"] == "s-1"
    assert request["mode"] == "queue", "默认排队，不打断人家正在说的话"
    assert request["content"] == [{"type": "text", "text": "在吗"}]
    assert len(request["requestId"]) >= 8


def test_send_prompt_refuses_empty_text_without_io():
    opener = _Opener()
    with pytest.raises(dshrelay.DshRelayError):
        dshrelay.send_prompt("http://127.0.0.1:3082", "s-1", "   ", opener=opener)
    assert opener.requests == []


def test_read_events_uses_the_session_address_shape():
    opener = _Opener(_ok({"records": []}))
    dshrelay.read_events("http://127.0.0.1:3082", "s-1", max_messages=5, opener=opener)
    body = json.loads(opener.requests[0].data.decode("utf-8"))
    request = body["payload"]["args"]["request"]
    assert request["address"] == {"kind": "session", "sessionId": "s-1"}
    assert request["maxMessages"] == 5


def test_list_sessions_returns_the_items():
    opener = _Opener(_ok({"items": [{"sessionId": "a"}, {"sessionId": "b"}]}))
    assert [x["sessionId"] for x in dshrelay.list_sessions("http://x:3082", opener=opener)] == [
        "a",
        "b",
    ]


# --------------------------------------------------------------- 摘要


def test_summarize_events_keeps_text_and_drops_tool_calls():
    value = {
        "records": [
            {"event": {"data": {"message": {"role": "user", "content": [{"type": "text", "text": "在吗"}]}}}},
            {"event": {"data": {"message": {"role": "assistant", "content": [
                {"type": "reasoning", "text": ""},
                {"type": "tool-call", "name": "bash"},
                {"type": "text", "text": "在的"},
            ]}}}},
        ]
    }
    assert dshrelay.summarize_events(value) == ["我: 在吗", "它: 在的"]


def test_summarize_events_tolerates_a_broken_page():
    assert dshrelay.summarize_events({}) == []
    assert dshrelay.summarize_events({"records": "nope"}) == []
