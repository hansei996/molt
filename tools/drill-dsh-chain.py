"""一条命令跑完 D18 那条链的回归：NAS 账本 → Windows agent:6191 → 127.0.0.1:3082。

为什么要这个脚本
----------------
2026-11-18 那一轮，我是一段一段手工验的：先 curl 本机 peer，再从 NAS 打 warden，
再单独试白名单拒绝……每次都要重新拼 token、重新记"上一次那个会话 id 是多少"。
手工验过的东西**会退化**，而且退化时没人知道 —— 所以把它写成一条能重跑的命令。

它验什么（每一项都对应 D18 / VERIFY 里的一行）
------------------------------------------------
1. peer 活着（`/health` 200，且是 `molt-dshpeer`）
2. peer 的鉴权边界：不带 token 打 `/dsh` 必须 401
3. 三个白名单方法各走一次：
   - `session/list`（用 `{"_request": {}}` 这个真实形状）
   - `session/events`（用真实形状 `{"request": {"address": …, "maxMessages": N}}`）
   - `session/prompt`（**只打一个不存在的会话**：能进到网关、回 `session/not-found`，
     说明方法在白名单里；同时一个字都不会发出去）
4. 拒绝项：`session/create` / `session/cancel` 必须 400，且理由里带白名单全文
5. 调用方塞 `address` / `upstream` 想改目标 —— 必须被忽略（仍拿到本机的结果）
6. NAS 那一跳（`{warden}/dsh`）：无 token 401、白名单外 400、三个方法各通一次
7. 端到端 CLI：`py -m molt.cli dsh sessions --shell …` 退出码 0

用法
----
    py tools/drill-dsh-chain.py --warden-url http://192.168.50.50:6186 --warden-token <token>
    py tools/drill-dsh-chain.py --warden-url … --json      # 给机器读

限制：**只读 + 一个注定失败的 prompt**。它不会往任何真实会话里发东西，也不碰电源。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from molt import dshrelay  # noqa: E402  （放在 sys.path 之后，允许直接在仓库里跑）

PEER = "127.0.0.1"
PEER_PORT = 6191
WHITELIST_TEXT = "只允许这三个方法"


def _force_utf8() -> None:
    """Windows 控制台默认 GBK —— 不改就会在打中文时炸掉（踩过）。"""

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _http(method: str, url: str, body: Optional[Dict[str, Any]] = None, token: str = "") -> Tuple[int, str]:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Molt-Token"] = token
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


class Drill:
    def __init__(self) -> None:
        self.results: List[Dict[str, Any]] = []

    def check(self, name: str, ok: bool, detail: str) -> bool:
        self.results.append({"name": name, "ok": bool(ok), "detail": detail})
        return ok

    def failed(self) -> List[Dict[str, Any]]:
        return [item for item in self.results if not item["ok"]]


def _first_session_id(warden: str, token: str, shell: str) -> str:
    status, text = _http("POST", f"{warden}/dsh", {"shell": shell, "method": "session/list", "args": {"_request": {}}}, token)
    if status != 200:
        return ""
    try:
        items = json.loads(text)["dsh"]["items"]
    except Exception:
        return ""
    for item in items:
        sid = str(item.get("sessionId") or item.get("id") or "")
        if sid:
            return sid
    return ""


def run(*, warden: str, token: str, shell: str) -> Drill:
    drill = Drill()
    warden = warden.rstrip("/")

    # ---- 1. peer 活着 -----------------------------------------------------
    status, text = _http("GET", f"http://{PEER}:{PEER_PORT}/health")
    ok = status == 200 and "molt-dshpeer" in text
    drill.check("peer /health", ok, f"HTTP {status} {text[:120]}")

    # ---- 2. peer 鉴权 -----------------------------------------------------
    status, _ = _http("POST", f"http://{PEER}:{PEER_PORT}/dsh", {"method": "session/list", "args": {"_request": {}}})
    drill.check("peer 无 token → 401", status == 401, f"HTTP {status}")

    # ---- 3. 三个白名单方法 ------------------------------------------------
    try:
        items = dshrelay.peer_call(PEER, PEER_PORT, "session/list", {"_request": {}}, token=token)["items"]
        drill.check("peer session/list", isinstance(items, list) and len(items) > 0, f"{len(items) if isinstance(items, list) else '?'} 个会话")
        session_id = ""
        for item in items or []:
            sid = str(item.get("sessionId") or item.get("id") or "")
            if sid:
                session_id = sid
                break
    except dshrelay.DshRelayError as exc:
        drill.check("peer session/list", False, str(exc))
        session_id = ""

    if session_id:
        try:
            events = dshrelay.peer_call(
                PEER,
                PEER_PORT,
                "session/events",
                {"request": {"address": {"kind": "session", "sessionId": session_id}, "maxMessages": 3}},
                token=token,
            )
            records = events.get("records")
            drill.check("peer session/events", isinstance(records, list) and len(records) >= 0, f"session={session_id[:20]}… records={len(records or [])}")
        except dshrelay.DshRelayError as exc:
            drill.check("peer session/events", False, str(exc))
    else:
        drill.check("peer session/events", False, "没有可用的 sessionId")

    # prompt：故意打一个不存在的会话 —— 能进网关（回 session/not-found）就说明在白名单里，
    # 而且**一个字都没发出去**。这是唯一一个不碰真实会话的验法。
    try:
        dshrelay.peer_call(
            PEER,
            PEER_PORT,
            "session/prompt",
            {"request": {"requestId": "drill", "sessionId": "session-drill-does-not-exist", "mode": "queue", "content": [{"type": "text", "text": "drill"}]}},
            token=token,
        )
        drill.check("peer session/prompt（打不存在的会话）", False, "居然成功了 —— 不该发生")
    except dshrelay.DshRelayError as exc:
        message = str(exc)
        drill.check(
            "peer session/prompt（打不存在的会话）",
            "session/not-found" in message,
            f"{message[:120]}",
        )

    # ---- 4. 拒绝项 --------------------------------------------------------
    for method in ("session/create", "session/cancel"):
        status, text = _http("POST", f"http://{PEER}:{PEER_PORT}/dsh", {"method": method, "args": {}}, token)
        drill.check(f"peer {method} → 400", status == 400 and WHITELIST_TEXT in text, f"HTTP {status} {text[:100]}")

    # ---- 5. 调用方指定目标必须无效 ---------------------------------------
    status, text = _http(
        "POST",
        f"http://{PEER}:{PEER_PORT}/dsh",
        {"method": "session/list", "args": {"_request": {}}, "address": "http://10.255.255.1:9", "upstream": "http://10.255.255.1:9"},
        token,
    )
    ok = status == 200 and '"items"' in text
    drill.check("peer 忽略调用方指定的目标", ok, f"HTTP {status}（仍回本机结果）")

    # ---- 6. NAS 那一跳 ----------------------------------------------------
    status, _ = _http("POST", f"{warden}/dsh", {"shell": shell, "method": "session/list", "args": {"_request": {}}})
    drill.check("warden 无 token → 401", status == 401, f"HTTP {status}")

    status, text = _http("POST", f"{warden}/dsh", {"shell": shell, "method": "session/create", "args": {}}, token)
    drill.check("warden session/create → 400", status == 400 and WHITELIST_TEXT in text, f"HTTP {status} {text[:100]}")

    status, text = _http("POST", f"{warden}/dsh", {"shell": shell, "method": "session/list", "args": {"_request": {}}}, token)
    count = 0
    if status == 200:
        try:
            count = len(json.loads(text)["dsh"]["items"])
        except Exception:
            count = 0
    drill.check("warden session/list", status == 200 and count > 0, f"HTTP {status} {count} 个会话")

    nas_session = _first_session_id(warden, token, shell)
    status, text = _http(
        "POST",
        f"{warden}/dsh",
        {"shell": shell, "method": "session/events", "args": {"request": {"address": {"kind": "session", "sessionId": nas_session}, "maxMessages": 3}}},
        token,
    )
    drill.check("warden session/events", status == 200 and '"records"' in text, f"HTTP {status} session={(nas_session or '?')[:20]}…")

    status, text = _http(
        "POST",
        f"{warden}/dsh",
        {"shell": shell, "method": "session/prompt", "args": {"request": {"requestId": "drill", "sessionId": "session-drill-does-not-exist", "mode": "queue", "content": [{"type": "text", "text": "drill"}]}}},
        token,
    )
    drill.check("warden session/prompt（不存在的会话）", status == 502 and "session/not-found" in text, f"HTTP {status} {text[:100]}")

    # ---- 7. 端到端 CLI ----------------------------------------------------
    got = subprocess.run(
        [sys.executable, "-m", "molt.cli", "--no-color", "dsh", "sessions", "--shell", shell, "--warden-url", warden, "--warden-token", token],
        cwd=str(REPO),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    out = (got.stdout or "") + (got.stderr or "")
    drill.check("CLI molt dsh sessions", got.returncode == 0 and "会话" in out, f"rc={got.returncode} {out.strip().splitlines()[1:2]}")
    return drill


def main(argv: Optional[List[str]] = None) -> int:
    _force_utf8()
    parser = argparse.ArgumentParser(description="D18 那条链的一条命令回归")
    parser.add_argument("--warden-url", required=True)
    parser.add_argument("--warden-token", required=True)
    parser.add_argument("--shell", default="desktop-h2kgiao")
    parser.add_argument("--json", action="store_true", help="输出 JSON（给机器读）")
    args = parser.parse_args(argv)

    drill = run(warden=args.warden_url, token=args.warden_token, shell=args.shell)
    failed = drill.failed()

    if args.json:
        print(json.dumps({"results": drill.results, "failed": len(failed)}, ensure_ascii=False, indent=2))
    else:
        for item in drill.results:
            print(f"{'✓' if item['ok'] else '✗'} {item['name']} —— {item['detail']}")
        print()
        if failed:
            print(f"{len(failed)} / {len(drill.results)} 项没过。链断了，先看上面 ✗ 的那几条。")
        else:
            print(f"全部 {len(drill.results)} 项通过：NAS 账本 → agent:{PEER_PORT} → 127.0.0.1:3082 这条链是通的。")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
