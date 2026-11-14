"""`tools/molt-agent.py` 的测试。

这个文件守的是**公理 0 的三个推论**：
- 不是给我的，我不动
- 有人还攥着租约，我不落位
- 本机已经在跑，我不起第二只

跑测试的 Python 没装 AstrBot、也不需要装：agent 只用标准库。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AGENT_PATH = ROOT / "tools" / "molt-agent.py"


def _load_agent():
    spec = importlib.util.spec_from_file_location("molt_agent_under_test", AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # 必须先登记进 sys.modules 再 exec：文件里有 `from __future__ import annotations`，
    # `@dataclass` 解析字符串注解时会 `sys.modules.get(cls.__module__).__dict__`，
    # 没登记就是 None → AttributeError: 'NoneType' object has no attribute '__dict__'。
    sys.modules["molt_agent_under_test"] = module
    spec.loader.exec_module(module)
    return module


agent = _load_agent()


# ---------------------------------------------------------------- 夹具


def ledger(*, generation=7, target="win-shell", consumed_by="", holder=None):
    latest = {
        "generation": generation,
        "target_shell": target,
        "consumed_by": consumed_by,
        "pushed_by": "nas-shell",
    }
    return {
        "current_epoch": 8,
        "lease": {"holder": holder, "state": "held" if holder else "no-lease"},
        "latest_generation": latest,
    }


ME = "win-shell"


def settings(**kw):
    base = dict(
        execute=True,
        shell=ME,
        data_dir=r"G:\molt-fish\data",
        molt_pkg=str(ROOT),
        python="py",
        boot_script=str(ROOT / "tools" / "boot-keepalive.py"),
        astrbot_python="astrbot-python",
        log_path=r"G:\molt-fish\logs\astrbot.log",
        platform_switch=str(ROOT / "tools" / "platform-switch.py"),
        warden_url="http://192.168.1.10:6186",
        warden_token="t0ken",
        port=6199,
        interval=0.01,
        timeout=1.0,
    )
    base.update(kw)
    return agent.Settings(**base)


class FakeRunner:
    def __init__(self, returncode=0, stdout="", stderr="", raises=None):
        self.calls = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.raises = raises

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


# ---------------------------------------------------------------- decide


def test_idle_when_ledger_has_no_generation():
    d = agent.decide({}, shell=ME, state={})
    assert d.kind == "idle"
    assert "还没有任何一代" in d.reason


def test_idle_when_generation_is_for_someone_else():
    d = agent.decide(ledger(target="nas-fish"), shell=ME, state={})
    assert d.kind == "idle"
    assert "nas-fish" in d.reason


def test_idle_when_i_already_consumed_it():
    d = agent.decide(ledger(consumed_by=ME), shell=ME, state={})
    assert d.kind == "idle"
    assert "已经落过" in d.reason


def test_idle_when_someone_else_consumed_it():
    d = agent.decide(ledger(consumed_by="other-box"), shell=ME, state={})
    assert d.kind == "idle"
    assert "other-box" in d.reason


def test_wait_when_lease_is_still_held():
    d = agent.decide(ledger(holder="nas-shell"), shell=ME, state={})
    assert d.kind == "wait"
    assert d.generation == 7
    assert "租约" in d.reason


def test_land_when_it_is_mine_and_nobody_holds_the_lease():
    d = agent.decide(ledger(), shell=ME, state={})
    assert d.kind == "land"
    assert d.generation == 7


def test_error_when_shell_is_blank():
    d = agent.decide(ledger(), shell="", state={})
    assert d.kind == "error"
    assert "不知道自己是谁" in d.reason


def test_error_when_generation_is_not_an_integer():
    bad = ledger()
    bad["latest_generation"]["generation"] = "第七代"
    d = agent.decide(bad, shell=ME, state={})
    assert d.kind == "error"
    assert "不是整数" in d.reason


def test_pending_start_debt_wins_over_a_newer_generation():
    """上一轮落位成功但没起起来，这笔债要先还。

    否则账本会显示 consumed_by=我（一切正常），而鱼其实没在说话。
    """
    state = {"needs_start": True, "landed_generation": 6}
    d = agent.decide(ledger(generation=9, consumed_by=""), shell=ME, state=state)
    assert d.kind == "start"
    assert d.generation == 6


def test_lease_held_but_for_me_is_still_a_wait():
    """哪怕持租约的是我自己那次迁徙，租约没还也不许落位。"""
    d = agent.decide(ledger(holder=ME), shell=ME, state={})
    assert d.kind == "wait"


# ---------------------------------------------------------------- 配置


def test_settings_defaults_are_harmless(tmp_path):
    cfg = agent.Settings.load(tmp_path / "nope.json")
    assert cfg.execute is False
    assert cfg.shell == ""
    assert cfg.problems()


def test_settings_reads_bom_json(tmp_path):
    path = tmp_path / "a.json"
    path.write_text("\ufeff" + json.dumps({"execute": True, "shell": ME}), encoding="utf-8")
    cfg = agent.Settings.load(path)
    assert cfg.execute is True
    assert cfg.shell == ME


def test_settings_rejects_broken_json_with_position(tmp_path):
    path = tmp_path / "a.json"
    path.write_text('{"execute": true,,}', encoding="utf-8")
    with pytest.raises(agent.ConfigError) as info:
        agent.Settings.load(path)
    assert "行" in str(info.value) and "列" in str(info.value)


def test_settings_rejects_non_object(tmp_path):
    path = tmp_path / "a.json"
    path.write_text("[1,2,3]", encoding="utf-8")
    with pytest.raises(agent.ConfigError):
        agent.Settings.load(path)


def test_settings_ignores_unknown_keys(tmp_path):
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"shell": ME, "拼错的键": 1}), encoding="utf-8")
    assert agent.Settings.load(path).shell == ME


def test_problems_lists_every_reason():
    problems = agent.Settings().problems()
    assert any("execute 是 false" in p for p in problems)
    assert any("shell" in p for p in problems)
    assert any("warden_url" in p for p in problems)
    assert any("warden_token" in p for p in problems)


def test_problems_flags_missing_molt_pkg(tmp_path):
    cfg = settings(molt_pkg=str(tmp_path / "not-here"))
    assert any("molt_pkg 不存在" in p for p in cfg.problems())


# ---------------------------------------------------------------- argv


def test_land_argv_is_explicit():
    argv = agent.Agent(settings=settings(), state_path=Path("x")).land_argv(7)
    for pair in (
        ("--generation", "7"),
        ("--data", r"G:\molt-fish\data"),
        ("--warden-url", "http://192.168.1.10:6186"),
        ("--warden-token", "t0ken"),
        ("--shell", ME),
    ):
        i = argv.index(pair[0])
        assert argv[i + 1] == pair[1]
    assert "--yes" in argv
    assert "--no-color" in argv


def test_boot_argv_passes_data_dir_port_and_log():
    argv = agent.Agent(settings=settings(), state_path=Path("x")).boot_argv()
    assert argv[0] == "astrbot-python"
    assert argv[1].endswith("boot-keepalive.py")
    assert argv[2] == r"G:\molt-fish\data"
    assert argv[3] == "6199"
    assert argv[4] == r"G:\molt-fish\logs\astrbot.log"


# ---------------------------------------------------------------- 执行


def _agent(tmp_path, **kw):
    runner = kw.pop("runner", FakeRunner())
    cfg = kw.pop("cfg", settings())
    probe = kw.pop("port_probe", lambda: False)
    spawned = kw.pop("spawned", [])
    logs = []

    def spawner(argv, log_path):
        spawned.append((list(argv), log_path))
        return 4242

    a = agent.Agent(
        settings=cfg,
        state_path=tmp_path / "state.json",
        runner=runner,
        port_probe=probe,
        spawner=spawner,
        log=logs.append,
        **kw,
    )
    return a, runner, spawned, logs


def test_dry_run_touches_nothing(tmp_path):
    runner = FakeRunner()
    a, runner, spawned, logs = _agent(
        tmp_path, runner=runner, cfg=settings(execute=False), status_fetcher=lambda: ledger()
    )
    decision, steps = a.run_once()
    assert decision.kind == "land"
    assert steps == []
    assert runner.calls == []
    assert spawned == []
    assert any("dry-run" in line for line in logs)


def test_status_failure_means_do_nothing(tmp_path):
    def boom():
        raise RuntimeError("连不上账本")

    runner = FakeRunner()
    a, runner, spawned, logs = _agent(tmp_path, runner=runner, status_fetcher=boom)
    decision, steps = a.run_once()
    assert decision.kind == "error"
    assert steps == []
    assert runner.calls == []


def test_happy_path_lands_then_switches_then_boots(tmp_path):
    runner = FakeRunner()
    a, runner, spawned, logs = _agent(tmp_path, runner=runner, status_fetcher=lambda: ledger())
    decision, steps = a.run_once()

    assert decision.kind == "start"
    assert steps and all(s.ok for s in steps)
    assert len(runner.calls) == 3  # land + platform-switch + announce
    assert "land" in runner.calls[0][0]
    assert "platform-switch.py" in " ".join(runner.calls[1][0])
    assert "announce" in runner.calls[2][0]
    assert len(spawned) == 1

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["landed_generation"] == 7
    assert state["needs_start"] is False
    assert "started_at" in state


def test_announce_tells_it_which_generation_moved_and_from_where(tmp_path):
    """通知的文案由 argv 决定，所以这里逐项钉住 —— 说错了它就会记错。"""
    runner = FakeRunner()
    a, runner, _, _ = _agent(tmp_path, runner=runner, status_fetcher=lambda: ledger())
    a.run_once()

    argv = runner.calls[-1][0]
    assert "announce" in argv
    assert "--generation" in argv and argv[argv.index("--generation") + 1] == "7"
    assert argv[argv.index("--dst") + 1] == a.settings.shell
    assert "--dry-run" not in argv
    assert argv[argv.index("--config") + 1].endswith("cmd_config.json")


def test_announce_failure_does_not_fail_the_handover(tmp_path):
    """通知没送到只意味着"这次它没记住"，交接已经成功了 —— 不能被它拖成失败。"""
    class AnnounceFails(FakeRunner):
        def __call__(self, argv, **kwargs):
            call = super().__call__(argv, **kwargs)
            if "announce" in argv:
                call.returncode = 2
                call.stdout = "读不到控制凭据"
            return call

    runner = AnnounceFails()
    a, runner, spawned, logs = _agent(tmp_path, runner=runner, status_fetcher=lambda: ledger())
    decision, steps = a.run_once()

    # 交接本身推到了 start 并且起了进程
    assert decision.kind == "start"
    assert len(spawned) == 1
    # 最后一步是通知，它失败了；但它是"失败的一步"，不是"失败的交接"
    assert steps[-1].ok is False
    assert "退出码 2" in steps[-1].detail


def test_announce_can_be_switched_off(tmp_path):
    runner = FakeRunner()
    a, runner, _, _ = _agent(tmp_path, runner=runner, status_fetcher=lambda: ledger())
    a.settings.announce = False
    a.run_once()

    assert not any("announce" in argv for argv, _ in runner.calls)


def test_land_failure_does_not_advance_state(tmp_path):
    runner = FakeRunner(returncode=4, stdout="取快照失败")
    a, _, spawned, logs = _agent(tmp_path, runner=runner, status_fetcher=lambda: ledger())
    decision, steps = a.run_once()

    assert decision.kind == "land"
    land = steps[-1]
    assert land.ok is False
    assert "退出码 4" in land.detail
    assert spawned == []
    assert a.load_state() == {}


def test_switch_failure_does_not_boot(tmp_path):
    class TwoStep(FakeRunner):
        def __call__(self, argv, **kwargs):
            self.calls.append((list(argv), kwargs))
            rc = 0 if "land" in argv else 1
            return subprocess.CompletedProcess(argv, rc, "", "平台开关炸了")

    runner = TwoStep()
    a, _, spawned, logs = _agent(tmp_path, runner=runner, status_fetcher=lambda: ledger())
    _, steps = a.run_once()

    assert [s.ok for s in steps] == [True, True, True, False]  # stop/checkpoint/land/switch
    assert spawned == []
    # 落位成功了，所以要记住"还欠一次启动"，否则鱼永远不开口
    assert a.load_state()["needs_start"] is True


def test_does_not_start_a_second_bot_on_the_same_machine(tmp_path):
    runner = FakeRunner()
    a, _, spawned, logs = _agent(
        tmp_path,
        runner=runner,
        status_fetcher=lambda: ledger(consumed_by=ME),
        port_probe=lambda: True,
    )
    decision, steps = a.run_once()
    assert decision.kind == "idle"
    assert spawned == []


def test_boot_is_skipped_when_port_is_already_listening(tmp_path):
    (tmp_path / "state.json").write_text(
        json.dumps({"landed_generation": 6, "needs_start": True}), encoding="utf-8"
    )
    a, runner, spawned, _ = _agent(
        tmp_path, port_probe=lambda: True, status_fetcher=lambda: ledger(generation=9)
    )
    decision, steps = a.run_once()
    assert decision.kind == "start"
    assert spawned == []
    assert any("已经在跑" in s.detail for s in steps if s.name == "boot")
    # 端口已经在听 = 它已经在说话，这笔债算还了
    assert a.load_state()["needs_start"] is False


def test_timeout_is_reported_not_raised(tmp_path):
    runner = FakeRunner(raises=subprocess.TimeoutExpired(cmd="x", timeout=600))
    a, _, spawned, _ = _agent(tmp_path, runner=runner, status_fetcher=lambda: ledger())
    _, steps = a.run_once()
    land = steps[-1]
    assert land.ok is False
    assert "超时" in land.detail
    assert spawned == []


def test_spawn_failure_is_reported(tmp_path):
    def bad_spawn(argv, log_path):
        raise OSError("没有这个可执行文件")

    a = agent.Agent(
        settings=settings(),
        state_path=tmp_path / "state.json",
        runner=FakeRunner(),
        port_probe=lambda: False,
        spawner=bad_spawn,
        log=lambda m: None,
        status_fetcher=lambda: ledger(),
    )
    _, steps = a.run_once()
    boot = [s for s in steps if s.name == "boot"][0]
    assert boot.ok is False
    assert "没有这个可执行文件" in boot.detail


def test_state_survives_corrupt_file(tmp_path):
    (tmp_path / "state.json").write_text("{不是 json", encoding="utf-8")
    a, _, _, _ = _agent(tmp_path, status_fetcher=lambda: ledger())
    assert a.load_state() == {}


def test_run_forever_once_exits_after_one_round(tmp_path):
    a, _, _, _ = _agent(tmp_path, status_fetcher=lambda: ledger(target="someone-else"))
    assert a.run_forever(once=True) == 0


# ---------------------------------------------------------------- bug #15


class _GbkStream:
    """一个只能打 ASCII 的假 stdout —— 模拟 Windows 的 GBK 控制台。"""

    encoding = "gbk"

    def __init__(self):
        self.written = []

    def write(self, text):
        # 真的按 gbk 编一次：编不了就抛，和真控制台一模一样
        text.encode("gbk")
        self.written.append(text)

    def flush(self):
        pass


def test_log_line_does_not_raise_on_gbk_console(monkeypatch):
    """一行日志打不出来，绝不该杀死一个正在搬家的进程。

    2026-11-14 真机上就是这一行把 agent 打死了（`\u2713 落位 gen-7`），
    而它当时刚好处在「已经落位、还没启动」的中间态 —— 险些让鱼两边都不在。
    """
    fake = _GbkStream()
    monkeypatch.setattr(agent.sys, "stdout", fake)

    agent._log_line("  \u2713 落位 gen-7")  # 不抛就算过


def test_log_line_still_writes_plain_ascii(monkeypatch):
    fake = _GbkStream()
    monkeypatch.setattr(agent.sys, "stdout", fake)

    agent._log_line("landed gen-7")
    assert any("landed gen-7" in w for w in fake.written)


def test_setup_console_tolerates_streams_without_reconfigure(monkeypatch):
    """`_setup_console()` 碰到的流可能没有 reconfigure（比如被重定向成文件对象）。"""

    class _Bare:
        def write(self, text):
            pass

        def flush(self):
            pass

    monkeypatch.setattr(agent.sys, "stdout", _Bare())
    monkeypatch.setattr(agent.sys, "stderr", _Bare())
    agent._setup_console()  # 不抛即通过


# ------------------------------------------------------- boot_extra（顺手要起来的）

def test_extra_boots_run_after_astrbot_boots(tmp_path):
    """AstrBot 先开口，然后才轮到"人干活要用的东西"。

    用户的要求就是这条链：鱼搬过来 → 它开口 → DSH 起来 → 人（或 agent）能干活。
    顺序反了的话，DSH 起来时鱼还没说话，人就先看到桌面而收不到消息。
    """
    order = []
    cfg = settings(execute=True, boot_extra=[{"argv": ["DSH.exe"], "skip_if": ""}])
    a, _r, _sp, _logs = _agent(tmp_path, cfg=cfg, status_fetcher=lambda: ledger(target=settings().shell))
    a.do_switch_on = lambda: (order.append("switch"), agent.StepResult("switch", True, ""))[1]
    a.do_boot = lambda: (order.append("boot"), agent.StepResult("boot", True, "ok"))[1]
    a.spawner = lambda argv, log: (order.append("extra:" + argv[0]), 1234)[1]
    a.run_once()
    assert order == ["switch", "boot", "extra:DSH.exe"]


def test_extra_boot_is_skipped_when_the_process_is_already_running(tmp_path, monkeypatch):
    """已经在跑就不重复开 —— 否则每次轮询都会多一个窗口。"""
    monkeypatch.setattr(agent, '_process_running', lambda name: True)
    calls = []
    cfg = settings(execute=True, boot_extra=[{"argv": ["DSH.exe"], "skip_if": "DSH.exe"}])
    a, _r, _sp, _logs = _agent(tmp_path, cfg=cfg, status_fetcher=lambda: ledger(target=settings().shell))
    a.do_switch_on = lambda: agent.StepResult("switch", True, "")
    a.do_boot = lambda: agent.StepResult("boot", True, "ok")
    a.spawner = lambda argv, log: (calls.append(argv), 1)[1]
    a.run_once()
    assert calls == []


def test_extra_boot_failure_does_not_fail_the_handoff(tmp_path):
    """DSH 起不来，不该让"迁徙成功"变成失败 —— 鱼已经在说话了。"""
    cfg = settings(execute=True, boot_extra=[{"argv": ["nope.exe"], "skip_if": ""}])
    a, _r, _sp, _logs = _agent(tmp_path, cfg=cfg, status_fetcher=lambda: ledger(target=settings().shell))
    a.do_switch_on = lambda: agent.StepResult("switch", True, "")
    a.do_boot = lambda: agent.StepResult("boot", True, "ok")

    def boom(argv, log):
        raise OSError("no such file")

    a.spawner = boom
    decision, steps = a.run_once()
    assert decision.kind == "start"
    assert any(not s.ok for s in steps if s.name == "extra")
    assert a.load_state().get("needs_start") is False


# ------------------------------------------------------- 落位前先停本机

def test_stop_happens_before_landing(tmp_path):
    """先停本机，再落位 —— 和 NAS 侧 homer 对称，这一步谁都不能省。

    2026-11-15 01:10 真机：NAS 已经把第 9 代推上来了，Windows 上的壳还在跑，
    `land` 因为目标目录"还活着"（-shm）拒绝落位，agent 每 10 秒失败一次。
    """
    order = []
    a, _r, spawned, _logs = _agent(tmp_path, status_fetcher=lambda: ledger(target=settings().shell))
    a.pid_probe = lambda: (order.append("probe"), 4321)[1]
    a.stopper = lambda pid: (order.append("stop:%d" % pid), True)[1]
    a.do_land = lambda gen: (order.append("land"), agent.StepResult("land", True, ""))[1]
    a.do_switch_on = lambda: agent.StepResult("switch", True, "")
    a.do_boot = lambda: agent.StepResult("boot", True, "")
    a.run_once()
    assert order == ["probe", "stop:4321", "land"]


def test_stop_failure_aborts_landing(tmp_path):
    """停不下来就别落位 —— 落在一个还在写的数据目录上，两边各说各话。"""
    order = []
    a, _r, spawned, _logs = _agent(tmp_path, status_fetcher=lambda: ledger(target=settings().shell))
    a.pid_probe = lambda: 4321
    a.stopper = lambda pid: False
    a.do_land = lambda gen: (order.append("land"), agent.StepResult("land", True, ""))[1]
    decision, steps = a.run_once()
    assert order == []
    assert any(not s.ok and s.name == "stop" for s in steps)


def test_stop_is_a_noop_when_nothing_is_running(tmp_path):
    order = []
    a, _r, spawned, _logs = _agent(tmp_path, status_fetcher=lambda: ledger(target=settings().shell))
    a.pid_probe = lambda: None
    a.stopper = lambda pid: (order.append("stop"), True)[1]
    a.do_land = lambda gen: (order.append("land"), agent.StepResult("land", True, ""))[1]
    a.do_switch_on = lambda: agent.StepResult("switch", True, "")
    a.do_boot = lambda: agent.StepResult("boot", True, "")
    a.run_once()
    assert order == ["land"]


def _recorder(calls):
    class _P:
        returncode = 0
        stderr = ""
    def _run(argv, **kw):
        calls.append(list(argv))
        return _P()
    return _run


def test_shutdown_after_leave_fires_when_generation_goes_elsewhere(tmp_path):
    cfg = settings(shutdown_after_leave=True, shutdown_delay_seconds=120, execute=True, power_action="shutdown")
    # 直接用 Agent 本体
    mod = sys.modules["molt_agent_under_test"]
    calls = []
    st = {"latest_generation": {"generation": 11, "target_shell": "nas-shell",
          "consumed_by": None, "pushed_by": "win-shell"}, "lease": None}
    agent = mod.Agent(settings=cfg, state_path=tmp_path / "s.json",
                      status_fetcher=lambda: st,
                      runner=_recorder(calls), port_probe=lambda port: False,
                      spawner=lambda argv: 1, log=lambda m: None)
    decision, steps = agent.run_once()
    assert [s.name for s in steps] == ["shutdown"]
    assert steps[0].ok is True
    assert calls and calls[-1][:3] == ["shutdown", "/s", "/t"]
    assert agent.load_state().get("shut_down_for") == 11


def test_shutdown_fires_against_the_real_ledger_lease_shape(tmp_path):
    """账本"没有租约"时给的是一个**非空** dict，不是 `null`。

    真机账本返回的是 `{"holder": null, "state": "no-lease"}`。第一版判据写成
    `and not lease`，它对这个形状永远是假 —— 于是关机钩子在生产环境从来没触发过，
    而单测因为夹具写的是 `"lease": None` 一直是绿的（2026-11-15 01:41 真机抓到）。
    """
    mod = sys.modules["molt_agent_under_test"]
    calls = []
    cfg = settings(shutdown_after_leave=True, shutdown_delay_seconds=30,
                   execute=True, power_action="shutdown")
    st = {
        "latest_generation": {"generation": 12, "target_shell": "nas-shell",
                              "consumed_by": "", "pushed_by": "win-shell"},
        "lease": {"holder": None, "state": "no-lease"},
    }
    agent = mod.Agent(settings=cfg, state_path=tmp_path / "s.json",
                      status_fetcher=lambda: st,
                      runner=_recorder(calls), port_probe=lambda port: False,
                      spawner=lambda argv: 1, log=lambda m: None)
    _decision, steps = agent.run_once()
    assert [s.name for s in steps] == ["shutdown"]
    assert calls and calls[-1][:4] == ["shutdown", "/s", "/t", "30"]


def test_shutdown_does_not_fire_while_someone_holds_a_lease(tmp_path):
    """有人持着租约时不关机：那一代正在飞，等它落定再决定。"""
    mod = sys.modules["molt_agent_under_test"]
    calls = []
    cfg = settings(shutdown_after_leave=True, execute=True, power_action="shutdown")
    st = {
        "latest_generation": {"generation": 12, "target_shell": "nas-shell",
                              "consumed_by": "", "pushed_by": "win-shell"},
        "lease": {"holder": "nas-shell", "state": "held"},
    }
    agent = mod.Agent(settings=cfg, state_path=tmp_path / "s.json",
                      status_fetcher=lambda: st,
                      runner=_recorder(calls), port_probe=lambda port: False,
                      spawner=lambda argv: 1, log=lambda m: None)
    _decision, steps = agent.run_once()
    assert steps == []
    assert calls == []


def test_shutdown_after_leave_does_not_fire_for_my_own_generation(tmp_path):
    mod = sys.modules["molt_agent_under_test"]
    calls = []
    cfg = settings(shutdown_after_leave=True)
    st = {"latest_generation": {"generation": 11, "target_shell": "win-shell",
          "consumed_by": "win-shell", "pushed_by": "nas-shell"}, "lease": None}
    agent = mod.Agent(settings=cfg, state_path=tmp_path / "s.json",
                      status_fetcher=lambda: st,
                      runner=_recorder(calls), port_probe=lambda port: False,
                      spawner=lambda argv: 1, log=lambda m: None)
    agent.run_once()
    assert not [c for c in calls if c and c[0] == "shutdown"]


def test_leave_sleeps_instead_of_shutdown_when_power_action_is_sleep(tmp_path, monkeypatch):
    """睡眠这条路不需要开机密码 —— 会话不销毁，醒来一切照旧。"""
    mod = sys.modules["molt_agent_under_test"]
    monkeypatch.setattr(mod, "_away_mode_enabled", lambda: False)
    monkeypatch.setattr(mod, "_suspend_now", lambda: (True, "已挂起"))
    calls = []
    cfg = settings(shutdown_after_leave=True, execute=True, power_action="sleep")
    st = {"latest_generation": {"generation": 12, "target_shell": "nas-shell",
          "consumed_by": None, "pushed_by": "win-shell"}, "lease": None}
    agent = mod.Agent(settings=cfg, state_path=tmp_path / "s.json",
                      status_fetcher=lambda: st,
                      runner=_recorder(calls), port_probe=lambda port: False,
                      spawner=lambda argv: 1, log=lambda m: None)
    _d, steps = agent.run_once()
    assert [s.name for s in steps] == ["shutdown"]
    assert steps[0].ok is True
    assert "睡着了" in steps[0].detail
    assert not [c for c in calls if c and c[0] == "shutdown"]


def test_away_mode_is_refused_and_explained(tmp_path, monkeypatch):
    """开着离开模式时，API 会返回成功但机器根本没睡 —— 不能假装成功。"""
    mod = sys.modules["molt_agent_under_test"]
    monkeypatch.setattr(mod, "_away_mode_enabled", lambda: True)
    cfg = settings(shutdown_after_leave=True, execute=True, power_action="sleep")
    st = {"latest_generation": {"generation": 14, "target_shell": "nas-shell",
          "consumed_by": None, "pushed_by": "win-shell"}, "lease": None}
    agent = mod.Agent(settings=cfg, state_path=tmp_path / "s.json",
                      status_fetcher=lambda: st, runner=_recorder([]),
                      port_probe=lambda port: False, spawner=lambda argv: 1,
                      log=lambda m: None)
    _d, steps = agent.run_once()
    assert steps and steps[-1].ok is False
    assert "离开模式" in steps[-1].detail
    assert "molt-sleep-task.ps1" in steps[-1].detail


def test_suspend_failure_is_reported(tmp_path, monkeypatch):
    mod = sys.modules["molt_agent_under_test"]
    monkeypatch.setattr(mod, "_away_mode_enabled", lambda: False)
    monkeypatch.setattr(mod, "_suspend_now", lambda: (False, "SetSuspendState 返回 False（5）"))
    cfg = settings(shutdown_after_leave=True, execute=True, power_action="sleep")
    st = {"latest_generation": {"generation": 15, "target_shell": "nas-shell",
          "consumed_by": None, "pushed_by": "win-shell"}, "lease": None}
    agent = mod.Agent(settings=cfg, state_path=tmp_path / "s.json",
                      status_fetcher=lambda: st, runner=_recorder([]),
                      port_probe=lambda port: False, spawner=lambda argv: 1,
                      log=lambda m: None)
    _d, steps = agent.run_once()
    assert steps[-1].ok is False
    assert "SetSuspendState" in steps[-1].detail


def test_unknown_power_action_is_refused_not_guessed(tmp_path):
    mod = sys.modules["molt_agent_under_test"]
    calls = []
    cfg = settings(shutdown_after_leave=True, execute=True, power_action="reboot")
    st = {"latest_generation": {"generation": 13, "target_shell": "nas-shell",
          "consumed_by": None, "pushed_by": "win-shell"}, "lease": None}
    agent = mod.Agent(settings=cfg, state_path=tmp_path / "s.json",
                      status_fetcher=lambda: st,
                      runner=_recorder(calls), port_probe=lambda port: False,
                      spawner=lambda argv: 1, log=lambda m: None)
    _d, steps = agent.run_once()
    assert steps and steps[-1].ok is False
    assert "只认 sleep / shutdown" in steps[-1].detail
    assert not [c for c in calls if c and c[0] in ("shutdown", "rundll32.exe", "schtasks")]




# ------------------------------------------------- 崩溃残留的 sidecar

def test_clean_sidecars_removes_shm_and_empty_wal(tmp_path):
    """断电留下的 `-shm` / 零字节 `-wal` 必须清掉，否则 land 的活目录护栏会一直拒绝。

    2026-11-15 01:48 真机卡了两分钟：机器重启过、AstrBot 一个进程都没有，
    但断电前那份 `-shm`（32768 B）还在，于是 agent 每 10 秒失败一次。
    """
    data = tmp_path / "data"
    (data / "knowledge_base").mkdir(parents=True)
    (data / "plugin_data" / "p").mkdir(parents=True)

    (data / "data_v4.db").write_bytes(b"db")
    (data / "data_v4.db-shm").write_bytes(b"\x00" * 64)
    (data / "data_v4.db-wal").write_bytes(b"")

    (data / "knowledge_base" / "kb.db").write_bytes(b"db")
    (data / "knowledge_base" / "kb.db-shm").write_bytes(b"\x00" * 64)

    # 非空 WAL 里有已提交事务，不能当垃圾删
    (data / "plugin_data" / "p" / "x.db").write_bytes(b"db")
    (data / "plugin_data" / "p" / "x.db-wal").write_bytes(b"\x01" * 32)

    a, _r, _s, _l = _agent(tmp_path, cfg=settings(data_dir=str(data)), status_fetcher=lambda: ledger())
    cleaned = a.do_clean_sidecars()

    assert "data_v4.db-shm" in cleaned
    assert "data_v4.db-wal" in cleaned
    assert "kb.db-shm" in cleaned
    assert not (data / "data_v4.db-shm").exists()
    assert not (data / "knowledge_base" / "kb.db-shm").exists()
    # 非空 WAL 原地保留
    assert (data / "plugin_data" / "p" / "x.db-wal").exists()
    assert "x.db-wal" not in cleaned


def test_clean_sidecars_never_touches_backup_dirs(tmp_path):
    """`.molt-pre-restore-*` 里是上一次落位前的原件，不是这次要清的垃圾。"""
    data = tmp_path / "data"
    backup = data / ".molt-pre-restore-20261115T011506"
    backup.mkdir(parents=True)
    (backup / "memory_companion.db-shm").write_bytes(b"\x00" * 64)

    a, _r, _s, _l = _agent(tmp_path, cfg=settings(data_dir=str(data)), status_fetcher=lambda: ledger())
    assert a.do_clean_sidecars() == []
    assert (backup / "memory_companion.db-shm").exists()
