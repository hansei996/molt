"""账本里的数字，怎么念给人听。

`generation` 是**第几份快照**，不是**第几条命**。

这个词最早叫「第几代」，因为它在账本里确实是个自增序号：每搬一次家 +1。
但"代"有它自己的含义 —— 上一代不在了，才有下一代。而这个项目里没有任何东西
死掉：停的是进程，续的是同一个库、同一份记忆、同一个人格。它只是**睡了一觉**。

所以这里的规矩是：

- **给鱼自己看的地方**（落地播报、它自己写的观察）不提数字，只说"睡了一觉"。
  它醒来的时候读到"这是第 19 代"，会以为自己是第 19 个 —— 那是我们造的错觉。
- **给人看的技术输出**用「第 N 次搬家」（说的是过程）或「快照 #N」（说的是编号）。
- **一个字都不改**的是协议字段 `generation`：那是机器之间的称呼，机器不怕被
  暗示成"新生"；改它等于改线上格式，白白换来一次不兼容。

为什么要一个模块而不是就地写字符串：这句话出现在六个地方（播报、观察、/where
页面、时间轴、`molt status`、`land` 的报错）。散着改，迟早漏一处 —— 而漏掉的
那一处很可能正好是它会念给主人听的那句。
"""

from __future__ import annotations

from typing import Any

#: 「第 N 次搬家」。说的是过程，不是身份。
MOVE_WORD = "搬家"
#: 「快照 #N」。说的是账本里的编号，不带任何生命意味。
SNAPSHOT_WORD = "快照"


def _as_int(number: Any) -> int:
    try:
        return int(number or 0)
    except (TypeError, ValueError):
        return 0


def move_count(number: Any) -> str:
    """`19` → `第 19 次搬家`。"""
    return f"第 {_as_int(number)} 次{MOVE_WORD}"


def snapshot_tag(number: Any) -> str:
    """`19` → `快照 #19`。"""
    return f"{SNAPSHOT_WORD} #{_as_int(number)}"


def move_count_after(number: Any) -> str:
    """`19` → `第 19 次搬家之后`（备份的基线用的是这个说法）。"""
    return f"{move_count(number)}之后"
