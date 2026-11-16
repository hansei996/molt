"""守夜人的测试。

判据不是"函数被调用了"，而是**它会不会在正确的时刻喊人、在错误的时刻闭嘴**。
2026-11-16 那次事故里没有一条日志是错的，但它们合起来在说谎；
所以这里的每一条都对应一个"如果它不响，人就要等四小时才知道"的场景。
"""

from __future__ import annotations

import json
import time

import pytest

from molt.daemon import nightwatch as nw


def _container(**kw):
    base = {
        "running": False,
        "status": "exited",
        "exit_code": 0,
        "oom_killed": False,
        "started_at": "2026-11-16T01:34:29Z",
        "finished_at": "2026-11-16T10:45:05Z",
        "restart_count": 0,
    }
    base.update(kw)
    return base


def _ledger(consumed_by="nas-box", generation=36):
    return {
        "generation_count": generation,
        "latest_generation": {
            "number": generation,
            "pushed_by": "desk-box",
            "target_shell": consumed_by,
            "consumed_by": consumed_by,
        },
    }


def _kinds(alerts):
    return [a.kind for a in alerts]


# --------------------------------------------------------------- 解析与观测


def test_inspect_output_is_parsed_into_the_few_fields_we_care_about():
    raw = json.dumps(
        [
            {
                "State": {
                    "Running": False,
                    "Status": "exited",
                    "ExitCode": 137,
                    "OOMKilled": False,
                    "StartedAt": "2026-11-16T01:34:29Z",
                    "FinishedAt": "2026-11-16T10:45:05Z",
                },
                "RestartCount": 0,
            }
        ]
    )
    got = nw.parse_container_state(raw)
    assert got["running"] is False
    assert got["exit_code"] == 137
    assert got["finished_at"] == "2026-11-16T10:45:05Z"


def test_garbage_from_docker_is_unknown_not_false():
    for raw in ("", "not json", "[]", "{}", json.dumps([{"State": "nope"}])):
        assert nw.parse_container_state(raw)["running"] is None


def test_a_missing_container_counts_as_not_running(tmp_path):
    class Done:
        returncode = 1
        stdout = ""
        stderr = "Error: No such object: astrbot"

    got = nw.inspect_container("astrbot", runner=lambda *a, **k: Done())
    assert got["running"] is False


# ------------------------------------------------------------------ 判断


def test_fish_down_when_the_ledger_says_it_lives_here():
    got = nw.decide(
        container=_container(),
        ledger=_ledger("nas-box"),
        flag={"exists": False},
        previous={},
        shell="nas-box",
    )
    assert "fish-down" in _kinds(got)
    assert "容器没在跑" in got[0].message


def test_no_fish_down_when_the_ledger_says_it_lives_elsewhere():
    got = nw.decide(
        container=_container(),
        ledger=_ledger("desk-box"),
        flag={"exists": False},
        previous={},
        shell="nas-box",
    )
    assert "fish-down" not in _kinds(got)


def test_a_non_zero_exit_is_reported_with_its_code():
    got = nw.decide(
        container=_container(exit_code=137),
        ledger=_ledger("desk-box"),
        flag={"exists": False},
        previous={},
        shell="nas-box",
    )
    kinds = _kinds(got)
    assert "container-died" in kinds
    assert "137" in got[kinds.index("container-died")].message


def test_a_clean_exit_is_not_a_crash_report():
    got = nw.decide(
        container=_container(exit_code=0),
        ledger=_ledger("desk-box"),
        flag={"exists": False},
        previous={},
        shell="nas-box",
    )
    assert "container-died" not in _kinds(got)


def test_a_running_container_while_the_ledger_says_elsewhere_is_a_double_warning():
    got = nw.decide(
        container=_container(running=True, status="running"),
        ledger=_ledger("desk-box"),
        flag={"exists": False},
        previous={},
        shell="nas-box",
    )
    assert "possible-double" in _kinds(got)


def test_no_double_warning_when_the_fish_is_here_and_running():
    got = nw.decide(
        container=_container(running=True, status="running"),
        ledger=_ledger("nas-box"),
        flag={"exists": False},
        previous={},
        shell="nas-box",
    )
    assert got == []


