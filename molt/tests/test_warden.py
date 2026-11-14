"""针对 warden.py 的测试。

这是最重要的一组测试，因为 Warden 是唯一允许改变灵魂的地方。
重点验证：

  - 原子写：不留半截文件
  - 状态往返：重启后 epoch / generation / 拒绝计数都还在
  - fencing 真的挡住了并发（两个壳抢同一个 Warden）
  - 续租不跳号（否则壳自己的写入会被自己拒掉）
  - 快照的 generation 必须递增（防重放）
  - 协议版本不匹配要拒绝（跨版本混状态比不能迁徙更糟）
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from molt.epoch import FencedState, Lease
from molt.errors import (
    EpochAhead,
    EpochStale,
    Fatal,
    LeaseExpired,
    LeaseHeld,
    NeedsHuman,
    ProtocolMismatch,
)
from molt.version import PROTOCOL_VERSION
from molt.warden import GenerationRecord, Warden, _atomic_write_json


def make_warden(tmp_path: Path) -> Warden:
    w = Warden(root=tmp_path / "molt")
    w.load()
    return w


def manifest(
    generation: int = 1,
    *,
    protocol_version: int = PROTOCOL_VERSION,
    created_at: float | None = None,
    source_host: str = "UGREEN-776B",
    artifacts: list | None = None,
) -> dict:
    return {
        "generation": generation,
        "created_at": created_at if created_at is not None else time.time(),
        "source_host": source_host,
        "source_root": "/mnt/dm-0/astrbot/data",
        "protocol_version": protocol_version,
        "astrbot_version": "4.29.4",
        "platform": "linux",
        "python_version": "3.12.16",
        "artifacts": artifacts
        or [
            {"relpath": "data_v4.db", "kind": "sqlite", "bytes": 10887168},
            {"relpath": "cmd_config.json", "kind": "copy", "bytes": 8199},
        ],
        "runtime_pip": ["edge_tts==7.3.1"],
    }


# --------------------------------------------------------------------------
# 原子写
# --------------------------------------------------------------------------


def test_atomic_write_creates_file_with_content(tmp_path: Path):
    p = tmp_path / "x.json"
    _atomic_write_json(p, {"a": 1})
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1}


def test_atomic_write_leaves_no_temp_files(tmp_path: Path):
    p = tmp_path / "x.json"
    _atomic_write_json(p, {"a": 1})
    leftovers = [f.name for f in tmp_path.iterdir() if f.name != "x.json"]
    assert leftovers == []


def test_atomic_write_replaces_existing_without_partial_state(tmp_path: Path):
    p = tmp_path / "x.json"
    _atomic_write_json(p, {"v": 1})
    _atomic_write_json(p, {"v": 2})
    assert json.loads(p.read_text(encoding="utf-8")) == {"v": 2}


def test_atomic_write_handles_non_ascii(tmp_path: Path):
    """state.json 里会存主机名，可能是中文。不能被转义或写坏。"""
    p = tmp_path / "x.json"
    _atomic_write_json(p, {"host": "群晖-NAS", "note": "大肥鱼"})
    raw = p.read_text(encoding="utf-8")
    assert "群晖-NAS" in raw
    assert "\\u" not in raw


def test_atomic_write_creates_parent_dirs(tmp_path: Path):
    p = tmp_path / "a" / "b" / "c.json"
    _atomic_write_json(p, {"ok": True})
    assert p.exists()


# --------------------------------------------------------------------------
# 载入 / 往返
# --------------------------------------------------------------------------


def test_first_load_creates_clean_state(tmp_path: Path):
    w = make_warden(tmp_path)
    assert w.state.current_epoch == 0
    assert w.state.lease is None
    assert w.generations == []


def test_state_roundtrip_preserves_epoch_and_generations(tmp_path: Path):
    w = make_warden(tmp_path)
    lease = w.acquire("win-shell-aaa")
    w.accept_snapshot(
        claimed_epoch=lease.epoch,
        claimed_shell_id="win-shell-aaa",
        manifest=manifest(1),
    )

    w2 = Warden(root=tmp_path / "molt")
    w2.load()

    assert w2.state.current_epoch == w.state.current_epoch
    assert len(w2.generations) == 1
    assert w2.generations[0].generation == 1
    # 记的是**提交者**，不是"接收者"——多机下这是两个不同的壳。
    # 详见 `GenerationRecord` 的 docstring。
    assert w2.generations[0].pushed_by == "win-shell-aaa"


def test_corrupt_state_raises_fatal_not_silent_reset(tmp_path: Path):
    """state.json 坏了必须炸，不能悄悄当成全新状态。

    悄悄重置的后果：epoch 归零 → 所有旧壳的写入都变成"超前"或被误接受，
    这是最危险的一种静默失败。
    """
    w = make_warden(tmp_path)
    w.acquire("x")
    (tmp_path / "molt" / "state.json").write_text("{not json", encoding="utf-8")

    w2 = Warden(root=tmp_path / "molt")
    with pytest.raises(Fatal) as ei:
        w2.load()
    assert "不能猜" in str(ei.value)


def test_future_protocol_version_is_rejected(tmp_path: Path):
    """别人用更新版协议写的 state，旧程序必须拒绝。

    降级运行会破坏状态——这比"启动失败"更糟。
    """
    w = make_warden(tmp_path)
    payload = w._snapshot_payload()
    payload["protocol_version"] = PROTOCOL_VERSION + 5
    _atomic_write_json(tmp_path / "molt" / "state.json", payload)

    w2 = Warden(root=tmp_path / "molt")
    with pytest.raises(ProtocolMismatch):
        w2.load()


# --------------------------------------------------------------------------
# 租约：互斥（公理 0 的实现）
# --------------------------------------------------------------------------


def test_acquire_grants_first_lease(tmp_path: Path):
    w = make_warden(tmp_path)
    lease = w.acquire("win-shell")
    assert lease.shell_id == "win-shell"
    assert lease.epoch == 1
    assert lease.remaining() > 0


def test_second_shell_is_refused(tmp_path: Path):
    """核心：两只鱼不能同时在线。"""
    w = make_warden(tmp_path)
    w.acquire("shell-A")

    with pytest.raises(LeaseHeld) as ei:
        w.acquire("shell-B")
    assert "公理 0" in str(ei.value) or "只能有一个" in str(ei.value)


def test_same_shell_renews_without_bumping_epoch(tmp_path: Path):
    """续租不换 epoch。

    如果每次心跳都跳号，壳正在进行的写入会被自己拒掉——
    这是个很容易犯的错，而且症状诡异（心跳一密就开始报 EpochStale）。
    """
    w = make_warden(tmp_path)
    a = w.acquire("shell-A", ttl_seconds=60)
    b = w.acquire("shell-A", ttl_seconds=60)

    assert b.epoch == a.epoch
    assert b.expires_at >= a.expires_at
    assert w.state.current_epoch == a.epoch


def expire_lease_on_disk(w: Warden, seconds_ago: float = 1.0) -> None:
    """把租约的 expires_at 拨到过去，**并且写进磁盘**。

    【为什么必须写磁盘】Warden 的每个修改操作都在跨进程锁内重新
    `load()`——这是并发正确性的前提（见 warden.py 的 _InterProcessLock
    文档）。所以直接改内存 `w.state.lease.expires_at` 会被下一次
    load 覆盖掉，测试就会误以为"过期检查失效"。

    这不是测试的妥协，是**验证真实路径**：现实中租约过期也是
    "磁盘上的时间到了"，不是"某个人改了内存变量"。
    """
    w._ensure_loaded()
    assert w.state.lease is not None
    w.state.lease.expires_at = time.time() - seconds_ago
    w.save()


def test_expired_lease_can_be_taken_over_by_another_shell(tmp_path: Path):
    """壳死了（不交还租约）→ TTL 到期后别人能接管。

    这是"多久能恢复"的机制，不是正确性机制。
    """
    w = make_warden(tmp_path)
    w.acquire("dead-shell", ttl_seconds=10)
    expire_lease_on_disk(w)

    lease = w.acquire("new-shell", ttl_seconds=60)
    assert lease.shell_id == "new-shell"
    assert lease.epoch > 1


def test_release_with_wrong_epoch_is_refused(tmp_path: Path):
    w = make_warden(tmp_path)
    lease = w.acquire("shell-A")

    with pytest.raises(EpochStale):
        w.release("shell-A", epoch=lease.epoch + 99)


def test_release_by_wrong_shell_is_refused(tmp_path: Path):
    w = make_warden(tmp_path)
    lease = w.acquire("shell-A")

    with pytest.raises(EpochStale):
        w.release("shell-B", epoch=lease.epoch)


def test_release_then_acquire_bumps_epoch(tmp_path: Path):
    w = make_warden(tmp_path)
    a = w.acquire("shell-A")
    w.release("shell-A", a.epoch)
    b = w.acquire("shell-B")

    assert b.epoch > a.epoch
    assert w.state.lease.shell_id == "shell-B"


def test_check_can_start_reports_lease_holder(tmp_path: Path):
    w = make_warden(tmp_path)
    ok, why = w.check_can_start("anyone")
    assert ok is True

    w.acquire("shell-A")
    ok2, why2 = w.check_can_start("shell-B")
    assert ok2 is False
    assert "shell-A" in why2 or "只能" in why2


# --------------------------------------------------------------------------
# accept_snapshot：唯一的写入入口
# --------------------------------------------------------------------------


def test_accept_snapshot_records_generation(tmp_path: Path):
    w = make_warden(tmp_path)
    lease = w.acquire("win-shell")
    rec = w.accept_snapshot(
        claimed_epoch=lease.epoch,
        claimed_shell_id="win-shell",
        manifest=manifest(1),
    )
    assert rec.generation == 1
    assert rec.epoch == lease.epoch
    assert rec.artifact_bytes == 10887168 + 8199


def test_accept_snapshot_rejects_stale_epoch(tmp_path: Path):
    """旧壳（epoch 已过期）试图写 → 必须被拒。

    这就是 Kleppmann 说的"存储端主动拒绝倒退的 token"。
    """
    w = make_warden(tmp_path)
    lease = w.acquire("shell-A")
    w.release("shell-A", lease.epoch)
    w.acquire("shell-B")

    with pytest.raises(EpochStale):
        w.accept_snapshot(
            claimed_epoch=lease.epoch,          # 旧 epoch
            claimed_shell_id="shell-A",
            manifest=manifest(1),
        )


def test_accept_snapshot_rejects_ahead_epoch(tmp_path: Path):
    """猜一个大 epoch 想抢写 → 必须被拒。"""
    w = make_warden(tmp_path)
    lease = w.acquire("shell-A")

    with pytest.raises(EpochAhead):
        w.accept_snapshot(
            claimed_epoch=lease.epoch + 1,
            claimed_shell_id="shell-A",
            manifest=manifest(1),
        )


def test_accept_snapshot_rejects_wrong_shell_id(tmp_path: Path):
    """epoch 对但 shell_id 不对 → 拒绝（另一个壳拿到了同一个 epoch 的情况）。"""
    w = make_warden(tmp_path)
    lease = w.acquire("shell-A")

    with pytest.raises(EpochAhead):
        w.accept_snapshot(
            claimed_epoch=lease.epoch,
            claimed_shell_id="shell-B",
            manifest=manifest(1),
        )


def test_accept_snapshot_rejects_expired_lease(tmp_path: Path):
    w = make_warden(tmp_path)
    lease = w.acquire("shell-A", ttl_seconds=10)
    expire_lease_on_disk(w)

    with pytest.raises(LeaseExpired):
        w.accept_snapshot(
            claimed_epoch=lease.epoch,
            claimed_shell_id="shell-A",
            manifest=manifest(1),
        )


def test_rejections_are_counted(tmp_path: Path):
    """拒绝次数要能被观察——它是发现"两条鱼在打架"的信号。

    这个测试守的是一个真实 bug：`accept_write` 在内存里把计数 +1
    之后抛异常，如果 Warden 不在这里 save，计数就永远不落盘，
    信号等于没接。
    """
    w = make_warden(tmp_path)
    lease = w.acquire("shell-A")

    for _ in range(3):
        with pytest.raises(EpochAhead):
            w.accept_snapshot(
                claimed_epoch=lease.epoch + 7,
                claimed_shell_id="shell-A",
                manifest=manifest(1),
            )

    assert w.state.rejected_write_count == 3

    # 更关键：重新从磁盘读，计数还在
    w2 = Warden(root=tmp_path / "molt")
    w2.load()
    assert w2.state.rejected_write_count == 3, "拒绝计数必须落盘，否则信号丢失"


def test_accept_snapshot_rejects_replayed_older_generation(tmp_path: Path):
    """重放旧快照（generation 不递增）→ 拒绝。"""
    w = make_warden(tmp_path)
    lease = w.acquire("shell-A")
    w.accept_snapshot(
        claimed_epoch=lease.epoch,
        claimed_shell_id="shell-A",
        manifest=manifest(2),
    )

    with pytest.raises(NeedsHuman) as ei:
        w.accept_snapshot(
            claimed_epoch=lease.epoch,
            claimed_shell_id="shell-A",
            manifest=manifest(1),       # 比已登记的 2 小
        )
    assert "重放" in str(ei.value) or "不大于" in str(ei.value)


def test_accept_snapshot_rejects_same_generation_again(tmp_path: Path):
    w = make_warden(tmp_path)
    lease = w.acquire("shell-A")
    w.accept_snapshot(
        claimed_epoch=lease.epoch, claimed_shell_id="shell-A", manifest=manifest(3)
    )
    with pytest.raises(NeedsHuman):
        w.accept_snapshot(
            claimed_epoch=lease.epoch, claimed_shell_id="shell-A", manifest=manifest(3)
        )


def test_accept_snapshot_rejects_wrong_protocol(tmp_path: Path):
    w = make_warden(tmp_path)
    lease = w.acquire("shell-A")

    with pytest.raises(ProtocolMismatch):
        w.accept_snapshot(
            claimed_epoch=lease.epoch,
            claimed_shell_id="shell-A",
            manifest=manifest(1, protocol_version=PROTOCOL_VERSION + 1),
        )


def test_accept_snapshot_rejects_missing_protocol(tmp_path: Path):
    w = make_warden(tmp_path)
    lease = w.acquire("shell-A")
    m = manifest(1)
    del m["protocol_version"]

    with pytest.raises(ProtocolMismatch):
        w.accept_snapshot(
            claimed_epoch=lease.epoch, claimed_shell_id="shell-A", manifest=m
        )


def test_generation_records_survive_reload_in_order(tmp_path: Path):
    w = make_warden(tmp_path)
    lease = w.acquire("shell-A")
    for gen in (1, 2, 3):
        w.accept_snapshot(
            claimed_epoch=lease.epoch,
            claimed_shell_id="shell-A",
            manifest=manifest(gen),
        )

    w2 = Warden(root=tmp_path / "molt")
    w2.load()
    assert [g.generation for g in w2.generations] == [1, 2, 3]


def test_next_generation_number_follows_history(tmp_path: Path):
    w = make_warden(tmp_path)
    assert w.next_generation_number() == 1

    lease = w.acquire("shell-A")
    w.accept_snapshot(
        claimed_epoch=lease.epoch, claimed_shell_id="shell-A", manifest=manifest(7)
    )
    assert w.next_generation_number() == 8


# --------------------------------------------------------------------------
# status（五层防线的第 5 层：人能看懂现在是谁）
# --------------------------------------------------------------------------


def test_status_with_no_lease(tmp_path: Path):
    w = make_warden(tmp_path)
    s = w.status()
    assert s["lease"]["state"] == "no-lease"
    assert s["current_epoch"] == 0
    assert s["generation_count"] == 0


def test_status_with_active_lease(tmp_path: Path):
    w = make_warden(tmp_path)
    w.acquire("win-shell")
    s = w.status()
    assert s["lease"]["state"] == "active"
    assert s["lease"]["holder"] == "win-shell"
    assert s["lease"]["remaining_seconds"] > 0


def test_status_with_expired_lease(tmp_path: Path):
    w = make_warden(tmp_path)
    w.acquire("dead-shell", ttl_seconds=10)
    expire_lease_on_disk(w, seconds_ago=5)
    s = w.status()
    assert s["lease"]["state"] == "expired"
    assert s["lease"]["expired_seconds_ago"] >= 4


def test_status_shows_latest_generation(tmp_path: Path):
    w = make_warden(tmp_path)
    lease = w.acquire("shell-A")
    w.accept_snapshot(
        claimed_epoch=lease.epoch, claimed_shell_id="shell-A", manifest=manifest(1)
    )
    s = w.status()
    assert s["generation_count"] == 1
    assert s["latest_generation"]["generation"] == 1


def test_status_is_json_serializable(tmp_path: Path):
    w = make_warden(tmp_path)
    w.acquire("shell-A")
    json.dumps(w.status(), ensure_ascii=False)      # 不抛即可


# --------------------------------------------------------------------------
# 目录与日志
# --------------------------------------------------------------------------


def test_generation_dir_is_zero_padded(tmp_path: Path):
    w = make_warden(tmp_path)
    assert w.generation_dir(1).name == "gen-000001"
    assert w.generation_dir(123456).name == "gen-123456"


def test_prepare_generation_dir_creates_it(tmp_path: Path):
    w = make_warden(tmp_path)
    p = w.prepare_generation_dir(1)
    assert p.exists() and p.is_dir()


def test_append_log_writes_readable_lines(tmp_path: Path):
    w = make_warden(tmp_path)
    w.append_log("info", "迁徙开始")
    w.append_log("error", "校验失败")

    text = w.log_path().read_text(encoding="utf-8")
    assert "迁徙开始" in text
    assert "[ERROR]" in text
    assert "[INFO ]" in text
