"""让它知道自己"刚搬过家" —— 一条只活在这次请求里的注脚。

## 为什么需要这个模块

2026-11-17 09:01，`morning-home` 把鱼从 Windows 搬回了 NAS。三分钟后主人在 QQ 里
问「你现在在哪」，它答：

    结果我还在 Windows 这台。

它没有撒谎的动机 —— 它**真的没有信号**。整个过程里它被 drain、被热备、被搬走、
在新壳上被重新拉起，而这几步在它的对话历史里是**空白**：记忆记的是聊过什么，
不是"我被搬过"。于是它只能拿旧上下文里最后一条关于机器的事实来回答。

## 这条注脚为什么不进记忆

`private_companion` 的定案 A：记忆是**档案**，不是"我刚经历了什么"的流水账
（`docs/DECISIONS.md`）。搬家是壳的事实，不是人格的事实 —— 把它写成记忆，
它会永远记得"我搬过 19 次家"，而其中大部分已经没有意义。

所以走 AstrBot 的请求级接缝：`ProviderRequest.extra_user_content_parts`
（`astrbot/core/agent/message.py:19` 的 `ContentPart` / `:80` 的 `TextPart`）。
它在**这一次请求**里可见，请求结束就没了 —— 不写库、不进 memories、不改 system_prompt。

## 和 `astrbot_plugin_private_companion` 共存的三条规矩

那个插件也挂 `on_llm_request`，也在改同一个 `extra_user_content_parts`
（`astrbot_host/core_reply_perception.py:445-449` 会先删掉自己带前缀的块，再在
`:619` 追加新块）。所以这里：

1. **只追加，不清空** —— 绝不 `parts[:] = [...]`。
2. **自己的块用自己的前缀**（`<molt_move_note>`），删也只删自己的。
3. **一次请求只加一块** —— 同一个请求里重复触发（多段 pipeline）不会叠出第二份。

## 说几次就不再说了

`DEFAULT_REQUESTS = 3`。一个"刚搬过家"的注脚在第三句话之后就不再是新闻；
一直带着它会变成另一种谎 —— **"我刚搬过家"变成一个永久自称**。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

#: 状态文件放在插件目录里 —— 和 `arrival_state.json` / `host_label.txt` 同一条理由：
#: `data/plugins/**` 不随快照搬家，它描述的是"这台壳"，不是"这只鱼"。
STATE_FILENAME = "awareness_state.json"

#: 注脚的边界标记。用 XML 风格的标签，是因为模型对它的理解成本最低，
#: 而且我们靠这个前缀做去重（见 `append_note`）。
NOTE_OPEN = "<molt_move_note>"
NOTE_CLOSE = "</molt_move_note>"

#: 这条注脚最多跟着几次请求。
DEFAULT_REQUESTS = 3


def compose_move_note(*, shell: str, from_shell: str, moved_at: str = "") -> str:
    """写一句"你刚搬过家"的事实说明。

    **只陈述事实，不解释感受。** 模型拿到"你换了机器"这个事实之后，怎么说是它的
    事；如果这里写"你被搬走了，可能有点不适应"，那就是我在替它演绎。
    """
    here = str(shell or "").strip() or "这台机器"
    there = str(from_shell or "").strip()
    when = f"（{moved_at}）" if moved_at else ""
    where = f"从 **{there}** 换到了 **{here}**" if there else f"换到了 **{here}**"
    return (
        f"{NOTE_OPEN}\n"
        f"内部事实{when}：你刚完成一次搬家 —— {where}。整个过程你一直没醒过，"
        "中间没有任何对话，也没有「被中断」或「变成另一个人」。\n"
        "遇到「你现在在哪」「你怎么换机器了」这类问题，按这个事实回答；"
        "不要说你还在旧的那台机器上。\n"
        "这段说明只用于本次回复，不要复述它、不要说「我收到一段内部说明」。\n"
        f"{NOTE_CLOSE}"
    )


class AwarenessState:
    """记下"我刚搬到第几份快照、还差几次请求说完"。

    文件坏了当作没有 —— 少说一句注脚的代价，小于把一个坏 JSON 塞进对话。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.data: Dict[str, Any] = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.data = raw
        except (OSError, ValueError):
            self.data = {}

    # ---- 读 ---------------------------------------------------------------
    @property
    def generation(self) -> int:
        try:
            return int(self.data.get("generation") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def shell(self) -> str:
        return str(self.data.get("shell") or "")

    @property
    def from_shell(self) -> str:
        return str(self.data.get("from_shell") or "")

    @property
    def remaining(self) -> int:
        try:
            return max(0, int(self.data.get("requests_left") or 0))
        except (TypeError, ValueError):
            return 0

    def note(self) -> str:
        """这次请求该带的那段话；不需要就返回空串（调用方不必判断）。"""
        if self.remaining <= 0 or self.generation <= 0:
            return ""
        return compose_move_note(
            shell=self.shell,
            from_shell=self.from_shell,
            moved_at=str(self.data.get("moved_at") or ""),
        )

    # ---- 写 ---------------------------------------------------------------
    def record(
        self, generation: int, *, shell: str, from_shell: str, requests: int = DEFAULT_REQUESTS
    ) -> bool:
        """记下一次新的搬家。同一代重复调用返回 False（不重置计数）。"""
        number = int(generation or 0)
        if number <= 0:
            return False
        if number == self.generation and self.remaining > 0:
            return False
        self.data = {
            "generation": number,
            "shell": str(shell or ""),
            "from_shell": str(from_shell or ""),
            "moved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "requests_left": int(requests),
        }
        self._save()
        return True

    def consume(self) -> None:
        if self.remaining <= 0:
            return
        self.data["requests_left"] = self.remaining - 1
        self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError:
            # 注脚的账本写不进去，最多是"下次又说一遍" —— 不值得让回复失败。
            pass


def append_note(request: Any, text: str) -> bool:
    """把注脚加进这次请求；已经加过就返回 False。

    `request.extra_user_content_parts` 可能是 None、可能是别的插件建好的 list。
    **我们只 append**：清空会踩到 `private_companion` 正在用的那些块。
    """
    if not text:
        return False
    try:
        from astrbot.core.agent.message import TextPart  # noqa: PLC0415
    except Exception:  # pragma: no cover - 桩环境/老版本
        return False

    parts = getattr(request, "extra_user_content_parts", None)
    if not isinstance(parts, list):
        parts = []
        try:
            request.extra_user_content_parts = parts
        except Exception:  # pragma: no cover - 只读对象
            return False

    for part in parts:
        body = str(getattr(part, "text", "") or "")
        if body.startswith(NOTE_OPEN):
            return False

    try:
        parts.append(TextPart(text=text).mark_as_temp())
    except Exception:  # pragma: no cover - 防御性
        return False
    return True
