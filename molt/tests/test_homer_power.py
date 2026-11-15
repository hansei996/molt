"""「走」和「关」是两件事 —— 但账本宿主（homer）永远不执行关机。

这个文件钉的是 NAS 那一侧的判断：

- 账本里有一张 `keep` 票 → 如实说"你说过留着"，并**把票划掉**（不再提）
- 账本里有一张 `shutdown` 票 → **拒绝执行**，而且**不划掉它**（划掉就等于假装处理了）
- 没有票 / 判不了 → `ask`：什么都不做，也不冒充"人说过别关"

为什么 homer 不能关自己：账本就在这台机器的文件系统上。把它关掉之后，
另一台壳再也读不到「鱼现在在哪」—— 它不会知道自己该接管，只会一直等。
**一个把账本一起关掉的关机，不是"机器休息"，是"整个 molt 停摆"。**
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOMER_PATH = ROOT / "tools" / "molt-homer.py"


def _load_homer():
    spec = importlib.util.spec_from_file_location("molt_homer_power_under_test", HOMER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # 必须先登记进 sys.modules 再 exec：文件里有 `from __future__ import annotations`，
    # `@dataclass` 解析字符串注解时会 `sys.modules.get(cls.__module__).__dict__`。
    sys.modules["molt_homer_power_under_test"] = module
    spec.loader.exec_module(module)
    return module


homer_mod = _load_homer()

SHELL = "nas-shell"


def ledger(*, generation=19, power=None, consumed_by=SHELL):
    payload = {
        "current_epoch": 21,
        "lease": {"holder": None, "state": "no-lease"},
        "latest_generation": {
            "generation": generation,
            "target_shell": SHELL,
            "consumed_by": consumed_by,
            "pushed_by": "win-shell",
        },
    }
    if power is not None:
        payload["power"] = power
    return payload


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    def __init__(self, running=True):
        self.calls = []
        self.running = running

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:1] == ["docker"] and argv[1:2] == ["inspect"]:
            return FakeProc(0, "true\n" if self.running else "false\n")
        return FakeProc(0, "")


def settings(**kw):
    base = dict(
        execute=True,
        shell=SHELL,
        container="astrbot",
        data_dir="/data",
        molt_pkg="/pkg",
        python="python3",
        platform_switch="/pkg/tools/platform-switch.py",
        warden_url="http://127.0.0.1:6186",
        warden_token="tok",
        interval=0.01,
        timeout=1.0,
        stop_timeout=1.0,
    )
    base.update(kw)
    return homer_mod.Settings(**base)


def make_homer(tmp_path, *, status, cfg=None, ack=None, logs=None):
    logs = logs if logs is not None else []
    return homer_mod.Homer(
        settings=cfg or settings(),
        state_path=tmp_path / homer_mod.STATE_FILENAME,
        runner=FakeRunner(running=True),
        status_fetcher=lambda: status,
        log=logs.append,
        power_ack_sender=ack,
    )


# ------------------------------------------------------------------ verdict


def test_a_shutdown_ticket_is_never_executed_here():
    """账本宿主没有 shutdown 这一档 —— 不是"忘了实现"，是刻意的。"""
    verdict = homer_mod.power_verdict(
        requires_permission=True,
        tickets={SHELL: {"action": "shutdown"}},
        shell=SHELL,
    )
    assert verdict == "refuse"


def test_a_keep_ticket_is_honoured():
    assert homer_mod.power_verdict(
        requires_permission=True, tickets={SHELL: {"action": "keep"}}, shell=SHELL
    ) == "keep"


def test_no_ticket_means_ask_not_keep():
    """没有票 = 没人回答过这个问题，不等于"人说了别关"。"""
    assert homer_mod.power_verdict(requires_permission=True, tickets={}, shell=SHELL) == "ask"
    assert homer_mod.power_verdict(
        requires_permission=True, tickets=None, shell=SHELL
    ) == "ask"


def test_somebody_elses_ticket_does_not_touch_this_machine():
    assert homer_mod.power_verdict(
        requires_permission=True,
        tickets={"other-shell": {"action": "shutdown"}},
        shell=SHELL,
    ) == "ask"


def test_a_broken_ticket_file_degrades_to_ask():
    """`power.json` 读坏了要当"没票"，绝不能当"可以关"。"""
    assert homer_mod.power_verdict(
        requires_permission=True, tickets="not-a-dict", shell=SHELL
    ) == "ask"
    assert homer_mod.power_verdict(
        requires_permission=True, tickets={SHELL: "garbage"}, shell=SHELL
    ) == "ask"


def test_permission_off_does_not_turn_into_a_shutdown_here():
    """即使配置成"不用许可"，homer 也不会关自己 —— 它没有那一档。"""
    assert homer_mod.power_verdict(
        requires_permission=False,
        tickets={SHELL: {"action": "shutdown"}},
        shell=SHELL,
    ) == "refuse"


# ------------------------------------------------------------------ run_once


def test_keep_is_acknowledged_so_it_stops_asking(tmp_path):
    acked = []
    logs = []
    h = make_homer(
        tmp_path,
        status=ledger(power={SHELL: {"action": "keep", "generation": 19}}),
        ack=lambda shell: acked.append(shell) or True,
        logs=logs,
    )
    h.run_once()
    assert acked == [SHELL]
    assert any("留着这台机器" in line for line in logs)


def test_a_shutdown_ticket_is_reported_but_left_untouched(tmp_path):
    """拒绝执行，并且**不划票** —— 划掉就等于假装处理了，而那个决定不是我的。"""
    acked = []
    logs = []
    h = make_homer(
        tmp_path,
        status=ledger(power={SHELL: {"action": "shutdown", "generation": 19}}),
        ack=lambda shell: acked.append(shell) or True,
        logs=logs,
    )
    h.run_once()
    assert acked == []
    assert any("账本所在的机器" in line for line in logs)


def test_the_refusal_is_only_said_once(tmp_path):
    """每 10 秒重复同一句抱怨会淹掉日志 —— 同一张票只记一次。"""
    logs = []
    ack = lambda shell: False  # noqa: E731 - 只是"没有人来划票"
    h = make_homer(
        tmp_path,
        status=ledger(power={SHELL: {"action": "shutdown", "generation": 19}}),
        ack=ack,
        logs=logs,
    )
    h.run_once()
    first = [line for line in logs if "账本所在的机器" in line]
    h.run_once()
    second = [line for line in logs if "账本所在的机器" in line]
    assert len(first) == 1
    assert len(second) == 1, "第二轮不该再重复一遍"


def test_no_ticket_keeps_the_machine_quietly(tmp_path):
    """没票时**什么都不做**：不销票、也不记"关机"之类的日志。"""
    acked = []
    logs = []
    h = make_homer(tmp_path, status=ledger(), ack=lambda shell: acked.append(shell) or True,
                   logs=logs)
    h.run_once()
    assert acked == []
    assert not any("关机" in line or "留着这台机器" in line for line in logs)


def test_ack_failure_does_not_break_the_handover(tmp_path):
    """销票失败只是账本上一行注记没写成 —— 不许把交接流程带崩。"""
    def boom(shell):
        raise RuntimeError("账本连不上")

    logs = []
    h = make_homer(
        tmp_path,
        status=ledger(power={SHELL: {"action": "keep", "generation": 19}}),
        ack=boom,
        logs=logs,
    )
    h.run_once()  # 不抛
    assert any("销票失败" in line for line in logs)


def test_the_two_daemons_agree_on_keep_and_ask():
    """agent 与 homer 各有一份 `power_verdict`（守护进程不能 import 同一个模块）。

    两份实现必须对 **keep / ask** 两个结论一致，否则"人说了留着"在一台机器上生效、
    在另一台上被当成没说过 —— 这种分歧没有任何日志会指出。
    """
    agent_spec = importlib.util.spec_from_file_location(
        "molt_agent_power_under_test", ROOT / "tools" / "molt-agent.py"
    )
    agent = importlib.util.module_from_spec(agent_spec)
    assert agent_spec.loader is not None
    sys.modules["molt_agent_power_under_test"] = agent
    agent_spec.loader.exec_module(agent)

    for tickets in ({SHELL: {"action": "keep"}}, {}, None, {"other": {"action": "keep"}}):
        a = agent.power_verdict(
            requires_permission=True, tickets=tickets, shell=SHELL
        )
        h = homer_mod.power_verdict(
            requires_permission=True, tickets=tickets, shell=SHELL
        )
        assert a == h, f"票={tickets!r} 时 agent 说 {a}，homer 说 {h}"
