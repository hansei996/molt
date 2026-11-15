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
            # 账本的租约视图字段是 holder / state / epoch —— 不是 shell_id。
            # 这两条用例原来用 `shell_id`，于是它们测的是"守卫永不触发"：
            # 而那种红与绿的区别，正好是我这次要修的那个 bug。
            "lease": {"holder": "nas-shell", "state": "active", "epoch": 7},
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
            # gpu-box 是开着的（3 秒前心跳）—— 这条用例验的是"去哪台"，
            # 不是"目标关着时怎么办"（后者由 test_plugin_two_step 专门验）。
            "heartbeats": {
                "win-shell": {"age_seconds": 4},
                "nas-shell": {"age_seconds": 9},
                "gpu-box": {"age_seconds": 3},
            },
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


# --------------------------------------------------------------------------- 触发源

#: 这一节钉的是 v0.6 的**接口层**（用户 2026-11-15 的要求：「这个要给未来留足接口，
#: 我们现在只是做通用的设计」）。四个来源里今天真的会用的只有 `human` 与 `self`；
#: `utterance`（「去打会儿游戏吧」）与 `schedule`（定时）只把路铺好，**不实装**。
#:
#: 两条不变量：
#:   1. 不认识的来源 -> 拒绝。宁可不动，也不要在确认文案里写一句猜出来的「是谁提的」。
#:   2. 配置里写了、代码还没实装的触发源 -> 拒绝并说明。默默忽略等于让人以为配好了。


def test_source_labels_are_explicit(plugin_dir: Path) -> None:
    assert cmd.source_label("human") == "你提的"
    assert cmd.source_label("self") == "我自己想走的"
    assert cmd.source_label("utterance") == "你那句话"
    assert cmd.source_label("schedule") == "到点了"
    # 不认识 -> 空串（调用方会退回「这次是你提的」这个保守说法，而不是打出 source=xyz）
    assert cmd.source_label("telepathy") == ""
    assert cmd.source_label("") == ""


def test_an_unknown_source_is_refused(plugin_dir: Path) -> None:
    m = mover(plugin_dir)
    cfg = _cfg(plugin_dir)
    reply = m.request_move(cfg, target="nas-shell", reason="", source="telepathy")
    assert "不知道这次是谁想搬" in reply.text
    assert m._proposal is None


def test_the_utterance_source_shows_up_in_the_confirm_note(plugin_dir: Path) -> None:
    """「去打会儿游戏吧」翻译成一次搬迁时，确认文案要说是**那句话**。"""
    m = mover(plugin_dir)
    cfg = _cfg(plugin_dir)
    reply = m.request_move(
        cfg,
        target="nas-shell",
        reason="主人说去打会儿游戏",
        source="utterance",
        quote="去打会儿游戏吧",
        said="你去打会儿游戏吧，我回 NAS 待着",
    )
    assert "nas-shell" in reply.text
    assert m._proposal is not None
    assert m._proposal[3] == "utterance"
    confirmed = m._confirm(cfg)
    assert confirmed.plan is not None
    assert "这次是因为你那句" in confirmed.text
    assert "去打会儿游戏吧" in confirmed.text


def test_a_trigger_this_version_does_not_understand_blocks_the_proposal(plugin_dir: Path) -> None:
    """写进配置但代码不认识的触发源 -> 拒绝，并指着那条配置说清楚。"""
    m = mover(plugin_dir)
    cfg = _cfg(plugin_dir, triggers={"cron": "0 3 * * *"})
    reply = m.request_move(cfg, target="nas-shell", reason="")
    assert "triggers.cron" in reply.text
    assert "还不认识" in reply.text
    assert m._proposal is None


def test_a_schedule_it_cannot_read_blocks_the_proposal(plugin_dir: Path) -> None:
    """`schedule` 已实装，但**读不懂的写法仍然阻塞** —— 不许当作"不到点"悄悄放行。"""
    m = mover(plugin_dir)
    cfg = _cfg(plugin_dir, triggers={"schedule": "每天 03:00"})
    reply = m.request_move(cfg, target="nas-shell", reason="")
    assert "triggers.schedule" in reply.text
    assert "读不懂" in reply.text
    assert m._proposal is None


