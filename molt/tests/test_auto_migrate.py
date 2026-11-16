"""M2：守护进程真的会在"到点"时自己搬吗。

这里只测**闸门**：政策对不对、鱼在不在我这儿、有没有租约、AstrBot 在不在跑。
`molt migrate` 本身用假 runner 接住 —— 我们不希望测试真的去搬家。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module  # dataclass 解析字符串注解要用
    spec.loader.exec_module(module)
    return module


agent = _load("molt/daemon/agent.py", "molt_agent_m2")
homer = _load("molt/daemon/homer.py", "molt_homer_m2")

ME = "nas-box"
TARGET = "desk-box"


def _at(hour: int, minute: int) -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1))


def status(*, policy=None, attempts=(), consumed_by=ME, holder=None, arrivals=None, generation=7):
    return {
        "current_epoch": 3,
        "lease": {"holder": holder, "state": "held" if holder else "no-lease"},
        "latest_generation": {
            "generation": generation,
            "target_shell": ME,
            "consumed_by": consumed_by,
            "pushed_by": "desk-box",
        },
        "policy": policy if policy is not None else {
            "id": "night-home",
            "enabled": True,
            "source": ME,
            "target": TARGET,
            "schedule": "23:30",
            "window_minutes": 30,
            "daily_limit": 2,
            "expires_at": 0.0,
        },
        "attempts": list(attempts),
        "arrivals": dict(arrivals or {}),
    }


class FakeRunner:
    def __init__(self, returncode=0, stdout="✓ 搬迁完成", stderr=""):
        self.calls = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


def _settings(**kw):
    base = dict(
        execute=True,
        shell=ME,
        data_dir="/data",
        molt_pkg=str(ROOT),
        python="python3",
        warden_url="http://127.0.0.1:6186",
        warden_token="t",
        port=6199,
        interval=0.01,
        timeout=1.0,
    )
    base.update(kw)
    return agent.Settings(**base)


@pytest.fixture()
def due_now(monkeypatch):
    """把"现在"钉在窗口里 —— 测试不该依赖跑测试的时刻。"""
    monkeypatch.setattr(homer.time, "mktime", homer.time.mktime)
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: _at(23, 35))
    yield
    monkeypatch.setattr(time, "time", real_time)


def _agent(**kw):
    a = agent.Agent(settings=_settings(**kw), state_path=Path("/tmp/m2-agent-state.json"))
    a.port_probe = lambda **_: True
    # 单元测试不等真实的 60 秒宽限期；要测宽限期的用例自己调小 notice_seconds。
    a.notice_seconds = 0.0
    return a


def test_agent_migrates_when_everything_lines_up(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: _at(23, 35))
    a = _agent()
    runner = FakeRunner()
    a.migrate_runner = runner
    seen = []
    a.attempt_sender = lambda result, **kw: seen.append((result, kw)) or True

    step = a.maybe_auto_migrate({}, status(), None)

    assert step is not None and step.ok
    assert runner.calls, "应该真的跑了一次 migrate"
    argv = runner.calls[0][0]
    assert "migrate" in argv and "--target" in argv and TARGET in argv
    assert seen and seen[-1][0] == "ok"


def test_agent_does_not_move_when_the_fish_is_not_here(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: _at(23, 35))
    a = _agent()
    runner = FakeRunner()
    a.migrate_runner = runner
    assert a.maybe_auto_migrate({}, status(consumed_by="desk-box"), None) is None
    assert runner.calls == []


def test_agent_does_not_move_while_a_lease_is_held(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: _at(23, 35))
    a = _agent()
    runner = FakeRunner()
    a.migrate_runner = runner
    assert a.maybe_auto_migrate({}, status(holder="desk-box"), None) is None
    assert runner.calls == []


def test_agent_does_not_move_outside_the_window(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: _at(12, 0))
    a = _agent()
    runner = FakeRunner()
    a.migrate_runner = runner
    assert a.maybe_auto_migrate({}, status(), None) is None
    assert runner.calls == []


def test_agent_never_moves_without_execute():
    a = _agent(execute=False)
    runner = FakeRunner()
    a.migrate_runner = runner
    assert a.maybe_auto_migrate({}, status(), None) is None
    assert runner.calls == []


def test_a_failed_run_is_recorded_as_failed(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: _at(23, 35))
    a = _agent()
    a.migrate_runner = FakeRunner(returncode=6, stdout="鱼回到源壳了")
    seen = []
    a.attempt_sender = lambda result, **kw: seen.append(result) or True
    step = a.maybe_auto_migrate({}, status(), None)
    assert step is not None and not step.ok
    assert seen[-1] == "failed"
    assert step.detail.startswith("退出码 6")


def test_homer_uses_the_same_gate(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: _at(23, 35))
    s = homer.Settings(
        execute=True,
        shell=ME,
        data_dir="/data",
        molt_pkg=str(ROOT),
        python="python3",
        warden_url="http://127.0.0.1:6186",
        warden_token="t",
        dashboard_port=6185,
        interval=0.01,
        timeout=1.0,
    )
    h = homer.Homer(settings=s, state_path=Path("/tmp/m2-homer-state.json"))
    h.container_running = lambda: True
    runner = FakeRunner()
    h.migrate_runner = runner
    h.attempt_sender = lambda result, **kw: True
    got = h.maybe_auto_migrate({}, status())
    assert got is not None and got["ok"]
    assert runner.calls

# ------------------------------------------------- 搬完之后才轮到"关旧壳"

def _power_policy(**kw):
    base = {
        "id": "morning-home",
        "enabled": True,
        "source": ME,
        "target": TARGET,
        "schedule": "23:30",
        "window_minutes": 30,
        "daily_limit": 1,
        "expires_at": 0.0,
        "power_off_source": True,
        "power_action": "shutdown",
        "arrival_timeout_seconds": 60.0,
    }
    base.update(kw)
    return base


def _arrivals(gen=8, shell=TARGET, delivered=True):
    return {
        str(gen): {
            "generation": gen,
            "shell": shell,
            "delivered": delivered,
            "at_readable": "2026-11-17 00:00:00",
        }
    }


def test_power_ticket_is_filed_only_after_the_arrival_receipt(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: _at(23, 35))
    a = _agent()
    a.migrate_runner = FakeRunner()
    calls = []
    # 搬完之后账本说：第 8 代被目标接走，且播报回执已送达
    after = status(policy=_power_policy(), consumed_by=TARGET, generation=8,
                   arrivals=_arrivals(8))
    a.status_fetcher = lambda: after
    a.arrival_waiter = lambda gen, timeout: True
    a.power_requester = lambda shell, action, gen, **kw: calls.append((shell, action, gen, kw))
    seen = []
    a.attempt_sender = lambda result, **kw: seen.append(result) or True

    step = a.maybe_auto_migrate({}, status(policy=_power_policy()), None)

    assert step is not None and step.ok
    assert calls == [(ME, "shutdown", 8, {"by": "autopilot"})], calls
    assert seen[-1] == "ok"


def test_no_power_ticket_when_the_arrival_never_lands(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: _at(23, 35))
    a = _agent()
    a.migrate_runner = FakeRunner()
    a.status_fetcher = lambda: status(
        policy=_power_policy(), consumed_by=TARGET, generation=8, arrivals={}
    )
    a.arrival_waiter = lambda gen, timeout: False
    calls = []
    a.power_requester = lambda *a, **k: calls.append((a, k))
    seen = []
    a.attempt_sender = lambda result, **kw: seen.append(result) or True

    step = a.maybe_auto_migrate({}, status(policy=_power_policy()), None)

    assert step is not None and step.ok          # 搬家本身成功
    assert calls == []                            # 但一张票都没放
    assert seen[-1] == "skipped"                  # 而且如实记成"没关"


def test_no_power_ticket_when_the_migration_failed(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: _at(23, 35))
    a = _agent()
    a.migrate_runner = FakeRunner(returncode=6, stdout="鱼回到源壳了")
    a.status_fetcher = lambda: status(policy=_power_policy())
    a.arrival_waiter = lambda gen, timeout: True
    calls = []
    a.power_requester = lambda *a_, **k: calls.append((a_, k))

    step = a.maybe_auto_migrate({}, status(policy=_power_policy()), None)

    assert step is not None and not step.ok
    assert calls == []


def test_power_off_is_off_by_default_in_the_policy():
    from molt import autopilot

    policy = autopilot.normalize_policy({"id": "x", "source": "a", "target": "b"})
    assert policy["power_off_source"] is False
    assert policy["power_action"] == "shutdown"


def test_it_says_something_before_it_leaves(monkeypatch):
    """到点先发一句提醒 —— 主人 2026-11-17 的"体验优先"。"""
    monkeypatch.setattr(time, "time", lambda: _at(23, 35))
    a = _agent()
    runner = FakeRunner()
    a.migrate_runner = runner
    said = []
    a.notice_sender = lambda text: said.append(text) or True
    a.notice_seconds = 0  # 单测不要真等 60 秒

    step = a.maybe_auto_migrate({}, status(), None)

    assert said and "我准备搬去" in said[0]
    assert TARGET in said[0] and "/molt stop-auto" in said[0]
    assert step is not None and step.ok
    assert runner.calls, "提醒之后仍然要真的搬"


def test_a_failed_notice_does_not_block_the_move(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: _at(23, 35))
    a = _agent()
    runner = FakeRunner()
    a.migrate_runner = runner
    a.notice_sender = lambda text: False
    a.notice_seconds = 0

    step = a.maybe_auto_migrate({}, status(), None)

    assert step is not None and step.ok
    assert runner.calls, "提醒是体验，不是前置条件"


def test_turning_it_off_during_the_grace_period_stops_the_move(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: _at(23, 35))
    a = _agent()
    runner = FakeRunner()
    a.migrate_runner = runner
    a.notice_sender = lambda text: True
    a.notice_seconds = 0.01
    # 宽限期里再看一眼账本：主人已经把策略关了
    monkeypatch.setattr(a, "_fresh_status", lambda: {"policy": {"id": "night-home", "enabled": False}})
    seen = []
    a.attempt_sender = lambda result, **kw: seen.append((result, kw)) or True

    step = a.maybe_auto_migrate({}, status(), None)

    assert step is not None and not step.ok
    assert runner.calls == [], "主人回话了就不许搬"
    assert seen and seen[-1][0] == "skipped"
