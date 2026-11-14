"""`molt.epoch` 的测试。

这个文件是 v0.1 里最重要的测试文件。理由：`accept_write()` 里的 epoch 倒退判断
是整个项目唯一"写错就全盘皆输"的地方。它一旦失效：

  - 一个我们以为死掉的壳可以继续写
  - 两份记忆会互相覆盖
  - 用户看到的是"我的鱼好像失忆了 / 说胡话了"，而不是一个报错

所以这里的测试不是"覆盖率"，是**证伪尝试**：每一条都在试图构造
"让一个老壳成功写入"的场景，然后要求它被拦住。
"""

from __future__ import annotations

import pytest

from molt.epoch import (
    DEFAULT_LEASE_TTL_SECONDS,
    FencedState,
    Lease,
    accept_write,
    grant_lease,
    is_safe_to_start,
    next_epoch,
)
from molt.errors import EpochAhead, EpochStale, LeaseExpired


# --------------------------------------------------------------------------
# next_epoch：只会变大
# --------------------------------------------------------------------------


def test_initial_state_has_epoch_zero():
    assert FencedState().current_epoch == 0


def test_next_epoch_is_strictly_increasing():
    state = FencedState(current_epoch=7)
    assert next_epoch(state) == 8


def test_next_epoch_never_reuses_a_number():
    """跳号可以，重用不行。

    重用 epoch 等于取消 fencing：一个还活着的旧壳会用同一个数字写入，
    存储端无法区分真假。
    """
    state = FencedState()
    seen = set()
    for _ in range(5):
        state.current_epoch = next_epoch(state)
        assert state.current_epoch not in seen
        seen.add(state.current_epoch)


# --------------------------------------------------------------------------
# grant_lease：租约绑定 shell 与 epoch
# --------------------------------------------------------------------------


def test_grant_lease_bumps_epoch_and_binds_shell():
    state = FencedState()
    lease = grant_lease(state, shell_id="nas", ttl_seconds=90, now=1000.0)

    assert lease.epoch == 1
    assert lease.shell_id == "nas"
    assert lease.expires_at == 1090.0
    assert state.current_epoch == 1
    assert state.lease is lease


def test_grant_lease_twice_produces_two_different_epochs():
    """第二次授予必须拿到更大的 epoch，否则第一个壳的写入不会被拦住。"""
    state = FencedState()
    first = grant_lease(state, "nas", now=1000.0)
    second = grant_lease(state, "win", now=2000.0)

    assert second.epoch > first.epoch
    assert state.lease.shell_id == "win"


# --------------------------------------------------------------------------
# accept_write：这是核心。每一条都在试图绕过 fencing。
# --------------------------------------------------------------------------


def test_accept_write_happy_path():
    state = FencedState()
    lease = grant_lease(state, "nas", now=1000.0)

    accept_write(state, lease.epoch, "nas", now=1001.0)

    assert state.last_accepted_epoch == lease.epoch


def test_accept_write_rejects_stale_epoch():
    """★ 整个项目最重要的一条测试。

    场景：NAS 壳持有 epoch=1，被 GC 停住了。Warden 判定它死了，
    把 epoch=2 给了 Windows 壳。现在 NAS 的 GC 结束，它带着 epoch=1 来写入。

    **必须拒绝。** 如果这里放行，第 31 行的注释就是全部意义。
    """
    state = FencedState()
    old_lease = grant_lease(state, "nas", now=1000.0)          # epoch=1
    grant_lease(state, "win", now=2000.0)                       # epoch=2

    with pytest.raises(EpochStale) as excinfo:
        accept_write(state, old_lease.epoch, "nas", now=2005.0)

    assert "拒绝写入" in str(excinfo.value)
    assert state.rejected_write_count == 1


def test_accept_write_rejects_future_epoch():
    """有人绕过 Warden 直接改状态，声称一个更高的 epoch。同样必须拒绝。"""
    state = FencedState()
    lease = grant_lease(state, "nas", now=1000.0)               # epoch=1

    with pytest.raises(EpochAhead):
        accept_write(state, lease.epoch + 99, "nas", now=1001.0)

    assert state.rejected_write_count == 1


def test_accept_write_rejects_expired_lease():
    """租约过期后不能写入。

    过期的租约意味着这个壳的时间判断不再可靠（GC 停顿、网络分区、时钟漂移），
    必须重新申请而不是延长。
    """
    state = FencedState()
    lease = grant_lease(state, "nas", ttl_seconds=90, now=1000.0)

    with pytest.raises(LeaseExpired):
        accept_write(state, lease.epoch, "nas", now=1091.0)     # 过期 1 秒

    assert state.rejected_write_count == 1


def test_accept_write_allows_write_at_exact_expiry_boundary_minus_epsilon():
    """边界：过期前一刻仍然允许。"""
    state = FencedState()
    lease = grant_lease(state, "nas", ttl_seconds=90, now=1000.0)

    accept_write(state, lease.epoch, "nas", now=1089.999)
    assert state.last_accepted_epoch == lease.epoch


