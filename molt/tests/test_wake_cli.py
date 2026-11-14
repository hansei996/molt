"""
test_wake_cli.py -- `molt wake` 与 `migrate --wake-mac` 的接缝测试。

这组测试真正想固定住的是**两条顺序约束**，而不是输出长什么样：

1. `migrate --wake-mac` 必须**先叫醒、再拿租约**。
   租约 TTL 只有 90 秒，而叫醒一台 S5 的机器实测要 23.1 秒（网络恢复）
   到几十秒（服务监听）。反过来做的话，那 90 秒里有一大截花在等 BIOS POST 上。

2. **叫不醒就不许继续**。否则账本里会出现一条"已交接"的记录，
   而目标机永远不会开机 —— 这是最难靠人发现的那类状态：
   账本说灵魂在 B 手上，B 从没打开过。
"""

import time

import pytest

from molt.cli import build_parser, main
from molt.errors import Fatal, NeedsHuman
from molt.warden import Warden


def _args(argv):
    return build_parser().parse_args(argv)


# ── parser ────────────────────────────────────────────────────────────


def test_wake_subcommand_is_registered():
    a = _args(["wake", "--mac", "aa:bb:cc:11:c3:d2"])
    assert a.func.__name__ == "cmd_wake"
    assert a.mac == "aa:bb:cc:11:c3:d2"
    assert a.count > 0
    assert a.prefix == 24


def test_migrate_has_wake_options():
    a = _args(
        [
            "migrate", "--data", ".", "--shell", "s",
            "--wake-mac", "aa:bb:cc:11:c3:d2", "--wake-host", "192.168.1.20",
        ]
    )
    assert a.wake_mac == "aa:bb:cc:11:c3:d2"
    assert a.wake_host == "192.168.1.20"
    assert a.wake_prefix == 24
    assert a.wake_timeout == 180.0
    assert a.no_wait_wake is False


def test_wake_options_are_absent_by_default():
    a = _args(["migrate", "--data", ".", "--shell", "s"])
    assert a.wake_mac is None
    assert a.no_wait_wake is False


# ── molt wake ─────────────────────────────────────────────────────────


def test_wake_no_wait_returns_zero(capsys):
    """发完就走：不该等，也不该报错。用 127.0.0.1 保证发包路径通。"""
    t0 = time.monotonic()
    rc = main(
        ["wake", "--mac", "aa:bb:cc:11:c3:d2", "--host", "127.0.0.1",
         "--count", "1", "--interval", "0", "--no-color"]
    )
    elapsed = time.monotonic() - t0
    assert rc == 0
    assert elapsed < 3.0, f"没给 --wait 却等了 {elapsed:.1f} 秒"
    out = capsys.readouterr().out
    assert "已发出" in out
    assert "没等它醒" in out


def test_wake_wait_times_out_with_exit_5(capsys):
    """叫不醒 → exit 5，且消息里必须告诉人去看什么。"""
    rc = main(
        ["wake", "--mac", "aa:bb:cc:11:c3:d2", "--host", "127.0.0.1",
         "--count", "1", "--interval", "0",
         "--wait-port", "1", "--wait-timeout", "0.6", "--wait-interval", "0.2",
         "--no-color"]
    )
    assert rc == 5
    out = capsys.readouterr().out
    assert "唤醒白名单" in out or "没能连上" in out


def test_wake_bad_mac_exits_4(capsys):
    """
    MAC 错必须在发包之前失败，且退出码区分于"叫不醒"：
    4 = 我这边有问题（参数/环境），5 = 目标机叫不醒。
    这两种故障要人做的事完全不同。
    """
    rc = main(["wake", "--mac", "nope", "--host", "127.0.0.1", "--no-color"])
    assert rc == 4
    assert "MAC" in capsys.readouterr().out


def test_wake_quiet_mode_still_reports(capsys):
    rc = main(
        ["wake", "--mac", "aa:bb:cc:11:c3:d2", "--host", "127.0.0.1",
         "--count", "2", "--interval", "0", "--quiet", "--no-color"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "轮次" in out


# ── migrate --wake-mac 的顺序约束 ────────────────────────────────────


@pytest.fixture
def data_dir(tmp_path):
    """一个最小的 data 目录：两个真 SQLite 库 + 一份配置。"""
    import json
    import sqlite3

    d = tmp_path / "data"
    d.mkdir()
    for name, rows in (("data_v4.db", 2), ("memory_companion.db", 1)):
        p = d / name if name == "data_v4.db" else (
            d / "plugin_data" / "astrbot_plugin_memory_companion" / name
        )
        p.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(p))
        con.execute("PRAGMA journal_mode=wal")
        con.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY, title TEXT)")
        for i in range(rows):
            con.execute("INSERT INTO conversations (title) VALUES (?)", (f"t{i}",))
        con.commit()
        con.close()
    (d / "cmd_config.json").write_text(
        json.dumps({"config_version": 5}), encoding="utf-8-sig"
    )
    return d


