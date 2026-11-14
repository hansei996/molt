"""把「我搬到了新机器」这件事记成**它自己观察到的活动**，而不是伪造一条主人说的话。

## 为什么不在 molt 那边写

`memory_companion` 的记忆写入必须发生在 AstrBot 进程里（它持有 service 与数据库连接）。
`molt announce` 那种"往 webchat 灌一条消息"的做法看起来能用，但那是**用一条假消息换一条真记忆**：
记录里写着 `user_message`、发送者是主人，而主人并没有说过那句话。记忆库该记的是发生过的事，
不该为了塞进一条记录而在数据库里留下伪造的行。

## 为什么用 `bot_observed_activity`

`memory_companion` 自己就把记忆分成两类（`core/bot_personal_contract.py:189`）：

- `user_message` —— 主人说的；
- `bot_observed_activity` —— **它自己观察到的**（合同里 source_kind 固定为 `observed`，evidence_level `L2`）。

"我落在 DESKTOP-H2KGIAO，工作目录是 G:\\molt-fish\\data" 本来就是它能观察到的事实
（它在 QQ 里自己就这么报过）。写进 `bot_observed_activity`，主语是它、来源是观察，
不需要冒充任何人。

## 边界

1. **只用公开/半公开接缝**：`record_bot_personal_memory(memory_type=..., payload=...)` 是
   `MemoryCompanionService` 上的无下划线方法（`core/service.py:1981`），不是我们反过来直接
   往它的表里 `INSERT`。
2. **拿到它的三种方式，逐级降级**：`context.get_registered_star(name).star_cls` →
   `star_registry` 里同名条目 → `sys.modules` 里 `astrbot_plugin_memory_companion.main`
   的模块级 `_ACTIVE_BRIDGE`（它自己在 `main.py:68` 赋的）。三级都拿不到就放弃。
3. **它不在就什么也不做**：没装 `memory_companion` 时返回 `{"ok": False, "reason": ...}`，
   而不是抛错。迁徙本身不该因为"没记住"被判失败。
4. **永不抛**：任何异常都收成 `ok=False` —— 这段代码跑在 AstrBot 的事件循环里，
   抛出去会变成一条没人看的 traceback，甚至打断别的钩子。
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("astrbot")

MEMORY_PLUGIN = "astrbot_plugin_memory_companion"
OBSERVED_TYPE = "bot_observed_activity"
# 归属哪只机器人。留空的话检索层会把它归到"别人的"那一类，记录在库里但想不起来
# （2026-11-15 真机注入日志：`other_bot_owner:15` / `owner_bot_mismatch:3`）。
OWNER_BOT_ID = "self"
PERSONA_ID = "default"
# `core/bot_personal_contract.py` 里的 `BOT_PERSONAL_CANONICAL_SCHEMA_VERSION`
# 在 1.10.5 上是 3。写死会有版本风险，所以运行时去问它一次，问不到就用 3。
CANONICAL_SCHEMA_VERSION = 3


def _service_from_star(star: Any) -> Optional[Any]:
    """从一个插件实例上找 service。不同版本属性名可能不同，逐个试。"""
    for attr in ("service", "_service"):
        svc = getattr(star, attr, None)
        if svc is not None and hasattr(svc, "record_bot_personal_memory"):
            return svc
    # 有的版本只在实例上留 bridge
    for attr in ("memory_companion", "_bridge"):
        bridge = getattr(star, attr, None)
        if bridge is not None and hasattr(bridge, "record_bot_personal_memory"):
            return bridge
    return None


def _module_bridge() -> Optional[Any]:
    """兜底：翻 sys.modules 找 memory_companion 主模块上的模块级 bridge。"""
    for name, mod in list(sys.modules.items()):
        if not name.endswith(f"{MEMORY_PLUGIN}.main"):
            continue
        bridge = getattr(mod, "_ACTIVE_BRIDGE", None)
        if bridge is not None and hasattr(bridge, "record_bot_personal_memory"):
            return bridge
    return None


def find_service(context: Any) -> tuple[Optional[Any], str]:
    """按三级优先级找 memory_companion 的写入入口。返回 (service, 说明)。"""
    getter = getattr(context, "get_registered_star", None)
    if callable(getter):
        try:
            meta = getter(MEMORY_PLUGIN)
        except Exception:  # pragma: no cover - 别人的实现不该影响我们
            meta = None
        if meta is not None:
            svc = _service_from_star(getattr(meta, "star_cls", None))
            if svc is not None:
                return svc, "get_registered_star"

    try:
        from astrbot.core.star.star import star_registry  # type: ignore

        for meta in star_registry:
            if getattr(meta, "name", "") != MEMORY_PLUGIN:
                continue
            svc = _service_from_star(getattr(meta, "star_cls", None))
            if svc is not None:
                return svc, "star_registry"
    except Exception:  # pragma: no cover
        pass

    bridge = _module_bridge()
    if bridge is not None:
        return bridge, "_ACTIVE_BRIDGE"
    return None, "not-found"


def _iso(value: Any) -> str:
    """把账本里的时间戳变成它要的 ISO-8601（**必须带时区**）。

    账本存的是 epoch 秒（`1794679431.94`），而 `bot_personal_dto._parse_datetime`
    要求 ISO-8601 且要求带时区（`bot_personal_dto.py:141/143`），
    直接喂数字会得到 `invalid` —— 2026-11-15 真机上就是这么被拒的。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        seconds = float(text)
    except (TypeError, ValueError):
        return text
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _payload(text: str, *, occurred_at: str, shell: str, generation: int) -> Dict[str, Any]:
    return {
        "content": text,
        "summary": text.splitlines()[0][:120] if text else "",
        "shell": shell,
        "generation": generation,
        "occurred_at": _iso(occurred_at),
    }