def test_the_shutdown_flag_is_reported_when_it_changes():
    got = nw.decide(
        container=_container(running=True, status="running"),
        ledger=_ledger("nas-box"),
        flag={"exists": True, "content": "shutdown", "mtime": 1000.0},
        previous={"flag": {"exists": True, "content": "shutdown", "mtime": 900.0}},
        shell="nas-box",
    )
    assert "shutdown-notice" in _kinds(got)


def test_the_same_flag_does_not_shout_twice():
    got = nw.decide(
        container=_container(running=True, status="running"),
        ledger=_ledger("nas-box"),
        flag={"exists": True, "content": "shutdown", "mtime": 1000.0},
        previous={"flag": {"exists": True, "content": "shutdown", "mtime": 1000.0}},
        shell="nas-box",
    )
    assert "shutdown-notice" not in _kinds(got)


def test_the_first_sighting_of_the_flag_is_just_a_baseline():
    """第一次看到 flag 不该喊：那天它可能已经躺了四小时，喊也没用。"""
    got = nw.decide(
        container=_container(running=True, status="running"),
        ledger=_ledger("nas-box"),
        flag={"exists": True, "content": "shutdown", "mtime": 1000.0},
        previous={},
        shell="nas-box",
    )
    assert "shutdown-notice" not in _kinds(got)


def test_ledger_unreachable_is_reported_once_not_every_round():
    first = nw.decide(
        container=_container(running=True, status="running"),
        ledger=None,
        flag={"exists": False},
        previous={},
        shell="nas-box",
        ledger_error="URLError: timed out",
    )
    assert "ledger-unreachable" in _kinds(first)
    again = nw.decide(
        container=_container(running=True, status="running"),
        ledger=None,
        flag={"exists": False},
        previous={"ledger_ok": False},
        shell="nas-box",
        ledger_error="URLError: timed out",
    )
    assert "ledger-unreachable" not in _kinds(again)


# ------------------------------------------------------------------ 跑一轮


class FakeWarden:
    def __init__(self, status):
        self._status = status
        self.sent = []

    def status(self):
        if isinstance(self._status, Exception):
            raise self._status
        return self._status

    def alert(self, kind, message, *, shell="", detail=None):
        self.sent.append({"kind": kind, "message": message, "shell": shell, "detail": detail})
        return {"kind": kind}


def _settings(tmp_path, **kw):
    base = dict(
        shell="nas-box",
        container="astrbot",
        flag_path=str(tmp_path / "notifyshutdownflag"),
        warden_url="http://127.0.0.1:6186",
        warden_token="t",
        interval_seconds=10.0,
        dedupe_seconds=600.0,
        state_path=str(tmp_path / "state.json"),
        log_path=str(tmp_path / "nightwatch.log"),
        execute=True,
    )
    base.update(kw)
    return nw.Settings(**base)


def test_run_once_posts_the_alert_and_dedupes_the_next_round(tmp_path):
    warden = FakeWarden(_ledger("nas-box"))
    cfg = _settings(tmp_path)
    state = {}
    got = nw.run_once(
        cfg, client=warden, state=state, inspector=lambda c: _container(), quiet=True
    )
    assert [a.kind for a in got] == ["fish-down"]
    assert warden.sent and warden.sent[0]["kind"] == "fish-down"

    again = nw.run_once(
        cfg, client=warden, state=state, inspector=lambda c: _container(), quiet=True
    )
    assert again == []
    assert len(warden.sent) == 1


def test_a_dry_run_never_touches_the_ledger(tmp_path):
    warden = FakeWarden(_ledger("nas-box"))
    cfg = _settings(tmp_path, execute=False)
    state = {}
    got = nw.run_once(
        cfg, client=warden, state=state, inspector=lambda c: _container(), quiet=True
    )
    # 空跑也要说"如果真跑会喊什么"，但账本一个字不动
    assert [a.kind for a in got] == ["fish-down"]
    assert warden.sent == []


