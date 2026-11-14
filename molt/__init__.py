"""molt —— 让一个 AI 存在在多台机器之间迁徙，而不失去连续性。

## 一句话

记忆不流动。流动的是"谁有资格读它、写它"。

## 为什么这个包长这样

三个核心概念，对应三个模块：

  `soul`    不可再生的那部分（数据库、记忆、人格）
  `shell`   可抛弃的那部分（Python 环境、二进制、桌面会话）
  `warden`  唯一权威，住在 NAS 上，决定谁有资格写 soul

再加一个 `epoch`，它比上面三个都小，但比它们都重要：
它是"fencing token"，单调递增，永不回退。整个系统的正确性
建立在"存储端拒绝 epoch 倒退的写入"这一条上。

## 公理 0

任意时刻只能有一个存在在线。

这不是技术限制，是产品定义。两只鱼同时回你消息，
那就不是一个存在，是两个机器人。

## 已知的坑（都实测踩过）

  - 两个进程抢租约：只加锁不重载状态是无效的，必须锁内 reload
  - 坏库的行为是抛 DatabaseError，不是 quick_check 返回非 ok
  - `cmd_config.json` 带 UTF-8 BOM，Linux 上 `json.load()` 直接报错
  - PowerShell 脚本没有 BOM 会被按 GBK 解码，中文注释导致整文件解析失败
"""

from .version import PROTOCOL_VERSION, REQUIRED_CAPABILITIES, __version__

__all__ = [
    "__version__",
    "PROTOCOL_VERSION",
    "REQUIRED_CAPABILITIES",
]
