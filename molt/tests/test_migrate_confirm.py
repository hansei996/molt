"""交出去之后必须确认"另一边真的接走了"，否则把鱼拉回来。

## 为什么这个文件存在

2026-11-15 23:30 那次：源壳 drain 了、快照上传了、也登记了，然后 `migrate`
**什么都不管地退出 0**。目标壳的 agent 卡在"停不下来"，于是两台壳都不在线
——用户看到的是"鱼死了"，日志里一句错都没有。

23:38 那次是它的兄弟问题：判"目标还活着"用的是心跳年龄（≤60 秒），
而机器 23:37:30 关机、23:38:29 时年龄才 59 秒 —— 快照照样被推到一台正在
关机的机器上。

这个文件钉住后半段："交出去"不等于"交接完成"。`consumed_by` 是唯一证据。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from molt.cli import main
from molt.warden import Warden


def _make_sqlite(path: Path, rows: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
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


class FakeControl:
    def __init__(self) -> None:
        self.resumed: List[str] = []

    def where(self) -> str:
        return "http://fake-control"

    def drain_and_verify(self, **_kwargs: Any) -> Dict[str, Any]:
        return {"ok": True, "drained": ["qq"], "still_connected": 0, "verified_silent": True}

    def resume(self, **_kwargs: Any) -> Dict[str, Any]:
        self.resumed.append("qq")
        return {"ok": True, "resumed": ["qq"]}


@pytest.fixture
def control(monkeypatch) -> FakeControl:
    fake = FakeControl()
    monkeypatch.setattr("molt.cli._control_client", lambda args: fake)
    return fake


def _argv(data: Path, root: Path, *extra: str) -> List[str]:
    return [
        "migrate",
        "--skip-target-check",
        "--data",
        str(data),
        "--shell",
        "shell-a",
        "--root",
        str(root),
        "--target",
        "shell-b",
        "--control-url",
        "http://fake-control",
        "--no-color",
        *extra,
    ]


def test_a_handoff_that_is_never_consumed_rolls_back(
    tmp_path: Path, control: FakeControl, capsys: pytest.CaptureFixture[str]
):
    """没人接 → 抢回租约、resume 源壳、退出码 6。**鱼没死**。"""
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"

    rc = main(_argv(data, root, "--confirm-timeout", "0"))

    assert rc == 6
    assert control.resumed == ["qq"], "源壳必须被放回来说话"
    out = capsys.readouterr().out
    assert "把鱼拉回源壳" in out
    assert "鱼没死" in out

    status = Warden(root=root).status()
    assert status["lease"]["holder"] == "shell-a", "租约必须抢回来（fencing）"
    assert status["current_epoch"] >= 2, "抢回来应该是一个新 epoch"


def test_a_handoff_that_is_consumed_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """另一边接走了 → 正常退出，不要回滚。"""
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"
    monkeypatch.setattr("molt.cli._wait_for_consumption", lambda *a, **k: 12.0)
    monkeypatch.setattr("molt.cli._control_client", lambda args: FakeControl())

    rc = main(_argv(data, root, "--confirm-timeout", "5"))

    assert rc == 0
    out = capsys.readouterr().out
    assert "已经接走了" in out


def test_without_a_confirm_window_the_old_behaviour_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`--no-wait-confirm` 是逃生口：给脚本用，也要能被测试覆盖到。"""
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"
    called: List[int] = []
    monkeypatch.setattr(
        "molt.cli._wait_for_consumption",
        lambda *a, **k: called.append(1) or 0.0,
    )
    monkeypatch.setattr("molt.cli._control_client", lambda args: FakeControl())

    rc = main(_argv(data, root, "--no-wait-confirm"))

    assert rc == 0
    assert called == [], "显式关掉确认时不应当去轮询账本"


def test_the_rollback_is_recorded_as_a_new_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """回滚不能只是"再拿一次租约"——必须是 epoch 变大，否则挡不住对方。"""
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"
    monkeypatch.setattr("molt.cli._control_client", lambda args: FakeControl())

    main(_argv(data, root, "--confirm-timeout", "0"))

    status = Warden(root=root).status()
    assert status["current_epoch"] == 2
    assert status["lease"]["holder"] == "shell-a"
