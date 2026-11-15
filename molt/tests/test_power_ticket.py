"""关机许可（power ticket）：**「走」和「关」是两件事**。

为什么这个文件必须存在：迁徙由一次 `/molt confirm` 决定，而「那台机器关不关」
是另一件事。绑在一起的话，人点一次「走」就顺带丢掉没保存的东西 ——
而那一次「走」本身完全是对的。所以这里钉三件事：

1. 票存在哪、读坏了会怎样（**坏票绝不能变成一次关机**）
2. 服务端确实能收票、能销票
3. 目标机在没有票的时候**什么都不做**（默认是"留着"）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from molt import server as server_mod
from molt.errors import NeedsHuman
from molt.server import WardenServer
from molt.warden import POWER_ACTIONS, Warden


def _agent_module():
    import importlib.util
    import sys

    path = Path(__file__).resolve().parents[2] / "tools" / "molt-agent.py"
    name = "_molt_agent_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # **必须先登记进 sys.modules**：dataclasses 在生成 __init__ 时要用
    # `sys.modules[cls.__module__]`，不登记就会 AttributeError: 'NoneType' has no __dict__。
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def w(tmp_path: Path) -> Warden:
    return Warden(root=tmp_path)


@pytest.fixture()
def srv(tmp_path: Path) -> WardenServer:
    return WardenServer(root=tmp_path, token="test-token", port=0)


# ------------------------------------------------------------------ 票本身


def test_a_ticket_lands_in_its_own_file_not_the_ledger(w: Warden) -> None:
    ticket = w.request_power("desktop-a", "shutdown", generation=19, by="nas-b")
    assert ticket["action"] == "shutdown"
    assert ticket["generation"] == 19
    assert (w.root / "power.json").exists()
    # 账本本身不写（`state.json` 还不存在：这张票不是交接记录）
    assert not (w.root / "state.json").exists()
    assert w.power_view()["desktop-a"]["action"] == "shutdown"


def test_a_corrupt_ticket_file_means_no_ticket(w: Warden) -> None:
    """**坏文件绝不能读成"可以关"** —— 默认永远是"没票" """
    (w.root / "power.json").write_text("{ 这不是 json", encoding="utf-8")
    assert w.power_view() == {}


def test_clearing_a_ticket_makes_it_stop_applying(w: Warden) -> None:
    w.request_power("desktop-a", "keep", generation=3)
    assert w.clear_power("desktop-a") is True
    assert w.power_view() == {}
    assert w.clear_power("desktop-a") is False


def test_only_known_actions_are_accepted(w: Warden) -> None:
    with pytest.raises(NeedsHuman):
        w.request_power("desktop-a", "explode")
    with pytest.raises(NeedsHuman):
        w.request_power("", "shutdown")
    assert "shutdown" in POWER_ACTIONS


def test_status_exposes_the_ticket(w: Warden) -> None:
    w.request_power("desktop-a", "shutdown", generation=19)
    st = w.status()
    assert st["power"]["desktop-a"]["action"] == "shutdown"


# ------------------------------------------------------------------ 服务端


def test_the_server_accepts_and_clears_a_ticket(srv: WardenServer) -> None:
    r = srv.dispatch("POST", "/power", {"shell": "desktop-a", "action": "shutdown", "generation": 19})
    assert r.status == 200
    assert r.payload["power"]["action"] == "shutdown"
    assert srv.dispatch("GET", "/status", None).payload["status"]["power"]["desktop-a"]["action"] == "shutdown"
    ack = srv.dispatch("POST", "/power/ack", {"shell": "desktop-a"})
    assert ack.status == 200 and ack.payload["cleared"] is True
    assert srv.dispatch("GET", "/status", None).payload["status"]["power"] == {}


def test_the_server_refuses_a_ticket_without_a_shell(srv: WardenServer) -> None:
    assert srv.dispatch("POST", "/power", {"action": "shutdown"}).status == 400
    assert srv.dispatch("POST", "/power/ack", {}).status == 400


def test_the_server_refuses_an_unknown_action(srv: WardenServer) -> None:
    r = srv.dispatch("POST", "/power", {"shell": "desktop-a", "action": "explode"})
    assert r.status == 422


# ------------------------------------------------------------------ 目标机的判断


def test_without_a_ticket_the_machine_stays_on() -> None:
    mod = _agent_module()
    assert mod.power_verdict(requires_permission=True, tickets={}, shell="desktop-a") == "ask"
    assert mod.power_verdict(requires_permission=True, tickets=None, shell="desktop-a") == "ask"


def test_a_shutdown_ticket_wins_and_a_keep_ticket_holds() -> None:
    mod = _agent_module()
    tickets = {"desktop-a": {"action": "shutdown"}}
    assert mod.power_verdict(requires_permission=True, tickets=tickets, shell="desktop-a") == "shutdown"
    keep = {"desktop-a": {"action": "keep"}}
    assert mod.power_verdict(requires_permission=True, tickets=keep, shell="desktop-a") == "keep"


def test_a_ticket_for_another_machine_does_not_touch_me() -> None:
    mod = _agent_module()
    tickets = {"desktop-b": {"action": "shutdown"}}
    assert mod.power_verdict(requires_permission=True, tickets=tickets, shell="desktop-a") == "ask"


def test_old_behaviour_is_still_reachable_but_not_the_default() -> None:
    mod = _agent_module()
    assert mod.power_verdict(requires_permission=False, tickets={}, shell="desktop-a") == "auto"
    assert mod.Settings().power_requires_permission is True
