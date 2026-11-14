"""
test_prune.py -- 中转区归档清理的行为。

## 这组测试真正在守什么

清理是**破坏性动作**，所以每一条断言都在检查"不该删的东西没被删"，
而不是"该删的删了"。具体三条边界：

1. **账本里记着的 generation 一律不许删。** 那些是"已经交接出去、
   但目标壳可能还没来取"的归档。删了它，账本里就留下一条取不到字节的记录
   ——这正是 `migrate` 里"先上传再 push"这个顺序想避免的状态。
2. **不认识的文件一律不许删。** 人手工扔进 `archives/` 的东西不归我们管。
3. **默认 `dry_run=True`。** 一个会删文件的函数不该在默认参数下就动手。
"""

import json

import pytest

from molt.errors import Fatal
from molt.warden import Warden, _parse_generation_from_name


def _w(tmp_path):
    return Warden(root=tmp_path)


def _touch(w, gen, size=1000):
    d = w.archive_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"gen-{gen:06d}.tar.gz"
    p.write_bytes(b"x" * size)
    return p


# ── 文件名解析 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,expected",
    [
        ("gen-000005.tar.gz", 5),
        ("gen-000000.tar.gz", 0),
        ("gen-999999.tar.gz", 999999),
        ("gen-5.tar.gz", None),          # 不补零 = 不是我们写的
        ("gen-000005.tar", None),        # 后缀不对
        ("gen-000005.tar.gz.part", None),  # 下载中间态
        ("someone-else.tar.gz", None),
        ("", None),
    ],
)
def test_parse_generation_from_name(name, expected):
    assert _parse_generation_from_name(name) == expected


# ── list_archives ─────────────────────────────────────────────────────


def test_list_marks_referenced_by_ledger(tmp_path):
    w = _w(tmp_path)
    a = _touch(w, 1)
    b = _touch(w, 2, size=2000)

    # 只把第 1 代记进账本（模拟"交接了但目标壳还没取")
    epoch = w.acquire("shell-a").epoch
    import hashlib

    w.accept_snapshot(
        claimed_epoch=epoch,
        claimed_shell_id="shell-a",
        manifest={
            "generation": 1,
            "protocol_version": 1,
            "artifacts": [
                {
                    "relpath": "data_v4.db",
                    "kind": "sqlite",
                    "bytes": a.stat().st_size,
                    "sha256": hashlib.sha256(a.read_bytes()).hexdigest(),
                    "quick_check": "ok",
                    "table_count": 1,
                }
            ],
        },
    )

    items = {x["name"]: x for x in w.list_archives()}
    assert items["gen-000001.tar.gz"]["referenced"] is True
    assert items["gen-000001.tar.gz"]["deletable"] is False
    assert items["gen-000002.tar.gz"]["referenced"] is False
    assert items["gen-000002.tar.gz"]["deletable"] is True
    assert items["gen-000002.tar.gz"]["bytes"] == 2000


def test_list_on_missing_dir_is_empty(tmp_path):
    w = _w(tmp_path)
    assert w.list_archives() == []


def test_list_ignores_foreign_files(tmp_path):
    w = _w(tmp_path)
    _touch(w, 1)
    (w.archive_dir() / "notes.txt").write_text("hi")
    names = [x["name"] for x in w.list_archives()]
    assert names == ["gen-000001.tar.gz"]


# ── prune_archives ────────────────────────────────────────────────────


def test_prune_defaults_to_dry_run(tmp_path):
    """默认不许删。一个会删文件的函数，默认参数下不能动手。"""
    w = _w(tmp_path)
    for g in (1, 2, 3):
        _touch(w, g)
    r = w.prune_archives()
    assert r["dry_run"] is True
    assert len(r["removed"]) == 1  # 3 份、keep_recent 默认 2
    assert (w.archive_dir() / "gen-000001.tar.gz").exists(), "dry_run 竟然真删了"


def test_prune_keeps_recent(tmp_path):
    w = _w(tmp_path)
    for g in range(1, 6):
        _touch(w, g)
    r = w.prune_archives(keep_recent=2, dry_run=False)
    remaining = sorted(p.name for p in w.archive_dir().iterdir())
    assert remaining == ["gen-000004.tar.gz", "gen-000005.tar.gz"]
    assert [x["name"] for x in r["removed"]] == [
        "gen-000001.tar.gz",
        "gen-000002.tar.gz",
        "gen-000003.tar.gz",
    ]
    assert r["freed_bytes"] == 3000


