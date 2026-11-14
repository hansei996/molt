#!/usr/bin/env python3
"""molt tell：以主人身份对一只壳说一句话，把它的回答读回来。

## 为什么需要它

迁徙是"你要它搬，它才搬"。到目前为止这个"你"只能是人 —— 在 QQ 里打字，
或者到那台机器前敲命令。于是自动化只能做一半：能自动搬，但**没有人能自动地
发起一次搬迁**。

这个脚本补的是那半截：给一只壳发一条消息（走的正是它的 WebChat 通道，
和人在网页上打字是同一条路径 —— `POST /api/v1/chat`），然后把它的回答
读回来。它不新建任何特权通道，也不绕过任何一道闸：`/molt go` 仍然要
管理员身份、仍然要两步确认、仍然要 `molt_command.json` 里 `enabled: true`。

## 鉴权

用 `cmd_config.json` 里的 `dashboard.jwt_secret` 自己签一个短时 JWT
（`molt.control.sign_dashboard_jwt`，标准库 HMAC，不引 PyJWT）。
所以**跑这个脚本的人必须已经持有那份配置** —— 也就是说，它不增加任何
新的信任边界：谁拿得到配置，谁本来就能控制那只壳。

token 默认只签 300 秒：一次对话足够，泄漏窗口更小。

## 两件容易踩的事

1. `cmd_config.json` 带 UTF-8 BOM，必须 `utf-8-sig` 读（ARCH §6.6.5）。
2. 流里**先来的是思考过程**（`chain_type: "reasoning"`），然后是正文。
   默认丢掉思考，用 ``--show-reasoning`` 才显示 —— 否则你看到的是一串
   英文自问自答，会以为它没在说人话。

用法：

    python tools/molt-tell.py --config G:\\molt-fish\\data\\cmd_config.json status
    python tools/molt-tell.py --config <cfg> --base http://192.168.1.10:6185 "你还好吗"

退出码：0 成功；2 参数或配置问题；3 连不上或 HTTP 报错；4 超时。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable, List, Optional

# 允许直接从仓库根跑，也允许把本文件单独拷走（tools/ 不是包）
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from molt.control import read_dashboard_credentials, sign_dashboard_jwt  # noqa: E402

DEFAULT_BASE = "http://127.0.0.1:6199"
DEFAULT_SESSION = "molt-tell"
DEFAULT_TIMEOUT = 120.0
DEFAULT_TOKEN_TTL = 300


# --------------------------------------------------------------------- 控制台


def _setup_console() -> None:
    """把 stdout/stderr 尽量切到 UTF-8；切不过去也不抛。

    这一条是 bug #1 / #15 的教训：Windows 的 GBK 控制台遇到 `✓` 会直接
    `UnicodeEncodeError` 把进程打死，而这里恰恰是最该活得像个工具的地方。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            try:
                stream.reconfigure(errors="replace")  # type: ignore[union-attr]
            except Exception:
                pass


def _safe_print(text: str) -> None:
    """print 一个可能超出当前编码能力的字符串，永远不抛。"""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        sys.stdout.write(text.encode(enc, "replace").decode(enc, "replace") + "\n")
        sys.stdout.flush()


# --------------------------------------------------------------------- 协议


