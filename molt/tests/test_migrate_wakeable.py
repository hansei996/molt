"""`wakeable` 到底参不参与判断 —— 用测试把它钉在"参与"这一边。

## 为什么这几条值得单独一个文件

`docs/DECISIONS.md` 的 D2 写着 `role` / `wakeable` ✅ 参与判断。但在 2026-11-15
之前，`wakeable` 只出现在 `/where` 和 `molt shells` 的输出里 —— 没有任何一行
代码因为它改变过行为。**文档说它参与了，代码说它只是被打印了。**

这种漂移比"功能没做"更糟：读到那句话的人会按它去配置，然后不生效。

## 三种取值，三种行为

    True   —— 我说过这台机器能被魔术封包叫醒
    False  —— 我说过它叫不醒（要人按电源键）
    None   —— 我什么都没说（老行为：给了 --wake-mac 就发）

把 `None` 当成 `False` 的后果是那台机器**永远收不到封包**，而症状看起来
是"叫不醒" —— 于是人去查网卡，问题其实在判断里。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from molt import cli as cli_module
from molt.cli import main
from molt.warden import Warden


def _make_sqlite(path: Path, rows: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY, title TEXT)")
        conn.executemany(
            "INSERT INTO conversations (title) VALUES (?)",
            [(f"第 {i} 条",) for i in range(rows)],
        )
        conn.commit()
    finally:
        conn.close()


def _make_source_data(base: Path) -> Path:
    data = base / "source" / "data"
    _make_sqlite(data / "data_v4.db")
    (data / "cmd_config.json").write_text('{"config_version": 5}', encoding="utf-8-sig")
    return data


class _WakeSpy:
    """替身：`W.wake` 一旦被调用就记一笔，但**不发任何包**。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, mac: str, **kwargs):
        self.calls.append({"mac": mac, **kwargs})
        # 形状要和真的一样：调用方会读 any_sent / rounds_ok / rounds_sent / targets / rounds
        return {
            "any_sent": True,
            "rounds_ok": 1,
            "rounds_sent": 1,
            "targets": ["255.255.255.255"],
            "rounds": [{"results": [{"target": "255.255.255.255", "error": ""}]}],
        }


def _declare(root: Path, **attrs) -> None:
    Warden(root=root).set_shell("shell-b", by="test", **attrs)


def _migrate(root: Path, data: Path, *extra: str) -> int:
    return main(
        [
            "migrate",
            "--data",
            str(data),
            "--shell",
            "shell-a",
            "--root",
            str(root),
            "--target",
            "shell-b",
            "--no-color",
            *extra,
        ]
    )


def test_a_shell_declared_not_wakeable_gets_no_magic_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """账本说叫不醒 —— 就不发。发了只会让人去查网卡。"""
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"
    _declare(root, wakeable=False)

    spy = _WakeSpy()
    monkeypatch.setattr(cli_module.W, "wake", spy)

    rc = _migrate(root, data, "--wake-mac", "D8-BB-C1-11-C3-D2", "--no-wait-wake")

    assert rc == 0, "不发封包不等于不能搬"
    assert spy.calls == [], "（wakeable: false）不该发魔术封包"
    out = capsys.readouterr().out
    assert "不可唤醒" in out
    assert "wakeable: false" in out


def test_an_undeclared_shell_still_gets_the_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """什么都没说 = 老行为。把 None 当成 False 会让老配置突然全部失效。"""
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"
    _declare(root, role="nas")  # 声明过 role，但**没提 wakeable**

    spy = _WakeSpy()
    monkeypatch.setattr(cli_module.W, "wake", spy)

    rc = _migrate(root, data, "--wake-mac", "D8-BB-C1-11-C3-D2", "--no-wait-wake")

    assert rc == 0
    assert len(spy.calls) == 1, "没声明 wakeable 的壳必须照旧收包"


def test_a_wakeable_shell_without_a_mac_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """账本说能叫醒，却没人给 MAC —— 这值得说一句。

    账本里不存 MAC：它是**地址**，不是属性。搬一台机器要的 MAC 只对当下这个
    网络有意义，塞进账本就会成为一条三周后还在、却指向错误网卡的事实。
    """
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"
    _declare(root, wakeable=True)

    spy = _WakeSpy()
    monkeypatch.setattr(cli_module.W, "wake", spy)

    rc = _migrate(root, data)

    assert rc == 0
    assert spy.calls == []
    out = capsys.readouterr().out
    assert "可以被叫醒" in out
    assert "--wake-mac" in out


def test_an_unreadable_ledger_does_not_stop_the_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """读不到账本不该让迁徙失败 —— 那会把"账本抖了一下"变成"它搬不了家"。"""
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"

    spy = _WakeSpy()
    monkeypatch.setattr(cli_module.W, "wake", spy)

    real_open = cli_module._backend

    class _BrokenStatus:
        """只坏 status()，其它照旧 —— 这样失败点被限定在"读声明"这一步。"""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def status(self):
            raise RuntimeError("账本抖了一下")

    monkeypatch.setattr(cli_module, "_backend", lambda args: _BrokenStatus(real_open(args)))

    rc = _migrate(root, data, "--wake-mac", "D8-BB-C1-11-C3-D2", "--no-wait-wake")

    assert rc == 0, "读不到声明就走老行为，不许把整个迁徙带崩"
    assert len(spy.calls) == 1


# ---------------------------------------------------------------------------
# 声明这一侧：能不能把"它叫不醒"写进账本，以及写进去之后看得见吗
# ---------------------------------------------------------------------------


def _declare_cli(root: Path, *flags: str) -> int:
    return main(
        [
            "--no-color",
            "declare",
            "--root",
            str(root),
            "--shell",
            "shell-b",
            *flags,
        ]
    )


def _shells_cli(root: Path, capsys: pytest.CaptureFixture[str]) -> str:
    assert main(["--no-color", "shells", "--root", str(root)]) == 0
    return capsys.readouterr().out


def test_the_cli_can_declare_that_a_shell_cannot_be_woken(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """没有 `--no-wakeable` 的时候，`wakeable: false` 只能靠手改账本。

    发现它的是写文档：SELFUSE 里写着"留着 false"，而 CLI 根本表达不出 false ——
    `--wakeable` 是 store_true，不写就是"没声明"。**文档里写不出来的配置，
    等于那条判断只有测试能碰到。**
    """
    root = tmp_path / "warden"
    assert _declare_cli(root, "--no-wakeable") == 0
    assert Warden(root=root).shells_view()["shell-b"]["wakeable"] is False

    out = _shells_cli(root, capsys)
    assert "不可唤醒" in out, "说了 false 就要看得见，不能印成空白"


def test_the_cli_distinguishes_silence_from_false(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """什么都没说 ≠ 说了叫不醒。这一条守的是"不许把两件事压成一个词"。"""
    root = tmp_path / "warden"
    assert _declare_cli(root, "--role", "desktop") == 0
    assert Warden(root=root).shells_view()["shell-b"]["wakeable"] is None

    out = _shells_cli(root, capsys)
    # "不可唤醒" 里**含有** "可唤醒"，所以这两条断言必须分开写：
    assert "可唤醒" not in out
    assert "不可唤醒" not in out


def test_the_cli_can_declare_that_a_shell_can_be_woken(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    root = tmp_path / "warden"
    assert _declare_cli(root, "--wakeable") == 0
    assert Warden(root=root).shells_view()["shell-b"]["wakeable"] is True

    out = _shells_cli(root, capsys)
    assert "可唤醒" in out
    assert "不可唤醒" not in out

