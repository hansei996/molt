"""Warden 的 HTTP 控制面：让远端的壳能跟 NAS 上的权威说话。

## 为什么不直接共享文件

`Warden` 是一个操作本地文件系统的类。开发时它跑在哪台机器上都能工作，
但**真实部署**是：

    壳在 Windows  ──网络──>  权威在 NAS

壳不能直接读写 NAS 上的 `state.json`——那需要 SMB/NFS 共享，而这条路径
恰好是本项目一开始就判定为"不可靠"的东西（ARCH §3）。即使技术上能挂上，
也不该让"谁有资格写灵魂"这个判断依赖一个网络文件系统的锁语义。

所以控制面走 HTTP：**唯一的权威进程住在库旁边**（这正是 SQLite 官方
`useovernet.html` 建议的 "implement a proxy that runs on the database
machine"），远端通过明确定义的接口请求它做事。

## 认证

一个共享密钥，放在请求头 `X-Molt-Token` 里。用 `secrets.compare_digest`
做常量时间比较——普通 `==` 会在第一个不同字节处返回，理论上可被计时攻击
逐字节猜出来。这在家用局域网里是偏执，但成本只有一行。

启动时如果没给 token 就**随机生成并打印一次**，而不是允许空 token 运行。
允许空 token 的后果是：任何能看到 6186 端口的人都能把灵魂抢走。

## 为什么用标准库

不引 aiohttp / fastapi / quart。理由：

  1. Warden 必须能在一台"什么都没装"的 NAS 上跑起来。实测那台 NAS 的
     宿主 python 是 3.10.7 的裸环境，装任何东西都可能踩到系统包管理。
  2. 这个服务只有 7 个端点、并发量是"两只鱼交接"，不需要异步框架。
  3. 依赖越少，出问题时能怪的东西越少。

`http.server.ThreadingHTTPServer` + `BaseHTTPRequestHandler` 足够。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .errors import Fatal, MoltError, NeedsHuman
from .render import render_timeline
from .version import PROTOCOL_VERSION, __version__
from .warden import Warden


TOKEN_HEADER = "X-Molt-Token"
SHA_HEADER = "X-Molt-Sha256"
DEFAULT_PORT = 6186          # 6185 被 AstrBot dashboard 占了，邻居一位
ARCHIVE_ROUTE = re.compile(r"^/archive/(\d+)$")
#: `POST /backup` 上传、`GET /backup/<name>` 下载。分开写而不是一个可选组：
#: 上传时 URL 里没有名字（名字由服务端按时间戳和壳名生成），下载时必须有。
BACKUP_NAME_ROUTE = re.compile(r"^/backup/([^/]+)$")



# ---------------------------------------------------------------------------
# 请求/响应的小工具
# ---------------------------------------------------------------------------


class _Response:
    """一个端点想返回什么。

    `payload` 是常规的 JSON 响应。`stream_path` 用于归档下载——那条路径
    返回的是几兆到几十兆的字节，塞进 JSON 要 base64 膨胀 33%，而且会让
    "端点语义"和"大文件搬运"混成同一套错误处理。
    """

    def __init__(
        self,
        status: int,
        payload: Dict[str, Any],
        *,
        stream_path: Optional[Path] = None,
        stream_sha256: str = "",
        html: Optional[str] = None,
    ) -> None:
        self.status = status
        self.payload = payload
        self.stream_path = stream_path
        self.stream_sha256 = stream_sha256
        self.html = html


def _where_html(status: Dict[str, Any]) -> str:
    """「鱼现在在哪」的一页纸。

    只用内联样式、零外部资源：这个页面会被人在手机上、在别的机器上打开，
    任何一次外部请求失败都会让"它到底在哪"变成一张白屏。
    """
    beats = status.get("heartbeats") or {}
    latest = status.get("latest_generation") or {}
    lease = status.get("lease") or {}

    def esc(text: Any) -> str:
        import html as _html

        return _html.escape(str(text if text is not None else ""))

    rows = []
    for shell_id, beat in sorted(beats.items(), key=lambda kv: kv[1].get("age_seconds", 0)):
        stale = beat.get("stale")
        color = "#b42318" if stale else "#067647"
        word = "哑了" if stale else "在跑"
        label = str(beat.get("label") or "").strip()
        name = (
            f"<b>{esc(label)}</b><br><span style=\"color:#98a2b3;font-size:12px\">{esc(shell_id)}</span>"
            if label
            else f"<b>{esc(shell_id)}</b>"
        )
        rows.append(
            "<tr>"
            f"<td>{name}</td>"
            f'<td style="color:{color};font-weight:600">{word}</td>'
            f"<td>{esc(beat.get('age_readable', ''))}</td>"
            f"<td>{esc(beat.get('generation', ''))}</td>"
            f"<td>{esc(beat.get('note', ''))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="5" style="color:#667085">还没有任何壳发过心跳</td></tr>')

    lease_text = (
        f"被 <b>{esc(lease.get('holder'))}</b> 持有（还剩 {esc(lease.get('remaining_seconds'))} 秒）"
        if lease.get("state") == "active"
        else "没有租约 —— 没有任何壳在搬家"
    )
    gen_text = (
        f"第 <b>{esc(latest.get('generation'))}</b> 代 · 目标壳 "
        f"<b>{esc(latest.get('target_shell') or '?')}</b> · 取走的是 "
        f"<b>{esc(latest.get('consumed_by') or '还没人取')}</b>"
        if latest
        else "账本里还没有任何一代"
    )

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>鱼现在在哪</title>
<style>
 body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
      margin:0;padding:24px;background:#f9fafb;color:#101828}}
 .card{{max-width:760px;margin:0 auto;background:#fff;border:1px solid #eaecf0;
        border-radius:12px;padding:20px 24px}}
 h1{{font-size:20px;margin:0 0 4px}}
 p.sub{{color:#667085;font-size:13px;margin:0 0 18px}}
 table{{width:100%;border-collapse:collapse;font-size:14px}}
 th{{text-align:left;color:#667085;font-weight:500;font-size:13px;
     border-bottom:1px solid #eaecf0;padding:8px 6px}}
 td{{border-bottom:1px solid #f2f4f7;padding:10px 6px}}
 .meta{{font-size:14px;line-height:1.9;color:#344054;margin-top:16px}}
 .meta b{{color:#101828}}
</style></head><body>
<div class="card">
  <h1>鱼现在在哪</h1>
  <p class="sub">数据来自各个壳自己上报的心跳。心跳超过 10 分钟没更新就算「哑了」。</p>
  <table>
    <thead><tr><th>壳</th><th>状态</th><th>最后心跳</th><th>代</th><th>说明</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <div class="meta">
    <div>账本：{gen_text}</div>
    <div>租约：{lease_text}</div>
    <div>当前 epoch：<b>{esc(status.get('current_epoch'))}</b> ·
         已登记 <b>{esc(status.get('generation_count'))}</b> 代</div>
  </div>
</div>
</body></html>"""


