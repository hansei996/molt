"""epoch：单调递增的 fencing token。

## 为什么需要它

考虑这个场景：

  1. Windows 壳持有租约 epoch=7，正在写记忆。
  2. Windows 那边发生了一次长 GC 停顿（或者系统卡死、网络分区）。
  3. 租约 TTL 到期，Warden 认为它死了，把租约发给 NAS 壳，epoch=8。
  4. NAS 壳开始写。
  5. Windows 的 GC 结束，它**不知道自己已经不是 7 了**，继续写。

如果没有 fencing，第 5 步会把第 4 步的数据覆盖掉，两边记忆分裂。

Kleppmann《How to do distributed locking》(2016-02-08) 的原话：

  "GC can pause a running thread at any point"
  "You cannot fix this problem by inserting a check on the lock expiry just
   before writing back"
  "This requires the storage server to take an active role in checking tokens,
   and rejecting any writes on which the token has gone backwards"

所以防线只能放在**存储端**（Warden），而且它在每次写入时都必须检查。

## 本模块的职责

只做三件事，且只做这三件事：

  - 分配下一个 epoch（只会变大）
  - 判定一次写入携带的 epoch 是否可接受
  - 持久化（由调用方注入 store，本模块不关心存储介质）

真正的"拒绝"发生在 Warden 里，本模块提供判定逻辑，确保判定只有一份实现
——这样它才可能被测试覆盖到。判定逻辑写两遍，就是两处会写错的地方。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .errors import EpochAhead, EpochStale, LeaseExpired


# 租约 TTL 的默认值。选 90 秒的理由：
#   - 足够长：一次正常的迁徙（备份 → 传输 → 校验 → 启动）通常 20–40 秒，
#     留 2 倍余量，避免正常干活时租约过期。
#   - 足够短：如果壳真的死了，Warden 90 秒后就能把控制权交出去。
#     相比"永久锁死需要人工介入"，90 秒是可接受的等待。
#
# 这个数字**不**影响正确性——正确性由 epoch 保证。TTL 只影响"多久能恢复"。
DEFAULT_LEASE_TTL_SECONDS = 90


@dataclass
class Lease:
    """一次成功的租约授予。

    epoch 是 fencing token：它只会变大，且每次交接必须严格 +1。
    """

    epoch: int
    shell_id: str
    granted_at: float          # unix 时间戳
    expires_at: float          # granted_at + ttl
    ttl_seconds: int

    def is_expired(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at

    def remaining(self, now: Optional[float] = None) -> float:
        return max(0.0, self.expires_at - (now if now is not None else time.time()))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "epoch": self.epoch,
            "shell_id": self.shell_id,
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
            "ttl_seconds": self.ttl_seconds,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Lease":
        return cls(
            epoch=int(d["epoch"]),
            shell_id=str(d["shell_id"]),
            granted_at=float(d["granted_at"]),
            expires_at=float(d["expires_at"]),
            ttl_seconds=int(d["ttl_seconds"]),
        )


@dataclass
class FencedState:
    """Warden 侧的持久状态。整个 Soul 的正确性挂在这几个字段上。"""

    current_epoch: int = 0
    # 当前租约。不存在时表示"没有壳在干活"。
    lease: Optional[Lease] = None
    # 上一次成功写入的 epoch，用于审计与排查。
    last_accepted_epoch: int = 0
    # 被拒绝的写入计数。这个数字如果非零，说明发生过 fencing 事件，
    # 值得告警——它意味着某个我们以为死掉的壳还活着。
    rejected_write_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "current_epoch": self.current_epoch,
            "lease": self.lease.to_dict() if self.lease else None,
            "last_accepted_epoch": self.last_accepted_epoch,
            "rejected_write_count": self.rejected_write_count,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FencedState":
        return cls(
            current_epoch=int(d.get("current_epoch", 0)),
            lease=Lease.from_dict(d["lease"]) if d.get("lease") else None,
            last_accepted_epoch=int(d.get("last_accepted_epoch", 0)),
            rejected_write_count=int(d.get("rejected_write_count", 0)),
        )


def next_epoch(state: FencedState) -> int:
    """计算下一个 epoch。

    这个函数唯一的规则：**返回值必须严格大于 current_epoch**。

    有人会想"能不能重用刚过期的那个 epoch，省一次自增"——不能。
    重用 epoch 等于取消了 fencing：一个还活着的旧壳会用同一个数字写入，
    存储端无法区分。宁可跳号也不能重用。
    """
    return state.current_epoch + 1


def grant_lease(
    state: FencedState,
    shell_id: str,
    ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    now: Optional[float] = None,
) -> Lease:
    """把租约授予 shell_id，并返回带新 epoch 的 Lease。

    调用方负责：确认没有别的未过期租约（或用 force 语义强行抢占）。
    本函数不做抢占判定，因为"该不该抢占"是策略，不是机制。
    """
    now = now if now is not None else time.time()
    epoch = next_epoch(state)

    state.current_epoch = epoch
    lease = Lease(
        epoch=epoch,
        shell_id=shell_id,
        granted_at=now,
        expires_at=now + ttl_seconds,
        ttl_seconds=ttl_seconds,
    )
    state.lease = lease
    return lease


def accept_write(
    state: FencedState,
    claimed_epoch: int,
    claimed_shell_id: str,
    now: Optional[float] = None,
) -> None:
    """判定一次写入是否可接受。不可接受时抛异常，**绝不返回 False 让调用方忽略**。

    设计上刻意用异常而不是布尔返回值：fencing 失败是必须中断流程的事件，
    写成返回值就一定会有人 `if not ok: pass`。

    三步判定，顺序不能变：

      1. epoch 必须**等于** current_epoch。小了是旧壳复活（EpochStale），
         大了是有人绕过了 Warden（EpochAhead）。两者都是致命。
      2. 租约必须未过期。
      3. shell_id 必须与租约持有者一致（防止同 epoch 的不同壳混写）。

    第 1 步用"等于"而不是"大于等于"，是因为"大于"已经被第 1 步的另一个分支
    覆盖了；剩下的唯一合法值就是相等。这比 `>=` 更严格，也更难写错。
    """
    now = now if now is not None else time.time()

    # --- 第 1 步：epoch 必须精确等于当前值 ---
    if claimed_epoch < state.current_epoch:
        state.rejected_write_count += 1
        raise EpochStale(
            f"拒绝写入：携带 epoch={claimed_epoch}，但当前已是 "
            f"{state.current_epoch}。一个我们以为已经停止的壳仍然活着，"
            f"并且正在尝试写入（shell_id={claimed_shell_id!r}）。"
            f"这是 fencing 生效的证据，请立刻告警并排查。"
        )

    if claimed_epoch > state.current_epoch:
        state.rejected_write_count += 1
        raise EpochAhead(
            f"拒绝写入：携带 epoch={claimed_epoch}，超前于当前 "
            f"{state.current_epoch}。有人绕过了 Warden 直接改状态"
            f"（shell_id={claimed_shell_id!r}）。不是兼容性问题，是完整性事件。"
        )

    # --- 第 2 步：租约必须仍然有效 ---
    if state.lease is None:
        state.rejected_write_count += 1
        raise LeaseExpired(
            f"拒绝写入：当前没有有效租约，但有壳（shell_id={claimed_shell_id!r}）"
            f"持 epoch={claimed_epoch} 想写入。"
        )

    if state.lease.is_expired(now):
        state.rejected_write_count += 1
        raise LeaseExpired(
            f"拒绝写入：epoch={claimed_epoch} 的租约已于 "
            f"{state.lease.expires_at:.3f} 过期（现在 {now:.3f}）。"
            f"过期的租约不能延长，必须重新申请——"
            f"因为它意味着这个壳的时间判断已经不可靠。"
        )

    # --- 第 3 步：写入者必须是租约持有者 ---
    if claimed_shell_id != state.lease.shell_id:
        state.rejected_write_count += 1
        raise EpochAhead(
            f"拒绝写入：epoch={claimed_epoch} 的租约属于 "
            f"{state.lease.shell_id!r}，但写入者是 {claimed_shell_id!r}。"
            f"同一个 epoch 不允许被两个壳共用。"
        )

    # --- 全部通过，记录 ---
    state.last_accepted_epoch = claimed_epoch


def is_safe_to_start(
    state: FencedState,
    now: Optional[float] = None,
) -> tuple[bool, str]:
    """启动一个新壳之前的自检。

    返回 (是否安全, 原因)。这个函数被 CLI 的 `molt status` 与启动前的
    preflight 共用，确保"人看到的判断"和"程序做的判断"是同一份逻辑。
    """
    now = now if now is not None else time.time()

    if state.lease is None:
        return True, "没有活跃租约，可以安全接手"

    if state.lease.is_expired(now):
        return True, (
            f"租约（epoch={state.lease.epoch}, shell={state.lease.shell_id!r}）"
            f"已于 {state.lease.expires_at:.3f} 过期，可以安全接手"
        )

    return False, (
        f"epoch={state.lease.epoch} 的租约仍由 {state.lease.shell_id!r} 持有，"
        f"还有 {state.lease.remaining(now):.0f} 秒到期。"
        f"此刻启动新壳会导致两只鱼同时在线（违反公理 0）。"
        f"如果确认旧壳已经死了，请等租约过期，或显式执行 force-revoke。"
    )
