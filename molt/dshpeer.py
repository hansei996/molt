"""在**有 DSH 的那台机器上**开一个小口，让账本代转进来。

## 为什么需要它

DSH 的网关只认回环对端：从 NAS 直接连 `http://<win>:3082/api/...`，即使把
`Host` 改写成 `127.0.0.1:3082`，对端仍然按**对端地址**判定，回 401。

所以那一跳必须发生在 Windows 本机。这个模块就是那一跳：

```
NAS warden(6186) ──HTTP──> Windows agent(6191) ──127.0.0.1──> DSH(3082)
```

## 三条边界（和 warden 那层同构）

- 方法白名单只有 `session/list`、`session/events`、`session/prompt`
- 必须带 `X-Molt-Token`（和账本同一个 token；用 `secrets.compare_digest` 比）
- 只转发到 `127.0.0.1:<port>`，**不接受调用方指定目标** —— 否则它就成了一个
  任意转发的隧道，而这里要的只是"把那一跳跃挪到本机来"。

没有请求重试、没有缓存：一次请求一次转发。
"""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

from . import dshrelay

#: 默认监听端口。选 6191 是因为 6199/6190 附近都是 AstrBot 的地盘。
DEFAULT_PORT = 6191

#: 单次请求的读取上限（JSON 而已，不需要更大）。
MAX_BODY = 1 << 20


class _Handler(BaseHTTPRequestHandler):
    server_version = "molt-dshpeer"

    # 由 `serve()` 注入
    token: str = ""
    upstream: str = "http://127.0.0.1:3082"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # 默认会往 stderr 打一行一行；守护进程的日志已经有自己的格式了。
        return

    def _reply(self, status: int, payload: Dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except OSError:
            pass

    def _authorized(self) -> bool:
        got = self.headers.get("X-Molt-Token") or ""
        return bool(self.token) and secrets.compare_digest(got, self.token)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("", "/health"):
            self._reply(200, {"ok": True, "service": "molt-dshpeer", "methods": list(dshrelay.ALLOWED_METHODS)})
            return
        self._reply(404, {"ok": False, "message": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/dsh":
            self._reply(404, {"ok": False, "message": "not found"})
            return
        if not self._authorized():
            self._reply(401, {"ok": False, "message": "token 不对"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._reply(400, {"ok": False, "message": "请求体为空或太大"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except ValueError:
            self._reply(400, {"ok": False, "message": "请求体不是合法 JSON"})
            return
        method = str((body or {}).get("method") or "")
        args = (body or {}).get("args") or {}
        if not isinstance(args, dict):
            args = {}
        if not dshrelay.is_allowed(method):
            self._reply(400, {"ok": False, "message": f"{method or '（空）'} 不在白名单里：{dshrelay.describe_scope()}"})
            return
        try:
            value = dshrelay.call(self.upstream, method, args)
        except dshrelay.DshRelayError as e:
            self._reply(502, {"ok": False, "message": str(e)})
            return
        self._reply(200, {"ok": True, "dsh": value})


class DshPeer:
    """一个可以被 `stop()` 的守护线程。"""

    def __init__(self, *, token: str, port: int = DEFAULT_PORT, upstream: str = "http://127.0.0.1:3082") -> None:
        if not token:
            raise ValueError("没有 token 的口子不能开")
        self.httpd = ThreadingHTTPServer(("0.0.0.0", int(port)), _Handler)
        self.httpd.token = token
        self.httpd.upstream = upstream
        handler_cls = self.httpd.RequestHandlerClass
        handler_cls.token = token  # type: ignore[attr-defined]
        handler_cls.upstream = upstream  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="molt-dshpeer", daemon=True)

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    def start(self) -> "DshPeer":
        self.thread.start()
        return self

    def stop(self) -> None:
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.httpd.server_close()
        except Exception:
            pass


def serve(*, token: str, port: int = DEFAULT_PORT, upstream: str = "http://127.0.0.1:3082") -> DshPeer:
    return DshPeer(token=token, port=port, upstream=upstream).start()


__all__ = ["DEFAULT_PORT", "DshPeer", "MAX_BODY", "serve"]

