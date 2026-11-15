"""搬家到底带走了哪些"记忆"。

## 这个文件存在的理由

`molt/soul.py` 里那张 `COPY_ARTIFACTS` 曾经是个**死清单**：它写着要把
`plugin_data` 带走，而真正的快照代码只抓 `data_v4.db` + `memory_companion.db`
+ `cmd_config.json`。于是 `astrbot_plugin_private_companion`（记忆核心）的
`companions.json` / `companion-v2.sqlite3` 每次都留在原地，文档却说它会跟着走。

这里钉住三件事：

1. 默认会把 `MEMORY_PLUGIN_DATA` 里的目录带走，且字节一致；
2. 里面的 sqlite 走 `backup()`（新壳拿到的是一份可打开的库，而不是半个 WAL）；
3. 缓存目录（`meme_manager`）、`-wal`、`.bak-*` 不会被搬走。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import List

import pytest

from molt.cli import main
from molt.soul import MAX_MEMORY_DIR_BYTES


def _make_sqlite(path: Path, rows: int = 0, *, journal: str = "WAL") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(f"PRAGMA journal_mode={journal}")
        conn.execute("CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY, body TEXT)")
        if rows:
            conn.executemany(
                "INSERT INTO memories (body) VALUES (?)",
                [(f"第 {i} 条记忆",) for i in range(rows)],
            )
        conn.commit()
    finally:
        conn.close()


def _make_source_data(base: Path, *, extra_plugins: List[str] = ()) -> Path:
    data = base / "source" / "data"
    _make_sqlite(data / "data_v4.db", rows=2)
    _make_sqlite(
        data / "plugin_data" / "astrbot_plugin_memory_companion" / "memory_companion.db",
        rows=1,
    )
    (data / "cmd_config.json").write_text('{"config_version": 5}', encoding="utf-8-sig")

    pc = data / "plugin_data" / "astrbot_plugin_private_companion"
    pc.mkdir(parents=True, exist_ok=True)
    (pc / "companions.json").write_text('{"companions": [{"name": "大肥鱼"}]}', encoding="utf-8")
    _make_sqlite(pc / "companion-7" / "installations" / "main" / "runtime" / "companion-v2.sqlite3", rows=3)
    # 这些都不该被搬走
    (pc / "companions.json.bak-20260101T000000").write_text("旧备份", encoding="utf-8")
    (pc / "companions.json.molt-part").write_text("半截", encoding="utf-8")
    (pc / "companion-7" / "installations" / "main" / "runtime" / "companion-v2.sqlite3-wal").write_bytes(b"x")
    # 纯缓存目录
    (data / "plugin_data" / "meme_manager" / "cache.bin").parent.mkdir(parents=True, exist_ok=True)
    (data / "plugin_data" / "meme_manager" / "cache.bin").write_bytes(b"\x00" * 32)

    for name in extra_plugins:
        d = data / "plugin_data" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text('{"plugin": "%s"}' % name, encoding="utf-8")
    return data


def _migrate(data: Path, root: Path, *extra: str) -> int:
    return main(
        [
            "migrate",
            # 这些用例验的不是「目标在不在」那道闸门（见 test_migrate_wakeable.py）。
            "--skip-target-check",
            "--data",
            str(data),
            "--shell",
            "shell-a",
            "--root",
            str(root),
            "--target",
            "shell-b",
            "--no-color",
            *extra,
        ]
    )


def _gen_dir(root: Path, n: int = 1) -> Path:
    # 产出目录是 `staging/gen-NNNNNN`（不是 `generations/`——那是账本那边的归档目录）
    return root / "staging" / f"gen-{n:06d}"


def test_migrate_carries_the_memory_plugin_data(tmp_path: Path) -> None:
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"
    assert _migrate(data, root) == 0

    gen = _gen_dir(root)
    moved = gen / "plugin_data" / "astrbot_plugin_private_companion" / "companions.json"
    assert moved.is_file(), "记忆文件没被带走 —— 这就是这张表以前骗人的地方"
    assert moved.read_bytes() == (
        data / "plugin_data" / "astrbot_plugin_private_companion" / "companions.json"
    ).read_bytes()


def test_the_manifest_lists_every_memory_file(tmp_path: Path) -> None:
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"
    assert _migrate(data, root) == 0

    manifest = json.loads((_gen_dir(root) / "manifest.json").read_text(encoding="utf-8"))
    relpaths = {a["relpath"] for a in manifest["artifacts"]}
    assert "plugin_data/astrbot_plugin_private_companion/companions.json" in relpaths
    assert (
        "plugin_data/astrbot_plugin_private_companion/companion-7/"
        "installations/main/runtime/companion-v2.sqlite3" in relpaths
    )


def test_sqlite_inside_a_memory_dir_is_backed_up_not_copied(tmp_path: Path) -> None:
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"
    assert _migrate(data, root) == 0

    got = (
        _gen_dir(root)
        / "plugin_data"
        / "astrbot_plugin_private_companion"
        / "companion-7"
        / "installations"
        / "main"
        / "runtime"
        / "companion-v2.sqlite3"
    )
    assert got.is_file()
    assert not (got.parent / "companion-v2.sqlite3-wal").exists()
    assert not (got.parent / "companion-v2.sqlite3-shm").exists()

    conn = sqlite3.connect(str(got))
    try:
        assert conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 3
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        conn.close()

    manifest = json.loads((_gen_dir(root) / "manifest.json").read_text(encoding="utf-8"))
    entry = next(
        a for a in manifest["artifacts"] if a["relpath"].endswith("companion-v2.sqlite3")
    )
    assert entry["kind"] == "sqlite"
    assert entry["quick_check"] == "ok"


def test_backups_wal_and_partial_files_are_left_behind(tmp_path: Path) -> None:
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"
    assert _migrate(data, root) == 0

    pc = _gen_dir(root) / "plugin_data" / "astrbot_plugin_private_companion"
    names = {p.name for p in pc.rglob("*") if p.is_file()}
    assert not any(n.endswith(("-wal", "-shm", ".molt-part")) for n in names)
    assert not any(".bak-" in n for n in names)


def test_cache_directories_are_not_carried(tmp_path: Path) -> None:
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"
    assert _migrate(data, root) == 0
    assert not (_gen_dir(root) / "plugin_data" / "meme_manager").exists()


def test_extra_plugin_data_is_only_carried_when_asked(tmp_path: Path) -> None:
    data = _make_source_data(tmp_path, extra_plugins=["astrbot_plugin_notes"])
    root = tmp_path / "warden"
    assert _migrate(data, root) == 0
    assert not (_gen_dir(root) / "plugin_data" / "astrbot_plugin_notes").exists()

    root2 = tmp_path / "warden2"
    assert _migrate(data, root2, "--with-plugin-data", "astrbot_plugin_notes") == 0
    assert (_gen_dir(root2) / "plugin_data" / "astrbot_plugin_notes" / "state.json").is_file()


def test_an_oversized_memory_dir_fails_cleanly(tmp_path: Path, monkeypatch, capsys) -> None:
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"
    monkeypatch.setattr("molt.soul.MAX_MEMORY_DIR_BYTES", 64)

    rc = _migrate(data, root)
    assert rc != 0
    out = capsys.readouterr().out
    assert "上限" in out
    # 失败必须是"什么都没搬"，而不是搬了一半
    assert not (_gen_dir(root) / "plugin_data" / "astrbot_plugin_private_companion").exists()


def test_checkout_carries_the_same_things_as_migrate(tmp_path: Path) -> None:
    """`checkout` 曾经是第二份实现，于是同一台机器两条命令搬走的东西不一样。"""
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"
    rc = main(
        [
            "checkout",
            "--data",
            str(data),
            "--shell",
            "shell-a",
            "--root",
            str(root),
            "--no-color",
        ]
    )
    assert rc == 0
    assert (
        _gen_dir(root) / "plugin_data" / "astrbot_plugin_private_companion" / "companions.json"
    ).is_file()


def test_the_limit_constant_is_a_real_number() -> None:
    """防止有人把它改成 0 或 None —— 那时所有记忆目录都会"超限"。"""
    assert isinstance(MAX_MEMORY_DIR_BYTES, int)
    assert MAX_MEMORY_DIR_BYTES >= 64 * 1024 * 1024