def test_an_empty_triggers_slot_blocks_nothing(plugin_dir: Path) -> None:
    m = mover(plugin_dir)
    cfg = _cfg(plugin_dir, triggers={})
    reply = m.request_move(cfg, target="nas-shell", reason="")
    assert m._proposal is not None
    assert "nas-shell" in reply.text


def test_a_legacy_three_element_proposal_still_confirms(plugin_dir: Path) -> None:
    """v0.5 写下的三元组（没有来源字段）不能因为加了第四位就读不出来。"""
    m = mover(plugin_dir)
    cfg = _cfg(plugin_dir)
    m._proposal = (m._clock() + 60, "hansei", "nas-shell")
    reply = m._confirm(cfg)
    assert reply.plan is not None
    assert "这次是你提的" in reply.text

# ---- 2.2 定案 C 的钉子：它只能问，不能自己出门 ----

def test_a_proposal_only_ever_asks_and_never_runs(plugin_dir: Path) -> None:
    """提议存在，但**一个执行计划都没被造出来** —— 这是 C 的全部含义"""
    m = mover(plugin_dir)
    cfg = _cfg(plugin_dir)
    reply = m.request_move(
        cfg,
        target="nas-shell",
        reason="这边随时会被关掉",
        source="utterance",
        quote="我一会儿要打游戏",
        said="我一会儿要打游戏，你自己看着办",
    )
    assert m.has_proposal() is True
    assert reply.plan is None, "提议阶段不许有执行计划"
    assert "/molt confirm" in reply.text
    assert m._proposal[3] == "utterance"


def test_an_expired_proposal_simply_vanishes(plugin_dir: Path) -> None:
    """过期就是什么都没发生：不重试、不排队、不留待执行"""
    m = mover(plugin_dir)
    cfg = _cfg(plugin_dir)
    m.request_move(cfg, target="nas-shell", reason="", source="self")
    assert m.has_proposal() is True

    m._clock.advance(cfg.self_propose_ttl_seconds + 1)

    assert m.has_proposal() is False
    refused = m._confirm(cfg)
    assert refused.plan is None
    assert "过期" in refused.text or "失效" in refused.text


# ---- 「走」和「关」是两张票：第二张票的测试 ----

class _RecordOpener:
    """既能答 `/status`，又记下每一次 POST。"""

    def __init__(self, status_payload):
        self.status_payload = status_payload
        self.posts = []

    def __call__(self, req, timeout=None):
        url = getattr(req, "full_url", "")
        body = getattr(req, "data", None)
        if url.endswith("/status"):
            data = json.dumps(self.status_payload).encode("utf-8")
        else:
            self.posts.append((url, json.loads(body.decode("utf-8")) if body else {}))
            data = json.dumps({"ok": True}).encode("utf-8")

        class _R:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return data

        return _R()


def _just_arrived_payload() -> Dict[str, Any]:
    """我刚从 nas-shell 搬过来，现在是 win-shell 在说话。"""
    return {
        "status": {
            "current_epoch": 21,
            "lease": {"holder": None, "state": "no-lease"},
            "heartbeats": {"win-shell": {"age_seconds": 3}, "nas-shell": {"age_seconds": 9}},
            "latest_generation": {
                "generation": 19,
                "pushed_by": "nas-shell",
                "target_shell": "win-shell",
                "consumed_by": "win-shell",
            },
        }
    }


def test_the_power_question_is_a_separate_ticket(plugin_dir: Path) -> None:
    """`/molt shutdown` 投的是**另一张票**，带的是我**刚离开**的那台机器"""
    rec = _RecordOpener(_just_arrived_payload())
    m = MoltMover(plugin_dir, clock=FakeClock(), runner=lambda *a, **k: _Ok(), opener=rec)
    cfg = _cfg(plugin_dir)
    reply = m.handle("shutdown", sender="hansei")
    assert len(rec.posts) == 1
    url, body = rec.posts[0]
    assert url.endswith("/power")
    assert body == {"shell": "nas-shell", "action": "shutdown", "generation": 19, "by": "win-shell"}
    assert "nas-shell" in reply.text
    assert "/molt keep" in reply.text


