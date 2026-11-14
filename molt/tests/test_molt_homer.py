"""`tools/molt-homer.py` 的测试。

这个文件守的是**回家的那半边**：NAS 上必须先把容器停掉，再落位，
再打开平台，再起容器 —— 而且任何一步失败，都不能让鱼"两边都不在"。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOMER_PATH = ROOT / "tools" / "molt-homer.py"


def _load_homer():
    spec = importlib.util.spec_from_file_location("molt_homer_under_test", HOMER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # 必须先登记进 sys.modules 再 exec：文件里有 `from __future__ import annotations`，
    # `@dataclass` 解析字符串注解时会 `sys.modules.get(cls.__module__).__dict__`。
    sys.modules["molt_homer_under_test"] = module
    spec.loader.exec_module(module)
    return module


homer_mod = _load_homer()


# ---------------------------------------------------------------- 夹具


def ledger(*, generation=7, target="nas-shell", consumed_by="",
           pushed_by="win-shell", holder=None):
    return {
        "current_epoch": 9,
        "lease": {"holder": holder, "state": "held" if holder else "no-lease"},
        "latest_generation": {
            "generation": generation,
            "target_shell": target,
            "consumed_by": consumed_by,
            "pushed_by": pushed_by,
        },
    }


SHELL = "nas-shell"


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    """记录每次调用，按 `plan` 里的前缀返回预设结果。"""

    def __init__(self, running_after_stop=False, land_ok=True, switch_ok=True,
                 start_ok=True, running_at_first=True):
        self.calls = []
        self.running = running_at_first
        self.running_after_stop = running_after_stop
        self.land_ok = land_ok
        self.switch_ok = switch_ok
        self.start_ok = start_ok

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        head = " ".join(argv[:3])
        if argv[:1] == ["docker"] and argv[1] == "inspect":
            return FakeProc(0, "true\n" if self.running else "false\n")
        if argv[:1] == ["docker"] and argv[1] == "kill":
            self.running = self.running_after_stop
            return FakeProc(0, "astrbot\n")
        if argv[:1] == ["docker"] and argv[1] == "start":
            self.running = self.start_ok
            return FakeProc(0 if self.start_ok else 1, "astrbot\n")
        if head.startswith("python3 -m molt.cli"):
            return FakeProc(0 if self.land_ok else 4, "ok" if self.land_ok else "boom")
        if "platform-switch" in " ".join(argv):
            return FakeProc(0 if self.switch_ok else 1, "ok" if self.switch_ok else "boom")
        return FakeProc(0, "")


def settings(**kw):
    base = dict(
        execute=True,
        shell=SHELL,
        container="astrbot",
        data_dir="/data",
        molt_pkg="/pkg",
        python="python3",
        platform_switch="/pkg/tools/platform-switch.py",
        warden_url="http://127.0.0.1:6186",
        warden_token="tok",
        interval=0.01,
        timeout=1.0,
        stop_timeout=1.0,
    )
    base.update(kw)
    return homer_mod.Settings(**base)


def make_homer(tmp_path, *, status=None, state=None, runner=None, cfg=None):
    cfg = cfg or settings()
    if state is not None:
        (tmp_path / homer_mod.STATE_FILENAME).write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8")
    h = homer_mod.Homer(
        settings=cfg,
        state_path=tmp_path / homer_mod.STATE_FILENAME,
        runner=runner or FakeRunner(),
        status_fetcher=(lambda: status) if status is not None else None,
        log=lambda msg: None,
    )
    return h


# ---------------------------------------------------------------- decide


def test_decide_lands_when_it_is_for_me_and_nobody_holds_lease():
    d = homer_mod.decide(ledger(), shell=SHELL, state={})
    assert d.kind == "land"
    assert d.generation == 7


def test_decide_is_idle_when_no_generation():
    assert homer_mod.decide({"latest_generation": None}, shell=SHELL, state={}).kind == "idle"


def test_decide_is_idle_when_target_is_someone_else():
    d = homer_mod.decide(ledger(target="win-shell"), shell=SHELL, state={})
    assert d.kind == "idle"


def test_decide_is_idle_when_already_consumed_by_me():
    d = homer_mod.decide(ledger(consumed_by=SHELL), shell=SHELL, state={})
    assert d.kind == "idle"


def test_decide_waits_when_someone_else_consumed_it():
    d = homer_mod.decide(ledger(consumed_by="other"), shell=SHELL, state={})
    assert d.kind == "wait"


def test_decide_waits_when_lease_is_held():
    d = homer_mod.decide(ledger(holder="win-shell"), shell=SHELL, state={})
    assert d.kind == "wait"


def test_decide_errors_without_shell():
    d = homer_mod.decide(ledger(), shell="", state={})
    assert d.kind == "error"


def test_decide_errors_on_bogus_generation():
    bad = ledger()
    bad["latest_generation"]["generation"] = "seven"
    assert homer_mod.decide(bad, shell=SHELL, state={}).kind == "error"


def test_decide_debt_beats_a_newer_generation():
    """上一轮停下了却没起来 —— 先把鱼救活，晚一代也认。"""
    d = homer_mod.decide(ledger(generation=9), shell=SHELL,
                         state={"needs_start": True, "landed_generation": 8})
    assert d.kind == "land"
    assert d.generation == 8
    assert "没起来" in d.reason


def test_decide_is_idle_when_state_already_matches_but_ledger_has_not_caught_up():
    d = homer_mod.decide(ledger(), shell=SHELL, state={"landed_generation": 7})
    assert d.kind == "idle"


# ---------------------------------------------------------------- argv


def test_land_argv_is_explicit(tmp_path):
    h = make_homer(tmp_path)
    assert h.land_argv(8) == [
        "python3", "-m", "molt.cli", "land",
        "--generation", "8",
        "--data", "/data",
        "--yes", "--no-color",
    ]


def test_switch_argv_points_at_cmd_config(tmp_path):
    h = make_homer(tmp_path)
    assert h.switch_argv() == [
        "python3", "/pkg/tools/platform-switch.py", "on",
        "/data/cmd_config.json", "--yes",
    ]


# ---------------------------------------------------------------- 配置


def test_config_missing_means_all_defaults(tmp_path):
    cfg = homer_mod.Settings.load(tmp_path / "nope.json")
    assert cfg.execute is False


def test_config_reads_bom_and_ignores_unknown_keys(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"execute": true, "shell": "x", "unknown": 1}', encoding="utf-8-sig")
    cfg = homer_mod.Settings.load(p)
    assert cfg.execute is True and cfg.shell == "x"


def test_config_rejects_broken_json_with_position(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{oops", encoding="utf-8")
    with pytest.raises(homer_mod.ConfigError) as ei:
        homer_mod.Settings.load(p)
    assert "第 1 行" in str(ei.value)


def test_problems_lists_everything_missing():
    ps = settings(execute=True, shell="", warden_token="", platform_switch="/nope").problems()
    joined = " | ".join(ps)
    assert "shell" in joined and "warden_token" in joined and "platform_switch" in joined


def test_dry_run_mode_does_not_require_existing_paths():
    """只报告模式下，路径不对不该拦住"看一眼账本"。"""
    ps = settings(execute=False, data_dir="/nope", molt_pkg="/nope2").problems()
    assert not any("data_dir" in p or "molt_pkg" in p for p in ps)


# ---------------------------------------------------------------- 完整流程


def test_happy_path_stops_lands_switches_and_starts(tmp_path):
    runner = FakeRunner(running_at_first=True, running_after_stop=False)
    h = make_homer(tmp_path, status=ledger(), runner=runner)
    result = h.run_once()

    assert result["ok"] is True
    assert result["generation"] == 7
    verbs = [c[1] for c in runner.calls if c[0] == "docker" and len(c) > 1]
    # 顺序：inspect → kill → inspect(轮询) → start
    assert "kill" in verbs and "start" in verbs
    assert verbs.index("kill") < verbs.index("start")

    state = json.loads((tmp_path / homer_mod.STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["landed_generation"] == 7
    assert state["needs_start"] is False


def test_land_happens_after_the_container_is_stopped(tmp_path):
    """落位必须发生在容器停掉之后 —— 否则那个活着的进程会继续往旧 inode 写。"""
    order = []

    class Ordered(FakeRunner):
        def __call__(self, argv, **kwargs):
            label = "docker-" + argv[1] if argv[0] == "docker" else "land"
            if argv[0] != "docker" and "platform-switch" in " ".join(argv):
                label = "switch"
            order.append(label)
            return super().__call__(argv, **kwargs)

    h = make_homer(tmp_path, status=ledger(), runner=Ordered(running_after_stop=False))
    h.run_once()
    assert order.index("docker-kill") < order.index("land") < order.index("docker-start")


def test_dry_run_touches_nothing(tmp_path):
    runner = FakeRunner()
    h = make_homer(tmp_path, status=ledger(), runner=runner, cfg=settings(execute=False))
    result = h.run_once()
    assert result["dry_run"] is True
    assert runner.calls == []


def test_stop_failure_aborts_before_landing(tmp_path):
    """容器停不下来就不许落位 —— 宁可不搬，不能两边各说各话。"""
    class NeverStops(FakeRunner):
        def __call__(self, argv, **kwargs):
            cp = super().__call__(argv, **kwargs)
            if argv[:1] == ["docker"] and argv[1] == "kill":
                self.running = True          # 假装它没停
            return cp

    runner = NeverStops(running_at_first=True, running_after_stop=True)
    h = make_homer(tmp_path, status=ledger(), runner=runner, cfg=settings(stop_timeout=0.05))
    result = h.run_once()
    assert result["ok"] is False
    assert not any("molt.cli" in " ".join(c) for c in runner.calls)


def test_land_failure_still_brings_the_container_back(tmp_path):
    """落位失败 → 先把容器拉起来。鱼不在账本上，至少别让它也不在机器上。"""
    runner = FakeRunner(running_after_stop=False, land_ok=False)
    h = make_homer(tmp_path, status=ledger(), runner=runner)
    result = h.run_once()

    assert result["ok"] is False
    assert any(c[1] == "start" for c in runner.calls if c[0] == "docker")
    state = json.loads((tmp_path / homer_mod.STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["needs_start"] is False


def test_switch_failure_is_reported_but_container_still_starts(tmp_path):
    runner = FakeRunner(running_after_stop=False, switch_ok=False)
    h = make_homer(tmp_path, status=ledger(), runner=runner)
    result = h.run_once()

    assert result["switched"] is False
    assert result["started"] is True
    # 平台没开 = 鱼哑着，但至少起得来；下一轮欠账会补 start，不会补 switch
    assert result["ok"] is False


def test_unreadable_ledger_does_nothing(tmp_path):
    runner = FakeRunner()

    def boom():
        raise RuntimeError("connection refused")

    h = make_homer(tmp_path, status=None, runner=runner)
    h.status_fetcher = boom
    result = h.run_once()
    assert result["ok"] is False
    assert runner.calls == []


def test_needs_start_debt_is_settled_on_the_next_round(tmp_path):
    """上一轮停下了却没起来 → 下一轮第一件事就是把它拉起来。"""
    runner = FakeRunner(running_at_first=False)
    h = make_homer(tmp_path, status=ledger(), runner=runner,
                   state={"landed_generation": 7, "needs_start": True})
    h.run_once()
    assert any(c[1] == "start" for c in runner.calls if c[0] == "docker")


def test_state_file_corruption_does_not_kill_the_agent(tmp_path):
    (tmp_path / homer_mod.STATE_FILENAME).write_text("{not json", encoding="utf-8")
    h = make_homer(tmp_path, status=ledger(), runner=FakeRunner(running_after_stop=False))
    assert h.run_once()["ok"] is True


def test_log_line_never_raises_on_a_narrow_console(monkeypatch):
    """bug #15 的形态：日志打不出来，不该杀死正在搬家的进程。"""

    class Gbk:
        encoding = "gbk"

        def __init__(self):
            self.written = []

        def write(self, text):
            text.encode("gbk")
            self.written.append(text)

        def flush(self):
            pass

    fake = Gbk()
    monkeypatch.setattr(homer_mod.sys, "stdout", fake)
    homer_mod._log_line("  \u2713 落位 gen-7")   # 不抛即过


