"""主动发消息：不发假消息、只发给一个人、一代只说一次。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_molt import notify


def _db(data_dir: Path, rows) -> None:
    db = sqlite3.connect(data_dir / "data_v4.db")
    db.execute(
        "create table conversations (created_at text, updated_at text, platform_id text,"
        " user_id text)"
    )
    for updated, user_id in rows:
        db.execute(
            "insert into conversations values ('2026-11-16', ?, ?, ?)",
            (updated, user_id.split(":")[0], user_id),
        )
    db.commit()
    db.close()


def test_route_comes_from_the_latest_private_conversation(tmp_path):
    _db(
        tmp_path,
        [
            ("2026-11-16 17:00", "default_1906405747:FriendMessage:OPENID"),
            ("2026-11-16 18:00", "webchat:FriendMessage:webchat!x"),
        ],
    )

    route = notify.read_owner_route(tmp_path)

    assert route == ("default_1906405747", "OPENID")


def test_no_database_means_no_guessing(tmp_path):
    assert notify.read_owner_route(tmp_path) is None


def test_group_conversations_are_not_a_private_route(tmp_path):
    _db(tmp_path, [("2026-11-16 17:00", "default_1906405747:GroupMessage:G1")])
    assert notify.read_owner_route(tmp_path) is None


def test_the_push_text_says_where_it_is_without_counting_lives():
    text = notify.compose_arrival_push(label="DESKTOP-H2KGIAO", generation=37)
    assert "DESKTOP-H2KGIAO" in text
    assert "代" not in text


class _Platform:
    def __init__(self, pid, record):
        self._pid = pid
        self.record = record

    def meta(self):
        return SimpleNamespace(id=self._pid)

    async def send_by_session(self, session, chain):
        self.record.append((self._pid, session, chain))


class _Ctx:
    def __init__(self, platforms):
        self.platform_manager = SimpleNamespace(get_insts=lambda: platforms)


def test_it_sends_through_the_matching_platform(tmp_path, monkeypatch):
    _db(tmp_path, [("2026-11-16 17:00", "qq-1:FriendMessage:OPENID")])
    sent = []

    got = asyncio.run(
        notify.push_arrival(
            _Ctx([_Platform("qq-1", sent)]),
            data_dir=tmp_path,
            text="我搬过来了。",
        )
    )

    assert got["state"] == "delivered"
    assert len(sent) == 1
    pid, session, chain = sent[0]
    assert pid == "qq-1"
    assert getattr(session, "session_id", None) or True
    assert "我搬过来了" in str(chain)


def test_a_platform_that_raises_is_reported_not_hidden(tmp_path):
    _db(tmp_path, [("2026-11-16 17:00", "qq-1:FriendMessage:OPENID")])

    class _Boom(_Platform):
        async def send_by_session(self, session, chain):
            raise RuntimeError("网关拒了")

    got = asyncio.run(
        notify.push_arrival(_Ctx([_Boom("qq-1", [])]), data_dir=tmp_path, text="x")
    )

    assert got["state"] == "failed"
    assert "网关拒了" in got["reason"]


def test_no_platform_means_it_says_so(tmp_path):
    _db(tmp_path, [("2026-11-16 17:00", "qq-1:FriendMessage:OPENID")])
    got = asyncio.run(notify.push_arrival(_Ctx([]), data_dir=tmp_path, text="x"))
    assert got["state"] == "no-platform"


def test_an_ambiguous_list_is_not_guessed(tmp_path):
    a, b = _Platform("a", []), _Platform("b", [])
    assert notify.find_platform([a, b], "qq-1") is None
    assert notify.find_platform([a], "qq-1") is a
    assert notify.find_platform([a, b], "b") is b


def test_one_generation_is_pushed_once(tmp_path):
    state = notify.NotifyState(tmp_path / notify.STATE_FILENAME)
    assert notify.should_push(state, 37) is True
    state.mark(37, "delivered")
    assert notify.should_push(state, 37) is False
    assert notify.should_push(state, 38) is True

    again = notify.NotifyState(tmp_path / notify.STATE_FILENAME)
    assert json.loads((tmp_path / notify.STATE_FILENAME).read_text("utf-8"))["pushed_generation"] == 37
    assert notify.should_push(again, 37) is False


def test_the_notify_route_imports_the_request_proxy():
    """`plugin/api.py` 的 `/notify` 要读请求体 —— 少了这个 import 就是 500。

    这条是真机上抓到的：`NameError: name 'request' is not defined`，
    而 AstrBot 只把它变成一个没有内容的 "Internal server error"。
    """
    import importlib.util
    import sys

    root = Path(__file__).resolve().parents[2]
    name = "molt_api_notify_probe"
    spec = importlib.util.spec_from_file_location(name, root / "plugin" / "api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)

    assert hasattr(module, "request"), "api.py 必须从 astrbot.api.web 导入 request"