def test_keep_is_also_a_ticket_not_a_silence(plugin_dir: Path) -> None:
    """「留着」也要落成一张票 —— 否则那台机器永远不知道能不能关"""
    rec = _RecordOpener(_just_arrived_payload())
    m = MoltMover(plugin_dir, clock=FakeClock(), runner=lambda *a, **k: _Ok(), opener=rec)
    cfg = _cfg(plugin_dir)
    reply = m.handle("keep", sender="hansei")
    assert rec.posts[0][1]["action"] == "keep"
    assert "留着不关" in reply.text


def test_it_refuses_to_guess_which_machine_to_shut_down(plugin_dir: Path) -> None:
    """这一代不是取的 → 我不知道你的旧壳是哪台 → 一张票都不投"""
    payload = _just_arrived_payload()
    payload["status"]["latest_generation"]["consumed_by"] = "somebody-else"
    rec = _RecordOpener(payload)
    m = MoltMover(plugin_dir, clock=FakeClock(), runner=lambda *a, **k: _Ok(), opener=rec)
    cfg = _cfg(plugin_dir)
    reply = m.handle("shutdown", sender="hansei")
    assert rec.posts == []
    assert "不猜" in reply.text


# ------------------------------------------------------------------ 可归因与定时


def _propose(m: MoltMover, cfg, **kw):
    """`request_move` 的薄包装：测试里只关心"提没提成、为什么没提成"。"""
    return m.request_move(cfg, **kw)


def test_an_utterance_without_a_quote_is_refused(plugin_dir):
    """说"这是主人那句话引出来的"，却拿不出那句话 —— 不许提。"""
    m = mover(plugin_dir)
    cfg = _cfg(plugin_dir)
    reply = _propose(m, cfg, source="utterance", reason="他说要打游戏")
    assert reply.plan is None
    assert m.has_proposal() is False
    assert "原话" in reply.text


def test_an_utterance_quoting_words_the_human_never_said_is_refused(plugin_dir):
    """引用必须能在**主人刚说的话**里找到 —— 编一句很像的话是最糟的那种糊弄。"""
    m = mover(plugin_dir)
    cfg = _cfg(plugin_dir)
    reply = _propose(
        m,
        cfg,
        source="utterance",
        reason="他说要打游戏",
        quote="你去 NAS 待着吧",
        said="我一会儿要打会儿游戏，你自己看着办",
    )
    assert reply.plan is None
    assert m.has_proposal() is False
    assert "不在主人刚说的话里" in reply.text


def test_a_quoted_utterance_keeps_the_quote_all_the_way_to_confirm(plugin_dir):
    m = mover(plugin_dir)
    cfg = _cfg(plugin_dir)
    said = "我一会儿要打会儿游戏，\n你去 NAS 待着吧，先说一声"
    reply = _propose(
        m, cfg, source="utterance", reason="他要占显卡", quote="你去 NAS 待着吧", said=said
    )
    assert reply.plan is None, "提议阶段不许造执行计划"
    assert m.has_proposal() is True
    assert m._proposal[3] == "utterance"
    assert m._proposal[4] == "你去 NAS 待着吧"

    confirmed = m.handle("confirm", sender="hansei")
    assert confirmed.plan is not None
    # 确认文案必须把**那句原话**还给主人看，而不是只说"你那句话"
    assert "你去 NAS 待着吧" in confirmed.text


def test_a_quote_survives_extra_whitespace(plugin_dir):
    """人打字会换行、会多空格；引用比对不能因此失配。"""
    m = mover(plugin_dir)
    cfg = _cfg(plugin_dir)
    reply = _propose(
        m,
        cfg,
        source="utterance",
        quote="去打会儿   游戏吧",
        said="好，那你去打会儿\n游戏吧，我回 NAS",
    )
    assert m.has_proposal() is True, reply.text


