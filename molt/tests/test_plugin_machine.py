"""QQ 里的 `/molt machine`：看 / 叫醒 / 关掉**另一台**机器。

这一层钉的是三件事：

1. **看**：把账本里的壳、心跳、地址念出来，并标出「鱼在这里」。
2. **叫醒**：允许只说壳名（MAC 从账本里查），因为"我要叫醒哪台机器"
   才是人的语言；查不到就明说叫不醒。
3. **关掉**：拒绝关掉鱼自己站着的那台；其余的留一张**会过期**的票。

第 3 条的方向不对称：拦错一次只是"那台还开着"，放错一次是"鱼没了"。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from astrbot_plugin_molt import command as cmd
from astrbot_plugin_molt.command import MoltMover


class _Resp:
    status = 200

    def __init__(self, data: bytes) -> None:
        self._data = data

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._data


class Recorder:
    """答 `/status`，并记下每个 POST 的 (url, body)。"""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload
        self.posts: List[tuple] = []

    def __call__(self, req, timeout=None) -> _Resp:
        url = getattr(req, "full_url", "")
        body = getattr(req, "data", None)
        if url.endswith("/status"):
            return _Resp(json.dumps(self.payload).encode("utf-8"))
        self.posts.append((url, json.loads(body.decode("utf-8")) if body else {}))
        return _Resp(b'{"ok": true}')


STATUS: Dict[str, Any] = {
    "status": {
        "current_epoch": 40,
        "lease": {"holder": None, "state": "no-lease"},
        "heartbeats": {
            "nas-box": {"age_seconds": 4},
            "win-box": {"age_seconds": 600},
        },
        "latest_generation": {
            "generation": 39,
            "pushed_by": "win-box",
            "target_shell": "nas-box",
            "consumed_by": "nas-box",
        },
        "shells": {
            "nas-box": {"role": "nas", "address": "192.168.50.50"},
            "win-box": {"role": "desktop", "wakeable": True, "wake_mac": "AA-BB-CC-DD-EE-02"},
            "nomac-box": {"role": "desktop", "wakeable": True},
        },
    }
}


@pytest.fixture()
def plugin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "plugin"
    d.mkdir()
    (d / "pkg").mkdir()
    cfg = {
        "enabled": True,
        "shell": "nas-box",
        "target": "win-box",
        "data_dir": "/AstrBot/data",
        "molt_pkg": str(d / "pkg"),
        "python": "python3",
        "warden_url": "http://warden:6186",
        "warden_token": "tok",
    }
    (d / cmd.CONFIG_FILENAME).write_text(json.dumps(cfg), encoding="utf-8")
    return d


def _mover(plugin_dir: Path, rec: Recorder) -> MoltMover:
    return MoltMover(plugin_dir, opener=rec)


def test_machine_list_names_every_machine_and_marks_where_the_fish_is(plugin_dir: Path) -> None:
    rec = Recorder(STATUS)
    reply = _mover(plugin_dir, rec).machine(rec_payload_cfg(plugin_dir), "machine")
    assert "nas-box" in reply.text and "win-box" in reply.text
    assert "鱼在这里" in reply.text
    assert "在跑" in reply.text


def test_machine_on_resolves_the_mac_from_the_ledger(plugin_dir: Path) -> None:
    rec = Recorder(STATUS)
    reply = _mover(plugin_dir, rec).machine(rec_payload_cfg(plugin_dir), "machine on win-box")
    assert rec.posts and rec.posts[0][0].endswith("/wake")
    assert rec.posts[0][1] == {"shell": "win-box"}
    assert "魔术封包" in reply.text


def test_machine_on_says_it_cannot_wake_a_shell_without_a_mac(plugin_dir: Path) -> None:
    rec = Recorder(STATUS)
    reply = _mover(plugin_dir, rec).machine(rec_payload_cfg(plugin_dir), "machine on nomac-box")
    assert "MAC" in reply.text
    assert rec.posts == []


def test_machine_off_refuses_to_shut_down_the_machine_the_fish_is_on(plugin_dir: Path) -> None:
    rec = Recorder(STATUS)
    reply = _mover(plugin_dir, rec).machine(rec_payload_cfg(plugin_dir), "machine off nas-box")
    assert "鱼就在" in reply.text
    assert rec.posts == []


def test_machine_off_leaves_an_expiring_ticket_for_another_machine(plugin_dir: Path) -> None:
    rec = Recorder(STATUS)
    reply = _mover(plugin_dir, rec).machine(rec_payload_cfg(plugin_dir), "machine off win-box")
    assert rec.posts and rec.posts[0][0].endswith("/power")
    body = rec.posts[0][1]
    assert body["shell"] == "win-box"
    assert body["action"] == "shutdown"
    assert "15 分钟" in reply.text


def test_machine_is_one_of_the_recognised_verbs() -> None:
    assert cmd.normalize_args("machine on win-box") == "machine"


def rec_payload_cfg(plugin_dir: Path):
    return _mover(plugin_dir, Recorder(STATUS)).settings()
