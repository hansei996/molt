"""壳端的 HTTP 客户端：跟 NAS 上的 Warden 说话。

这是 `molt/shell.py` 里"身份与能力"的另一半——那半边回答"我是什么"，
这半边回答"我能不能拿到写灵魂的资格"。

## 为什么客户端要严于服务端

服务端只保证自己不犯错；客户端要保证**自己的错不会变成数据损坏**。
所以这里做三件服务端做不到的事：

1. **超时是硬性的**。没有超时的请求在有线局域网里也会挂住（拔网线、
   NAS 重启、交换机抽风），而挂住的迁徙进程比失败的迁徙进程危险得多
   ——它会在"我以为还活着"的状态里待很久。

2. **网络错误被翻译成 MoltError**。`urllib` 抛的是 `URLError` /
   `HTTPError` / `socket.timeout`，壳端上面那层（CLI）只认 `MoltError`
   的 `kind`。如果让原始异常逃出去，调用方只能写 `except Exception`，
   那就等于没有错误处理。

3. **4xx 与 5xx 的语义不同**。409/410 是"你的租约没了，去重拿"，
   401 是"你配置错了"，426 是"版本不匹配"，500 是"服务端炸了"。
   混成一个 `MoltError` 就把这四种处置方式合并成了一种。

## 为什么重试只对一部分错误做

只有"连接层失败"才重试。业务层的拒绝（epoch 过期、租约被抢）
重试一万次都是同一个结果，而且每重试一次都在浪费那 90 秒的 TTL。
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin

from .errors import (
    CapabilityMissing,
    EpochAhead,
    EpochStale,
    Fatal,
    LeaseExpired,
    LeaseHeld,
    MoltError,
    NeedsHuman,
    ProtocolMismatch,
    SelfHealable,
)
from .server import TOKEN_HEADER
from .version import PROTOCOL_VERSION


DEFAULT_TIMEOUT = 10.0
DEFAULT_RETRIES = 3
RETRY_BACKOFF = 0.5           # 第 n 次重试等 n * 0.5 秒，最多 1.5 秒

# 服务端回来的 `kind` → 要抛的异常。
# 这张表必须与 `server.py` 的 `_ERROR_MAP` 一一对应——一边改了另一边不改，
# 壳端就会把"租约被抢"当成"未知错误"处理，然后就出现了两个壳都在写。
_KIND_TO_EXC = {
    "epoch_stale": EpochStale,
    "epoch_ahead": EpochAhead,
    "lease_held": LeaseHeld,
    "lease_expired": LeaseExpired,
    "protocol_mismatch": ProtocolMismatch,
    "capability_missing": CapabilityMissing,
    "needs_human": NeedsHuman,
    "self_healable": SelfHealable,
    "fatal": Fatal,
}


class WardenClient:
    """`WardenServer` 的客户端。

    用法::

        c = WardenClient("http://192.168.1.10:6186", token="...")
        if not c.ping():
            ...
        lease = c.acquire("win-shell-1a2b3c4d")
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
    ) -> None:
        if not base_url:
            raise Fatal("WardenClient 需要一个 base_url，例如 http://192.168.1.10:6186")
        if not token:
            # 空 token 如果被服务端接受，等于局域网里任何人都能抢走灵魂。
            # 客户端先拒绝，比服务端后拒绝更早给出清楚的错误。
            raise Fatal("WardenClient 需要一个 token（服务端启动时会打印）")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.retries = max(1, retries)

    # -- 低层 -----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        data = None
        headers = {TOKEN_HEADER: self.token}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"

        last_exc: Optional[Exception] = None
        for attempt in range(self.retries):
            req = urllib.request.Request(
                url, data=data, headers=headers, method=method
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    payload = json.loads(raw.decode("utf-8"))
                    return self._unwrap(payload)
            except urllib.error.HTTPError as e:
                # HTTPError 是"服务端回话了，只是状态码不是 200"。
                # 这类错误**不能重试**：服务端已经把话说清楚了。
                raw = e.read()
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = {"ok": False, "message": raw.decode("utf-8", "replace")}
                raise self._to_error(payload, e.code) from None
            except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
                # 连接层失败——可能只是 NAS 正在喘气，值得重试。
                last_exc = e
                if attempt < self.retries - 1:
                    time.sleep(RETRY_BACKOFF * (attempt + 1))
                    continue
            except json.JSONDecodeError as e:
                # 服务端回了非 JSON——极端情况（比如被中间设备劫持到登录页）。
                raise Fatal(
                    f"服务端返回的不是 JSON（{url}）：{e}。"
                    "这通常意味着中间有设备改写了响应。"
                ) from None

        raise Fatal(
            f"连不上 Warden（{url}），试了 {self.retries} 次。"
            f"最后一处错误：{last_exc}"
        ) from last_exc

    def _unwrap(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if payload.get("ok"):
            return payload
        raise self._to_error(payload, 200)

    def _to_error(self, payload: Dict[str, Any], status: int) -> MoltError:
        kind = str(payload.get("kind") or "")
        message = str(payload.get("message") or f"HTTP {status}")

        # 版本不匹配要在业务错误之前判——因为协议不同的时候，
        # 对面说的 `kind` 可能根本不是我这个版本能理解的值。
        remote_proto = payload.get("protocol_version")
        if remote_proto is not None and remote_proto != PROTOCOL_VERSION:
            return ProtocolMismatch(
                f"协议版本不匹配：对面 {remote_proto}，本机 {PROTOCOL_VERSION}。"
                "两边必须同版本才能交接。"
            )

        exc_cls = _KIND_TO_EXC.get(kind)
        if exc_cls is not None:
            return exc_cls(message)
        return MoltError(f"{message}（HTTP {status}, kind={kind or '未提供'}）")

    # -- 高层：与 Warden 方法一一对应 ------------------------------------

    def ping(self) -> Dict[str, Any]:
        """探活。返回服务信息；连不上会抛 `Fatal`。"""
        return self._request("GET", "/")

    def try_ping(self) -> bool:
        """探活但不抛异常。用于"NAS 还在吗"这类只需要布尔答案的地方。"""
        try:
            self.ping()
            return True
        except MoltError:
            return False

    def status(self) -> Dict[str, Any]:
        return self._request("GET", "/status").get("status", {})

    def history(self) -> Dict[str, Any]:
        return self._request("GET", "/history").get("history", {})

    def set_shell(
        self,
        shell_id: str,
        *,
        role: Optional[str] = None,
        gpu: Optional[str] = None,
        wakeable: Optional[bool] = None,
        cost_per_hour: Optional[float] = None,
        label: Optional[str] = None,
        by: str = "",
    ) -> Dict[str, Any]:
        """登记"这台机器是什么"。**只传你想声明的字段** —— 没传的保持原样。

        这是局部更新，不是整份覆盖：守护进程启动时只声明它知道的那几个，
        而 gpu / cost_per_hour 往往是人事后手填的。整份覆盖会让手工填的
        字段在下次重启时消失，而那种 bug 的形态是"跑一阵之后就不见了"。
        """
        body: Dict[str, Any] = {"shell": shell_id}
        for key, value in (
            ("role", role),
            ("gpu", gpu),
            ("wakeable", wakeable),
            ("cost_per_hour", cost_per_hour),
            ("label", label),
        ):
            if value is not None:
                body[key] = value
        if by:
            body["by"] = by
        return self._request("POST", "/shell", body).get("shell", {})

    def shells(self) -> Dict[str, Dict[str, Any]]:
        return self._request("GET", "/shells").get("shells", {})

    def acquire(self, shell_id: str, ttl_seconds: Optional[int] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {"shell_id": shell_id}
        if ttl_seconds is not None:
            body["ttl_seconds"] = int(ttl_seconds)
        return self._request("POST", "/acquire", body).get("lease", {})

    def release(self, shell_id: str, epoch: int) -> None:
        self._request(
            "POST", "/release", {"shell_id": shell_id, "epoch": int(epoch)}
        )

    def check_can_start(self, shell_id: str) -> Dict[str, Any]:
        return self._request("POST", "/check_can_start", {"shell_id": shell_id})

    def push(
        self, shell_id: str, epoch: int, manifest: Dict[str, Any]
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/push",
            {"shell_id": shell_id, "epoch": int(epoch), "manifest": manifest},
        ).get("generation", {})

    def mark_consumed(self, generation: int, shell_id: str) -> Dict[str, Any]:
        """登记"这只壳已经把这一代取走并落位了"。

        走与 `push` 相同的错误通道：缺 shell_id 是 400（bad_request），
        账本里没有这一代是 500（fatal），同一代被两只壳认领是 422
        （needs_human）。壳端因此能明确区分"我配错了"、"账本不认"、
        和"两条鱼在打架"。
        """
        return self._request(
            "POST",
            f"/generations/{int(generation)}/consumed",
            {"shell_id": shell_id},
        ).get("generation", {})

    def generations(self) -> list:
        return self._request("GET", "/generations").get("generations", [])

    def next_generation(self) -> int:
        return int(self._request("GET", "/next_generation").get("generation", 0))

    # -- 归档的字节传输 --------------------------------------------------
    #
    # 这两个方法跟上面那些有一个根本区别：上面传的是**意图**（"我要租约"），
    # 这里传的是**字节**。所以它们不走 `_request`——那条路会把整个响应读进
    # 内存再 json.loads，对 30 MB 的二进制归档既不合适也不可能。

    def upload_archive(
        self,
        generation: int,
        archive_path: Path,
        *,
        expected_sha256: str = "",
    ) -> Dict[str, Any]:
        """把归档 PUT 给 Warden。

        `expected_sha256` 给了就带上，让服务端复核——能抓住"传输中坏了"。
        不给也能成，最终由目标壳的 `verify` 兜底（它重算每个工件的哈希）。
        """
        p = Path(archive_path)
        if not p.is_file():
            raise Fatal(f"没有这个归档：{p}")

        url = urljoin(self.base_url + "/", f"archive/{int(generation)}")
        size = p.stat().st_size
        headers = {
            TOKEN_HEADER: self.token,
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
        }
        if expected_sha256:
            headers["X-Molt-Sha256"] = expected_sha256

        # 请求体是打开的文件对象：urllib 会按 Content-Length 分块读，
        # 不会一次性把 30 MB 读进内存。
        last_exc: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                with open(p, "rb") as fh:
                    req = urllib.request.Request(
                        url, data=fh, headers=headers, method="PUT"
                    )
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        payload = json.loads(resp.read().decode("utf-8"))
                        return self._unwrap(payload)
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = {"ok": False, "message": raw.decode("utf-8", "replace")}
                raise self._to_error(payload, e.code) from None
            except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
                last_exc = e
                if attempt < self.retries - 1:
                    time.sleep(RETRY_BACKOFF * (attempt + 1))
                    continue

        raise Fatal(
            f"上传归档失败（{url}），试了 {self.retries} 次。"
            f"最后一处错误：{last_exc}"
        ) from last_exc

    def download_archive(
        self,
        generation: int,
        dest_path: Path,
        *,
        expected_sha256: str = "",
    ) -> Dict[str, Any]:
        """把 Warden 上的归档取下来写到 `dest_path`。

        边收边算 sha256，收完立刻比对——**不在校验之前让别人看见这个文件**。
        所以先写 `<dest>.part`，验过才 `os.replace`。这跟服务端上传时的
        做法是同一条理由：写入中途的文件绝不能被当成完整文件。
        """
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")

        url = urljoin(self.base_url + "/", f"archive/{int(generation)}")
        headers = {TOKEN_HEADER: self.token}

        last_exc: Optional[Exception] = None
        for attempt in range(self.retries):
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    digest = hashlib.sha256()
                    total = 0
                    with open(part, "wb") as fh:
                        while True:
                            chunk = resp.read(1 << 20)
                            if not chunk:
                                break
                            digest.update(chunk)
                            fh.write(chunk)
                            total += len(chunk)
                got = digest.hexdigest()
                if expected_sha256 and expected_sha256.lower() != got:
                    part.unlink(missing_ok=True)
                    raise Fatal(
                        f"下载的归档指纹不符：期望 {expected_sha256[:16]}…，"
                        f"实际 {got[:16]}…。这个文件已丢弃，没有落位。"
                    )
                os.replace(part, dest)
                return {"path": str(dest), "bytes": total, "sha256": got}
            except urllib.error.HTTPError as e:
                raw = e.read()
                part.unlink(missing_ok=True)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = {"ok": False, "message": raw.decode("utf-8", "replace")}
                raise self._to_error(payload, e.code) from None
            except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
                part.unlink(missing_ok=True)
                last_exc = e
                if attempt < self.retries - 1:
                    time.sleep(RETRY_BACKOFF * (attempt + 1))
                    continue

        raise Fatal(
            f"下载归档失败（{url}），试了 {self.retries} 次。"
            f"最后一处错误：{last_exc}"
        ) from last_exc

    def archives(self) -> list:
        return self._request("GET", "/archives").get("archives", [])

    # -- 中途备份 --------------------------------------------------------

    def upload_backup(
        self,
        backup_path: Path,
        *,
        shell: str = "",
        generation: int = 0,
        expected_sha256: str = "",
    ) -> Dict[str, Any]:
        """把一份中途备份 POST 给 Warden。

        与 `upload_archive` 的差别只有三处：路径是 `/backup`、方法用 POST
        （**没有 generation 可以拼进 URL**，备份不是交接，不占代）、
        壳名和代走请求头让人在 `backups/` 里看得出哪台机器什么时候推的。
        """
        p = Path(backup_path)
        if not p.is_file():
            raise Fatal(f"没有这份备份：{p}")
        url = urljoin(self.base_url + "/", "backup")
        size = p.stat().st_size
        headers = {
            TOKEN_HEADER: self.token,
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
            "X-Molt-Shell": shell or "",
            "X-Molt-Generation": str(int(generation or 0)),
        }
        if expected_sha256:
            headers["X-Molt-Sha256"] = expected_sha256

        last_exc: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                with open(p, "rb") as fh:
                    req = urllib.request.Request(url, data=fh, headers=headers, method="POST")
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        payload = json.loads(resp.read().decode("utf-8"))
                        return self._unwrap(payload).get("backup", {})
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = {"ok": False, "message": raw.decode("utf-8", "replace")}
                raise self._to_error(payload, e.code) from None
            except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
                last_exc = e
                if attempt < self.retries - 1:
                    time.sleep(RETRY_BACKOFF * (attempt + 1))
                    continue

        raise Fatal(
            f"上传备份失败（{url}），试了 {self.retries} 次。最后一处错误：{last_exc}"
        ) from last_exc

    def download_backup(self, name: str, dest_path: Path) -> Dict[str, Any]:
        """把一份中途备份取下来。边收边算 sha256，收完写 `.part` 再改名。"""
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        url = urljoin(self.base_url + "/", f"backup/{name}")
        headers = {TOKEN_HEADER: self.token}

        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                digest = hashlib.sha256()
                total = 0
                with open(part, "wb") as fh:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        digest.update(chunk)
                        fh.write(chunk)
                        total += len(chunk)
        except urllib.error.HTTPError as e:
            part.unlink(missing_ok=True)
            raw = e.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {"ok": False, "message": raw.decode("utf-8", "replace")}
            raise self._to_error(payload, e.code) from None
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            part.unlink(missing_ok=True)
            raise Fatal(f"下载备份失败（{url}）：{e}") from e

        os.replace(part, dest)
        return {"path": str(dest), "bytes": total, "sha256": digest.hexdigest()}

    def backups(self) -> list:
        return self._request("GET", "/backups").get("backups", [])

    def prune_backups(self, *, keep_recent: int = 12, dry_run: bool = True) -> Dict[str, Any]:
        return self._request(
            "POST", "/backups/prune", {"keep_recent": keep_recent, "dry_run": dry_run}
        )

    def prune(self, *, keep_recent: int = 2, dry_run: bool = True) -> Dict[str, Any]:
        """让 Warden 清理中转区。

        **默认 `dry_run=True` 是刻意的**：这个方法的调用方可能是一个
        定时任务，而"清理"是这个系统里唯一会真的删文件的操作。
        默认安全，想删就显式说要删。
        """
        return self._request(
            "POST", "/prune", {"keep_recent": int(keep_recent), "dry_run": bool(dry_run)}
        )

    # -- 插件配置包 ------------------------------------------------------
    #
    # 这个包很小（几十 KB），所以整份放进内存发出去就够了 ——
    # 不像归档那样要边读边发。

    def publish_plugin_bundle(
        self,
        payload: bytes,
        *,
        shell: str,
        sha256: str = "",
        plugins: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        if not shell:
            raise Fatal("上传插件配置包必须带壳名")
        url = urljoin(self.base_url + "/", "plugin-configs")
        headers = {
            TOKEN_HEADER: self.token,
            "Content-Type": "application/gzip",
            "Content-Length": str(len(payload)),
            "X-Molt-Shell": shell,
        }
        if sha256:
            headers[  # 与归档同一个头，服务端会复核
                "X-Molt-Sha256"
            ] = sha256
        if plugins:
            # 壳名以外还要告诉账本"这包里有哪些插件"：
            # 下载方据此先算交集，不必为了看一眼清单而解包。
            headers["X-Molt-Plugins"] = ",".join(plugins)

        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return self._unwrap(body)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = {"ok": False, "message": raw.decode("utf-8", "replace")}
            raise self._to_error(body, e.code) from None
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            raise Fatal(f"上传插件配置包失败（{url}）：{e}") from e

    def plugin_bundles(self) -> list:
        return self._request("GET", "/plugin-configs").get("plugin_bundles", [])

    def download_plugin_bundle(self, shell: str) -> bytes:
        """取回某个壳的插件配置包（原始字节）。"""
        url = urljoin(self.base_url + "/", f"plugin-configs/{quote(str(shell), safe='')}")
        req = urllib.request.Request(url, headers={TOKEN_HEADER: self.token}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = {"ok": False, "message": raw.decode("utf-8", "replace")}
            raise self._to_error(body, e.code) from None
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            raise Fatal(f"下载插件配置包失败（{url}）：{e}") from e

