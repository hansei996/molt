"""版本与协议常量。

PROTOCOL_VERSION 是 Warden 与 Shell 之间的握手协议版本。它和软件版本是两件
不同的事：软件可以频繁升级，协议不能。任何一边不匹配时，双方必须拒绝交接，
而不是"尽力而为"——一个半新半旧的交接比不交接危险得多。
"""

__version__ = "0.5.0"

PROTOCOL_VERSION = 1

# 本版本的能力声明。Warden 用它判断对面的 Shell 能不能承担这次交接。
# 缺任何一个能力都必须拒绝，不能降级继续。
REQUIRED_CAPABILITIES = (
    "soul.read",       # 能读出 Soul 快照
    "soul.write",      # 能写入 Soul 快照
    "shell.identity",  # 能自报 shell_id（用于租约绑定）
)
