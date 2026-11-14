"""`tools/molt-homer.py` 的测试。

这个文件守的是**回家的那半边**：NAS 上必须先把容器停掉，再落位，
再打开平台，再起容器 —— 而且任何一步失败，都不能让鱼"两边都不在"。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
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
    """`land` 的参数必须逐项写死，不能靠默认值。

    尤其 `--warden-url` / `--warden-token`：归档在 Warden 的中转区，不在本地磁盘。
    2026-11-15 00:59 真机第一次跑回家，就是漏了这两个，`land` 退出码 2，
    容器被停下来又拉起来，鱼回到了家却是个哑的。

    还要 `--shell`：`land` 只在同时给了 `--generation` 和 `--shell` 时才登记
    `consumed_by`。漏了它账本永远显示"还没人取走"，NAS 侧的 bounce 就一直 wait
    （2026-11-15 01:45 真机抓到）。
    """
    h = make_homer(tmp_path)
    assert h.land_argv(8) == [
        "python3", "-m", "molt.cli", "land",
        "--generation", "8",
        "--data", "/data",
        "--yes", "--no-color",
        "--shell", "nas-shell",
        "--warden-url", "http://127.0.0.1:6186",
        "--warden-token", "tok",
    ]


def test_land_argv_omits_warden_flags_when_not_configured(tmp_path):
    """没配 Warden 时不塞空参数：`--warden-url ""` 会让 land 以为要走远端。"""
    h = make_homer(tmp_path, cfg=settings(warden_url="", warden_token=""))
    assert h.land_argv(3) == [
        "python3", "-m", "molt.cli", "land",
        "--generation", "3",
        "--data", "/data",
        "--yes", "--no-color",
        "--shell", "nas-shell",
    ]


def test_land_argv_refuses_to_omit_shell_when_shell_is_empty(tmp_path):
    """`shell` 没配时宁可不传，也不传一个空串。

    空 `--shell ""` 会让 `land` 觉得"这只壳报了名"，然后拿空字符串去认领这一代。
    """
    h = make_homer(tmp_path, cfg=settings(shell=""))
    assert "--shell" not in h.land_argv(3)


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
            # 只记 land 那一刻的 state：后面的 announce 会把 needs_start 清掉，
            # 那不是这条测试想证的事。
            if "land" in argv and "molt.cli" in " ".join(argv):
                state_file = tmp_path / homer_mod.STATE_FILENAME
                if state_file.is_file():
                    seen["state"] = json.loads(state_file.read_text(encoding="utf-8"))
            return super().__call__(argv, **kwargs)

    h = make_homer(tmp_path, status=ledger(), runner=Spy(running_after_stop=False))
    h.run_once()

    assert seen.get("state", {}).get("needs_start") is True, \
        "落位开始的时候，needs_start 必须已经在磁盘上了"


# ---------------------------------------------------------------- WAL checkpoint


def _make_wal_db(path: Path):
    """造一个"主库是旧的、WAL 里躺着已提交事务"的现场，并返回那个活着的写者。"""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    return conn


def test_checkpoint_merges_wal_into_the_main_file(tmp_path):
    """WAL 里留着已提交事务时，checkpoint 必须把它合回主库。

    真机 2026-11-15 01:05：`memory_companion.db-wal` 有 20,632 字节，
    容器明明已经优雅停机，`land` 却因为"看到非空 -wal"拒绝落位 —— 鱼回家卡在这一步。
    """
    data = tmp_path / "data"
    data.mkdir()
    db = data / "x.db"
    writer = _make_wal_db(db)
    try:
        wal = data / "x.db-wal"
        assert wal.exists() and wal.stat().st_size > 0, "造出来的现场得真的有 WAL"
        report = homer_mod.checkpoint_databases(str(data))
        assert len(report) == 1
        name, before, after = report[0]
        assert name == "x.db"
        assert before > 0
        assert after == 0
        assert (wal.stat().st_size if wal.exists() else 0) == 0
    finally:
        writer.close()


def test_checkpoint_skips_databases_without_wal(tmp_path):
    """没有 WAL 的库不该被碰（也不必出现在报告里）。"""
    data = tmp_path / "data"
    data.mkdir()
    conn = sqlite3.connect(str(data / "y.db"))
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.commit()
    conn.close()
    assert homer_mod.checkpoint_databases(str(data)) == []


def test_checkpoint_happens_after_stop_and_before_land(tmp_path, monkeypatch):
    """顺序：停容器 → 合并 WAL → 落位。

    少了中间这一步，`land` 一定被自己的护栏挡住；
    而如果放在停容器之前，就等于"在有人写的时候合并"，那才是危险的。
    """
    order = []

    def fake_checkpoint(data_dir):
        order.append("checkpoint")
        return []

    monkeypatch.setattr(homer_mod, "checkpoint_databases", fake_checkpoint)
    h = make_homer(tmp_path, status=ledger(target=SHELL), state={})
    h.do_stop = lambda: order.append("stop") or True
    h.do_land = lambda gen: order.append("land") or True
    h.do_switch_on = lambda: order.append("switch") or True
    h.do_start = lambda: order.append("start") or True
    result = h.run_once()
    assert result["ok"] is True
    assert order == ["stop", "checkpoint", "land", "switch", "start"]


def test_checkpoint_failure_restarts_container_and_skips_landing(tmp_path, monkeypatch):
    """WAL 合不干净时不许落位，但必须把容器拉起来 —— 别让它两边都不在。"""
    order = []
    monkeypatch.setattr(homer_mod, "checkpoint_databases",
                        lambda data_dir: [("x.db", 100, 40)])
    h = make_homer(tmp_path, status=ledger(target=SHELL), state={})
    h.do_stop = lambda: order.append("stop") or True
    h.do_land = lambda gen: order.append("land") or True
    h.do_start = lambda: order.append("start") or True
    result = h.run_once()
    assert result["ok"] is False
    assert "WAL" in result["reason"]
    assert order == ["stop", "start"]


def test_land_argv_carries_this_shells_dashboard(tmp_path):
    """dashboard 的地址必须由**目标壳**说，不能跟着快照走。

    2026-11-15 01:02 真机：从 Windows 回家的快照带着 `127.0.0.1:6199`，
    落进容器之后 WebUI 谁都连不上 —— 容器里监听 loopback，映射出去的端口
    后面什么都没有。鱼活着、在 QQ 上说话，但管理页面死了。
    """
    h = make_homer(tmp_path, cfg=settings(dashboard_host="0.0.0.0", dashboard_port=6185))
    argv = h.land_argv(8)
    assert "--dashboard-host" in argv and "0.0.0.0" in argv
    assert "--dashboard-port" in argv and "6185" in argv


def test_land_argv_omits_dashboard_when_not_configured(tmp_path):
    h = make_homer(tmp_path, cfg=settings(dashboard_host="", dashboard_port=0))
    argv = h.land_argv(8)
    assert "--dashboard-host" not in argv
    assert "--dashboard-port" not in argv


# ------------------------------------------------- 鱼被别人接走之后，我要闭嘴

def test_should_stand_down_only_when_other_side_really_took_it(tmp_path):
    a = make_homer(tmp_path)
    # 目标不是我，但对方还没取 —— 还不能死
    assert a.should_stand_down(ledger(generation=11, target="win-shell", consumed_by="")) is False
    assert a.should_stand_down(ledger(generation=11, target="win-shell", consumed_by="win-shell")) is True
    # 是我的就别停
    assert a.should_stand_down(ledger(generation=11, target=SHELL, consumed_by=SHELL)) is False


def test_idle_stands_down_and_stops_the_container(tmp_path):
    """2026-11-15 真机事故：gen-11 被 Windows 接走，NAS 容器还在说话。"""
    runner = FakeRunner(running_at_first=True, running_after_stop=False)
    a = make_homer(tmp_path, runner=runner, cfg=settings(execute=True),
                   status=ledger(generation=11, target="win-shell", consumed_by="win-shell"))
    result = a.run_once()
    assert result["ok"] is True
    assert result.get("stood_down") is True
    assert any(c[:2] == ["docker", "kill"] for c in runner.calls)


def test_idle_does_not_stop_when_generation_is_mine(tmp_path):
    runner = FakeRunner(running_at_first=True)
    a = make_homer(tmp_path, runner=runner, cfg=settings(execute=True),
                   state={"landed_generation": 11},
                   status=ledger(generation=11, target=SHELL, consumed_by=SHELL))
    result = a.run_once()
    assert result.get("stood_down") is None
    assert not any(c[:2] == ["docker", "kill"] for c in runner.calls)


# ---------------------------------------------------------------- 迁徙通知


def test_announce_tells_it_which_generation_moved(tmp_path):
    """起来之后要告诉它「你搬家了」—— 否则这次迁徙永远不进它的记忆。

    真机实测（2026-11-15 02:34）：memories 84 条里提到迁徙的那些，
    **全是"主人说过的关于搬家的话"，没有一条是"我被搬了"**。
    """
    runner = FakeRunner()
    a = make_homer(tmp_path, runner=runner, cfg=settings(execute=True), status=ledger())
    a.run_once()

    announce = [c for c in runner.calls if "announce" in c]
    assert len(announce) == 1, "落位并且起容器之后应该投一条通知"
    argv = announce[0]
    assert argv[argv.index("--generation") + 1] == "7"
    assert argv[argv.index("--dst") + 1] == SHELL
    assert argv[argv.index("--config") + 1].endswith("cmd_config.json")


def test_announce_happens_only_after_the_container_starts(tmp_path):
    """容器没起来就没有人能收到这条消息 —— 顺序错了等于白发。"""
    runner = FakeRunner()
    a = make_homer(tmp_path, runner=runner, cfg=settings(execute=True), status=ledger())
    a.run_once()

    verbs = []
    for call in runner.calls:
        if call[:1] == ["docker"] and len(call) > 1:
            verbs.append("docker-" + call[1])
        elif "land" in call:
            verbs.append("land")
        elif "platform-switch" in " ".join(call):
            verbs.append("switch")
        elif "announce" in call:
            verbs.append("announce")
    assert verbs.index("docker-start") < verbs.index("announce")


def test_announce_failure_does_not_fail_the_handover(tmp_path):
    """通知没送到只意味着"这次它没记住"，交接已经成功了。"""
    class AnnounceFails(FakeRunner):
        def __call__(self, argv, **kwargs):
            proc = super().__call__(argv, **kwargs)
            if "announce" in argv:
                proc.returncode = 2
                proc.stdout = "读不到控制凭据"
            return proc

    runner = AnnounceFails()
    a = make_homer(tmp_path, runner=runner, cfg=settings(execute=True), status=ledger())
    result = a.run_once()
    assert result["ok"] is True


def test_announce_can_be_switched_off(tmp_path):
    runner = FakeRunner()
    a = make_homer(tmp_path, runner=runner, cfg=settings(execute=True, announce=False),
                   status=ledger())
    a.run_once()
    assert not any("announce" in c for c in runner.calls)