def test_self_proposals_do_not_need_a_quote(plugin_dir):
    """只有 `utterance` 这一档要证据；它自己想走不需要引用任何话。"""
    m = mover(plugin_dir)
    cfg = _cfg(plugin_dir)
    reply = _propose(m, cfg, source="self", reason="这边随时会被关机")
    assert m.has_proposal() is True
    assert m._proposal[3] == "self"
    assert m._proposal[4] == ""


# -- 定时：判据是纯函数，边界都被钉住


def test_schedule_accepts_the_short_and_the_long_form():
    assert cmd.schedule_spec("23:30") == {"minutes": 23 * 60 + 30, "window": 30, "days": None}
    assert cmd.schedule_spec(
        {"at": "3:05", "window_minutes": 10, "days": ["mon", "五"]}
    ) == {"minutes": 3 * 60 + 5, "window": 10, "days": {0, 4}}


@pytest.mark.parametrize(
    "broken",
    ["25:00", "23:60", "2330", "", None, {"at": "23:30", "window_minutes": 0},
     {"at": "23:30", "days": ["notaday"]}, {"at": "23:30", "days": []}, [1, 2]],
)
def test_schedule_rejects_what_it_cannot_read(broken):
    assert cmd.schedule_spec(broken) is None
    assert cmd.schedule_problem(broken) != ""


def _local_ts(year, month, day, hour, minute):
    import time as _t

    return _t.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


def test_schedule_window_matches_and_expires():
    # 2026-11-15 是周日
    assert cmd.schedule_due("23:30", _local_ts(2026, 11, 15, 23, 30)) is True
    assert cmd.schedule_due("23:30", _local_ts(2026, 11, 15, 23, 59)) is True
    assert cmd.schedule_due("23:30", _local_ts(2026, 11, 16, 0, 0)) is False
    assert cmd.schedule_due("23:30", _local_ts(2026, 11, 15, 22, 0)) is False


def test_schedule_window_crosses_midnight():
    """`23:45` + 30 分钟 覆盖到次日 00:15 —— 跨午夜必须算对。"""
    assert cmd.schedule_due({"at": "23:45", "window_minutes": 30},
                            _local_ts(2026, 11, 16, 0, 10)) is True
    assert cmd.schedule_due({"at": "23:45", "window_minutes": 30},
                            _local_ts(2026, 11, 16, 0, 20)) is False


def test_schedule_days_filter_is_respected():
    """2026-11-15 是周日（tm_wday=6）；只写周一就不该在周日到点。"""
    sunday = _local_ts(2026, 11, 15, 23, 30)
    assert cmd.schedule_due({"at": "23:30", "days": ["sun"]}, sunday) is True
    assert cmd.schedule_due({"at": "23:30", "days": ["mon"]}, sunday) is False


def test_schedule_says_i_dont_know_instead_of_guessing():
    """判不了就返回 None —— 不许把"判不了"说成"不到点"。"""
    assert cmd.schedule_due(None, _local_ts(2026, 11, 15, 23, 30)) is None
    assert cmd.schedule_due("乱写的", _local_ts(2026, 11, 15, 23, 30)) is None


def test_a_well_formed_schedule_no_longer_blocks_proposals(plugin_dir):
    """判据实装之后，配了定时不该再让所有提议都失败（以前会）。"""
    m = mover(plugin_dir)
    cfg = _cfg(plugin_dir, triggers={"schedule": "23:30"})
    reply = _propose(m, cfg, source="self", reason="测试")
    assert m.has_proposal() is True, reply.text


def test_a_broken_schedule_still_blocks_and_says_why(plugin_dir):
    m = mover(plugin_dir)
    cfg = _cfg(plugin_dir, triggers={"schedule": "25:99"})
    reply = _propose(m, cfg, source="self", reason="测试")
    assert m.has_proposal() is False
    assert "读不懂" in reply.text