def test_the_alert_survives_the_warden_being_down(tmp_path):
    warden = FakeWarden(RuntimeError("connection refused"))
    cfg = _settings(tmp_path)
    state = {}
    got = nw.run_once(
        cfg,
        client=warden,
        state=state,
        inspector=lambda c: _container(running=True, status="running"),
        quiet=True,
    )
    # 账本连不上时不该抛：它只喊一次，然后继续看
    assert "ledger-unreachable" in [a.kind for a in got]


def test_dedupe_relies_on_the_state_file_and_says_so(tmp_path):
    """冷却窗口是靠落盘的 `sent` 记着的 —— 文件丢了，它就会再喊一次。

    这是刻意的取舍：宁可重复喊，也不要因为状态文件写坏而永久闭嘴。
    """
    warden = FakeWarden(_ledger("nas-box"))
    cfg = _settings(tmp_path)
    state = {}
    nw.run_once(cfg, client=warden, state=state, inspector=lambda c: _container(), quiet=True)
    assert "fish-down" in state["sent"]

    # 同一个 state 再来一轮：闭嘴
    nw.run_once(cfg, client=warden, state=state, inspector=lambda c: _container(), quiet=True)
    assert len(warden.sent) == 1

    # 状态丢了的"新进程"：会再喊一次
    nw.run_once(cfg, client=warden, state={}, inspector=lambda c: _container(), quiet=True)
    assert len(warden.sent) == 2


# ------------------------------------------------------------- 账本那一侧


def test_the_warden_keeps_the_alert_and_exposes_it(tmp_path):
    from molt.warden import Warden

    w = Warden(root=tmp_path / "root")
    got = w.record_alert("fish-down", "容器没在跑", shell="nas-box", detail={"exit": 137})
    assert got["kind"] == "fish-down"

    st = w.status()
    assert [a["kind"] for a in st["alerts"]] == ["fish-down"]
    assert st["alerts"][0]["shell"] == "nas-box"


def test_alerts_survive_a_reload(tmp_path):
    from molt.warden import Warden

    root = tmp_path / "root"
    Warden(root=root).record_alert("fish-down", "第一台机器看到的")
    again = Warden(root=root)
    assert [a["kind"] for a in again.alerts_view()] == ["fish-down"]


def test_an_alert_without_kind_or_message_is_refused(tmp_path):
    from molt.errors import NeedsHuman
    from molt.warden import Warden

    w = Warden(root=tmp_path / "root")
    for kind, msg in (("", "有话说"), ("fish-down", "")):
        with pytest.raises(NeedsHuman):
            w.record_alert(kind, msg)


def test_the_server_accepts_an_alert(tmp_path):
    from molt.server import WardenServer

    srv = WardenServer(root=tmp_path / "root", token="t")
    r = srv.dispatch(
        "POST",
        "/alert",
        {"kind": "fish-down", "message": "容器没在跑", "shell": "nas-box"},
    )
    assert r.status == 200
    assert r.payload["alert"]["kind"] == "fish-down"
    assert srv.dispatch("GET", "/status", None).payload["status"]["alerts"]


def test_the_server_refuses_an_empty_alert(tmp_path):
    from molt.server import WardenServer

    srv = WardenServer(root=tmp_path / "root", token="t")
    r = srv.dispatch("POST", "/alert", {"kind": "fish-down"})
    assert r.status == 400


def test_coming_back_is_reported_so_the_red_banner_does_not_rot():
    got = nw.decide(
        container=_container(running=True, status="running"),
        ledger=_ledger("desk-box"),
        flag={"exists": False},
        previous={"container": {"running": False, "finished_at": "2026-11-16T15:29:07Z"}},
        shell="nas-box",
    )
    assert "fish-back" in [a.kind for a in got]


def test_a_container_that_was_already_running_is_not_a_recovery():
    got = nw.decide(
        container=_container(running=True, status="running"),
        ledger=_ledger("nas-box"),
        flag={"exists": False},
        previous={"container": {"running": True}},
        shell="nas-box",
    )
    assert got == []