def build_payload(message: str, session_id: str) -> bytes:
    """构造 `/api/v1/chat` 的请求体。

    `platform_id` 显式给 `webchat`：不写的话 AstrBot 会按下发消息的通道兜底，
    而我们希望这条消息永远走 WebChat（它是壳自带的本地通道，不需要任何
    IM 凭据，也不会因为 QQ 掉线而失效）。
    """
    body = {
        "message": message,
        "session_id": session_id,
        "platform_id": "webchat",
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def parse_sse_line(line: str) -> Optional[dict]:
    """把一行 SSE 变成 dict；不是 `data:` 行或者坏 JSON 就返回 None。"""
    line = line.strip()
    if not line or not line.startswith("data:"):
        return None
    raw = line[len("data:") :].strip()
    if not raw or raw == "[DONE]":
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def text_of(event: dict) -> str:
    """从一条事件里取出可读文本；取不到就返回空串。

    `chain_type == "reasoning"` 是模型的思考过程，不是它说的话 —— 默认丢掉。
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


def iter_reply(lines: Iterable[bytes], *, show_reasoning: bool, verbose: bool) -> Iterable[str]:
    """从 SSE 字节行里不断吐出正文增量。

    生成器而不是一次性拼接，是为了让调用方可以边收边打 —— 一次迁徙要跑
    二十秒，如果最后才一次性输出，人在终端前会以为它卡住了。
    """
    for raw in lines:
        line = raw.decode("utf-8", "replace")
        event = parse_sse_line(line)
        if event is None:
            continue
        kind = event.get("type")
        if verbose:
            _safe_print(f"[event] {kind} chain={event.get('chain_type')}")
        if kind in ("session_id", "run_started", "user_message_saved", "done"):
            continue
        if event.get("chain_type") == "reasoning" and not show_reasoning:
            continue
        piece = text_of(event)
        if piece:
            yield piece


# --------------------------------------------------------------------- 入口


def talk(
    base: str,
    token: str,
    message: str,
    session_id: str,
    timeout: float,
    *,
    show_reasoning: bool = False,
    verbose: bool = False,
) -> int:
    url = base.rstrip("/") + "/api/v1/chat"
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

    collected: List[str] = []
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for piece in iter_reply(
                response,
                show_reasoning=show_reasoning,
                verbose=verbose,
            ):
                collected.append(piece)
                sys.stdout.write(piece)
                sys.stdout.flush()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        _safe_print(f"\n[连上了，但它拒绝了这次请求：HTTP {exc.code} {detail}]")
        return 3
    except urllib.error.URLError as exc:
        _safe_print(f"\n[连不上 {url}：{exc.reason}]")
        _safe_print("  那个壳在跑吗？dashboard 端口对吗？")
        return 3
    except TimeoutError:
        _safe_print(f"\n[等了 {timeout:.0f} 秒没等到回复]")
        return 4

    if not collected:
        _safe_print("\n[它一个字的正文都没回 —— 用 --show-reasoning 看它想了什么]")
        return 0
    _safe_print("")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="molt-tell",
        description="以主人身份对一只 AstrBot 壳说一句话，把回答读回来。",
    )
    parser.add_argument("message", nargs="+", help="要说的话，可以直接写指令，如 /molt status")
    parser.add_argument("--config", required=True, help="那只壳的 cmd_config.json")
    parser.add_argument("--base", default=DEFAULT_BASE, help=f"dashboard 地址，默认 {DEFAULT_BASE}")
    parser.add_argument("--session", default=DEFAULT_SESSION, help="会话 id（同一个 id 会接着上文）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="等多久，默认 120 秒")
    parser.add_argument("--token-ttl", type=int, default=DEFAULT_TOKEN_TTL, help="token 有效期，默认 300 秒")
    parser.add_argument("--show-reasoning", action="store_true", help="连思考过程一起打出来")
    parser.add_argument("--verbose", action="store_true", help="打印所有事件类型，排查用")
    args = parser.parse_args(argv)

    message = " ".join(args.message).strip()
    if not message:
        _safe_print("[没话可说]")
        return 2

    try:
        secret, username = read_dashboard_credentials(args.config)
    except Exception as exc:  # NeedsHuman / Fatal 都是普通异常
        _safe_print(f"[读配置失败] {exc}")
        return 2

    try:
        token = sign_dashboard_jwt(secret, username, ttl=args.token_ttl)
    except Exception as exc:
        _safe_print(f"[签 token 失败] {exc}")
        return 2

    if args.verbose:
        _safe_print(f"[对 {args.base} 说：{message}]（身份 {username}）")

    return talk(
        args.base,
        token,
        message,
        args.session,
        args.timeout,
        show_reasoning=args.show_reasoning,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    _setup_console()
    sys.exit(main())
