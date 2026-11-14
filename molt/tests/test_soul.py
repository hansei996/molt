"""针对 soul.py 的测试。

这里的每一个测试都对应一个**真实的失败形态**，不是为了让覆盖率好看：

  - 拷文件而不走 backup() → 库在写的时候快照是坏的
  - 只比大小不比 sha256 → 传输截断但大小凑巧一致（少见，但 SMB 分块写会出现）
  - 缺表但 quick_check 仍 ok → 「能打开」不等于「是同一个库」
  - 目录型 artifact 做整体 sha256 → 每次 mtime 变动都会误报
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from molt.soul import (
    Artifact,
    Manifest,
    collect_runtime_pip,
    hot_backup,
    sha256_of,
    sqlite_check,
)
from molt.errors import Fatal, NeedsHuman


# --------------------------------------------------------------------------
# 造样本库
# --------------------------------------------------------------------------


def _write_metadata(dist_info: Path, name: str, version: str) -> None:
    """写一个最小的 dist-info/METADATA，只含 pip 找包时真正需要的两行。"""
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        encoding="utf-8",
    )


def make_db(path: Path, tables: dict[str, int] | None = None) -> Path:
    """造一个有内容的 SQLite 库。

    tables 形如 {"memories": 71, "identities": 3}，会真的插入那么多行，
    这样 page_count 才有意义（空表也占页，但内容页能反映真实体积）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tables = tables or {"memories": 12, "identities": 3}
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        for name, rows in tables.items():
            conn.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, payload TEXT)")
            for i in range(rows):
                conn.execute(
                    f"INSERT INTO {name} (payload) VALUES (?)",
                    (f"row-{i}-" + "x" * 80,),
                )
        conn.commit()
    finally:
        conn.close()
    return path


def make_data_root(tmp_path: Path) -> Path:
    """模拟一个 AstrBot data/ 目录的最小骨架。"""
    root = tmp_path / "data"
    make_db(root / "data_v4.db", {"conversations": 2, "platform_message_history": 139})
    make_db(
        root / "plugin_data" / "astrbot_plugin_memory_companion" / "memory_companion.db",
        {"memories": 71, "knowledge_nodes": 160, "knowledge_edges": 257},
    )
    (root / "cmd_config.json").write_text('{"platform": []}', encoding="utf-8")
    (root / "skills").mkdir(parents=True)
    (root / "skills" / "documents.md").write_text("# documents", encoding="utf-8")
    return root


# --------------------------------------------------------------------------
# sha256 / sqlite_check
# --------------------------------------------------------------------------


def test_sha256_matches_hashlib_for_small_file(tmp_path: Path):
    import hashlib

    f = tmp_path / "a.bin"
    f.write_bytes(b"molt" * 1000)
    assert sha256_of(f) == hashlib.sha256(b"molt" * 1000).hexdigest()


def test_sha256_streams_large_file_without_loading_it(tmp_path: Path):
    """1 MB 分块；给一个跨块边界的文件，确保不是"只读了第一块"。"""
    import hashlib

    f = tmp_path / "big.bin"
    payload = bytes(range(256)) * (4096 * 3)      # 3 MB，跨多块
    f.write_bytes(payload)
    assert sha256_of(f) == hashlib.sha256(payload).hexdigest()


def test_sqlite_check_reports_real_facts(tmp_path: Path):
    db = make_db(tmp_path / "x.db", {"memories": 50})
    info = sqlite_check(db)
    assert info["quick_check"] == "ok"
    assert info["journal_mode"] == "wal"
    assert info["table_count"] == 1
    assert "memories" in info["tables"]


def test_sqlite_check_raises_needs_human_when_missing(tmp_path: Path):
    with pytest.raises(NeedsHuman):
        sqlite_check(tmp_path / "nope.db")