def _ok(**fields: Any) -> _Response:
    body = {"ok": True}
    body.update(fields)
    return _Response(200, body)


def _err(status: int, message: str, kind: str = "error") -> _Response:
    return _Response(status, {"ok": False, "kind": kind, "message": message})


# 归档上限。真实的 generation 是 30 MB 上下（两个库 + 配置），留 20 倍余量
# 已经很宽松；设上限不是为了省空间，是为了**一个写错的循环不要把 NAS 的
# 7.3T 填满**。磁盘满会让 AstrBot 自己也挂掉，那比这次迁徙失败严重得多。
MAX_ARCHIVE_BYTES = 640 * 1024 ** 2



# 错误类型 → (HTTP 状态码, 机器可读的 kind)。
# 这张表是壳端判断"该重试、该放弃、还是该叫人"的唯一依据，
# 所以每一条都要有确定的语义，不能糊成 500。
_ERROR_MAP: Tuple[Tuple[str, int, str], ...] = (
    # 名字,              HTTP,  kind
    ("EpochStale",        409,  "epoch_stale"),
    ("EpochAhead",        409,  "epoch_ahead"),
    ("LeaseHeld",         409,  "lease_held"),
    ("LeaseExpired",      410,  "lease_expired"),
    ("ProtocolMismatch",  426,  "protocol_mismatch"),
    ("CapabilityMissing", 412,  "capability_missing"),
    ("NeedsHuman",        422,  "needs_human"),
    ("SelfHealable",      503,  "self_healable"),
    ("Fatal",             500,  "fatal"),
)


