"""`/shell` 与 `/shells` 两个端点，以及守护进程的"启动时声明一次"。

为什么这两个端点要单独测：**它们是跨机器的那一半**。
`Warden.set_shell()` 在本进程里测通了，只能证明"同一台机器上能用"；
真正让账本知道"那台 Windows 是什么"，靠的是这台壳隔着网络发出去的那一个 POST。

注意大部分用例**直接调 `dispatch()` 而不起套接字** —— 和 `test_server.py`
一样的选择：测一个端点的业务语义不该依赖 TCP 的时序。只有"要 token"这一条
值得真起服务。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from molt.server import WardenServer
from molt.warden import Warden


@pytest.fixture()
def srv(tmp_path: Path) -> WardenServer:
    return WardenServer(root=tmp_path / "warden", token="t0ken", port=0)


def test_the_ledger_learns_what_a_shell_is(srv: WardenServer) -> None:
    r = srv.dispatch(
        "POST",
        "/shell",
        {"shell": "nas-1", "role": "nas", "wakeable": True, "by": "homer"},
    )
    assert r.status == 200
    assert r.payload["shell"]["role"] == "nas"
    assert r.payload["shell"]["wakeable"] is True
    # 端点写进去的东西，本地 Warden 必须立刻看得到（同一个文件）。
    assert srv.warden.shells_view()["nas-1"]["role"] == "nas"


def test_shells_lists_everything(srv: WardenServer) -> None:
    srv.dispatch("POST", "/shell", {"shell": "box", "role": "desktop"})
    r = srv.dispatch("GET", "/shells", None)
    assert r.status == 200
    assert r.payload["shells"]["box"]["role"] == "desktop"


def test_a_missing_shell_is_a_400(srv: WardenServer) -> None:
    r = srv.dispatch("POST", "/shell", {"role": "nas"})
    assert r.status == 400
    assert r.payload["kind"] == "bad_request"


def test_an_unknown_field_is_a_400_not_a_silent_write(srv: WardenServer) -> None:
    """白名单：账本里不该出现一个谁都能塞键的字典。

    静默接受拼错的键，症状是"我明明填了 gpu"，而账本里那个键永远不存在。
    """
    r = srv.dispatch("POST", "/shell", {"shell": "box", "colour": "red"})
    assert r.status == 400
    assert "colour" in r.payload["message"]


def test_a_bad_role_is_a_422(srv: WardenServer) -> None:
    r = srv.dispatch("POST", "/shell", {"shell": "box", "role": "laptop"})
    assert r.status == 422
    assert r.payload["kind"] == "needs_human"


def test_get_shell_is_404_not_a_generic_handler(srv: WardenServer) -> None:
    """`/shell` 只接受 POST；`/shells` 只接受 GET。

    容易写错的一处：`head == "shell"` 会把 `/shells` 也带进来，
    所以两个分支都要判 `parts[1:2]`，否则 `/shells` 会掉进 POST 分支报 400。
    """
    assert srv.dispatch("GET", "/shell", None).status == 404
    assert srv.dispatch("POST", "/shells", {}).status == 404


def test_the_endpoint_needs_the_token(tmp_path: Path) -> None:
    import urllib.error
    import urllib.request

    server = WardenServer(root=tmp_path / "warden", token="t0ken", port=0)
    _, port = server.start()
    try:
        body = json.dumps({"shell": "box", "role": "nas"}).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/shell",
            data=body,
            headers={"Content-Type": "application/json", "X-Molt-Token": "WRONG"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as info:
            urllib.request.urlopen(req, timeout=10)
        assert info.value.code in (401, 403)
    finally:
        server.stop()


# ---------------------------------------------------------------- 守护进程侧
#
# 两个类各自有一份 `_declare_shell()`。它们必须只发一次、失败不致命、
# 而且**不许因为声明失败就不干活**。

def _load_daemon(name: str):
    import importlib

    return importlib.import_module(f"molt.daemon.{name}")


class _RecordingSender:
    def __init__(self, ok: bool = True, explode: bool = False) -> None:
        self.ok = ok
        self.explode = explode
        self.calls: list = []

    def __call__(self, shell_id: str, attrs: Dict[str, Any], **kwargs: Any) -> bool:
        self.calls.append((shell_id, dict(attrs), kwargs.get("by")))
        if self.explode:
            raise RuntimeError("网络断了")
        return self.ok


def test_the_agent_declares_itself_once(tmp_path: Path) -> None:
    agent = _load_daemon("agent")
    settings = agent.Settings(
        shell="win-shell", shell_attrs={"role": "desktop", "wakeable": True}
    )
    sender = _RecordingSender()
    a = agent.Agent(
        settings=settings,
        state_path=tmp_path / "agent.json",
        shell_attr_sender=sender,
        log=lambda _line: None,
    )
    a._declare_shell()
    a._declare_shell()
    assert len(sender.calls) == 1
    assert sender.calls[0][0] == "win-shell"
    assert sender.calls[0][1] == {"role": "desktop", "wakeable": True}
    assert sender.calls[0][2] == "agent"


def test_the_agent_skips_declaring_when_nothing_is_configured(tmp_path: Path) -> None:
    agent = _load_daemon("agent")
    sender = _RecordingSender()
    a = agent.Agent(
        settings=agent.Settings(shell="win-shell"),
        state_path=tmp_path / "agent.json",
        shell_attr_sender=sender,
        log=lambda _line: None,
    )
    a._declare_shell()
    assert sender.calls == []


def test_a_failed_declaration_does_not_kill_the_daemon(tmp_path: Path) -> None:
    """声明失败只是一行日志：一台能搬家的机器不该因为 role 没填好就罢工。"""
    agent = _load_daemon("agent")
    lines: list = []
    a = agent.Agent(
        settings=agent.Settings(shell="win-shell", shell_attrs={"role": "desktop"}),
        state_path=tmp_path / "agent.json",
        shell_attr_sender=_RecordingSender(explode=True),
        log=lines.append,
    )
    a._declare_shell()  # 不抛
    assert any("壳属性没送进账本" in line for line in lines)


def test_the_homer_declares_itself_once(tmp_path: Path) -> None:
    homer = _load_daemon("homer")
    settings = homer.Settings(shell="nas-shell", shell_attrs={"role": "nas"})
    sender = _RecordingSender()
    h = homer.Homer(
        settings=settings,
        state_path=tmp_path / "homer.json",
        shell_attr_sender=sender,
        log=lambda _line: None,
    )
    h._declare_shell()
    h._declare_shell()
    assert len(sender.calls) == 1
    assert sender.calls[0][2] == "homer"


def test_the_two_daemons_speak_the_same_wire_format() -> None:
    """agent 与 homer 各有一份 `Warden.set_shell()`。它们必须发同样的 body。

    两份实现是刻意的（守护进程不能依赖 CLI 的依赖图），所以需要一条断言
    把它们钉在一起 —— 否则某天一边改了字段名，另一边会安静地发出去没人看。
    """
    agent = _load_daemon("agent")
    homer = _load_daemon("homer")
    sent: list = []

    class _Capture:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        sent.append(json.loads(req.data.decode("utf-8")))
        return _Capture()

    for module in (agent, homer):
        original = module.urllib.request.urlopen
        module.urllib.request.urlopen = fake_urlopen
        try:
            module.Warden("http://127.0.0.1:1", "tok").set_shell(
                "shell-x", {"role": "nas"}, by="test"
            )
        finally:
            module.urllib.request.urlopen = original

    assert sent[0] == sent[1]
    assert sent[0] == {"shell": "shell-x", "role": "nas", "by": "test"}
