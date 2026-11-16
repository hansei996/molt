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
