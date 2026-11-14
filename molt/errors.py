"""错误类型。

设计原则：错误必须是可分类的，因为 Warden 对不同类别的反应完全不同。

- 可自愈（SelfHealable）：当场重试一次就能好（比如网卡 arm 状态丢了）。
- 需要人（NeedsHuman）：自动化不该硬扛的事（比如休眠没关、BIOS 没设）。
- 致命（Fatal）：出现了不该出现的状态，必须停下来，绝不"尽力而为"继续。

fencing 相关的错误单独成类，因为它们的正确反应是**立刻停手**，而不是重试。
"""

from __future__ import annotations


class MoltError(Exception):
    """所有 Molt 异常的基类。"""

    kind = "unknown"


class SelfHealable(MoltError):
    """当场可以自动修复的阻塞，Warden 应该自愈后重试一次。"""

    kind = "self_healable"


class NeedsHuman(MoltError):
    """需要人介入的阻塞，Warden 必须拒绝交接并告警。"""

    kind = "needs_human"


class Fatal(MoltError):
    """不该出现的情况，必须停止。"""

    kind = "fatal"


class EpochStale(Fatal):
    """写入携带的 epoch 比存储端的当前 epoch 旧。

    这是 fencing 的**核心防线**，也是最不应该发生的事：它意味着有一个
    我们以为已经死掉的壳还活着，并且在试图写入。正确反应是立刻拒绝、
    立刻告警、**绝不**因为"数据看起来是对的"就放行。

    见 ARCH §11.3.1：这一行写错，整套 fencing 就废了。
    """

    kind = "epoch_stale"


class EpochAhead(Fatal):
    """写入携带的 epoch 比存储端的当前 epoch 新。

    同样致命：说明有人绕过了 Warden 直接改状态。比起"往后兼容"，
    更应该停下来查清楚。
    """

    kind = "epoch_ahead"


class LeaseHeld(MoltError):
    """租约被另一个壳持有且未过期。

    注意：这**不是**安全问题，只是效率问题。真正的屏障是 epoch。
    """

    kind = "lease_held"


class LeaseExpired(NeedsHuman):
    """租约已过期，无法再用于写入。

    过期的租约意味着这个壳的判断可能已经不可靠（GC 停顿、网络分区、
    时钟漂移），必须重新申请而不是强制延长。
    """

    kind = "lease_expired"


class ProtocolMismatch(Fatal):
    """协议版本不匹配。必须拒绝交接。"""

    kind = "protocol_mismatch"


class CapabilityMissing(Fatal):
    """对面的壳缺少必需能力。必须拒绝交接。"""

    kind = "capability_missing"
