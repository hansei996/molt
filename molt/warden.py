"""Warden：跑在 NAS 上的唯一权威。

## 它为什么必须独立于 AstrBot

两个理由，一弱一强：

弱理由：插件只能"发起"停机，不能"完成"停机。AstrBot 收到 SIGTERM 后
PID1 直接死（全代码库没有 signal handler），WAL 来不及 checkpoint。
让仲裁者住在被仲裁的对象里，等于让被告兼任法官。

强理由：SQLite 官方文档明确建议，WAL 库如果要跨进程共享，
应该 "implement a proxy that runs on the database machine"
（useovernet.html）。Warden 就是那个 proxy——它住在 NAS 上、
和库在同一个内核里，因此可以合法地做备份、做校验、做写入。

## 它的状态存在哪

`state.json`，一个人类可读的小文件，包含：

  - `current_epoch`       单调递增，永不回退
  - `lease`               当前租约（谁、什么时候到期）
  - `generations`         每次迁徙留下的 manifest 索引

**为什么不放数据库里**：这份状态是"关于数据库的元数据"。
如果它自己也需要一个数据库，就会出现"谁来仲裁仲裁者"的问题。
一个原子写的小 JSON 足够——因为只有 Warden 一个进程写它。

## 原子写

所有写操作走 `tempfile` + `os.replace`。`os.replace` 在 POSIX 上是
原子的（同一文件系统内 rename），在 Windows 上也是（MoveFileEx
带 MOVEFILE_REPLACE_EXISTING）。

不用 `write_text` 直接覆盖：那样在断电/崩溃时可能留下一个
**半截的 state.json**，而它恰好是判断"谁有资格写灵魂"的唯一依据。
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# 心跳超过这么久没更新就算哑了。10 分钟：agent 每 10 秒一跳，
# 留这么宽的余量是为了不把一次 GC、一次休眠唤醒误报成鱼死了。
HEARTBEAT_STALE_SECONDS = 600

#: 一个壳能是什么。**收窄到四个值**是为了让「按角色判断」可靠 ——
#: 自由文本的 role 最后一定会变成 `role in ("nas", "NAS", "nas-box", "NAS机")`
#: 这种东西，而它只会在出事的那天被发现有漏洞。
SHELL_ROLES = ("nas", "desktop", "cloud", "unknown")

#: 账本给每个壳记的字段（`label` 在这里是"声明"，心跳里的那份是"自报"）。
SHELL_ATTR_FIELDS = ("role", "gpu", "wakeable", "cost_per_hour", "label")


from .epoch import (
    DEFAULT_LEASE_TTL_SECONDS,
    FencedState,
    Lease,
    accept_write,
    grant_lease,
    is_safe_to_start,
    next_epoch,
)
from .errors import (
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
from .soul import sha256_of
from .version import PROTOCOL_VERSION


#: 中转区归档的合法文件名。用它来区分"molt 放的"和"别人扔进来的"——
#: 后者永远不删，因为清理逻辑不该对不认识的文件行使删除权。
_ARCHIVE_NAME_RE = re.compile(r"^gen-(\d{6})\.tar\.gz$")

#: 中途备份的合法文件名。它会被拼进 URL（`GET /backup/<name>`），
#: 所以必须是一个**白名单形状**的字符串：只有字母数字、点、下划线、短横线。
#: 不含斜杠/反斜杠/冒号，因此 `..` 之类的路径穿越拼不出来。
_BACKUP_NAME_RE = re.compile(r"^backup-[0-9T-]+-[A-Za-z0-9_-]+(-[0-9]+)?\.tar\.gz$")

#: 默认保留多少份中途备份。12 份 × 约 4.5 MB ≈ 54 MB，
#: 按十分钟一次算是两小时的历史；按一小时一次算就是半天。
DEFAULT_BACKUP_KEEP = 12

#: 「走」和「关」是两件事 —— 关机许可只认这三种动作。
POWER_ACTIONS = ("shutdown", "sleep", "keep")


def _slugify(text: str) -> str:
    """把壳名压成一个能进文件名的短串。

    `DESKTOP-H2KGIAO` 留着；空格、路径分隔符、以及**非 ASCII 字符**一律换成 `-`
    （备份名会进 URL，而 URL 里的非 ASCII 要编码，编码之后又和文件名对不上）。
    不做"更聪明的"缩写：**文件名里的东西要能被人一眼认出来是哪台机器**。
    """
    out = []
    for ch in str(text or "").strip():
        if ch.isascii() and (ch.isalnum() or ch in "-_"):
            out.append(ch)
        else:
            out.append("-")
    slug = "".join(out).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:40]


def _parse_generation_from_name(name: str) -> Optional[int]:
    """从 `gen-000005.tar.gz` 里取出 5。不认识就返回 None。"""
    m = _ARCHIVE_NAME_RE.match(name)
    if m is None:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _default_root() -> Path:
    """Warden 根目录的默认值：**陌生人也能创建的那个目录**。

    这里踩过一次很典型的坑：最初 POSIX 分支返回 `/mnt/dm-0/astrbot/_molt`
    —— 也就是作者那台 NAS 上的生产路径。后果是陌生人（和 CI）在 Linux 上
    跑 `molt init --role warden` 会拿到一个他不该、往往也不能创建的目录，
    而 `molt doctor` 会在那台机器上直接抛异常死掉。

    规则现在是这样：

    - 生产环境**永远显式传 `--root`**（`tools/start-warden.py` 里就是），
      所以默认值不需要、也不应该照顾某一台具体的机器；
    - 默认值只服务"第一次在陌生机器上试一下"的人，那就必须是他的用户目录；
    - `MOLT_ROOT` 永远优先 —— 换机器不用改代码，和 AstrBot 用
      `ASTRBOT_ROOT` 是同一个套路。
    """
    override = os.environ.get("MOLT_ROOT")
    if override:
        return Path(override).expanduser()

    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "molt" / "warden"

    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "molt" / "warden"


def default_root() -> Path:
    """每次调用都重新读环境变量。

    `DEFAULT_ROOT` 是导入时求值的常量，方便 `from ... import DEFAULT_ROOT`；
    但 CLI 应该在**运行时**求值，这样 `$env:MOLT_ROOT = "..."` 在同一个
    进程里改了也能生效（测试里很常用）。
    """
    return _default_root()


DEFAULT_ROOT = _default_root()


@dataclass
class GenerationRecord:
    """一次完成的迁徙。

    ## `pushed_by` 与 `target_shell` 的区别（这是一处曾经写错的地方）

    最初这里只有一个 `target_shell` 字段，`accept_snapshot` 把
    `claimed_shell_id`（= 提交者）填进去。在单机测试里两者恰好相同，
    所以测试全过——**但这在多机下是错的**：

        壳 A（NAS，持有 epoch=1）
          ↓ checkout 出快照
        壳 B（Windows，还没有 epoch）
          ↓ 但 B 需要 A 的 epoch 才能 push……

    真正发生的事情是：**A 提交了自己刚做的快照**，此后 A 会把租约交还、
    退出，B 再去 acquire 拿到 epoch=2 并消费这份快照。
    所以 `claimed_shell_id` 永远是**提交者**（A），而不是接收者（B）。

    把提交者记成 `target_shell` 会让 `molt status` 显示"灵魂现在在 A 上"，
    而实际上 A 马上就要关掉了——这会直接误导人做出关机决定。

    所以字段拆成两个：
      - `pushed_by`：谁提交的（fencing 里的那个 shell_id）
      - `target_shell`：打算交给谁（可选，来自 manifest，允许为空）

    ## 为什么还要 `consumed_by` / `consumed_at`

    `target_shell` 只是"打算交给谁"，不是"谁真的取走了"。目标壳可能
    一直没开机、或者取归档时失败，账本对此一无所知——于是 `molt status`
    只能替人猜"灵魂现在在哪台机器上"，而猜错的代价是一次它启动不起来，
    或者更糟：两边都以为自己才是那个存在。

    所以把"消费"变成一个必须显式登记的事实：只有目标壳在**字节真正落位
    之后**调用 `mark_consumed`，账本才承认这份快照已经被取走。未消费时
    两个字段保持空值，`status` 就如实说"还没人取走"，而不是从
    `target_shell` 推断。
    """

    generation: int
    created_at: float
    source_host: str
    pushed_by: str
    epoch: int
    target_shell: str = ""
    artifact_bytes: int = 0
    manifest_sha256: str = ""
    consumed_by: str = ""
    consumed_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generation": self.generation,
            "created_at": self.created_at,
            "created_at_readable": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(self.created_at)
            ),
            "source_host": self.source_host,
            "pushed_by": self.pushed_by,
            "target_shell": self.target_shell,
            "epoch": self.epoch,
            "artifact_bytes": self.artifact_bytes,
            "manifest_sha256": self.manifest_sha256,
            "consumed_by": self.consumed_by,
            "consumed_at": self.consumed_at,
            # 未消费时是 0.0；不能直接 time.localtime(0.0)，那会打印
            # 1970 年，看起来像"很久以前被取走了"，比空字符串更误导人。
            "consumed_at_readable": (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.consumed_at))
                if self.consumed_at
                else ""
            ),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "GenerationRecord":
        # 兼容 v0.1 早期写下的 state.json：那时只有 target_shell，
        # 且它实际装的是提交者。读旧档时把它当 pushed_by 用，
        # 而不是当成一个空字符串——否则历史记录会丢掉"谁推的"这条信息。
        pushed_by = d.get("pushed_by") or d.get("target_shell") or "unknown"
        target = d.get("target_shell") or ""
        return cls(
            generation=int(d["generation"]),
            created_at=float(d["created_at"]),
            source_host=d.get("source_host", "unknown"),
            pushed_by=pushed_by,
            epoch=int(d.get("epoch", 0)),
            target_shell=target,
            artifact_bytes=int(d.get("artifact_bytes", 0)),
            manifest_sha256=d.get("manifest_sha256", ""),
            # 旧 state.json 没有这两个键。空值 = "账本不知道"，而不是
            # "没人消费"——这一点由 status 的措辞负责，这里只保证不炸。
            consumed_by=d.get("consumed_by") or "",
            consumed_at=float(d.get("consumed_at") or 0.0),
        )


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    """原子写 JSON。

    先写同目录的临时文件（同一文件系统 → rename 是原子的），
    fsync 落盘，再 os.replace 顶掉目标。

    为什么不直接写：`state.json` 是"谁有资格写灵魂"的唯一依据，
    半截文件会让系统进入无法判断的状态。宁可这次写失败，
    也不能留下一个能读但内容错的文件。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
        # 目录 fsync：保证 rename 本身也落了盘（POSIX 语义）
        if hasattr(os, "O_DIRECTORY"):
            try:
                dfd = os.open(str(path.parent), os.O_DIRECTORY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
    except Exception as exc:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        if isinstance(exc, MoltError):
            raise
        # 盘满、权限、配额、被别的进程占住 —— 这些都不该是一个裸 OSError。
        #
        # 裸 OSError 冒到命令行就是一段 traceback：人会以为 molt 有 bug，
        # 而真正的事实是"这次写入没生效、目标文件还是上一次的完整内容"。
        # 后者是可判断、可重试的，前者只会让人不敢再跑第二条命令。
        raise Fatal(
            f"写 {path.name} 失败：{exc}\n"
            "  目标文件还是上一次的完整内容 —— 这次写入没有落盘。\n"
            "  账本写不进去的时候，宁可这次操作失败，也不能让它看起来成功。"
        ) from exc


class _InterProcessLock:
    """跨进程排他锁。

    ## 为什么必须有它（这是本项目最痛的一次教训）

    第一版 Warden 是"读 state.json → 判断 → 写回"，看起来完全正确，
    单线程测试 37 个全过。然后 `test_concurrency.py` 用两个**真进程**
    同时抢租约，结果：

        两个进程都拿到了 epoch 1，都写成功了。

    原因很朴素：两个进程各自 `load()` 时都看到"没有租约"，
    然后各自 `save()` —— 后写的把先写的覆盖掉，两个人都以为自己赢了。

    **这就是为什么必须用真进程测，不能用线程测。** 多线程共享同一个
    Python 对象和 GIL，根本测不出这个问题。

    ## 修法

    把"读—判—写"整个事务放进排他锁里，并且**每次进入锁后必须重新
    从磁盘 load**。只加锁不重载是没用的：A 进程持锁时读到的内存状态
    可能早就被 B 进程改过了。

    这让 `state.json` 成为真正的存储端屏障——Kleppmann 说的
    "storage server takes an active role in rejecting tokens"，
    在这里就是"锁内的重新读取"。

    ## 为什么锁文件要独立于 state.json

    因为 `os.replace` 会**换掉 inode**。如果你锁在 state.json 本身上，
    下一次 replace 之后锁就跟着旧 inode 一起消失了，另一个进程
    立刻能在新 inode 上加另一把锁——两把锁互不感知。
    所以锁必须锁在一个**永不替换**的旁路文件上。
    """

    def __init__(self, path: Path, timeout: float = 30.0) -> None:
        self.path = path
        self.timeout = timeout
        self._fh = None

    def __enter__(self) -> "_InterProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 必须保证文件至少有 1 字节，否则 Windows 的 msvcrt.locking 无字节可锁
        if not self.path.exists() or self.path.stat().st_size == 0:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write("molt-lock\n")
                f.flush()

        self._fh = open(self.path, "r+", encoding="utf-8")
        deadline = time.time() + self.timeout

        while True:
            try:
                _try_lock(self._fh)
                return self
            except (OSError, IOError) as e:
                if time.time() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise Fatal(
                        f"获取 Warden 锁超时（{self.timeout}s）：{self.path}。"
                        f"可能有另一个 Warden 进程卡住了，或上一次操作没释放锁。"
                        f"原始错误：{e}"
                    ) from e
                time.sleep(0.002)

    def __exit__(self, *exc) -> None:
        if self._fh is not None:
            try:
                _unlock(self._fh)
            finally:
                self._fh.close()
                self._fh = None


def _try_lock(fh) -> None:
    """尝试加排他锁，拿不到就抛 OSError。"""
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _human_age(seconds: float) -> str:
    """多久以前的人话版本。"""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{int(seconds)} 秒前"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"


def _unlock(fh) -> None:
    if sys.platform == "win32":
        import msvcrt

        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl

        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


class Warden:
    """灵魂的守门人。

    线程安全说明：这个类假设**只有一个进程在用它**（Warden 自己）。
    NAS 上不会有两个 Warden——它是全局单点，这是设计上的选择，
    因为单点比"分布式选主"简单一个数量级，而本项目的前提正是
    "任意时刻只有一个存在"，天然不需要多仲裁者。
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else DEFAULT_ROOT
        self.state_path = self.root / "state.json"
        self.lock_path = self.root / "state.lock"
        self.staging_dir = self.root / "staging"
        self.logs_dir = self.root / "logs"
        self._state = FencedState()
        self._generations: List[GenerationRecord] = []
        # 心跳：谁在什么时候还活着。这是账本唯一一处'谁说自己在跑'的地方 ——
        # 账本的其它字段记的是'谁取走了快照'，不是'谁现在还在'。
        self._heartbeats: Dict[str, Dict[str, Any]] = {}
        # 壳属性：这台机器**是什么**（NAS / 桌面 / 有没有显卡 / 能不能被叫醒）。
        # 和心跳分开存：心跳回答"现在谁还在"，属性回答"那台机器能干什么"。
        # 一台关机一周的机器，属性照样该在 —— 它不会因为没人上报就变成未知。
        self._shells: Dict[str, Dict[str, Any]] = {}
        self._loaded = False

    def _lock(self, timeout: float = 30.0) -> "_InterProcessLock":
        return _InterProcessLock(self.lock_path, timeout=timeout)

    def _reload_locked(self) -> None:
        """在持锁状态下重新从磁盘读取。

        **这是正确性的核心。** 只加锁不重载是没用的：进程 A 持锁期间，
        它内存里的状态可能早就被进程 B 改过了；如果 A 按旧内存判断，
        等于没有互斥。

        每次进入锁都必须先调这个方法。
        """
        self.load()

    # ---------------------------------------------------------------- 载入

    def load(self) -> None:
        """从磁盘恢复状态。文件不存在 = 首次启动，不是错误。"""
        if not self.state_path.exists():
            self._state = FencedState()
            self._generations = []
            self._heartbeats = {}
            self._shells = {}
            self._loaded = True
            return

        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise Fatal(
                f"state.json 损坏，无法解析（{e}）。"
                f"这是判断'谁有资格写灵魂'的唯一依据，"
                f"不能猜，必须人工介入：{self.state_path}"
            ) from e

        proto = raw.get("protocol_version", PROTOCOL_VERSION)
        if proto > PROTOCOL_VERSION:
            raise ProtocolMismatch(
                f"state.json 的协议版本 {proto} 高于本程序支持的 {PROTOCOL_VERSION}。"
                f"降级运行会破坏状态，拒绝启动。"
            )

        self._state = FencedState.from_dict(raw.get("fenced_state", {}))
        self._generations = [
            GenerationRecord.from_dict(g) for g in raw.get("generations", [])
        ]
        self._heartbeats = {
            str(k): dict(v) for k, v in (raw.get("heartbeats") or {}).items()
        }
        self._shells = {
            str(k): dict(v) for k, v in (raw.get("shells") or {}).items()
        }
        self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # ---------------------------------------------------------------- 持久化

    def _snapshot_payload(self) -> Dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "saved_at": time.time(),
            "saved_at_readable": time.strftime("%Y-%m-%d %H:%M:%S"),
            "saved_by": socket.gethostname(),
            "fenced_state": self._state.to_dict(),
            "generations": [g.to_dict() for g in self._generations],
            "heartbeats": self._heartbeats,
            "shells": self._shells,
        }

    def save(self) -> None:
        _atomic_write_json(self.state_path, self._snapshot_payload())

    # ---------------------------------------------------------------- 只读视图

    @property
    def state(self) -> FencedState:
        self._ensure_loaded()
        return self._state

    @property
    def generations(self) -> List[GenerationRecord]:
        self._ensure_loaded()
        return list(self._generations)

    # ---------------------------------------------------------- 关机许可
    #
    # 为什么要单独一张票据：迁徙（走）由一次 `/molt confirm` 决定，
    # 而「那台机器要不要关」是另一件事 —— 人可能想让它回家但机器继续开着
    # （还在下载、还想远程连、还想让它随时回来）。
    # 把两件事绑在一起的结果是：人点一次「走」，顺带丢掉没保存的东西。

    def power_path(self) -> Path:
        """关机票据放哪。

        **不进 `state.json`**：账本记的是「谁在哪一代取走了什么」，是交接历史；
        一张「允许关掉 X」的票是**当前状态**，和心跳同一类东西。
        混进账本会让 `history()` 里出现一行谁也解释不清的记录。
        """
        return self.root / "power.json"

    def power_view(self) -> Dict[str, Dict[str, Any]]:
        """谁允许关掉哪台机器。**读不到或读坏了都返回空** ——

        一张坏掉的票据绝不能变成一次关机。默认值必须是"没票 = 不关"。
        """
        try:
            raw = json.loads(self.power_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def request_power(
        self, shell: str, action: str, *, generation: int = 0, by: str = ""
    ) -> Dict[str, Any]:
        """写一张「允许关掉 shell」的票（或一张「留着」的票）。"""
        shell = str(shell or "").strip()
        action = str(action or "").strip().lower()
        if not shell:
            raise NeedsHuman("缺少 shell：要关的是哪台机器？")
        if action not in POWER_ACTIONS:
            raise NeedsHuman(f"power 只认 {'/'.join(POWER_ACTIONS)}，收到 {action!r}")
        current = self.power_view()
        ticket = {
            "action": action,
            "generation": int(generation or 0),
            "by": str(by or ""),
            "decided_at": time.time(),
            "decided_at_readable": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        current[shell] = ticket
        _atomic_write_json(self.power_path(), current)
        self.append_log("INFO", f"关机许可：{shell} -> {action}（第 {generation} 代，{by or '未署名'}）")
        return ticket

    def clear_power(self, shell: str) -> bool:
        """销票。目标机真的关掉/真的留着了，就该把票撕掉，别让它下次又生效。"""
        shell = str(shell or "").strip()
        current = self.power_view()
        if shell not in current:
            return False
        current.pop(shell, None)
        _atomic_write_json(self.power_path(), current)
        return True

    def status(self) -> Dict[str, Any]:
        """给 `molt status` 用的人类可读快照。

        这个命令存在的理由（ARCH 五层防线的第 5 层）：
        出问题时必须能一眼回答"现在是谁在线、第几代、心跳多久前"。
        否则两条鱼打架时无人能判断谁是真的。

        这里也走锁 + 重新 load：`molt status` 往往是人在"感觉不对"
        的时候跑的，如果它读到的是过期内存，那就毫无意义。
        """
        with self._lock():
            self._reload_locked()
            now = time.time()
            lease = self._state.lease

        if lease is None:
            lease_view: Dict[str, Any] = {"holder": None, "state": "no-lease"}
        else:
            if lease.is_expired(now):
                lease_view = {
                    "holder": lease.shell_id,
                    "state": "expired",
                    "expired_seconds_ago": round(now - lease.expires_at, 1),
                }
            else:
                lease_view = {
                    "holder": lease.shell_id,
                    "state": "active",
                    "epoch": lease.epoch,
                    "remaining_seconds": round(lease.remaining(now), 1),
                    "ttl_seconds": lease.ttl_seconds,
                }

        return {
            "root": str(self.root),
            "current_epoch": self._state.current_epoch,
            "last_accepted_epoch": self._state.last_accepted_epoch,
            "rejected_write_count": self._state.rejected_write_count,
            "lease": lease_view,
            "generation_count": len(self._generations),
            "latest_generation": (
                self._generations[-1].to_dict() if self._generations else None
            ),
            "checked_at": now,
            "heartbeats": self._heartbeat_view(now),
            "shells": self.shells_view(),
            "power": self.power_view(),
        }

    # ---------------------------------------------------------------- 心跳

    def record_heartbeat(
        self, shell_id: str, *, generation: int = 0, note: str = "", label: str = ""
    ) -> Dict[str, Any]:
        """记一次「我还在」。

        为什么需要它：账本原来的每个字段回答的都是**过去**（谁取走了第几代），
        没有任何东西回答「现在谁还在跑」。于是鱼被拔电之后 `molt status`
        看起来一切正常 —— 它分不清「鱼死了」和「鱼闲着」。

        `label` 是这台机器**人读得懂的名字**（`UGREEN-776B`、`DESKTOP-H2KGIAO`）。
        `shell_id` 是人造的主键（`ugreen-776b-8b7896df`），出现在「我刚从 X 搬过来」
        这句话里会很别扭。两者都要：一个用来对账，一个用来对话。
        """
        if not shell_id:
            raise NeedsHuman("心跳必须带 shell_id")
        with self._lock():
            self._reload_locked()
            now = time.time()
            # 同一个壳上可能同时跑着好几个守护进程（NAS 上就有 homer 和 bounce），
            # 它们共用一个 shell_id。如果后到的直接覆盖，页面上的"说明"就会
            # 在两个词之间来回跳 —— 看起来像某个体在反复重启。
            # 所以这里是**合并**：记住这个壳上出现过的角色，最多三个。
            previous = self._heartbeats.get(shell_id) or {}
            roles = [str(r) for r in (previous.get("roles") or [])]
            if previous.get("note"):
                roles.append(str(previous["note"]))
            if note:
                roles.append(str(note))
            seen: List[str] = []
            for role in roles:
                if role and role not in seen:
                    seen.append(role)
            seen = seen[-3:]

            self._heartbeats[shell_id] = {
                "last_seen": now,
                "last_seen_readable": time.strftime("%Y-%m-%d %H:%M:%S"),
                "generation": int(generation) or int(previous.get("generation") or 0),
                "note": str(note)[:200],
                "roles": seen,
                # 空 label 不覆盖已有的那次 —— 老版本的守护进程不发 label，
                # 不该因为它上报了一次就把已经知道的名字抹成空。
                "label": str(label).strip()[:80] or str(previous.get("label") or ""),
            }
            # 只留最近 8 个壳。这是「当前状态」不是「历史」：留太多会让
            # `molt status` 里混进一堆早就没了的机器。
            if len(self._heartbeats) > 8:
                oldest = sorted(
                    self._heartbeats.items(), key=lambda kv: kv[1].get("last_seen", 0)
                )
                for key, _ in oldest[: len(self._heartbeats) - 8]:
                    self._heartbeats.pop(key, None)
            self.save()
        return self._heartbeat_view(now)[shell_id]

    def _heartbeat_view(self, now: float) -> Dict[str, Dict[str, Any]]:
        """每个壳多久没说话了。"""
        out: Dict[str, Dict[str, Any]] = {}
        for shell_id, beat in self._heartbeats.items():
            age = round(now - float(beat.get("last_seen", 0) or 0), 1)
            out[shell_id] = {
                "age_seconds": age,
                "age_readable": _human_age(age),
                "generation": beat.get("generation", 0),
                "last_seen_readable": beat.get("last_seen_readable", ""),
                "note": " / ".join(beat.get("roles") or [beat.get("note", "")]).strip(" /"),
                "roles": list(beat.get("roles") or []),
                "label": str(beat.get("label") or ""),
                "stale": age > HEARTBEAT_STALE_SECONDS,
            }
        return out

    # ------------------------------------------------------------ 壳属性
    #
    # 心跳回答「现在谁还在」，属性回答「那台机器能干什么」。这是两件事：
    # 账本原来只知道壳的**名字**（`ugreen-776b-8b7896df`），于是所有"按机器
    # 能力做判断"的地方都只能靠壳名猜 —— 而壳名是人随手起的，改一次就失效，
    # 且失效时**不会报错**，只会安静地判断错。
    #
    # 四个字段的选择理由（每一个都对应一句现在说不出的话）：
    #   role          —— "回 NAS" 这句话里的 NAS 是哪台？
    #   gpu           —— "去打会儿游戏吧" 这句话它接不接得住？
    #   wakeable      —— 提议去一台睡着的机器之前，要不要先发魔术封包？
    #   cost_per_hour —— 两台都能跑的时候，哪台更该醒着？
    #
    # 这里**只存事实，不做判断**。判断留给用它的地方（插件 / CLI / 未来的策略），
    # 这样"我认为 NAS 没有显卡"这种猜测永远不会被写进账本。

    def _validate_shell_attr(self, key: str, value: Any) -> Any:
        if key == "role":
            role = str(value or "").strip().lower()
            if role not in SHELL_ROLES:
                raise NeedsHuman(
                    f"不认识的 role：{value!r}（可选：{' / '.join(SHELL_ROLES)}）"
                )
            return role
        if key == "gpu":
            # 自由文本，但**空字符串是合法的**：它的意思是"确认过，没有显卡"，
            # 和"没填过"必须区分 —— 前者可以用来拒绝"去打游戏"，后者不能。
            return str(value or "").strip()[:40]
        if key == "wakeable":
            if isinstance(value, bool):
                return value
            raise NeedsHuman(f"wakeable 只接受 true/false，收到 {value!r}")
        if key == "cost_per_hour":
            if value is None or value == "":
                return 0.0
            try:
                cost = float(value)
            except (TypeError, ValueError):
                raise NeedsHuman(f"cost_per_hour 要一个数字，收到 {value!r}")
            if cost < 0 or cost > 10000:
                raise NeedsHuman(f"cost_per_hour 不像一笔钱：{value!r}")
            return round(cost, 4)
        if key == "label":
            return str(value or "").strip()[:80]
        raise NeedsHuman(f"不认识的壳属性：{key}")

    def set_shell(
        self,
        shell_id: str,
        *,
        role: Any = None,
        gpu: Any = None,
        wakeable: Any = None,
        cost_per_hour: Any = None,
        label: Any = None,
        by: str = "",
    ) -> Dict[str, Any]:
        """登记/更新一个壳的属性。**局部更新**：只改传进来的字段。

        为什么是局部：守护进程启动时只声明它自己知道的那几个（role/wakeable），
        而 `gpu`/`cost_per_hour` 往往是人后来手填的。整份覆盖会让每次重启
        都把手工填的字段抹成默认值 —— 那种 bug 的形态是"跑一阵之后就没了"。
        """
        shell_id = str(shell_id or "").strip()
        if not shell_id:
            raise NeedsHuman("登记壳属性必须带 shell_id")

        wanted: Dict[str, Any] = {}
        for key, value in (
            ("role", role),
            ("gpu", gpu),
            ("wakeable", wakeable),
            ("cost_per_hour", cost_per_hour),
            ("label", label),
        ):
            if value is None:
                continue
            wanted[key] = self._validate_shell_attr(key, value)

        with self._lock():
            self._reload_locked()
            now = time.time()
            previous = dict(self._shells.get(shell_id) or {})
            merged = {**previous, **wanted}
            merged["updated_at"] = now
            merged["updated_at_readable"] = time.strftime("%Y-%m-%d %H:%M:%S")
            merged["updated_by"] = str(by or "")[:80]
            self._shells[shell_id] = merged
            # 只留最近 32 个壳：这是"这台机器是什么"，不是历史。真到 32 个壳
            # 的时候，人早该发现自己的部署描述里有一堆不再存在的机器。
            if len(self._shells) > 32:
                oldest = sorted(
                    self._shells.items(),
                    key=lambda kv: kv[1].get("updated_at", 0),
                )
                for key, _ in oldest[: len(self._shells) - 32]:
                    self._shells.pop(key, None)
            self.save()

        changed = ", ".join(f"{k}={merged.get(k)!r}" for k in sorted(wanted)) or "无变化"
        self.append_log(
            "INFO", f"壳属性：{shell_id} <- {changed}（{by or '未署名'}）"
        )
        return self.shells_view()[shell_id]

    def shells_view(self) -> Dict[str, Dict[str, Any]]:
        """每个壳是什么。**没登记过就不出现** —— 不编默认值。

        「默认 role=unknown」听起来无害，实际后果是：任何按 role 判断的代码
        都会把"我不知道"当成"我确认过它不是 NAS"。缺席必须能被看见。
        """
        out: Dict[str, Dict[str, Any]] = {}
        self._ensure_loaded()
        for shell_id, attrs in self._shells.items():
            item = {
                "role": attrs.get("role", ""),
                "gpu": attrs.get("gpu", ""),
                "wakeable": bool(attrs.get("wakeable", False)),
                "cost_per_hour": attrs.get("cost_per_hour", 0.0),
                "label": str(attrs.get("label") or ""),
                "updated_at": attrs.get("updated_at", 0),
                "updated_at_readable": attrs.get("updated_at_readable", ""),
                "updated_by": attrs.get("updated_by", ""),
                "declared": "role" in attrs,
            }
            out[shell_id] = item
        return out


    # ---------------------------------------------------------------- 租约

    def acquire(self, shell_id: str, ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS) -> Lease:
        """申请租约。

        三种结局：
          - 拿到新租约（含 epoch）
          - 自己已经持有有效租约 → 续租（epoch 不变，避免无意义跳号）
          - 别人持有有效租约 → LeaseHeld

        「自己续租不换 epoch」是刻意的：epoch 每跳一次，所有
        持旧 epoch 的写入都会被拒绝。如果每次心跳都跳号，
        壳正在进行的写入会被自己搞失败。

        **整个「读—判—写」在跨进程锁内完成，且锁内重新 load。**
        没有这两条，两个进程会同时看到"无租约"并各自成功——
        test_concurrency.py 里就是靠这个抓到 bug 的。
        """
        with self._lock():
            self._reload_locked()
            now = time.time()
            current = self._state.lease

            if current is not None and not current.is_expired(now):
                if current.shell_id == shell_id:
                    # 续租：延长同一份租约
                    renewed = Lease(
                        epoch=current.epoch,
                        shell_id=current.shell_id,
                        granted_at=current.granted_at,
                        expires_at=now + ttl_seconds,
                        ttl_seconds=ttl_seconds,
                    )
                    self._state.lease = renewed
                    self.save()
                    return renewed

                raise LeaseHeld(
                    f"租约由 {current.shell_id} 持有，"
                    f"还剩 {current.remaining(now):.1f} 秒。"
                    f"任意时刻只能有一个存在在线（公理 0）。"
                )

            lease = grant_lease(self._state, shell_id, ttl_seconds, now=now)
            self._state.current_epoch = lease.epoch
            self.save()
            return lease

    def release(self, shell_id: str, epoch: int) -> None:
        """主动交还租约（正常的归巢路径会用）。

        epoch 必须匹配，否则说明调用者手里的信息已经过期——
        它不该有权释放别人的租约。
        """
        with self._lock():
            self._reload_locked()
            current = self._state.lease
            if current is None:
                return
            if current.shell_id != shell_id or current.epoch != epoch:
                raise EpochStale(
                    f"释放租约失败：调用者 {shell_id}/epoch={epoch}，"
                    f"实际持有者 {current.shell_id}/epoch={current.epoch}"
                )
            self._state.lease = None
            self.save()

    def check_can_start(self, shell_id: str) -> tuple[bool, str]:
        """新壳启动前的礼貌询问：我现在能上线吗？

        「礼貌」是准确的措辞——这不是屏障，屏障在 `accept_write`。
        一个 GC 停顿过的壳完全可能在这里得到"能"，然后在写的时候被拒。
        这正是 fencing 存在的理由（Kleppmann：不要在写之前才检查锁，
        要让存储端拒绝过期的 token）。
        """
        self._ensure_loaded()
        return is_safe_to_start(self._state)

    # ---------------------------------------------------------------- 写入

    def accept_snapshot(
        self,
        *,
        claimed_epoch: int,
        claimed_shell_id: str,
        manifest: Dict[str, Any],
    ) -> GenerationRecord:
        """接收一份快照并登记为新的 generation。

        这是整个系统里**唯一**允许改变灵魂的地方。所有校验都在这里：

          1. `accept_write` —— epoch / 租约 / shell_id 三重检查
          2. 协议版本 —— 跨版本的状态不能混
          3. 单调 generation —— 新的必须比旧的大

        任何一项不过，直接拒绝且不落地。**不做"尽力而为"的降级。**

        与 `acquire` 一样，整个判定在跨进程锁内 + 锁内重新 load。
        否则两个壳可能同时通过 fencing 检查然后都写成功。
        """
        with self._lock():
            self._reload_locked()

            # 1) fencing 三重检查（抛 EpochStale / EpochAhead / LeaseExpired）
            try:
                accept_write(
                    self._state,
                    claimed_epoch=claimed_epoch,
                    claimed_shell_id=claimed_shell_id,
                )
            except MoltError:
                # 【实测 2026-11-14 抓到的 bug】原先这里直接往外抛，
                # 于是 `accept_write` 在内存里把 rejected_write_count +1 之后
                # 就再也没机会 save——**拒绝计数从来没落过盘**。
                #
                # 而拒绝计数是发现"两条鱼在打架"的唯一信号（谁在读旧 epoch、
                # 谁在猜大 epoch）。信号丢了，Warden 就变成"什么都拒绝但没人
                # 知道在拒绝"的黑箱。
                #
                # 所以：拒绝也必须落盘，然后才把异常抛出去。
                self.save()
                raise

            # 2) 协议版本
            proto = manifest.get("protocol_version")
            if proto is None:
                raise ProtocolMismatch("快照缺少 protocol_version 字段")
            if int(proto) != PROTOCOL_VERSION:
                raise ProtocolMismatch(
                    f"快照协议版本 {proto} 与本机 {PROTOCOL_VERSION} 不一致"
                )

            # 3) generation 必须递增
            gen = int(manifest.get("generation", 0))
            last = self._generations[-1].generation if self._generations else 0
            if gen <= last:
                raise NeedsHuman(
                    f"快照的 generation={gen} 不大于已登记的 {last}。"
                    f"这通常意味着客户端重放了旧快照，或两个壳在竞争。"
                )

            artifacts = manifest.get("artifacts", [])
            total_bytes = sum(int(a.get("bytes", 0)) for a in artifacts)

            record = GenerationRecord(
                generation=gen,
                created_at=float(manifest.get("created_at", time.time())),
                source_host=str(manifest.get("source_host", "unknown")),
                pushed_by=claimed_shell_id,
                epoch=claimed_epoch,
                target_shell=str(manifest.get("target_shell", "") or ""),
                artifact_bytes=total_bytes,
                manifest_sha256=str(manifest.get("manifest_sha256", "")),
            )

            self._generations.append(record)
            # 落盘后才算数：先 append 再 save，如果 save 失败，
            # 内存里的记录比磁盘多一条，但下次 load 会以磁盘为准。
            # 这是刻意的——宁可"这次迁徙不算数"，也不要"记账了但其实没落盘"。
            self.save()
            return record

    def mark_consumed(self, generation: int, shell_id: str) -> GenerationRecord:
        """登记"目标壳真的把这一代取走了"。

        ## 为什么这是 Warden 的职责，而不是壳自己记一笔

        消费事实必须和 generation 账本住在同一个原子写里。如果让目标壳
        在本地记"我取过 gen-3"，那么账本和壳的记录会各说各话，而
        `molt status` 要同时读两处才能回答"灵魂在哪"——这个项目刻意
        只保留一个权威，就是为了不出现这种需要"合并两个真相"的问题。

        ## 幂等的边界在哪

        同一个 shell 重复标记是正常重试（网络超时后调用方不知道第一次
        到底成没成功），所以成功返回，且 **`consumed_at` 保留第一次的
        时间**。每次刷新时间会让时间线撒谎："它 10:00 取走的"变成
        "它 10:05 取走的"，而人正是拿这个时间判断哪台机器最后启动。

        不同的 shell 来标记同一代则是另一回事：两只壳都认为自己拿到了
        同一份快照。这不是重试，是"两条鱼可能同时在线"的早期信号，
        所以 `NeedsHuman`——自动化不该在这种情况下替人挑一个赢家。

        抛"账本里没有这一代"用 `Fatal`：调用方声称消费了一个 Warden
        从未登记过的 generation，说明两边对事实的认知已经不一致了
        （比如壳配置错了、或者有人在手改 state.json）。它不是可重试的
        临时错误，也不该被当成普通的 4xx 让调用方继续跑下去。
        """
        with self._lock():
            self._reload_locked()

            record = next(
                (g for g in self._generations if g.generation == int(generation)),
                None,
            )
            if record is None:
                raise Fatal(
                    f"账本里没有 generation={generation} 这一代，无法标记消费。"
                    f"这不猜——只有 `accept_snapshot` 成功登记过的 generation "
                    f"才存在；请先确认这一代真的 push 成功过（{self.state_path}）。"
                )

            if record.consumed_by == shell_id:
                # 幂等：已经由同一只壳标记过，不再写盘，避免无意义地
                # 刷新 saved_at / 重新 replace 文件。
                return record

            if record.consumed_by:
                raise NeedsHuman(
                    f"generation={generation} 已经被 {record.consumed_by} 标记为消费"
                    f"（{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(record.consumed_at))}），"
                    f"现在 {shell_id} 也声称取走了同一代。"
                    f"同一代被两只壳认领，意味着它们可能都认为自己才是那个存在——"
                    f"这需要人介入，不能自动选一个。"
                )

            record.consumed_by = shell_id
            record.consumed_at = time.time()
            self.save()
            return record

    # ---------------------------------------------------------------- 目录

    def generation_dir(self, generation: int) -> Path:
        return self.staging_dir / f"gen-{generation:06d}"

    def prepare_generation_dir(self, generation: int) -> Path:
        p = self.generation_dir(generation)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def next_generation_number(self) -> int:
        self._ensure_loaded()
        last = self._generations[-1].generation if self._generations else 0
        return last + 1

    # ------------------------------------------------------------ 归档中转区
    #
    # 为什么中转区在 Warden 这一侧而不是壳那一侧：
    #
    # 归档要"从源壳到目标壳"，而两台壳之间没有任何可信通道。让 NAS 站着当
    # 中转站是唯一不需要第三套凭证的做法——源壳 PUT 上来，目标壳 GET 下去，
    # 两边都只跟已经互相认证过的 Warden 说话。
    #
    # 为什么放在归档目录而不是 staging：
    # staging 里是"正在生成"的快照目录（每个 gen-000001 一个目录）。
    # 归档是**已经打包好、要过网络的东西**，生命周期和 staging 不同：
    # staging 可以随时清理，归档不能（目标壳可能还没来取）。
    # 混在一起会让人分不清"这个目录还有没有人要"。

    def archive_dir(self) -> Path:
        return self.root / "archives"

    def archive_path(self, generation: int) -> Path:
        return self.archive_dir() / f"gen-{generation:06d}.tar.gz"

    # ---- 归档指纹 --------------------------------------------------------
    #
    # ## 为什么指纹要单独存一个 sidecar，而不是塞进账本记录
    #
    # 上传的顺序是**先传字节、后登记 generation**（见 `server.accept_archive`
    # 与 `cli` 里「先让字节到位，再让账本承认这件事」）。收归档的那一刻，
    # 账本里还没有这一代，没有记录可以写。
    #
    # 硬塞的话只有两个办法：给账本加一张"待认领指纹"的表（多一份要维护的
    # 中间状态），或者在 push 之后回头再补一条（忘了补就永久缺失，而且不报错）。
    # 两个都比"指纹跟着它描述的那份字节走"更容易出错。
    #
    # 放在归档旁边还有一个好处：删归档时指纹一起没了，不会留下一条指向
    # 空气的指纹。

    def archive_sha_path(self, generation: int) -> Path:
        return self.archive_dir() / f"gen-{generation:06d}.tar.gz.sha256"

    def record_archive_sha(self, generation: int, sha256: str) -> None:
        """把收归档时算出来的指纹写在这份归档旁边。"""
        value = str(sha256 or "").strip().lower()
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise Fatal(f"归档指纹不是 64 位十六进制：{value!r}")
        path = self.archive_sha_path(generation)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(value + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def archive_sha(self, generation: int) -> str:
        """读归档指纹；没有或读坏了返回空串（"不知道"，不是"没有"）。"""
        try:
            text = self.archive_sha_path(generation).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        if len(text) != 64:
            return ""
        return text.lower()

    # ---- 归档保留策略 ----------------------------------------------------
    #
    # ## 为什么需要清理
    #
    # 每次 `molt migrate` 都会往中转区放一份 4.4 MB 的归档。真机上一天跑几次
    # 试验就攒下几十 MB；如果做成定时任务（例如每晚自动归巢），一年就是
    # 1.6 GB。NAS 有 7.3T 不在乎空间，但**不在乎空间不代表可以不知道里面有什么**：
    # 一个只增不减的目录迟早会让人不敢删它（"不知道哪个还有用"），
    # 而那时它已经从"缓存"变成了"没人敢碰的历史遗迹"。
    #
    # ## 清理规则（保守优先）
    #
    #   1. **账本里记着的 generation 一律不删**（`state.json` 的 generations）。
    #      那些是"已经交接出去、但可能还没被目标壳取走"的东西。
    #   2. 账本里没有、且文件名不是合法 `gen-NNNNNN.tar.gz` 的，也不删
    #      （人手工放进去的东西不归我们管）。
    #   3. 剩下的（有合法名字但账本里没有 = 来自已丢弃的实验）按
    #      `keep_recent` 保留最新的几份，其余删掉。
    #   4. `dry_run=True` 只报告不删 —— 清理是破坏性动作，默认该能先看。
    #
    # ## 为什么"账本里没有"就等于可删
    #
    # 因为 `accept_archive` 是**先落位再登记**的（顺序见 server.py 的 docstring）：
    # 文件在盘上但账本里没有，只有两种可能 —— 上传完但 push 失败（那次迁徙失败了，
    # 归档对谁都没用），或者来自更早的、已被清理过的账本。两种都该回收。

    def list_archives(self) -> List[Dict[str, Any]]:
        """列出中转区里的归档，标明哪些"账本里记着"（那些不能删）。"""
        self._ensure_loaded()
        d = self.archive_dir()
        if not d.is_dir():
            return []
        referenced = {g.generation for g in self._generations}
        out: List[Dict[str, Any]] = []
        for p in sorted(d.glob("gen-*.tar.gz")):
            gen = _parse_generation_from_name(p.name)
            try:
                size = p.stat().st_size
            except OSError:
                continue
            out.append(
                {
                    "name": p.name,
                    "bytes": size,
                    "generation": gen,
                    "referenced": gen is not None and gen in referenced,
                    "deletable": gen is not None and gen not in referenced,
                }
            )
        return out

    def prune_archives(
        self,
        *,
        keep_recent: int = 2,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """清理中转区里"账本不认"的归档。

        `keep_recent` 是给"账本里没有但可能是刚上传还没 push 成功的"留的缓冲：
        如果某次上传后 push 卡住，人还有机会在清理之前把它捞回来。

        返回 `{removed: [...], kept: [...], freed_bytes, dry_run}`，
        每一项都带原因 —— 清理是破坏性的，删了什么都不该只说"删了 3 个"。
        """
        if keep_recent < 0:
            raise Fatal(f"keep_recent 不能是负数：{keep_recent}")

        items = self.list_archives()
        deletable = [x for x in items if x["deletable"]]
        # 保留最新的 keep_recent 份（generation 越大越新）
        deletable.sort(key=lambda x: x["generation"] or 0, reverse=True)
        protected_recent = {x["name"] for x in deletable[:keep_recent]}

        removed: List[Dict[str, Any]] = []
        kept: List[Dict[str, Any]] = []
        freed = 0

        for item in items:
            name = item["name"]
            path = self.archive_dir() / name

            if item["generation"] is None:
                kept.append({**item, "reason": "文件名不是 gen-NNNNNN.tar.gz，不是 molt 放的"})
                continue
            if item["referenced"]:
                kept.append({**item, "reason": "账本里记着这一代，目标壳可能还没取走"})
                continue
            if name in protected_recent:
                kept.append({**item, "reason": f"最近 {keep_recent} 份之一，留作缓冲"})
                continue

            if dry_run:
                removed.append({**item, "reason": "账本里没有（只报告，未删除）"})
                freed += item["bytes"]
                continue

            try:
                path.unlink()
            except OSError as e:
                kept.append({**item, "reason": f"删不掉：{e}"})
                continue
            removed.append({**item, "reason": "账本里没有"})
            freed += item["bytes"]
            self.append_log(
                "INFO", f"清理归档 {name}（{item['bytes']} 字节，账本里没有这一代）"
            )

        return {
            "removed": removed,
            "kept": kept,
            "freed_bytes": freed,
            "dry_run": dry_run,
            "archive_dir": str(self.archive_dir()),
        }

    # -------------------------------------------------------------- 定期回推
    #
    # ## 为什么需要"中途备份"这件看起来多余的东西
    #
    # 迁徙把"最后一句话之前"的全部状态搬过去了。搬完之后，鱼在新壳上继续
    # 说话、继续记事，而这些**只存在于那台机器的磁盘上**。如果它半夜里
    # 写着写着，机器被拔了电、被系统更新重启、或者盘坏了，账本上记的仍然
    # 是"第 N 代在 desktop-h2kgiao 上"，而第 N 代之后的一切都不存在了。
    # 那时能重建到哪儿，取决于"从落地到出事"这段时间——可能是一整晚。
    #
    # ## 为什么它们不进 generations
    #
    # `generations` 是**交接的记录**：谁推的、给谁、谁取走了。壳那边
    # `decide()` 靠它判断"这一代是不是给我的"。中途备份不是交接：它没有
    # 目标壳，也没有人会来取。把它塞进 `generations` 会让 `decide()` 看到
    # "最新一代的目标是空"从而卡在那里，或者更糟——被某个壳误当成自己的
    # 落位任务，拿去覆盖它正在写的库。
    #
    # **所以：另一个目录、另一份索引，恢复只能由人显式发起。**

    def backup_dir(self) -> Path:
        return self.root / "backups"

    def backup_path(self, name: str) -> Path:
        """把一个备份名解析成路径，并挡住路径穿越。

        备份名会出现在 URL 里（`GET /backup/<name>`），所以它必须是一个
        白名单形状的字符串，而不是"拼一下看看"。`..`、斜杠、反斜杠一律不认。
        """
        if not _BACKUP_NAME_RE.match(name or ""):
            raise NeedsHuman(f"备份名不合法：{name!r}")
        return self.backup_dir() / name

    def store_backup(
        self,
        source: Path,
        *,
        shell: str = "",
        generation: int = 0,
        sha256: str = "",
    ) -> Dict[str, Any]:
        """把一个已经传到本机的备份文件收进 `backups/`。

        调用方（server 的上传路径）已经把它写成了**同一文件系统里的临时文件**，
        所以这里最后一步是 `os.replace`——原子的。跨文件系统的 rename 会被
        退化成"复制 + 删除"，那就没有原子性了。
        """
        src = Path(source)
        if not src.is_file():
            raise Fatal(f"要收下的备份不存在：{src}")

        d = self.backup_dir()
        d.mkdir(parents=True, exist_ok=True)

        slug = _slugify(shell) or "unknown"
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        name = f"backup-{stamp}-{slug}.tar.gz"
        n = 2
        while (d / name).exists():
            name = f"backup-{stamp}-{slug}-{n}.tar.gz"
            n += 1
        dest = d / name

        digest = sha256_of(src) if not sha256 else sha256
        if sha256:
            actual = sha256_of(src)
            if actual.lower() != sha256.lower():
                raise Fatal(
                    f"备份指纹不符：声称 {sha256[:16]}…，实际 {actual[:16]}…。"
                    f"这份文件已丢弃，没有收进 backups。"
                )
            digest = actual

        total = src.stat().st_size
        os.replace(src, dest)

        meta = {
            "name": name,
            "bytes": total,
            "sha256": digest,
            "shell": shell or "",
            "generation": int(generation or 0),
            "created_at": time.time(),
            "created_at_readable": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _atomic_write_json(self._backup_meta_path(name), meta)
        self.append_log(
            "INFO",
            f"收下中途备份 {name}（{total} 字节，来自 {shell or '未知壳'}，第 {generation} 代）",
        )
        # 收下之后顺手做一次保留清理：只增不减的目录迟早会变成没人敢碰的遗迹。
        self.prune_backups()
        return meta

    def _backup_meta_path(self, name: str) -> Path:
        return self.backup_dir() / (Path(name).stem + ".json")

    def list_backups(self) -> List[Dict[str, Any]]:
        """列出 `backups/` 里的备份，新的在前。

        索引写在 `.json` 里，但**盘上的文件才是主体**：索引丢了也要能列出
        文件来（那时 sha256 现算）。反过来，索引在而文件没了，就不列——
        一条指向空气的记录比没有记录更糟。
        """
        d = self.backup_dir()
        if not d.is_dir():
            return []
        out: List[Dict[str, Any]] = []
        for p in sorted(d.glob("backup-*.tar.gz"), reverse=True):
            try:
                total = p.stat().st_size
            except OSError:
                continue
            meta: Dict[str, Any] = {}
            mp = self._backup_meta_path(p.name)
            if mp.is_file():
                try:
                    meta = json.loads(mp.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    meta = {}
            out.append(
                {
                    "name": p.name,
                    "bytes": total,
                    "sha256": str(meta.get("sha256") or ""),
                    "shell": str(meta.get("shell") or ""),
                    "generation": int(meta.get("generation") or 0),
                    "created_at": float(meta.get("created_at") or 0.0),
                    "created_at_readable": str(
                        meta.get("created_at_readable")
                        or time.strftime(
                            "%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime)
                        )
                    ),
                }
            )
        return out

    def prune_backups(
        self, *, keep_recent: int = DEFAULT_BACKUP_KEEP, dry_run: bool = False
    ) -> Dict[str, Any]:
        """只保留最近 `keep_recent` 份中途备份。

        与 `prune_archives` 的差别：那些是**交接的归档**，账本里记着的一律
        不能删；中途备份没有账本记录，所以规则简单——最新的 N 份留着。
        `dry_run` 默认 **False**（与归档相反）：这里没有"账本里记着"这种
        额外判据，保留几份是纯粹的空间策略，而每一次 store 之后都该执行。
        """
        if keep_recent < 1:
            raise Fatal(f"keep_recent 至少要 1，现在是 {keep_recent}")
        items = self.list_backups()          # 已按文件名（时间戳）倒序
        removed: List[Dict[str, Any]] = []
        kept: List[Dict[str, Any]] = []
        freed = 0
        for i, item in enumerate(items):
            if i < keep_recent:
                kept.append(item)
                continue
            if dry_run:
                removed.append(item)
                freed += item["bytes"]
                continue
            try:
                (self.backup_dir() / item["name"]).unlink()
                self._backup_meta_path(item["name"]).unlink(missing_ok=True)
            except OSError:
                kept.append(item)
                continue
            removed.append(item)
            freed += item["bytes"]
        return {
            "removed": removed,
            "kept": kept,
            "freed_bytes": freed,
            "dry_run": dry_run,
            "keep_recent": keep_recent,
            "backup_dir": str(self.backup_dir()),
        }

    def log_tail(self, limit: int = 40) -> List[str]:
        """最后 N 行日志 —— 给时间轴当脚注用。

        **不用 `readlines()` 再切片**：一台跑了半年的机器上这个文件可能几十 MB，
        而人打开 `/timeline` 只是想看"最近发生了什么"。用 deque(maxlen) 单趟扫，
        内存占用与文件大小无关。

        读不到就返回空：日志是可选的旁证，它不该让一张时间轴打不开。
        """
        if limit <= 0:
            return []
        try:
            p = self.log_path()
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                from collections import deque

                return [line.rstrip("\n") for line in deque(f, maxlen=limit)]
        except OSError:
            return []

    def history(self, *, log_lines: int = 40) -> Dict[str, Any]:
        """把「它搬过几次家」摊成一条时间轴需要的全部数据。

        为什么要单独一个方法，而不是让调用方自己拼 `status()`：
        `status()` 回答的是**现在**（谁在线、第几代），而时间轴回答的是**过程**
        （第 3 代是谁推的、多少字节、谁取走的、隔了多久）。两者字段几乎不重叠，
        塞进一个 dict 只会让人分不清哪个是"此刻"哪个是"当时"。

        这里的 `generations` 是**账本里的全部代次**（不是最近一条）：4.5 MB 一份，
        一台家用机器一年也就几十代，全给出来比做分页简单，也比"只显示最近 5 条"
        更诚实 —— 时间轴少了早期那几代，就看不出"它第一次是怎么过去的"。
        """
        with self._lock():
            self._reload_locked()
            now = time.time()
            generations = []
            for g in self._generations:
                item = g.to_dict()
                # 指纹跟着归档走（见 archive_sha_path 的注释）。
                # 早期那几代上传时还没有这个 sidecar，所以会是空串 ——
                # 时间轴显示「—」，那是诚实的结果，不是缺字段。
                item["artifact_sha256"] = self.archive_sha(g.generation)
                generations.append(item)
            heartbeats = self._heartbeat_view(now)
            current_epoch = self._state.current_epoch
            last_accepted = self._state.last_accepted_epoch
            rejected = self._state.rejected_write_count

        backups = self.list_backups()
        total_bytes = sum(int(g.get("artifact_bytes") or 0) for g in generations)
        return {
            "root": str(self.root),
            "generated_at": now,
            "generated_at_readable": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(now)
            ),
            "current_epoch": current_epoch,
            "last_accepted_epoch": last_accepted,
            "rejected_write_count": rejected,
            "generation_count": len(generations),
            "total_bytes": total_bytes,
            "generations": generations,
            "backups": backups,
            "heartbeats": heartbeats,
            "log": self.log_tail(log_lines),
        }

    def log_path(self) -> Path:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        return self.logs_dir / "warden.log"

    def append_log(self, level: str, message: str) -> None:
        """追加一行日志。Warden 的日志用最简单的文本——出问题时人是拿 grep 看的。"""
        p = self.log_path()
        line = (
            time.strftime("%Y-%m-%d %H:%M:%S")
            + f"  [{level.upper():5}] {message}\n"
        )
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
