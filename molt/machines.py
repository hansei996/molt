"""把「哪台机器」变成可以少打字的东西。

## 为什么要有这个模块

2026-11-18 00:54，主人看着 `/molt machine on desktop-h2kgiao` 说了一句：

> 这也太难写了吧

他说得对。壳名是 `desktop-h2kgiao` 这种机器名 —— 它适合当账本主键
（唯一、稳定、不会变），不适合当人从手机上敲的东西。人想说的是
「那台台式机」，不是「主机名」。

所以这里只做一件事：把"人怎么指一台机器"与"账本里怎么记"分开。

- 编号：`list` 会打 `1. / 2.`，指人的时候可以只说 `1`
- 完整名字：老写法继续有效
- 前缀：`desk` 够唯一就行
- 片段：`h2kgiao` 也行

**有歧义就不猜** —— 报出来让人选，而不是挑一个最像的。机器是会被
关机、被叫醒、被推快照的东西，猜错一次的代价不是重输一遍命令。
"""

from __future__ import annotations

from typing import Dict, List, Tuple


def ordered_shells(shells: Dict[str, Dict]) -> List[str]:
    """列表里机器的固定顺序 —— 编号必须跨次调用稳定。

    按壳名排序是刻意的：账本里插一条新壳时，已经有编号的旧壳不该
    跟着换号（按插入顺序或者按心跳时间排都会换）。人记的是"2 号"，
    改天它变成"3 号"，这条便签就变成了一个陷阱。
    """
    return sorted(str(name) for name in (shells or {}))


def alive_text(age_seconds) -> str:
    """心跳多久之前 —— 25 秒以内算"在跑"（与 `molt declare`/看门狗同一判据）。"""
    try:
        seconds = float(age_seconds)
    except (TypeError, ValueError):
        return "没上报心跳"
    if seconds <= 25:
        return f"在跑（{seconds:.0f} 秒前）"
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分钟没心跳"
    return f"{seconds / 3600:.1f} 小时没心跳"


def resolve(text: str, shells: Dict[str, Dict]) -> Tuple[str, str]:
    """人说的那一小段，对上账本里的哪一台。返回 `(壳名, 错误)`。

    对上了返回 `(名字, "")`；对不上返回 `("", 一句人话)`。
    绝不返回"最像的那一台"。
    """
    order = ordered_shells(shells)
    raw = str(text or "").strip()
    if not raw:
        return "", "没说是哪台机器。"
    if not order:
        return "", "账本里还没有任何机器。"

    # 1) 序号（1 起）
    if raw.isdigit():
        index = int(raw)
        if 1 <= index <= len(order):
            return order[index - 1], ""
        return "", f"没有第 {index} 台 —— 现在一共 {len(order)} 台。"

    lowered = raw.lower()
    exact = [name for name in order if name.lower() == lowered]
    if len(exact) == 1:
        return exact[0], ""
    if len(exact) > 1:  # 理论上不会（壳名是主键），但别假设
        return "", f"「{raw}」对上了不止一台：{'、'.join(exact)}"

    prefix = [name for name in order if name.lower().startswith(lowered)]
    if len(prefix) == 1:
        return prefix[0], ""
    if len(prefix) > 1:
        return "", f"「{raw}」能对上 {len(prefix)} 台：{'、'.join(prefix)} —— 说得再具体一点。"

    inside = [name for name in order if lowered in name.lower()]
    if len(inside) == 1:
        return inside[0], ""
    if len(inside) > 1:
        return "", f"「{raw}」能对上 {len(inside)} 台：{'、'.join(inside)} —— 说得再具体一点。"

    return "", f"账本里没有「{raw}」这台机器。发 `/molt machine` 看一遍（带编号）。"


def power_text(*, alive: bool, wakeable, has_mac: bool) -> str:
    """这台机器现在该说"已开机"还是"关着 · 可唤醒"。

    2026-11-18 01:11 主人看着列表说：「desktop 是已经开机的状态吧，不需要唤醒」——
    他说得对。`可唤醒` 是**关着的时候**的属性；一台正在报心跳的机器被标成
    "可唤醒"，会让人以为它没开。所以这里按"现在开没开"分两句话，而不是
    把账本里的静态属性直接念出来。
    """
    if alive:
        return "已开机"
    if wakeable is True:
        return "关着 · 可唤醒（没登记 MAC）" if not has_mac else "关着 · 可唤醒"
    if wakeable is False:
        return "关着 · 叫不醒"
    return "没在跑（账本没登记能不能叫醒）"