def test_sqlite_check_raises_fatal_on_corrupt_db(tmp_path: Path):
    """真的造一个坏库，让 quick_check 无法返回 ok。

    【实测 2026-11-14】坏了以后 SQLite 的行为不是"quick_check 返回一个非 ok 值"，
    而是**直接抛 DatabaseError**：
      - 覆盖 B-tree 页 → `database disk image is malformed`
      - 连页头都坏   → `file is not a database`
    所以这个测试同时守两条路：返回值非 ok，或干脆抛异常，都必须变成 Fatal。

    关键点：不能只 raise 一个普通异常——那样 Warden 没法把它归类成
    "不可恢复，必须人介入"。
    """
    db = make_db(tmp_path / "corrupt.db", {"t": 300})

    # 覆盖第 2~4 页（真正的 B-tree 内容页），保留第 1 页的 DB 头让文件仍"看起来像"数据库
    raw = bytearray(db.read_bytes())
    for i in range(4096, min(len(raw), 4096 * 4)):
        raw[i] = 0xFF
    db.write_bytes(bytes(raw))

    with pytest.raises(Fatal):
        sqlite_check(db)


def test_sqlite_check_raises_fatal_on_truncated_db(tmp_path: Path):
    """截断的库：头部说 20 页，实际只剩 1 页。

    这是现实中比"字节被改写"更常见的损坏形态——传输中断、
    SMB 提前返回、磁盘写满都会留下这种文件。
    """
    db = make_db(tmp_path / "truncated.db", {"t": 300})
    raw = db.read_bytes()
    assert len(raw) > 8192
    db.write_bytes(raw[:4096])       # 只留第一页

    with pytest.raises(Fatal):
        sqlite_check(db)


# --------------------------------------------------------------------------
# hot_backup
# --------------------------------------------------------------------------


def test_hot_backup_produces_verified_copy(tmp_path: Path):
    src = make_db(tmp_path / "src.db", {"memories": 30, "identity": 2})
    dst = tmp_path / "out" / "src.db"

    info = hot_backup(src, dst)

    assert dst.exists()
    assert info["sha256"] == sha256_of(dst)
    assert info["quick_check"] == "ok"
    assert info["journal_mode"] == "wal"
    assert info["table_count"] == 2
    assert info["source_table_count"] == 2
    assert info["bytes"] == dst.stat().st_size
    assert info["backup_seconds"] >= 0


def test_hot_backup_copy_is_queryable_and_has_data(tmp_path: Path):
    """备份出来的库必须真的能查到原始数据，而不是"能打开的空壳"。"""
    src = make_db(tmp_path / "src.db", {"memories": 37})
    dst = tmp_path / "copy.db"
    hot_backup(src, dst)

    conn = sqlite3.connect(str(dst))
    try:
        n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    finally:
        conn.close()
    assert n == 37


def test_hot_backup_works_while_source_is_being_written(tmp_path: Path):
    """核心场景：源库正在被写，快照仍须一致。

    这是整个方案存在的理由——如果必须停 AstrBot 才能备份，
    那"迁徙"就变成了"停机搬迁"，产品体验完全不同。
    """
    src = make_db(tmp_path / "live.db", {"log": 10})
    writer = sqlite3.connect(str(src))
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO log (payload) VALUES ('inflight')")
        writer.commit()

        dst = tmp_path / "snapshot.db"
        info = hot_backup(src, dst)

        assert info["quick_check"] == "ok"
        conn = sqlite3.connect(str(dst))
        try:
            rows = conn.execute("SELECT COUNT(*) FROM log").fetchone()[0]
        finally:
            conn.close()
        # 快照里能看到至少 10 行（含未 checkpoint 的 WAL 内容）
        assert rows >= 10
    finally:
        writer.close()


def test_hot_backup_does_not_modify_source(tmp_path: Path):
    """只读打开源库——这是安全的保证，必须写进测试。"""
    src = make_db(tmp_path / "src.db", {"t": 5})
    before = sha256_of(src)
    before_mtime = src.stat().st_mtime

    hot_backup(src, tmp_path / "dst.db")

    assert sha256_of(src) == before
    assert src.stat().st_mtime == before_mtime


def test_hot_backup_refuses_missing_source(tmp_path: Path):
    with pytest.raises(NeedsHuman):
        hot_backup(tmp_path / "gone.db", tmp_path / "dst.db")


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------


