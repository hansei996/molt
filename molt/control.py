"""Molt 侧的 AstrBot 控制面客户端。

## 这个模块解决的是"谁来把源壳按住"

交接协议（ARCH §5.2）要求**先死后生**：源壳必须停止对外说话，目标壳才能开口。
在 `astrbot_plugin_molt` 出现之前，唯一的办法是 `docker kill -s INT` 杀掉整个容器。
那能用，但它把"断开一个 IM 连接"和"杀掉一个进程"绑成了同一件事：

* 一次迁徙会让它丢掉调度器、定时任务、正在跑的一轮对话；
* 想恢复只能重启，而重启要重新拉 4173 个 LLM 元数据、重建所有连接。

现在源壳可以**只闭嘴**（`POST /drain`），迁完之后再**重新开口**（`POST /resume`）。

## 鉴权：为什么这里要自己签 JWT

AstrBot 的 `/api/plug/*` 走 dashboard 的 JWT 中间件
（`astrbot/dashboard/server.py:295` 起：`/api` 下除白名单外一律要求 token）。
所以这个客户端必须持有一个 dashboard JWT。两条路：

1. **调 `/api/auth/login` 拿一个** —— 需要 dashboard 的用户名密码。
   Molt 已经持有整个 `cmd_config.json`（里面有 `dashboard.jwt_secret`），
   再额外存一份密码是多余的秘密。
2. **用 `dashboard.jwt_secret` 自己签一个** —— 只要有 secret 就够，
   而这个 secret 本来就在 soul 里，Warden 侧天然能读到。

选 2。用标准库 `hmac` + `hashlib` + `base64` 手写 HS256，**不引入 PyJWT**：
Warden 必须能在"什么都没装"的 NAS 上跑（裸 python 3.10.7），
这是 `server.py` / `wake.py` 一以贯之的约束。

## 一个必须记住的后果

`jwt_secret` 是**可以签发任意 dashboard 身份**的东西。所以：

* `overlay.py` 落位时会重新生成它（原值跟着 soul 搬过去就等于把 NAS 的钥匙
  给了 Windows 那台机器）；
* 本客户端读的是**目标壳自己那份** `cmd_config.json` 里的 secret。

这两条合起来意味着：迁徙之后，源壳无法再控制目标壳，反之亦然。
这不是缺点——**一个能远程 drain 对方的壳，等于一个能远程关掉它的后门。**
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional
from urllib.parse import urljoin

from .errors import Fatal, MoltError, NeedsHuman

#: dashboard 的登录态默认有效期。AstrBot 自己签的是 7 天，
#: 我们只要够一次迁徙用 —— 短一点意味着泄漏窗口更小。
DEFAULT_TOKEN_TTL = 300

#: 控制面端点的固定前缀。AstrBot 把插件注册的路由拼成
#: `/api/plug/<插件目录名><route>`（`dashboard/api/plugins.py:209`）。
CONTROL_PREFIX = "/api/plug/astrbot_plugin_molt"

DEFAULT_TIMEOUT = 10.0


# ---------------------------------------------------------------------- JWT


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sign_dashboard_jwt(secret: str, username: str, *, ttl: int = DEFAULT_TOKEN_TTL) -> str:
    """用 AstrBot 的 `dashboard.jwt_secret` 签一个 HS256 dashboard token。

    这个 token 与 dashboard 网页登录后拿到的**在服务端眼里没有区别** ——
    这是刻意的：`_validate_dashboard_token`（`dashboard/server.py:388`）
    除了验签和过期时间，不检查别的。

    所以它的权限等于"以该用户名登录 dashboard 的全部权限"。
    调用方要自己保证这个 token 不被写进日志、不被留在磁盘上。
    """
    if not secret:
        raise Fatal("没有 dashboard.jwt_secret，签不出 token。这份 soul 的 cmd_config.json 可能不完整。")
    if not username:
        raise Fatal("没有 dashboard 用户名，签不出 token。")

    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {"username": username, "iat": now, "exp": now + int(ttl)}

    segments = [
        _b64url(json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")),
        _b64url(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")),
    ]
    signing_input = ".".join(segments).encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    segments.append(_b64url(signature))
    return ".".join(segments)


def read_dashboard_credentials(config_path) -> tuple[str, str]:
    """从 `cmd_config.json` 里取出 `(jwt_secret, username)`。

    必须用 `utf-8-sig` —— 这个文件带 BOM，普通 `utf-8` 在 Linux 上
    `json.load` 会直接抛 `JSONDecodeError: Unexpected UTF-8 BOM`（ARCH §6.6.5）。
    """
    from pathlib import Path

    p = Path(config_path)
    if not p.exists():
        raise NeedsHuman(f"找不到 AstrBot 配置：{p}\n  控制面需要它来签 dashboard token。")
    try:
        raw = p.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise Fatal(f"{p.name} 不是合法的 UTF-8（{exc}）。这个文件通常带 BOM，要用 utf-8-sig 读。") from exc
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Fatal(f"{p.name} 不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}") from exc

    dash = cfg.get("dashboard") or {}
    secret = dash.get("jwt_secret") or ""
    user = dash.get("username") or ""
    return str(secret), str(user)


# ---------------------------------------------------------------------- 客户端


def push_text(
    config_path,
    text: str,
    *,
    base: str = "http://127.0.0.1:6199",
    timeout: float = 20.0,
) -> bool:
    """用 dashboard 的 JWT 打插件的 `/notify`，把一句话送到主人私聊里。

    返回 False 只表示"这次没送出去"（没配 secret、连不上、插件没升级）。
    调用方（守护进程）**不该因此放弃搬家** —— 提醒是体验，不是前置条件。
    """
    secret, user = read_dashboard_credentials(config_path)
    if not secret:
        return False
    body = json.dumps({"text": str(text)}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{str(base).rstrip('/')}/api/plug/astrbot_plugin_molt/notify",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {sign_dashboard_jwt(secret, user)}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= int(getattr(resp, "status", 200)) < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


class AstrBotControl:
    """对一个 AstrBot 实例的控制面。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        prefix: str = CONTROL_PREFIX,
    ):
        if not base_url:
            raise Fatal("没有 AstrBot 的地址（形如 http://127.0.0.1:6199）。")
        if not token:
            raise Fatal("没有 dashboard token。控制面不提供匿名访问。")
        self.base_url = base_url.rstrip("/") + "/"
        self.token = token
        self.timeout = timeout
        self.prefix = prefix.rstrip("/")

    # -- 构造 ---------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        base_url: str,
        config_path,
        *,
        ttl: int = DEFAULT_TOKEN_TTL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> "AstrBotControl":
        """从目标壳自己的 `cmd_config.json` 现签一个 token。"""
        secret, user = read_dashboard_credentials(config_path)
        token = sign_dashboard_jwt(secret, user, ttl=ttl)
        return cls(base_url, token, timeout=timeout)

    def where(self) -> str:
        return f"AstrBot 控制面 {self.base_url.rstrip('/')}{self.prefix}"

    # -- 底层 ---------------------------------------------------------

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, self.prefix.lstrip("/") + path)

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self._url(path)
        data = None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            # 401/403 = 签错了或 secret 不对；这是配置问题，不该重试。
            if exc.code in (401, 403):
                raise NeedsHuman(
                    f"控制面拒绝了我们的 token（HTTP {exc.code}）。\n"
                    f"  多半是这份 cmd_config.json 里的 dashboard.jwt_secret 与对面实际用的不一致。\n"
                    f"  地址：{url}"
                ) from exc
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            # 409 是控制面的"业务拒绝"（比如 resume 前没 drain），要原样带出去。
            if exc.code == 409:
                raise MoltError(f"控制面拒绝了这次操作（HTTP 409）：{detail}") from exc
            raise Fatal(f"控制面返回 HTTP {exc.code}：{detail or url}") from exc
        except urllib.error.URLError as exc:
            raise NeedsHuman(
                f"连不上 AstrBot 控制面：{url}\n  原因：{exc.reason}\n"
                f"  检查：那个壳在跑吗？端口对吗？插件 astrbot_plugin_molt 装了吗？"
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise Fatal(f"控制面请求失败：{url}（{exc}）") from exc

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Fatal(f"控制面返回的不是 JSON：{url}（{exc}）") from exc

        if not isinstance(payload, dict):
            raise Fatal(f"控制面返回的不是对象：{url}")
        if payload.get("success") is False:
            raise MoltError(f"控制面报告失败：{payload.get('error') or payload}")
        return payload

    # -- 端点 ---------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        return self._request("GET", "/status")

    def drain(self) -> Dict[str, Any]:
        """让它停止对外说话。

        幂等：已经 drain 过再调一次，返回的 `drained` 是空的，`still_connected` 仍是 0。
        """
        return self._request("POST", "/drain", {})

    def resume(self) -> Dict[str, Any]:
        """让它重新开口。**要求之前 drain 过**，否则控制面会 409。"""
        return self._request("POST", "/resume", {})

    def checkpoint(self) -> Dict[str, Any]:
        """把 WAL 合并进主库。"""
        return self._request("POST", "/checkpoint", {})

    # -- 复合动作 -----------------------------------------------------

    def is_speaking(self) -> Optional[bool]:
        """它现在在对外说话吗？`None` = 问不出来（不猜）。"""
        try:
            st = self.status()
        except MoltError:
            return None
        speaking = st.get("speaking")
        return bool(speaking) if isinstance(speaking, bool) else None

    def drain_and_verify(self, *, attempts: int = 3, delay: float = 0.4) -> Dict[str, Any]:
        """drain 并**确认**它真的不说话了。

        为什么要 confirm 而不是相信 drain 的返回值：`terminate_platform`
        在某些适配器上只保证"发起了关闭"（见 `platform/manager.py:298` 的
        `except Exception: logger.warning(...)` —— 关不掉也只是一条警告）。
        一次迁徙里"我以为它闭嘴了但其实没有"就是两条鱼同时在线。
        """
        first = self.drain()
        speaking = self.is_speaking()

        tries = 0
        while speaking and tries < attempts:
            time.sleep(delay)
            speaking = self.is_speaking()
            tries += 1

        if speaking:
            raise NeedsHuman(
                "drain 之后它仍在对外说话。**不要继续这次迁徙**——"
                "现在启动目标壳就会变成两条鱼同时在线（公理 0）。\n"
                "  手工确认：curl 那个壳的 /api/plug/astrbot_plugin_molt/status"
            )
        result = dict(first)
        result["verified_silent"] = True
        result["confirm_attempts"] = tries
        return result