def test_land_failure_does_not_open_the_platform(tmp_path):
    """落位没成功，就不许把嘴打开。

    否则会出现最难查的一种状态：平台开着、库还是旧的 —— 鱼在说话，
    但说的是上一代的事。开门之前，先确认灵魂真的到了。
    """
    runner = FakeRunner(running_after_stop=False, land_ok=False)
    h = make_homer(tmp_path, status=ledger(), runner=runner)
    h.run_once()

    assert not any("platform-switch" in " ".join(c) for c in runner.calls)


def test_needs_start_is_on_disk_before_landing(tmp_path):
    """「先记账，再冒险」：落位还没开始，欠账就已经写进文件了。

    这一条守的是 2026-11-14 真机上救过一次的那个顺序 ——
    agent 在"已经落位、还没启动"的中间态被一行日志打死，
    活下来靠的就是欠账比动作先落盘。
    """
    seen = {}

    class Spy(FakeRunner):
        def __call__(self, argv, **kwargs):
            if argv[:1] != ["docker"] and "molt.cli" in " ".join(argv):
                state_file = tmp_path / homer_mod.STATE_FILENAME
                if state_file.is_file():
                    seen["state"] = json.loads(state_file.read_text(encoding="utf-8"))
            return super().__call__(argv, **kwargs)

    h = make_homer(tmp_path, status=ledger(), runner=Spy(running_after_stop=False))
    h.run_once()

    assert seen.get("state", {}).get("needs_start") is True, \
        "落位开始的时候，needs_start 必须已经在磁盘上了"
