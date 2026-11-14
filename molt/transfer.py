"""搬迁的"搬运"这一半：打包、校验、解包、落位。

## 为什么单独一个模块

`migrate` 这个动作要做四件事：热备 → 打包 → 传输 → 登记。前三件都不属于
`Warden`（它只管"谁有资格写"），也不属于 `Soul`（它只管"什么状态必须保真"）。
搬运有自己的一组失败模式，而且每一条都能悄悄毁掉一次迁徙：

  - **打包时漏文件**：tar 的 `--exclude` 写错、或者 glob 没匹配上，
    归档里少一个库，对方照样能 verify 通过（因为 manifest 里本来就没它）
  - **传输截断**：网断、盘满、SMB 提前返回。tar 的退出码 0 只说明
    "它认为自己写完了"，不说明文件完整
  - **解包时路径穿越**：恶意或损坏的归档带 `../../` 或绝对路径，
    解包会把文件写到 staging 之外
  - **落位时覆盖掉本地未合并的 WAL**：目标机上那个库可能正被写，
    `-wal` 里躺着已提交但未合并的事务，直接拷贝会丢数据

这四条都要独立的检查，所以给它们自己的模块和自己的测试。

## 为什么打包后要重新读一遍整个归档算 sha256

tar 返回 0 不代表完整。磁盘写满时 tar 会在 stderr 抱怨但（某些版本）
仍以 0 退出；网络文件系统上写入可能被静默截断。所以：

    打包 → 重读整个归档算 sha256 → 记进返回值
                              ↓
    解包前 → 再算一遍 → 和调用方给的 sha256 比对 → 不符就拒绝

**这是唯一能发现截断的地方。** 只信 tar 的退出码是本项目已经踩过的
那一类"看起来对就交付"的错误。
"""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

from .errors import Fatal, NeedsHuman
from .soul import sha256_of

#: 归档里不该出现的东西。`__pycache__` 是上一个模块的教训：把 .pyc 传到
#: 另一台机器，python 会因为字节码版本/路径不匹配而忽略它，但归档会变大。
DEFAULT_EXCLUDES = ("__pycache__", ".pytest_cache", ".git", "*.pyc", "*.pyo")

#: 明确不允许出现在迁徙归档里的文件类型。理由见 `_check_members`。
_FORBIDDEN_SUFFIX = (".lnk",)


def _sidecar_noise(p: Path) -> Optional[str]:
    """判断 p 是不是 WAL 的 sidecar 噪音；是就说明原因，不是返回 None。

    这条规则是实测撞出来的：`molt land` 在 Windows 上落位后，目标 data/
    里凭空多出了 `data_v4.db-shm`（32768 B）和 `data_v4.db-wal`（0 B），
    而归档里明明只有 4 个文件。

    根因不是落位逻辑，而是**校验本身**：`molt verify` 用只读连接打开
    gen_dir 里的库做 quick_check，任何打开 wal 模式库的连接都会建出
    `-shm`/`-wal`，只读连接关闭时删不掉。于是 sidecar 落进 gen_dir，
    被当成快照内容一起搬走。

    危害不是"多两个文件"，而是：**把 `-shm` 送到另一台机器，等于人为
    制造一个"看起来像崩溃现场"的状态。** 所以：

      - `-shm` 永远是噪音，任何大小都丢掉
      - `-wal` 为空 → 噪音（没有未合并事务）
      - `-wal` 非空 → **不是噪音**，这份快照不是自包含的，必须报错
    """
    name = p.name
    if name.endswith("-shm"):
        return "共享内存索引文件，只对生成它的那个进程有意义"
    if name.endswith("-wal"):
        try:
            size = p.stat().st_size
        except OSError:
            return None
        if size == 0:
            return "空的 WAL，没有未合并的事务"
        raise Fatal(
            f"快照里的 {name} 有 {size} 字节未合并的事务：{p}\n"
            "  这份快照不是自包含的——只拷主文件会丢掉这些已提交的写入。\n"
            "  正确做法：在源端让 AstrBot 正常停机（SIGINT）让 SQLite"
            "自己 checkpoint，然后重新 checkout。"
        )
    return None