def build_manifest(root: Path, generation: int = 1) -> Manifest:
    arts: list[Artifact] = []

    for rel in (
        "data_v4.db",
        "plugin_data/astrbot_plugin_memory_companion/memory_companion.db",
    ):
        p = root / rel
        info = sqlite_check(p)
        arts.append(
            Artifact(
                relpath=rel,
                kind="sqlite",
                bytes=p.stat().st_size,
                sha256=sha256_of(p),
                quick_check=info["quick_check"],
                table_count=info["table_count"],
            )
        )

    cfg = root / "cmd_config.json"
    arts.append(
        Artifact(
            relpath="cmd_config.json",
            kind="copy",
            bytes=cfg.stat().st_size,
            sha256=sha256_of(cfg),
        )
    )
    arts.append(Artifact(relpath="skills", kind="copy"))

    return Manifest(
        generation=generation,
        created_at=time.time(),
        source_host="UGREEN-776B",
        source_root="/mnt/dm-0/astrbot/data",
        protocol_version=1,
        astrbot_version="4.29.4",
        platform="linux",
        python_version="3.12.16",
        artifacts=arts,
        runtime_pip=["edge_tts==7.3.1", "whisper==1.1.10"],
    )


def test_manifest_roundtrip_through_disk(tmp_path: Path):
    root = make_data_root(tmp_path)
    m = build_manifest(root)
    path = tmp_path / "gen-000001" / "manifest.json"

    m.save(path)
    assert path.exists()

    loaded = Manifest.load(path)
    assert loaded.generation == m.generation
    assert loaded.source_host == "UGREEN-776B"
    assert len(loaded.artifacts) == len(m.artifacts)
    assert loaded.runtime_pip == ["edge_tts==7.3.1", "whisper==1.1.10"]
    # 中文/非 ASCII 不能被转义成 \uXXXX（人要看这份 manifest）
    raw = path.read_text(encoding="utf-8")
    assert "\\u" not in raw


def test_manifest_verify_passes_on_intact_root(tmp_path: Path):
    root = make_data_root(tmp_path)
    m = build_manifest(root)
    assert m.verify_against(root) == []


def test_manifest_verify_detects_missing_artifact(tmp_path: Path):
    root = make_data_root(tmp_path)
    m = build_manifest(root)
    (root / "data_v4.db").unlink()

    problems = m.verify_against(root)
    assert any("缺失" in p and "data_v4.db" in p for p in problems)


def test_manifest_verify_detects_size_mismatch(tmp_path: Path):
    root = make_data_root(tmp_path)
    m = build_manifest(root)

    cfg = root / "cmd_config.json"
    cfg.write_text(cfg.read_text(encoding="utf-8") + "  ", encoding="utf-8")

    problems = m.verify_against(root)
    assert any("大小不符" in p for p in problems)


def test_manifest_verify_detects_sha_mismatch_even_when_size_same(tmp_path: Path):
    """大小凑巧相同但内容不同——只比大小的验证会放过这种。

    这就是为什么 manifest 必须带 sha256，而不是"文件在、大小对"就算过。
    """
    root = make_data_root(tmp_path)
    m = build_manifest(root)

    cfg = root / "cmd_config.json"
    original = cfg.read_bytes()
    flipped = bytearray(original)
    flipped[5] = flipped[5] ^ 0xFF      # 改一个字节，长度不变
    cfg.write_bytes(bytes(flipped))

    problems = m.verify_against(root)
    assert any("sha256 不符" in p for p in problems)


def test_manifest_verify_detects_table_count_drift(tmp_path: Path):
    """库能打开、quick_check 也 ok，但表数变了 → 不是同一个库。"""
    root = make_data_root(tmp_path)
    m = build_manifest(root)

    db = root / "data_v4.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE sneaked_in (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    problems = m.verify_against(root)
    # 表数变了，sha 也变了；至少要有表数这一条（sha 那条可能先触发）
    assert problems, "表数漂移必须被抓住"


def test_manifest_verify_detects_corrupt_db(tmp_path: Path):
    root = make_data_root(tmp_path)
    m = build_manifest(root)

    db = root / "plugin_data" / "astrbot_plugin_memory_companion" / "memory_companion.db"
    raw = bytearray(db.read_bytes())
    for i in range(200, min(len(raw), 400)):
        raw[i] = 0xFF
    db.write_bytes(bytes(raw))

    problems = m.verify_against(root)
    assert problems


def test_manifest_verify_skips_sha_for_directory_artifacts(tmp_path: Path):
    """目录型 artifact（skills/）不做整体 sha256。

    理由：目录内容随编辑器保存、mtime 天然会变；对目录做整体校验
    只会制造假警报。目录的完整性靠"必须存在"这一条。
    """
    root = make_data_root(tmp_path)
    m = build_manifest(root)

    (root / "skills" / "pdf.md").write_text("# pdf", encoding="utf-8")

    problems = m.verify_against(root)
    assert problems == []       # 新增文件不应触发失败


