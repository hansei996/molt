"""v0.4 硬化：守护进程必须在"上次落位没做完"时拒绝启动鱼。

`molt land` 的原子性分两半：

1. 每个文件先写 `.molt-part` 再 `os.replace`（`molt/transfer.py`）
2. 全部成功之后才删掉 data 目录里的 `.molt-restore-incomplete`

第 1 半只保证"不会留下一个看起来正常的半份库"，但**半份的库本身还在**。
所以第 2 半必须有人读：容器一启动、AstrBot 一打开 `data_v4.db`，
半份的库就会在某个查询里报 `database disk image is malformed` ——

    到那时"为什么坏"已经查不动了。

这个文件守的就是"有人读"这件事，外加一条防漂移的常量比对。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AGENT_PATH = ROOT / "tools" / "molt-agent.py"
HOMER_PATH = ROOT / "tools" / "molt-homer.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


agent_mod = _load("molt_agent_guard_under_test", AGENT_PATH)
homer_mod = _load("molt_homer_guard_under_test", HOMER_PATH)


# --------------------------------------------------------------------------
# 常量漂移
# --------------------------------------------------------------------------


def test_both_daemons_agree_with_transfer_on_the_marker_name():
    """两侧各写了一遍这个字符串，必须钉死。

    守护进程不能 `import molt.transfer`（它们是被 `python tools/xxx.py`
    直接跑起来的脚本，`sys.path[0]` 是 tools 目录），所以名字只能复制。
    **复制的东西必须有一条测试盯着**，否则某天只改了一边，标记就再也不被读到 ——
    而那正是"码全都在，只是没人执行"的故障形态。
    """
    from molt.transfer import RESTORE_MARKER_NAME

    assert agent_mod.RESTORE_MARKER_NAME == RESTORE_MARKER_NAME
    assert homer_mod.RESTORE_MARKER_NAME == RESTORE_MARKER_NAME


# --------------------------------------------------------------------------
# agent
# --------------------------------------------------------------------------


def _agent(tmp_path, *, data_dir: Path):
    cfg = agent_mod.Settings(
        data_dir=str(data_dir),
        astrbot_python=str(tmp_path / "python.exe"),
        boot_script=str(tmp_path / "boot.py"),
        log_path=str(tmp_path / "astrbot.log"),
    )
    spawned = []

    def spawner(argv, log_path):
        spawned.append((list(argv), log_path))
        return 4242

    a = agent_mod.Agent(
        settings=cfg,
        state_path=tmp_path / "state.json",
        runner=agent_mod.FakeRunner() if hasattr(agent_mod, "FakeRunner") else None,
        port_probe=lambda: False,
        spawner=spawner,
        log=lambda msg: None,
    )
    return a, spawned


def test_agent_boots_normally_without_a_marker(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    a, spawned = _agent(tmp_path, data_dir=data)

    result = a.do_boot()

    assert result.ok is True
    assert len(spawned) == 1


def test_agent_refuses_to_boot_when_the_last_restore_did_not_finish(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / agent_mod.RESTORE_MARKER_NAME).write_text(
        json.dumps({"generation": 12, "started_at": "2026-11-15 06:00:00"}),
        encoding="utf-8",
    )
    a, spawned = _agent(tmp_path, data_dir=data)

    result = a.do_boot()

    assert result.ok is False
    assert spawned == [], "半份的库面前不许把 AstrBot 拉起来"
    assert "第 12 代" in result.detail
    assert "molt land" in result.detail


def test_agent_refuses_to_boot_when_the_marker_is_unreadable(tmp_path):
    """标记本身写坏了，同样算"没做完"（说明断电正好断在写标记那一刻）。"""
    data = tmp_path / "data"
    data.mkdir()
    (data / agent_mod.RESTORE_MARKER_NAME).write_text("{ half", encoding="utf-8")
    a, spawned = _agent(tmp_path, data_dir=data)

    result = a.do_boot()

    assert result.ok is False
    assert spawned == []


def test_agent_marker_check_reports_the_reason_without_the_file(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    a, _ = _agent(tmp_path, data_dir=data)
    assert a.restore_marker_reason() == ""


# --------------------------------------------------------------------------
# homer
# --------------------------------------------------------------------------


def _homer(tmp_path, *, data_dir: Path, running: bool = False):
    """造一个 Homer，用假 runner 回答 `docker inspect`（端口/容器状态的唯一来源）。

    不能像 agent 那样注入一个 `container_running` 回调 —— homer 的
    `container_running()` 是个真方法，它自己去跑 `docker inspect`。
    """
    cfg = homer_mod.Settings(data_dir=str(data_dir), container="astrbot")
    calls: list = []

    class _Cp:
        def __init__(self, stdout: str, returncode: int = 0):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def runner(argv, timeout=None):
        calls.append(list(argv))
        if argv[:2] == ["docker", "inspect"]:
            return _Cp("true\n" if running else "false\n")
        return _Cp("astrbot\n")

    h = homer_mod.Homer(
        settings=cfg,
        state_path=tmp_path / "state.json",
        runner=runner,
        log=lambda msg: None,
    )
    return h, calls


def test_homer_starts_the_container_normally(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    h, calls = _homer(tmp_path, data_dir=data)

    assert h.do_start() is True
    starts = [c for c in calls if c[:2] == ["docker", "start"]]
    assert starts, calls


def test_homer_refuses_to_start_the_container_after_a_half_restore(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / homer_mod.RESTORE_MARKER_NAME).write_text(
        json.dumps({"generation": 9, "started_at": "2026-11-15 05:00:00"}),
        encoding="utf-8",
    )
    h, calls = _homer(tmp_path, data_dir=data)

    assert h.do_start() is False
    starts = [c for c in calls if c[:2] == ["docker", "start"]]
    assert starts == [], "容器一起来 AstrBot 就会去开那个半份的库"
    assert "第 9 代" in h.restore_marker_reason()


def test_homer_already_running_container_wins_over_the_marker(tmp_path):
    """容器本来就在跑：说明它已经在用那份数据，这时再"拒绝启动"毫无意义。

    这条不是为标记服务，是为"别把一次已在运行的状态误报成阻塞"。
    """
    data = tmp_path / "data"
    data.mkdir()
    (data / homer_mod.RESTORE_MARKER_NAME).write_text("{}", encoding="utf-8")
    h, calls = _homer(tmp_path, data_dir=data, running=True)

    assert h.do_start() is True
    starts = [c for c in calls if c[:2] == ["docker", "start"]]
    assert starts == []