def _iter_payload_files(src: Path) -> List[Path]:
    """列出要打包的文件，去掉缓存目录与 WAL sidecar 噪音。顺序固定，便于测试与 diff。"""
    out: List[Path] = []
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        parts = set(rel.parts)
        if parts & {"__pycache__", ".pytest_cache", ".git"}:
            continue
        if p.suffix in (".pyc", ".pyo"):
            continue
        if _sidecar_noise(p) is not None:
            continue
        out.append(p)
    return out


def make_archive(
    src_dir: Path,
    out_path: Path,
    *,
    excludes: tuple = DEFAULT_EXCLUDES,
) -> Dict[str, Any]:
    """把 generation 目录打成一个 .tar.gz，并返回它的指纹。

    返回 `{path, bytes, sha256, file_count, files}`。

    **不声称可复现**：tar 会记文件 mtime，同一份数据在不同时间打包会有
    不同的 sha256。指纹的用途是"这次传输有没有被截断"，不是"两次打包
    是否相同"。想做后者要把 mtime 归一化，那是另一个需求。
    """
    src_dir = Path(src_dir)
    out_path = Path(out_path)
    if not src_dir.is_dir():
        raise Fatal(f"要打包的目录不存在：{src_dir}")

    files = _iter_payload_files(src_dir)
    if not files:
        raise Fatal(f"{src_dir} 里没有可打包的文件——空归档不算一次迁徙")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # --- 先写 `.part`，验完才改名 ---
    #
    # 直接写最终文件名的话，进程在 `tf.add` 中间被杀掉（断电、盘满、SIGKILL），
    # 磁盘上就留下一个**截断的 `gen-000012.tar.gz`**。它为什么会骗人：
    #
    #   - `list_archives()` 用 `glob("gen-*.tar.gz")` 枚举，它会被列出来
    #   - `tarfile.open()` 对截断的 gzip 往往能读出前面几个成员，不报错
    #   - 名字里带着代次，看起来"就是那一代"
    #
    # `.part` 和 `out_path` 必须同目录：`os.replace` 只有在同一个文件系统内
    # 才是原子的，跨文件系统会退化成"拷贝 + 删除"——那就又回到中间态了。
    part_path = out_path.with_name(out_path.name + ".part")
    try:
        with tarfile.open(part_path, "w:gz", compresslevel=6) as tf:
            for p in files:
                rel = p.relative_to(src_dir).as_posix()
                tf.add(str(p), arcname=rel, recursive=False)

        # 立刻重读整个归档算指纹——这是发现截断的唯一手段。
        digest = sha256_of(part_path)
        size = part_path.stat().st_size

        # --- 再验一遍：回读归档，逐个核对成员数与字节数 ---
        #
        # 一开始这里写的是"归档小于 1KB 就报错"，那是个坏检查：它把
        # "内容真的很大"当成了"归档是对的"，而一个 100MB 的截断归档照样
        # 能通过。真正的判据是**我写进去的东西能不能原样读出来**。
        try:
            with tarfile.open(part_path, "r:gz") as tf:
                got = {m.name: m.size for m in tf.getmembers() if m.isfile()}
        except tarfile.TarError as e:
            raise Fatal(
                f"刚写出的归档已经读不开了（多半是写到一半失败或盘满）：{out_path}\n  {e}"
            ) from e

        expected = {p.relative_to(src_dir).as_posix(): p.stat().st_size for p in files}
        if got != expected:
            missing = sorted(set(expected) - set(got))
            extra = sorted(set(got) - set(expected))
            wrong = sorted(
                n for n in set(expected) & set(got) if expected[n] != got[n]
            )
            raise Fatal(
                "归档内容与源目录不一致，拒绝继续。\n"
                f"  缺失 {missing[:5]}{' …' if len(missing) > 5 else ''}\n"
                f"  多出 {extra[:5]}{' …' if len(extra) > 5 else ''}\n"
                f"  大小不符 {wrong[:5]}{' …' if len(wrong) > 5 else ''}"
            )

        os.replace(part_path, out_path)
    finally:
        # 成功时 `.part` 已经被改名，这里什么都不会发生；失败时必须清掉，
        # 否则下一轮会看到"一个上一轮留下的半份文件"，而它看起来也像归档。
        if part_path.exists():
            try:
                part_path.unlink()
            except OSError:  # pragma: no cover - 清理失败不该盖住真正的错误
                pass

    return {
        "path": str(out_path),
        "bytes": size,
        "sha256": digest,
        "file_count": len(files),
        "files": [p.relative_to(src_dir).as_posix() for p in files],
    }


