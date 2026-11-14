"""Soul：可迁移的那部分状态。

## 什么是 Soul

不是"人格数据"，而是**这个存在活着所必需的全部非机器专属状态**：

  - `data_v4.db`              会话、消息历史、persona
  - `memory_companion.db`     长期记忆（memories / knowledge_nodes / identities）
  - `cmd_config.json`         配置（含机器绑定项，见 §6.6 覆写层）
  - `plugin_data/**`          插件数据（meme_manager 的表情包是大头）
  - `skills/ workspaces/ attachments/ knowledge_base/`

**不是 Soul 的**：`data/site-packages/`（119 个包 / 1325 MB，122 个 `.so`，
全是 linux-x86_64，含 torch）。它能重装，记忆不能重建。

## 为什么用 sqlite3.backup() 而不是拷文件

拷文件的做法（`cp data_v4.db`、`rsync`）在库正在被写的时候是**错的**：

  - 只拷 `.db` 不拷 `-wal` → 丢已提交事务（wal.html §4）
  - 普通 `rsync` 不理解 SQLite 事务 → "The database copy might be corrupt."
    （rsync.html §6 原文）
  - WAL 模式下，一致性瞬时快照需要拿到 read lock

`sqlite3.Connection.backup()` 是官方提供的热备 API，源库以只读 URI
（`file:...?mode=ro`）打开，不需要停 AstrBot。本项目实测：
10.9 MB 的库 0.017–0.27 秒完成，`PRAGMA quick_check` 返回 ok。

## 为什么每个 artifact 都要 sha256 + quick_check

因为"文件传过去了"和"文件能用"是两件事。跨机传输、SMB、压缩解压
每一个环节都可能悄悄坏掉；而 AstrBot 拿到一个坏库时的表现是
**启动时报一个看不懂的错**，或者更糟：启动了但记忆是空的。

所以快照自带 manifest，接收侧必须逐项验证后才允许启动。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .errors import Fatal, NeedsHuman


# 这些是"必须在快照里出现"的库。缺任何一个都不能算一次成功的迁徙。
# 允许某个库在这个部署里不存在（比如用户没装 memory_companion），
# 但一旦它存在于源端，就必须被完整带走。
REQUIRED_SQLITE_ARTIFACTS = (
    "data_v4.db",
    "plugin_data/astrbot_plugin_memory_companion/memory_companion.db",
)

# 这些是目录/文件，整体同步即可，没有事务语义。
COPY_ARTIFACTS = (
    "cmd_config.json",
    "mcp_server.json",
    "skills",
    "workspaces",
    "attachments",
    "knowledge_base",
    "plugin_data",
)


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    """流式计算 sha256。大文件（1325 MB 的 site-packages）不读进内存。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sqlite_check(path: Path) -> Dict[str, Any]:
    """对一个 SQLite 文件做体检，返回可写进 manifest 的事实。

    用 `quick_check` 而不是 `integrity_check`：
      - quick_check 跳过 UNIQUE 与索引一致性校验
      - 前者 O(N)，后者 O(N log N)
      - 我们要回答的是"这个文件能不能被打开并正常查询"，
        不是"它的每一个索引都和表严格对应"

    这不是妥协：索引损坏会在 AstrBot 实际查询时立刻暴露，
    而全量 integrity_check 会让每次迁徙慢一个数量级。
    """
    if not path.exists():
        raise NeedsHuman(f"快照体检失败：文件不存在 {path}")

    # 只读打开，绝不动源文件
    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            quick = conn.execute("PRAGMA quick_check").fetchone()
            journal = conn.execute("PRAGMA journal_mode").fetchone()
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            page_count = conn.execute("PRAGMA page_count").fetchone()
            freelist = conn.execute("PRAGMA freelist_count").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        # 【实测 2026-11-14】坏库的行为不是"quick_check 返回非 ok"，
        # 而是直接抛 DatabaseError：
        #   覆盖 B-tree 页 → "database disk image is malformed"
        #   连页头都坏    → "file is not a database"
        # 必须在这里包成 Fatal，否则它会以一个 Warden 不认识的原始异常逃出去，
        # 既不会被归类成"不可恢复"，也不会触发正确的处置路径。
        raise Fatal(
            f"快照体检失败：{path.name} 无法作为数据库打开（{e}）。"
            f"这个文件已损坏，不能拿去做迁徙的源。"
        ) from e

    verdict = quick[0] if quick else "no-result"
    if verdict != "ok":
        raise Fatal(
            f"快照体检失败：{path.name} 的 quick_check 返回 {verdict!r}，"
            f"不是 'ok'。这个库已经损坏，不能拿去做迁徙的源。"
        )

    return {
        "quick_check": verdict,
        "journal_mode": journal[0] if journal else None,
        "table_count": len(tables),
        "tables": [r[0] for r in tables],
        "page_count": page_count[0] if page_count else None,
        "freelist_count": freelist[0] if freelist else None,
    }


