"""`migrate` 与 `land` 两条命令的行为测试。

## 这两条命令的测试重点是什么

不是"能不能跑通"——那在真机端到端里验过了。这里的重点是**失败时
状态是否干净**，因为这是它们唯一比手工做四步更好的地方：

  - 热备失败 → 租约必须还回去，不能白占 90 秒
  - 打包失败 → 同上
  - 登记失败 → 同上
  - 落位失败 → 目标 data 目录里的原有文件不能被破坏

一个"成功路径能用、失败路径把系统留在半死状态"的工具，比没有工具
更危险，因为它让人以为状态是确定的。

## 为什么用 `main()` 而不是直接调 `cmd_migrate`

`args` 的形状是 parser 定义的，手搓一个 Namespace 会和真实调用漂移
（比如漏了某个 `default=SUPPRESS` 的参数）。走 `main([...])` 更接近
用户真的敲的命令，也能顺带测到参数解析。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from molt.cli import main
from molt.soul import Manifest, sha256_of
from molt.warden import Warden


# --------------------------------------------------------------------------
# 夹具：造一个像样的源端 data 目录
# --------------------------------------------------------------------------


def _make_sqlite(path: Path, rows: int = 3) -> None:
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


def _make_source_data(base: Path) -> Path:
    data = base / "source" / "data"
    _make_sqlite(data / "data_v4.db", rows=3)
    _make_sqlite(
        data / "plugin_data" / "astrbot_plugin_memory_companion" / "memory_companion.db",
        rows=2,
    )
    (data / "cmd_config.json").write_text(
        '{"config_version": 5}', encoding="utf-8-sig"
    )
    return data


def _read_state(root: Path) -> dict:
    return json.loads((root / "state.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# migrate：成功路径
# --------------------------------------------------------------------------


def test_migrate_happy_path(tmp_path: Path):
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"

    rc = main(
        [
            "migrate",
            "--data",
            str(data),
            "--shell",
            "shell-a",
            "--root",
            str(root),
            "--target",
            "shell-b",
            "--no-color",
        ]
    )

    assert rc == 0, "migrate 应该成功"

    # 账本对
    st = _read_state(root)
    assert st["fenced_state"]["current_epoch"] == 1
    assert len(st["generations"]) == 1
    rec = st["generations"][0]
    assert rec["pushed_by"] == "shell-a"
    assert rec["target_shell"] == "shell-b"
    assert rec["epoch"] == 1

    # 归档产出且文件在那
    archive = root / "staging" / "gen-000001.tar.gz"
    assert archive.is_file()
    assert archive.stat().st_size > 0

    # manifest 在 generation 目录里
    mpath = root / "staging" / "gen-000001" / "manifest.json"
    assert mpath.is_file()
    m = Manifest.load(mpath)
    assert m.source_host
    assert m.generation == 1
    assert len(m.artifacts) == 3


def test_migrate_releases_lease_by_default(tmp_path: Path):
    """默认必须在做完后放掉租约，否则目标壳进不来（先死后生）。"""
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"

    rc = main(
        ["migrate", "--data", str(data), "--shell", "shell-a", "--root", str(root), "--no-color"]
    )
    assert rc == 0

    st = _read_state(root)
    assert st["fenced_state"]["lease"] is None, "做完应该没有活动租约"


def test_migrate_keep_lease_option(tmp_path: Path):
    """`--keep-lease` 时租约要留着（我还要再拍一份的场景）。"""
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"

    rc = main(
        [
            "migrate",
            "--data",
            str(data),
            "--shell",
            "shell-a",
            "--root",
            str(root),
            "--keep-lease",
            "--no-color",
        ]
    )
    assert rc == 0

    st = _read_state(root)
    assert st["fenced_state"]["lease"] is not None
    assert st["fenced_state"]["lease"]["shell_id"] == "shell-a"


def test_migrate_second_run_takes_new_epoch(tmp_path: Path):
    """第二次迁徙必须拿到 epoch 2，而不是复用 1。"""
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"

    assert main(["migrate", "--data", str(data), "--shell", "shell-a", "--root", str(root), "--no-color"]) == 0
    assert main(["migrate", "--data", str(data), "--shell", "shell-b", "--root", str(root), "--no-color"]) == 0

    st = _read_state(root)
    assert st["fenced_state"]["current_epoch"] == 2
    assert [g["epoch"] for g in st["generations"]] == [1, 2]
    assert [g["pushed_by"] for g in st["generations"]] == ["shell-a", "shell-b"]


def test_migrate_no_archive_still_registers(tmp_path: Path):
    """`--no-archive` 只跳过打包，登记照做。"""
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"

    rc = main(
        [
            "migrate",
            "--data",
            str(data),
            "--shell",
            "shell-a",
            "--root",
            str(root),
            "--no-archive",
            "--no-color",
        ]
    )
    assert rc == 0
    assert not (root / "staging" / "gen-000001.tar.gz").exists()
    assert len(_read_state(root)["generations"]) == 1


# --------------------------------------------------------------------------
# migrate：失败时必须还租约（这是最重要的三条）
# --------------------------------------------------------------------------


def test_migrate_refuses_when_other_shell_holds_lease(tmp_path: Path):
    """公理 0：别的壳在线时必须拒绝，而且不能动账本。"""
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"

    w = Warden(root=root)
    w.acquire("shell-other")
    before = _read_state(root)

    rc = main(
        ["migrate", "--data", str(data), "--shell", "shell-a", "--root", str(root), "--no-color"]
    )

    assert rc == 3
    after = _read_state(root)
    assert after["fenced_state"]["lease"]["shell_id"] == "shell-other"
    assert after["fenced_state"]["current_epoch"] == before["fenced_state"]["current_epoch"]
    assert len(after["generations"]) == 0


def test_migrate_releases_lease_when_backup_fails(tmp_path: Path):
    """源库坏了 → 备份失败 → 租约必须还回去，不能白占 90 秒。"""
    data = _make_source_data(tmp_path)
    # 把主库写成垃圾（截断到 1 页的合法头 + 垃圾）
    victim = data / "data_v4.db"
    victim.write_bytes(b"SQLite format 3\x00" + b"\xff" * 2048)
    root = tmp_path / "warden"

    rc = main(
        ["migrate", "--data", str(data), "--shell", "shell-a", "--root", str(root), "--no-color"]
    )

    assert rc == 4
    st = _read_state(root)
    assert st["fenced_state"]["lease"] is None, "失败后必须还租约"


def test_migrate_releases_lease_when_no_artifacts(tmp_path: Path):
    """源目录里一个库都没有 → 没意义 → 还租约。"""
    data = tmp_path / "source" / "data"
    data.mkdir(parents=True)
    root = tmp_path / "warden"

    rc = main(
        ["migrate", "--data", str(data), "--shell", "shell-a", "--root", str(root), "--no-color"]
    )

    assert rc == 4
    assert _read_state(root)["fenced_state"]["lease"] is None


def test_migrate_refuses_missing_data_dir(tmp_path: Path):
    root = tmp_path / "warden"
    rc = main(
        [
            "migrate",
            "--data",
            str(tmp_path / "nope"),
            "--shell",
            "shell-a",
            "--root",
            str(root),
            "--no-color",
        ]
    )
    assert rc == 2
    # 连租约都不该碰
    assert not (root / "state.json").exists() or _read_state(root)["fenced_state"]["lease"] is None


def test_migrate_refuses_ambiguous_backend(tmp_path: Path):
    """同时给 --root 和 --warden-url 必须拒绝（含糊地猜是出事的开始）。"""
    data = _make_source_data(tmp_path)
    rc = main(
        [
            "migrate",
            "--data",
            str(data),
            "--shell",
            "shell-a",
            "--root",
            str(tmp_path / "w"),
            "--warden-url",
            "http://127.0.0.1:1",
            "--warden-token",
            "t",
            "--no-color",
        ]
    )
    assert rc == 2


# --------------------------------------------------------------------------
# land
# --------------------------------------------------------------------------


def _do_migrate(tmp_path: Path) -> tuple[Path, Path, dict]:
    """跑一次 migrate，返回 (归档路径, 源 data, manifest dict)。"""
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"
    assert main(
        ["migrate", "--data", str(data), "--shell", "shell-a", "--root", str(root), "--no-color"]
    ) == 0
    archive = root / "staging" / "gen-000001.tar.gz"
    m = Manifest.load(root / "staging" / "gen-000001" / "manifest.json")
    return archive, data, m.to_dict()


def test_land_dry_run_touches_nothing(tmp_path: Path):
    """不给 --yes 时只能检查，绝不能动 data 目录。"""
    archive, _src, _md = _do_migrate(tmp_path)
    target = tmp_path / "target" / "data"
    target.mkdir(parents=True)
    (target / "data_v4.db").write_bytes(b"OLD")

    rc = main(
        [
            "land",
            "--archive",
            str(archive),
            "--data",
            str(target),
            "--sha256",
            sha256_of(archive),
            "--no-color",
        ]
    )

    assert rc == 0
    assert (target / "data_v4.db").read_bytes() == b"OLD", "dry-run 不能动文件"
    assert not (target / "cmd_config.json").exists()


def test_land_yes_restores_files(tmp_path: Path):
    archive, src, md = _do_migrate(tmp_path)
    target = tmp_path / "target" / "data"
    target.mkdir(parents=True)

    rc = main(
        [
            "land",
            "--archive",
            str(archive),
            "--data",
            str(target),
            "--sha256",
            sha256_of(archive),
            "--yes",
            "--no-color",
        ]
    )

    assert rc == 0
    assert (target / "data_v4.db").is_file()
    assert (target / "cmd_config.json").is_file()
    # manifest 不该进 data/
    assert not (target / "manifest.json").exists()
    assert not (target / "x.txt").exists(), "上一份残留文件不能混进来"

    # 注意：不能断言 `== (src/"data_v4.db").read_bytes()`。
    # 热备产物与源库**逻辑等价但字节不同**——SQLite 的 backup() 会重写页，
    # 而且源库的头里有 WAL 模式/变更计数等运行时字段。第一次写这条断言时
    # 就是踩了这个：看起来"应该一样"，其实是两个不同的文件。
    # 该断言的是**内容**，那才是真正要保真的东西。
    def rows(db: Path):
        conn = sqlite3.connect(str(db))
        try:
            return [r[0] for r in conn.execute("SELECT title FROM conversations ORDER BY id")]
        finally:
            conn.close()

    assert rows(target / "data_v4.db") == rows(src / "data_v4.db")
    assert len(rows(target / "data_v4.db")) == 3


def test_land_rejects_wrong_sha(tmp_path: Path):
    """指纹不对必须在解包前就停手。"""
    archive, _src, _md = _do_migrate(tmp_path)
    target = tmp_path / "target" / "data"
    target.mkdir(parents=True)

    rc = main(
        [
            "land",
            "--archive",
            str(archive),
            "--data",
            str(target),
            "--sha256",
            "0" * 64,
            "--yes",
            "--no-color",
        ]
    )

    assert rc == 4
    assert not (target / "data_v4.db").exists()


def test_land_refuses_non_molt_archive(tmp_path: Path):
    """不是一个 molt 快照（没有 manifest）必须拒绝。"""
    import tarfile

    payload = tmp_path / "x.txt"
    payload.write_text("hi", encoding="utf-8")
    archive = tmp_path / "not-molt.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(str(payload), arcname="x.txt")

    target = tmp_path / "target" / "data"
    target.mkdir(parents=True)

    rc = main(
        [
            "land",
            "--archive",
            str(archive),
            "--data",
            str(target),
            "--yes",
            "--no-color",
        ]
    )
    assert rc == 6


def test_land_refuses_missing_archive(tmp_path: Path):
    target = tmp_path / "data"
    target.mkdir()
    rc = main(
        ["land", "--archive", str(tmp_path / "nope.tar.gz"), "--data", str(target), "--no-color"]
    )
    assert rc == 2


def test_land_refuses_when_parent_of_target_is_missing(tmp_path: Path):
    """父目录都不在 → 认成路径打错，拒绝。

    这一条与下一条是同一个检查的两侧：**父目录存在与否**是区分
    「这台机器还没跑过 AstrBot」和「你手滑打错了盘符」的唯一判据。
    """
    archive, _s, _m = _do_migrate(tmp_path)
    rc = main(
        [
            "land",
            "--archive",
            str(archive),
            "--data",
            str(tmp_path / "no-such-parent" / "data"),
            "--yes",
            "--no-color",
        ]
    )
    assert rc == 2
    assert not (tmp_path / "no-such-parent").exists()


def test_land_creates_target_data_dir_on_a_brand_new_shell(tmp_path: Path, capsys):
    """全新壳上 data/ 本来就不存在 —— 落位应该自己建它，而不是要求人先 mkdir。

    AstrBot 第一次启动才会创建 data/。如果落位要求它已存在，
    那"迁到一台全新的 Windows 上"这个最主要的场景就变成了
    「先随便跑一遍 AstrBot、再停掉、再落位」的谜题。
    """
    archive, src, _m = _do_migrate(tmp_path)
    target = tmp_path / "fresh-shell" / "data"
    parent = target.parent
    parent.mkdir()
    rc = main(
        ["land", "--archive", str(archive), "--data", str(target), "--yes", "--no-color"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "目标 data 目录不存在" in out
    assert "全新壳" in out
    # 真的落进去了，而且数据是源的
    assert (target / "data_v4.db").is_file()
    assert (target / "cmd_config.json").is_file()

    def rows(db: Path):
        conn = sqlite3.connect(str(db))
        try:
            return conn.execute("SELECT title FROM conversations ORDER BY id").fetchall()
        finally:
            conn.close()

    assert rows(target / "data_v4.db") == rows(src / "data_v4.db")


def test_land_refuses_when_target_has_nonempty_wal(tmp_path: Path):
    """目标库有未合并事务时，落位必须拒绝（这是数据安全的核心一条）。"""
    archive, _src, _md = _do_migrate(tmp_path)
    target = tmp_path / "target" / "data"
    target.mkdir(parents=True)
    (target / "data_v4.db").write_bytes(b"OLD")
    (target / "data_v4.db-wal").write_bytes(b"\x00" * 4096)

    rc = main(
        [
            "land",
            "--archive",
            str(archive),
            "--data",
            str(target),
            "--sha256",
            sha256_of(archive),
            "--yes",
            "--no-color",
        ]
    )

    assert rc == 2
    assert (target / "data_v4.db").read_bytes() == b"OLD"


def test_land_full_roundtrip_data_survives(tmp_path: Path):
    """端到端：源库 → 归档 → 落位 → 目标库能读出同样的数据。"""
    archive, src, _md = _do_migrate(tmp_path)
    target = tmp_path / "target" / "data"
    target.mkdir(parents=True)

    rc = main(
        [
            "land",
            "--archive",
            str(archive),
            "--data",
            str(target),
            "--sha256",
            sha256_of(archive),
            "--yes",
            "--no-color",
        ]
    )
    assert rc == 0

    def rows(db: Path):
        conn = sqlite3.connect(str(db))
        try:
            return [r[0] for r in conn.execute("SELECT title FROM conversations ORDER BY id")]
        finally:
            conn.close()

    assert rows(target / "data_v4.db") == rows(src / "data_v4.db")
    assert len(rows(target / "data_v4.db")) == 3


def test_land_refuses_when_target_data_dir_looks_live(tmp_path: Path, capsys):
    """目标 data 里还有 `-shm` = 有个活着的 AstrBot 正握着那个库。

    这条路以前是敞开的：NAS 的容器一直跑着（只是被 drain 哑了），
    `land` 会把文件换掉，而那个进程继续往被移走的旧 inode 上写 ——
    账本说第 N 代已落地，运行中的进程还在读写第 N-2 代。
    **这不是崩溃，是两边各说各话。**
    """
    archive, src, _md = _do_migrate(tmp_path)
    target = tmp_path / "target" / "data"
    target.mkdir(parents=True)
    (target / "data_v4.db").write_bytes(b"OLD")
    (target / "data_v4.db-shm").write_bytes(b"\x00" * 32768)

    rc = main(
        [
            "land",
            "--archive",
            str(archive),
            "--data",
            str(target),
            "--sha256",
            sha256_of(archive),
            "--yes",
            "--no-color",
        ]
    )

    out = capsys.readouterr().out
    assert rc == 2
    # 一个字节都没动
    assert (target / "data_v4.db").read_bytes() == b"OLD"
    assert "正在被一个活着的 AstrBot 使用" in out
    assert "data_v4.db-shm" in out
    # 提示里要给出可执行的下一步
    assert "docker kill -s INT" in out
    assert "--force" in out


def test_land_force_lands_despite_live_marker(tmp_path: Path, capsys):
    """`--force` 能跳过它 —— 人确认过就行，猜测不该是不可绕过的。"""
    archive, src, _md = _do_migrate(tmp_path)
    target = tmp_path / "target" / "data"
    target.mkdir(parents=True)
    (target / "data_v4.db").write_bytes(b"OLD")
    (target / "data_v4.db-shm").write_bytes(b"\x00" * 32768)

    rc = main(
        [
            "land",
            "--archive",
            str(archive),
            "--data",
            str(target),
            "--sha256",
            sha256_of(archive),
            "--yes",
            "--force",
            "--no-color",
        ]
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "跳过这项检查" in out
    assert (target / "data_v4.db").stat().st_size == (src / "data_v4.db").stat().st_size
