"""M2 的判据与存储：定时自动搬家。

这一组测试只关心**"什么时候允许它自己动"**，不关心它怎么搬
（那是 `molt migrate` 的事，`test_migrate_confirm.py` 已经钉住了）。

最要紧的一条：所有判不了的输入都必须走 False。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from molt import autopilot
from molt.errors import NeedsHuman
from molt.warden import Warden


def _today_at(hour: int, minute: int, *, day_offset: int = 0) -> float:
    lt = time.localtime()
    base = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + day_offset, hour, minute, 0, 0, 0, -1))
    return base


def _policy(**over):
    data = {
        "id": "night-home",
        "enabled": True,
        "source": "nas-box",
        "target": "desk-box",
        "schedule": "23:30",
        "window_minutes": 30,
        "daily_limit": 2,
        "expires_at": 0.0,
    }
    data.update(over)
    return data


# ---------------------------------------------------------------- 校验


def test_a_complete_policy_has_no_problems():
    assert autopilot.policy_problems(_policy()) == []


@pytest.mark.parametrize(
    "key,value,hint",
    [
        ("id", "", "id"),
        ("source", "", "source"),
        ("target", "", "target"),
        ("schedule", "not a time", "schedule"),
        ("daily_limit", 0, "daily_limit"),
        ("window_minutes", -1, "window_minutes"),
    ],
)
def test_a_broken_policy_is_refused_with_the_field_name(key, value, hint):
    problems = autopilot.policy_problems(_policy(**{key: value}))
    assert any(hint in p for p in problems)


def test_source_and_target_being_the_same_is_refused():
    problems = autopilot.policy_problems(_policy(target="nas-box"))
    assert any("同一台壳" in p for p in problems)


def test_an_unknown_key_is_refused_not_ignored():
    problems = autopilot.policy_problems(_policy(taget="desk-box"))
    assert any("不认识的键" in p and "taget" in p for p in problems)


def test_an_expired_policy_cannot_be_stored():
    problems = autopilot.policy_problems(_policy(expires_at=time.time() - 10))
    assert any("过期" in p for p in problems)


# ---------------------------------------------------------------- 到点了吗


def test_when_the_window_is_open_it_is_due(monkeypatch=None):
    now = _today_at(23, 35)
    ok, why = autopilot.due(_policy(), now=now, shell="nas-box")
    assert ok, why


def test_outside_the_window_it_is_not_due():
    now = _today_at(12, 0)
    ok, why = autopilot.due(_policy(), now=now, shell="nas-box")
    assert not ok and "还没到点" in why


def test_a_policy_that_runs_elsewhere_does_not_fire_here():
    now = _today_at(23, 35)
    ok, why = autopilot.due(_policy(), now=now, shell="desk-box")
    assert not ok and "不是这台" in why


def test_a_disabled_policy_never_fires():
    now = _today_at(23, 35)
    ok, why = autopilot.due(_policy(enabled=False), now=now, shell="nas-box")
    assert not ok and "关着" in why


def test_an_unreadable_schedule_is_never_treated_as_due():
    now = _today_at(23, 35)
    ok, why = autopilot.due(_policy(schedule="每天半夜"), now=now, shell="nas-box")
    assert not ok and "读不懂" in why


def test_an_expired_policy_does_not_fire_even_in_the_window():
    now = _today_at(23, 35)
    ok, why = autopilot.due(_policy(expires_at=now - 1), now=now, shell="nas-box")
    assert not ok and "过期" in why


def test_the_daily_limit_is_counted_from_the_ledger():
    now = _today_at(23, 35)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    attempts = [
        {"at": now - 3600, "at_readable": stamp, "result": "failed"},
        {"at": now - 1800, "at_readable": stamp, "result": "ok"},
    ]
    ok, why = autopilot.due(_policy(), now=now, shell="nas-box", attempts=attempts)
    assert not ok and "上限" in why


def test_after_a_failed_attempt_the_cooldown_holds_it_back():
    now = _today_at(23, 35)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    attempts = [{"at": now - 30, "at_readable": stamp, "result": "failed"}]
    ok, why = autopilot.due(_policy(), now=now, shell="nas-box", attempts=attempts)
    assert not ok and "冷却" in why


def test_yesterdays_attempts_do_not_count_towards_today():
    now = _today_at(23, 35)
    old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 86400))
    attempts = [{"at": now - 86400, "at_readable": old, "result": "ok"}] * 5
    ok, why = autopilot.due(_policy(), now=now, shell="nas-box", attempts=attempts)
    assert ok, why


# ---------------------------------------------------------------- 存储


def test_a_policy_survives_a_restart(tmp_path: Path):
    Warden(root=tmp_path).set_policy(_policy(), by="lead")
    again = Warden(root=tmp_path).policy_view()
    assert again["id"] == "night-home"
    assert again["source"] == "nas-box"


def test_a_broken_policy_file_reads_as_no_policy_at_all(tmp_path: Path):
    (tmp_path / "policy.json").write_text("{not json", encoding="utf-8")
    assert Warden(root=tmp_path).policy_view() == {}


def test_a_policy_that_does_not_validate_is_not_written(tmp_path: Path):
    with pytest.raises(NeedsHuman):
        Warden(root=tmp_path).set_policy(_policy(schedule="nope"))
    assert not (tmp_path / "policy.json").exists()


def test_clearing_removes_the_file_instead_of_disabling_it(tmp_path: Path):
    w = Warden(root=tmp_path)
    w.set_policy(_policy(), by="lead")
    assert w.clear_policy() is True
    assert not (tmp_path / "policy.json").exists()
    assert w.policy_view() == {}


def test_an_attempt_is_recorded_and_visible(tmp_path: Path):
    w = Warden(root=tmp_path)
    w.record_attempt(result="failed", reason="目标没上线", policy_id="night-home", by="homer")
    got = Warden(root=tmp_path).attempts_view()
    assert got and got[-1]["result"] == "failed"
    assert got[-1]["reason"] == "目标没上线"


def test_an_unknown_attempt_result_is_refused(tmp_path: Path):
    with pytest.raises(NeedsHuman):
        Warden(root=tmp_path).record_attempt(result="whatever")


def test_status_carries_policy_and_attempts(tmp_path: Path):
    w = Warden(root=tmp_path)
    w.set_policy(_policy(), by="lead")
    w.record_attempt(result="ok", reason="搬过去了", policy_id="night-home")
    st = Warden(root=tmp_path).status()
    assert st["policy"]["id"] == "night-home"
    assert st["attempts"][-1]["result"] == "ok"

def test_policy_commands_can_point_at_a_remote_ledger(tmp_path, capsys):
    """`molt policy` 必须能指向远端账本 —— 否则它只能在 NAS 的那台机器上敲。"""
    import argparse

    from molt.cli import cmd_policy

    ns = argparse.Namespace(
        policy_action="show",
        root=None,
        warden_url="http://127.0.0.1:1",
        warden_token="t0ken",
    )
    rc = cmd_policy(ns)
    assert rc == 2
    out = capsys.readouterr().out
    assert "127.0.0.1:1" in out

def test_policy_set_prints_what_it_saved_even_from_a_remote_ledger(tmp_path, capsys):
    """远端后端回 {"policy": {…}}，本地后端回策略本身 —— 两种都要念对。"""
    import argparse

    from molt.cli import cmd_policy

    class FakeBackend:
        kind = "remote"

        def set_policy(self, policy, *, by=""):
            return {"ok": True, "policy": dict(policy, created_by=by)}

    import molt.cli as cli_mod

    real = cli_mod._backend
    cli_mod._backend = lambda args: FakeBackend()
    try:
        ns = argparse.Namespace(
            policy_action="set",
            root=None,
            warden_url="http://x:1",
            warden_token="t",
            id="night-home",
            source="a",
            target="b",
            at="23:30",
            window_minutes=30,
            daily_limit=2,
            expires_at=0.0,
            by="lead",
        )
        assert cmd_policy(ns) == 0
    finally:
        cli_mod._backend = real

    out = capsys.readouterr().out
    assert "存好了：night-home（a → b）" in out


# ---------------------------------------------------------------- 链式收尾：搬完关旧壳

def _power_policy(**kw):
    base = dict(
        id="morning-home",
        enabled=True,
        source="desk-box",
        target="nas-box",
        schedule="09:00",
        window_minutes=30,
        daily_limit=1,
        power_off_source=True,
        power_action="shutdown",
        arrival_timeout_seconds=60.0,
    )
    base.update(kw)
    return base


def _arrival(gen=7, shell="nas-box", delivered=True):
    return {"generation": gen, "shell": shell, "delivered": delivered, "at": 1.0}


def test_power_decision_needs_the_new_shell_to_have_taken_it():
    ok, why = autopilot.power_decision(
        _power_policy(), generation=7, consumed_by="", shell="desk-box", arrival=_arrival()
    )
    assert not ok and "还没接走" in why


def test_power_decision_needs_the_arrival_receipt():
    ok, why = autopilot.power_decision(
        _power_policy(), generation=7, consumed_by="nas-box", shell="desk-box", arrival=None
    )
    assert not ok and "播报" in why


def test_power_decision_rejects_a_receipt_from_another_generation():
    ok, why = autopilot.power_decision(
        _power_policy(),
        generation=8,
        consumed_by="nas-box",
        shell="desk-box",
        arrival=_arrival(gen=7),
    )
    assert not ok and "这一代" in why


def test_power_decision_rejects_a_receipt_from_another_shell():
    ok, why = autopilot.power_decision(
        _power_policy(),
        generation=7,
        consumed_by="nas-box",
        shell="desk-box",
        arrival=_arrival(shell="other-box"),
    )
    assert not ok and "不是新壳" in why


def test_power_decision_rejects_an_undelivered_receipt():
    ok, why = autopilot.power_decision(
        _power_policy(),
        generation=7,
        consumed_by="nas-box",
        shell="desk-box",
        arrival=_arrival(delivered=False),
    )
    assert not ok and "没送到" in why


def test_power_decision_is_off_when_the_policy_says_so():
    ok, why = autopilot.power_decision(
        _power_policy(power_off_source=False),
        generation=7,
        consumed_by="nas-box",
        shell="desk-box",
        arrival=_arrival(),
    )
    assert not ok and "没要求" in why


def test_power_decision_allows_the_shutdown_when_everything_holds():
    ok, why = autopilot.power_decision(
        _power_policy(), generation=7, consumed_by="nas-box", shell="desk-box", arrival=_arrival()
    )
    assert ok and "可以关" in why


def test_a_power_action_of_keep_with_power_off_is_a_contradiction():
    problems = autopilot.policy_problems(_power_policy(power_action="keep"))
    assert any("自相矛盾" in p for p in problems)


def test_an_unknown_power_action_is_refused():
    problems = autopilot.policy_problems(_power_policy(power_action="explode"))
    assert any("power_action" in p for p in problems)


def test_the_arrival_timeout_is_bounded():
    problems = autopilot.policy_problems(_power_policy(arrival_timeout_seconds=1.0))
    assert any("arrival_timeout_seconds" in p for p in problems)
