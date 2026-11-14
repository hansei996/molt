"""中途备份：`backups/` 那一层。

## 这组测试在防什么

迁徙把"最后一句话之前"的状态搬到新壳上，之后的一切只活在那台机器的盘里。
定时回推把它们推给 Warden，出事了至少能回到十分钟前。

这里最要紧的一条不是"能不能存"，而是**它们绝不能进账本的 generations**：
一旦进去，壳那边的 `decide()` 会把一份备份当成一次真正的交接去落位。
所以每个"存下了"的用例后面都跟着一条"账本没多出东西"。
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import urllib.request
from pathlib import Path

import pytest

from molt.client import WardenClient
from molt.errors import Fatal, NeedsHuman
from molt.server import TOKEN_HEADER, WardenServer
from molt.warden import Warden


@pytest.fixture
def live(tmp_path: Path):
    srv = WardenServer(root=tmp_path, token="tok", port=0)
    _host, port = srv.start()
    try:
        yield srv, f"http://127.0.0.1:{port}"
    finally:
        srv.stop()


def _tarball(files: dict) -> bytes:
    """造一份小归档，形状和真的一样（里面是一堆文件）。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _write(tmp_path: Path, name: str = "incoming.tar.gz", files=None) -> Path:
    p = tmp_path / name
    p.write_bytes(_tarball(files or {"data_v4.db": b"x" * 256}))
    return p


# ---------------------------------------------------------------------------
# Warden（直接调，不起 HTTP）
# ---------------------------------------------------------------------------


def test_store_backup_lands_with_a_readable_name(tmp_path: Path):
    w = Warden(root=tmp_path)
    meta = w.store_backup(_write(tmp_path), shell="DESKTOP-H2KGIAO", generation=13)

    assert meta["shell"] == "DESKTOP-H2KGIAO"
    assert meta["generation"] == 13
    assert meta["bytes"] > 0
    assert meta["name"].startswith("backup-")
    assert meta["name"].endswith("-DESKTOP-H2KGIAO.tar.gz")
    assert (tmp_path / "backups" / meta["name"]).is_file()


def test_store_backup_keeps_the_ledger_untouched(tmp_path: Path):
    """**最重要的一条**：备份不是交接。账本里不该多出任何一代。"""
    w = Warden(root=tmp_path)
    before = w.status()["generation_count"]
    w.store_backup(_write(tmp_path), shell="a-shell", generation=13)
    assert w.status()["generation_count"] == before
    assert w.status()["latest_generation"] is None


def test_store_backup_refuses_a_lying_checksum(tmp_path: Path):
    w = Warden(root=tmp_path)
    src = _write(tmp_path)
    with pytest.raises(Fatal, match="指纹不符"):
        w.store_backup(src, shell="a-shell", sha256="0" * 64)
    # 声称的和实际不符时，文件**不能**被收进 backups/，否则
    # 以后取回来的东西和索引上写的不是同一个。
    assert w.list_backups() == []


def test_list_backups_reads_the_sidecar(tmp_path: Path):
    w = Warden(root=tmp_path)
    meta = w.store_backup(_write(tmp_path), shell="win", generation=7)
    items = w.list_backups()

    assert len(items) == 1
    assert items[0]["name"] == meta["name"]
    assert items[0]["sha256"] == meta["sha256"]
    assert items[0]["generation"] == 7
    assert items[0]["shell"] == "win"
    assert items[0]["created_at_readable"]


def test_list_backups_ignores_an_orphan_sidecar(tmp_path: Path):
    """索引在、文件没了 → 不列。一条指向空气的记录比没有记录更糟。"""
    w = Warden(root=tmp_path)
    meta = w.store_backup(_write(tmp_path), shell="win")
    (tmp_path / "backups" / meta["name"]).unlink()

    assert w.list_backups() == []


def test_list_backups_still_works_without_the_sidecar(tmp_path: Path):
    """反过来：文件在、索引丢了，仍要能列出来（那时指纹留空）。"""
    w = Warden(root=tmp_path)
    meta = w.store_backup(_write(tmp_path), shell="win")
    (tmp_path / "backups" / (Path(meta["name"]).stem + ".json")).unlink()

    items = w.list_backups()
    assert [x["name"] for x in items] == [meta["name"]]
    assert items[0]["sha256"] == ""
    assert items[0]["bytes"] > 0


def test_prune_backups_keeps_the_newest(tmp_path: Path):
    w = Warden(root=tmp_path)
    names = []
    for i in range(4):
        src = _write(tmp_path, name=f"in{i}.tar.gz", files={"f": bytes([i]) * 16})
        names.append(w.store_backup(src, shell=f"s{i}")["name"])

    report = w.prune_backups(keep_recent=2)
    assert len(report["kept"]) == 2
    assert len(report["removed"]) == 2
    assert len(w.list_backups()) == 2
    # 留下的是最新的那两份
    assert sorted(x["name"] for x in w.list_backups()) == sorted(names[-2:])