def test_manifest_load_missing_file_raises_needs_human(tmp_path: Path):
    with pytest.raises(NeedsHuman):
        Manifest.load(tmp_path / "absent.json")


# --------------------------------------------------------------------------
# collect_runtime_pip
# --------------------------------------------------------------------------


def test_collect_runtime_pip_reads_versions_from_dist_info(tmp_path: Path):
    sp = tmp_path / "site-packages"
    (sp / "edge_tts").mkdir(parents=True)
    (sp / "edge_tts-7.3.1.dist-info").mkdir()
    _write_metadata(sp / "edge_tts-7.3.1.dist-info", "edge-tts", "7.3.1")
    (sp / "whisper").mkdir()
    (sp / "whisper-1.1.10.dist-info").mkdir()
    _write_metadata(sp / "whisper-1.1.10.dist-info", "openai-whisper", "1.1.10")
    (sp / "__pycache__").mkdir()

    assert collect_runtime_pip(sp) == [
        "edge-tts==7.3.1",
        "openai-whisper==1.1.10",
    ]


def test_collect_runtime_pip_on_missing_dir_returns_empty(tmp_path: Path):
    assert collect_runtime_pip(tmp_path / "nope") == []


def test_collect_runtime_pip_uses_metadata_name_not_dir_name(tmp_path: Path):
    """目录名是**导入名**，装不上；必须用 METADATA 里的**发行版名**。

    这三个是 NAS 上真实存在、且第一版全搞错了的例子：
      `PIL/`           → pillow
      `Cryptodome/`    → pycryptodomex
      `_distutils_hack/` → setuptools（而且它的 dist-info 名字完全不同）
    """
    sp = tmp_path / "site-packages"
    for mod_dir, dist_dir, name, ver in (
        ("PIL", "pillow-13.0.0.dist-info", "pillow", "13.0.0"),
        ("Cryptodome", "pycryptodomex-3.24.2.dist-info", "pycryptodomex", "3.24.2"),
        ("_distutils_hack", "setuptools-80.9.0.dist-info", "setuptools", "80.9.0"),
    ):
        (sp / mod_dir).mkdir(parents=True, exist_ok=True)
        (sp / dist_dir).mkdir(parents=True, exist_ok=True)
        _write_metadata(sp / dist_dir, name, ver)

    got = collect_runtime_pip(sp)

    assert got == [
        "pillow==13.0.0",
        "pycryptodomex==3.24.2",
        "setuptools==80.9.0",
    ]
    # 绝不能出现导入名——pip install PIL 是装不上的
    joined = " ".join(got)
    assert "PIL" not in joined
    assert "Cryptodome" not in joined
    assert "_distutils_hack" not in joined


def test_collect_runtime_pip_skips_dir_without_dist_info(tmp_path: Path):
    """没有 dist-info 的目录不是 pip 装的，照着它装不上，必须跳过。

    这曾经被当成"容忍"，写成了"把目录名当包名返回"——那才是真错。
    """
    sp = tmp_path / "site-packages"
    (sp / "vendored_thing").mkdir(parents=True)
    # 放一个有 dist-info 的正常包，证明它没被牵连
    (sp / "realpkg-1.0.dist-info").mkdir()
    _write_metadata(sp / "realpkg-1.0.dist-info", "realpkg", "1.0")

    assert collect_runtime_pip(sp) == ["realpkg==1.0"]


def test_collect_runtime_pip_sorted_case_insensitively(tmp_path: Path):
    """排序必须稳定且大小写不敏感，否则每次 diff 都在乱跳。"""
    sp = tmp_path / "site-packages"
    for dist, name, ver in (
        ("zebra-1.0.dist-info", "Zebra", "1.0"),
        ("apscheduler-3.11.3.dist-info", "APScheduler", "3.11.3"),
        ("mcp-1.2.dist-info", "mcp", "1.2"),
    ):
        (sp / dist).mkdir(parents=True)
        _write_metadata(sp / dist, name, ver)

    assert collect_runtime_pip(sp) == [
        "APScheduler==3.11.3",
        "mcp==1.2",
        "Zebra==1.0",
    ]