def _drop_empty_sidecars(db: Path) -> List[str]:
    """删掉副本库旁边空的 `-wal` / `-shm`，返回删掉的路径。

    ## 为什么会有这两个文件

    副本是从 WAL 模式的源库备份出来的，所以副本的文件头里
    journal_mode 仍然是 `wal`。**任何**打开它的连接——哪怕只是
    `mode=ro` 的只读体检——SQLite 都会为它建出 `-shm`（32KB 的
    共享内存索引）和 `-wal`。

    只读连接关闭时**删不掉**它们（没有写权限、也没打算写）。
    于是校验完的 staging 目录里就留下了这两个文件。

    ## 为什么必须删掉

    把一个上次关机时没清理的 `-shm` 送到另一台机器上，是在赌
    那份共享内存索引与它的库文件还匹配。正常的 SQLite 生命周期
    里 `-shm` 是临时性的：最后一个连接关闭时就该消失。
    留下它 = 人工制造了一个"看起来像崩溃现场"的状态。

    ## 只在 `-wal` 是空的时候删

    `-wal` 非空意味着里面还有**尚未合并进主库的事务**。那种情况下
    删 `-wal` 就是丢数据，必须报错让人看。

    另外：**这个函数只能对副本调用，绝不能对源库调用**。
    对正在被 AstrBot 写着的源库删 `-wal`/`-shm` 会直接把库搞坏。
    """
    removed: List[str] = []
    wal = db.with_name(db.name + "-wal")
    shm = db.with_name(db.name + "-shm")

    if wal.exists():
        size = wal.stat().st_size
        if size > 0:
            raise Fatal(
                f"副本 {db.name} 的 -wal 里有 {size} 字节未合并的事务，"
                f"删掉它会丢数据。这次热备需要人工检查。"
            )
        wal.unlink()
        removed.append(str(wal))

    if shm.exists():
        shm.unlink()
        removed.append(str(shm))

    return removed


