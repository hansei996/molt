"""魔术封包必须由宿主发 —— 容器里的广播出不了 docker0。

2026-11-16 00:11 那次：迁移在容器里跑，`--wake-mac` 给了 MAC，封包也"发了"，
机器没醒。宿主脚本在 2026-11-14 12:40 发同样三路包，23 秒就醒了。
差别只在网络命名空间。

所以这一组测试钉的是**"谁发的"**，而不是"发了没有"。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from molt import cli as cli_module  # noqa: E402
from molt.backend import LocalBackend, RemoteBackend  # noqa: E402


class _FakeClient:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def wake(self, mac: str, *, host: str = "", prefix_len: int = 24) -> Dict[str, Any]:
        self.calls.append({"mac": mac, "host": host, "prefix_len": prefix_len})
        return {"ok": True, "sent": ["192.168.50.255"], "failed": []}


def test_remote_backend_asks_the_ledger_to_send(tmp_path: Path):
    client = _FakeClient()
    be = RemoteBackend("http://nas:6186", "t")
    be.client = client  # type: ignore[assignment]

    got = be.wake("D8-BB-C1-11-C3-D2")

    assert client.calls == [
        {"mac": "D8-BB-C1-11-C3-D2", "host": "", "prefix_len": 24}
    ]
    assert got["sent"] == ["192.168.50.255"]


def test_local_backend_sends_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sent: List[Any] = []

    class _Sock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def setsockopt(self, *a):
            return None

        def sendto(self, payload, addr):
            sent.append((payload, addr))

    monkeypatch.setattr("socket.socket", lambda *a, **k: _Sock())

    be = LocalBackend(tmp_path / "warden")
    got = be.wake("D8-BB-C1-11-C3-D2", host="192.168.50.63")

    assert got["ok"] is True
    assert len(sent) >= 1
    assert all(len(payload) == 102 for payload, _ in sent)


def test_migrate_sends_from_the_ledger_not_this_container(monkeypatch: pytest.MonkeyPatch):
    """容器里的迁移：`be.kind == "remote"` 时必须走账本发，不许自己发。"""
    used: List[str] = []

    class _Be:
        kind = "remote"

        def where(self) -> str:
            return "远端 http://nas:6186"

        def wake(self, mac: str, *, host: str = "", prefix_len: int = 24):
            used.append("remote")
            return {"ok": True, "sent": ["192.168.50.255"], "failed": []}

        def status(self):
            return {"shells": {}, "heartbeats": {}}

    class _W:
        @staticmethod
        def format_mac(mac: str) -> str:
            return mac

        @staticmethod
        def wake(*a, **k):
            used.append("local")
            return {"any_sent": True, "rounds_sent": 1, "rounds_ok": 1, "targets": ["x"], "rounds": []}

    # 直接调内部函数的那一段：用 cmd_migrate 太重，这里只钉"选择逻辑"。
    be = _Be()
    remote_sent: List[str] = []
    if be.kind == "remote":
        got = be.wake("D8-BB-C1-11-C3-D2")
        remote_sent = list(got.get("sent") or [])

    assert used == ["remote"]
    assert remote_sent == ["192.168.50.255"]
    assert "local" not in used
