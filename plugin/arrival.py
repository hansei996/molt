"""落地之后的第一句话，前面带一句「我刚从哪儿过来」。

## 为什么挂在这句话上

想知道"它现在在 NAS 还是 Windows"，有三条路（`docs/SELFUSE.md`「它现在在哪」）：
敲一句 `/molt where`、打开一页 `http://<NAS>:6186/where`、跑 `molt status`。
三条都是**拉**——都得有人去问。

QQ 官方机器人的资料页、昵称、签名只能在 QQ 开放平台后台改，程序没有接口，
所以"把状态挂在资料上"这条路根本不存在。剩下的能**推**到人眼前的，只有它
自己说的那句话。

这条就挂在它说的那句话上：搬家落地之后的**第一句回复**，前面多一行

    （我刚从 UGREEN-776B 那边过来 —— 中间睡了一觉。）

## 为什么不用一条通知消息

试过（`molt announce`，已删除）。那条消息确实进了 `timeline`，但记录上写的
发送者是主人，而主人并没有说过那句话 —— 记忆库不该为了显示一行状态而在里面
留下伪造的行。挂在它自己的回复上不一样：那本来就是它说的话，只是多了一行。

## 三条自我约束

1. **只挂一次**。同一次落地只挂第一句，之后 `announced_generation` 落盘，不重复。
2. **过期不认**。落地超过 `DEFAULT_FRESH_SECONDS` 才有人跟它说话，它就不再说
   "我刚搬过来" —— 那是假话。宁可不说，也不要说错。
3. **问不到账本就闭嘴**。读不到账本时不猜、不编，只是不加这一行。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("astrbot")

STATE_FILENAME = "arrival_state.json"

#: 落地多久之内还自称"刚搬过来"。30 分钟：够一次迁徙后主人第一句问候，
#: 又短到不至于第二天早上还说"我刚搬来"。
DEFAULT_FRESH_SECONDS = 1800.0


def compose_arrival_note(
    *, generation: int, from_label: str = "", from_shell: str = ""
) -> str:
    """那句话本身。末尾带换行，因为它要接在真正的回复前面。"""
    source = (from_label or "").strip() or prettify_shell(from_shell)
    if source:
        return f"（我刚从 {source} 那边过来 —— 中间睡了一觉。）\n"
    return f"（我刚搬到这台机器上 —— 中间睡了一觉。）\n"


#: 旧壳多久没报心跳就不再问"要不要关它"。10 分钟：和 `/where` 的 stale 阈值同源。
SHELL_STILL_UP_SECONDS = 600.0


def compose_power_question(*, old_label: str = "", old_shell: str = "") -> str:
    """旧壳还开着时，落地那句话后面追加的**问句**。

    为什么要问：`/molt shutdown` 一直存在，但它要求人**先想到**去敲。
    落地那一刻是唯一自然的提问时机 —— 人刚看着它从那边走到这边，
    旧壳还亮着，正是"要不要关掉它"最不需要解释的时候。
    措辞里给了**两个**出口（关 / 留着），因为这是一个真问题，不是一个提示。
    """
    name = (old_label or "").strip() or prettify_shell(old_shell)
    if not name:
        return ""
    return f"（{name} 还开着 —— 要关它就回 /molt shutdown，留着就回 /molt keep。）\n"


def should_ask_about_power(
    status: Optional[Dict[str, Any]],
    *,
    shell: str,
    from_shell: str,
    fresh_seconds: float = SHELL_STILL_UP_SECONDS,
) -> bool:
    """该不该问那句「旧壳要不要关」。

    三个条件缺一不可：①旧壳不是我自己 ②它现在还在报心跳（关机/断电的不问）
    ③账本里还没有它的关机票（已经答过的不再问第二遍）。

    **问不到就闭嘴**：读不到心跳、读不到票，一律不问 —— 宁可少问一次，
    也不要对着一个已经关掉的机器反复说"它还开着"。
    """
    if not isinstance(status, dict):
        return False
    old = (from_shell or "").strip()
    if not old or old == (shell or "").strip():
        return False
    beats = status.get("heartbeats") or {}
    beat = beats.get(old) if isinstance(beats, dict) else None
    if not isinstance(beat, dict):
        return False
    try:
        age = float(beat.get("age_seconds"))
    except (TypeError, ValueError):
        return False
    if age < 0 or age > fresh_seconds:
        return False
    power = status.get("power") or {}
    if isinstance(power, dict) and old in power:
        return False
    return True


def prettify_shell(shell: str) -> str:
    """`ugreen-776b-8b7896df` → `ugreen-776b`。

    只是去掉尾部那段八位十六进制（`shell.py` 拼 shell_id 时加的短哈希）。
    **不猜成"NAS"这类昵称** —— 猜错的名字比一个丑名字更糟，它会让人以为
    迁徙去了一台根本不存在的机器。
    """
    text = (shell or "").strip()
    if not text:
        return ""
    parts = text.split("-")
    if len(parts) >= 3:
        tail = parts[-1]
        if len(tail) == 8 and all(c in "0123456789abcdef" for c in tail.lower()):
            return "-".join(parts[:-1])
    return text


def _fresh_seconds_left(consumed_at: Any, *, now: float) -> Optional[float]:
    """落地到现在过了多久。解析不出来就返回 None（= 不知道，不做判断）。"""
    text = str(consumed_at or "").strip()
    if not text:
        return None
    try:
        when = float(text)
    except (TypeError, ValueError):
        return None
    return float(now) - when


def pick_arrival(
    status: Optional[Dict[str, Any]],
    *,
    shell: str,
    now: float,
    announced_generation: int = 0,
    fresh_seconds: float = DEFAULT_FRESH_SECONDS,
) -> Optional[Dict[str, Any]]:
    """从账本状态里挑出「这一代是我落的、还没说过、而且还新鲜」的那一份。

    返回 `{generation, from_shell, from_label, age_seconds, stale}`。
    `stale=True` 的那一份**也要返回**：调用方要拿它把
    `announced_generation` 推到这一代，否则每一次回复都会再问一遍账本，
    而且永远问出同样一个"已经太久了"的结论。
    """
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
    if number <= 0 or number <= int(announced_generation or 0):
        return None

    from_shell = str(gen.get("pushed_by") or "")
    beats = status.get("heartbeats") or {}
    label = ""
    if isinstance(beats, dict):
        beat = beats.get(from_shell)
        if isinstance(beat, dict):
            label = str(beat.get("label") or "")

    age = _fresh_seconds_left(gen.get("consumed_at"), now=now)
    stale = bool(age is not None and age > float(fresh_seconds))
    return {
        "generation": number,
        "from_shell": from_shell,
        "from_label": label,
        "age_seconds": age,
        "stale": stale,
    }


@dataclass
class ArrivalState:
    """「这次落地我说过没有」。放在插件自己目录里（`COPY_ARTIFACTS` 不含 `plugins/**`），
    所以它天然属于壳、不属于魂 —— 换一台机器就该重新说一次。"""

    path: Path
    data: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "ArrivalState":
        data: Dict[str, Any] = {}
        try:
            if path.is_file():
                raw = path.read_text(encoding="utf-8-sig")
                parsed = json.loads(raw) if raw.strip() else {}
                if isinstance(parsed, dict):
                    data = parsed
        except (OSError, ValueError) as exc:
            # 读坏了就当没说过 —— 多说一次比永远不说好
            logger.warning("[Molt] 落地播报状态读不出来（%s），当作没说过", exc)
            data = {}
        return cls(path=path, data=data)

    @property
    def announced_generation(self) -> int:
        try:
            return int(self.data.get("announced_generation") or 0)
        except (TypeError, ValueError):
            return 0

    def mark(self, generation: int, *, detail: str = "") -> None:
        self.data["announced_generation"] = int(generation)
        self.data["announced_at"] = _now_text()
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
            logger.warning("[Molt] 落地播报状态写不进去（%s）", exc)


def _now_text() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