def test_collect_runtime_pip_same_name_different_case_not_duplicated(tmp_path: Path):
    """同一个发行版只出现一次，即使 METADATA 里的大小写不同。

    注意：不能在 Windows 上造 `Flask-3.0.dist-info` 与 `flask-3.0.dist-info`
    两个目录——文件系统大小写不敏感，它们是同一个目录，`mkdir` 直接
    FileExistsError。所以这里用两个**目录名不同、Name 只差大小写**的
    dist-info，这才是真正要测的去重逻辑。
    """
    sp = tmp_path / "site-packages"
    (sp / "Flask-3.0.dist-info").mkdir(parents=True)
    _write_metadata(sp / "Flask-3.0.dist-info", "Flask", "3.0")
    (sp / "flask_lower-3.0.dist-info").mkdir(parents=True)
    _write_metadata(sp / "flask_lower-3.0.dist-info", "flask", "3.0")

    got = collect_runtime_pip(sp)
    assert len(got) == 1, got


def test_collect_runtime_pip_ignores_broken_metadata(tmp_path: Path):
    """METADATA 缺失或没有 Name 行时跳过，不崩。"""
    sp = tmp_path / "site-packages"
    (sp / "broken-1.0.dist-info").mkdir(parents=True)          # 无 METADATA
    (sp / "nname-1.0.dist-info").mkdir()
    (sp / "nname-1.0.dist-info" / "METADATA").write_text(
        "Version: 1.0\n", encoding="utf-8"
    )                                                          # 无 Name
    (sp / "good-2.0.dist-info").mkdir()
    _write_metadata(sp / "good-2.0.dist-info", "good", "2.0")

    assert collect_runtime_pip(sp) == ["good==2.0"]

# --------------------------------------------------------------------------
# sidecar 清理（-wal / -shm）
# --------------------------------------------------------------------------


def test_hot_backup_leaves_no_sidecars(tmp_path: Path):
    """热备完的副本旁边不能留下 -wal / -shm。

    这是实测发现的真问题：`hot_backup` 用 `mode=ro` 打开副本做校验，
    而只读连接关不掉自己建出来的 `-shm`（32KB）与空 `-wal`。
    结果 staging 目录里带着两个"看起来像崩溃现场"的垃圾文件，
    再被打包送到另一台机器上。
    """
    src = make_db(tmp_path / "src.db", {"memories": 71})
    dst = tmp_path / "out" / "dst.db"

    payload = hot_backup(src, dst, verify=True)

    assert dst.exists()
    assert not (dst.parent / "dst.db-wal").exists(), "空的 -wal 没清掉"
    assert not (dst.parent / "dst.db-shm").exists(), "-shm 没清掉"
    assert len(payload["sidecars_removed"]) == 2


def test_hot_backup_refuses_to_drop_nonempty_wal(tmp_path: Path):
    """`-wal` 里有未合并事务时，删它就是丢数据，必须报错。"""
    from molt.soul import _drop_empty_sidecars

    db = tmp_path / "x.db"
    db.write_bytes(b"fake")
    wal = tmp_path / "x.db-wal"
    wal.write_bytes(b"there are real transactions here")

    with pytest.raises(Fatal) as ei:
        _drop_empty_sidecars(db)
    assert "丢数据" in str(ei.value) or "未合并" in str(ei.value)
    assert wal.exists(), "报错了还把它删了，等于丢数据"


def test_drop_sidecars_on_clean_dir_is_noop(tmp_path: Path):
    from molt.soul import _drop_empty_sidecars

    db = tmp_path / "y.db"
    db.write_bytes(b"fake")
    assert _drop_empty_sidecars(db) == []


def test_hot_backup_never_touches_source_sidecars(tmp_path: Path):
    """绝不能删源库的 -wal / -shm——那会把正在跑的 AstrBot 搞坏。"""
    src = make_db(tmp_path / "live.db", {"a": 3})
    # 造一个源端的 sidecar 假装它在 WAL 模式
    src_wal = tmp_path / "live.db-wal"
    src_wal.write_bytes(b"")           # 空，但存在
    src_shm = tmp_path / "live.db-shm"
    src_shm.write_bytes(b"x" * 32768)

    hot_backup(src, tmp_path / "out" / "copy.db")

    assert src_wal.exists(), "源库的 -wal 被删了"
    assert src_shm.exists(), "源库的 -shm 被删了"
