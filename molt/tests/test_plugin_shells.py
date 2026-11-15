"""插件里「壳是什么」的三处用法。

账本存了壳属性，但**如果没人用它，那一层就是装饰**。这三条用例钉住用它的
三个地方，以及一个更重要的性质：**没登记就不许编**。

编一句"那边是：unknown"看起来无害，实际后果是它读起来像一个**已知的事实**，
而它其实是"我不知道"。人被那句话误导之后，会把一次迁徙的责任算到你头上。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from molt.tests.test_molt_want import (
    FakeClock,
    FakeLedger,
    _Ok,
    _cfg,
    plugin_dir as plugin_dir_fixture,
    write_cfg,
)


@pytest.fixture()
def plugin_dir(plugin_dir_fixture: Path) -> Path:
    return plugin_dir_fixture


def mover(plugin_dir: Path, *, payload=None):
    from astrbot_plugin_molt import command as cmd

    return cmd.MoltMover(
        plugin_dir,
        clock=FakeClock(),
        runner=lambda *a, **k: _Ok(),
        opener=FakeLedger(payload),
    )


def _status_with(beats, shells=None) -> dict:
    return {
        "status": {
            "heartbeats": beats,
            "shells": shells or {},
            "lease": {"holder": None, "state": "no-lease"},
        }
    }


def test_where_shows_what_the_target_shell_is(plugin_dir: Path) -> None:
    payload = _status_with(
        {"nas-shell": {"age_seconds": 3, "age_readable": "3 秒前", "stale": False}},
        {"nas-shell": {"role": "nas", "gpu": "", "wakeable": True}},
    )
    m = mover(plugin_dir, payload=payload)
    text = "\n".join(m._where_lines(payload["status"]))
    assert "nas" in text
    assert "可唤醒" in text


def test_where_does_not_invent_a_role_when_nothing_is_declared(plugin_dir: Path) -> None:
    """这条是本文件最重要的：**没登记就不写**。

    它防的不是"少显示一行"，而是 `_shell_what()` 哪天被改成
    `return "unknown"` —— 那会把"我不知道"伪装成"我知道"。
    """
    payload = _status_with(
        {"mystery-box": {"age_seconds": 3, "age_readable": "3 秒前", "stale": False}},
        {},
    )
    m = mover(plugin_dir, payload=payload)
    text = "\n".join(m._where_lines(payload["status"]))
    assert "mystery-box" in text
    assert "unknown" not in text
    assert "没登记" not in text  # 也不加一句"没登记"的旁白：安静地少一段就对了


def test_the_proposal_says_what_the_target_is(plugin_dir: Path) -> None:
    payload = _status_with(
        {"win-shell": {}, "nas-shell": {}},
        {"nas-shell": {"role": "nas", "gpu": "", "wakeable": True}},
    )
    m = mover(plugin_dir, payload=payload)
    cfg = _cfg(plugin_dir)
    reply = m.request_move(cfg, target="nas-shell", reason="这边要关机了")
    assert "那边是：nas" in reply.text
    assert "可唤醒" in reply.text


def test_the_proposal_stays_silent_when_the_target_is_undeclared(plugin_dir: Path) -> None:
    payload = _status_with({"win-shell": {}, "nas-shell": {}}, {})
    m = mover(plugin_dir, payload=payload)
    cfg = _cfg(plugin_dir)
    reply = m.request_move(cfg, target="nas-shell", reason="")
    assert "那边是：" not in reply.text
    assert "unknown" not in reply.text


def test_a_busy_ledger_actually_stops_the_go(plugin_dir: Path) -> None:
    """这条守的是"读对了字段名"。

    原来的实现读 `lease["shell_id"]`，而账本的租约视图只有 `holder` ——
    于是这个守卫**从来没生效过**，看起来和"确实没人在搬"一模一样。
    """
    payload = {
        "status": {
            "heartbeats": {"win-shell": {}, "nas-shell": {}},
            "shells": {},
            "lease": {"holder": "nas-shell", "state": "active", "epoch": 7},
        }
    }
    m = mover(plugin_dir, payload=payload)
    cfg = _cfg(plugin_dir)
    reply = m.request_move(cfg, target="nas-shell", reason="")
    assert "不插队" in reply.text
    assert "nas-shell" in reply.text
    assert m._proposal is None


def test_an_expired_lease_does_not_stop_the_go(plugin_dir: Path) -> None:
    """租约过期了就别再拦 —— stale 的 holder 不该永远挡住下一只鱼。"""
    payload = {
        "status": {
            "heartbeats": {"win-shell": {}, "nas-shell": {}},
            "shells": {},
            "lease": {"holder": "nas-shell", "state": "expired"},
        }
    }
    m = mover(plugin_dir, payload=payload)
    cfg = _cfg(plugin_dir)
    reply = m.request_move(cfg, target="nas-shell", reason="")
    assert "不插队" not in reply.text
    assert m._proposal is not None