def _check_members(tf: tarfile.TarFile, dest_dir: Path) -> None:
    """拒绝任何会写到 dest_dir 之外、或不是普通文件的成员。

    迁移归档里只该有普通文件。符号链接、硬链接、设备文件在这里没有任何
    正当用途，而它们正是路径穿越的载体（链接指向 /etc 再写进去）。
    所以一律拒绝，不做"小心处理"。
    """
    dest_resolved = dest_dir.resolve()
    for m in tf.getmembers():
        raw = m.name.replace("\\", "/")

        if m.issym() or m.islnk():
            raise Fatal(f"归档里有链接，拒绝解包：{raw}")
        if not (m.isfile() or m.isdir()):
            raise Fatal(f"归档里有非普通文件，拒绝解包：{raw}")
        if raw.lower().endswith(_FORBIDDEN_SUFFIX):
            raise Fatal(f"归档里有 Windows 快捷方式，拒绝解包：{raw}")

        # 绝对路径 / 盘符 / .. 逃逸
        if raw.startswith("/"):
            raise Fatal(f"归档里有绝对路径，拒绝解包：{raw}")
        if len(raw) > 1 and raw[1] == ":":
            raise Fatal(f"归档里有盘符路径，拒绝解包：{raw}")
        parts = PurePosixPath(raw).parts
        if ".." in parts:
            raise Fatal(f"归档里有上跳路径，拒绝解包：{raw}")

        target = (dest_resolved / raw).resolve()
        try:
            target.relative_to(dest_resolved)
        except ValueError:
            raise Fatal(f"归档成员会写到目录之外，拒绝解包：{raw}") from None


def extract_archive(
    archive_path: Path,
    dest_dir: Path,
    *,
    expected_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    """解包，并在解包**之前**校验整个归档的 sha256。

    返回 `{files, bytes, sha256}`。

    校验顺序刻意是"先算指纹再看内容"：如果字节都不对，里面的 tar 结构
    也可能被截断成看似合法的样子，此时逐个检查成员是在给错误的东西
    做体检，得到的结论没有意义。
    """
    archive_path = Path(archive_path)
    dest_dir = Path(dest_dir)

    if not archive_path.is_file():
        raise NeedsHuman(f"归档不存在：{archive_path}")

    digest = sha256_of(archive_path)
    if expected_sha256:
        expected = expected_sha256.strip().lower()
        if digest.lower() != expected:
            raise Fatal(
                "归档指纹不符——这次传输被截断或改动了。\n"
                f"  期望 {expected}\n"
                f"  实际 {digest}\n"
                "  不要重试解包，先重新传一遍。"
            )

    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        with tarfile.open(archive_path, "r:gz") as tf:
            _check_members(tf, dest_dir)
            # 3.12+ 的 filter 参数是安全解包的官方手段；3.10 没有，
            # 所以上面先自己做了一遍成员检查，两条路都留着。
            try:
                tf.extractall(dest_dir, filter="data")
            except TypeError:
                tf.extractall(dest_dir)
            names = [m.name for m in tf.getmembers() if m.isfile()]
    except tarfile.TarError as e:
        raise Fatal(f"归档损坏，无法解开：{e}") from e

    total = 0
    for name in names:
        p = dest_dir / PurePosixPath(name)
        if p.is_file():
            total += p.stat().st_size

    return {"files": names, "bytes": total, "sha256": digest}


# --------------------------------------------------------------------------
# 落位：把快照搬进目标壳的 data 目录
# --------------------------------------------------------------------------


def _sidecar_of(db: Path) -> List[Path]:
    """返回这个库可能存在的 -wal / -shm。"""
    return [Path(str(db) + "-wal"), Path(str(db) + "-shm")]


RESTORE_MARKER_NAME = ".molt-restore-incomplete"


def incomplete_restore(data_dir: Path) -> Optional[Dict[str, Any]]:
    """目标 data 目录里有没有"上一次落位没做完"的标记。

    读不到（不存在）返回 `None`。标记存在但内容坏了，返回一个带
    `{"broken": True}` 的字典 —— **读不出来的标记同样要当成"没做完"**：
    那说明上一次正好在写标记的时候断电了，而那个时刻本来就在中段。
    """
    marker = Path(data_dir) / RESTORE_MARKER_NAME
    if not marker.exists():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"broken": True}
    if not isinstance(payload, dict):
        return {"broken": True}
    return payload


