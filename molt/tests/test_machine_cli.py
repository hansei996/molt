"""`molt machine` 与关机票 TTL 的回归测试。

这两件事放在一起测，是因为它们回答的是同一个问题：
**「NAS 上的鱼怎么安全地碰一下另一台机器」。**
- 票会过期 → 一次「关了吧」不会在三天后突然生效；
- `machine off` 拒绝关掉鱼自己待着的那台；
- `machine on` 没 MAC 就明说叫不醒。
"""
import argparse
import json
import socket
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from molt import cli
from molt.warden import Warden


def _be(root: Path) -> cli.LocalBackend:  # type: ignore[attr-defined]
    return cli._backend(argparse.Namespace(root=str(root), warden_url=None, warden_token=None))


def _args(root: Path, action: str = "list", shell: str = "") -> argparse.Namespace:
    return argparse.Namespace(
        root=str(root),
        warden_url=None,
        warden_token=None,
        machine_action=action,
        shell=shell,
        json=False,
    )


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    w = Warden(root=tmp_path / "warden")
    w.set_shell("nas-box", role="nas", address="127.0.0.1", wake_mac="AA-BB-CC-DD-EE-01")
    w.set_shell("win-box", role="desktop", address="127.0.0.1", wakeable=True, wake_mac="AA-BB-CC-DD-EE-02")
    return tmp_path / "warden"


# ------------------------------------------------------------------ 关机票 TTL


def test_a_power_ticket_carries_an_expiry(root: Path) -> None:
    be = _be(root)
    ticket = be.request_power("win-box", "shutdown", by="test")
    assert ticket["expires_at"] > time.time()
    assert ticket["expires_at_readable"]


def test_an_expired_ticket_is_gone_from_the_ledger(root: Path) -> None:
    be = _be(root)
    be.request_power("win-box", "shutdown", ttl_seconds=0.01)
    time.sleep(0.05)
    assert "win-box" not in be.status()["power"]


def test_a_zero_ttl_is_refused(root: Path) -> None:
    be = _be(root)
    from molt.errors import NeedsHuman

    with pytest.raises(NeedsHuman):
        be.request_power("win-box", "shutdown", ttl_seconds=0)


def test_verdict_ignores_an_expired_ticket() -> None:
    from molt.daemon.agent import power_verdict

    expired = {"win-box": {"action": "shutdown", "expires_at": time.time() - 5}}
    assert power_verdict(requires_permission=True, tickets=expired, shell="win-box") == "ask"
    fresh = {"win-box": {"action": "shutdown", "expires_at": time.time() + 60}}
    assert power_verdict(requires_permission=True, tickets=fresh, shell="win-box") == "shutdown"


# ------------------------------------------------------------------ machine list


class _FakeBackend:
    """只实现 cmd_machine 用到的那四个方法。

    用假后端而不是真 Warden：这些用例测的是**命令怎么说话、什么时候拒绝**，
    不是账本怎么落盘（那在 test_power_ticket.py 与 test_shell_attrs.py 里）。
    """

    def __init__(self, *, here: str = "", power: Dict[str, Any] | None = None) -> None:
        self.here = here
        self.power = power or {}
        self.woken: List[str] = []
        self.tickets: List[Dict[str, Any]] = []

    def shells(self) -> Dict[str, Dict[str, Any]]:
        return {
            "nas-box": {"role": "nas", "address": "", "wake_mac": "AA-BB-CC-DD-EE-01"},
            "win-box": {"role": "desktop", "address": "", "wakeable": True,
                        "wake_mac": "AA-BB-CC-DD-EE-02"},
            "nomac-box": {"role": "desktop", "wakeable": True},
        }

    def status(self) -> Dict[str, Any]:
        return {
            "latest_generation": {"generation": 9, "consumed_by": self.here},
            "heartbeats": {"win-box": {"age_seconds": 3.0}, "nas-box": {"age_seconds": 40.0}},
            "power": self.power,
        }

    def wake(self, mac: str, **kw: Any) -> Dict[str, Any]:
        self.woken.append(mac)
        return {"sent": ["192.168.50.255", "255.255.255.255"]}

    def request_power(self, shell: str, action: str, **kw: Any) -> Dict[str, Any]:
        ticket = {"action": action, "expires_at_readable": "2026-11-18 00:30:00"}
        self.tickets.append({"shell": shell, "action": action})
        return ticket


def _fake(monkeypatch: pytest.MonkeyPatch, be: _FakeBackend) -> None:
    monkeypatch.setattr(cli, "_backend", lambda args: be)


def test_machine_list_shows_role_heartbeat_and_where_the_fish_is(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake(monkeypatch, _FakeBackend(here="win-box"))
    assert cli.cmd_machine(_args(Path("."), "list")) == 0
    out = capsys.readouterr().out
    assert "nas-box" in out and "win-box" in out
    assert "鱼在这里" in out
    assert "在跑" in out


def test_machine_on_uses_the_mac_from_the_ledger(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    be = _FakeBackend()
    _fake(monkeypatch, be)
    assert cli.cmd_machine(_args(Path("."), "on", "win-box")) == 0
    assert be.woken == ["AA-BB-CC-DD-EE-02"]
    assert "已发魔术封包" in capsys.readouterr().out


def test_machine_on_refuses_a_shell_without_a_mac(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake(monkeypatch, _FakeBackend())
    assert cli.cmd_machine(_args(Path("."), "on", "nomac-box")) == 4
    assert "MAC" in capsys.readouterr().out


def test_machine_off_refuses_the_shell_the_fish_is_standing_in(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    be = _FakeBackend(here="nas-box")
    _fake(monkeypatch, be)
    assert cli.cmd_machine(_args(Path("."), "off", "nas-box")) == 4
    assert "鱼就在" in capsys.readouterr().out
    assert be.tickets == []


def test_machine_off_leaves_a_ticket_for_someone_else(monkeypatch: pytest.MonkeyPatch) -> None:
    be = _FakeBackend(here="nas-box")
    _fake(monkeypatch, be)
    assert cli.cmd_machine(_args(Path("."), "off", "win-box")) == 0
    assert be.tickets == [{"shell": "win-box", "action": "shutdown"}]


def _plain(text: str) -> str:
    """去掉 ANSI 颜色再断言 —— 否则「1. nas-box」会变成「1. \\x1b[36mnas-box」。"""
    import re

    return re.sub("\x1b\\[[0-9;]*m", "", text)


def test_the_list_is_numbered(root: Path, capsys) -> None:
    assert cli.cmd_machine(_args(root, "list")) == 0
    out = _plain(capsys.readouterr().out)
    assert "1. nas-box" in out or "1. " in out
    assert "2. " in out


def test_a_number_out_of_range_is_refused(root: Path, capsys) -> None:
    rc = cli.cmd_machine(_args(root, "on", "9"))
    assert rc == 4
    assert "9" in _plain(capsys.readouterr().out)


def test_a_number_wakes_the_row_the_list_printed(root: Path, capsys) -> None:
    # 2 号是 win-box（有 MAC），所以 on 2 应该真的走到 wake 分支
    rc = cli.cmd_machine(_args(root, "on", "2"))
    assert rc == 0
    out = _plain(capsys.readouterr().out)
    assert "win-box" in out
