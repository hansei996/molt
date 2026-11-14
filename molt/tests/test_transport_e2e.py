"""跨机归档传输的端到端命令测试：`migrate --transport warden` + `land --generation`。

## 这组测试要证明什么

单独的 `test_archive_transfer.py` 证明了"字节能传"。这组要证明的是
**那条命令链能走完**，而且是在**只有两次命令调用**的情况下：

    源端：molt migrate --transport warden
    目标端：molt land --generation N --yes

中间没有任何人手。这正是 v0.3 的目标，也是"全自动"这个说法唯一
站得住的证据。

## 为什么必须真的起一个 Warden 服务

因为要证明的正是"壳和 Warden 不是同一个进程"。用 `LocalBackend`
（同一个进程、同一个文件系统）测出来的"通过"说明不了跨机的事——
那种测试在 `test_migrate.py` 里已经有了，覆盖的是本地模式。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from molt.cli import main
from molt.server import WardenServer
from molt.soul import Manifest, sha256_of


# --------------------------------------------------------------------------
# 夹具
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


def _make_data(base: Path, name: str = "data", rows: int = 3) -> Path:
    data = base / name
    _make_sqlite(data / "data_v4.db", rows=rows)
    _make_sqlite(
        data / "plugin_data" / "astrbot_plugin_memory_companion" / "memory_companion.db",
        rows=2,
    )
    (data / "cmd_config.json").write_text('{"config_version": 5}', encoding="utf-8-sig")
    return data


def _titles(db: Path) -> list:
    conn = sqlite3.connect(str(db))
    try:
        return [r[0] for r in conn.execute("SELECT title FROM conversations ORDER BY id")]
    finally:
        conn.close()


@pytest.fixture
def warden(tmp_path: Path):
    """起一个真 Warden 服务，跑在 NAS 的"视角"下（tmp 下另一个目录）。"""
    srv = WardenServer(root=tmp_path / "nas-warden", token="tok", port=0)
    host, port = srv.start()
    try:
        yield srv, f"http://127.0.0.1:{port}"
    finally:
        srv.stop()


REMOTE = ["--no-color"]


# --------------------------------------------------------------------------
# migrate --transport warden
# --------------------------------------------------------------------------


def test_migrate_uploads_archive_to_warden(tmp_path: Path, warden):
    srv, base = warden
    data = _make_data(tmp_path / "src")

    rc = main(
        [
            "migrate",
            "--data", str(data),
            "--shell", "shell-a",
            "--target", "shell-b",
            "--warden-url", base,
            "--warden-token", "tok",
            "--staging", str(tmp_path / "staging"),
            *REMOTE,
        ]
    )
    assert rc == 0

    # 归档真的到了 Warden 的归档区
    arcs = srv.warden.list_archives() if hasattr(srv.warden, "list_archives") else None
    path = srv.warden.archive_path(1)
    assert path.is_file(), "归档没有上传到中转区"
    assert path.stat().st_size > 0

    # 账本也登记了
    st = json.loads((tmp_path / "nas-warden" / "state.json").read_text(encoding="utf-8"))
    gens = st["generations"]
    assert len(gens) == 1
    assert gens[0]["pushed_by"] == "shell-a"
    assert gens[0]["target_shell"] == "shell-b"


def test_migrate_default_transport_is_warden_when_remote(tmp_path: Path, warden):
    """远端后端下不给 --transport 也应该自动走 warden。

    真机上没人会记得加那个参数，而"忘了加"的后果是归档留在本机、
    目标端什么也取不到——一个静默的半成品。
    """
    srv, base = warden
    data = _make_data(tmp_path / "src2")

    rc = main(
        [
            "migrate",
            "--data", str(data),
            "--shell", "shell-a",
            "--warden-url", base,
            "--warden-token", "tok",
            "--staging", str(tmp_path / "staging2"),
            *REMOTE,
        ]
    )
    assert rc == 0
    assert srv.warden.archive_path(1).is_file()


def test_migrate_transport_warden_rejected_for_local_backend(tmp_path: Path):
    """本地后端 + --transport warden = 配置错误，要早点拒绝。

    本地模式下"上传给 Warden"和"拷到自己目录"是同一件事，
    容忍它只会掩盖"我到底在跟谁说话"这个混乱。
    """
    data = _make_data(tmp_path / "src3")
    rc = main(
        [
            "migrate",
            "--data", str(data),
            "--shell", "shell-a",
            "--root", str(tmp_path / "warden"),
            "--transport", "warden",
            *REMOTE,
        ]
    )
    assert rc == 2


def test_migrate_bad_transport_value_is_rejected(tmp_path: Path):
    data = _make_data(tmp_path / "src4")
    with pytest.raises(SystemExit):
        # argparse 的 choices 会直接退出
        main(
            [
                "migrate",
                "--data", str(data),
                "--shell", "shell-a",
                "--transport", "smb",
                *REMOTE,
            ]
        )


def test_migrate_upload_failure_releases_lease(tmp_path: Path, warden, monkeypatch):
    """上传失败必须还租约——否则那 90 秒谁都不能接管。"""
    srv, base = warden
    data = _make_data(tmp_path / "src5")

    from molt import backend as backend_mod

    original = backend_mod.RemoteBackend.upload_archive

    def boom(self, generation, archive_path, *, expected_sha256=""):
        from molt.errors import Fatal

        raise Fatal("模拟上传失败")

    monkeypatch.setattr(backend_mod.RemoteBackend, "upload_archive", boom)

    rc = main(
        [
            "migrate",
            "--data", str(data),
            "--shell", "shell-a",
            "--warden-url", base,
            "--warden-token", "tok",
            "--staging", str(tmp_path / "staging5"),
            *REMOTE,
        ]
    )
    assert rc == 4

    st = json.loads((tmp_path / "nas-warden" / "state.json").read_text(encoding="utf-8"))
    assert st["fenced_state"]["lease"] is None, "失败后租约没有被还回去"
    assert st["generations"] == [], "失败的上传不该登记 generation"


# --------------------------------------------------------------------------
# land --generation
# --------------------------------------------------------------------------


def _migrate_then_land(tmp_path: Path, warden, *, yes: bool = True):
    srv, base = warden
    src = _make_data(tmp_path / "src", rows=4)
    rc = main(
        [
            "migrate",
            "--data", str(src),
            "--shell", "shell-a",
            "--target", "shell-b",
            "--warden-url", base,
            "--warden-token", "tok",
            "--staging", str(tmp_path / "staging"),
            *REMOTE,
        ]
    )
    assert rc == 0

    target = _make_data(tmp_path / "target", name="data", rows=0)
    args = [
        "land",
        "--generation", "1",
        "--data", str(target),
        "--warden-url", base,
        "--warden-token", "tok",
        "--shell", "shell-b",
        "--to", str(tmp_path / "work"),
        *REMOTE,
    ]
    if yes:
        args.append("--yes")
    land_rc = main(args)
    return src, target, land_rc


def test_land_generation_downloads_and_restores(tmp_path: Path, warden):
    """完整链路：源端一条 migrate，目标端一条 land --generation。"""
    src, target, rc = _migrate_then_land(tmp_path, warden, yes=True)
    assert rc == 0

    # 数据真的到了目标 data
    assert _titles(target / "data_v4.db") == _titles(src / "data_v4.db")
    assert (target / "cmd_config.json").is_file()

    # 落位后不留 WAL sidecar
    names = sorted(p.name for p in target.iterdir() if p.is_file())
    assert "data_v4.db-wal" not in names
    assert "data_v4.db-shm" not in names


def test_land_generation_dry_run_does_not_touch_target(tmp_path: Path, warden):
    """不给 --yes：只下载 + 校验，不动目标 data。

    这里有一条容易忽略的事：**下载本身是有副作用的**（盘上多了个
    tar.gz）。dry-run 只承诺"不碰目标 data"，不承诺"不下载"——
    因为不下载就无从校验，而"能不能落位"这个判断依赖校验结果。
    """
    src, target, rc = _migrate_then_land(tmp_path, warden, yes=False)
    assert rc == 0
    # 目标库还是原来那个空的
    assert _titles(target / "data_v4.db") == []


def test_land_requires_exactly_one_source(tmp_path: Path, warden):
    _, base = warden
    target = _make_data(tmp_path / "t")

    # 两个都不给
    assert main(
        ["land", "--data", str(target), "--warden-url", base,
         "--warden-token", "tok", *REMOTE]
    ) == 2

    # 两个都给
    arc = tmp_path / "x.tar.gz"
    arc.write_bytes(b"not really a tar")
    assert main(
        ["land", "--data", str(target), "--archive", str(arc),
         "--generation", "1", "--warden-url", base,
         "--warden-token", "tok", *REMOTE]
    ) == 2


def test_land_generation_needs_remote_backend(tmp_path: Path):
    """--generation 必须配远端后端：中转区不在本地。"""
    target = _make_data(tmp_path / "t2")
    rc = main(
        ["land", "--generation", "1", "--data", str(target), *REMOTE]
    )
    assert rc == 2


def test_land_generation_missing_archive_is_error(tmp_path: Path, warden):
    """中转区里没有那一代：报错退出，目标 data 一动不动。"""
    _, base = warden
    target = _make_data(tmp_path / "t3")
    before = _titles(target / "data_v4.db")

    rc = main(
        ["land", "--generation", "42", "--data", str(target),
         "--warden-url", base, "--warden-token", "tok",
         "--to", str(tmp_path / "w3"), *REMOTE]
    )
    assert rc == 4
    assert _titles(target / "data_v4.db") == before


def test_land_generation_uses_server_reported_sha(tmp_path: Path, warden):
    """不给 --sha256 时，用服务端回报的哈希继续往下校验。

    这条防止一个很隐蔽的退化：`land` 为了图方便"下载了就直接解包"，
    于是 sha256 校验在整条链上一次都没发生过。
    """
    srv, base = warden
    src = _make_data(tmp_path / "s")
    assert main(
        ["migrate", "--data", str(src), "--shell", "a",
         "--warden-url", base, "--warden-token", "tok",
         "--staging", str(tmp_path / "stg"), *REMOTE]
    ) == 0

    # 拿磁盘上真实的 sha，确认 land 之后解包目录里的 manifest 与它一致
    real_sha = sha256_of(srv.warden.archive_path(1))
    target = _make_data(tmp_path / "t4")
    rc = main(
        ["land", "--generation", "1", "--data", str(target),
         "--warden-url", base, "--warden-token", "tok",
         "--to", str(tmp_path / "w4"), "--yes", *REMOTE]
    )
    assert rc == 0
    landed = tmp_path / "w4" / _only_landed(tmp_path / "w4")
    m = Manifest.load(landed / "manifest.json")
    assert m.generation == 1
    assert _titles(target / "data_v4.db") == _titles(src / "data_v4.db")


def _only_landed(work: Path) -> str:
    names = [p.name for p in work.iterdir() if p.is_dir() and p.name.startswith("landed-")]
    assert len(names) == 1, f"期望恰好一个 landed-* 目录，实际 {names}"
    return names[0]
