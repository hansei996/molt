"""M2：定时自动搬家 —— "到点了"到底算不算数。

这里只有**判断**，没有执行。执行永远是同一条 `molt migrate`，
和手动那一次走的是同一段代码（`request_move()` 那条规矩：新增触发源
不该新增执行路径）。所以这个模块不 import 任何守护进程，也不碰网络。

三道保险写在这儿：

1. **范围封闭**：一条策略只能写死"哪台壳 → 哪台壳 + 什么时刻 + 每天几次
   + 什么时候过期"。没有"随便搬"这种模式。
2. **判不了就是不许搬**：定时规则读不懂、盘上时间读不出来、账本读不到，
   一律返回 `False`（附一句人话），绝不返回 True。
3. **冷却**：上一次自动尝试之后 `COOLDOWN_SECONDS` 内不再试。
   迁移本身要 1–3 分钟，而守护进程 10 秒一轮 —— 没有冷却的话
   一次失败会在一分钟里重试六次，把对面机器反复吵醒。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from .schedule import schedule_due, schedule_problem, schedule_spec

#: 一条策略默认每天最多自动搬几次。
#: 为什么是 2 而不是 3：M2 的授权是"人不在场时也生效"，
#: 而每一次搬家都有 1–3 分钟的静默窗口。一天两次已经够表达
#: "白天在桌面、夜里回 NAS"；再多的自动搬家更像抖动，不像生活。
DEFAULT_DAILY_LIMIT = 2

#: 两次自动尝试之间至少隔多久（秒）。
COOLDOWN_SECONDS = 300.0

#: 策略里允许出现的键。多出来的键一律拒 —— 打错一个字母就静默不生效，
#: 比直接报错难查得多。
POLICY_FIELDS = (
    "id",
    "enabled",
    "source",
    "target",
    "schedule",
    "window_minutes",
    "daily_limit",
    "expires_at",
    "created_at",
    "created_by",
)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_policy(raw: Any) -> Dict[str, Any]:
    """把一条策略整成固定形状。**不校验**（校验在 `policy_problems`）。"""
    data = dict(raw) if isinstance(raw, dict) else {}
    return {
        "id": str(data.get("id") or "").strip(),
        "enabled": bool(data.get("enabled", True)),
        "source": str(data.get("source") or "").strip().lower(),
        "target": str(data.get("target") or "").strip().lower(),
        "schedule": data.get("schedule", ""),
        "window_minutes": _as_int(data.get("window_minutes"), 0),
        "daily_limit": _as_int(data.get("daily_limit"), DEFAULT_DAILY_LIMIT),
        "expires_at": float(data.get("expires_at") or 0.0),
        "created_at": float(data.get("created_at") or 0.0),
        "created_by": str(data.get("created_by") or "").strip(),
    }


def policy_problems(raw: Any) -> List[str]:
    """这条策略能不能存。空列表 = 能。"""
    if not isinstance(raw, dict):
        return ["策略得是一个对象（JSON dict）"]

    problems: List[str] = []
    unknown = [k for k in raw if k not in POLICY_FIELDS]
    if unknown:
        problems.append(f"不认识的键：{', '.join(sorted(unknown))}")

    policy = normalize_policy(raw)
    if not policy["id"]:
        problems.append("缺 id（给它起个名字，比如 `night-home`）")
    if not policy["source"]:
        problems.append("缺 source（在哪台壳上执行，比如 `ugreen-776b-8b7896df`）")
    if not policy["target"]:
        problems.append("缺 target（搬去哪台壳）")
    if policy["source"] and policy["target"] and policy["source"] == policy["target"]:
        problems.append("source 和 target 是同一台壳 —— 那不用搬")

    if schedule_spec(policy["schedule"]) is None:
        problems.append(f"schedule {schedule_problem(policy['schedule'])}")

    if policy["window_minutes"]:
        if not (1 <= policy["window_minutes"] <= 24 * 60):
            problems.append("window_minutes 要在 1–1440 之间")

    if not (1 <= policy["daily_limit"] <= 24):
        problems.append("daily_limit 要在 1–24 之间（一天最多自动搬几次）")

    if policy["expires_at"] and policy["expires_at"] < time.time():
        problems.append("expires_at 已经过去了 —— 这条策略一存就是过期的")

    return problems


def attempts_today(attempts: Any, *, now: Optional[float] = None) -> int:
    """今天（本机时区）已经自动尝试过几次。读不懂的条目不算。"""
    today = time.strftime("%Y-%m-%d", time.localtime(time.time() if now is None else now))
    count = 0
    for item in attempts or []:
        if not isinstance(item, dict):
            continue
        stamp = item.get("at_readable") or ""
        if str(stamp).startswith(today):
            count += 1
    return count


def _last_attempt(attempts: Any) -> Optional[Dict[str, Any]]:
    best: Optional[Dict[str, Any]] = None
    for item in attempts or []:
        if not isinstance(item, dict):
            continue
        if best is None or float(item.get("at") or 0) > float(best.get("at") or 0):
            best = item
    return best


def due(
    policy: Any,
    *,
    now: float,
    shell: str,
    attempts: Any = (),
) -> Tuple[bool, str]:
    """现在该不该自动搬一次。返回 `(要不要搬, 为什么)`。

    **判不了就是不许搬。** 这句在整个项目里出现过很多次，这里是它的
    第四处：`schedule_due` 返回 `None`、账本时间读不出来、策略形状不对，
    全部走 `False` 分支。一个"永远为真"的自动搬家比没有这个功能糟得多。
    """
    data = normalize_policy(policy)
    now = float(now)
    shell = str(shell or "").strip().lower()

    if not data["id"]:
        return False, "账本里没有自动搬家策略"
    if not data["enabled"]:
        return False, "自动搬家策略是关着的"
    if data["source"] != shell:
        return False, f"这条策略在 {data['source'] or '?'} 上执行，不是这台"
    if data["target"] == shell:
        return False, "目标就是自己，不用搬"
    if data["expires_at"] and now > data["expires_at"]:
        return False, "策略已经过期"
    if schedule_spec(data["schedule"]) is None:
        return False, f"定时规则读不懂：{schedule_problem(data['schedule'])}"

    when = schedule_due(data["schedule"], now)
    if when is None:
        return False, "定时规则读不懂（判不了）"
    if not when:
        return False, "还没到点"

    done = attempts_today(attempts, now=now)
    if done >= data["daily_limit"]:
        return False, f"今天已经自动试过 {done} 次，上限 {data['daily_limit']}"

    last = _last_attempt(attempts)
    if last:
        gap = now - float(last.get("at") or 0)
        if gap < COOLDOWN_SECONDS:
            return False, f"上一次自动尝试在 {int(gap)} 秒前，冷却中"

    return True, "到点了"