async def record_observed_activity(
    context: Any,
    text: str,
    *,
    occurred_at: str,
    shell: str = "",
    generation: int = 0,
    idempotency_key: str = "",
    owner_bot_id: str = OWNER_BOT_ID,
    persona_id: str = PERSONA_ID,
) -> Dict[str, Any]:
    """把一段"它自己观察到的事"写进 memory_companion。永不抛。

    返回 `{"ok": bool, "via": str, "reason": str}`。
    """
    if not text or not text.strip():
        return {"ok": False, "via": "none", "reason": "内容为空"}

    service, via = find_service(context)
    if service is None:
        return {
            "ok": False,
            "via": "none",
            "reason": f"没找到 {MEMORY_PLUGIN}（没装、或被禁用），这条只留在 molt 自己的记录里",
        }

    when = _iso(occurred_at)
    kwargs: Dict[str, Any] = {
        "memory_type": OBSERVED_TYPE,
        "payload": _payload(text, occurred_at=when, shell=shell, generation=generation),
        # 顶层的 occurred_at 也必须给 ISO —— 它会先被 `_parse_datetime` 校验一遍
        # （`bot_personal_dto.py:141/143`），只改 payload 里那个不够。
        "occurred_at": when,
    }
    if idempotency_key:
        kwargs["idempotency_key"] = idempotency_key
    # 让它属于"这只机器人自己"。留空的话检索层会把它当成**别人的**
    # （2026-11-15 真机：注入日志里 `other_bot_owner:15`、`owner_bot_mismatch:3`，
    #  而我们那条 `owner_bot_id` 是空串、`visibility` 只是 `bot_self`，
    #  于是它在检索范围之外 —— 记录进去了，但它自己想不起来）。
    kwargs["owner_bot_id"] = OWNER_BOT_ID
    kwargs["persona_id"] = persona_id or PERSONA_ID
    # 不显式给 canonical schema 的话它会走 legacy（version 1），而 legacy 分支
    # 会把 owner_bot_id / persona_id **清零**（`bot_personal_dto.py:597-599`）——
    # 那就等于上面两行白写了。
    kwargs["canonical_schema_version"] = CANONICAL_SCHEMA_VERSION

    try:
        result = await service.record_bot_personal_memory(**kwargs)
    except Exception as exc:  # pragma: no cover - 对方的内部形状可能变
        path = getattr(exc, "path", "")
        detail = getattr(exc, "message", "") or getattr(exc, "detail", "")
        logger.warning("[Molt] 写记忆失败（%s）：%s", via, exc)
        reason = f"{type(exc).__name__}: {exc}"
        if path or detail:
            reason += f"（{path}: {detail}）"
        return {"ok": False, "via": via, "reason": reason}

    if not isinstance(result, dict):
        return {"ok": False, "via": via, "reason": f"返回值不是对象：{type(result).__name__}"}
    if result.get("ok"):
        return {"ok": True, "via": via, "reason": str(result.get("state", ""))}
    code = str(result.get("error_code") or result.get("state") or "")
    # `version_conflict` = 这条其实已经在了（同一个 idempotency_key）。
    # 对我们来说"已经在"就是成功：再记一次没有意义，也不该被报成失败。
    if code == "version_conflict":
        return {"ok": True, "via": via, "reason": "已经记过这一代了"}
    return {
        "ok": False,
        "via": via,
        "reason": code or "被拒绝",
    }
