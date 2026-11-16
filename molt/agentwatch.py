"""怎么看"那个守护进程还活着吗" —— 只判断，不动手。

为什么要单独一个模块
--------------------
2026-11-17，Windows 上的 `molt-agent` 在一个晚上死了两次（01:19 死 25 分钟、
02:06 前又死一次）。它死掉的时候没有任何东西会响：

* `agent.log` 停在一行普通的 `[idle] 快照 #37 我已经落过了`
* `agent.log.err` 末尾是一个 `^C`
* 账本那边什么都不缺 —— 心跳只是慢慢变旧

而它偏偏是**早上 9 点那条自动搬家策略能不能触发的唯一前提**。所以"它死了"
这件事必须由别的东西发现。

判据（写死在这里，不散在脚本里）
--------------------------------
1. **进程不在** → `dead`。
2. **进程在、但状态文件不更新了** → 仍然算 `dead`。
   这是这一节最重要的一条：一个卡住的进程和一个死掉的进程，从外面看是一样的
   —— 都完成不了任何工作。`molt-agent` 每一轮都会原子写一次
   `molt-agent-state.json`，所以"落盘时间戳停了"就是"它不再干活了"。
3. **进程在、状态文件在动、但账本上心跳停了** → `silent`。
   这可能是心跳通道坏了（网络），也可能是它卡在某个长任务里。**不叫 dead**，
   因为落盘还在动说明主循环活着。
4. **两边都读不到** → `unknown`。宁可漏报也不误报：一个误报会让人学会忽略红条。

这个模块**不认识 PowerShell、不认识 Windows、不碰进程** —— 它只接收
"你看到的三件事"然后给一个判决。这样它能被桩测试完整覆盖，而真正的
探测器（`tools/agent-guard.ps1`）只负责读数和上报。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

#: 状态文件多久没更新就算"它不再干活了"。
#: 主循环一轮 10 秒，留 18 倍余量 —— 一次搬家/一次 boot 都不该超过这个数。
STATE_SILENT_SECONDS = 180.0

#: 账本上的心跳多久没更新算"看不见它"，比状态文件宽：心跳走网络，会有抖动。
HEARTBEAT_SILENT_SECONDS = 180.0

#: 三种判决 + 一个"我看不出来"。
ALIVE = "alive"
DEAD = "dead"
SILENT = "silent"
UNKNOWN = "unknown"

#: 两种判决各自对应的告警类别（写进账本的 `kind`）。
ALERT_DEAD = "agent-dead"
ALERT_SILENT = "agent-silent"


def judge(
    *,
    pid_alive: Optional[bool],
    state_age: Optional[float] = None,
    heartbeat_age: Optional[float] = None,
    state_limit: float = STATE_SILENT_SECONDS,
    heartbeat_limit: float = HEARTBEAT_SILENT_SECONDS,
) -> Dict[str, Any]:
    """根据"进程在不在 / 状态文件多久没动 / 心跳多久没动"给一个判决。

    参数都是"你自己看到的事实"，不是靠这个函数去查的：

    * `pid_alive`：`True` 找到进程，`False` 确定没有，`None` 读不到
      （权限不够、不是 Windows、探测脚本自己出错）。
    * `state_age`：状态文件最后一次落盘距今多少秒；`None` 表示读不到。
    * `heartbeat_age`：账本里这个壳最后一次心跳距今多少秒；`None` 表示没读到。

    返回 `{"state": ..., "reason": ..., "silent_for": ...}`，
    其中 `silent_for` 是"能证明它不动了"里最大的那个秒数（拿不到就是 None）。
    """

    ages = [a for a in (state_age, heartbeat_age) if a is not None]

    def _longest() -> Optional[float]:
        return max(ages) if ages else None

    # 1. 确定没有进程 —— 直接判死，不看时间戳（进程没了就是没了）。
    if pid_alive is False:
        return {
            "state": DEAD,
            "reason": "进程不在了",
            "silent_for": _longest(),
        }

    # 2. 进程在，但落盘停了 —— 这也是 dead。
    #    "进程还在"这个事实不能单独拿来当"它活着"的证据：一个卡死的进程
    #    在任务管理器里看起来和正常进程一模一样。
    if pid_alive is True and state_age is not None and state_age > state_limit:
        return {
            "state": DEAD,
            "reason": f"进程在，但状态文件已经 {int(state_age)} 秒没更新了 —— 它不再干活",
            "silent_for": state_age,
        }

    # 3. 进程在、落盘还在动，但心跳停了 —— 说实话，不确定。
    if (
        pid_alive is True
        and heartbeat_age is not None
        and heartbeat_age > heartbeat_limit
    ):
        return {
            "state": SILENT,
            "reason": (
                f"进程在、状态文件也还在写，但账本上已经 {int(heartbeat_age)} 秒没心跳"
                " —— 大概率和账本之间断了"
            ),
            "silent_for": heartbeat_age,
        }

    # 4. 读不到进程，只能靠时间戳说话：两者都新鲜就不算死。
    if pid_alive is None:
        stale = [
            age
            for age, limit in ((state_age, state_limit), (heartbeat_age, heartbeat_limit))
            if age is not None and age > limit
        ]
        if stale:
            return {
                "state": SILENT,
                "reason": (
                    "读不到进程状态，但"
                    + "、".join(f"{int(a)} 秒前最后一动" for a in stale)
                    + " —— 它大概率没在干活"
                ),
                "silent_for": max(stale),
            }
        return {
            "state": UNKNOWN,
            "reason": "读不到进程状态，时间戳也都不够旧 —— 不下结论",
            "silent_for": _longest(),
        }

    # 5. 进程在、时间戳都新鲜（或读不到时间戳）—— 活着。
    return {"state": ALIVE, "reason": "", "silent_for": _longest()}


def alert_kind(verdict: Dict[str, Any]) -> Optional[str]:
    """什么判决值得写一条告警；`alive` / `unknown` 都不写。"""
    state = str(verdict.get("state") or "")
    if state == DEAD:
        return ALERT_DEAD
    if state == SILENT:
        return ALERT_SILENT
    return None


def alert_message(verdict: Dict[str, Any], *, shell: str = "") -> str:
    """把判决写成一句人话（进账本、进 `/where` 的红条、进 `molt status`）。"""
    where = f"（{shell}）" if shell else ""
    state = str(verdict.get("state") or "")
    reason = str(verdict.get("reason") or "")
    if state == DEAD:
        tail = "它不在，早上那条自动搬家策略就不会触发。"
    elif state == SILENT:
        tail = "它还在，但账本上看不见它 —— 先看看网络。"
    else:
        return ""
    return f"这台机器上的 molt-agent{where}出问题了：{reason}。{tail}"
