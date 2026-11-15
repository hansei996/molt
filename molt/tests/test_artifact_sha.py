"""归档指纹：`Warden.archive_sha*` + `accept_archive` 落 sidecar + 时间轴显示。

## 这一组测试在防什么

`molt timeline` 的每一代都有一列「指纹」。它以前永远是空的 —— 因为账本里
那个 `manifest_sha256` 字段从来没有人写过值。空着的那一列比去掉它还糟：
看见"指纹"两个字，人会以为这里能核对，而实际上没有东西可核对。

现在指纹在**收归档时算出来、写在归档旁边**。跨机的顺序是"先传字节、后登记
generation"，所以收归档那一刻账本里还没有这一代 —— 这正是它不能塞进
`GenerationRecord` 的原因（见 `Warden.archive_sha_path` 的注释）。

三个环节都要各自钉住，缺一个就会静默退化：
  1. 收归档时真的写下了 sidecar（否则时间轴永远空）
  2. `history()` 真的把它读出来挂到每一代上（否则前端拿不到）
  3. 没有 sidecar 时给空串而不是编一个（"不知道"与"没有"必须能区分）
"""

from __future__ import annotations

import hashlib
import io
import sys
from pathlib import Path

import pytest

MOLT_ROOT = Path(__file__).resolve().parents[2]
if str(MOLT_ROOT) not in sys.path:
    sys.path.insert(0, str(MOLT_ROOT))

from molt.errors import Fatal  # noqa: E402
from molt.render import render_timeline  # noqa: E402
from molt.server import TOKEN_HEADER, WardenServer  # noqa: E402
from molt.version import PROTOCOL_VERSION  # noqa: E402
from molt.warden import Warden  # noqa: E402

SHA_A = "a" * 64
SHA_B = "b" * 64


def _manifest(generation: int = 1) -> dict:
    return {
        "generation": generation,
        "created_at": 1794713000.0,
        "source_host": "UGREEN-776B",
        "source_root": "/mnt/dm-0/astrbot/data",
        "protocol_version": PROTOCOL_VERSION,
        "artifacts": [{"path": "data_v4.db", "bytes": 10_887_168}],
        "runtime_pip": [],
    }


# -- Warden 层 -----------------------------------------------------------


def test_record_and_read_back_the_fingerprint(tmp_path: Path) -> None:
    w = Warden(root=tmp_path)
    w.record_archive_sha(3, SHA_A)
    assert w.archive_sha(3) == SHA_A
    # 落盘的是大写还是小写都该被归一化 —— 人手工抄过指纹这件事很常见
    w.record_archive_sha(4, SHA_B.upper())
    assert w.archive_sha(4) == SHA_B


def test_missing_fingerprint_is_empty_not_invented(tmp_path: Path) -> None:
    w = Warden(root=tmp_path)
    assert w.archive_sha(1) == ""


def test_broken_fingerprint_file_reads_as_unknown(tmp_path: Path) -> None:
    w = Warden(root=tmp_path)
    path = w.archive_sha_path(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("这不是指纹", encoding="utf-8")
    assert w.archive_sha(1) == ""


def test_bad_fingerprint_is_refused(tmp_path: Path) -> None:
    w = Warden(root=tmp_path)
    with pytest.raises(Fatal):
        w.record_archive_sha(1, "abc")
    with pytest.raises(Fatal):
        w.record_archive_sha(1, "z" * 64)


def test_history_carries_the_fingerprint_for_each_generation(tmp_path: Path) -> None:
    w = Warden(root=tmp_path)
    lease = w.acquire("win-a")
    w.accept_snapshot(
        claimed_epoch=lease.epoch, claimed_shell_id="win-a", manifest=_manifest(1)
    )
    w.record_archive_sha(1, SHA_A)

    h = w.history()
    assert h["generations"][0]["artifact_sha256"] == SHA_A


def test_history_gives_empty_string_when_no_sidecar(tmp_path: Path) -> None:
    w = Warden(root=tmp_path)
    lease = w.acquire("win-a")
    w.accept_snapshot(
        claimed_epoch=lease.epoch, claimed_shell_id="win-a", manifest=_manifest(1)
    )
    assert w.history()["generations"][0]["artifact_sha256"] == ""


# -- 服务端层 ------------------------------------------------------------


def test_receiving_an_archive_writes_the_sidecar(tmp_path: Path) -> None:
    srv = WardenServer(root=tmp_path, token="tok", port=0)
    body = b"pretend this is a tar.gz"
    digest = hashlib.sha256(body).hexdigest()

    # 走真正的接收路径：把 body 写成临时文件再交给 accept_archive
    tmp = tmp_path / "upload.part"
    tmp.write_bytes(body)
    resp = srv.accept_archive(7, tmp, digest)
    assert resp.status == 200
    assert resp.payload["sha256"] == digest
    assert srv.warden.archive_sha(7) == digest


def test_archive_with_a_wrong_claimed_digest_writes_nothing(tmp_path: Path) -> None:
    srv = WardenServer(root=tmp_path, token="tok", port=0)
    tmp = tmp_path / "upload.part"
    tmp.write_bytes(b"bytes")
    resp = srv.accept_archive(7, tmp, "f" * 64)
    assert resp.status == 400
    assert srv.warden.archive_sha(7) == ""


# -- 渲染层 -------------------------------------------------------------


def _history(gen: dict) -> dict:
    return {
        "root": "/var/lib/molt",
        "generated_at": 1794713454.0,
        "generated_at_readable": "2026-11-15 11:30:54",
        "current_epoch": 1,
        "last_accepted_epoch": 1,
        "rejected_write_count": 0,
        "generation_count": 1,
        "total_bytes": 10_887_168,
        "generations": [gen],
        "backups": [],
        "heartbeats": {},
        "log": [],
    }


def _gen(**over) -> dict:
    g = {
        "generation": 1,
        "created_at": 1794713000.0,
        "created_at_readable": "2026-11-15 11:23:20",
        "source_host": "UGREEN-776B",
        "pushed_by": "nas-1",
        "target_shell": "desk-1",
        "epoch": 1,
        "artifact_bytes": 10_887_168,
        "manifest_sha256": "",
        "artifact_sha256": "",
        "consumed_by": "",
        "consumed_at": 0,
        "consumed_at_readable": "",
    }
    g.update(over)
    return g


def test_timeline_shows_the_archive_fingerprint() -> None:
    page = render_timeline(_history(_gen(artifact_sha256=SHA_A)))
    assert SHA_A[:16] in page


def test_timeline_falls_back_to_the_manifest_fingerprint() -> None:
    page = render_timeline(_history(_gen(manifest_sha256=SHA_B)))
    assert SHA_B[:16] in page


def test_timeline_says_dash_when_there_is_no_fingerprint() -> None:
    page = render_timeline(_history(_gen()))
    assert "指纹 <code>—</code>" in page


def test_timeline_prefers_the_archive_fingerprint_over_the_manifest_one() -> None:
    page = render_timeline(_history(_gen(artifact_sha256=SHA_A, manifest_sha256=SHA_B)))
    assert SHA_A[:16] in page
    assert SHA_B[:16] not in page
