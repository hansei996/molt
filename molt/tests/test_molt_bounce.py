"""`tools/molt-bounce.py` 的测试。

它是 NAS 侧的往返触发器：鱼回家落地之后，等一会儿再把它推出去。
最需要测的是那个纯函数 `decide()` —— 判错一次，它就会在半夜把对面的机器反复吵醒。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"
MOD_PATH = TOOLS / "molt-bounce.py"


def _load():
    """按文件路径加载（`tools/` 不是包）。

    必须在 `exec_module` **之前** 把模块塞进 `sys.modules`：
    文件头有 `from __future__ import annotations`，dataclass 解析字符串注解时会
    `sys.modules.get(cls.__module__).__dict__`，拿不到就 `AttributeError: 'NoneType'`。
    """
    name = "molt_bounce_tool"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, MOD_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bounce_mod = _load()


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------
def ledger(*, generation=7, target="nas-shell", consumed="nas-shell", holder=None):
    return {
        "latest_generation": {
            "generation": generation,
            "target_shell": target,
            "consumed_by": consumed,
        },
        "lease": {"holder": holder} if holder else None,
    }


def settings(**over):
    base = dict(
        shell="nas-shell",
        data_dir="/data",
        molt_pkg="/pkg",
        python="python3",
        warden_url="http://127.0.0.1:6186",
        warden_token="tok",
        target="win-shell",
        wake_mac="AA-BB-CC-DD-EE-FF",
        wake_host="192.168.1.20",
        after_seconds=0.0,
        interval=10.0,
        state_file="",
    )
    base.update(over)
    return bounce_mod.Settings(**base)


class FakeProc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


class FakeRunner:
    def __init__(self, proc=None, raises=None):
        self.proc = proc or FakeProc()
        self.raises = raises
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.raises:
            raise self.raises
        return self.proc


def make_bounce(tmp_path, *, cfg=None, status=None, runner=None, sleeps=None):
    state_path = tmp_path / "bounce-state.json"
    sleeps = sleeps if sleeps is not None else []
    fetched = status if status is not None else ledger()
    return bounce_mod.Bounce(
        settings=cfg or settings(),
        state_path=state_path,
        runner=runner or FakeRunner(),
        status_fetcher=lambda: fetched,
        sleep=lambda s: sleeps.append(s),
        log=lambda msg: None,
    )


# --------------------------------------------------------------------------
# decide：全分支
# --------------------------------------------------------------------------
def test_shell_missing_is_an_error():
    d = bounce_mod.decide(ledger(), shell="", state={})
    assert d.kind == "error"
    assert "shell" in d.reason


def test_no_generation_yet_is_idle():
    d = bounce_mod.decide({"latest_generation": None, "lease": None}, shell="nas-shell", state={})
    assert d.kind == "idle"


def test_generation_not_an_int_is_an_error():
    st = ledger(generation="seven")
    d = bounce_mod.decide(st, shell="nas-shell", state={})
    assert d.kind == "error"


def test_generation_for_someone_else_is_idle():
    d = bounce_mod.decide(ledger(target="win-shell"), shell="nas-shell", state={})
    assert d.kind == "idle"
    assert "win-shell" in d.reason


def test_generation_consumed_by_someone_else_is_idle():
    d = bounce_mod.decide(ledger(consumed="win-shell"), shell="nas-shell", state={})
    assert d.kind == "idle"


def test_generation_not_yet_consumed_by_me_waits():
    d = bounce_mod.decide(ledger(consumed=""), shell="nas-shell", state={})
    assert d.kind == "wait"
    assert "还没落地" in d.reason


def test_already_bounced_this_generation_is_idle():
    d = bounce_mod.decide(ledger(), shell="nas-shell", state={"bounced_generation": 7})
    assert d.kind == "idle"
    assert "一次" in d.reason


def test_lease_held_by_someone_else_waits():
    d = bounce_mod.decide(ledger(holder="win-shell"), shell="nas-shell", state={})
    assert d.kind == "wait"
    assert "公理 0" in d.reason


def test_landed_and_free_is_a_bounce():
    d = bounce_mod.decide(ledger(), shell="nas-shell", state={})
    assert d.kind == "bounce"
    assert d.generation == 7


# --------------------------------------------------------------------------
# argv
# --------------------------------------------------------------------------
def test_migrate_argv_is_explicit(tmp_path):
    b = make_bounce(tmp_path)
    argv = b.migrate_argv()
    assert argv[:4] == ["python3", "-m", "molt.cli", "migrate"]
    assert argv[argv.index("--data") + 1] == "/data"
    assert argv[argv.index("--shell") + 1] == "nas-shell"
    assert argv[argv.index("--target") + 1] == "win-shell"
    assert argv[argv.index("--warden-url") + 1] == "http://127.0.0.1:6186"
    assert argv[argv.index("--warden-token") + 1] == "tok"
    assert argv[argv.index("--wake-mac") + 1] == "AA-BB-CC-DD-EE-FF"
    assert argv[argv.index("--wake-host") + 1] == "192.168.1.20"
    assert "--no-color" in argv


def test_migrate_argv_omits_wake_flags_when_not_configured(tmp_path):
    b = make_bounce(tmp_path, cfg=settings(wake_mac="", wake_host=""))
    argv = b.migrate_argv()
    assert "--wake-mac" not in argv
    assert "--wake-host" not in argv


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def test_settings_loads_with_bom(tmp_path):
    p = tmp_path / "c.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"shell": "nas-shell", "after_seconds": 5}).encode("utf-8"))
    cfg = bounce_mod.Settings.load(str(p))
    assert cfg.shell == "nas-shell"
    assert cfg.after_seconds == 5.0


def test_settings_missing_file_raises(tmp_path):
    with pytest.raises(bounce_mod.ConfigError):
        bounce_mod.Settings.load(str(tmp_path / "nope.json"))


def test_settings_bad_json_reports_position(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{oops", encoding="utf-8")
    with pytest.raises(bounce_mod.ConfigError) as ei:
        bounce_mod.Settings.load(str(p))
    assert "行" in str(ei.value)


def test_settings_non_object_is_refused(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("[1,2]", encoding="utf-8")
    with pytest.raises(bounce_mod.ConfigError):
        bounce_mod.Settings.load(str(p))


def test_settings_unknown_keys_are_ignored(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"shell": "s", "wat": 1}), encoding="utf-8")
    assert bounce_mod.Settings.load(str(p)).shell == "s"


def test_problems_lists_what_is_missing(tmp_path):
    cfg = settings(shell="", data_dir="/nope-1", molt_pkg="/nope-2", warden_token="", target="")
    problems = cfg.problems()
    joined = "\n".join(problems)
    for needle in ("shell", "warden_token", "target", "data_dir 不存在", "molt_pkg 不存在"):
        assert needle in joined


def test_problems_accepts_a_good_config(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    p = tmp_path / "pkg"
    p.mkdir()
    assert settings(data_dir=str(d), molt_pkg=str(p)).problems() == []


# --------------------------------------------------------------------------
# run_once
# --------------------------------------------------------------------------
def test_run_once_bounces_and_records(tmp_path):
    runner = FakeRunner()
    sleeps = []
    b = make_bounce(tmp_path, runner=runner, sleeps=sleeps)
    decision, notes = b.run_once()
    assert decision.kind == "bounce"
    assert len(runner.calls) == 1
    state = json.loads((tmp_path / "bounce-state.json").read_text(encoding="utf-8"))
    assert state["bounced_generation"] == 7
    assert state["last_ok"] is True
    assert any("推出去" in n for n in notes)


def test_run_once_waits_before_bouncing(tmp_path):
    sleeps = []
    b = make_bounce(tmp_path, cfg=settings(after_seconds=42.0), sleeps=sleeps)
    b.run_once()
    assert sleeps == [42.0]


def test_run_once_does_not_bounce_when_idle(tmp_path):
    runner = FakeRunner()
    b = make_bounce(tmp_path, status=ledger(target="win-shell"), runner=runner)
    decision, _ = b.run_once()
    assert decision.kind == "idle"
    assert runner.calls == []


def test_failed_bounce_is_still_recorded(tmp_path):
    """失败也必须记账 —— 否则每 10 秒重试一次，把对面的机器反复吵醒。"""
    runner = FakeRunner(proc=FakeProc(returncode=1, stdout="boom"))
    b = make_bounce(tmp_path, runner=runner)
    decision, notes = b.run_once()
    assert decision.kind == "bounce"
    state = json.loads((tmp_path / "bounce-state.json").read_text(encoding="utf-8"))
    assert state["bounced_generation"] == 7
    assert state["last_ok"] is False
    assert "退出码 1" in "\n".join(notes)
    # 第二轮不会再试
    decision2, _ = b.run_once()
    assert decision2.kind == "idle"
    assert len(runner.calls) == 1


def test_spawn_failure_is_reported_not_raised(tmp_path):
    runner = FakeRunner(raises=OSError("nope"))
    b = make_bounce(tmp_path, runner=runner)
    _d, notes = b.run_once()
    assert any("起不来" in n for n in notes)


def test_dry_run_touches_nothing(tmp_path):
    runner = FakeRunner()
    sleeps = []
    b = make_bounce(tmp_path, runner=runner, sleeps=sleeps)
    decision, notes = b.run_once(dry_run=True)
    assert decision.kind == "bounce"
    assert runner.calls == []
    assert sleeps == []
    assert not (tmp_path / "bounce-state.json").exists()
    assert any("dry-run" in n for n in notes)


def test_ledger_unreachable_does_nothing(tmp_path):
    runner = FakeRunner()
    b = make_bounce(tmp_path, runner=runner)
    b.status_fetcher = lambda: (_ for _ in ()).throw(RuntimeError("超时"))
    decision, notes = b.run_once()
    assert decision.kind == "wait"
    assert runner.calls == []
    assert any("读不到账本" in n for n in notes)


def test_state_being_corrupt_does_not_kill_it(tmp_path):
    (tmp_path / "bounce-state.json").write_text("{not json", encoding="utf-8")
    b = make_bounce(tmp_path)
    decision, _ = b.run_once()
    assert decision.kind == "bounce"


def test_run_forever_once_returns(tmp_path):
    b = make_bounce(tmp_path)
    b.run_forever(once=True)          # 不抛即过


def test_bounce_passes_pythonpath_and_cwd(tmp_path):
    runner = FakeRunner()
    b = make_bounce(tmp_path, runner=runner)
    b.run_once()
    _argv, kwargs = runner.calls[0]
    assert kwargs["env"]["PYTHONPATH"] == "/pkg"
    assert kwargs["cwd"] == "/pkg"
    assert kwargs["timeout"] == bounce_mod.DEFAULT_TIMEOUT
