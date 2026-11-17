"""把"本地 Warden"和"远端 Warden"抹平成同一个接口。

## 为什么需要这一层

`Warden`（本地类）和 `WardenClient`（HTTP 客户端）做的是同一件事，
但签名不一样：

    Warden.acquire(...)        -> Lease 对象（要 .epoch）
    WardenClient.acquire(...)  -> dict（要 ["epoch"]）

    Warden.accept_snapshot(claimed_epoch=..., claimed_shell_id=..., manifest=...)
                               -> GenerationRecord
    WardenClient.push(shell_id, epoch, manifest)
                               -> dict

CLI 不该关心这些差别。如果每个 `cmd_*` 里都写 `if args.remote: ... else: ...`，
那么这六个命令里就会散着六份同样的分叉，而且新加一个命令时很容易只改一边。
所以差异全部收在这里。

## staging 目录的归属

本地模式下 staging 由 `Warden` 管（`<root>/staging/gen-NNNNNN`），因为它
和 `state.json` 必须待在同一个文件系统上——这点在 `Warden.prepare_generation_dir`
的注释里解释过。

远端模式下**没有"Warden 的文件系统"可用**：壳在 Windows，Warden 在 NAS，
壳写不了 NAS 的路径。这时 staging 落在**壳自己的本地目录**里，由
`--staging` 指定或在系统临时目录下自动挑一个。

这不是妥协，而是正确的边界：**快照在传过去之前，属于做备份的那台机器**。
Warden 只在 `push` 那一刻才需要接触它——而那时 manifest 已经带着
sha256 过去了，Warden 校验的是"你声称的东西"而不是"你的文件"。

## 归档传输为什么最终也由这一层承担

v0.1 时"真正的字节级传输是 shell 自己的事：scp、SMB、U 盘都行"。
那句话在真机上跑过一次之后就站不住了——**它把一次不可中断的动作
（拿到租约 → 备份 → 传输 → 登记）的中间卡了一个人**，而租约 TTL 只有
90 秒，人手工 scp 一趟泡杯茶就过期了。人看到的现象是"我明明备份成功了，
却推不上去"。

所以归档的字节传输也走 Warden 的 HTTP 面：源壳 PUT 上去，目标壳 GET 下来，
两边都只跟已经互相认证过的 Warden 说话。这样既不需要第三套凭证
（再配一个 SMB 账号或 SSH 密钥），也不需要在 NAT/防火墙后面开第二个端口。
"""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .client import WardenClient
from .errors import Fatal
from .soul import sha256_of
from .warden import Warden, default_root