def test_prune_backups_dry_run_deletes_nothing(tmp_path: Path):
    w = Warden(root=tmp_path)
    for i in range(3):
        w.store_backup(_write(tmp_path, name=f"in{i}.tar.gz"), shell=f"s{i}")
    report = w.prune_backups(keep_recent=1, dry_run=True)
    assert len(report["removed"]) == 2
    assert len(w.list_backups()) == 3


def test_store_backup_auto_prunes(tmp_path: Path):
    """每收一份就顺手清理一次——只增不减的目录迟早变成没人敢碰的遗迹。"""
    w = Warden(root=tmp_path)
    for i in range(14):
        w.store_backup(_write(tmp_path, name=f"in{i}.tar.gz"), shell=f"s{i}")
    assert len(w.list_backups()) <= 12


def test_backup_path_refuses_traversal(tmp_path: Path):
    w = Warden(root=tmp_path)
    for bad in ("../state.json", "backup/../../x.tar.gz", "gen-000001.tar.gz", ""):
        with pytest.raises(NeedsHuman):
            w.backup_path(bad)


def test_backup_name_is_url_safe():
    """备份名会进 URL。非 ASCII 会被编码，编码之后又和文件名对不上。"""
    from molt.warden import _slugify

    assert _slugify("DESKTOP-H2KGIAO") == "DESKTOP-H2KGIAO"
    assert _slugify("nas 壳/1") == "nas-1"
    assert _slugify("") == ""


# ---------------------------------------------------------------------------
# HTTP：上传 / 列表 / 下载 / 清理
# ---------------------------------------------------------------------------


def _post(url: str, token: str, data: bytes, *, sha: str = "", shell: str = "",
          generation: int = 0) -> dict:
    headers = {
        TOKEN_HEADER: token,
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(data)),
        "X-Molt-Shell": shell,
        "X-Molt-Generation": str(generation),
    }
    if sha:
        headers["X-Molt-Sha256"] = sha
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_http_upload_then_download(live):
    srv, base = live
    data = _tarball({"data_v4.db": b"hello" * 100})
    sha = hashlib.sha256(data).hexdigest()

    body = _post(f"{base}/backup", "tok", data, sha=sha, shell="win", generation=13)
    assert body["ok"] is True
    name = body["backup"]["name"]
    assert body["backup"]["sha256"] == sha

    req = urllib.request.Request(
        f"{base}/backup/{name}", headers={TOKEN_HEADER: "tok"}, method="GET"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        assert resp.read() == data


def test_http_upload_with_a_lying_checksum_is_rejected(live):
    _srv, base = live
    data = _tarball({"f": b"x"})
    with pytest.raises(urllib.error.HTTPError) as ei:
        _post(f"{base}/backup", "tok", data, sha="0" * 64)
    assert ei.value.code == 400


def test_http_upload_without_a_token_is_refused(live):
    _srv, base = live
    data = _tarball({"f": b"x"})
    with pytest.raises(urllib.error.HTTPError) as ei:
        _post(f"{base}/backup", "wrong", data)
    assert ei.value.code == 401


def test_http_backups_lists_them(live):
    _srv, base = live
    _post(f"{base}/backup", "tok", _tarball({"f": b"x"}), shell="win")
    req = urllib.request.Request(
        f"{base}/backups", headers={TOKEN_HEADER: "tok"}, method="GET"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    assert len(body["backups"]) == 1
    assert body["total_bytes"] > 0


def test_http_download_of_a_missing_backup_is_404(live):
    _srv, base = live
    req = urllib.request.Request(
        f"{base}/backup/backup-20260101T000000-win.tar.gz",
        headers={TOKEN_HEADER: "tok"},
        method="GET",
    )
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=15)
    assert ei.value.code == 404


def test_http_download_refuses_a_traversal_name(live):
    _srv, base = live
    req = urllib.request.Request(
        f"{base}/backup/..%2Fstate.json", headers={TOKEN_HEADER: "tok"}, method="GET"
    )
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=15)
    assert ei.value.code in (404, 422)


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


def test_client_roundtrip(live, tmp_path: Path):
    _srv, base = live
    c = WardenClient(base, "tok")
    src = _write(tmp_path, files={"a.db": b"y" * 512})

    meta = c.upload_backup(src, shell="desktop", generation=5, expected_sha256=hashlib.sha256(src.read_bytes()).hexdigest())
    assert meta["name"].startswith("backup-")

    listed = c.backups()
    assert [x["name"] for x in listed] == [meta["name"]]

    dest = tmp_path / "got.tar.gz"
    got = c.download_backup(meta["name"], dest)
    assert got["bytes"] == src.stat().st_size
    assert dest.read_bytes() == src.read_bytes()

    report = c.prune_backups(keep_recent=12, dry_run=True)
    assert report["dry_run"] is True