def _write_restore_marker(data_dir: Path, gen_dir: Path, payload: Dict[str, Any]) -> None:
    marker = data_dir / RESTORE_MARKER_NAME
    body = {
        "gen_dir": str(gen_dir),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "generation": payload.get("generation"),
    }
    tmp = marker.with_name(marker.name + ".tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, marker)


def restore_into(
    gen_dir: Path,
    target_data_dir: Path,
    *,
    backup: bool = True,
) -> Dict[str, Any]:
    """把 generation 目录里的工件搬到目标壳的 data 目录里。

    返回 `{restored, backup_dir, skipped, sidecars_cleaned}`。

    ## 为什么这件事必须这么小心

    目标目录里可能已经有一个**活着的**库。此时直接覆盖 `data_v4.db` 会：

      - 丢掉那个库里已被写但未合并进主文件的事务（在它的 `-wal` 里）
      - 留下一个指向旧主文件的 `-shm`，导致下一次打开时读到一个
        自认为有 read-mark、实际主文件已换掉的索引

    第二条最难查：它不报错，它让库"看起来能用"然后某天烂掉。所以：

      1. 目标库的 `-wal` **非空** → 拒绝落位（有未合并事务，覆盖就是丢数据）
      2. 目标库的 `-wal` 为空、`-shm` 存在 → 一起移进备份目录
      3. 原有文件全部移到 `.molt-pre-restore-<stamp>/`，不删除

    ## 为什么每个文件都要先写 `.molt-part`

    直接 `shutil.copy2` 到 `dst`，中途断电会在目标 `data/` 里留下一个
    **截断的 `data_v4.db`**：文件名对、位置对、`stat().st_size > 0`。
    只有 SQLite 真去打开它，才会报 `file is not a database`。

    所以：写 `<dst>.molt-part` → `os.replace` 就位（同一目录内原子）。
    再加一个 `.molt-restore-incomplete` 标记：落位开始前写、全部完成后删。
    **标记只有被人读到才算数** —— `tools/molt-agent.py` 与
    `tools/molt-homer.py` 在启动 AstrBot 之前都查它。

    `backup=False` 只给测试用：生产路径上永远要留一份原样。
    """
    gen_dir = Path(gen_dir)
    target_data_dir = Path(target_data_dir)

    if not gen_dir.is_dir():
        raise NeedsHuman(f"快照目录不存在：{gen_dir}")
    if not target_data_dir.is_dir():
        raise NeedsHuman(
            f"目标 data 目录不存在：{target_data_dir}\n"
            "  这个目录必须是 AstrBot 将要使用的 data/，不是随便一个空目录。"
        )

    # --- 先查有没有未合并的 WAL，有就立刻拒绝 ---
    for db_name in ("data_v4.db", "plugin_data/astrbot_plugin_memory_companion/memory_companion.db"):
        live_db = target_data_dir / db_name
        if not live_db.exists():
            continue
        for side in _sidecar_of(live_db):
            if side.name.endswith("-wal") and side.exists() and side.stat().st_size > 0:
                raise NeedsHuman(
                    f"目标库有 {side.stat().st_size} 字节未合并的事务：{side}\n"
                    "  现在覆盖它 = 丢掉这些已提交的写入。\n"
                    "  正确做法：先让那个 AstrBot 正常停机（SIGINT），"
                    "让 SQLite 自己 checkpoint，再重新落位。"
                )

    # --- 备份目录 ---
    import time as _time

    stamp = _time.strftime("%Y%m%dT%H%M%S")
    backup_dir = target_data_dir / f".molt-pre-restore-{stamp}"
    restored: List[str] = []
    skipped: List[str] = []
    parts: List[Path] = []

    _write_restore_marker(target_data_dir, gen_dir, {})

    try:
        payload = _iter_payload_files(gen_dir)
        for src_file in payload:
            rel = src_file.relative_to(gen_dir)
            if rel.as_posix() == "manifest.json":
                # manifest 描述的是"这份快照从哪来"，不该进目标机的 data/。
                # 它留在 generation 目录里，由 Warden 的账本引用。
                skipped.append(rel.as_posix())
                continue

            dst = target_data_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)

            if dst.exists():
                if backup:
                    bak = backup_dir / rel
                    bak.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dst), str(bak))
                else:
                    dst.unlink()

            # 顺手清掉目标处的空 WAL / 陈旧 SHM，避免新旧混在一起
            for side in _sidecar_of(dst):
                if side.exists():
                    if backup:
                        bak = backup_dir / side.name
                        bak.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(side), str(bak))
                    else:
                        side.unlink()

            # 先写 .molt-part 再改名：中途断电只会留下一个明显的半成品，
            # 而不是一个"看起来正常的库"。
            part = dst.with_name(dst.name + ".molt-part")
            parts.append(part)
            shutil.copy2(src_file, part)
            os.replace(part, dst)
            restored.append(rel.as_posix())
    finally:
        for part in parts:
            if part.exists():
                try:
                    part.unlink()
                except OSError:  # pragma: no cover
                    pass

    # 落位完再收一次尾。
    #
    # 上面逐个文件清 sidecar 只能处理"目标处原有"的，处理不了"搬过来之后
    # 又被谁建出来"的。实测撞到的那次是：`molt verify` 在 gen_dir 里做
    # quick_check，只读连接给每个 wal 库建出了 `-shm`/空 `-wal`，它们随后
    # 被当成快照内容复制到了目标 data/。现在这类噪音在 `_iter_payload_files`
    # 里就被拦掉了，但落位是最后一环，宁可再确认一次：
    # **落位结束后，目标 data/ 里不该有任何刚落下的 sidecar。**
    leftover: List[str] = []
    for db_name in ("data_v4.db", "plugin_data/astrbot_plugin_memory_companion/memory_companion.db"):
        main_db = target_data_dir / db_name
        if not main_db.exists():
            continue
        for side in _sidecar_of(main_db):
            if not side.exists():
                continue
            if side.name.endswith("-wal") and side.stat().st_size > 0:
                # 非空 WAL 出现在这里只有一个解释：copy2 之后有别的进程
                # 打开过这个库并写了东西。那不是我们能决定的事，报出来。
                raise NeedsHuman(
                    f"落位后目标库出现了 {side.stat().st_size} 字节的 WAL：{side}\n"
                    "  这不是 molt 写的。检查是否有别的进程正在用这个 data 目录。"
                )
            leftover.append(side.name)
            side.unlink()

    # 走到这里说明所有文件都已经就位 —— 标记才可以删。
    marker = target_data_dir / RESTORE_MARKER_NAME
    if marker.exists():
        try:
            marker.unlink()
        except OSError:  # pragma: no cover
            pass

    return {
        "restored": restored,
        "skipped": skipped,
        "backup_dir": str(backup_dir) if backup_dir.exists() else "",
        "sidecars_cleaned": leftover,
    }
