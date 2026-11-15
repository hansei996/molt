"""两次确认：先问要不要开机，再问要不要搬。

主人的原话（2026-11-16 00:24）：
「应该是我发 /molt go 它先告诉我，window 当前是开机的还是关机的，如果是关机的，
问我要不要开机，我 confirm 了，就等开机。等它开机了，再问我要不要迁移，
所以我就发两次 confirm 不就行了吗」

这一组测试钉的是这条流程，以及它最重要的一条性质：
**任何一步失败时，鱼都在源壳上。**
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from molt.tests.test_molt_want import (  # noqa: E402
    FakeClock,
    FakeLedger,
    _cfg,
    _Ok,
    mover,
    plugin_dir as plugin_dir_fixture,
    write_cfg,
)


@pytest.fixture
def plugin_dir(plugin_dir_fixture: Path) -> Path:
    return plugin_dir_fixture


OFF_PAYLOAD: Dict[str, Any] = {
    "status": {
        "current_epoch": 31,
        "lease": None,
        "heartbeats": {
            "win-shell": {"age_seconds": 2.0},
            "nas-shell": {"age_seconds": 149.0, "last_seen_readable": "两分半前"},
        },
        "shells": {"nas-shell": {"role": "nas", "wakeable": True, "wake_mac": "D8-BB-C1-11-C3-D2"}},
    }
}

ON_PAYLOAD: Dict[str, Any] = {
    "status": {
        "current_epoch": 31,
        "lease": None,
        "heartbeats": {
            "win-shell": {"age_seconds": 2.0},
            "nas-shell": {"age_seconds": 3.0},
        },
        "shells": {"nas-shell": {"role": "nas", "wakeable": True, "wake_mac": "D8-BB-C1-11-C3-D2"}},
    }
}


def test_go_tells_you_whether_the_target_is_on_or_off(plugin_dir: Path) -> None:
    """第一句话必须是"它是关着的"，不是"我准备好了"。"""
    m = mover(plugin_dir, payload=OFF_PAYLOAD)
    cfg = _cfg(plugin_dir)

    reply = m.request_move(cfg, target="nas-shell", reason="")

    assert reply.plan is None
    assert "关着的" in reply.text or "149" in reply.text
    assert "/molt confirm" in reply.text
    assert "两次确认" in reply.text


def test_confirm_sends_the_packet_and_does_not_move(plugin_dir: Path) -> None:
    """第一次 confirm = 只发封包。它不搬，鱼还在源壳上。"""
    m = mover(plugin_dir, payload=OFF_PAYLOAD)
    cfg = _cfg(plugin_dir)
    m.request_move(cfg, target="nas-shell", reason="")

    sent: List[str] = []
    m.wake_target = lambda cfg, shell: (sent.append(shell) or (True, "已由账本发出 3 个封包"))  # type: ignore[assignment]

    reply = m._confirm(cfg)

    assert sent == ["nas-shell"]
    assert reply.plan is None, "叫醒这一步绝不能排定迁徙"
    assert m.has_pending() is False
    assert "那一 次" not in reply.text
    assert "第二次确认" in reply.text


def test_second_confirm_actually_moves(plugin_dir: Path) -> None:
    m = mover(plugin_dir, payload=OFF_PAYLOAD)
    cfg = _cfg(plugin_dir)
    m.request_move(cfg, target="nas-shell", reason="")
    m.wake_target = lambda cfg, shell: (True, "已由账本发出 3 个封包")  # type: ignore[assignment]
    m._confirm(cfg)  # 第一次：发封包

    # 它开机了（账本里的心跳变新）
    m._opener = FakeLedger(ON_PAYLOAD)  # type: ignore[attr-defined]
    reply = m._confirm(cfg)  # 第二次：真的搬

    assert reply.plan is not None
    argv: List[str] = reply.plan.argv
    assert argv[argv.index("--target") + 1] == "nas-shell"


def test_if_it_never_wakes_up_the_fish_does_not_move(plugin_dir: Path) -> None:
    """最要紧的一条：叫不醒时，鱼哪儿都不去。"""
    m = mover(plugin_dir, payload=OFF_PAYLOAD)
    cfg = _cfg(plugin_dir)
    m.request_move(cfg, target="nas-shell", reason="")
    m.wake_target = lambda cfg, shell: (True, "已由账本发出 3 个封包")  # type: ignore[assignment]
    m._confirm(cfg)  # 发封包

    # 还是没上线（心跳依旧 149 秒）
    m._opener = FakeLedger(OFF_PAYLOAD)  # type: ignore[attr-defined]
    reply = m._confirm(cfg)

    assert reply.plan is None
    assert "还没上线" in reply.text
    assert "魔术封包" in reply.text
    assert "鱼没动" in reply.text
    # 提议不能丢：丢了他就得回去重新 `/molt go`，而"这次是谁提的"也会丢
    assert m.has_proposal() is True


def test_no_mac_means_it_says_so_instead_of_pretending(plugin_dir: Path) -> None:
    payload = {
        "status": {
            "current_epoch": 31,
            "lease": None,
            "heartbeats": {"win-shell": {"age_seconds": 2.0}, "nas-shell": {"age_seconds": 200.0}},
            "shells": {"nas-shell": {"role": "nas", "wakeable": True}},
        }
    }
    m = mover(plugin_dir, payload=payload)
    cfg = _cfg(plugin_dir)
    m.request_move(cfg, target="nas-shell", reason="")

    ok_sent, msg = m.wake_target(cfg, "nas-shell")

    assert ok_sent is False
    assert "MAC" in msg



def test_a_target_that_just_went_offline_gets_a_packet_at_confirm_time(
    plugin_dir: Path,
) -> None:
    """`/molt go` 时它开着（于是问都没问），按 confirm 时它已经躺下了。

    这是 2026-11-16 00:38 那次真实翻车：上一次交接完关机票执行，
    机器自己关了；再 `/molt go` 时心跳还是新的 → 走的是"直接搬"分支；
    等按 confirm，它已经关机四分钟。光说"没上线"没用 —— 得当场发封包。
    """
    m = mover(plugin_dir, payload=ON_PAYLOAD)
    cfg = _cfg(plugin_dir)
    m.request_move(cfg, target="nas-shell", reason="")  # 此时 on → phase=move

    sent: List[str] = []
    m.wake_target = lambda cfg, shell: (sent.append(shell) or (True, "已由账本发出 2 个封包"))  # type: ignore[assignment]
    m._opener = FakeLedger(OFF_PAYLOAD)  # type: ignore[attr-defined]  # 它已经关了

    reply = m._confirm(cfg)

    assert sent == ["nas-shell"], "这种时候必须真的发一封，而不是只回一句『没上线』"
    assert reply.plan is None
    assert "魔术封包" in reply.text
    assert "鱼没动" in reply.text
    assert m.has_proposal() is True


def test_when_it_cannot_wake_it_says_which_part_is_missing(plugin_dir: Path) -> None:
    """走的是"直接搬"分支（go 时还开着），按 confirm 时关了、而且账本里没 MAC。"""
    no_mac_off = {
        "status": {
            "current_epoch": 31,
            "lease": None,
            "heartbeats": {"win-shell": {"age_seconds": 2.0}, "nas-shell": {"age_seconds": 200.0}},
            "shells": {"nas-shell": {"role": "nas", "wakeable": True}},
        }
    }
    m = mover(plugin_dir, payload=ON_PAYLOAD)
    cfg = _cfg(plugin_dir)
    m.request_move(cfg, target="nas-shell", reason="")
    m._opener = FakeLedger(no_mac_off)  # type: ignore[attr-defined]

    reply = m._confirm(cfg)

    assert reply.plan is None
    assert "叫不醒" in reply.text
    assert "MAC" in reply.text
    assert "鱼没动" in reply.text


def test_wake_target_really_constructs_a_client(plugin_dir: Path, monkeypatch) -> None:
    """不许再让"整段被 mock 掉"掩盖 `WardenClient` 没导入这类错。

    2026-11-16 00:52 真机原文：
        `我也叫不醒它：让账本发包失败：name 'WardenClient' is not defined`
    —— 当时 5 条测试全绿，因为它们都把 `wake_target` 整个换成了 lambda。
    """
    import molt.client as client_mod

    calls: List[tuple] = []

    class FakeClient:
        def __init__(self, base: str, token: str) -> None:
            calls.append(("init", base, token))

        def wake(self, mac: str, *, host: str = "") -> Dict[str, Any]:
            calls.append(("wake", mac, host))
            return {"ok": True, "sent": ["192.168.50.255"], "failed": []}

    monkeypatch.setattr(client_mod, "WardenClient", FakeClient)

    m = mover(plugin_dir, payload=ON_PAYLOAD)
    cfg = _cfg(plugin_dir, warden_url="http://192.168.50.50:6186", warden_token="t0k")

    ok, msg = m.wake_target(cfg, "nas-shell")

    assert ok is True
    assert ("init", "http://192.168.50.50:6186", "t0k") in calls
    assert any(c[0] == "wake" and c[1] == "D8-BB-C1-11-C3-D2" for c in calls)
    assert "1 个封包" in msg


def test_wake_target_reports_it_when_nothing_went_out(plugin_dir: Path, monkeypatch) -> None:
    import molt.client as client_mod

    class FakeClient:
        def __init__(self, base: str, token: str) -> None:
            pass

        def wake(self, mac: str, *, host: str = "") -> Dict[str, Any]:
            return {"ok": False, "sent": [], "failed": ["socket boom"]}

    monkeypatch.setattr(client_mod, "WardenClient", FakeClient)

    m = mover(plugin_dir, payload=ON_PAYLOAD)
    cfg = _cfg(plugin_dir)
    ok, msg = m.wake_target(cfg, "nas-shell")

    assert ok is False
    assert "socket boom" in msg


def test_it_does_not_resend_the_packet_on_every_confirm(plugin_dir: Path) -> None:
    """2026-11-16 00:58：机器正在开机，主人连发 5 次 confirm，每次都再发一封。

    重发不提高成功率，只让人以为它坏了。60 秒内第二封就算了。
    """
    m = mover(plugin_dir, payload=OFF_PAYLOAD)
    cfg = _cfg(plugin_dir)

    sent: List[str] = []

    class FakeClient:
        def __init__(self, base: str, token: str) -> None:
            pass

        def wake(self, mac: str, *, host: str = "") -> Dict[str, Any]:
            sent.append(mac)
            return {"ok": True, "sent": ["192.168.50.255"], "failed": []}

    import molt.client as client_mod

    original = client_mod.WardenClient
    client_mod.WardenClient = FakeClient  # type: ignore[assignment]
    try:
        ok1, msg1 = m.wake_target(cfg, "nas-shell")
        ok2, msg2 = m.wake_target(cfg, "nas-shell")
    finally:
        client_mod.WardenClient = original  # type: ignore[assignment]

    assert ok1 is True and ok2 is True
    assert len(sent) == 1, "第二封必须被冷却挡掉"
    assert "不重复发" in msg2
