"""本机代转小口（`molt/dshpeer.py`）：那一跳必须发生在有 DSH 的机器上。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from molt import dshpeer, dshrelay


@pytest.fixture()
def peer(monkeypatch):
    calls = []

    def fake_call(base, method, args, **kwargs):
        calls.append((base, method, args))
        return {"echo": method}

    monkeypatch.setattr(dshrelay, "call", fake_call)
    server = dshpeer.serve(token="t0ken", port=0)
    yield server, calls
    server.stop()


def _post(port: int, body: dict, token: str = "t0ken"):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/dsh",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Molt-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_a_request_without_the_token_is_refused(peer):
    server, _ = peer
    status, payload = _post(server.port, {"method": "session/list", "args": {}}, token="wrong")
    assert status == 401
    assert payload["ok"] is False


def test_a_method_outside_the_whitelist_is_refused(peer):
    server, calls = peer
    status, payload = _post(server.port, {"method": "session/create", "args": {}})
    assert status == 400
    assert "白名单" in payload["message"]
    assert calls == [], "白名单必须在转发之前生效"


def test_it_forwards_to_loopback_and_returns_the_value(peer):
    server, calls = peer
    status, payload = _post(server.port, {"method": "session/list", "args": {"_request": {}}})
    assert status == 200
    assert payload["dsh"] == {"echo": "session/list"}
    base, method, args = calls[0]
    assert base == "http://127.0.0.1:3082", "只能转给本机，调用方不能指定目标"
    assert method == "session/list"


def test_a_refusal_from_dsh_becomes_a_502(peer, monkeypatch):
    server, _ = peer

    def boom(base, method, args, **kwargs):
        raise dshrelay.DshRelayError("对端说不行")

    monkeypatch.setattr(dshrelay, "call", boom)
    status, payload = _post(server.port, {"method": "session/list", "args": {}})
    assert status == 502
    assert "不行" in payload["message"]


def test_a_tokenless_peer_refuses_to_start():
    with pytest.raises(ValueError):
        dshpeer.DshPeer(token="", port=0)


def test_health_says_which_methods_it_will_forward(peer):
    server, _ = peer
    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/health", timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
    assert payload["service"] == "molt-dshpeer"
    assert set(payload["methods"]) == set(dshrelay.ALLOWED_METHODS)
