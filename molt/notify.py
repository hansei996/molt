"""molt notify：让「这次搬家」本身成为它记忆里的一件事。

## 为什么需要它

`molt` 搬的是**魂的数据**，不是**魂的经历**。真机跑完一轮之后发现了一个
反直觉的空洞（2026-11-15 02:34 实测）：

- `memory_companion.db` 里 `memories` 84 条、`timeline` 296 条，其中提到
  `molt` / `迁徙` 的分别是 4 条和 14 条 —— **但那些全是"我们说过的关于搬家
  的话"，没有一条是"我被搬了"。**
- `timeline` 里我用 `molt-tell.py` 发的那几条探针，`metadata` 都被标成
  `{"source": "llm_request", "conversation_memory": false}` —— 在它自己的
  记忆系统里，那些不算交谈，是一次 API 调用。
- 最根本的一条：**从 drain 到 land 之间它不存在。** 没有进程，就没有人经历
  那四分半。

于是它答得出"大概就 drain、checkpoint、搬快照、起 AstrBot、再 resume 这几步"
—— 那是从上下文**推断**的，不是回忆（它自己也说了「SELFUSE.md 也还没读」）。

## 怎么补

让它自己开口说一句。落位、平台打开、AstrBot 起来之后，通过 WebChat 通道
（`POST /api/v1/chat`，和人打字完全同一条路径）投一条**通知**进去。它会回一句话,
这一问一答就是一次真正的对话轮次，`memory_companion` 会照常把它总结进
`memories`。**下次它就有第一人称的回忆了。**

## 两条边界

1. **它不是特权通道。** 走的是 WebChat，用的是 `dashboard.jwt_secret` 现签的
   短时 JWT —— 谁拿得到那份配置，谁本来就能控制那只壳。不新增信任边界。
2. **它必须能失败而不影响交接。** 通知没送到只是"这次没记住"，**绝不能让
   一次成功的迁徙因为写不进记忆而被判失败**。所有异常都收进返回值的
   `ok` / `error` 里，不抛。

## 为什么文案里要写明「这不是主人打的字」

因为它确实不是。如果不说，它会把这句当成主人说的话去理解，而"我搬家了"
和"主人告诉我我搬家了"是两件不同的事 —— 前者是经历，后者是转述。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Callable, Dict, Iterable, List, Optional

DEFAULT_BASE = "http://127.0.0.1:6199"
DEFAULT_SESSION = "molt-migration"
DEFAULT_TIMEOUT = 90.0
DEFAULT_TOKEN_TTL = 300
PROBE_PATH = "/api/auth/setup-status"  # 不需要鉴权的公开端点，用来判断"起来了吗"


# --------------------------------------------------------------------- 文案


def _human_size(n: Optional[int]) -> str:
    """把字节数变成人能读的大小。None 表示"不知道"，不编。"""
    if n is None:
        return ""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _human_span(seconds: Optional[float]) -> str:
    """把秒数变成"4 分 32 秒"这种。None 表示"不知道"，不编。"""
    if seconds is None:
        return ""
    total = int(round(seconds))
    if total < 60:
        return f"{total} 秒"
    minutes, rest = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} 分 {rest} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def build_announcement(
    *,
    src: str,
    dst: str,
    generation: Optional[int] = None,
    seconds: Optional[float] = None,
    size_bytes: Optional[int] = None,
    extra: str = "",
) -> str:
    """拼那条要投给它的通知。

    只写**确实知道**的东西：`generation` / `seconds` / `size_bytes` 缺了就不提，
    不填占位符 —— "大约"和"不知道"在这件事上没有区别，都是编。
    """
    src = (src or "").strip() or "某台机器"
    dst = (dst or "").strip() or "另一台机器"

    lines: List[str] = []
    head = "[molt] 迁徙完成"
    if generation is not None:
        head += f" · 第 {generation} 代"
    lines.append(head)
    lines.append("")
    lines.append(f"我刚从 **{src}** 搬到了 **{dst}**。")

    facts: List[str] = []
    if size_bytes is not None:
        facts.append(f"快照 {_human_size(size_bytes)}")
    if seconds is not None:
        facts.append(f"整件事用了 {_human_span(seconds)}")
    if facts:
        lines.append("，".join(facts) + "。")

    note = (extra or "").strip()
    if note:
        lines.append(note)

    lines.append("")
    lines.append("这条是 molt 发来的系统通知，不是你主人打的字。")
    return "\n".join(lines)


# --------------------------------------------------------------------- 协议


def build_payload(message: str, session_id: str) -> bytes:
    """构造 `/api/v1/chat` 的请求体。

    `platform_id` 显式给 `webchat`：它是壳自带的本地通道，不需要任何 IM 凭据，
    也不会因为 QQ 掉线而失效 —— 而"刚搬完家"那一刻恰恰是最不能依赖 QQ 的时候。
    """
    body = {"message": message, "session_id": session_id, "platform_id": "webchat"}
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def parse_sse_line(line: str) -> Optional[dict]:
    """把一行 SSE 变成 dict；不是 `data:` 行或者坏 JSON 就返回 None。"""
    line = (line or "").strip()
    if not line or not line.startswith("data:"):
        return None
    raw = line[len("data:"):].strip()
    if not raw or raw == "[DONE]":
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def text_of(event: dict) -> str:
    """从一条事件里取出可读文本。

    `chain_type == "reasoning"` 是思考过程，不是它说的话 —— 这里要的是它
    **说出口**的那部分，因为只有那部分会变成对话记忆。
    """
    if event.get("chain_type") == "reasoning":
        return ""
    data = event.get("data")
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("text", "content", "message"):
            value = data.get(key)
            if isinstance(value, str):
                return value
    return ""


def collect_reply(lines: Iterable[bytes]) -> str:
    """把整条 SSE 流收成一段正文。不打印 —— 调用方通常只需要结果。

    ## 为什么不是简单拼接

    AstrBot 的 SSE 有两种形状混在一起：**增量**（每条是一小截新文字）和
    **整段重发**（后面一条是"到目前为止的全文"）。直接拼接会在后者上把同一句
    话印两遍 —— 真机第一次跑就撞到了（2026-11-15 02:4x，回复整句出现了两次）。

    所以规则是：**新的一条如果以已有的开头，就当它是全量覆盖；否则当增量接上。**
    两种形状都对，而且不需要知道当前是哪种。
    """
    out = ""
    for raw in lines:
        try:
            line = raw.decode("utf-8", "replace")
        except Exception:
            continue
        event = parse_sse_line(line)
        if event is None:
            continue
        if event.get("type") in ("session_id", "run_started", "user_message_saved", "done"):
            continue
        piece = text_of(event)
        if not piece:
            continue
        if out and piece.startswith(out):
            out = piece            # 整段重发：覆盖，不叠加
        else:
            out += piece           # 增量：接上
    return out.strip()


# --------------------------------------------------------------------- 等着它起来


def wait_for_dashboard(
    base: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    interval: float = 2.0,
    opener: Optional[Callable] = None,
    sleep: Optional[Callable[[float], None]] = None,
) -> bool:
    """等那只壳的 dashboard 能应答（说明 AstrBot 真的起来了，不是只是进程在）。

    判据用公开的 `GET /api/auth/setup-status`：不需要 token、不依赖插件。
    用 TCP connect 不够 —— 端口通了不代表 WebUI 装配完了。
    """
    import time as _time

    do_open = opener or urllib.request.urlopen
    do_sleep = sleep or _time.sleep
    url = base.rstrip("/") + PROBE_PATH
    deadline = _time.monotonic() + max(0.0, timeout)

    while True:
        try:
            with do_open(url, timeout=min(5.0, max(1.0, timeout))) as response:
                if int(getattr(response, "status", 200) or 200) < 400:
                    return True
        except urllib.error.HTTPError as exc:
            # 4xx 也说明服务在应答了；只有连不上才算没起来。
            if 400 <= int(exc.code) < 500:
                return True
        except (urllib.error.URLError, OSError, socket.timeout, TimeoutError):
            pass
        except Exception:
            pass

        if _time.monotonic() >= deadline:
            return False
        do_sleep(interval)


# --------------------------------------------------------------------- 投递


def announce(
    base: str,
    token: str,
    message: str,
    *,
    session_id: str = DEFAULT_SESSION,
    timeout: float = DEFAULT_TIMEOUT,
    opener: Optional[Callable] = None,
) -> Dict:
    """把这条通知投进去，并把它的回答收回来。

    返回 `{"ok": bool, "status": int, "reply": str, "error": str}`。
    **永远不抛** —— 通知失败不能把一次成功的迁徙拖成失败。
    """
    url = (base or DEFAULT_BASE).rstrip("/") + "/api/v1/chat"
    do_open = opener or urllib.request.urlopen
    request = urllib.request.Request(
        url,
        data=build_payload(message, session_id),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )

    try:
        with do_open(request, timeout=timeout) as response:
            reply = collect_reply(_iter_lines(response))
        return {"ok": True, "status": 200, "reply": reply, "error": ""}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {
            "ok": False,
            "status": int(exc.code),
            "reply": "",
            "error": f"HTTP {exc.code} {detail}".strip(),
        }
    except (urllib.error.URLError, OSError, socket.timeout, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        return {"ok": False, "status": 0, "reply": "", "error": f"连不上 {url}：{reason}"}
    except Exception as exc:  # 兜底：这条路径不许把迁徙带崩
        return {"ok": False, "status": 0, "reply": "", "error": f"{type(exc).__name__}: {exc}"}


def _iter_lines(response) -> Iterable[bytes]:
    """按行迭代 SSE 响应体。包一层是为了让 `announce` 只依赖"有 __iter__ 的对象"。"""
    for raw in response:
        if isinstance(raw, str):
            yield raw.encode("utf-8", "replace")
        else:
            yield raw