def _classify(exc: MoltError) -> Tuple[int, str]:
    name = type(exc).__name__
    for known, status, kind in _ERROR_MAP:
        if name == known:
            return status, kind
    return 500, "unknown"


# ---------------------------------------------------------------------------
# 服务端
# ---------------------------------------------------------------------------


class WardenServer:
    """把 `Warden` 暴露成一个小的 HTTP 服务。

    线程模型：`ThreadingHTTPServer`，每个请求一个线程。`Warden` 自己用
    跨进程文件锁保护状态，所以即使多个请求线程同时进来也安全——
    这也是为什么这里不需要再套一层 `threading.Lock`：**锁的层次越少，
    死锁的机会越少**，而 `Warden` 的锁已经被 5 个并发进程的测试证明过了。
    """

    def __init__(
        self,
        root: Optional[Path] = None,
        *,
        token: Optional[str] = None,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
    ) -> None:
        self.warden = Warden(root=root)
        self.token = token or secrets.token_urlsafe(32)
        self.generated_token = token is None
        self.host = host
        self.port = port
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # -- 端点表 ---------------------------------------------------------

    def dispatch(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]],
    ) -> _Response:
        """把一次请求路由到对应的 `Warden` 方法。

        单独抽出来是为了能**脱离 HTTP 直接测**。测一个端点不该需要起套接字、
        抢端口、或者跟 TCP 的时序较劲。
        """
        body = body or {}
        parts = [p for p in path.strip("/").split("/") if p]

        if not parts:
            return _ok(
                service="molt-warden",
                version=__version__,
                protocol_version=PROTOCOL_VERSION,
            )

        head = parts[0]

        try:
            # ---- GET /status ----
            if head == "status" and method == "GET":
                return _ok(status=self.warden.status())

            # ---- POST /heartbeat ----
            if head == "heartbeat" and method == "POST":
                shell_id = str(body.get("shell_id") or "").strip()
                if not shell_id:
                    return _err(400, "缺少 shell_id", "bad_request")
                try:
                    generation = int(body.get("generation") or 0)
                except (TypeError, ValueError):
                    generation = 0
                beat = self.warden.record_heartbeat(
                    shell_id,
                    generation=generation,
                    note=str(body.get("note") or ""),
                    label=str(body.get("label") or ""),
                )
                return _ok(heartbeat=beat)

            # ---- POST /power ----（「走」和「关」是两件事，这是第二件）
            if head == "power" and parts[1:2] == [] and method == "POST":
                shell_id = str(body.get("shell") or "").strip()
                if not shell_id:
                    return _err(400, "缺少 shell", "bad_request")
                try:
                    generation = int(body.get("generation") or 0)
                except (TypeError, ValueError):
                    generation = 0
                ticket = self.warden.request_power(
                    shell_id,
                    str(body.get("action") or ""),
                    generation=generation,
                    by=str(body.get("by") or ""),
                )
                return _ok(power=ticket)

            # ---- POST /power/ack ----（票用掉了就撕掉，别让它下次又生效）
            if head == "power" and parts[1:2] == ["ack"] and method == "POST":
                shell_id = str(body.get("shell") or "").strip()
                if not shell_id:
                    return _err(400, "缺少 shell", "bad_request")
                return _ok(cleared=self.warden.clear_power(shell_id))

            # ---- GET /where ----
            # 一个给人看的页面，不是给机器用的。它回答的是「鱼现在在哪」，
            # 而这个问题的答案以前只能靠「问它」。
            if head == "where" and method == "GET":
                return _Response(
                    200, {"ok": True}, html=_where_html(self.warden.status())
                )

            # ---- GET /history ----
            # 时间轴的数据面。和 /where 分开：一个回答「现在」，一个回答「过程」。
            if head == "history" and method == "GET":
                return _ok(history=self.warden.history())

            # ---- GET /timeline ----
            # 时间轴的页面。和 /where 一样是给人看的，所以也认 `?token=`。
            if head == "timeline" and method == "GET":
                return _Response(
                    200,
                    {"ok": True},
                    html=render_timeline(self.warden.history()),
                )

            # ---- POST /acquire ----
            if head == "acquire" and method == "POST":
                shell_id = body.get("shell_id")
                if not shell_id:
                    return _err(400, "缺少 shell_id", "bad_request")
                ttl = body.get("ttl_seconds")
                lease = (
                    self.warden.acquire(shell_id, int(ttl))
                    if ttl is not None
                    else self.warden.acquire(shell_id)
                )
                return _ok(lease=lease.to_dict())

            # ---- POST /release ----
            if head == "release" and method == "POST":
                shell_id = body.get("shell_id")
                epoch = body.get("epoch")
                if not shell_id or epoch is None:
                    return _err(400, "缺少 shell_id 或 epoch", "bad_request")
                self.warden.release(shell_id, int(epoch))
                return _ok()

            # ---- POST /check_can_start ----
            if head == "check_can_start" and method == "POST":
                shell_id = body.get("shell_id")
                if not shell_id:
                    return _err(400, "缺少 shell_id", "bad_request")
                can, reason = self.warden.check_can_start(shell_id)
                return _ok(can_start=can, reason=reason)

            # ---- POST /push ----
            if head == "push" and method == "POST":
                claimed_epoch = body.get("epoch")
                claimed_shell = body.get("shell_id")
                manifest = body.get("manifest")
                if claimed_epoch is None or not claimed_shell or not manifest:
                    return _err(
                        400, "缺少 epoch / shell_id / manifest", "bad_request"
                    )
                record = self.warden.accept_snapshot(
                    claimed_epoch=int(claimed_epoch),
                    claimed_shell_id=str(claimed_shell),
                    manifest=manifest,
                )
                return _ok(generation=record.to_dict())

            # ---- GET /generations ----
            if head == "generations" and method == "GET":
                return _ok(
                    generations=[g.to_dict() for g in self.warden.generations]
                )

            # ---- POST /generations/<n>/consumed ----
            if (
                head == "generations"
                and method == "POST"
                and len(parts) == 3
                and parts[2] == "consumed"
            ):
                try:
                    generation = int(parts[1])
                except (TypeError, ValueError):
                    return _err(
                        400,
                        f"generation 必须是整数：{parts[1]!r}",
                        "bad_request",
                    )
                shell_id = body.get("shell_id")
                if not shell_id:
                    return _err(400, "缺少 shell_id", "bad_request")
                record = self.warden.mark_consumed(generation, str(shell_id))
                return _ok(generation=record.to_dict())

            # ---- GET /next_generation ----
            if head == "next_generation" and method == "GET":
                return _ok(generation=self.warden.next_generation_number())

            # ---- GET /archives ----
            if head == "archives" and method == "GET":
                return _ok(archives=self.list_archives())

            # ---- POST /prune ----
            if head == "prune" and method == "POST":
                keep_recent = body.get("keep_recent", 2)
                dry_run = body.get("dry_run", True)
                try:
                    keep_recent = int(keep_recent)
                except (TypeError, ValueError):
                    return _err(
                        400,
                        f"keep_recent 必须是整数：{keep_recent!r}",
                        "bad_request",
                    )
                return _ok(
                    **self.warden.prune_archives(
                        keep_recent=keep_recent, dry_run=bool(dry_run)
                    )
                )

            # ---- GET /backups ----
            # 中途备份的索引。它们不是交接（没有目标壳），所以不走 generations。
            if head == "backups" and method == "GET":
                items = self.warden.list_backups()
                return _ok(
                    backups=items,
                    total_bytes=sum(int(x.get("bytes") or 0) for x in items),
                    backup_dir=str(self.warden.backup_dir()),
                )

            # ---- POST /backups/prune ----
            if head == "backups" and method == "POST" and len(parts) == 2 and parts[1] == "prune":
                try:
                    keep_recent = int(body.get("keep_recent", 12))
                except (TypeError, ValueError):
                    return _err(400, "keep_recent 必须是整数", "bad_request")
                return _ok(
                    **self.warden.prune_backups(
                        keep_recent=keep_recent,
                        dry_run=bool(body.get("dry_run", True)),
                    )
                )

        except MoltError as e:
            status, kind = _classify(e)
            return _err(status, str(e), kind)

        return _err(404, f"未知路径：{method} {path}", "not_found")

    # -- 归档的字节传输 --------------------------------------------------

    def list_archives(self) -> list:
        """列出中转区里现存的归档。

        直接复用 `Warden.list_archives()` 而不是自己扫目录：那份实现会
        标出"账本里记着"的 generation，而这个信息正是 `molt prune` 判断
        "能不能删"的唯一依据。两处各写一遍迟早会漂移，然后出现
        "服务端说能删、清理时却不敢删"这种没人能解释的状态。
        """
        return self.warden.list_archives()

    def stream_archive(self, generation: int) -> _Response:
        """把中转区里的归档交出去。

        这个端点是 `_Response.stream_path` 唯一的用户。为什么不塞进 JSON：
        30 MB 的二进制 base64 之后是 40 MB，多出来的四分之一要在内存里
        同时存在两份，而 NAS 上这个进程只是一个 20 MB 的 python。
        """
        path = self.warden.archive_path(generation)
        if not path.is_file():
            return _err(
                404,
                f"中转区里没有 generation {generation} 的归档。"
                f"源壳需要先 PUT 上来，或者用 molt migrate --upload 自动传。",
                "not_found",
            )
        return _Response(
            200,
            {"ok": True, "generation": generation, "bytes": path.stat().st_size},
            stream_path=path,
        )

    # -- 中途备份的字节传输 ----------------------------------------------

    def accept_backup(self, tmp_path: Path, claimed_sha: str, shell: str, generation: int) -> _Response:
        """接收壳自己定时推上来的中途备份。

        它和归档走同一套"边收边写盘 → 算哈希 → 原子落位"，但**不碰账本**：
        备份没有目标壳、不占用 generation、不需要租约。理由写在
        `Warden.backup_dir()` 的注释里——它一旦进了 `generations`，
        壳那边的 `decide()` 就会把它当成一次真的交接。
        """
        if not tmp_path.is_file():
            return _err(400, "没有收到备份内容", "bad_request")
        size = tmp_path.stat().st_size
        if size == 0:
            tmp_path.unlink(missing_ok=True)
            return _err(400, "备份是空的", "bad_request")

        # 与归档同一条顺序：**先算哈希，再收下**。
        # 声称的和实际不符时，这份字节不能被收进 `backups/` ——
        # 否则以后取回来的东西和索引上写的不是同一个。
        digest = hashlib.sha256()
        with open(tmp_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        got = digest.hexdigest()
        if claimed_sha and claimed_sha.lower() != got:
            tmp_path.unlink(missing_ok=True)
            return _err(
                400,
                f"备份指纹不符：声明 {claimed_sha[:16]}…，实际 {got[:16]}…。"
                f"这份备份在传输中损坏了，已丢弃。",
                "checksum_mismatch",
            )

        try:
            meta = self.warden.store_backup(
                tmp_path,
                shell=shell,
                generation=generation,
                sha256=got,
            )
        except MoltError as e:
            tmp_path.unlink(missing_ok=True)
            status, kind = _classify(e)
            return _err(status, str(e), kind)

        return _ok(backup=meta)

    def stream_backup(self, name: str) -> _Response:
        try:
            path = self.warden.backup_path(name)
        except MoltError as e:
            status, kind = _classify(e)
            return _err(status, str(e), kind)
        if not path.is_file():
            return _err(404, f"没有这份备份：{name}", "not_found")
        return _Response(
            200,
            {"ok": True, "name": name, "bytes": path.stat().st_size},
            stream_path=path,
        )

    def accept_archive(self, generation: int, tmp_path: Path, claimed_sha: str) -> _Response:
        """接收源壳 PUT 上来的归档。

        三件事按顺序做，顺序不能变：

          1. 大小上限（在读到内存之前就拦住，见 handler）
          2. **算哈希再落位**——先落位后校验意味着一个坏归档已经在
             正确的文件名上，任何并发读者都会把它当成好的
          3. `os.replace` 原子落位——写到一半的 `.part` 绝不能被
             当成完整归档（那正是"看起来像崩溃现场"的制造方法）
        """
        if not tmp_path.is_file():
            return _err(400, "没有收到归档内容", "bad_request")

        size = tmp_path.stat().st_size
        if size == 0:
            tmp_path.unlink(missing_ok=True)
            return _err(400, "归档是空的", "bad_request")

        digest = hashlib.sha256()
        with open(tmp_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        got = digest.hexdigest()

        # 校验头里的 sha256 是可选的：源壳在上传前自己算过一次，
        # 传上来让服务端复核，能抓住"传输过程中坏了"这一类。
        # 没给就跳过——毕竟文件内容是完整的，坏没坏最终由目标壳的
        # verify 兜底（它会重新算每个工件的 sha256）。
        if claimed_sha and claimed_sha.lower() != got:
            tmp_path.unlink(missing_ok=True)
            return _err(
                400,
                f"归档指纹不符：声明 {claimed_sha[:16]}…，实际 {got[:16]}…。"
                f"这份归档在传输中损坏了，不要重试同一个文件，重新打包一次。",
                "checksum_mismatch",
            )

        dest = self.warden.archive_path(generation)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Windows 上 os.replace 目标已存在时也能覆盖，但目标被别的进程
        # 打开着会抛 PermissionError——这时报一句人话，而不是裸异常。
        try:
            os.replace(tmp_path, dest)
        except OSError as e:
            tmp_path.unlink(missing_ok=True)
            raise Fatal(
                f"归档落位失败：{dest}（{e}）。"
                f"如果目标文件正被另一个进程读取，等它结束再试。"
            ) from e

        self.warden.record_archive_sha(generation, got)
        self.warden.append_log(
            "INFO", f"收到归档 gen-{generation:06d} {size} 字节 sha256={got[:16]}…"
        )
        return _ok(generation=generation, bytes=size, sha256=got)


    # -- HTTP 包装 ------------------------------------------------------

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = f"molt-warden/{__version__}"

            # 关掉默认的 stderr 访问日志：它会为每个请求打印一行，
            # 在 NAS 上长时间跑会把 syslog 淹掉。真正重要的操作
            # （谁拿了租约、谁被拒了）由 Warden 自己的 append_log 记。
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def _authorized(self) -> bool:
                got = self.headers.get(TOKEN_HEADER, "")
                if secrets.compare_digest(got, server.token):
                    return True
                # `/where` 与 `/timeline` 是给人用手机浏览器打开的一页纸，而浏览器
                # 没法加自定义头。只给它们开后门：token 走查询串，并且**只认这两个
                # 只读页面**。`/history` 不在名单里 —— 它是给脚本用的 JSON，脚本加
                # 一个头没有任何代价，而"把它也放进来"会让能改租约的 token 有更多
                # 机会出现在浏览历史、转发和截图里。
                parsed = urlparse(self.path)
                if parsed.path.rstrip("/").endswith(("/where", "/timeline")):
                    given = (parse_qs(parsed.query).get("token") or [""])[0]
                    return secrets.compare_digest(given, server.token)
                return False

            def _read_body(self) -> Optional[Dict[str, Any]]:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return None
                raw = self.rfile.read(length)
                try:
                    return json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return None

            def _send(self, resp: _Response) -> None:
                # 流式响应：不读进内存，边读边发。30 MB 的归档对 NAS 的
                # 7.6 GB 内存不算什么，但"用多少拿多少"是这条路径唯一
                # 合理的写法——它不该有随文件大小增长的峰值内存。
                if resp.stream_path is not None:
                    path = resp.stream_path
                    try:
                        size = path.stat().st_size
                    except OSError:
                        self._send(_err(404, "归档在发送过程中消失了", "not_found"))
                        return
                    self.send_response(resp.status)
                    self.send_header(
                        "Content-Type", "application/octet-stream"
                    )
                    self.send_header("Content-Length", str(size))
                    self.send_header("X-Molt-Generation", str(resp.payload.get("generation", "")))
                    self.end_headers()
                    try:
                        with open(path, "rb") as fh:
                            shutil.copyfileobj(fh, self.wfile, length=1 << 20)
                    except (BrokenPipeError, ConnectionResetError):
                        # 壳那头提前断开（用户按了 Ctrl+C 之类）。这不是
                        # 服务端的错，但也不该让异常爬到 serve_forever 里
                        # 打出一整段 traceback 淹掉日志。
                        return
                    return

                if resp.html is not None:
                    raw = resp.html.encode("utf-8")
                    self.send_response(resp.status)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return

                payload = dict(resp.payload)
                payload.setdefault("protocol_version", PROTOCOL_VERSION)
                payload.setdefault("service_version", __version__)
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(resp.status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _receive_archive(self, generation: int) -> None:
                """把请求体写进临时文件，再交给 dispatch 之外的那条路径处理。

                为什么不用 `_read_body`：那是给 JSON 用的（先读进内存再
                `json.loads`）。归档是二进制、可能几十兆，**边收边写盘**
                才不会让一次上传把内存顶起来。

                临时文件建在归档目录**同一个文件系统**上（同目录的 `.part`），
                这样最后一步 `os.replace` 才是原子的。跨文件系统的 rename
                会被退化成"复制 + 删除"，那就没有原子性了。
                """
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    self._send(_err(400, "没有 Content-Length 或长度为 0", "bad_request"))
                    return
                if length > MAX_ARCHIVE_BYTES:
                    # 直接拒绝并且不读 body：读进来再判断等于先让人把
                    # 640 MB 传完再说"不行"。
                    self._send(
                        _err(
                            413,
                            f"归档 {length} 字节超过上限 {MAX_ARCHIVE_BYTES} 字节。"
                            f"这个上限存在的意义是拦一个写错的循环，"
                            f"正常 generation 只有 30 MB 上下。",
                            "too_large",
                        )
                    )
                    return

                archive_dir = server.warden.archive_dir()
                archive_dir.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(
                    dir=str(archive_dir), prefix=f".gen-{generation:06d}-", suffix=".part"
                )
                tmp_path = Path(tmp_name)
                try:
                    remaining = length
                    with os.fdopen(fd, "wb") as fh:
                        while remaining > 0:
                            chunk = self.rfile.read(min(1 << 20, remaining))
                            if not chunk:
                                break
                            fh.write(chunk)
                            remaining -= len(chunk)
                    if remaining != 0:
                        tmp_path.unlink(missing_ok=True)
                        self._send(
                            _err(
                                400,
                                f"上传中断：还差 {remaining} 字节没收到。"
                                f"这份归档不完整，已丢弃。",
                                "incomplete",
                            )
                        )
                        return
                    resp = server.accept_archive(
                        generation, tmp_path, self.headers.get(SHA_HEADER, "")
                    )
                except Exception:
                    tmp_path.unlink(missing_ok=True)
                    raise
                self._send(resp)

            def _receive_backup(self) -> None:
                """把请求体写进 `backups/` 下的临时文件，再交给 Warden 收下。

                临时文件与最终文件在**同一个目录**里，所以最后一步
                `os.replace` 是原子的（跨文件系统会被退化成复制+删除）。
                """
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    self._send(_err(400, "没有 Content-Length 或长度为 0", "bad_request"))
                    return
                if length > MAX_ARCHIVE_BYTES:
                    self._send(
                        _err(
                            413,
                            f"备份 {length} 字节超过上限 {MAX_ARCHIVE_BYTES} 字节。",
                            "too_large",
                        )
                    )
                    return

                backup_dir = server.warden.backup_dir()
                backup_dir.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(
                    dir=str(backup_dir), prefix=".backup-", suffix=".part"
                )
                tmp_path = Path(tmp_name)
                try:
                    remaining = length
                    with os.fdopen(fd, "wb") as fh:
                        while remaining > 0:
                            chunk = self.rfile.read(min(1 << 20, remaining))
                            if not chunk:
                                break
                            fh.write(chunk)
                            remaining -= len(chunk)
                    if remaining != 0:
                        tmp_path.unlink(missing_ok=True)
                        self._send(
                            _err(
                                400,
                                f"上传中断：还差 {remaining} 字节没收到。这份备份已丢弃。",
                                "incomplete",
                            )
                        )
                        return
                    try:
                        generation = int(self.headers.get("X-Molt-Generation") or 0)
                    except (TypeError, ValueError):
                        generation = 0
                    resp = server.accept_backup(
                        tmp_path,
                        self.headers.get(SHA_HEADER, ""),
                        str(self.headers.get("X-Molt-Shell") or ""),
                        generation,
                    )
                except Exception:
                    tmp_path.unlink(missing_ok=True)
                    raise
                self._send(resp)

            def _handle(self, method: str) -> None:
                parsed = urlparse(self.path)
                if not self._authorized():
                    # 401 不带"token 不对"这种细节——既不帮攻击者，
                    # 也不把内部结构漏给扫端口的人。
                    self._send(_err(401, "未授权", "unauthorized"))
                    return

                # 归档走独立的二进制通道，不进 dispatch 的 JSON 世界。
                m = ARCHIVE_ROUTE.match(parsed.path)
                if m is not None:
                    generation = int(m.group(1))
                    if method == "GET":
                        self._send(server.stream_archive(generation))
                        return
                    if method == "PUT":
                        self._receive_archive(generation)
                        return
                    self._send(
                        _err(405, f"归档端点只接受 GET 和 PUT，收到 {method}", "bad_method")
                    )
                    return

                # 中途备份也是二进制。`/backup`（无名字）只收 POST，
                # `/backup/<name>` 只收 GET——两个方向都不该允许对方的方法，
                # 否则"用一个 GET 触发一次写"这种事会变得可能。
                if parsed.path.rstrip("/") == "/backup":
                    if method == "POST":
                        self._receive_backup()
                        return
                    self._send(
                        _err(405, "上传备份请用 POST", "bad_method")
                    )
                    return
                mb = BACKUP_NAME_ROUTE.match(parsed.path)
                if mb is not None:
                    if method == "GET":
                        self._send(server.stream_backup(mb.group(1)))
                        return
                    self._send(_err(405, "下载备份请用 GET", "bad_method"))
                    return

                body = self._read_body() if method in ("POST", "PUT") else None
                if method in ("POST", "PUT") and body is None:
                    self._send(_err(400, "请求体不是合法 JSON", "bad_request"))
                    return
                self._send(server.dispatch(method, parsed.path, body))

            def do_GET(self) -> None:              # noqa: N802
                self._handle("GET")

            def do_POST(self) -> None:             # noqa: N802
                self._handle("POST")

            def do_PUT(self) -> None:              # noqa: N802
                self._handle("PUT")


        return Handler

    # -- 生命周期 ------------------------------------------------------

    def start(self) -> Tuple[str, int]:
        """在后台线程里起服务，返回实际绑定的 (host, port)。"""
        httpd = ThreadingHTTPServer((self.host, self.port), self._make_handler())
        self._httpd = httpd
        self.port = httpd.server_address[1]
        thread = threading.Thread(
            target=httpd.serve_forever, name="molt-warden", daemon=True
        )
        thread.start()
        self._thread = thread
        return self.host, self.port

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "WardenServer":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()
