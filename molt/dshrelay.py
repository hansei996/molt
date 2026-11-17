"""让鱼待在 NAS 上，也能跟另一台机器上的 DSH 说话。

## 为什么需要一层代转

DSH 的 host 服务是**按回环地址做 Host 校验**的（`dsh_connector` 的 README
也写着：http 模式要求 `http_base_url` 是回环地址，其它 authority 会被拒）。
所以「从 NAS 直接连 `http://192.168.50.63:3082`」这条路，在**协议层**就
不成立 —— 不是防火墙，也不是没开端口。

于是由账本那台机器（NAS）代转：它本来就能同时看见两台机器，也只在这条
链路上充当一次 HTTP 客户端。转发时把 `Host` 改写成 `127.0.0.1:<端口>`，
让对端以为自己在跟本机说话。

## 三个方法，够用就行

- `session/list`   —— 那台机器上有哪些会话（读）
- `session/events` —— 某个会话的历史消息（读）
- `session/prompt` —— 往某个会话发一句话（写）

**只放这三个。** 代转口的本质是"在另一台机器上执行一次 RPC"，白名单越
窄越好：多放一个方法，就等于多给外面一个能在那台机器上动手的入口。

## 一条纪律

这一层**不做重试、不做缓存、不解释语义**。它只负责把请求送过去、把回执
拿回来。判据留在调用方 —— 代转层一旦开始"帮你重试一次"，调用方就再也
分不清自己看到的是一次还是两次。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List

#: DSH host 服务的默认端口（桌面版实际监听 0.0.0.0:3082，但 Host 校验要回环）。
DSH_PORT = 3082

#: 允许经代转口调用的方法。**不要随手加。**
ALLOWED_METHODS = ("session/list", "session/events", "session/prompt")

#: 读类请求的超时（历史可能很长）。
READ_TIMEOUT = 30.0

#: 发 prompt 的超时（对端要真的开始回答）。
SEND_TIMEOUT = 60.0


class DshRelayError(RuntimeError):
    """代转失败 —— 调用方要能分清"没通"和"通了但对端说不行"。"""


def resolve_base(status: Dict[str, Any], shell: str) -> str:
    """从账本的壳视图里取出 `http://<address>:3082`。

    取不到地址就明确报错，**不要退回到 127.0.0.1** —— 那会让 NAS 去连自己
    的 3082，然后报一个"连不上"的错，把真正的配置问题伪装成网络问题。
    """

    shells = (status or {}).get("shells") or {}
    entry = shells.get(shell) or {}
    address = str(entry.get("address") or "").strip()
    if not address:
        raise DshRelayError(
            f"账本里没有 {shell} 的地址。先声明一次：molt declare --shell {shell} --address <IP>"
        )
    return f"http://{address}:{DSH_PORT}"


def build_body(method: str, args: Dict[str, Any], *, rpc_id: str = "") -> Dict[str, Any]:
    """拼一个 DSH 网关认识的 client-request。"""

    return {
        "type": "client-request",
        "rpcId": rpc_id or uuid.uuid4().hex[:12],
        "method": method,
        "payload": {"args": dict(args or {})},
    }


def call(
    base: str,
    method: str,
    args: Dict[str, Any],
    *,
    timeout: float = READ_TIMEOUT,
    opener: Any = None,
) -> Dict[str, Any]:
    """把一次 RPC 送到 `base`，返回 `result` 字段（原样，不解释）。

    `opener` 只给测试用（签名同 `urllib.request.urlopen`）。
    """

    if method not in ALLOWED_METHODS:
        raise DshRelayError(
            f"{method} 不在代转白名单里（只允许 {', '.join(ALLOWED_METHODS)}）"
        )
    body = json.dumps(build_body(method, args), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{base.rstrip('/')}/api/{method}",
        data=body,
        headers={
            "Content-Type": "application/json",
            # 关键一行：对端按 Host 做回环校验，不改写就会被拒。
            "Host": f"127.0.0.1:{DSH_PORT}",
            "Origin": f"http://127.0.0.1:{DSH_PORT}",
        },
    )
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        raise DshRelayError(f"{method} 被对端拒绝（HTTP {e.code}）") from e
    except (urllib.error.URLError, OSError) as e:
        raise DshRelayError(f"连不上 {base}：{e}") from e
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as e:
        raise DshRelayError(f"{base} 回的不是 JSON（{len(raw)} 字节）") from e
    result = payload.get("result")
    if not isinstance(result, dict):
        raise DshRelayError(f"{base} 的回执里没有 result：{str(payload)[:200]}")
    if not result.get("ok", False):
        error = result.get("error") or {}
        raise DshRelayError(
            f"{method} 失败：{error.get('code') or 'unknown'} {error.get('message') or ''}".strip()
        )
    value = result.get("value")
    return value if isinstance(value, dict) else {"value": value}


# --------------------------------------------------------------- 三个便利封装


def list_sessions(base: str, *, opener: Any = None) -> List[Dict[str, Any]]:
    """那台机器上的会话列表（最新的在前，字段来自 DSH 自己）。"""

    value = call(base, "session/list", {"_request": {}}, opener=opener)
    items = value.get("items")
    return list(items) if isinstance(items, list) else []


def read_events(
    base: str,
    session_id: str,
    *,
    max_messages: int = 20,
    opener: Any = None,
) -> Dict[str, Any]:
    """读某个会话的历史（一页）。"""

    if not session_id:
        raise DshRelayError("没有 sessionId")
    args = {
        "request": {
            "address": {"kind": "session", "sessionId": session_id},
            "maxMessages": max(1, int(max_messages)),
        }
    }
    return call(base, "session/events", args, opener=opener)


def send_prompt(
    base: str,
    session_id: str,
    text: str,
    *,
    mode: str = "queue",
    request_id: str = "",
    opener: Any = None,
) -> Dict[str, Any]:
    """往某个会话发一句话。

    `mode="queue"` 是"排在这一轮之后"，`"steer"` 是"插进去改方向"。
    默认排后面 —— 让人家的回答被打断，不该是默认行为。
    """

    if not str(text or "").strip():
        raise DshRelayError("不发空话")
    if not session_id:
        raise DshRelayError("没有 sessionId")
    args = {
        "request": {
            "requestId": request_id or uuid.uuid4().hex,
            "sessionId": session_id,
            "mode": mode if mode in ("queue", "steer") else "queue",
            "content": [{"type": "text", "text": str(text)}],
        }
    }
    return call(base, "session/prompt", args, timeout=SEND_TIMEOUT, opener=opener)


def peer_call(
    address: str,
    port: Any,
    method: str,
    args: Dict[str, Any],
    *,
    token: str,
    timeout: float = READ_TIMEOUT,
    opener: Any = None,
) -> Dict[str, Any]:
    """请那台机器上的"代转小口"转发（`molt/dshpeer.py`）。

    为什么要多这一跳：DSH 的网关按**对端地址**判回环，从 NAS 直接连会被
    401 —— 所以那一跳必须发生在有 DSH 的那台机器本机。
    """

    if method not in ALLOWED_METHODS:
        raise DshRelayError(f"{method} 不在代转白名单里（只允许 {', '.join(ALLOWED_METHODS)}）")
    if not token:
        raise DshRelayError("没有 token，不能请对面的小口转发")
    base = f"http://{address}:{int(port)}/dsh"
    body = json.dumps({"method": method, "args": dict(args or {})}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        base,
        data=body,
        headers={"Content-Type": "application/json", "X-Molt-Token": token},
    )
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8", "replace")).get("message") or ""
        except Exception:
            detail = ""
        raise DshRelayError(f"对面小口拒绝了（HTTP {e.code}）{detail}") from e
    except (urllib.error.URLError, OSError) as e:
        raise DshRelayError(f"连不上小口 {base}：{e}") from e
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as e:
        raise DshRelayError(f"小口回的不是 JSON（{len(raw)} 字节）") from e
    if not payload.get("ok"):
        raise DshRelayError(str(payload.get("message") or "小口说不"))
    value = payload.get("dsh")
    return value if isinstance(value, dict) else {"value": value}



def summarize_events(value: Dict[str, Any], *, limit: int = 20) -> List[str]:
    """把一页事件压成 `角色: 摘要` 的几行，给命令行和聊天里看。"""

    records = value.get("records")
    if not isinstance(records, list):
        return []
    lines: List[str] = []
    for record in records:
        event = (record or {}).get("event") or {}
        data = event.get("data") or {}
        message = data.get("message") or {}
        role = str(message.get("role") or "")
        parts = message.get("content")
        text = ""
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "text":
                    text += str(part.get("text") or "")
        if not text:
            continue
        who = {"user": "我", "assistant": "它"}.get(role, role or "?")
        lines.append(f"{who}: {text.strip()[:200]}")
    return lines[-limit:]


def is_allowed(method: str) -> bool:
    return method in ALLOWED_METHODS


def describe_scope() -> str:
    """给文档和 `molt dsh --help` 用的一句话。"""

    return (
        "经账本代转，只允许这三个方法："
        + "、".join(ALLOWED_METHODS)
        + "（读会话、读历史、发一句话）"
    )


__all__ = [
    "ALLOWED_METHODS",
    "DSH_PORT",
    "DshRelayError",
    "READ_TIMEOUT",
    "SEND_TIMEOUT",
    "build_body",
    "call",
    "describe_scope",
    "is_allowed",
    "list_sessions",
    "peer_call",
    "read_events",
    "resolve_base",
    "send_prompt",
    "summarize_events",
]