def hot_backup(src: Path, dst: Path, *, verify: bool = True) -> Dict[str, Any]:
    """把正在被写的 SQLite 库热备到 dst，并默认做完整校验。

    关键点：源库以 `mode=ro` 只读 URI 打开。这既是安全的（不会意外写源库），
    也让 SQLite 知道我们只是个读者，不会尝试拿写锁去阻塞 AstrBot。
    """
    if not src.exists():
        raise NeedsHuman(f"热备失败：源文件不存在 {src}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()

    src_uri = f"file:{src.as_posix()}?mode=ro"
    source = sqlite3.connect(src_uri, uri=True)
    try:
        dest = sqlite3.connect(str(dst))
        try:
            source.backup(dest)
        finally:
            dest.close()
    except sqlite3.DatabaseError as e:
        # 坏库在这里抛的是 `sqlite3.DatabaseError: file is not a database`。
        # 不包成 Fatal 的后果实测过：它会以未经归类的原始异常穿过
        # `cmd_migrate` 的 `except Fatal`，于是 **租约永远收不回来**，
        # 白白占住 90 秒，而且人看到的是一段 Traceback 而不是"备份失败"。
        #
        # 这也说明一条规律：凡是 sqlite3 可能抛的地方都要包。上次只包了
        # `sqlite_check`，漏了这里。
        raise Fatal(
            f"热备失败：{src.name} 无法被读取（{e}）。"
            "这个库可能已经损坏，或者根本不是 SQLite 文件。"
        ) from e
    finally:
        source.close()

    elapsed = time.time() - started

    info: Dict[str, Any] = {
        "path": str(dst),
        "bytes": dst.stat().st_size,
        "sha256": sha256_of(dst),
        "backup_seconds": round(elapsed, 4),
    }

    if verify:
        info.update(sqlite_check(dst))
        # 源库的表清单必须与副本一致，防止"备份了半个库"
        src_info = sqlite_check(src)
        if src_info["table_count"] != info["table_count"]:
            raise Fatal(
                f"热备结果不一致：{src.name} 源库 {src_info['table_count']} 张表，"
                f"副本 {info['table_count']} 张表。这次备份不可信。"
            )
        info["source_table_count"] = src_info["table_count"]

    # 校验完之后清掉 sidecar —— 必须在校验之后，
    # 因为校验本身（只读打开）正是产生它们的原因。
    # 顺序反了就会删掉刚生成的文件然后校验又建回来。
    info["sidecars_removed"] = _drop_empty_sidecars(dst)

    return info


@dataclass
class Artifact:
    """manifest 里的一个条目。"""

    relpath: str                 # 相对 data/ 的路径
    kind: str                    # "sqlite" | "copy"
    bytes: int = 0
    sha256: str = ""
    quick_check: Optional[str] = None
    table_count: Optional[int] = None
    backup_seconds: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "relpath": self.relpath,
            "kind": self.kind,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "quick_check": self.quick_check,
            "table_count": self.table_count,
            "backup_seconds": self.backup_seconds,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Artifact":
        return cls(
            relpath=d["relpath"],
            kind=d["kind"],
            bytes=int(d.get("bytes", 0)),
            sha256=d.get("sha256", ""),
            quick_check=d.get("quick_check"),
            table_count=d.get("table_count"),
            backup_seconds=d.get("backup_seconds"),
        )


MANIFEST_SCHEMA_VERSION = 1

# 缺了任何一个，这份快照就说不清"从哪来、第几代、谁交的"。
# 而说不清来源的快照落位之后，出事时无从追责 —— 所以不是"尽量补默认值"，
# 而是直接拒绝。
_REQUIRED_MANIFEST_FIELDS = (
    ("generation", int, "第几代"),
    ("created_at", (int, float), "快照生成时刻"),
    ("source_host", str, "从哪台机器来的"),
    ("source_root", str, "源 data 目录"),
    ("protocol_version", int, "协议版本"),
)


def manifest_problems(d: Any) -> List[str]:
    """检查一份 manifest 字典，返回问题清单（空 = 通过）。

    为什么不在 `from_dict` 里直接 `int(d["generation"])` 了事：
    那样缺字段报的是 `KeyError: 'generation'`，类型错报的是
    `ValueError: invalid literal for int()`。**这两种错误都指向"代码写错了"，
    而不是"这份快照有问题"** —— 于是它会被人当成 bug 而不是被当成拒收理由。
    """
    problems: List[str] = []
    if not isinstance(d, dict):
        return [f"顶层不是一个 JSON 对象，而是 {type(d).__name__}"]

    for key, kinds, meaning in _REQUIRED_MANIFEST_FIELDS:
        if key not in d:
            problems.append(f"缺字段 `{key}`（{meaning}）")
            continue
        value = d[key]
        # bool 是 int 的子类，`True` 不该被当成合法的 generation
        if isinstance(value, bool) or not isinstance(value, kinds):
            problems.append(
                f"字段 `{key}`（{meaning}）类型不对：期望 "
                f"{' / '.join(k.__name__ for k in (kinds if isinstance(kinds, tuple) else (kinds,)))}，"
                f"实际 {type(value).__name__}（{value!r}）"
            )

    for key in ("source_host", "source_root"):
        if isinstance(d.get(key), str) and not d[key].strip():
            problems.append(f"字段 `{key}` 是空的 —— 等于没说从哪来")

    if "schema_version" in d:
        sv = d["schema_version"]
        if isinstance(sv, bool) or not isinstance(sv, int):
            problems.append(f"`schema_version` 必须是整数，实际 {type(sv).__name__}")
        elif sv != MANIFEST_SCHEMA_VERSION:
            problems.append(
                f"`schema_version` 是 {sv}，本机只认 {MANIFEST_SCHEMA_VERSION}"
            )

    return problems


@dataclass
class Manifest:
    """一次迁徙的出生证明。

    这张表是"它还活着"的证据：什么时候、从哪台机器、带走了什么、
    每一项多大、校验和是多少、体检过没有。
    """

    generation: int
    created_at: float
    source_host: str
    source_root: str
    protocol_version: int
    astrbot_version: str
    platform: str                       # "linux" | "windows"
    python_version: str
    artifacts: List[Artifact] = field(default_factory=list)
    # 运行时装在 data/site-packages 里的额外包（edge_tts / whisper 等）。
    # 它们不在源码 requirements.txt 里，新壳必须重装，否则人格的语音能力会缺。
    runtime_pip: List[str] = field(default_factory=list)
    # 打算交给哪个壳。**允许为空**——checkout 时往往还不知道目标是谁
    # （顺序是"先死后生"，目标壳可能还没开机）。留空不是缺陷，
    # 而是如实记录"这次提交时还不知道"。
    target_shell: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generation": self.generation,
            "created_at": self.created_at,
            "created_at_readable": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(self.created_at)
            ),
            "source_host": self.source_host,
            "source_root": self.source_root,
            "protocol_version": self.protocol_version,
            "astrbot_version": self.astrbot_version,
            "platform": self.platform,
            "python_version": self.python_version,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "runtime_pip": self.runtime_pip,
            "target_shell": self.target_shell,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Manifest":
        problems = manifest_problems(d)
        if problems:
            raise Fatal(
                "这份 manifest 不完整，不能拿它当快照的出生证明：\n"
                + "\n".join(f"  - {p}" for p in problems)
                + "\n  一份说不清「从哪来、第几代、谁交的」的快照，落位之后无从追责。"
            )
        return cls(
            generation=int(d["generation"]),
            created_at=float(d["created_at"]),
            source_host=d["source_host"],
            source_root=d["source_root"],
            protocol_version=int(d["protocol_version"]),
            astrbot_version=d.get("astrbot_version", "unknown"),
            platform=d.get("platform", "unknown"),
            python_version=d.get("python_version", "unknown"),
            artifacts=[Artifact.from_dict(a) for a in d.get("artifacts", [])],
            runtime_pip=list(d.get("runtime_pip", [])),
            target_shell=d.get("target_shell", "") or "",
        )

    # -- 落盘 --

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        if not path.exists():
            raise NeedsHuman(f"manifest 不存在：{path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError as e:
            raise Fatal(f"manifest 不是 UTF-8 文本：{path}\n  {e}") from e
        except ValueError as e:
            raise Fatal(
                f"manifest 不是合法 JSON：{path}\n"
                f"  第 {getattr(e, 'lineno', '?')} 行第 {getattr(e, 'colno', '?')} 列："
                f"{getattr(e, 'msg', e)}\n"
                "  半份的 manifest 看起来和完整的一样能读出来，但它的含义是"
                "「上一次写它的时候断电了」。"
            ) from e
        return cls.from_dict(payload)

    # -- 校验 --

    def verify_against(self, root: Path) -> List[str]:
        """逐项验证快照内容，返回问题清单（空列表 = 全部通过）。

        刻意返回列表而不是抛异常：一次迁徙可能同时有多个问题，
        把它们全列出来比"报第一个就停"更有用。

        但也刻意不提供"忽略某些问题继续"的开关——验证不通过就是不能启动。
        """
        problems: List[str] = []

        for art in self.artifacts:
            target = root / art.relpath
            if not target.exists():
                problems.append(f"缺失：{art.relpath}")
                continue

            if art.kind == "copy" and target.is_dir():
                continue        # 目录型 copy artifact 不做整体 sha256

            actual_bytes = target.stat().st_size
            if art.bytes and actual_bytes != art.bytes:
                problems.append(
                    f"大小不符：{art.relpath} 期望 {art.bytes}，实际 {actual_bytes}"
                )
                continue

            actual_sha = sha256_of(target)
            if art.sha256 and actual_sha != art.sha256:
                problems.append(
                    f"sha256 不符：{art.relpath} 期望 {art.sha256[:16]}…，"
                    f"实际 {actual_sha[:16]}…"
                )
                continue

            if art.kind == "sqlite":
                try:
                    info = sqlite_check(target)
                except Fatal as e:
                    problems.append(f"体检失败：{art.relpath} —— {e}")
                    continue
                if art.table_count is not None and info["table_count"] != art.table_count:
                    problems.append(
                        f"表数不符：{art.relpath} 期望 {art.table_count}，"
                        f"实际 {info['table_count']}"
                    )

        return problems


def _dist_name_from_metadata(dist_info: Path) -> Optional[str]:
    """从 `*.dist-info/METADATA` 里读出发行版的真名。

    ## 为什么不能靠目录名

    `site-packages` 里既有**导入名**目录也有**发行版**目录，两者经常不一致：

    | 目录 | 真正的发行版名 |
    |---|---|
    | `PIL/` | `pillow` |
    | `Cryptodome/` | `pycryptodomex` |
    | `_distutils_hack/` | `setuptools` |

    第一版直接拿目录名拼 `pip install PIL`，那是装不上的——`PIL` 是个
    历史遗留名字，PyPI 上真正的项目叫 `Pillow`。生成出来的清单看起来
    很合理（61 个包），但新壳照着装会一路失败。

    唯一可靠的来源是 METADATA 里的 `Name:` 字段。
    """
    meta = dist_info / "METADATA"
    if not meta.exists():
        return None
    try:
        with meta.open("r", encoding="utf-8", errors="ignore") as fh:
            for _ in range(40):          # Name 总在最前面几行，不必读全文件
                line = fh.readline()
                if not line:
                    break
                if line.startswith("Name:"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        return None
    return None


def collect_runtime_pip(site_packages: Path) -> List[str]:
    """列出装在 `data/site-packages/` 里的额外包，形式 `name==version`。

    为什么需要这个：实测发现 `edge_tts`（7.3.1）与 `whisper` **不在**
    AstrBot 源码的 requirements.txt 里（grep 无命中），它们是运行时
    pip 装进 data/site-packages 的。而人格的语音能力就依赖它们。

    所以新壳不能只 `pip install -r requirements.txt`——那样起得来，
    但一说话就 ModuleNotFoundError。

    ## 关键：以 `*.dist-info` 为唯一事实来源

    不要遍历目录去猜包名（会拿到 `PIL` / `Cryptodome` / `_distutils_hack`
    这些导入名，装不上）。要遍历 `*.dist-info`，从 METADATA 里读
    发行版真名与版本。**dist-info 存在 = 这个发行版被装过**，
    这是 pip 自己也认的判据。

    排序用 `name.lower()`：pip 的包名大小写不敏感，但显示要稳定，
    否则每次跑出来的清单顺序都可能不同，diff 就没法看了。
    """
    if not site_packages.exists():
        return []

    found: Dict[str, str] = {}       # lower(name) -> "Name==Version"
    for dist_info in sorted(site_packages.glob("*.dist-info")):
        name = _dist_name_from_metadata(dist_info)
        if not name:
            continue
        # 目录名形如 `pillow-13.0.0.dist-info`
        stem = dist_info.name[: -len(".dist-info")]
        version = ""
        if "-" in stem:
            version = stem.rsplit("-", 1)[1]
        found[name.lower()] = f"{name}=={version}" if version else name

    return [found[k] for k in sorted(found)]
