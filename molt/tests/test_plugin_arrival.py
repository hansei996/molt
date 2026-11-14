"""落地播报：搬家之后第一句话前面那行「我刚从哪儿过来」。

## 这个功能为什么值得测试

它挂在**每一条回复的发送路径**上。写错一次不是"少显示一行字"，是三种更糟的结果：

1. 每说一句话都带一遍「我刚从 NAS 搬过来」—— 三天之后它还在说"刚搬过来"。
2. 它从来不说 —— 而这个失效**没有任何症状**：回复照常到达，只是少了一行。
3. 它抛异常 —— 那个人从此说不出话，而且日志里只有一条没人看的 traceback。

所以这里的测试盯的都是这三件事：**说几次、什么时候不说、以及它绝不抛**。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

MOLT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = MOLT_ROOT / 'plugin'
# sys.path 与 astrbot 桩都由 conftest.py 装好（理由见那里：为什么不装真的 AstrBot）。

import arrival  # noqa: E402  直接按模块名 import，因为 conftest 已经把 plugin/ 放进 sys.path
from astrbot.api.message_components import Plain  # noqa: E402  桩，见 conftest

# --------------------------------------------------------------------------- 纯函数


def test_note_names_the_source_by_label():
    note = arrival.compose_arrival_note(
        generation=13, from_label="UGREEN-776B", from_shell="ugreen-776b-8b7896df"
    )
    assert note == "（我刚从 UGREEN-776B 搬过来，这是第 13 代。）\n"


def test_note_falls_back_to_prettified_shell_id():
    """没有 label 时用去掉短哈希的壳名，不编一个"NAS"这种昵称。"""
    note = arrival.compose_arrival_note(generation=7, from_shell="ugreen-776b-8b7896df")
    assert "UGREEN" not in note
    assert "ugreen-776b" in note
    assert "8b7896df" not in note


def test_note_without_any_source_still_says_something_true():
    note = arrival.compose_arrival_note(generation=3)
    assert note == "（我刚搬到这台机器上，这是第 3 代。）\n"


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("ugreen-776b-8b7896df", "ugreen-776b"),
        ("desktop-h2kgiao", "desktop-h2kgiao"),
        ("mac-1a2b3c4d", "mac-1a2b3c4d"),  # 只有两段，不动
        ("host-zzzzzzzz", "host-zzzzzzzz"),  # 尾巴不是十六进制，不动
        ("", ""),
    ],
)
def test_prettify_shell(raw, want):
    assert arrival.prettify_shell(raw) == want


# --------------------------------------------------------------------------- pick_arrival


def _status(*, gen=13, consumed="desktop-h2kgiao", pushed="ugreen-776b-8b7896df",
            consumed_at=None, beats=None):
    return {
        "latest_generation": {
            "generation": gen,
            "consumed_by": consumed,
            "pushed_by": pushed,
            "consumed_at": str(time.time() if consumed_at is None else consumed_at),
        },
        "heartbeats": beats or {},
    }


def test_pick_ignores_generation_someone_else_consumed():
    got = arrival.pick_arrival(
        _status(consumed="someone-else"), shell="desktop-h2kgiao", now=time.time()
    )
    assert got is None


def test_pick_ignores_generation_already_announced():
    got = arrival.pick_arrival(
        _status(gen=13), shell="desktop-h2kgiao", now=time.time(), announced_generation=13
    )
    assert got is None


def test_pick_marks_old_arrivals_as_stale_instead_of_hiding_them():
    """陈旧的也要返回 —— 调用方要拿它把 announced_generation 推上去，
    否则每一条回复都会再问一遍账本，永远得出同一个"太久了"的结论。"""
    now = time.time()
    got = arrival.pick_arrival(
        _status(consumed_at=now - 3 * 3600), shell="desktop-h2kgiao", now=now
    )
    assert got is not None
    assert got["stale"] is True
    assert got["age_seconds"] > 3600


def test_pick_is_fresh_right_after_landing():
    now = time.time()
    got = arrival.pick_arrival(_status(consumed_at=now - 30), shell="desktop-h2kgiao", now=now)
    assert got["stale"] is False


def test_pick_does_not_guess_when_landing_time_is_unreadable():
    """账本的 consumed_at 读不出来时不判陈旧 —— "不知道"不等于"太久"。"""
    got = arrival.pick_arrival(
        _status(consumed_at="not-a-number"), shell="desktop-h2kgiao", now=time.time()
    )
    assert got["stale"] is False
    assert got["age_seconds"] is None


def test_pick_takes_the_human_label_from_heartbeats():
    beats = {"ugreen-776b-8b7896df": {"label": "UGREEN-776B", "age_seconds": 3}}
    got = arrival.pick_arrival(
        _status(beats=beats), shell="desktop-h2kgiao", now=time.time()
    )
    assert got["from_label"] == "UGREEN-776B"


def test_pick_survives_status_without_heartbeats():
    got = arrival.pick_arrival(
        {"latest_generation": _status()["latest_generation"]},
        shell="desktop-h2kgiao",
        now=time.time(),
    )
    assert got["from_label"] == ""


@pytest.mark.parametrize("bad", [None, {}, {"latest_generation": None}, {"latest_generation": {}}])
def test_pick_on_empty_status(bad):
    assert arrival.pick_arrival(bad, shell="desktop-h2kgiao", now=time.time()) is None


def test_pick_needs_a_shell():
    assert arrival.pick_arrival(_status(), shell="", now=time.time()) is None


# --------------------------------------------------------------------------- 状态文件


def test_state_roundtrip(tmp_path):
    path = tmp_path / arrival.STATE_FILENAME
    state = arrival.ArrivalState.load(path)
    assert state.announced_generation == 0
    state.mark(13, detail="announced")
    again = arrival.ArrivalState.load(path)
    assert again.announced_generation == 13
    assert json.loads(path.read_text(encoding="utf-8"))["last_detail"] == "announced"


def test_state_survives_a_corrupt_file(tmp_path):
    path = tmp_path / arrival.STATE_FILENAME
    path.write_text("{ not json", encoding="utf-8")
    assert arrival.ArrivalState.load(path).announced_generation == 0


# --------------------------------------------------------------------------- 插件接线


class _Sent(list):
    """收集 event.send() 真的发出去的东西。"""

    pass


class _Event:
    """只提供 on_waiting_llm_request 那个 handler 用到的方法。"""

    def __init__(self):
        self.sent = _Sent()

    def plain_result(self, text):
        return text

    async def send(self, payload):
        self.sent.append(payload)


class _FakeMover:
    def __init__(self, status, *, shell="desktop-h2kgiao", boom=False):
        self._status = status
        self._shell = shell
        self._boom = boom
        self.ledger_calls = 0

    def settings(self):
        if self._boom:
            raise RuntimeError("配置文件坏了")
        return type("S", (), {"shell": self._shell})()

    def ledger_status(self):
        self.ledger_calls += 1
        return self._status


def _plugin(tmp_path, mover):
    """造一个只有落地播报相关字段的插件实例。

    刻意不跑 __init__：那会去 import command.py、读真实配置文件。
    这里要验的是这段逻辑说几次、什么时候不说，不是构造过程。
    """
    from astrbot_plugin_molt.main import MoltControlPlugin

    plugin = MoltControlPlugin.__new__(MoltControlPlugin)
    plugin._mover = mover
    plugin._arrival_state = None
    # __init__ 里那几个字段在这里手工补上 —— 和 test_plugin_control 补
    # _drained / _has_drained 是同一条理由：不跑构造过程，但字段要齐。
    plugin._arrival_checked = False
    plugin._arrival_attempts = 0
    plugin._arrival_said = False
    plugin._arrival_state_path = lambda: tmp_path / arrival.STATE_FILENAME
    return plugin


def _run(plugin, event=None):
    import asyncio

    event = event or _Event()
    asyncio.run(plugin.announce_arrival(event))
    return event


def test_plugin_says_the_note_once_before_the_reply(tmp_path):
    mover = _FakeMover(_status(beats={"ugreen-776b-8b7896df": {"label": "UGREEN-776B"}}))
    plugin = _plugin(tmp_path, mover)

    event = _run(plugin)
    assert len(event.sent) == 1
    assert "我刚从 UGREEN-776B 搬过来" in event.sent[0]
    assert "第 13 代" in event.sent[0]

    # 第二次（同一次落地）什么都不说
    assert _run(plugin).sent == []


def test_plugin_does_not_ask_the_ledger_again_after_speaking(tmp_path):
    """说过之后就不再问账本 —— 每一条回复一次 HTTP 请求是不可接受的。"""
    mover = _FakeMover(_status())
    plugin = _plugin(tmp_path, mover)
    for _ in range(4):
        _run(plugin)
    assert mover.ledger_calls == 1


def test_plugin_stays_quiet_when_the_arrival_is_old_but_still_marks_it(tmp_path):
    mover = _FakeMover(_status(consumed_at=time.time() - 4 * 3600))
    plugin = _plugin(tmp_path, mover)

    assert _run(plugin).sent == []          # 不说
    # 但状态被推到了这一代：不会再问第二次
    assert plugin._arrival_state_obj().announced_generation == 13
    _run(plugin)
    assert mover.ledger_calls == 1


def test_plugin_says_nothing_when_the_ledger_is_unreachable(tmp_path):
    mover = _FakeMover(None)
    plugin = _plugin(tmp_path, mover)
    assert _run(plugin).sent == []


def test_plugin_says_nothing_when_settings_explode(tmp_path):
    """配置读不出来时它只是不说话 —— 绝不能让显示一行状态变成说不出话。"""
    mover = _FakeMover(_status(), boom=True)
    plugin = _plugin(tmp_path, mover)
    assert _run(plugin).sent == []


def test_plugin_without_mover_is_silent(tmp_path):
    plugin = _plugin(tmp_path, None)
    assert _run(plugin).sent == []


def test_plugin_marks_state_even_when_the_send_fails(tmp_path):
    """发不出去也不能每分钟重试一次 —— 这一代就这样了。"""

    class _BoomEvent(_Event):
        async def send(self, payload):
            raise RuntimeError("发不出去")

    mover = _FakeMover(_status())
    plugin = _plugin(tmp_path, mover)
    import asyncio

    asyncio.run(plugin.announce_arrival(_BoomEvent()))
    assert plugin._arrival_state_obj().announced_generation == 13
    assert _run(plugin).sent == []
    assert mover.ledger_calls == 1


def test_plugin_stops_asking_after_three_failed_ledger_calls(tmp_path):
    """账本问不到时最多试三次。

    每一次 ledger_status() 自带 5 秒超时，不封顶的话最坏情况下这个人
    每说一句话都要先等 5 秒 —— 一个为了显示状态而让对话变慢的功能，
    最后一定会被关掉。
    """
    mover = _FakeMover(None)
    plugin = _plugin(tmp_path, mover)
    for _ in range(6):
        _run(plugin)
    assert mover.ledger_calls == 3
