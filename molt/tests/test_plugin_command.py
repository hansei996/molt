"""聊天指令层：`/molt go` → 提议 → `/molt confirm` → 真的搬。

这一层是整个项目里**唯一一处"鱼可以因为一句话而离开"**的地方，
所以测试的重心不是"功能能不能用"，而是"它在什么情况下会拒绝"。
每条"不搬"的分支都必须有测试，因为它们的失败方向是不对称的：
功能坏了 = 搬不动（人再敲一次），护栏坏了 = 它在你没盯着的时候搬了。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from astrbot_plugin_molt import command as cmd
from astrbot_plugin_molt.command import (
    ConfigError,
    MoltMover,
    Plan,
    Settings,
    build_plan,
    normalize_args,
)
from astrbot_plugin_molt.main import command_args


# --------------------------------------------------------------------------- 夹具


GOOD_CFG = {
    "enabled": True,
    "shell": "nas-shell",
    "target": "win-shell",
    "data_dir": "/AstrBot/data",
    "molt_pkg": "",  # 由夹具填成真实存在的目录
    "python": "python3",
    "warden_url": "http://172.17.0.1:6186",
    "warden_token": "tok",
    "control_url": "http://127.0.0.1:6185",
    "control_config": "/AstrBot/data/cmd_config.json",
    "confirm_ttl_seconds": 120,
}


@pytest.fixture
def pkg_dir(tmp_path: Path) -> Path:
    """一个"存在"的 molt 源码目录。`problems()` 会真的 `is_dir()` 检查它。"""
    d = tmp_path / "pkg"
    d.mkdir()
    return d


@pytest.fixture
def plugin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "plugin"
    d.mkdir()
    return d


def write_cfg(plugin_dir: Path, **overrides) -> Path:
    cfg = dict(GOOD_CFG)
    cfg.update(overrides)
    path = plugin_dir / cmd.CONFIG_FILENAME
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return path


class FakeClock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeLedger:
    """假的 Warden：只回答 `GET /status`。"""

    def __init__(self, payload=None, *, boom: bool = False):
        self.payload = payload if payload is not None else {"status": {"current_epoch": 8}}
        self.boom = boom
        self.calls: list = []

    def __call__(self, request, timeout=None):
        self.calls.append((request.full_url, timeout))

        if self.boom:

            class _Boom:
                def __enter__(self):
                    raise OSError("no route to host")

                def __exit__(self, *exc):
                    return False

            return _Boom()

        body = json.dumps(self.payload).encode("utf-8")

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return body

        return _Resp()


def mover(plugin_dir: Path, *, clock=None, runner=None, opener=None) -> MoltMover:
    return MoltMover(
        plugin_dir,
        clock=clock or FakeClock(),
        runner=runner or (lambda *a, **k: _Ok()),
        opener=opener or FakeLedger(),
    )


class _Ok:
    returncode = 0
    stdout = "✓ 已交还租约"
    stderr = ""


# ------------------------------------------------------------------ 参数归一化


@pytest.mark.parametrize(
    "raw,expect",
    [
        ("go", "go"),
        ("GO", "go"),
        ("回家", "go"),
        ("  go  ", "go"),
        ("go 吧", "go"),  # 只认第一个词，后面的忽略
        ("confirm", "confirm"),
        ("确认", "confirm"),
        ("取消", "cancel"),
        ("status", "status"),
        ("在哪", "status"),
        ("help", "help"),
        ("", ""),
        ("随便说点什么", ""),
        ("我不想回家", ""),  # 整词匹配：这句话不是指令
        ("gohome", "go"),
    ],
)
def test_normalize_args(raw, expect):
    assert normalize_args(raw) == expect


def test_normalize_args_does_not_substring_match():
    """故意的：包含匹配会让"我不想回家"变成"回家"。

    搬家这件事上，宁可让人多敲一个词，也不要猜。
    """
    assert normalize_args("我不想回家") == ""
    assert normalize_args("别 go 了") == ""


def test_command_args_strips_prefix_and_command():
    assert command_args("/molt go") == "go"
    assert command_args("！molt  confirm ") == "confirm"
    assert command_args("/molt") == ""
    assert command_args("molt回家") == "回家"
    assert command_args("") == ""


def test_command_args_tolerates_other_wake_prefixes():
    for prefix in ("/", "!", "！", "。", "."):
        assert command_args(f"{prefix}molt status") == "status"


# --------------------------------------------------------------------- 配置加载


def test_missing_config_means_disabled(plugin_dir):
    cfg = Settings.load(plugin_dir / cmd.CONFIG_FILENAME)
    assert cfg.enabled is False


def test_config_tolerates_bom(plugin_dir):
    path = plugin_dir / cmd.CONFIG_FILENAME
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"enabled": True}).encode("utf-8"))
    assert Settings.load(path).enabled is True


def test_config_bad_json_raises(plugin_dir):
    path = plugin_dir / cmd.CONFIG_FILENAME
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        Settings.load(path)


def test_config_non_object_raises(plugin_dir):
    path = plugin_dir / cmd.CONFIG_FILENAME
    path.write_text("[1,2,3]", encoding="utf-8")
    with pytest.raises(ConfigError):
        Settings.load(path)


def test_config_ignores_unknown_keys(plugin_dir):
    path = plugin_dir / cmd.CONFIG_FILENAME
    path.write_text(json.dumps({"enabled": True, "谁认识的字段": 1}), encoding="utf-8")
    assert Settings.load(path).enabled is True


def test_problems_lists_everything_missing(pkg_dir):
    cfg = Settings.load(Path("/definitely/not/here.json"))
    problems = cfg.problems()
    joined = " | ".join(problems)
    assert "shell" in joined
    assert "target" in joined
    assert "warden_token" in joined
    assert "molt_pkg" in joined


def test_problems_reports_missing_pkg_dir(plugin_dir):
    cfg = Settings(molt_pkg="/definitely/not/a/dir", shell="s", target="t", warden_token="x")
    joined = " | ".join(cfg.problems())
    assert "不存在" in joined


def test_problems_clean_config(pkg_dir):
    cfg = Settings(
        shell="s",
        target="t",
        warden_token="x",
        data_dir="/AstrBot/data",
        molt_pkg=str(pkg_dir),
        control_config="/AstrBot/data/cmd_config.json",
    )
    assert cfg.problems() == []


# ------------------------------------------------------------- 三道闸：关着就拒绝


def test_disabled_refuses_go(plugin_dir):
    m = mover(plugin_dir)  # 没有配置文件
    reply = m.handle("go")
    assert "没开" in reply.text
    assert reply.plan is None
    assert m.has_pending() is False


def test_disabled_refuses_confirm(plugin_dir):
    m = mover(plugin_dir)
    assert "没开" in m.handle("confirm").text
    assert m.has_pending() is False


def test_disabled_still_answers_status(plugin_dir):
    m = mover(plugin_dir)
    assert "没开" in m.handle("status").text


def test_incomplete_config_refuses_go(plugin_dir):
    write_cfg(plugin_dir, molt_pkg="/nope")
    m = mover(plugin_dir)
    reply = m.handle("go")
    assert "配置不完整" in reply.text
    assert m.has_pending() is False


def test_lease_held_refuses_go(plugin_dir, pkg_dir):
    write_cfg(plugin_dir, molt_pkg=str(pkg_dir))
    ledger = FakeLedger({"status": {"lease": {"shell_id": "someone-else", "epoch": 9}}})
    m = mover(plugin_dir, opener=ledger)
    reply = m.handle("go")
    assert "someone-else" in reply.text
    assert m.has_pending() is False


# --------------------------------------------------------- 两步确认与时效


def test_go_then_confirm_produces_plan(plugin_dir, pkg_dir):
    write_cfg(plugin_dir, molt_pkg=str(pkg_dir))
    m = mover(plugin_dir)
    first = m.handle("go", sender="u1")
    assert first.plan is None
    assert "confirm" in first.text
    assert m.has_pending() is False  # 提议阶段**绝不**排定执行

    second = m.handle("confirm")
    assert second.plan is not None
    assert "我走了" in second.text
    assert m.has_pending() is True


def test_confirm_without_go_is_refused(plugin_dir, pkg_dir):
    write_cfg(plugin_dir, molt_pkg=str(pkg_dir))
    m = mover(plugin_dir)
    reply = m.handle("confirm")
    assert "没有等我确认" in reply.text
    assert m.has_pending() is False


def test_proposal_expires(plugin_dir, pkg_dir):
    clock = FakeClock()
    write_cfg(plugin_dir, molt_pkg=str(pkg_dir), confirm_ttl_seconds=120)
    m = mover(plugin_dir, clock=clock)
    m.handle("go")
    clock.advance(121)
    reply = m.handle("confirm")
    assert "过期" in reply.text
    assert reply.plan is None


def test_proposal_just_inside_ttl_still_works(plugin_dir, pkg_dir):
    clock = FakeClock()
    write_cfg(plugin_dir, molt_pkg=str(pkg_dir), confirm_ttl_seconds=120)
    m = mover(plugin_dir, clock=clock)
    m.handle("go")
    clock.advance(119)
    assert m.handle("confirm").plan is not None


def test_cancel_clears_proposal(plugin_dir, pkg_dir):
    write_cfg(plugin_dir, molt_pkg=str(pkg_dir))
    m = mover(plugin_dir)
    m.handle("go")
    assert "取消" in m.handle("cancel").text
    assert m.handle("confirm").plan is None


def test_cancel_without_proposal_says_so(plugin_dir, pkg_dir):
    write_cfg(plugin_dir, molt_pkg=str(pkg_dir))
    m = mover(plugin_dir)
    assert "本来就没有" in m.handle("cancel").text


def test_plan_is_consumed_exactly_once(plugin_dir, pkg_dir):
    write_cfg(plugin_dir, molt_pkg=str(pkg_dir))
    m = mover(plugin_dir)
    m.handle("go")
    m.handle("confirm")
    assert m.take_pending() is not None
    assert m.take_pending() is None


def test_second_confirm_after_success_does_not_replan(plugin_dir, pkg_dir):
    write_cfg(plugin_dir, molt_pkg=str(pkg_dir))
    m = mover(plugin_dir)
    m.handle("go")
    m.handle("confirm")
    second = m.handle("confirm")
    assert second.plan is None
    assert "没有等我确认" in second.text


# --------------------------------------------------------------------- 计划构造


def test_build_plan_never_guesses_endpoints(pkg_dir):
    cfg = Settings(
        shell="s",
        target="t",
        data_dir="/AstrBot/data",
        molt_pkg=str(pkg_dir),
        warden_url="http://w:6186",
        warden_token="tok",
        control_url="http://127.0.0.1:6185",
        control_config="/AstrBot/data/cmd_config.json",
    )
    plan = build_plan(cfg)
    assert plan.argv[:4] == ["python3", "-m", "molt.cli", "migrate"]
    # 关键三项必须显式出现：没有 --control-url 就不会先闭嘴
    for flag, value in (
        ("--data", "/AstrBot/data"),
        ("--shell", "s"),
        ("--target", "t"),
        ("--warden-url", "http://w:6186"),
        ("--warden-token", "tok"),
        ("--control-url", "http://127.0.0.1:6185"),
        ("--control-config", "/AstrBot/data/cmd_config.json"),
    ):
        assert flag in plan.argv
        assert plan.argv[plan.argv.index(flag) + 1] == value
    assert "--no-color" in plan.argv
    assert plan.env["PYTHONPATH"] == str(pkg_dir)
    assert plan.cwd == str(pkg_dir)


def test_build_plan_omits_wake_flags_by_default(pkg_dir):
    cfg = Settings(
        shell="s", target="t", molt_pkg=str(pkg_dir), warden_token="x", data_dir="/d"
    )
    assert "--wake-mac" not in build_plan(cfg).argv


def test_build_plan_includes_wake_when_configured(pkg_dir):
    cfg = Settings(
        shell="s",
        target="t",
        molt_pkg=str(pkg_dir),
        warden_token="x",
        data_dir="/d",
        wake_mac="AA-BB-CC-DD-EE-FF",
        wake_host="192.168.1.20",
    )
    argv = build_plan(cfg).argv
    assert argv[argv.index("--wake-mac") + 1] == "AA-BB-CC-DD-EE-FF"
    assert argv[argv.index("--wake-host") + 1] == "192.168.1.20"


# ----------------------------------------------------------------------- 执行


class FakeRunner:
    def __init__(self, returncode=0, stdout="ok", stderr="", raises=None):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.raises = raises
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.raises is not None:
            raise self.raises

        class _P:
            pass

        p = _P()
        p.returncode = self.returncode
        p.stdout = self.stdout
        p.stderr = self.stderr
        return p


def test_run_plan_records_success(plugin_dir, pkg_dir):
    runner = FakeRunner()
    m = mover(plugin_dir, runner=runner)
    plan = Plan(argv=["python3", "-m", "molt.cli", "migrate"], cwd=str(pkg_dir), env={})
    record = m.run_plan(plan)

    assert record["ok"] is True
    assert record["returncode"] == 0
    assert len(runner.calls) == 1
    assert runner.calls[0][0] == plan.argv
    assert runner.calls[0][1]["cwd"] == str(pkg_dir)
    assert runner.calls[0][1]["timeout"] == cmd.MIGRATE_TIMEOUT_SECONDS
    # 遗嘱必须落盘：鱼哑了之后，人回来第一个问题就是"刚才怎么了"
    saved = json.loads((plugin_dir / cmd.RESULT_FILENAME).read_text(encoding="utf-8"))
    assert saved["ok"] is True


def test_run_plan_records_failure(plugin_dir, pkg_dir):
    runner = FakeRunner(returncode=3, stdout="", stderr="LeaseHeld: 还剩 65.3 秒")
    m = mover(plugin_dir, runner=runner)
    plan = Plan(argv=["x"], cwd=str(pkg_dir), env={})
    record = m.run_plan(plan)

    assert record["ok"] is False
    assert record["returncode"] == 3
    assert "65.3" in record["stderr_tail"]


def test_run_plan_records_timeout(plugin_dir, pkg_dir):
    runner = FakeRunner(raises=subprocess.TimeoutExpired(cmd=["x"], timeout=1))
    m = mover(plugin_dir, runner=runner)
    record = m.run_plan(Plan(argv=["x"], cwd=str(pkg_dir), env={}))
    assert record["ok"] is False
    assert "超时" in record["error"]


def test_run_plan_records_missing_executable(plugin_dir, pkg_dir):
    runner = FakeRunner(raises=FileNotFoundError("no python3"))
    m = mover(plugin_dir, runner=runner)
    record = m.run_plan(Plan(argv=["x"], cwd=str(pkg_dir), env={}))
    assert record["ok"] is False
    assert "起不来" in record["error"]


def test_run_plan_survives_unwritable_plugin_dir(pkg_dir):
    """插件目录不可写（只读挂载）时，不该把一次迁徙失败变成一次抛异常。"""
    ghost = Path("/definitely/not/writable")
    m = MoltMover(ghost, runner=FakeRunner())
    record = m.run_plan(Plan(argv=["x"], cwd=str(pkg_dir), env={}))
    assert record["ok"] is True  # 执行本身成功了，只是没落盘


# --------------------------------------------------------------------- 账本读法


def test_ledger_status_parses_status_envelope(plugin_dir, pkg_dir):
    write_cfg(plugin_dir, molt_pkg=str(pkg_dir))
    ledger = FakeLedger({"status": {"current_epoch": 7, "lease": None}})
    m = mover(plugin_dir, opener=ledger)
    st = m.ledger_status()
    assert st == {"current_epoch": 7, "lease": None}
    assert ledger.calls[0][0].endswith("/status")


def test_ledger_status_returns_none_when_unreachable(plugin_dir, pkg_dir):
    write_cfg(plugin_dir, molt_pkg=str(pkg_dir))
    m = mover(plugin_dir, opener=FakeLedger(boom=True))
    assert m.ledger_status() is None


def test_ledger_status_returns_none_on_bad_payload(plugin_dir, pkg_dir):
    write_cfg(plugin_dir, molt_pkg=str(pkg_dir))
    m = mover(plugin_dir, opener=FakeLedger({"nope": 1}))
    assert m.ledger_status() is None


def test_status_text_reports_lease(plugin_dir, pkg_dir):
    write_cfg(plugin_dir, molt_pkg=str(pkg_dir))
    ledger = FakeLedger(
        {"status": {"current_epoch": 9, "lease": {"shell_id": "win", "epoch": 9}}}
    )
    text = mover(plugin_dir, opener=ledger).handle("status").text
    assert "win" in text
    assert "9" in text


def test_status_text_reports_no_lease(plugin_dir, pkg_dir):
    write_cfg(plugin_dir, molt_pkg=str(pkg_dir))
    ledger = FakeLedger({"status": {"current_epoch": 9, "lease": None}})
    text = mover(plugin_dir, opener=ledger).handle("status").text
    assert "没有租约" in text


def test_status_text_when_ledger_unreachable(plugin_dir, pkg_dir):
    write_cfg(plugin_dir, molt_pkg=str(pkg_dir))
    m = mover(plugin_dir, opener=FakeLedger(boom=True))
    assert "问不到账本" in m.handle("status").text


# ------------------------------------------------ 薄壳：main.py 里那两个 handler


class FakeEvent:
    def __init__(self, message: str, sender: str = "0123456789ABCDEF", admin: bool = True):
        self._message = message
        self._sender = sender
        self._admin = admin
        self.sent: list = []

    def get_message_str(self):
        return self._message

    def get_sender_id(self):
        return self._sender

    def is_admin(self):
        return self._admin

    def plain_result(self, text):
        return ("plain", text)


def _plugin_with(tmp_path: Path, **overrides):
    """造一个插件对象，只装聊天指令层（不装控制面 API）。"""
    from astrbot_plugin_molt.main import MoltControlPlugin

    plugin = MoltControlPlugin.__new__(MoltControlPlugin)
    plugin.context = None
    plugin.config = {}
    plugin._drained = []
    plugin._has_drained = False
    plugin._api = None
    plugin._mover = MoltMover(tmp_path, runner=FakeRunner(), opener=FakeLedger())
    return plugin


def _collect(plugin, event):
    """跑一次 `cmd_molt`（async generator），拿到它 yield 出来的东西。

    用 `asyncio.run` 而不是 pytest-asyncio：这个项目**运行时零依赖**，
    测试也不该为了五个用例引进一个插件——"跑测试"要保持"装个 pytest 就行"。
    """

    async def _go():
        return [r async for r in plugin.cmd_molt(event)]

    import asyncio

    return asyncio.run(_go())


def test_cmd_molt_yields_reply(tmp_path, pkg_dir):
    write_cfg(tmp_path, molt_pkg=str(pkg_dir))
    plugin = _plugin_with(tmp_path)
    out = _collect(plugin, FakeEvent("/molt status"))
    assert out and out[0][0] == "plain"
    assert "账本" in out[0][1] or "租约" in out[0][1]


def test_cmd_molt_go_does_not_schedule_run(tmp_path, pkg_dir):
    """提议阶段：绝不能排定执行。这条是公理 0 在聊天入口上的投影。"""
    write_cfg(tmp_path, molt_pkg=str(pkg_dir))
    plugin = _plugin_with(tmp_path)
    _collect(plugin, FakeEvent("/molt go"))
    assert plugin._mover.has_pending() is False


def test_cmd_molt_confirm_schedules_run(tmp_path, pkg_dir):
    write_cfg(tmp_path, molt_pkg=str(pkg_dir))
    plugin = _plugin_with(tmp_path)
    _collect(plugin, FakeEvent("/molt go"))
    _collect(plugin, FakeEvent("/molt confirm"))
    assert plugin._mover.has_pending() is True


def test_after_message_sent_is_a_noop_without_plan(tmp_path, pkg_dir):
    import asyncio

    write_cfg(tmp_path, molt_pkg=str(pkg_dir))
    plugin = _plugin_with(tmp_path)
    asyncio.run(plugin.on_after_message_sent(FakeEvent("随便")))
    assert plugin._mover.has_pending() is False


def test_after_message_sent_does_not_spawn_task_without_plan(tmp_path, pkg_dir, monkeypatch):
    """没有计划时**一个协程都不该被创建**。

    只断言 `has_pending() is False` 是不够的：把 `if plan is None: return`
    删掉之后 `create_task(self._run_plan(None))` 照样会让那条断言为真，
    然后在一个没人看的地方抛 `AttributeError: 'NoneType' object has no attribute 'describe'`。
    变异测试第 11 条就是这么漏过去的——所以这里直接盯住 `create_task` 有没有被调用。
    """
    import asyncio

    spawned: list = []
    real = asyncio.create_task

    def _spy(coro, **kwargs):
        spawned.append(coro)
        coro.close()  # 不让它真的跑，也不留下 "coroutine was never awaited" 警告
        return None

    monkeypatch.setattr(asyncio, "create_task", _spy)
    try:
        write_cfg(tmp_path, molt_pkg=str(pkg_dir))
        plugin = _plugin_with(tmp_path)
        asyncio.run(plugin.on_after_message_sent(FakeEvent("随便")))
    finally:
        monkeypatch.setattr(asyncio, "create_task", real)

    assert spawned == []


def test_after_message_sent_spawns_task_when_plan_ready(tmp_path, pkg_dir, monkeypatch):
    import asyncio

    spawned: list = []
    real = asyncio.create_task

    def _spy(coro, **kwargs):
        spawned.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(asyncio, "create_task", _spy)
    try:
        write_cfg(tmp_path, molt_pkg=str(pkg_dir))
        plugin = _plugin_with(tmp_path)
        _collect(plugin, FakeEvent("/molt go"))
        _collect(plugin, FakeEvent("/molt confirm"))
        assert plugin._mover.has_pending() is True
        asyncio.run(plugin.on_after_message_sent(FakeEvent("（告别）")))
    finally:
        monkeypatch.setattr(asyncio, "create_task", real)

    assert len(spawned) == 1
    # 计划被取走之后就不该再留一份，否则会重复搬
    assert plugin._mover.has_pending() is False


def test_cmd_molt_works_when_mover_missing(tmp_path):
    """`command.py` 没加载成功时，指令要能说人话，而不是抛 AttributeError。"""
    from astrbot_plugin_molt.main import MoltControlPlugin

    plugin = MoltControlPlugin.__new__(MoltControlPlugin)
    plugin._mover = None
    out = _collect(plugin, FakeEvent("/molt go"))
    assert "没装成功" in out[0][1]


def test_decorated_handlers_live_in_main_module():
    """handler 的 `__module__` 必须是插件主模块，否则 AstrBot 绑不到 `self`。

    `star_manager.py:1264` 按 `handler_module_path == metadata.module_path` 匹配，
    再 `functools.partial(raw_handler, 实例)`。放在 `command.py` 里就永远匹配不上，
    而且报错会指向一个不相干的地方。
    """
    from astrbot_plugin_molt.main import MoltControlPlugin

    for name in ("cmd_molt", "on_after_message_sent"):
        fn = getattr(MoltControlPlugin, name)
        assert fn.__module__ == MoltControlPlugin.__module__
