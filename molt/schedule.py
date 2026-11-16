"""定时规则的纯函数：`"23:30"` 到底算不算"到点了"。

这些函数原来住在 `plugin/command.py` 里，只有那座壳上的插件能用；
M2（定时自动搬家）之后守护进程也要用同一条规则，所以搬到这里。
**只放在这里一份** —— 两处各写一遍，迟早会有一条规则在两个地方意思不同。
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Optional, Set, Tuple


#: 定时触发的默认窗口：说 `23:30` 时，指的是 23:30 之后这半小时之内。
#: 为什么不是"只在那一分钟"：家用机器的时钟、守护进程 10 秒一轮的轮询、
#: 以及人不可能每次都把时刻掐到分钟，都让"恰好 23:30:00"变成一个不现实的判据。
SCHEDULE_DEFAULT_WINDOW_MINUTES = 30

#: 星期名 → Python 的 `tm_wday`（周一 = 0）。
_WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
}


def _parse_hhmm(text: Any) -> Optional[Tuple[int, int]]:
    """`"23:30"` / `"3:05"` → `(23, 30)`；`"25:00"` 这种不合法返回 None。"""
    raw = str(text or "").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def schedule_spec(trigger: Any) -> Optional[Dict[str, Any]]:
    """把 `triggers.schedule` 归一成 `{"minutes": 当天第几分钟, "window": 分钟数, "days": set|None}`。

    接受两种写法，因为人写配置的心情不一样：

    - 短写：`"23:30"`
    - 长写：`{"at": "23:30", "window_minutes": 45, "days": ["mon", "tue"]}`

    解析不出来返回 None —— **调用方必须把它当成"我不知道"，不是"不到点"**。
    """
    if isinstance(trigger, str):
        trigger = {"at": trigger}
    if not isinstance(trigger, dict):
        return None
    at = _parse_hhmm(trigger.get("at"))
    if at is None:
        return None
    window = trigger.get("window_minutes", SCHEDULE_DEFAULT_WINDOW_MINUTES)
    try:
        window = int(window)
    except (TypeError, ValueError):
        return None
    if not (1 <= window <= 24 * 60):
        return None
    days_raw = trigger.get("days")
    days: Optional[Set[int]] = None
    if days_raw is not None:
        if not isinstance(days_raw, (list, tuple)) or not days_raw:
            return None
        days = set()
        for item in days_raw:
            if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 6:
                days.add(int(item))
                continue
            key = str(item or "").strip().lower()
            if key in _WEEKDAYS:
                days.add(_WEEKDAYS[key])
                continue
            if key.isdigit() and 0 <= int(key) <= 6:
                days.add(int(key))
                continue
            return None
    return {"minutes": at[0] * 60 + at[1], "window": window, "days": days}


def schedule_problem(trigger: Any) -> str:
    """这条定时规则能不能用；能用返回空串，不能用返回一句人话。"""
    if schedule_spec(trigger) is not None:
        return ""
    return (
        '要一个时刻，比如 `"23:30"` 或 '
        '`{"at": "23:30", "window_minutes": 30, "days": ["mon"]}`，'
        f"我读到的是 `{trigger!r}`"
    )


def schedule_due(trigger: Any, now: float) -> Optional[bool]:
    """按**本机时区**判断"现在是不是在那个窗口里"。

    返回 `None` 表示"判不了"（没配、或配坏了）—— 调用方不许把它当成 False 或 True。
    窗口可以跨午夜（`"23:30"` + 30 分钟 = 到次日 00:00），所以比较是在"一天第几分钟"上做的。
    """
    spec = schedule_spec(trigger)
    if spec is None:
        return None
    lt = time.localtime(now)
    if spec["days"] is not None and lt.tm_wday not in spec["days"]:
        return False
    minutes = lt.tm_hour * 60 + lt.tm_min
    start = int(spec["minutes"])
    span = int(spec["window"])
    return ((minutes - start) % (24 * 60)) < span


