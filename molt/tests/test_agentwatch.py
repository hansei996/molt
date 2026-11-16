"""`molt.agentwatch` 的桩测试：只喂事实、只看判决，不碰真进程。

这些用例全都是一次真事故的切片：2026-11-17 那两个晚上，agent 死了而没人知道。
所以这里断言的不是"函数返回什么字符串"，而是**"哪种情况必须被当成死"**。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

_MOD = None


def _watch():
    global _MOD
    if _MOD is None:
        spec = importlib.util.spec_from_file_location(
            "molt_agentwatch_under_test", REPO / "molt" / "agentwatch.py"
        )
        mod = importlib.util.module_from_spec(spec)
        # dataclass / 字符串注解解析要靠 sys.modules 里的模块对象
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        _MOD = mod
    return _MOD


def test_a_missing_process_is_dead_even_with_fresh_timestamps():
    w = _watch()
    got = w.judge(pid_alive=False, state_age=1.0, heartbeat_age=1.0)
    assert got["state"] == w.DEAD
    assert "进程不在" in got["reason"]


def test_a_running_process_with_a_stalled_state_file_is_dead():
    """这条是核心：进程在 ≠ 活着。卡死的进程和死掉的进程一样干不了活。"""
    w = _watch()
    got = w.judge(pid_alive=True, state_age=w.STATE_SILENT_SECONDS + 60, heartbeat_age=5.0)
    assert got["state"] == w.DEAD
    assert "不再干活" in got["reason"]


def test_a_running_process_with_fresh_state_but_no_heartbeat_is_only_silent():
    w = _watch()
    got = w.judge(
        pid_alive=True,
        state_age=3.0,
        heartbeat_age=w.HEARTBEAT_SILENT_SECONDS + 300,
    )
    assert got["state"] == w.SILENT
    assert "没心跳" in got["reason"]


def test_everything_fresh_is_alive():
    w = _watch()
    got = w.judge(pid_alive=True, state_age=4.0, heartbeat_age=6.0)
    assert got["state"] == w.ALIVE
    assert got["reason"] == ""
    assert w.alert_kind(got) is None


def test_being_unable_to_read_the_process_is_not_a_death_sentence():
    """读不到进程、时间戳也新鲜 —— 不许猜。宁可漏报也不误报。"""
    w = _watch()
    got = w.judge(pid_alive=None, state_age=3.0, heartbeat_age=3.0)
    assert got["state"] == w.UNKNOWN
    assert w.alert_kind(got) is None


def test_being_unable_to_read_the_process_but_stale_timestamps_is_silent():
    w = _watch()
    got = w.judge(
        pid_alive=None,
        state_age=w.STATE_SILENT_SECONDS + 600,
        heartbeat_age=None,
    )
    assert got["state"] == w.SILENT
    assert got["silent_for"] >= w.STATE_SILENT_SECONDS + 600


def test_no_reading_at_all_is_unknown():
    w = _watch()
    got = w.judge(pid_alive=None, state_age=None, heartbeat_age=None)
    assert got["state"] == w.UNKNOWN
    assert got["silent_for"] is None


def test_dead_and_silent_map_to_distinct_alert_kinds():
    w = _watch()
    dead = w.judge(pid_alive=False)
    silent = w.judge(pid_alive=True, state_age=1.0, heartbeat_age=w.HEARTBEAT_SILENT_SECONDS + 1)
    assert w.alert_kind(dead) == "agent-dead"
    assert w.alert_kind(silent) == "agent-silent"
    assert w.alert_kind(dead) != w.alert_kind(silent)


def test_the_alert_message_says_what_it_costs_to_ignore_it():
    w = _watch()
    dead = w.judge(pid_alive=False)
    text = w.alert_message(dead, shell="desktop-h2kgiao")
    assert "desktop-h2kgiao" in text
    assert "早上那条自动搬家策略就不会触发" in text
    # 活着的时候不该产生任何文案（否则会把"没事"也写成一条告警）
    assert w.alert_message(w.judge(pid_alive=True, state_age=1.0, heartbeat_age=1.0)) == ""
