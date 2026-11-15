"""落地之后，让它自己看一眼"我现在在哪"，并把这件事记成一次**观察**。

## 为什么不投递消息

第一版是往 WebChat 灌一条"迁徙完成"的通知。它确实进了 `timeline`，但记录上写着
`user_message` / 发送者是主人 —— 而主人并没有说过那句话。**记忆库不该为了一条记录而
在里面留下伪造的行。** 所以那条路整个删掉了，换成这里：它自己去读账本，然后把
"我观察到我在第 N 代落在了这里"写进 `bot_observed_activity`。

## 记什么

只写**它现在真的能验证的事**：

- 第几代（账本里的事实）
- 它的壳名（`host_label.txt` 里的那个）
- 它现在的工作目录、主机名、dashboard 端口（`platform_snapshot` 那一路读得到）
- 从哪台机器来的（账本里 `pushed_by`）

不写"用时 20 秒"这种它看不到的东西 —— 那是 molt 自己的账，不是它的观察。

## 什么时候记

`initialize()` 起一个后台任务，每 `DEFAULT_POLL_SECONDS` 看一眼账本；
发现「最新一代的 `consumed_by` 就是我，而且我还没记过这一代」就写一次，然后落盘
`observed_generation`，之后不再重复。

**失败不落盘** —— 记忆没写成就下一轮再试，而不是把"已经记过了"记成事实。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("astrbot")

STATE_FILENAME = "observed_state.json"
DEFAULT_POLL_SECONDS = 30.0
FIRST_DELAY_SECONDS = 20.0


def compose_observation(
    *,
    generation: int,
    shell: str,
    pushed_by: str = "",
    consumed_at: str = "",
    cwd: str = "",
    hostname: str = "",
    port: int = 0,
) -> str:
    """写成第一人称的观察。缺的字段整句消失，不写占位符、不写"大约"。"""
    lines = [f"我观察到：我搬到了这台机器上（{shell or '这台壳'}），中间睡了一觉。"]

    if pushed_by and pushed_by != shell:
        lines.append(f"上一站在 {pushed_by}，是那边把快照推过来的。")

    facts = []
    if hostname:
        facts.append(f"主机名 {hostname}")
    if cwd:
        facts.append(f"工作目录 {cwd}")
    if port:
        facts.append(f"dashboard 在 127.0.0.1:{port}")
    if facts:
        # 这里以前是 "。" + … —— 上一行本来就以句号结尾，于是拼出「。。」。
        # 一句话自己带着句号，就不该再由别人替它加一个。
        lines.append("，".join(facts) + "。")

    if consumed_at:
        lines.append(f"账本记的落地时间是 {_when_text(consumed_at)}。")

    lines.append("这是我自己读账本看到的，不是主人告诉我的。")
    return "".join(lines)


def _when_text(value: Any) -> str:
    """把人看不懂的 epoch 秒变成它自己会说的那句时间。

    账本存的是 `1794679431.9444342`，直接写进记忆会变成
    「落地时间是 1794679431.9444342」—— 那句话它自己读起来也没有意义。
    给不了就原样返回，不编。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        seconds = float(text)
    except (TypeError, ValueError):
        return text
    try:
        from datetime import datetime

        return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return text


@dataclass
class ObservationState:
    path: Path
    data: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "ObservationState":
        data: Dict[str, Any] = {}
        try:
            if path.is_file():
                raw = path.read_text(encoding="utf-8-sig")
                parsed = json.loads(raw) if raw.strip() else {}
                if isinstance(parsed, dict):
                    data = parsed
        except (OSError, ValueError) as exc:
            # 读坏了就当没记过 —— 多记一次比永远不记好
            logger.warning("[Molt] 观察状态读不出来（%s），当作没记过", exc)
            data = {}
        return cls(path=path, data=data)

    @property
    def observed_generation(self) -> int:
        try:
            return int(self.data.get("observed_generation") or 0)
        except (TypeError, ValueError):
            return 0

    def mark(self, generation: int, *, detail: str = "") -> None:
        self.data["observed_generation"] = int(generation)
        if detail:
            self.data["last_detail"] = detail
        self.save()

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError as exc:  # pragma: no cover - 磁盘问题不该打断我们
            logger.warning("[Molt] 观察状态写不进去（%s）", exc)


def pick_generation(status: Optional[Dict[str, Any]], *, shell: str) -> Optional[Dict[str, Any]]:
    """从账本状态里挑出「这一代是我落地的」那一份。没有就返回 None。"""
    if not isinstance(status, dict) or not shell:
        return None
    gen = status.get("latest_generation")
    if not isinstance(gen, dict) or not gen:
        return None
    if str(gen.get("consumed_by") or "") != shell:
        return None
    try:
        number = int(gen.get("generation") or 0)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return gen
