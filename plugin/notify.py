"""搬完家之后，主动发一条消息给你 —— 不用等你先开口。

2026-11-16 主人报告："迁移完以后它不能主动给我发消息"。

以前的落地播报挂在 ``on_waiting_llm_request`` 上：**它得先收到你的一句话**，
才会顺带说出「我刚从 X 搬过来」。所以半夜自动搬完、或者你几小时不理它，
你就什么也看不到。

这里走的是 AstrBot 官方的主动发消息接缝
（``Platform.send_by_session(session, chain)``，同一个接缝
``astrbot_plugin_private_companion`` 的"主动联系"在用）：

* **不发假消息**：不伪造一条"用户说了什么"进 core 的入站管线，
  不写 conversations、不触发模型、不产生记忆 —— 只是一条出站消息。
  （2026-11-15 删掉 ``molt announce`` 就是因为当时那条是假的入站消息。）
* **只发给一个人**：路由从 ``data_v4.db`` 的最近一条**私聊**会话里读
  （platform_id + openid），读不到就不猜。
* **一代只说一次**：``notify_state.json`` 记 ``pushed_generation``。
* **发不出去就说发不出去**：返回有状态的回执，绝不假报成功。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

STATE_FILENAME = "notify_state.json"
FRIEND = "FriendMessage"
SKIP_PLATFORMS = ("webchat",)
DEFAULT_PUSH_WAIT = 90.0
POLL_SECONDS = 3.0


def read_owner_route(data_dir: Path) -> Optional[Tuple[str, str]]:
    """从最近一条私聊会话里读出 (platform_id, openid)；读不到返回 None。"""
    db = Path(data_dir) / "data_v4.db"
    if not db.is_file():
        return None
    tmp = Path(tempfile.mkdtemp(prefix="molt-route-")) / "c.db"
    try:
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            src.close()
        rows = dst.execute(
            "select user_id from conversations order by updated_at desc limit 50"
        ).fetchall()
        dst.close()
    except sqlite3.Error:
        return None
    finally:
        tmp.unlink(missing_ok=True)
        tmp.parent.rmdir() if tmp.parent.exists() and not any(tmp.parent.iterdir()) else None
    for (user_id,) in rows:
        parts = str(user_id or "").split(":")
        if len(parts) != 3 or parts[1] != FRIEND:
            continue
        platform_id, _, openid = parts
        if not platform_id or not openid or platform_id in SKIP_PLATFORMS:
            continue
        return platform_id, openid
    return None


def compose_arrival_push(*, label: str = "", generation: int = 0) -> str:
    """它自己会说的那句话。不提"第几代"，只说它现在在哪。"""
    where = f"我现在在 **{label}**" if label else "我搬过来了"
    return f"{where}。\n这一觉睡得有点沉 —— 路上没醒。你找我随时说话。"


def find_platform(platforms: List[Any], platform_id: str) -> Optional[Any]:
    """按 platform_id 找适配器；找不到但只有一个能发的，就用那一个。"""
    usable = [p for p in platforms if callable(getattr(p, "send_by_session", None))]
    for p in usable:
        try:
            if str(getattr(p.meta(), "id", "")) == platform_id:
                return p
        except Exception:  # pragma: no cover - 某些适配器 meta() 会抛
            continue
    return usable[0] if len(usable) == 1 else None


async def push_arrival(context: Any, *, data_dir: Path, text: str) -> Dict[str, Any]:
    """真的发一次。返回 {"state": delivered|no-route|no-platform|failed, ...}。"""
    route = read_owner_route(Path(data_dir))
    if route is None:
        return {"state": "no-route", "reason": "账本里没有可用的私聊会话"}
    platform_id, openid = route
    try:
        manager = context.platform_manager
        platforms = list(manager.get_insts())
    except Exception as exc:  # pragma: no cover - 防御性
        return {"state": "no-platform", "reason": f"拿不到平台列表：{exc}"}
    platform = find_platform(platforms, platform_id)
    if platform is None:
        return {"state": "no-platform", "reason": f"没有可用的平台实例（要找 {platform_id}）"}
    try:
        from astrbot.core.message.components import Plain
        from astrbot.core.message.message_event_result import MessageChain
        from astrbot.core.platform.message_session import MessageSession
        from astrbot.core.platform.message_type import MessageType

        session = MessageSession(platform_id, MessageType.FRIEND_MESSAGE, openid)
        await platform.send_by_session(session, MessageChain([Plain(str(text))]))
    except Exception as exc:  # noqa: BLE001 - 平台失败只记状态，不重试
        return {"state": "failed", "reason": f"{type(exc).__name__}: {exc}"}
    return {"state": "delivered", "platform_id": platform_id, "openid": openid}


class NotifyState:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.pushed_generation = 0
        self.last_detail = ""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        self.pushed_generation = int(raw.get("pushed_generation") or 0)
        self.last_detail = str(raw.get("last_detail") or "")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"pushed_generation": self.pushed_generation, "last_detail": self.last_detail},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def mark(self, generation: int, detail: str) -> None:
        self.pushed_generation = int(generation)
        self.last_detail = str(detail)
        self.save()


def generation_of(latest: Any) -> int:
    """从账本的 `latest_generation` 里读出代次。

    **2026-11-17 的现场**：账本里的字段叫 `generation`，而自动通知与手动补发
    这两处都读的是 `number` —— 于是 `number` 永远是 0：
    手动那条因为文案里不用数字，看起来"能用"；自动那条被 `should_push(0)` 判成
    "没有新一代"，**从来没主动说过一句话**，只留下一条"等超时了"的日志。

    所以读法收在一处，并且两种键名都认（账本视图以后要改名时不会再踩一次）。
    """
    if not isinstance(latest, dict):
        return 0
    for key in ("generation", "number"):
        try:
            value = int(latest.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value:
            return value
    return 0


def should_push(state: NotifyState, generation: int) -> bool:
    return bool(generation) and int(generation) > int(state.pushed_generation or 0)
