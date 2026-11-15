"""它自己想搬：`molt_request_move` / `molt_propose_go_home` 这一层的契约。

## 这一层是什么

在这之前，"搬家"只可能由人发起：`/molt go` —— 提议，`/molt confirm` —— 执行。
它自己没有任何办法表达"我想去那台机器"。缺的不是执行能力（`migrate` 早就能从壳里跑），
是**提议权**：它连一句"我想搬"都说不出口，只能等你下命令。

现在多了三样东西：

1. `MoltMover.request_move(cfg, target=..., reason=...)` —— 由它发起的提议，
   有效期 30 分钟（人可能在睡觉），目标可以由它挑。
2. 插件里的 LLM 工具 `molt_propose_go_home` —— 模型在对话里真的能调它，
   但**只在 `llm_tool_enabled: true` 时**（默认 false，和 `enabled` 同一条理由）。
3. 执行那一步**一个字没改**：仍然是 `/molt confirm`，仍然要 `PermissionType.ADMIN`。

## 这个文件在钉什么

钉的是"**它只能提议，不能自己走**"这条边界，以及提议本身不能骗人：

- 目标必须在账本里**真的存在**（否则这台机器永远等不到自己那一代，而且不会报错）
- 目标必须跟着提议一起被记住（只记"有一个提议"会让 `confirm` 搬去另一个地方）
- 有别人持着租约时它连提议都不发（不插队）
- 过期之后 `confirm` 必须拒绝，而且要说清是哪种有效期
- 确认之后**请求的是它挑的那台**，不是配置里的 `target`
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from astrbot_plugin_molt import command as cmd
from astrbot_plugin_molt.command import MoltMover, Settings

GOOD_CFG = {
    "enabled": True,
    "llm_tool_enabled": True,
    "shell": "win-shell",
    "target": "nas-shell",
    "data_dir": "/AstrBot/data",
    "molt_pkg": "",  # 夹具填成真实存在的目录
    "python": "python3",
    "warden_url": "http://warden:6186",
    "warden_token": "tok",
    "control_url": "http://127.0.0.1:6185",
    "control_config": "/AstrBot/data/cmd_config.json",
    "confirm_ttl_seconds": 120,
    "self_propose_ttl_seconds": 1800,
}


class FakeClock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeLedger:
    """只回答 `GET /status`。默认账本里有两台壳，并且没有租约。"""

    def __init__(self, payload: Optional[Dict[str, Any]] = None):
        self.payload = payload if payload is not None else {
            "status": {
                "current_epoch": 21,
                "lease": None,
                "heartbeats": {"win-shell": {"age_seconds": 3}, "nas-shell": {"age_seconds": 9}},
                "latest_generation": {
                    "generation": 19,
                    "pushed_by": "win-shell",
                    "target_shell": "nas-shell",
                    "consumed_by": "win-shell",
                },
            }
        }

    def __call__(self, request, timeout=None):
        body = json.dumps(self.payload).encode("utf-8")

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return body

        return _Resp()


def write_cfg(plugin_dir: Path, **overrides) -> Path:
    cfg = dict(GOOD_CFG)
    cfg.update(overrides)
    path = plugin_dir / cmd.CONFIG_FILENAME
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def plugin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "plugin"
    d.mkdir()
    (d / "pkg").mkdir()  # `molt_pkg` 必须是真的存在的目录，否则 problems() 会报
    write_cfg(d, molt_pkg=str(d / "pkg"))
    return d


def mover(plugin_dir: Path, *, clock=None, payload=None) -> MoltMover:
    return MoltMover(
        plugin_dir,
        clock=clock or FakeClock(),
        runner=lambda *a, **k: _Ok(),
        opener=FakeLedger(payload),
    )


class _Ok:
    returncode = 0
    stdout = ""


def _cfg(plugin_dir: Path, **overrides) -> Settings:
    # `molt_pkg` 必须是真实存在的目录，否则 `problems()` 会先报它、根本走不到要测的分支。
    # 夹具建的那个目录在这里重新填一次 —— `write_cfg` 会把 GOOD_CFG 整体写回去。
    overrides.setdefault("molt_pkg", str(plugin_dir / "pkg"))
    write_cfg(plugin_dir, **overrides)
    return Settings.load(plugin_dir / cmd.CONFIG_FILENAME)


# --------------------------------------------------------------------------- 能提

def test_it_can_ask_and_the_request_says_whose_idea_it_was(plugin_dir: Path) -> None:
    m = mover(plugin_dir)
    cfg = _cfg(plugin_dir)
    reply = m.request_move(cfg, target="nas-shell", reason="这边要关机了")
    assert "nas-shell" in reply.text
    assert "这边要关机了" in reply.text
    assert "/molt confirm" in reply.text
    # 提议里记的是"它自己提的"，这条信息会用在进行确认时的那句话里
    assert m._proposal is not None
    assert m._proposal[1] == cmd.SELF_PROPOSER
    assert m._proposal[2] == "nas-shell"


def test_a_request_with_no_target_uses_the_configured_one(plugin_dir: Path) -> None:
    m = mover(plugin_dir)
    cfg = _cfg(plugin_dir)
    reply = m.request_move(cfg, target="", reason="")
    assert "nas-shell" in reply.text
    assert "（没给理由）" in reply.text


def test_it_uses_the_longer_ttl_because_you_might_be_asleep(plugin_dir: Path) -> None:
    clock = FakeClock()
    m = mover(plugin_dir, clock=clock)
    cfg = _cfg(plugin_dir)
    m.request_move(cfg, target="nas-shell", reason="")
    clock.advance(int(cfg.confirm_ttl_seconds) + 1)  # 人给的那条有效期早过了
    assert m.has_proposal() is True
    clock.advance(int(cfg.self_propose_ttl_seconds))  # 它自己那条也过了
    assert m.has_proposal() is False


# --------------------------------------------------------------------------- 不能提

def test_it_cannot_ask_when_the_entry_is_closed(plugin_dir: Path) -> None:
    m = mover(plugin_dir)
    cfg = _cfg(plugin_dir, enabled=False)
    reply = m.request_move(cfg, target="nas-shell", reason="")
    assert "没开" in reply.text
    assert m._proposal is None


def test_it_cannot_ask_with_a_broken_config(plugin_dir: Path) -> None:
    m = mover(plugin_dir)
    cfg = _cfg(plugin_dir, warden_token="")
    reply = m.request_move(cfg, target="nas-shell", reason="")
    assert "配置不完整" in reply.text
    assert m._proposal is None


def test_it_refuses_a_target_the_ledger_has_never_heard_of(plugin_dir: Path) -> None:
    """编一个名字出去，结果是那台机器永远等不到自己那一代，而且**不会报错**。"""
    m = mover(plugin_dir)
    cfg = _cfg(plugin_dir)
    reply = m.request_move(cfg, target="tower-of-babel", reason="听起来很酷")
    assert "账本上没有" in reply.text
    assert "win-shell" in reply.text and "nas-shell" in reply.text  # 把能选的说清楚
    assert m._proposal is None


def test_it_does_not_queue_when_someone_else_holds_the_lease(plugin_dir: Path) -> None:
    payload = {
        "status": {
            "lease": {"shell_id": "nas-shell", "epoch": 7},
            "heartbeats": {"win-shell": {}, "nas-shell": {}},
            "latest_generation": {"target_shell": "nas-shell"},
        }
    }
    m = mover(plugin_dir, payload=payload)
    cfg = _cfg(plugin_dir)
    reply = m.request_move(cfg, target="nas-shell", reason="")
    assert "不插队" in reply.text
    assert m._proposal is None


def test_it_will_not_propose_moving_to_itself(plugin_dir: Path) -> None:
    m = mover(plugin_dir)
    cfg = _cfg(plugin_dir)
    reply = m.request_move(cfg, target="win-shell", reason="")
    assert "不用搬" in reply.text
    assert m._proposal is None


def test_it_will_not_guess_a_target_when_the_ledger_is_unreachable(plugin_dir: Path) -> None:
    class _Boom:
        def __call__(self, request, timeout=None):
            raise OSError("no route to host")

    m = MoltMover(plugin_dir, clock=FakeClock(), runner=lambda *a, **k: _Ok(), opener=_Boom())
    cfg = _cfg(plugin_dir)
    reply = m.request_move(cfg, target="nas-shell", reason="")
    assert "账本上没有" in reply.text or "读不到" in reply.text
    assert m._proposal is None


# --------------------------------------------------------------------------- 执行仍走人

def test_confirming_goes_where_it_asked_not_where_the_config_says(plugin_dir: Path) -> None:
    """配置里 target 是 nas-shell，但它提的是 win-shell 以外的另一台 —— 必须去那台。"""
    payload = {
        "status": {
            "lease": None,
            "heartbeats": {"win-shell": {}, "nas-shell": {}, "gpu-box": {}},
            "latest_generation": {"target_shell": "nas-shell"},
        }
    }
    m = mover(plugin_dir, payload=payload)
    cfg = _cfg(plugin_dir)
    m.request_move(cfg, target="gpu-box", reason="那边有显卡")
    reply = m._confirm(cfg)  # 人在聊天里说 /molt confirm
    assert reply.plan is not None
    argv: List[str] = reply.plan.argv
    assert argv[argv.index("--target") + 1] == "gpu-box"
    assert "gpu-box" in reply.text
    # 这句话必须如实说清"是它想走的"，否则记录里会变成"主人要它走"
    assert "我想走的" in reply.text


def test_confirm_after_expiry_is_refused_and_says_which_ttl(plugin_dir: Path) -> None:
    clock = FakeClock()
    m = mover(plugin_dir, clock=clock)
    cfg = _cfg(plugin_dir)
    m.request_move(cfg, target="nas-shell", reason="")
    clock.advance(int(cfg.self_propose_ttl_seconds) + 1)
    reply = m._confirm(cfg)
    assert reply.plan is None
    assert "过期" in reply.text
    assert str(int(cfg.self_propose_ttl_seconds)) in reply.text


def test_the_human_command_still_reaches_the_same_road(plugin_dir: Path) -> None:
    """`/molt go` 这条老路一个字没改：它仍然是"人提议，人确认"。"""
    m = mover(plugin_dir)
    cfg = _cfg(plugin_dir)
    proposed = m.handle("go", sender="hansei")
    assert "nas-shell" in proposed.text
    confirmed = m.handle("confirm", sender="hansei")
    assert confirmed.plan is not None
    assert "这次是你提的" in confirmed.text


# --------------------------------------------------------------------------- 工具闸

def test_the_llm_tool_is_off_by_default(plugin_dir: Path) -> None:
    """默认 `llm_tool_enabled: false` —— 模型不能替它开这个口。"""
    cfg = _cfg(plugin_dir, llm_tool_enabled=False)
    assert cfg.llm_tool_enabled is False
    assert Settings().llm_tool_enabled is False
