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
    assert "还没有上线" in reply.text
    assert "鱼没动" in reply.text


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

