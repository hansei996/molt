"""`molt backup` / `molt backups` / `molt recover` 三条命令的行为测试。

## 这个文件在守什么

`test_backup.py` 验的是 Warden 那一层（存、列、剪、HTTP）。这里验的是
**人在终端里敲下去会发生什么**，而命令行这一层有三处特别容易骗自己：

  1. **`--dry-run` 真的没上传吗** —— 只看"打印了 dry-run"是不够的，
     必须去看 `backups/` 里到底有没有多出东西。
  2. **`backup` 会不会偷偷动账本** —— 它必须完全不进 `generations`。
     这条如果坏了，壳侧的 `decide()` 会开始把备份当成落位任务。
  3. **`recover` 是真的走 `land` 吗** —— 它可以有一百种实现方式，但
     只要它自己重新实现一遍落位，就等于给"最危险的一次操作"开了第
     二条路径。所以这里的断言是**`cmd_land` 被调到了，且拿到的参数
     是整理好的形状**，而不是"文件最后出现了"。

## 为什么用 `main([...])` 而不是直接调 `cmd_backup`

`args` 的形状由 parser 定义。手搓一个 Namespace 会和真实调用漂移
（漏一个 `default=SUPPRESS` 的参数就会在真机上炸）。走 `main()` 还能
顺带测到子命令注册本身。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from molt import cli
from molt.cli import main
from molt.soul import sha256_of
from molt.warden import Warden


# --------------------------------------------------------------------------
# 夹具：一个像样的 data 目录
# --------------------------------------------------------------------------


def _make_sqlite(path: Path, *, rows: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY, title TEXT)")
        conn.executemany(
            "INSERT INTO conversations (title) VALUES (?)",
            [(f"第 {i} 条",) for i in range(rows)],
        )
        conn.commit()
    finally:
        conn.close()


def _make_data(base: Path) -> Path:
    data = base / "data"
    _make_sqlite(data / "data_v4.db", rows=3)
    _make_sqlite(
        data / "plugin_data" / "astrbot_plugin_memory_companion" / "memory_companion.db",
        rows=2,
    )
    (data / "cmd_config.json").write_text('{"config_version": 5}', encoding="utf-8-sig")
    return data


def _backup_files(root: Path) -> list:
    d = root / "backups"
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.glob("backup-*.tar.gz"))


def _run(argv: list) -> int:
    return main([*argv, "--no-color"])


# --------------------------------------------------------------------------
# backup
# --------------------------------------------------------------------------


def test_backup_uploads_and_touches_no_ledger(tmp_path: Path, capsys):
    data = _make_data(tmp_path)
    root = tmp_path / "warden"

    rc = _run(["backup", "--data", str(data), "--root", str(root), "--shell", "shell-a"])

    assert rc == 0
    names = _backup_files(root)
    assert len(names) == 1, "应该正好收下一份备份"
    assert "shell-a" in names[0], "名字里应该带上壳名，人才认得出来"

    # 账本必须一动不动。用 Warden 自己的 status() 看，而不是去读 state.json ——
    # 备份这条路压根不该写账本，所以那个文件可能**根本没被创建过**，"文件不存在"
    # 本身就是一个正确答案，不该被当成测试失败。
    st = Warden(root=root).status()
    assert st["generation_count"] == 0
    assert st["latest_generation"] is None
    assert st["current_epoch"] == 0

    out = capsys.readouterr().out
    assert "备份不是交接" in out, "要显式告诉用户它没进迁徙历史"


def test_backup_dry_run_uploads_nothing(tmp_path: Path, capsys):
    data = _make_data(tmp_path)
    root = tmp_path / "warden"

    rc = _run(
        ["backup", "--data", str(data), "--root", str(root), "--shell", "shell-a", "--dry-run"]
    )

    assert rc == 0
    assert _backup_files(root) == [], "--dry-run 不该往 Warden 上留下任何东西"
    assert "--dry-run" in capsys.readouterr().out


def test_backup_refuses_a_missing_data_dir(tmp_path: Path, capsys):
    rc = _run(
        ["backup", "--data", str(tmp_path / "nope"), "--root", str(tmp_path / "w")]
    )
    assert rc == 2
    assert "不存在" in capsys.readouterr().out


def test_backup_snapshot_matches_the_source(tmp_path: Path):
    """备份里的库要能被还原成一样的对话（不要求字节相同）。"""
    import tarfile

    data = _make_data(tmp_path)
    root = tmp_path / "warden"
    assert _run(["backup", "--data", str(data), "--root", str(root), "--shell", "s"]) == 0

    name = _backup_files(root)[0]
    out_dir = tmp_path / "unpacked"
    with tarfile.open(root / "backups" / name) as tf:
        # `filter` 参数 3.12 才有。CI 的 py3.10 是 3.10.11，没有它 ——
        # 产品代码 `molt/transfer.py` 早就两条路都留着，这里漏了。
        try:
            tf.extractall(out_dir, filter="data")
        except TypeError:
            tf.extractall(out_dir)

    conn = sqlite3.connect(str(out_dir / "data_v4.db"))
    try:
        rows = [r[0] for r in conn.execute("SELECT title FROM conversations ORDER BY id")]
    finally:
        conn.close()
    assert rows == ["第 0 条", "第 1 条", "第 2 条"]


# --------------------------------------------------------------------------
# backups
# --------------------------------------------------------------------------


def test_backup_tells_the_warden_what_fingerprint_to_expect(tmp_path: Path, monkeypatch):
    """上传时必须把**我们算出来的**指纹一起发过去。

    端到端看不出这件事：服务端自己也会算一遍，对不上照样 400。但它决定了
    **哪一侧是判据** —— 让服务端去当唯一裁判，等于放弃了"我本地打的包
    和我以为的一不一样"这个问题，而那是传输损坏最早能被发现的地方。
    """
    data = _make_data(tmp_path)
    root = tmp_path / "warden"
    seen = {}

    class _FakeBackend:
        def status(self):
            return {"latest_generation": None}

        def upload_backup(self, path, **kwargs):
            seen["path"] = Path(path)
            seen["kwargs"] = kwargs
            return {"name": "backup-x.tar.gz", "bytes": Path(path).stat().st_size}

    monkeypatch.setattr(cli, "_backend", lambda args: _FakeBackend())

    rc = _run(["backup", "--data", str(data), "--root", str(root), "--shell", "shell-a"])

    assert rc == 0
    assert seen["kwargs"]["shell"] == "shell-a"
    assert seen["kwargs"]["generation"] == 0
    expected = seen["kwargs"]["expected_sha256"]
    # 归档上传成功后会被删掉（临时目录不许留垃圾），所以指纹只能核"形状"，
    # 真正的"字节对不对"由服务端在收包时复核（test_the_ledger_refuses_a_tampered_bundle）。
    assert len(expected) == 64 and all(c in "0123456789abcdef" for c in expected)
    assert not Path(seen["path"]).exists(), "上传成功之后归档应该已经删了"


def test_backups_lists_what_backup_stored(tmp_path: Path, capsys):
    data = _make_data(tmp_path)
    root = tmp_path / "warden"
    assert _run(["backup", "--data", str(data), "--root", str(root), "--shell", "s"]) == 0
    capsys.readouterr()

    rc = _run(["backups", "--root", str(root)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "backup-" in out
    assert "1 份" in out
    assert "molt recover" in out, "列表要给出去恢复的路"


def test_backups_on_empty_root_says_so(tmp_path: Path, capsys):
    rc = _run(["backups", "--root", str(tmp_path / "warden")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "一份都没有" in out
    assert "backup_interval" in out, "空的时候要告诉人怎么让它自己产生"


# --------------------------------------------------------------------------
# recover
# --------------------------------------------------------------------------


def test_recover_downloads_then_hands_over_to_land(tmp_path: Path, monkeypatch, capsys):
    """`recover` 的契约：取下来、核对指纹、然后把活交给 `cmd_land`。"""
    data = _make_data(tmp_path)
    root = tmp_path / "warden"
    assert _run(["backup", "--data", str(data), "--root", str(root), "--shell", "s"]) == 0
    name = _backup_files(root)[0]
    expected = sha256_of(root / "backups" / name)
    capsys.readouterr()

    seen = {}

    def fake_land(ns):
        seen["archive"] = ns.archive
        seen["sha256"] = ns.sha256
        seen["generation"] = ns.generation
        seen["data"] = ns.data
        seen["archive_sha"] = sha256_of(Path(ns.archive))
        return 0

    monkeypatch.setattr(cli, "cmd_land", fake_land)

    target = tmp_path / "target" / "data"
    rc = _run(
        [
            "recover",
            "--backup",
            name,
            "--root",
            str(root),
            "--data",
            str(target),
            "--yes",
        ]
    )

    assert rc == 0
    assert seen["sha256"] == expected, "指纹必须来自下载结果，而不是用户嘴上说的"
    assert seen["archive_sha"] == expected, "交出去的归档必须就是那份备份"
    assert seen["generation"] is None, "备份没有代，必须显式置空而不是瞎猜"
    assert seen["data"] == str(target)


def test_recover_leaves_original_backup_in_place(tmp_path: Path, monkeypatch):
    data = _make_data(tmp_path)
    root = tmp_path / "warden"
    assert _run(["backup", "--data", str(data), "--root", str(root), "--shell", "s"]) == 0
    name = _backup_files(root)[0]

    monkeypatch.setattr(cli, "cmd_land", lambda ns: 0)
    assert _run(["recover", "--backup", name, "--root", str(root), "--data", str(tmp_path / "t")]) == 0

    assert _backup_files(root) == [name], "恢复不该把备份本身消耗掉"


def test_recover_refuses_an_unknown_name(tmp_path: Path, capsys):
    root = tmp_path / "warden"
    rc = _run(
        ["recover", "--backup", "backup-nope.tar.gz", "--root", str(root), "--data", str(tmp_path / "t")]
    )
    assert rc == 4
    assert "取不下来" in capsys.readouterr().out


def test_recover_refuses_an_escaping_name(tmp_path: Path, capsys):
    """名字来自网络那侧，不能直接拼进路径。"""
    root = tmp_path / "warden"
    rc = _run(
        ["recover", "--backup", "../../state.json", "--root", str(root), "--data", str(tmp_path / "t")]
    )
    assert rc != 0
    assert not (tmp_path / "target").exists()
