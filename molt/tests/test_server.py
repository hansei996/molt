"""`WardenServer` 与 `WardenClient` 的测试。

这里有一个刻意的选择：**大部分测试直接调 `dispatch()`，不起套接字**。
理由不是图快，而是——测一个端点的业务语义不该依赖 TCP 的时序；
只有认证、脚本体、协议版本这些"HTTP 层才存在"的东西才值得真起服务。
两类测试都有，各管各的。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from molt import server as server_mod
from molt.client import WardenClient
from molt.errors import (
    EpochAhead,
    EpochStale,
    Fatal,
    LeaseExpired,
    LeaseHeld,
    MoltError,
    NeedsHuman,
    ProtocolMismatch,
)
from molt.server import TOKEN_HEADER, WardenServer
from molt.version import PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def srv(tmp_path: Path) -> WardenServer:
    return WardenServer(root=tmp_path, token="test-token", port=0)


def _manifest(generation: int = 1) -> dict:
    """一份最小但结构合法的 manifest（服务端不验 sha256，只记元数据）。"""
    return {
        "generation": generation,
        "created_at": 1700000000.0,
        "source_host": "UGREEN-776B",
        "source_root": "/mnt/dm-0/astrbot",
        "protocol_version": PROTOCOL_VERSION,
        "astrbot_version": "config-5",
        "platform": "linux",
        "python_version": "3.10.7",
        "artifacts": [],
        "runtime_pip": [],
    }


# ---------------------------------------------------------------------------
# dispatch：业务语义（不起套接字）
# ---------------------------------------------------------------------------


def test_root_reports_service_identity(srv: WardenServer) -> None:
    r = srv.dispatch("GET", "/", None)
    assert r.status == 200
    assert r.payload["ok"] is True
    assert r.payload["service"] == "molt-warden"
    assert r.payload["protocol_version"] == PROTOCOL_VERSION


def test_acquire_then_status_shows_lease(srv: WardenServer) -> None:
    r = srv.dispatch("POST", "/acquire", {"shell_id": "win-abc"})
    assert r.status == 200
    lease = r.payload["lease"]
    assert lease["shell_id"] == "win-abc"
    assert lease["epoch"] == 1

    s = srv.dispatch("GET", "/status", None).payload["status"]
    assert s["lease"]["state"] == "active"
    assert s["lease"]["holder"] == "win-abc"
    assert s["current_epoch"] == 1


def test_second_shell_is_rejected_with_lease_held(srv: WardenServer) -> None:
    srv.dispatch("POST", "/acquire", {"shell_id": "win-a"})
    r = srv.dispatch("POST", "/acquire", {"shell_id": "win-b"})
    assert r.status == 409
    assert r.payload["kind"] == "lease_held"
    assert "公理 0" in r.payload["message"] or "只能有一个" in r.payload["message"]


def test_reacquire_by_same_shell_keeps_epoch(srv: WardenServer) -> None:
    first = srv.dispatch("POST", "/acquire", {"shell_id": "win-a"}).payload["lease"]
    second = srv.dispatch("POST", "/acquire", {"shell_id": "win-a"}).payload["lease"]
    assert first["epoch"] == second["epoch"], "续租不能跳号，否则壳自己的写入会被拒"


def test_push_requires_matching_epoch(srv: WardenServer) -> None:
    lease = srv.dispatch("POST", "/acquire", {"shell_id": "win-a"}).payload["lease"]
    good = srv.dispatch(
        "POST",
        "/push",
        {"shell_id": "win-a", "epoch": lease["epoch"], "manifest": _manifest(1)},
    )
    assert good.status == 200
    assert good.payload["generation"]["generation"] == 1

    stale = srv.dispatch(
        "POST",
        "/push",
        {"shell_id": "win-a", "epoch": lease["epoch"] - 1, "manifest": _manifest(2)},
    )
    assert stale.status == 409
    assert stale.payload["kind"] == "epoch_stale"


def test_push_with_ahead_epoch_is_rejected(srv: WardenServer) -> None:
    lease = srv.dispatch("POST", "/acquire", {"shell_id": "win-a"}).payload["lease"]
    r = srv.dispatch(
        "POST",
        "/push",
        {
            "shell_id": "win-a",
            "epoch": lease["epoch"] + 5,
            "manifest": _manifest(1),
        },
    )
    assert r.status == 409
    assert r.payload["kind"] == "epoch_ahead"


def test_push_replay_of_old_generation_is_rejected(srv: WardenServer) -> None:
    lease = srv.dispatch("POST", "/acquire", {"shell_id": "win-a"}).payload["lease"]
    srv.dispatch(
        "POST",
        "/push",
        {"shell_id": "win-a", "epoch": lease["epoch"], "manifest": _manifest(3)},
    )
    r = srv.dispatch(
        "POST",
        "/push",
        {"shell_id": "win-a", "epoch": lease["epoch"], "manifest": _manifest(2)},
    )
    assert r.status == 422
    assert r.payload["kind"] == "needs_human"


def test_push_with_wrong_protocol_is_rejected(srv: WardenServer) -> None:
    lease = srv.dispatch("POST", "/acquire", {"shell_id": "win-a"}).payload["lease"]
    m = _manifest(1)
    m["protocol_version"] = PROTOCOL_VERSION + 1
    r = srv.dispatch(
        "POST", "/push", {"shell_id": "win-a", "epoch": lease["epoch"], "manifest": m}
    )
    assert r.status == 426
    assert r.payload["kind"] == "protocol_mismatch"


def test_release_then_new_epoch_is_granted(srv: WardenServer) -> None:
    lease = srv.dispatch("POST", "/acquire", {"shell_id": "win-a"}).payload["lease"]
    srv.dispatch(
        "POST", "/release", {"shell_id": "win-a", "epoch": lease["epoch"]}
    )
    nxt = srv.dispatch("POST", "/acquire", {"shell_id": "win-b"}).payload["lease"]
    assert nxt["epoch"] == lease["epoch"] + 1


def test_generations_list_and_next_number(srv: WardenServer) -> None:
    lease = srv.dispatch("POST", "/acquire", {"shell_id": "win-a"}).payload["lease"]
    srv.dispatch(
        "POST",
        "/push",
        {"shell_id": "win-a", "epoch": lease["epoch"], "manifest": _manifest(1)},
    )
    gens = srv.dispatch("GET", "/generations", None).payload["generations"]
    assert len(gens) == 1
    assert srv.dispatch("GET", "/next_generation", None).payload["generation"] == 2


def test_unknown_path_is_404(srv: WardenServer) -> None:
    r = srv.dispatch("GET", "/nope", None)
    assert r.status == 404
    assert r.payload["kind"] == "not_found"


def test_missing_fields_are_400(srv: WardenServer) -> None:
    assert srv.dispatch("POST", "/acquire", {}).status == 400
    assert srv.dispatch("POST", "/release", {"shell_id": "x"}).status == 400
    assert srv.dispatch("POST", "/push", {"shell_id": "x"}).status == 400


def test_wrong_method_is_404_not_500(srv: WardenServer) -> None:
    # 用 GET 去打一个只接受 POST 的端点，应该是干净的 404，
    # 而不是掉进某个分支里崩掉。
    assert srv.dispatch("GET", "/acquire", None).status == 404


# ---------------------------------------------------------------------------
# 错误分类表：两边的 kind 必须对得上
# ---------------------------------------------------------------------------


def test_error_map_covers_every_client_exception() -> None:
    """服务端发出哪些 kind，客户端就必须认识哪些。

    如果不认识，客户端会把它降级成裸 MoltError，于是"租约被抢"和
    "服务端炸了"在调用方看来是一样的——这两个的处置完全不同。
    """
    from molt.client import _KIND_TO_EXC

    server_kinds = {kind for _, _, kind in server_mod._ERROR_MAP}
    client_kinds = set(_KIND_TO_EXC)
    assert server_kinds == client_kinds, (
        f"服务端多出 {server_kinds - client_kinds}，客户端多出 {client_kinds - server_kinds}"
    )


def test_every_molt_error_subclass_is_classified() -> None:
    """每个 MoltError 子类都要在 `_ERROR_MAP` 里有位置。

    漏一个的后果：那个错误会以 500 冒出来，而 500 在客户端意味着
    "服务端出问题了"，可实际上可能是"你的租约过期了"这种正常流程。
    """
    import molt.errors as errs

    mapped = {name for name, _, _ in server_mod._ERROR_MAP}
    defined = {
        name
        for name, obj in vars(errs).items()
        if isinstance(obj, type)
        and issubclass(obj, errs.MoltError)
        and obj is not errs.MoltError
    }
    missing = defined - mapped
    assert not missing, f"这些错误没有 HTTP 分类：{missing}"


# ---------------------------------------------------------------------------
# 真起服务：认证、脚本体、协议头
# ---------------------------------------------------------------------------


@pytest.fixture
def live(srv: WardenServer):
    host, port = srv.start()
    yield srv, f"http://127.0.0.1:{port}"
    srv.stop()


def test_client_can_ping_live_server(live) -> None:
    srv, url = live
    c = WardenClient(url, "test-token")
    info = c.ping()
    assert info["service"] == "molt-warden"


def test_wrong_token_is_401_and_becomes_molt_error(live) -> None:
    srv, url = live
    c = WardenClient(url, "wrong-token")
    with pytest.raises(MoltError) as ei:
        c.ping()
    assert not isinstance(ei.value, Fatal) or "连不上" not in str(ei.value)


def test_missing_token_header_is_401(live) -> None:
    srv, url = live
    req = urllib.request.Request(url + "/", method="GET")
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=5)
    assert ei.value.code == 401


def test_client_acquire_push_roundtrip_over_http(live) -> None:
    srv, url = live
    c = WardenClient(url, "test-token")
    lease = c.acquire("win-http")
    assert lease["epoch"] == 1
    rec = c.push("win-http", lease["epoch"], _manifest(1))
    assert rec["generation"] == 1
    assert c.next_generation() == 2
    assert len(c.generations()) == 1


def test_client_maps_lease_held_to_exception(live) -> None:
    srv, url = live
    a = WardenClient(url, "test-token")
    b = WardenClient(url, "test-token")
    a.acquire("win-a")
    with pytest.raises(LeaseHeld):
        b.acquire("win-b")


def test_client_maps_epoch_stale_to_exception(live) -> None:
    srv, url = live
    c = WardenClient(url, "test-token")
    lease = c.acquire("win-a")
    with pytest.raises(EpochStale):
        c.push("win-a", lease["epoch"] - 1, _manifest(1))


def test_client_maps_protocol_mismatch_from_body(live) -> None:
    """协议版本不一致时，即使 kind 结构本身不同也必须能认出来。"""
    srv, url = live
    c = WardenClient(url, "test-token")
    lease = c.acquire("win-a")
    m = _manifest(1)
    m["protocol_version"] = PROTOCOL_VERSION + 1
    with pytest.raises(ProtocolMismatch):
        c.push("win-a", lease["epoch"], m)


def test_client_rejects_empty_token_and_url(tmp_path: Path) -> None:
    with pytest.raises(Fatal):
        WardenClient("", "tok")
    with pytest.raises(Fatal):
        WardenClient("http://x", "")


def test_client_try_ping_does_not_raise(tmp_path: Path) -> None:
    # 端口 9 是 discard 服务，家用环境里基本没人监听；用 1 号端口太慢，
    # 这里直接指向一个必然连不上的地址段并靠短超时收场。
    c = WardenClient("http://127.0.0.1:1", "tok", timeout=0.3, retries=1)
    assert c.try_ping() is False


def test_client_connection_failure_is_fatal_not_httperror() -> None:
    c = WardenClient("http://127.0.0.1:1", "tok", timeout=0.3, retries=1)
    with pytest.raises(Fatal) as ei:
        c.ping()
    assert "连不上" in str(ei.value)


# ---------------------------------------------------------------------------
# 并发：HTTP 层的互斥必须与本地锁一致
# ---------------------------------------------------------------------------


def test_concurrent_acquire_over_http_has_exactly_one_winner(live) -> None:
    """两个真线程同时抢，只能有一个拿到租约。

    这条测的是"HTTP 层没有绕过 `Warden` 的锁"——如果 `dispatch` 里
    手抖缓存了 state，或者忘了 reload，这里就会冒出两个赢家。
    """
    srv, url = live
    results: list = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker(shell_id: str) -> None:
        c = WardenClient(url, "test-token")
        barrier.wait()
        try:
            lease = c.acquire(shell_id)
            with lock:
                results.append(("ok", shell_id, lease["epoch"]))
        except MoltError as e:
            with lock:
                results.append(("err", shell_id, type(e).__name__))

    threads = [
        threading.Thread(target=worker, args=(f"win-{i}",)) for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    winners = [r for r in results if r[0] == "ok"]
    assert len(winners) == 1, f"应当恰好一个赢家，实际 {results}"
    assert len(results) == 2


def test_server_token_is_generated_when_not_given(tmp_path: Path) -> None:
    s = WardenServer(root=tmp_path, port=0)
    assert s.generated_token is True
    assert len(s.token) >= 20, "自动生成的 token 必须足够长"
    s2 = WardenServer(root=tmp_path, port=0)
    assert s2.token != s.token, "每次启动都应该是一个新 token"


def test_server_records_state_on_disk(tmp_path: Path) -> None:
    s = WardenServer(root=tmp_path, token="t", port=0)
    s.dispatch("POST", "/acquire", {"shell_id": "win-a"})
    raw = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert "win-a" in raw
    parsed = json.loads(raw)
    assert parsed["protocol_version"] == PROTOCOL_VERSION

# ---------------------------------------------------------------------------
# 只读页面：/where 与 /timeline 认 ?token=，/history 不认
#
# 这三条是同一条安全边界的三个面，必须一起测：
# 能改租约的 token 出现在 URL 里，会被记进浏览历史、被转发、被截图。
# 所以「哪个端点允许它出现在 URL 里」是个白名单，而不是默认行为。
# ---------------------------------------------------------------------------


def _get(url: str, token: str = "") -> tuple:
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header(TOKEN_HEADER, token)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_where_accepts_the_token_in_the_query(live) -> None:
    srv, url = live
    status, body = _get(url + "/where?token=test-token")
    assert status == 200
    assert b"<!doctype html>" in body


def test_timeline_accepts_the_token_in_the_query(live) -> None:
    srv, url = live
    status, body = _get(url + "/timeline?token=test-token")
    assert status == 200
    assert b"<!doctype html>" in body
    assert "它搬过几次家".encode("utf-8") in body


def test_timeline_rejects_a_wrong_token_in_the_query(live) -> None:
    srv, url = live
    status, _ = _get(url + "/timeline?token=wrong")
    assert status == 401


def test_history_does_not_accept_the_token_in_the_query(live) -> None:
    """`/history` 是给脚本读的 JSON —— 脚本加一个头没有代价，所以不开这个口子。"""
    srv, url = live
    status, _ = _get(url + "/history?token=test-token")
    assert status == 401


def test_history_works_with_the_header(live) -> None:
    srv, url = live
    status, body = _get(url + "/history", token="test-token")
    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload["history"]["generation_count"] == 0
    assert "heartbeats" in payload["history"]


def test_history_lists_a_generation_after_a_push(srv: WardenServer) -> None:
    lease = srv.dispatch("POST", "/acquire", {"shell_id": "win-a"}).payload["lease"]
    srv.dispatch(
        "POST",
        "/push",
        {"shell_id": "win-a", "epoch": lease["epoch"], "manifest": _manifest(1)},
    )
    h = srv.dispatch("GET", "/history", None).payload["history"]
    assert h["generation_count"] == 1
    assert h["generations"][0]["generation"] == 1
    assert h["current_epoch"] == 1


def test_timeline_page_renders_the_same_history_as_the_json(srv: WardenServer) -> None:
    lease = srv.dispatch("POST", "/acquire", {"shell_id": "win-a"}).payload["lease"]
    srv.dispatch(
        "POST",
        "/push",
        {"shell_id": "win-a", "epoch": lease["epoch"], "manifest": _manifest(1)},
    )
    page = srv.dispatch("GET", "/timeline", None).html
    assert page and "第 1 次搬家" in page
    assert "<!doctype html>" in page

def test_arrival_receipts_are_recorded_and_visible(tmp_path):
    """新壳回执「落地播报送到了」—— 旧壳关自己之前唯一认的凭据。"""
    import json as _json

    from molt import server as srv
    from molt.warden import Warden

    w = Warden(root=tmp_path)
    server = srv.WardenServer(root=tmp_path)

    resp = server.dispatch(
        "POST",
        "/arrival",
        {"generation": 8, "shell": "nas-box", "delivered": True, "detail": "sent"},
    )
    assert resp.status == 200
    assert resp.payload["arrival"]["generation"] == 8

    st = w.status()
    assert st["arrivals"]["8"]["delivered"] is True
    assert st["arrivals"]["8"]["shell"] == "nas-box"


def test_an_arrival_receipt_without_a_shell_is_refused(tmp_path):
    import json as _json

    from molt import server as srv
    from molt.warden import Warden

    server = srv.WardenServer(root=tmp_path)
    resp = server.dispatch("POST", "/arrival", {"generation": 8, "delivered": True})
    assert resp.status in (400, 422)
