"""落地观察：把「我在第 N 代搬到了这里」记成它自己的观察，而不是伪造一条主人的话。

覆盖三块：

1. `plugin/observed.py` —— 文案只说真话、账本挑代、状态文件读写；
2. `plugin/memory_bridge.py` —— 三级查找、写入成功/失败/异常都不抛；
3. `plugin/main.py` 的 `observe_migration_once()` —— 记一次、之后跳过、失败不落盘。

第 3 条是这一版的重点：**失败不能落盘**，否则"记忆没写进去"会被记成"已经记过了"，
而两者在日志里长得一模一样。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugin"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from astrbot_plugin_molt import memory_bridge, observed  # noqa: E402
from astrbot_plugin_molt.main import MoltControlPlugin  # noqa: E402


LEDGER = {
    "latest_generation": {
        "generation": 13,
        "pushed_by": "nas-shell",
        "consumed_by": "win-shell",
        "consumed_at": "2026-11-14T18:03:50Z",
    },
    "lease": {"holder": None, "state": "no-lease"},
}


# --------------------------------------------------------------- observed.py


def test_compose_says_only_what_it_knows():
    text = observed.compose_observation(
        generation=13,
        shell="win-shell",
        pushed_by="nas-shell",
        consumed_at="2026-11-14T18:03:50Z",
        cwd="G:/molt-fish/data",
        hostname="DESKTOP-H2KGIAO",
    )
    assert "第 13 代" in text
    assert "win-shell" in text
    assert "nas-shell" in text
    assert "DESKTOP-H2KGIAO" in text
    assert "G:/molt-fish/data" in text
    assert "2026-11-14T18:03:50Z" in text
    assert "不是主人告诉我的" in text
    # 不编
    assert "None" not in text
    assert "大约" not in text


def test_compose_omits_unknown_fields_entirely():
    """缺的字段整句消失 —— 不写占位符，也不写"未知"。"""
    text = observed.compose_observation(generation=1, shell="win-shell")
    assert "上一站" not in text
    assert "主机名" not in text
    assert "落地时间" not in text
    assert "None" not in text


def test_compose_does_not_repeat_the_shell_as_its_own_previous_stop():
    """pushed_by 和 shell 相同（同一台机器重落）时不写"上一站是它自己"。"""
    text = observed.compose_observation(generation=2, shell="win-shell", pushed_by="win-shell")
    assert "上一站" not in text


def test_pick_generation_only_returns_my_own_landing():
    assert observed.pick_generation(LEDGER, shell="win-shell")["generation"] == 13
    assert observed.pick_generation(LEDGER, shell="nas-shell") is None
    assert observed.pick_generation(None, shell="win-shell") is None
    assert observed.pick_generation({}, shell="win-shell") is None
    assert observed.pick_generation(LEDGER, shell="") is None


@pytest.mark.parametrize("bad", [{"generation": "abc"}, {"generation": 0}, {"generation": None}])
def test_pick_generation_rejects_non_positive_or_garbage(bad):
    status = {"latest_generation": {"consumed_by": "win-shell", **bad}}
    assert observed.pick_generation(status, shell="win-shell") is None


def test_state_roundtrip(tmp_path):
    path = tmp_path / "observed_state.json"
    st = observed.ObservationState.load(path)
    assert st.observed_generation == 0
    st.mark(13, detail="get_registered_star")

    again = observed.ObservationState.load(path)
    assert again.observed_generation == 13
    assert again.data["last_detail"] == "get_registered_star"


def test_state_survives_corrupt_file(tmp_path):
    path = tmp_path / "observed_state.json"
    path.write_text("{ 这不是 json", encoding="utf-8")
    st = observed.ObservationState.load(path)
    assert st.observed_generation == 0


def test_state_survives_non_object_json(tmp_path):
    path = tmp_path / "observed_state.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert observed.ObservationState.load(path).observed_generation == 0


# --------------------------------------------------------- memory_bridge.py


class FakeService:
    def __init__(self, result=None, *, raises=None):
        self.calls = []
        self._result = result if result is not None else {"ok": True, "state": "sent"}
        self._raises = raises

    async def record_bot_personal_memory(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise self._raises
        return self._result


class FakeStar:
    def __init__(self, service=None, bridge=None):
        if service is not None:
            self.service = service
        if bridge is not None:
            self.memory_companion = bridge


class FakeMeta:
    def __init__(self, name, star_cls):
        self.name = name
        self.star_cls = star_cls


class FakeContext:
    def __init__(self, star_cls=None):
        self._star_cls = star_cls

    def get_registered_star(self, name):
        if self._star_cls is None or name != memory_bridge.MEMORY_PLUGIN:
            return None
        return FakeMeta(name, self._star_cls)


def test_record_uses_observed_type_not_a_fake_user_message():
    svc = FakeService()
    ctx = FakeContext(FakeStar(service=svc))

    out = asyncio.run(
        memory_bridge.record_observed_activity(
            ctx, "我观察到：我在第 13 代搬到了 win-shell。",
            occurred_at="2026-11-14T18:03:50Z", shell="win-shell", generation=13,
            idempotency_key="molt-migration-gen-13",
        )
    )
    assert out["ok"] is True
    assert out["via"] == "get_registered_star"
    call = svc.calls[0]
    assert call["memory_type"] == "bot_observed_activity"
    assert call["payload"]["generation"] == 13
    assert call["idempotency_key"] == "molt-migration-gen-13"
    assert call["occurred_at"] == "2026-11-14T18:03:50Z"


def test_record_falls_back_to_the_bridge_attribute():
    bridge = FakeService()
    ctx = FakeContext(FakeStar(bridge=bridge))
    out = asyncio.run(
        memory_bridge.record_observed_activity(ctx, "x", occurred_at="t", shell="s", generation=1)
    )
    assert out["ok"] is True
    assert bridge.calls


def test_record_without_the_memory_plugin_is_a_quiet_no():
    out = asyncio.run(
        memory_bridge.record_observed_activity(FakeContext(), "x", occurred_at="t")
    )
    assert out["ok"] is False
    assert "没找到 astrbot_plugin_memory_companion" in out["reason"]


def test_record_never_raises_when_the_other_plugin_blows_up():
    svc = FakeService(raises=RuntimeError("数据库锁住了"))
    ctx = FakeContext(FakeStar(service=svc))
    out = asyncio.run(
        memory_bridge.record_observed_activity(ctx, "x", occurred_at="t")
    )
    assert out["ok"] is False
    assert "RuntimeError" in out["reason"]


def test_record_reports_refusal_from_the_other_side():
    svc = FakeService(result={"ok": False, "error_code": "store_unavailable"})
    ctx = FakeContext(FakeStar(service=svc))
    out = asyncio.run(
        memory_bridge.record_observed_activity(ctx, "x", occurred_at="t")
    )
    assert out["ok"] is False
    assert out["reason"] == "store_unavailable"


def test_record_rejects_empty_text():
    out = asyncio.run(
        memory_bridge.record_observed_activity(FakeContext(), "   ", occurred_at="t")
    )
    assert out["ok"] is False


# ------------------------------------------------------------- main.py 的调度


class FakeSettings:
    shell = "win-shell"


class FakeMover:
    def __init__(self, status):
        self._status = status

    def settings(self):
        return FakeSettings()

    def ledger_status(self):
        return self._status


def _plugin(tmp_path, *, status=LEDGER, monkeypatch=None):
    p = MoltControlPlugin.__new__(MoltControlPlugin)
    p.context = FakeContext(FakeStar(service=FakeService()))
    p._mover = FakeMover(status)
    p._obs_state = observed.ObservationState.load(tmp_path / "observed_state.json")
    p._obs_task = None
    return p


def test_observe_once_records_and_marks(tmp_path, monkeypatch):
    monkeypatch.setenv("MOLT_HOST_LABEL", "win-shell")
    p = _plugin(tmp_path)

    box = {}

    async def fake_record(ctx, text, **kwargs):
        box["text"] = text
        box.update(kwargs)
        return {"ok": True, "via": "get_registered_star", "reason": "sent"}

    monkeypatch.setattr(memory_bridge, "record_observed_activity", fake_record)
    out = asyncio.run(p.observe_migration_once())

    assert out == {"action": "recorded", "generation": 13, "via": "get_registered_star"}
    assert box["generation"] == 13
    assert "第 13 代" in box["text"]
    assert p._obs_state.observed_generation == 13
    # 落盘了
    saved = json.loads((tmp_path / "observed_state.json").read_text(encoding="utf-8"))
    assert saved["observed_generation"] == 13


def test_observe_twice_does_not_record_twice(tmp_path, monkeypatch):
    monkeypatch.setenv("MOLT_HOST_LABEL", "win-shell")
    p = _plugin(tmp_path)
    calls = []

    async def fake_record(ctx, text, **kwargs):
        calls.append(kwargs["generation"])
        return {"ok": True, "via": "x", "reason": ""}

    monkeypatch.setattr(memory_bridge, "record_observed_activity", fake_record)
    asyncio.run(p.observe_migration_once())
    out = asyncio.run(p.observe_migration_once())

    assert out["action"] == "skip"
    assert "已经记过了" in out["reason"]
    assert calls == [13]


def test_observe_failure_does_not_mark_the_generation(tmp_path, monkeypatch):
    """失败不落盘 —— 否则"没记住"会被记成"已经记过了"，而这正是最难查的那种。"""
    monkeypatch.setenv("MOLT_HOST_LABEL", "win-shell")
    p = _plugin(tmp_path)

    async def failing(ctx, text, **kwargs):
        return {"ok": False, "via": "x", "reason": "store_unavailable"}

    monkeypatch.setattr(memory_bridge, "record_observed_activity", failing)
    out = asyncio.run(p.observe_migration_once())

    assert out["action"] == "failed"
    assert p._obs_state.observed_generation == 0
    assert not (tmp_path / "observed_state.json").exists()


def test_observe_skips_when_the_generation_is_not_mine(tmp_path, monkeypatch):
    monkeypatch.setenv("MOLT_HOST_LABEL", "win-shell")
    p = _plugin(tmp_path, status={"latest_generation": {**LEDGER["latest_generation"],
                                                        "consumed_by": "nas-shell"}})
    out = asyncio.run(p.observe_migration_once())
    assert out["action"] == "skip"


def test_observe_skips_when_the_ledger_is_unreachable(tmp_path, monkeypatch):
    monkeypatch.setenv("MOLT_HOST_LABEL", "win-shell")
    p = _plugin(tmp_path, status=None)
    out = asyncio.run(p.observe_migration_once())
    assert out["action"] == "skip"
    assert "不猜" in out["reason"]


def test_observe_skips_without_the_command_layer(tmp_path):
    p = MoltControlPlugin.__new__(MoltControlPlugin)
    p.context = FakeContext()
    p._mover = None
    p._obs_state = observed.ObservationState.load(tmp_path / "observed_state.json")
    out = asyncio.run(p.observe_migration_once())
    assert out["action"] == "skip"