def test_migrate_wakes_before_acquiring_lease(tmp_path, data_dir, monkeypatch):
    """
    **本块最重要的一条。** 用打桩记录调用顺序：
    叫醒（wake + wait_online）必须全部发生在 acquire 之前。
    """
    order = []

    import molt.cli as CLI

    def fake_wake(*a, **kw):
        order.append("wake")
        return {"any_sent": True, "rounds_ok": 1, "rounds_sent": 1,
                "targets": ["192.168.1.20"], "rounds": [{"results": []}]}

    def fake_wait(*a, **kw):
        order.append("wait")
        return {"seconds": 0.1, "criterion": "icmp", "attempts": 1, "online": True}

    monkeypatch.setattr(CLI.W, "wake", fake_wake)
    monkeypatch.setattr(CLI.W, "wait_online", fake_wait)

    real_acquire = Warden.acquire

    def spy_acquire(self, *a, **kw):
        order.append("acquire")
        return real_acquire(self, *a, **kw)

    monkeypatch.setattr(Warden, "acquire", spy_acquire)

    root = tmp_path / "w"
    rc = main(
        [
            "migrate", "--data", str(data_dir), "--shell", "shell-a",
            "--root", str(root),
            "--wake-mac", "aa:bb:cc:11:c3:d2", "--wake-host", "192.168.1.20",
            "--no-color",
        ]
    )
    assert rc == 0, "应当成功"
    assert order == ["wake", "wait", "acquire"], f"顺序不对：{order}"


def test_migrate_refuses_to_continue_when_wake_fails(tmp_path, data_dir, monkeypatch):
    """
    叫不醒 → exit 5，且**租约一次都不许碰**。

    这是公理 0 的延伸：账本里不该出现一条指向一台没开机的机器的记录。
    """
    import molt.cli as CLI

    monkeypatch.setattr(
        CLI.W, "wake",
        lambda *a, **kw: {"any_sent": True, "rounds_ok": 1, "rounds_sent": 1,
                          "targets": ["192.168.1.20"], "rounds": [{"results": []}]},
    )

    def boom(*a, **kw):
        raise NeedsHuman("模拟：叫不醒")

    monkeypatch.setattr(CLI.W, "wait_online", boom)

    root = tmp_path / "w"
    rc = main(
        [
            "migrate", "--data", str(data_dir), "--shell", "shell-a",
            "--root", str(root),
            "--wake-mac", "aa:bb:cc:11:c3:d2", "--wake-host", "192.168.1.20",
            "--no-color",
        ]
    )
    assert rc == 5
    # 账本应当根本没被创建（或创建了但没有任何租约/记录）
    if (root / "state.json").exists():
        w = Warden(root=root)
        assert w.state.lease is None
        assert w.generations == []


def test_migrate_wake_send_failure_exits_4(tmp_path, data_dir, monkeypatch):
    """一个封包都发不出去 = 本机环境问题，退出码 4（区别于 5 = 目标叫不醒）。"""
    import molt.cli as CLI

    monkeypatch.setattr(
        CLI.W, "wake",
        lambda *a, **kw: {"any_sent": False, "rounds_ok": 0, "rounds_sent": 1,
                          "targets": ["192.168.1.20"],
                          "rounds": [{"results": [{"target": "192.168.1.20",
                                                   "ok": False, "error": "Network unreachable"}]}]},
    )
    root = tmp_path / "w"
    rc = main(
        [
            "migrate", "--data", str(data_dir), "--shell", "shell-a",
            "--root", str(root),
            "--wake-mac", "aa:bb:cc:11:c3:d2", "--wake-host", "192.168.1.20",
            "--no-color",
        ]
    )
    assert rc == 4


def test_migrate_no_wait_wake_skips_verification(tmp_path, data_dir, monkeypatch, capsys):
    """`--no-wait-wake` 只发包不确认。这条必须能跑通（有的人就是知道自己关了机）。"""
    import molt.cli as CLI

    monkeypatch.setattr(
        CLI.W, "wake",
        lambda *a, **kw: {"any_sent": True, "rounds_ok": 1, "rounds_sent": 1,
                          "targets": ["192.168.1.20"], "rounds": [{"results": []}]},
    )
    called = []
    monkeypatch.setattr(CLI.W, "wait_online", lambda *a, **kw: called.append(1))

    root = tmp_path / "w"
    rc = main(
        [
            "migrate", "--data", str(data_dir), "--shell", "shell-a",
            "--root", str(root),
            "--wake-mac", "aa:bb:cc:11:c3:d2", "--wake-host", "192.168.1.20",
            "--no-wait-wake", "--no-color",
        ]
    )
    assert rc == 0
    assert called == [], "给了 --no-wait-wake 就不该调 wait_online"


def test_migrate_without_wake_options_unchanged(tmp_path, data_dir):
    """不给 --wake-mac 时行为与以前完全一致（不叫醒、直接干活）。"""
    root = tmp_path / "w"
    rc = main(
        ["migrate", "--data", str(data_dir), "--shell", "shell-a",
         "--root", str(root), "--no-color"]
    )
    assert rc == 0
    w = Warden(root=root)
    assert len(w.generations) == 1
    assert w.generations[0].pushed_by == "shell-a"