def test_prune_never_deletes_ledger_referenced(tmp_path):
    """
    **核心边界。** 账本里记着的即使很旧也不许删。
    """
    import hashlib

    w = _w(tmp_path)
    old = _touch(w, 1)
    for g in (2, 3, 4, 5):
        _touch(w, g)

    epoch = w.acquire("shell-a").epoch
    w.accept_snapshot(
        claimed_epoch=epoch,
        claimed_shell_id="shell-a",
        manifest={
            "generation": 1,
            "protocol_version": 1,
            "artifacts": [
                {
                    "relpath": "data_v4.db",
                    "kind": "sqlite",
                    "bytes": old.stat().st_size,
                    "sha256": hashlib.sha256(old.read_bytes()).hexdigest(),
                    "quick_check": "ok",
                    "table_count": 1,
                }
            ],
        },
    )

    r = w.prune_archives(keep_recent=1, dry_run=False)
    assert old.exists(), "账本里记着的归档被删了 —— 那会让账本指向空气"
    reasons = {x["name"]: x["reason"] for x in r["kept"]}
    assert "账本" in reasons["gen-000001.tar.gz"]


def test_prune_never_deletes_foreign_files(tmp_path):
    w = _w(tmp_path)
    d = w.archive_dir()
    d.mkdir(parents=True, exist_ok=True)
    foreign = d / "somebody-elses-backup.tar.gz"
    foreign.write_bytes(b"z" * 500)
    _touch(w, 1)
    _touch(w, 2)

    w.prune_archives(keep_recent=0, dry_run=False)
    assert foreign.exists(), "不认识的文件不该被清理逻辑删掉"


def test_prune_keep_zero_removes_all_unreferenced(tmp_path):
    w = _w(tmp_path)
    for g in (1, 2, 3):
        _touch(w, g)
    r = w.prune_archives(keep_recent=0, dry_run=False)
    assert r["removed"]
    assert list(w.archive_dir().iterdir()) == []


def test_prune_rejects_negative_keep_recent(tmp_path):
    w = _w(tmp_path)
    with pytest.raises(Fatal):
        w.prune_archives(keep_recent=-1)


def test_prune_reports_freed_bytes_only_for_removed(tmp_path):
    w = _w(tmp_path)
    _touch(w, 1, size=111)
    _touch(w, 2, size=222)
    _touch(w, 3, size=333)
    r = w.prune_archives(keep_recent=1, dry_run=False)
    assert r["freed_bytes"] == 111 + 222


def test_prune_logs_removals(tmp_path):
    """删了东西必须留痕 —— 否则人只发现磁盘上少了文件，不知道谁删的。"""
    w = _w(tmp_path)
    for g in (1, 2):
        _touch(w, g)
    w.prune_archives(keep_recent=0, dry_run=False)
    log = w.log_path().read_text(encoding="utf-8")
    assert "清理归档 gen-000001.tar.gz" in log
    assert "清理归档 gen-000002.tar.gz" in log


def test_prune_dry_run_does_not_log(tmp_path):
    w = _w(tmp_path)
    for g in (1, 2):
        _touch(w, g)
    w.prune_archives(keep_recent=0, dry_run=True)
    # 日志文件可能不存在（没写过东西），存在也不该有"清理"字样
    if w.log_path().exists():
        assert "清理归档" not in w.log_path().read_text(encoding="utf-8")


def test_prune_is_idempotent(tmp_path):
    w = _w(tmp_path)
    for g in (1, 2, 3):
        _touch(w, g)
    w.prune_archives(keep_recent=1, dry_run=False)
    r2 = w.prune_archives(keep_recent=1, dry_run=False)
    assert r2["removed"] == []
    assert r2["freed_bytes"] == 0


def test_prune_handles_partial_download(tmp_path):
    """
    下载中断会留下 `gen-000005.tar.gz.part`（client 的兜底清理万一没跑成）。
    它不是合法归档名，所以清理逻辑不碰它 —— 但也**不该因此报警**。
    """
    w = _w(tmp_path)
    d = w.archive_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "gen-000005.tar.gz.part").write_bytes(b"half")
    _touch(w, 1)
    r = w.prune_archives(keep_recent=0, dry_run=False)
    assert (d / "gen-000005.tar.gz.part").exists()
    assert all(x["name"] != "gen-000005.tar.gz.part" for x in r["removed"])