class LocalBackend:
    """壳和 Warden 在同一台机器上。"""

    kind = "local"

    def __init__(self, root: Optional[Path] = None) -> None:
        self.warden = Warden(root=root)

    # -- 只读 -----------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        return self.warden.status()

    def history(self) -> Dict[str, Any]:
        return self.warden.history()

    def set_shell(self, shell_id: str, **attrs: Any) -> Dict[str, Any]:
        return self.warden.set_shell(shell_id, **attrs)

    # -- 自动搬家策略（M2）---------------------------------------------

    def policy(self) -> Dict[str, Any]:
        return {
            "policy": self.warden.policy_view(),
            "attempts": self.warden.attempts_view(limit=10),
        }

    def set_policy(self, policy: Dict[str, Any], *, by: str = "") -> Dict[str, Any]:
        return self.warden.set_policy(policy, by=by)

    def clear_policy(self) -> Dict[str, Any]:
        return {"cleared": self.warden.clear_policy()}

    def record_attempt(self, result: str, **kwargs: Any) -> Dict[str, Any]:
        return {"attempt": self.warden.record_attempt(result=result, **kwargs)}

    def record_arrival(
        self, *, generation: int, shell: str, delivered: bool, detail: str = ""
    ) -> Dict[str, Any]:
        return {
            "arrival": self.warden.record_arrival(
                generation=generation, shell=shell, delivered=delivered, detail=detail
            )
        }

    def arrivals(self) -> Dict[str, Any]:
        return self.warden.arrivals_view()

    def request_power(
        self,
        shell: str,
        action: str,
        *,
        generation: int = 0,
        by: str = "",
        ttl_seconds: Any = None,
    ) -> Dict[str, Any]:
        return self.warden.request_power(
            shell, action, generation=generation, by=by, ttl_seconds=ttl_seconds
        )

    def wake(self, mac: str, *, host: str = "", prefix_len: int = 24) -> Dict[str, Any]:
        """本地后端：就由这个进程发。它跑在宿主上，广播出得去。"""
        from .wake import magic_packet, wake_targets

        sent: List[str] = []
        failures: List[str] = []
        for target in wake_targets(host, prefix_len=prefix_len):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    sock.sendto(magic_packet(mac), (target, 9))
                sent.append(target)
            except OSError as e:
                failures.append(f"{target}: {e}")
        return {"ok": bool(sent), "sent": sent, "failed": failures}
    def shells(self) -> Dict[str, Dict[str, Any]]:
        return self.warden.shells_view()

    def where(self) -> str:
        return f"本地 {self.warden.root}"

    # -- 租约 -----------------------------------------------------------

    def acquire(self, shell_id: str) -> int:
        """返回 epoch（而不是整个 Lease）。

        CLI 里所有地方都只用到 epoch，返回对象反而要每个调用点都写 `.epoch`。
        """
        return self.warden.acquire(shell_id).epoch

    def release(self, shell_id: str, epoch: int) -> None:
        self.warden.release(shell_id, epoch)

    # -- generation -----------------------------------------------------

    def next_generation(self) -> int:
        return self.warden.next_generation_number()

    def prepare_generation_dir(self, generation: int) -> Path:
        return self.warden.prepare_generation_dir(generation)

    def push(
        self, shell_id: str, epoch: int, manifest: Dict[str, Any]
    ) -> Dict[str, Any]:
        return self.warden.accept_snapshot(
            claimed_epoch=epoch,
            claimed_shell_id=shell_id,
            manifest=manifest,
        ).to_dict()

    def mark_consumed(self, generation: int, shell_id: str) -> Dict[str, Any]:
        """登记这一代已经被目标壳取走。

        返回 dict 而不是 `GenerationRecord`：CLI 只把它拿去打印，而远端
        后端返回的本来就是 dict。两边返回同一个形状，调用方才不需要
        分叉（这正是这一层存在的理由）。
        """
        return self.warden.mark_consumed(int(generation), shell_id).to_dict()

    # -- 归档传输 -------------------------------------------------------

    def upload_archive(
        self, generation: int, archive_path: Path, *, expected_sha256: str = ""
    ) -> Dict[str, Any]:
        """本地模式：归档本来就在这台机器上，拷到 Warden 的归档区即可。

        但也走一次拷贝而不是"直接告诉 Warden 那个路径"——因为源归档通常
        在 staging 里，而 staging 是可以被清理的临时区。让归档区持有一份
        独立副本，才能保证"账本里记着的 generation 一定能被取到"。
        """
        src = Path(archive_path)
        if not src.is_file():
            raise Fatal(f"没有这个归档：{src}")
        dest = self.warden.archive_path(generation)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.resolve() == dest.resolve():
            return {
                "generation": generation,
                "bytes": dest.stat().st_size,
                "sha256": sha256_of(dest),
                "already_in_place": True,
            }
        shutil.copy2(src, dest)
        return {
            "generation": generation,
            "bytes": dest.stat().st_size,
            "sha256": sha256_of(dest),
        }

    def download_archive(
        self, generation: int, dest_path: Path, *, expected_sha256: str = ""
    ) -> Dict[str, Any]:
        """本地模式：从归档区拷出来。"""
        src = self.warden.archive_path(generation)
        if not src.is_file():
            raise Fatal(f"归档区里没有 generation {generation} 的归档：{src}")
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        got = sha256_of(dest)
        if expected_sha256 and expected_sha256.lower() != got:
            dest.unlink(missing_ok=True)
            raise Fatal(
                f"归档指纹不符：期望 {expected_sha256[:16]}…，实际 {got[:16]}…。"
            )
        return {"path": str(dest), "bytes": dest.stat().st_size, "sha256": got}

    def archives(self) -> list:
        return self.warden.list_archives()

    # -- 中途备份 --------------------------------------------------------

    def upload_backup(
        self,
        backup_path: Path,
        *,
        shell: str = "",
        generation: int = 0,
        expected_sha256: str = "",
    ) -> Dict[str, Any]:
        """本地模式：直接把文件收进 `backups/`。

        先拷到 `backups/` 里的一个 `.part` 再让 Warden 用 `os.replace` 收下 ——
        和远端那条路径（服务端边收边写 `.part`）保持同一个原子性保证。
        """
        src = Path(backup_path)
        if not src.is_file():
            raise Fatal(f"没有这份备份：{src}")
        d = self.warden.backup_dir()
        d.mkdir(parents=True, exist_ok=True)
        part = d / f".incoming-{int(time.time() * 1000)}.part"
        shutil.copy2(src, part)
        try:
            meta = self.warden.store_backup(
                part, shell=shell, generation=generation, sha256=expected_sha256
            )
        finally:
            part.unlink(missing_ok=True)
        return meta

    def download_backup(self, name: str, dest_path: Path) -> Dict[str, Any]:
        src = self.warden.backup_path(name)
        if not src.is_file():
            raise Fatal(f"没有这份备份：{name}")
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        return {"path": str(dest), "bytes": dest.stat().st_size, "sha256": sha256_of(dest)}

    def backups(self) -> list:
        return self.warden.list_backups()

    def prune_backups(self, *, keep_recent: int = 12, dry_run: bool = True) -> Dict[str, Any]:
        return self.warden.prune_backups(keep_recent=keep_recent, dry_run=dry_run)

    def prune(self, *, keep_recent: int = 2, dry_run: bool = True) -> Dict[str, Any]:
        return self.warden.prune_archives(keep_recent=keep_recent, dry_run=dry_run)

    # -- 插件配置包 ------------------------------------------------------

    def publish_plugin_bundle(
        self,
        payload: bytes,
        *,
        shell: str,
        sha256: str = "",
        plugins: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """本地模式：写进 `plugin-configs/<壳名>.tar.gz`（先 `.part` 再改名）。"""
        d = self.warden.plugin_bundle_dir()
        d.mkdir(parents=True, exist_ok=True)
        part = d / f".incoming-{int(time.time() * 1000)}.part"
        part.write_bytes(payload)
        try:
            return self.warden.store_plugin_bundle(
                part, shell=shell, sha256=sha256, plugins=plugins
            )
        finally:
            part.unlink(missing_ok=True)

    def plugin_bundles(self) -> list:
        return self.warden.list_plugin_bundles()

    def plugin_bundle_bytes(self, shell: str) -> bytes:
        path = self.warden.plugin_bundle_path(shell)
        if not path.is_file():
            raise Fatal(f"没有 {shell} 的插件配置包")
        return path.read_bytes()


class RemoteBackend:
    """壳在别处，Warden 在 NAS 上，走 HTTP。"""

    kind = "remote"

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        staging: Optional[Path] = None,
    ) -> None:
        self.client = WardenClient(base_url, token)
        if staging is not None:
            self.staging = Path(staging).expanduser().resolve()
        else:
            # 落在哪里由三条互相冲突的要求决定：
            #
            # ① 不能是随机 tmp 目录（`tmpXXXX`），过几天人想看"上次那份快照
            #    在哪"时根本找不到。
            # ② 不能是 `/tmp` 下的固定名字。第一次真机跑 `molt migrate` 就
            #    落在 /tmp/molt-staging，然后意识到一件事：**NAS 重启、
            #    systemd-tmpfiles 清理、或者人跑一次 `rm -rf /tmp/*`，
            #    那份已经登记进账本、理论上"归新壳管"的快照就没了。**
            #    而账本里还记着它——变成一条指向空气的记录。
            # ③ 必须在**壳自己**的盘上。远端模式下壳写不了 NAS 路径
            #    （要 SMB/NFS，那正是 ARCH §3 判定不可靠的东西）。
            #
            # 所以用用户级应用数据目录，跨重启稳定：
            #   Windows  %LOCALAPPDATA%\molt\staging
            #   POSIX    ~/.local/share/molt/staging（或 XDG_DATA_HOME）
            local_appdata = os.environ.get("LOCALAPPDATA")
            if local_appdata:
                base = Path(local_appdata)
            elif os.environ.get("XDG_DATA_HOME"):
                base = Path(os.environ["XDG_DATA_HOME"])
            else:
                base = Path.home() / ".local" / "share"
            self.staging = base / "molt" / "staging"

    # -- 只读 -----------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        return self.client.status()

    def history(self) -> Dict[str, Any]:
        return self.client.history()

    # -- 自动搬家策略（M2）---------------------------------------------

    def policy(self) -> Dict[str, Any]:
        return self.client.policy()

    def set_policy(self, policy: Dict[str, Any], *, by: str = "") -> Dict[str, Any]:
        return self.client.set_policy(policy, by=by)

    def clear_policy(self) -> Dict[str, Any]:
        return self.client.clear_policy()

    def record_attempt(self, result: str, **kwargs: Any) -> Dict[str, Any]:
        return self.client.record_attempt(result, **kwargs)

    def record_arrival(
        self, *, generation: int, shell: str, delivered: bool, detail: str = ""
    ) -> Dict[str, Any]:
        return self.client.record_arrival(
            generation=generation, shell=shell, delivered=delivered, detail=detail
        )

    def arrivals(self) -> Dict[str, Any]:
        return (self.client.status() or {}).get("arrivals", {}) or {}

    def wake(self, mac: str, *, host: str = "", prefix_len: int = 24) -> Dict[str, Any]:
        """远端后端：让账本那台机器发。容器里的广播出不了 docker0。"""
        return self.client.wake(mac, host=host, prefix_len=prefix_len)
    def set_shell(self, shell_id: str, **attrs: Any) -> Dict[str, Any]:
        return self.client.set_shell(shell_id, **attrs)

    def shells(self) -> Dict[str, Dict[str, Any]]:
        return self.client.shells()

    def where(self) -> str:
        return f"远端 {self.client.base_url}"

    # -- 租约 -----------------------------------------------------------

    def acquire(self, shell_id: str) -> int:
        return int(self.client.acquire(shell_id)["epoch"])

    def release(self, shell_id: str, epoch: int) -> None:
        self.client.release(shell_id, epoch)

    # -- generation -----------------------------------------------------

    def next_generation(self) -> int:
        return self.client.next_generation()

    def prepare_generation_dir(self, generation: int) -> Path:
        path = self.staging / f"gen-{generation:06d}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def push(
        self, shell_id: str, epoch: int, manifest: Dict[str, Any]
    ) -> Dict[str, Any]:
        return self.client.push(shell_id, epoch, manifest)

    def mark_consumed(self, generation: int, shell_id: str) -> Dict[str, Any]:
        """远端模式：直接转发给 Warden 的 HTTP 端点。"""
        return self.client.mark_consumed(int(generation), shell_id)

    # -- 归档传输 -------------------------------------------------------

    def upload_archive(
        self, generation: int, archive_path: Path, *, expected_sha256: str = ""
    ) -> Dict[str, Any]:
        return self.client.upload_archive(
            generation, Path(archive_path), expected_sha256=expected_sha256
        )

    def download_archive(
        self, generation: int, dest_path: Path, *, expected_sha256: str = ""
    ) -> Dict[str, Any]:
        return self.client.download_archive(
            generation, Path(dest_path), expected_sha256=expected_sha256
        )

    def archives(self) -> list:
        return self.client.archives()

    def prune(self, *, keep_recent: int = 2, dry_run: bool = True) -> Dict[str, Any]:
        return self.client.prune(keep_recent=keep_recent, dry_run=dry_run)

    # -- 中途备份 --------------------------------------------------------

    def upload_backup(
        self,
        backup_path: Path,
        *,
        shell: str = "",
        generation: int = 0,
        expected_sha256: str = "",
    ) -> Dict[str, Any]:
        return self.client.upload_backup(
            Path(backup_path),
            shell=shell,
            generation=generation,
            expected_sha256=expected_sha256,
        )

    def download_backup(self, name: str, dest_path: Path) -> Dict[str, Any]:
        return self.client.download_backup(name, Path(dest_path))

    def backups(self) -> list:
        return self.client.backups()

    def prune_backups(self, *, keep_recent: int = 12, dry_run: bool = True) -> Dict[str, Any]:
        return self.client.prune_backups(keep_recent=keep_recent, dry_run=dry_run)

    # -- 插件配置包 ------------------------------------------------------

    def publish_plugin_bundle(
        self,
        payload: bytes,
        *,
        shell: str,
        sha256: str = "",
        plugins: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return self.client.publish_plugin_bundle(
            payload, shell=shell, sha256=sha256, plugins=plugins
        )

    def plugin_bundles(self) -> list:
        return self.client.plugin_bundles()

    def plugin_bundle_bytes(self, shell: str) -> bytes:
        return self.client.download_plugin_bundle(shell)



def open_backend(
    *,
    root: Optional[str] = None,
    url: Optional[str] = None,
    token: Optional[str] = None,
    staging: Optional[str] = None,
) -> Any:
    """按参数决定用哪个后端。

    优先级：给了 `url` 就是远端，否则本地。**两者都给是错误的配置**，
    因为人需要明确知道"我在跟谁说话"——含糊地猜是分布式系统出事的
    典型起点。
    """
    if url and root:
        raise ValueError(
            "同时给了 --warden-url 和 --root，无法判断该跟谁说话。"
            "远端模式用 --warden-url，本地模式用 --root。"
        )
    if url:
        if not token:
            raise ValueError(
                "使用 --warden-url 时必须同时给 --warden-token"
                "（Warden 启动时会打印它）。"
            )
        return RemoteBackend(url, token, staging=Path(staging) if staging else None)
    return LocalBackend(root=Path(root) if root else None)


# 供 CLI 复用的默认值
__all__ = [
    "LocalBackend",
    "RemoteBackend",
    "open_backend",
    "default_root",
]