def test_accept_write_rejects_wrong_shell_same_epoch():
    """同一个 epoch 不允许被两个不同的壳共用。

    这是防"我刚抢到租约，但另一个进程用了同一个 epoch"这种混乱。
    """
    state = FencedState()
    lease = grant_lease(state, "nas", now=1000.0)

    with pytest.raises(EpochAhead):
        accept_write(state, lease.epoch, "win", now=1001.0)


def test_accept_write_rejects_when_no_lease():
    state = FencedState(current_epoch=5)          # 有 epoch 但没有租约

    with pytest.raises(LeaseExpired):
        accept_write(state, 5, "nas", now=1000.0)


def test_rejected_write_count_accumulates():
    """被拒绝的次数是要告警的信号，不能因为抛异常就丢了。"""
    state = FencedState()
    lease = grant_lease(state, "nas", now=1000.0)

    for _ in range(3):
        with pytest.raises(EpochAhead):
            accept_write(state, lease.epoch + 50, "nas", now=1001.0)

    assert state.rejected_write_count == 3


def test_accept_write_uses_equality_not_greater_equal():
    """刻意记录这个设计：判定是 `==` 而不是 `>=`。

    如果哪天有人把 `==` 改成 `>=`，`test_accept_write_rejects_stale_epoch`
    仍然会通过（因为 1 < 2），但"当前 epoch 之后的所有数字都合法"这个漏洞
    就打开了。这条测试用当前 epoch 本身来钉住语义：它必须通过，
    而它之后的任何数字都必须失败。
    """
    state = FencedState()
    lease = grant_lease(state, "nas", now=1000.0)

    # 当前 epoch 本身：通过
    accept_write(state, lease.epoch, "nas", now=1001.0)

    # 当前 epoch + 1：失败
    with pytest.raises(EpochAhead):
        accept_write(state, lease.epoch + 1, "nas", now=1002.0)


# --------------------------------------------------------------------------
# 序列化：状态要能落盘，否则重启就丢了
# --------------------------------------------------------------------------


def test_fenced_state_roundtrip():
    state = FencedState()
    grant_lease(state, "nas", ttl_seconds=90, now=1000.0)
    accept_write(state, 1, "nas", now=1001.0)

    restored = FencedState.from_dict(state.to_dict())

    assert restored.current_epoch == state.current_epoch
    assert restored.last_accepted_epoch == state.last_accepted_epoch
    assert restored.rejected_write_count == state.rejected_write_count
    assert restored.lease is not None
    assert restored.lease.epoch == state.lease.epoch
    assert restored.lease.shell_id == state.lease.shell_id
    assert restored.lease.expires_at == state.lease.expires_at


def test_fenced_state_roundtrip_without_lease():
    state = FencedState(current_epoch=3)
    restored = FencedState.from_dict(state.to_dict())

    assert restored.current_epoch == 3
    assert restored.lease is None


def test_restored_state_still_enforces_fencing():
    """反序列化之后 fencing 不能失效——这是最容易漏的一条。"""
    state = FencedState()
    old = grant_lease(state, "nas", now=1000.0)
    grant_lease(state, "win", now=2000.0)

    revived = FencedState.from_dict(state.to_dict())

    with pytest.raises(EpochStale):
        accept_write(revived, old.epoch, "nas", now=2005.0)


# --------------------------------------------------------------------------
# Lease 自身
# --------------------------------------------------------------------------


def test_lease_expiry_helpers():
    lease = Lease(
        epoch=1, shell_id="nas", granted_at=1000.0,
        expires_at=1090.0, ttl_seconds=90,
    )

    assert not lease.is_expired(1089.0)
    assert lease.is_expired(1090.0)          # 恰好到期即算过期
    assert lease.is_expired(2000.0)
    assert lease.remaining(1000.0) == 90.0
    assert lease.remaining(2000.0) == 0.0    # 不为负


def test_default_ttl_is_documented_value():
    """TTL 不参与正确性判定，只影响"多久能恢复"。

    钉住这个数字是为了让文档和代码不会悄悄漂开。
    """
    assert DEFAULT_LEASE_TTL_SECONDS == 90


# --------------------------------------------------------------------------
# is_safe_to_start：CLI 与 preflight 共用同一份判断
# --------------------------------------------------------------------------


def test_safe_to_start_when_no_lease():
    ok, reason = is_safe_to_start(FencedState(), now=1000.0)
    assert ok is True
    assert "没有活跃租约" in reason


def test_safe_to_start_when_lease_expired():
    state = FencedState()
    grant_lease(state, "nas", ttl_seconds=90, now=1000.0)

    ok, reason = is_safe_to_start(state, now=1100.0)
    assert ok is True
    assert "过期" in reason


def test_not_safe_to_start_when_lease_active():
    """这条守的是公理 0：任何时刻只能有一个。

    租约还有效时启动新壳，就是两只鱼同时在线。
    """
    state = FencedState()
    grant_lease(state, "nas", ttl_seconds=90, now=1000.0)

    ok, reason = is_safe_to_start(state, now=1030.0)
    assert ok is False
    assert "只能有一个" in reason or "同时在线" in reason


def test_safe_to_start_reports_remaining_seconds():
    state = FencedState()
    grant_lease(state, "nas", ttl_seconds=90, now=1000.0)

    ok, reason = is_safe_to_start(state, now=1030.0)
    assert ok is False
    assert "60" in reason          # 还剩 60 秒
