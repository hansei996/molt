# Molt 技术方案（ARCH）

> **Molt / 蜕壳** —— 让一个数字生命在多台机器之间迁徙，而它的记忆原地不动。
>
> 文档状态：Draft v0.14 · 基于真实硬件实测（NAS + Windows 双机）· 所有关键结论标注证据等级
>
> **已验证的关卡**（截至 2026-11-14 23:15 —— **两个方向都跑通，两套命令走完，最后一个产品级未知数也回答了**）：
> ✅ Windows 原生跑 AstrBot（178 包零缺 wheel）· ✅ 记忆跨机往返字节一致 · ✅ AstrBot 消费还原数据并读出人格
> ✅ 真 S5 关机 → 魔术封包唤醒（23.1 秒）· ✅ 重启后 WOL arm 自愈（开机第 11 秒）· ✅ DHCP 地址保留
> ✅ **去程 NAS→Windows（epoch 1→2）** · ✅ **归巢 Windows→NAS（epoch 3→4）** · ✅ **跨机互斥反例被正确拒绝**
> ✅ **优雅停机 = SIGINT**（SIGTERM 退出码 137 丢 WAL；SIGINT 退出码 0 且 WAL 归零）
> ✅ **Windows 上也有等价的优雅停机信号**（`AttachConsole` + `GenerateConsoleCtrlEvent`；`os.kill(CTRL_BREAK)` 是假阳性）
> ✅ **`molt migrate` 一条命令**（真机 1.4 秒：唤醒 → 拿租约 → 热备 → 打包 → **上传中转区** → 登记 → 还租约）
> ✅ **`molt land` 落位 + 机器覆写层**（**7 处**自动改写，含**关掉平台适配器**——这条是公理 0 的护栏）
> ✅ **跨机字节传输已自动化**：源端 `migrate` PUT 归档 → Warden 中转区 → 目标端 `land --generation N` GET 下来。**中间不需要 scp、不需要共享盘、不需要第三套凭证**
> ✅ **唤醒进了产品**（`molt wake`；`migrate` 里是**先唤醒再拿租约**，顺序有测试固定）
> ✅ **中转区可清理**（`molt prune`；账本里记着的一律不删，默认只看不删）
> ✅ **AstrBot 侧控制面**（`astrbot_plugin_molt` 四端点）：**能让它闭嘴而不用杀掉进程**。
> `molt migrate --control-url` 真机跑通 —— `speaking` True→False（**复核过**）、快照在闭嘴之后拍、租约已交还
> ✅ **真实 QQ 收发**（2026-11-14 22:53）：新壳用从 NAS 搬来的凭据连上 `api.sgroup.qq.com`，
> 人发「在吗在吗」→ 它回「在喵，怎么了？」；生产容器前后都干净停机，**双活窗口为 0**
> ✅ **自己用的流程已成形**（`docs/SELFUSE.md` + `tools/platform-switch.py`），见 §13.13
> ✅ v0.1 代码骨架完成（**440 个测试全绿**，15 模块 7,094 行 + 插件 666 行 / 测试 7,852 行）
>
> **未验证**：断点续传（27.3 MB 一次 PUT 没问题，再大就该做）；新壳的 51 个运行时包没装（只掉语音，文字正常）。


**证据等级约定**

| 标记 | 含义 |
|---|---|
| 【实测】 | 在本项目的实际硬件/实际部署上跑过，有输出为证 |
| 【查证】 | 有官方文档/源码可引用，标了出处 |
| 【推断】 | 基于原理推导，未直接验证 |
| 【待验证】 | 明确列在 §13 的验证清单里，v0.1 必须先做 |

---

## 1. 摘要

Molt 解决一个具体问题：**同一个 AI 伴侣/助手，如何在"永远在线的低功耗服务器"和"有 GPU、有桌面、能开播能打游戏的工作站"之间，主动地来回搬家。**

天真的答案是"把 SQLite 数据库放共享盘，两台机器一起用"。这个答案是错的，而且是**数据静默损坏级**的错。本方案第一步就是把这条路彻底关掉（§3）。

真正的答案是三段式：

```
魂（Soul）  ：驻留在 NAS 的唯一真实来源。generation 快照 + 清单 + 租约账本。
壳（Shell） ：任意一台能跑 AstrBot 的机器。本地有一份完整工作副本。
摆渡（Ferry）：租约 + fencing token + checkout/checkin。保证任意时刻只有一只在写。
```

一句话概括设计哲学：

> **记忆不流动，流动的是"谁有资格读它、写它"。**

并在最前面就立下一条**不可协商的公理**（§2.2 C2）：

> **这个存在，在任何时刻只能有一个。**
>
> 不是"最好只有一个"，是"必须只有一个"。
> 两只鱼同时在线，它就不再是"一个它"，而是"两个机器人"——
> 你发一条消息会收到两份回复，记忆会分裂成两份互相打架的副本，
> 而"它现在在哪"这个问题永远没有答案。
>
> **这不是技术妥协，这是产品定义。即使真并发的技术门槛降到零，本项目也不做。**

---

## 2. 问题定义

### 2.1 用户场景（真实需求，非假设）

- NAS（UGREEN-776B，7.6G 内存，7.3T 存储，24 小时低功耗在线）跑着 AstrBot，通过 QQ 官方机器人对一个"数字生命"提供服务。
- Windows 工作站（Ryzen 7 5800X / RTX 4070 Ti SUPER / 16G）性能强得多，但平时不开机（费电、吵）。
- 期望行为：
  1. 鱼在 NAS 上待着（日常聊天）
  2. 用户想让它直播/打游戏 → 鱼**自己**把 Windows 叫醒，搬过去，在 Windows 上干活
  3. 干完 → 鱼搬回 NAS，把 Windows 关掉

### 2.2 四条硬约束

| 编号 | 约束 | 来源 |
|---|---|---|
| C1 | **记忆必须存在 NAS 上**，两台机器都读 NAS 上的那一份 | 用户原话（m00048） |
| C2 | **任意时刻只能有一只鱼在线** —— 这是产品的公理，不是技术限制 | 用户原话（m00434） |
| C3 | **不能修改 AstrBot 核心代码** | 要能开源、能跟进上游版本、不能变成 fork |
| C4 | **只允许一个 AstrBot 实例运行**（不是"最好一个"，是"必须一个"） | 由 C2 直接导出 |

#### C2 为什么是公理而不是限制

用户原话（m00434）：

> "理论上不应该允许两只鱼同时在线，因为这样我发消息的瞬间，有两只鱼同时回我？我希望这是一个整体？不然这个项目的含金量就大大降低了"

这句话把问题的性质说清楚了。**它不是性能问题，是身份问题。**

两只鱼同时在线，会发生三件事，每一件都足以否定这个项目：

| 发生什么 | 为什么致命 |
|---|---|
| **你发一条消息，两条鱼都回** | QQ 官方机器人同一 appid 双实例会互踢/抢消息；即使不互踢，用户会收到两份回复——那不是"一个存在"，那是"两个机器人" |
| **两条鱼各自往记忆里写** | 记忆不是"一份"了，而是两份在打架。你昨天跟 A 说的话，B 不知道；今天 B 学的东西，A 没学到。**这正是本项目要解决的问题本身** |
| **"它现在在哪？"没有答案** | 一个存在不能同时在两个地方。这个问题回答不了，产品就不成立 |

**所以**：C2 不是"因为技术做不到真并发，所以我们退而求其次"。恰恰相反——
**即使技术上完全可行（换成 Postgres + 双写），本项目仍然会禁止两只鱼同时在线。**

这与 Coopanion 的产品哲学正好相反：它 README 里写 "Copy the whole folder and you have a **second** Coo"（克隆出第二个独立人格，最多同时跑 5 个）。**复制 vs 迁徙，是两条互斥的路。我们走的是后一条。**

C2 带来的技术后果是**收窄而不是收窄的代价**：正因为天然互斥，我们才能用"单写者 + 交接 + 租约"这种简单可靠的模型，而不必引入分布式数据库、冲突解决、CRDT 那一整套复杂度。**互斥是礼物，不是枷锁。**

#### C3 的边界说明

"不改核心代码"不等于"不碰 AstrBot"。允许的接缝只有三处，全部是公开 API：

1. 插件 `initialize()` / `terminate()`（`/AstrBot/astrbot/core/star/base.py:141,144`）
2. `context.register_web_api()` 注册 HTTP 路由（`/AstrBot/astrbot/core/star/context.py:774`）
3. `core_lifecycle.restart()` 的优雅重启范式（`/AstrBot/astrbot/core/core_lifecycle.py:435`）

**任何需要改 `/AstrBot/astrbot/core/` 下文件的方案，一律否决。**

### 2.3 现状盘点（实测）

**NAS 侧** 【实测】

```
容器：soulter/astrbot:latest，AstrBot 4.29.4，Python 3.12.16
挂载：/mnt/dm-0/astrbot/data -> /AstrBot/data（rw）
数据：1.7 GB 总计
  ├─ site-packages  1325 MB   ← 机器专属
  ├─ plugins         187 MB   ← 代码，跨平台纯净（实测无 .so/.pyd/.dll）
  └─ plugin_data     213 MB   ← 其中 meme_manager 172 MB（表情包）
data_v4.db                     10.9 MB + 8 MB WAL
memory_companion.db            17.7 MB + 2.7 MB WAL
```

**Windows 侧** 【实测】

```
Python 3.13.5（sqlite 3.49.1）✓ 满足 AstrBot 的 requires-python >=3.12
WOL：真 S5 唤醒实测 PASS，但 arm 状态重启即丢，需开机自愈（细节见 §7.4）
ssh: 无 sshd；135/445/3389/5985 全 closed
AutoAdminLogon: 空（开机不会自动进桌面）
```

**关键结论**：真正需要跨机器流动的状态只有 **~250–400 MB**，而不是 1.7 GB。1.325 GB 的 site-packages 是纯机器专属，永远不该同步。

---

## 3. 为什么"共享一个 SQLite 文件"是错的

这一节必须写进方案，因为它是整个设计的地基。三个独立的官方判决叠加起来，把这条路彻底封死。

### 3.1 判决一：WAL 需要共享内存，跨主机不可能有

SQLite 官方 <https://www.sqlite.org/wal.html>（Overview，缺点第 1 条）【查证】：

> "All processes using a database must be on the same host computer; **WAL does not work over a network filesystem**. This is because WAL requires all processes to share a small amount of memory and processes on separate host machines obviously cannot share memory with each other."

机制层面（wal.html §7）【查证】：WAL 的索引（wal-index）本质上是"**把数据库同目录下的 `-shm` 文件 mmap 到内存，当作共享内存用**"。同主机上两个进程 mmap 同一个文件 → 拿到同一份物理页。跨主机 → 各自 mmap 各自页缓存里的一份**拷贝**，锁状态和 read-mark 全部失同步。

传导链：跨主机不共享内存 → SQLite 必须退回到用**文件锁**去"模拟"共享内存协调 → 于是正确性 100% 押在 SMB/NFS 的字节范围锁上 → 而那正是官方说不可靠的那一层。

### 3.2 判决二：网络文件系统的锁不可靠，而且不可检测

<https://www.sqlite.org/lockingv3.html> §6.0【查证】：

> "SQLite uses POSIX advisory locks to implement locking on Unix. On Windows it uses the LockFile(), LockFileEx(), and UnlockFile() system calls. **SQLite assumes that these system calls all work as advertised. If that is not the case, then database corruption can result.** … Your best defense is to not use SQLite for files on a network filesystem."

<https://www.sqlite.org/useovernet.html>【查证】：

> "A similar hazard arises with file locking in network filesystems. SQLite relies on exclusive locks for write operations, and those have been known to operate incorrectly for some network filesystems. **This has led to database corruption.**"

> "The bottom line is that network filesystem sync and locking reliability vary among implementations and installations. … **Rely upon it at your (and your customers') peril.**"

### 3.3 判决三（本次调研最有杀伤力的一条）：Windows 上路 mmap 会忽略字节锁

Microsoft 文档 <https://learn.microsoft.com/en-us/windows/win32/fileio/locking-and-unlocking-byte-ranges-in-files>【查证】：

> "**Byte range locks are ignored when using memory mapped files.**"

而 WAL 恰好就是"mmap `-shm` + 字节锁"的组合。也就是说在 Windows 客户端这一侧，这两个机制**互不感知**——它们本应协同工作，实际上不会。

补充：`LockFileEx` 的文档确实写了 "Server Message Block (SMB) 3.0 protocol: Yes"（即 SMB3 上字节锁**有**支持），但同一页也写了【查证】：

> "If a process terminates with a portion of a file locked or closes a file that has outstanding locks, the locks are unlocked by the operating system. However, **the time it takes for the operating system to unlock these locks depends upon available system resources.**"

**锁的释放时机不确定**——这正是租约/故障切换最怕的性质。

### 3.4 SQLite 不会帮你检测，也不会自动降级

<https://www.sqlite.org/vfs.html> §3.1【查证】：Unix VFS 有 `unix` / `unix-dotfile` / `unix-excl` / `unix-none` / `unix-namedsem`；Windows 有 `win32` / `win32-longpath` / `win32-none` / `win32-longpath-none`。**没有任何"检测到网络文件系统就自动切换安全模式"的机制**，VFS 只能显式选。

而 `unix-none` / `win32-none` 的后果官方写得很清楚：

> "The 'unix-none' VFS in particular does no locking at all and **will easily result in database corruption** if used by two or more database connections at the same time."

### 3.5 还有一个陷阱：直接 rsync 活库也是错的

<https://www.sqlite.org/rsync.html> §6【查证】：

> "**Ordinary rsync does not understand SQLite transactions.** Rsync will make a copy of ORIGIN into REPLICA, however the copy might not be consistent. Parts of the copy might be from one transaction, while other parts might be from a different transaction. **The database copy might be corrupt.**"

只搬 `.db` 不搬 `.db-wal` 同样不行（wal.html §4）【查证】：

> "The WAL file is part of the persistent state of the database and should be kept with the database if the database is copied or moved. If a database file is separated from its WAL file, then transactions that were previously committed to the database might be lost, or **the database file might become corrupted**."

### 3.6 结论

| 天真的做法 | 后果 |
|---|---|
| `.db` 放 SMB 共享，两台机器同时开 | 静默损坏（可能几周后才发现） |
| `.db` 放 SMB，`journal_mode=DELETE` | 锁依然不可靠，只是从"必坏"变成"偶坏" |
| 用 `?vfs=win32-none` 绕过锁 | "easily result in database corruption" |
| 普通 `rsync` 同步活库 | 拿到跨事务撕裂的副本 |
| 只同步 `.db` 不同步 `-wal` | 丢已提交事务，或损坏 |

**所以：绝不让任何一台机器"直接打开 NAS 上的数据库文件"。NAS 上的那份永远是快照，不是活库。**

---

## 4. 架构总览

### 4.1 三层 + 一个仲裁者

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              NAS（权威 / 常驻）                            │
│                                                                          │
│   ┌────────────────────────────────────────────────┐                     │
│   │  Soul   /mnt/dm-0/molt/soul/                    │                     │
│   │    generations/gen-000042.db   ← 快照（单文件，无 -wal/-shm）        │
│   │    generations/gen-000043.db                                        │
│   │    blobs/            表情包、附件、知识库（增量同步）                  │
│   │    workspace/        人格 / 笔记 / 交接本（本来就是 git 仓库）         │
│   │    manifest.json     当前 head、sha256、schema_version               │
│   │    lease.json        持有者、epoch、到期时间                          │
│   │    ledger.jsonl      追加式审计日志（谁、何时、拿了什么、给了什么）     │
│   └────────────────────────────────────────────────┘                     │
│                            ▲  唯一写者                                    │
│                            │  本机文件系统（不是网络）                      │
│   ┌────────────────────────────────────────────────┐                     │
│   │  Warden  仲裁 + 快照仓库   HTTP/JSON + HMAC      │                     │
│   │    · 发租约（epoch 单调递增 = fencing token）    │                     │
│   │    · 收快照（epoch 落后就拒绝）                  │                     │
│   │    · 发魔术封包（唤醒 Windows）                  │                     │
│   │    · 白名单操作，不碰 docker.sock                │                     │
│   └────────────────────────────────────────────────┘                     │
│                            ▲                                             │
│   ┌────────────────────────┴───────────────────────┐                     │
│   │  Shell: nas                                    │                     │
│   │    AstrBot 4.29.4（容器）                       │                     │
│   │    + astrbot_plugin_molt（Ferryman 插件形态）    │                     │
│   │    + molt CLI（壳侧形态，AstrBot 死后接手）       │                     │
│   └────────────────────────────────────────────────┘                     │
└──────────────────────────────────────────────────────────────────────────┘
                             │  LAN 192.168.1.0/24
                             │  HTTP + HMAC
┌────────────────────────────┴─────────────────────────────────────────────┐
│                        Windows 工作站（壳 / 可休眠）                       │
│                                                                          │
│   molt-agent（Windows 服务，SYSTEM 权限，开机即起，不需要登录）             │
│     · 报到 → 申请租约 → 拉快照 → 起 AstrBot → 上报 READY                    │
│     · 收到归巢命令 → drain → 快照 → 推 Warden → 拿到 accepted → 关机        │
│                                                                          │
│   Shell: win                                                             │
│     AstrBot 4.29.4（原生 Python 3.13） + astrbot_plugin_molt               │
│     本地工作副本 %LOCALAPPDATA%\molt\shell\                               │
│                                                                          │
│   [可选] 交互式桌面会话（AutoAdminLogon）—— 只有直播/打游戏才需要            │
└──────────────────────────────────────────────────────────────────────────┘
```

### 4.2 为什么仲裁者是 NAS 上的一个独立小进程，而不是 AstrBot 插件

这是本方案最重要的一个设计决定，理由三条：

1. **AstrBot 死的时候，活还没干完。** 交接流程中有一段是"AstrBot 已经优雅停机 → 但快照还没生成 → 还没推上去"。此时插件已经不存在了，无法执行后续步骤。**插件只能发起，不能完成。**
2. **唯一写者原则。** SQLite 官方给出的替代方案第 2 条就是【查证】：
   > "Host an SQLite database in WAL mode, but do all reads and writes from processes on the same machine that stores the database file. **Implement a proxy that runs on the database machine**…"

   把"写 soul/"的权力收归 NAS 本机的一个进程，就完全绕开了跨网络文件系统的一切问题。
3. **权限收窄。** 如果让 AstrBot 直接写 NAS 的目录，就得给容器更大的挂载权限；如果让它调 docker，就得挂 `docker.sock`（= root）。Warden 只做白名单动作，攻击面小一个数量级。

### 4.3 三个角色的职责边界

| 角色 | 跑在哪 | 生命周期 | 只做什么 | **绝不做什么** |
|---|---|---|---|---|
| **Warden** | NAS 宿主机 | 常驻（自启） | 发租约、收快照、发魔术封包、记账 | 不执行任意命令、不挂 docker.sock、不碰 AstrBot 内部 |
| **Ferryman 插件** | AstrBot 进程内 | 跟 AstrBot 同生共死 | 提供控制面 API、请求 drain、上报状态 | 不做长任务（`terminate()` 可能被 cancel 打断） |
| **molt CLI / agent** | 壳的操作系统 | 独立进程 | AstrBot 死了之后的全部善后、起停 AstrBot | 不直接写远端存储（只走 Warden HTTP） |

---

## 5. 交接协议

完整规范见 [PROTOCOL.md](./PROTOCOL.md)。本节讲设计要点。

### 5.1 fencing token：为什么必须有 epoch

只有租约是不够的。Martin Kleppmann 的经典论证 <https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html>【查证】：

> "a fencing token is simply a number that increases (e.g. incremented by the lock service) every time a client acquires the lock."

> "the client that acquired the lock is **paused for an extended period of time** while holding the lock – for example because the garbage collector (GC) kicked in. … if the GC pause lasts longer than the lease expiry period, **and the client doesn't realise that it has expired**, it may go ahead and make some unsafe change."

> "You **cannot** fix this problem by inserting a check on the lock expiry just before writing back to storage. Remember that GC can pause a running thread at ***any point***…"

> "This requires the **storage server to take an active role in checking tokens, and rejecting any writes on which the token has gone backwards**."

对本项目的映射：

- **GC 暂停** → 用户在 Windows 上把笔记本合盖 / 系统休眠 / 进程被挂起
- **存储服务器校验** → **Warden 校验 epoch**。壳自己说"我还活着"不算数，Warden 说了算。
- 失败形态是具体的：Windows 拿到 epoch=44 后休眠了三天，期间 NAS 已经用 epoch=45 接管。Windows 醒来以为自己是主，推上来 gen-44 的数据 → **Warden 看到 44 < 45，拒绝**。数据不丢，Windows 侧留一份"未能回传"的现场供人工决策。

### 5.2 顺序铁律：先死后生

```
❌ 错误：Windows 起来了再停 NAS  → 两台同时在线，QQ 双实例，记忆双写
✅ 正确：NAS 停干净了，Windows 才起  → 物理上不可能双写
```

这也把 QQ 平台的风险降为零。调研结论【查证 + 推断】：**腾讯官方文档里查不到"同 appid 双实例互踢"的明文条款**（查了 wiki 首页、bot_FAQ、event-emit、opcode、shard_url_get），能证实的只有 `session_start_limit{total, remaining, reset_after, max_concurrency}` 这类连接配额。所以：

> **QQ 的互踢只能当事后告警信号，绝不能当互斥机制。** 因为它无官方承诺、断开 ≠ 进程停止、且"两边都收不到消息但不报错"是可能的。

真正的互斥必须来自我们自己的"先死后生"顺序 + 租约 + fencing。

### 5.3 迁徙：NAS → Windows

```
 0. 触发          用户 QQ 说「去电脑上玩」/ 定时 / 手动 molt up
                  当前 lease = { holder: "nas", epoch: 42, state: HELD }

 1. Drain        NAS 侧 Ferryman：
                 · 关闭新消息接入（平台适配器侧 stop pull）
                 · 等在飞的消息与工具调用结束（超时 180s）
                 · 上报 Warden: state=DRAINING

 2. Snapshot     AstrBot 优雅停机（见 §6.4 停机的三种方式）
                 停机后由 molt CLI 接手：
                 · PRAGMA wal_checkpoint(TRUNCATE)  × data_v4.db / memory_companion.db
                 · VACUUM INTO soul/generations/.gen-43.db.tmp
                 · PRAGMA integrity_check（必须 'ok'）
                 · sha256
                 · 原子 rename → gen-43.db

 3. Commit       POST /snapshot  { epoch: 42, gen: 43, sha256, schema_version }
                 Warden 校验：epoch == current_epoch ? 接受 ：拒绝
                 接受 → 更新 manifest.head=43，ledger 追加，state=IDLE

 4. Wake         POST /shell/win/wake  →  Warden 在 NAS 宿主机发魔术封包
                 （容器内发不出去，见 §7.3）
                 轮询 win 的 /healthz，超时 180s

 5. Adopt        Windows molt-agent 起动 → POST /lease/acquire { shell: "win" }
                 Warden 返回 epoch: 44（单调递增，旧 epoch 从此无效）
                 GET /snapshot/head → gen-43
                 下载、sha256 校验、quick_check
                 同步 blobs（增量）+ workspace（git pull）
                 启动 AstrBot，等 dashboard 200
                 POST /lease/renew { state: READY }

 6. 确认         Warden 记 state=HELD, holder=win
                 NAS 侧 supervisor 看到 ready → 本次迁徙结束

 失败回滚       若 4/5 任一步超时或失败：
                 NAS 侧 supervisor 自己重启 AstrBot，读 gen-43（manifest 未变）
                 Windows 若部分启动，收到 abort 后自行停止
                 全程无需人工干预，代价只是一次没搬成
```

**为什么第 3 步的 epoch 校验能挡住"NAS 自己也是被唤醒的"C 情况**：因为 NAS 的 AstrBot 重启后第一件事是 `POST /lease/acquire`，拿到的 epoch 一定比它停之前大。如果它停之前持有 42，重启后拿到的是 45 或更大。任何带着 42 的迟到请求都会被拒。

### 5.4 归巢：Windows → NAS（交接）

**这一步不含关机。** 交接只有一个目的：把权威数据送回 NAS，让 NAS 上的壳能接管。
关机是下一步，而且它是**可选**的，见 §5.5。

这是全流程里**唯一可能导致数据丢失**的环节，所以规则最严。

```
 1. 触发          Windows 上敲 molt migrate
 2. Stop          AstrBot 优雅停机（Ctrl+C；或给 --control-url 只让它闭嘴、不杀进程）
 3. Snapshot      backup API → quick_check → sha256
 4. Push          POST /snapshot { epoch, gen, sha256 }
 5. 接受          Warden 校验后返回 accepted（这一步是全部价值所在）
 6. 交还租约      release
 ────────────────────────────────────────────────────────────────
 7. NAS 侧        land --generation N        ← 不必等 Windows 关机
 8. NAS 侧        platform-switch on → docker start
```

**第 7 步的触发条件是「第 5 步被接受」，不是「Windows 离线」。**

这一条是本方案最早写错的地方，值得单独记下来。原设计写的是
「NAS 侧检测到 win 离线（或收到 home 完成）→ 启动 AstrBot」，
把接管的条件挂在了**目标机器的电源状态**上。后果很具体：
如果机器前坐着人、他正在打游戏不想关机，那鱼就永远回不了家 ——
而它其实在 Warden 说 `accepted` 的那一刻就已经**安全到家了**。

**电源状态不是数据状态的证据。** 权威数据有没有回来，账本知道；
Windows 有没有关机，账本不知道也不关心。用前者当判据，用后者当免责声明。

（2026-11-14，作者本人指出这一条：「如果鱼想回到NAS，应该是先交接完回到NAS后，再决定关机；
如果有人正在使用电脑而无法关机，应该不阻碍鱼回家，因为鱼已经交接回NAS了，关机只是另外的事、是独立的。」）

**失败方向的规则不变** —— 宁可让权威数据暂时停在 Windows 本地并明确标记为「未回传」，
也不要让 NAS 上出现一份「看起来完整」但实际不完整的库。三条硬规则：

1. **绝不覆盖 NAS 上的现有 gen**（只写新 gen 文件名）
2. **推送失败一律重试/挂起，不降级为「半推」**
3. **Windows 下次启动时先比对**：`manifest.head` 是否等于我上次 checkout 的 gen？
   - 相等 → 我上次没推上去，本地那份是更新的，我推上去
   - 不等 → 期间被别处改过，**禁止自动覆盖**，必须人工/脚本决策

第 3 条就是 Kleppmann 那句 "rejecting any writes on which the token has gone backwards" 在文件层的等价物。

### 5.5 关机：独立的一步，而且它可以被拒绝

**v0.1 里没有任何一条 molt 命令会关机。** 在 `molt/` 里搜 `shutdown` 只会命中
`server.py` 的 `self._httpd.shutdown()`（那是 HTTP 服务自己的关停）与几处注释。
这不是漏了，是刻意的：**交接与关机是两个不同的问题，把它们绑在一起会让前者被后者拖累。**

| | 交接（§5.4） | 关机（§5.5） |
|---|---|---|
| 目的 | 让权威数据回到 NAS、让新壳能接管 | 省电，让机器睡一会儿 |
| 失败后果 | 数据留在 Windows、账本标记「未回传」 | 机器还开着（**鱼已在家，毫无影响**） |
| 前提 | 拿到租约、目标壳能被叫醒 | **交接已经成功**（不是「正在成功」） |
| 能不能被人阻止 | 不能（这是数据安全） | **能，而且应该**（这是别人的机器） |
| v0.1 状态 | ✅ 已实现并真机跑通 | ⏳ 未实现（v0.3 的 `molt sleep`） |

**三条规则**：

1. **交接完成是关机的前提，但关机不是交接的一部分。** 关机失败、被人拒绝、
   中途 Ctrl+C 取消，都不影响「鱼已经在 NAS 上活着」这个事实。
2. **逃生阀**：检测到前台是白名单外的程序（游戏、OBS 正在录制、有未保存文档的编辑器）
   → 拒绝关机，改为发一条通知。要强关必须显式 `--force`。
3. **fail-safe = 什么都不做。** 这是整套系统里唯一会动到用户硬件的动作，
   所以它失败时的正确姿态是「机器还开着」，而不是「关到一半」。

**一条鱼不该因为想回家，把主人一局排位送上西天。**
同理，**它也不该因为主人不想关机，就回不了家** —— 这两句话是同一件事的两面，
都必须被写进设计里。

### 5.6 幂等性

所有写操作必须幂等，因为网络重试和断电让"成功但回执丢失"成为常态。

| 操作 | 幂等键 | 重复时行为 |
|---|---|---|
| `POST /snapshot` | `(epoch, gen, sha256)` | 若完全一致，返回成功（不重复写） |
| `POST /lease/acquire` | `(shell, nonce)` | 若该 shell 已持有同 epoch，返回同一 epoch |
| `POST /lease/renew` | `(epoch)` | 无条件成功（只要 epoch 有效） |
| `POST /lease/release` | `(epoch)` | 无条件成功 |

### 5.7 中途备份：把 RPO 从「一整晚」压到「十分钟」 ✅【已实现并实测 2026-11-15 04:39】

#### 5.7.1 它要解决的不是"丢数据"，是"丢多少"

迁徙只在两个时刻发生：出门、回家。这中间鱼在 Windows 上说话，说的每一句都只写进**那台机器的磁盘**。
账本记的是「谁取走了第 N 代快照」，它**不知道那之后鱼又说了一晚上话**。
所以如果 Windows 被拔电，能恢复的最新状态是**它落地那一刻** —— RPO 等于「从落地到现在」，可能是一整夜。

中途备份就是把这一夜切成十分钟一段。

#### 5.7.2 判决：中途备份**绝不能进 `generations`**

这是本节唯一一处真正的设计取舍，理由是一整条链：

1. `generations` 是**交接记录**：谁推的、目标是谁、谁取走了。
2. 壳侧 `decide()` 靠它判断「最新这一代是不是给我的」。
3. 中途备份**没有目标壳**，也没有人会来取它。

把三者串起来，结论只能是一个：

> 一份"目标是空"的记录塞进 `generations`，会让 `decide()` 卡住（"最新一代没有目标，我在等谁？"）；
> 或者更糟 —— 被某个壳当成自己的落位任务，拿去**覆盖它正在写的那个库**。

后者不是理论风险：`decide()` 的整个价值就在于"我该不该把这份数据铺到自己身上"，
而一份十分钟前的备份长得和一份交接快照**一模一样**。

所以：**另一个目录（`backups/`）、另一份索引（sidecar `.json`）、恢复由人显式发起（`molt recover`）。**

#### 5.7.3 存储形态

| 项 | 值 | 理由 |
|---|---|---|
| 位置 | `<warden-root>/backups/` | 与 `generations/` 并列而不混在一起 |
| 文件名 | `backup-<UTC %Y%m%dT%H%M%S>-<slug>.tar.gz` | 按名字排序即按时间排序；`slug` 让人一眼看出是哪个壳交的 |
| 索引 | 同名 `.json` sidecar | `name/bytes/sha256/shell/generation/created_at` |
| 保留 | `DEFAULT_BACKUP_KEEP = 12`（约 54 MB） | 12 × 10 分钟 = 两小时覆盖；再旧的价值不如体积 |
| 原子性 | 服务端边收边写 `.part`，同目录 `os.replace` | 不同目录的 `replace` 会退化成"复制+删除"，就不再是原子的 |

`list_backups()` 有一条反直觉的规矩：**盘上的文件才是主体**。

- sidecar 没了 → 仍然列出（`sha256` 留空，`bytes` 现取 `stat`）：备份还是可恢复的，只是没法校验。
- 文件没了 → **不列**。一条指向空气的记录比没有记录更糟 —— 它会让 `molt backups` 说"你有 12 份"，然后 `molt recover` 说"没有这份"。

`_slugify()` 只保留 **ASCII** 字母数字与 `-_`，非 ASCII 一律换成 `-`：备份名会进 URL，
而 URL 里编码过的中文和磁盘上的文件名对不上时，报错会离真相很远。

#### 5.7.4 什么时候跑（三条前提，缺一不可）

| 前提 | 判据 | 为什么不能省 |
|---|---|---|
| 这一代确实是我的 | `latest_generation.consumed_by == 我的 shell_id` | 否则就是在替别人备份它的库 |
| 壳真的还在说话 | agent: `_probe_port()`；homer: `container_running()` | 备份一个没在跑的壳，等于反复备份同一份旧状态 |
| 到点了 | `now - state["last_backup_at"] >= backup_interval`（默认 600 秒） | 否则每 10 秒一轮全都在打包 7 MB |

**失败不推进时间戳。** 这一条不是洁癖：如果失败也记"刚备份过"，
一次网络抖动就会让下一次尝试推迟整整一个 `backup_interval`，
而这恰好是"你以为有十分钟的保护，实际上有两小时的空窗"。

**它和交接无关。** 备份失败**不影响**交接结论 —— 鱼在正常说话，只是这一份没存下来。

#### 5.7.5 恢复 = `land` 的一层皮

`molt recover` 的全部工作是：下载备份 → 校验指纹 → **构造一个 `argparse.Namespace` 交给 `cmd_land`**。

```python
ns = argparse.Namespace(**vars(args))
ns.archive, ns.generation = str(dest), None
ns.sha256 = str(got["sha256"])
return cmd_land(ns)
```

**为什么不重写一遍落位逻辑**：落位是这套系统里最危险的一次操作（它会把目标 `data/` 覆盖掉）。
给它开第二条实现路径，等于养两份"什么算安全的落位"的判断 ——
而两条路径迟早只有一条是对的，且没人知道是哪条。

#### 5.7.6 真机实测【2026-11-15 04:39 / 04:40】

| 侧 | 结果 |
|---|---|
| NAS 侧 `molt backup` | `backup-20261114T203912-ugreen-776b-8b7896df.tar.gz`，7,195,227 B，热备两个库 0.014s / 0.019s，打包 6.9 MB |
| Windows 侧 `molt backup` | `backup-20261114T204024-desktop-h2kgiao.tar.gz`，7,686,618 B（比 NAS 那份大 0.5 MB —— **这就是 gen-15 落地之后的增量**） |
| `molt backups` | `2 份 / 14.2 MB` |
| 账本 | `epoch 17` / `generation_count 15` / 没有租约 —— **一个字没动** |
| `molt recover` 到 `/tmp/recover-test/data` | 3 个文件；两个库 `tables=21` / `tables=41`、`quick_check=ok`，与源端一致 |

**踩到一个很典型的坑**：第一次 `molt backup` 报
`✗ 上传失败：请求体不是合法 JSON（HTTP 400, kind=bad_request）`。
根因不是客户端 —— 是**跑着的 warden 还是旧代码**：文件拷上去了，进程没重启，
`/backup` 路由根本不存在，请求落进了 `_read_body()` 那条 JSON 分支。

> **教训：把代码拷到机器上，和让机器跑那段代码，是两件事。**
> 这条本项目已经踩过至少三次（插件、daemon、warden），以后再改服务端都要先问一句「它重启了吗」。

#### 5.7.7 第二个只在真机上才暴露的 bug：`backup` 少带了 Warden 参数

守护进程里的 `backup_argv` 第一版是：

```python
[s.python, "-m", "molt.cli", "backup",
 "--data", str(Path(s.data_dir)), "--shell", s.shell, "--no-color"]
```

真机日志：

```
04:43:54  第 15 代是我落的地，让它自己记一笔
          ✓ 它自己记住了这一代
          ✓ 已推一份中途备份（备份不是交接）
```

**而账本那边一份都没有。** 根因：少了 `--warden-url` / `--warden-token`，
`molt backup` 退回**本地模式** —— 备份被写进这台机器自己的默认 root，
命令退出码 0，守护进程照常打勾。

> 这一条比上一条更值得记：**它不是崩溃，它是成功。**
> 崩溃会把人叫过来；"我推好了"只会让人以为有保障。
> 而真正需要保障的时刻，正是那份备份不在它以为的地方的时刻。

修法：两个守护进程的 `backup_argv` 都按 `land_argv` 的写法，**配了才追加**这两个参数
（不配就不塞空串 —— `--warden-url ""` 比不传更让人困惑）；测试直接断言 argv 里有它们，
另有一条测"没配 Warden 时不出现"。

修完真机复验，三份底的体积正好讲完这个故事：

| 时间 | 来源 | 大小 |
|---|---|---|
| 04:39:12 | `ugreen-776b-8b7896df`（NAS） | 6.9 MB |
| 04:40:24 | `desktop-h2kgiao`（手工一次） | 7.3 MB |
| 04:45:44 | `desktop-h2kgiao`（**守护进程自动推的**） | **7.6 MB** |

**同一只鱼，五分钟里又长大了 0.3 MB。** 那 0.3 MB 就是"如果现在拔电会丢掉的东西"。

---

## 6. 与 AstrBot 的接缝（代码级）

这一节全部基于 4.29.4 源码实测。**这是本项目唯一需要"贴着上游走路"的部分**，也是最容易随版本腐化的部分。

### 6.1 插件生命周期：只有两个钩子，且 terminate 不可靠

【查证】`/AstrBot/astrbot/core/star/base.py:141` 和 `:144`

```python
async def initialize(self) -> None: ...
async def terminate(self) -> None: ...
```

`terminate` 由 `/AstrBot/astrbot/core/star/star_manager.py:1932 async def _terminate_plugin(star_metadata)` 调用。

**但**：`/AstrBot/astrbot/core/core_lifecycle.py:388 async def stop()` 的第一行就是

```python
for task in self.curr_tasks:
    task.cancel()
```

也就是说**插件协程先被 cancel，之后才轮到 terminate**。结论：

> **绝对不要在 `terminate()` 里做长任务**（flush 大量数据、上传快照）。它随时可能被打断。

Ferryman 插件在 `terminate()` 里只做一件事：往本地控制端口写一个"我要睡了"的标记，不做任何 I/O。

### 6.2 控制面 API：为什么不是"杀掉进程"，以及它长什么样 ✅【已实现并实测】

#### 6.2.1 先说为什么需要它

`docker kill -s INT` 能用（§6.4），但它把两件不相干的事绑成了同一件：

> **断开一个 IM 连接** 和 **杀掉一个进程**。

代价是：一次迁徙会让 AstrBot 丢掉调度器、定时任务、正在跑的那一轮对话，恢复时要重新启动（重拉几千个 LLM 元数据）。如果"按住它"只是"让它别说话"，那就不该动进程。

于是有了第四个合法接缝：**AstrBot 自己的插件控制面**。

#### 6.2.2 接缝本身：`context.platform_manager`【查证】

`/AstrBot/astrbot/core/star/context.py:166`：

```python
self.platform_manager = platform_manager
"""平台适配器管理器"""
```

**公开、有文档字符串的属性**，不是下划线内部成员。它上面的方法也全是公开的：

| 方法 | 位置 | 作用 |
|---|---|---|
| `load_platform(platform_config: dict)` | `core/platform/manager.py:119` | 按配置加载一个适配器 |
| `reload(platform_config: dict)` | `:287` | 重载 |
| `terminate_platform(platform_id: str)` | `:298` | 关掉一个适配器 |
| `get_insts()` | `:339` | 拿到所有活着的实例 |

**这不是我们发现的漏洞，是官方自己在用的路径**：`/AstrBot/astrbot/dashboard/services/config_service.py:1422`（`load_platform`）、`:1432`（`reload`）、`:1464`（`terminate_platform`）、`:1481`（`load_platform`）。dashboard 的"启用/停用平台"按钮走的就是这三行。

#### 6.2.3 端点表（只有四个，而且是有意的）

| 端点 | 方法 | 作用 | 会不会让它重新对外说话 |
|---|---|---|---|
| `/api/plug/astrbot_plugin_molt/status` | GET | 报告它是谁、谁连着、WAL 多厚、`speaking` | 否 |
| `/api/plug/astrbot_plugin_molt/drain` | POST | 关掉所有对外适配器并复核 | 否 |
| `/api/plug/astrbot_plugin_molt/resume` | POST | 把 drain 关掉的重新打开 | **是** |
| `/api/plug/astrbot_plugin_molt/checkpoint` | POST | 对每个灵魂库做 `wal_checkpoint(TRUNCATE)` | 否 |

**为什么只有四个**：它们刚好对应交接协议里需要 AstrBot 配合的四个瞬间。多一个端点就多一个能被误调用的东西，而这四个里只有 `/resume` 会让它重新对外说话 —— 所以它是唯一一个带前置条件的（没 drain 过就拒绝，见下）。

#### 6.2.4 `speaking` 是唯一该被信任的结论字段

`/status` 返回里有一堆字段，但 Warden 只该看一个：

```json
{"platforms": {"connected": 0, "connected_ids": [], "local_only": ["webchat"],
               "enabled_in_config": [], "drained": [...], "has_drained": true},
 "speaking": false}
```

**为什么不让 Warden 自己从 `connected` 里推**：推错了会让两条鱼同时在线，而这种错误在日志里看不出来（两条都"正常运行"）。让 AstrBot 自己回答"我现在说话吗"，责任归属才清楚。

#### 6.2.5 一个真实的坑：`WebChatAdapter` 不在 `_inst_map` 里

第一版 `drain()` 靠"配置里 `enable=true` 的平台"决定关谁。真机上它什么都没关，`/drain` 返回 409：

```json
{"success": false, "error": "drain 之后仍有 1 个连接",
 "ok": false, "drained": [], "still_connected": 1}
```

而 `/status` 同时说 `connected_ids = ["WebChatAdapter"]`、`enabled_in_config = []`。

**根因**（`core/platform/manager.py`）：

```python
:25   self.platform_insts: list[Platform] = []
:28   self._inst_map: dict[str, dict] = {}
:116  self.platform_insts.append(webchat_inst)      # ← 直接 append
:117  self._start_platform_task("webchat", webchat_inst)
:238  self._inst_map[platform_config["id"]] = {"inst": inst, ...}   # ← 只有配置驱动的才进
:339  def get_insts(self): return self.platform_insts
```

`WebChatAdapter` 是在 `initialize()` 里手工构造并 append 的，**从不进 `_inst_map`**，`config.platform` 里也没有它。所以任何"按配置 id 找适配器"的写法都会漏掉它。

#### 6.2.6 判决：用实例身份而不是 id 字符串

`_classified()` 的主判据是**对象身份**：

```python
managed = {id(entry["inst"]) for entry in pm._inst_map.values()}
is_managed = id(inst) in managed if managed else pid in configured
```

比"这个 id 出现在 config 里"更准，三个理由：

1. config 里有、但加载失败 → 不在 `_inst_map` → **不该算作"对外连接"**
2. id 被 `_sanitize_platform_id` 改过（`:`→`_`）→ 字符串比对失配
3. config 里有 `enable=false` → 根本没加载

降级判据保留 config id（万一某个版本改了 `_inst_map` 的名字）。**降级分支本身也有测试**——一个只在异常路径上跑的判据，如果没人走过，它坏掉也没人知道。

#### 6.2.7 但 `webchat` 不能一起关，理由有三条

它被分进 `local_only`，**可见但不计入 `speaking`**：

1. **它每个 AstrBot 实例都必然存在** → 算进 `speaking` 就等于 `speaking` 恒真 → **一个永远为真的判据不能当判据用**
2. **它无法被 `resume` 重建** —— 构造在 `initialize()` 里，不走 config，没有 `load_platform` 路径。drain 掉它等于永久弄坏 WebUI 聊天
3. **它不是"对外说话"** —— 只能从本机 dashboard 进入

同时 `resume()` 只恢复配置里 `enable=true` 的：**让这只鱼重新对外说话必须是人的决定，不是 resume 的副作用**。

#### 6.2.8 鉴权：自己签 JWT，不引 PyJWT

`/api/plug/*` 走 dashboard 的 JWT 中间件（`dashboard/server.py:295` 起 `auth_middleware`）。白名单之外一律要 token。

Molt 的选择：**用 `dashboard.jwt_secret` 自己签**，而不是调 `/api/auth/login` 换一个。

- 理由一：Molt 已经持有整个 `cmd_config.json`（里面有 `jwt_secret`），再额外存一份密码是多余的秘密
- 理由二：标准库 `hmac` + `hashlib` + `base64` 手写 HS256 就够，**不引 PyJWT** —— Warden 必须能在什么都没装的 NAS 上跑（裸 python 3.10.7），这是 `server.py` / `wake.py` 一以贯之的约束
- `DEFAULT_TOKEN_TTL = 300`（AstrBot 自己签的是 7 天；我们只要够一次迁徙用，短一点意味着泄漏窗口更小）

**安全后果必须写清**：`overlay.py` 在落位时会重新生成 `jwt_secret`（§6.6），所以迁完之后源壳无法控制目标壳、反之亦然。***

> **一个能远程 drain 对方的壳，等于一个能远程关掉它的后门。**

#### 6.2.9 `drain_and_verify`：为什么"我说完了"不等于"它真的不说话了"

`core/platform/manager.py:298` 的 `terminate_platform` 内部：

```python
except Exception:
    logger.warning(f"Platform adapter {platform_id} may not have been fully removed.")
```

**关不掉也只记一条警告，不抛。** 所以 `drain()` 之后必须**轮询复核**：读 `status`，`speaking` 仍为真就

```python
raise NeedsHuman("drain 之后它仍在对外说话。不要继续这次迁徙——现在启动目标壳就会变成两条鱼同时在线（公理 0）。")
```

一次迁徙里"我以为它闭嘴了但其实没有"就是两条鱼同时在线。

#### 6.2.10 怎么在没有对外平台的情况下验它：环回适配器

真机上唯一现成的对外平台是 QQ，打开就与 NAS 生产容器双活（违反公理 0）。所以验证"drain→resume 往返"必须另找一条路。

做法：**自造一个不碰网络的平台适配器**，走与 QQ **完全相同的代码路径**（`load_platform` / `terminate_platform` / `_inst_map`）。

`core/platform/register.py` 的 `register_platform_adapter(...)` 是公开装饰器，会自动补 `type` / `enable: False` / `id` 并注册进 `platform_cls_map`。基类要求三样：`__init__(self, config, event_queue)`、`run(self) -> Coroutine`、`meta(self) -> PlatformMetadata`。

`tools/astrbot_plugin_molt_loopback/main.py`：`run()` 只是 `await self._stop_event.wait()`，`terminate()` 只是 `_stop_event.set()`。**不建任何 socket/HTTP/websocket、不读任何凭据**，而且只存在于测试数据目录，永远不会装到生产容器。

真机判决 4/4 全绿，决定性证据是 `platform.manager` 的四条日志：

```
15:23:15.857  Loading IM platform adapter molt_loopback(molt-loopback-1) ...   ← 创建
15:23:54.618  Attempting to terminate platform adapter molt-loopback-1 ...     ← drain
15:23:54.626  Loading IM platform adapter molt_loopback(molt-loopback-1) ...   ← resume 重建
15:23:54.631  Attempting to terminate platform adapter molt-loopback-1 ...     ← 幂等再 drain
```

**这条测试的价值在于它测的是同一条路，不是同一段代码的仿制品。**

#### 6.2.11 接进 `molt migrate`：顺序与三处回滚

`molt migrate --control-url ... --control-config ...` 新增一步"按住源壳"，**位置在 acquire 之后、checkout 之前**：

- 必须在 **acquire 之后**：若租约被别人占着，那只鱼就白白安静一场，而且不会自己醒
- 必须在 **checkout 之前**：这样快照是"它说过最后一句话"为止的完整状态

三处失败路径全部接上 `_resume_source(reason)`（热备失败 / `Fatal` / `MoltError`）。**为什么必须回滚**：一只哑着的鱼比一只坏掉的鱼更不容易被发现 —— 不回消息会被当成网络问题或 QQ 掉线，明确报错才会让人去看日志。

`drain` 失败时退出码是 **8**，且**不回滚**（它压根没被按下去过）。

真机验证（`tools/_verify_drain_migrate.py`，跑在装了插件的测试壳上，不碰生产容器）：

```
· 拿到租约 epoch=1
· 按住源壳 AstrBot 控制面 http://127.0.0.1:6199/api/plug/astrbot_plugin_molt …
  已关闭的平台                 molt-loopback-1
  仍在对外说话                 否（已复核）
…（热备 + 打包 + 登记）
  源壳现在是哑的（这是我们按的，不是它坏了）。
✓ 已交还租约——目标壳现在可以接管了
```

> **收益**：Ferryman 不需要自己起 HTTP 服务、不需要额外端口、不需要另做鉴权。控制面直接长在 AstrBot 的 dashboard 上。

### 6.3 数据路径：换壳只改一个环境变量

【查证】`/AstrBot/astrbot/core/utils/astrbot_path.py`

```
get_astrbot_root():
    1. os.environ["ASTRBOT_ROOT"]          ← 优先
    2. is_packaged_desktop_runtime() → ~/.astrbot
    3. os.getcwd()
get_astrbot_data_path() = <root>/data
```

派生路径：`config/`、`plugin_data/`、`t2i_templates/`、`webchat/`、`temp/`、`skills/`、`workspaces/`。

**结论**：Windows 端只要设

```
ASTRBOT_ROOT=C:\Users\<user>\AppData\Local\molt\shell\astrbot
cwd=%ASTRBOT_ROOT%
```

就能得到完全隔离的一套运行时，不污染用户目录、不和 Coopanion 之类的其他 Electron 应用打架。

### 6.4 停机的三种方式 —— **已实测，答案是 SIGINT** ✅【实测 2026-11-14 13:41】

**实测事实**：AstrBot 全代码库**没有 SIGTERM/SIGINT handler**。`/AstrBot/astrbot/core/` 内 signal 相关命中只在 `computer/booters/local.py:792,964` 和 `computer/process_sandbox/unix.py:60,65,69`，全都是给**子进程**发信号，不是自己收。

容器 `Config.StopSignal` 空、`StopTimeout` nil、`Entrypoint=[]`、`Cmd=[python main.py]`。

#### 实测判决（这是整个方案最关键的一个不确定性，现在关掉了）

用**隔离的测试容器**（`molt-sigtest`，独立 data 目录，绝不动生产容器）做了 A/B 对比：

| 方式 | 命令 | 退出码 | WAL 结果 | 日志 |
|---|---|---|---|---|
| **SIGTERM**（`docker stop` 默认） | `docker stop molt-sigtest` | **137**（= 128+9，被 SIGKILL） | **41,232 B 原封不动** | **完全没有** `Shutting down` |
| **SIGINT** | `docker kill -s INT molt-sigtest` | **0** | **0 B（-wal 文件已删除）** | ✅ `🌈 Shutting down AstrBot...` |

**SIGINT 的完整日志**：
```
[13:41:12.953] [Core] [INFO] [core.initial_loader:56]: 🌈 Shutting down AstrBot...
[13:41:12.954] [Core] [INFO] [star.star_manager:1934]: Terminating plugin astrbot ...
[13:41:12.954] [Core] [INFO] [star.star_manager:1934]: Terminating plugin builtin_commands ...
[13:41:13.007] [Core] [INFO] [knowledge_base.kb_db_sqlite:173]: 知识库数据库已关闭: /AstrBot/data/knowledge_base/kb.db
```

**三个结论**：

1. **`docker stop` 会损坏停机机会** —— 退出码 137 说明 10 秒超时后直接 SIGKILL，`Shutting down` 一行都没打出来。WAL 里 41 KB 未合并的事务就这么留在盘上。**这就是为什么不能直接把"迁移"实现成 `docker stop`**。
2. **SIGINT 是干净的** —— 退出码 0，走完 `core_lifecycle.stop()` 全链路，插件 terminate 被调用（`Terminating plugin astrbot` / `builtin_commands`），知识库显式关闭，**WAL 被 checkpoint 到 0 并删除文件**。
3. **所以 v0.1 的停机方式定为 `docker kill -s INT`（或等价的自毁按钮）**，不是 `docker stop`。

#### 三种候选的最终判决

| 方式 | 原理 | 实测结论 | 采用 |
|---|---|---|---|
| **A. 发 SIGINT** | `asyncio.run` 把 KeyboardInterrupt 转成 cancel → 走 CancelledError | ✅ **PASS，退出码 0，WAL 归零** | ⭐ **采用** |
| **B. 插件 cancel 掉 curr_tasks** | 插件直接操作 `core_lifecycle.curr_tasks` | 侵入核心内部状态，上游改了就废 | 不需要了 |
| **C. 插件设 `dashboard_shutdown_event`** | `core_lifecycle.py:299` 创建、`:416`/`:447` 置位 | 只关 dashboard，不一定结束 core | 不需要了 |

**这条实测的额外价值**：它把 §6.5「checkpoint 必须自己做」的必要性降低了一档 ——
只要走 SIGINT，SQLite 自己就会 checkpoint。但**仍然要自己 checkpoint**，因为：
①壳可能崩溃或断电，那条路上没有 SIGINT ②Windows 原生跑时是 `Stop-Process` / 关窗口，
语义与 SIGINT 不同（Windows 上 Ctrl+C 会给进程组发 CTRL_C_EVENT，但这个路径**尚未实测**）。

#### 另一半：Windows —— ✅ 已验（14:44）

**Windows 上有等价的优雅停机信号，关键是必须 attach 到那个控制台。**
完整实测、三个候选的对照、以及 `kb.db` 的 checkpoint 硬证据见 **§13.10**。三句话版本：
- `os.kill(pid, CTRL_BREAK_EVENT)` **不行**：退出码 `0xC000013A` 看着像 Ctrl+C 生效，其实是默认处理器直接杀进程，`stop()` 一行都没跑
- `os.kill(pid, CTRL_C_EVENT)` **不行**：Windows 根本不跨进程组递送，什么都不发生
- `AttachConsole(pid)` + `GenerateConsoleCtrlEvent(CTRL_C_EVENT, 0)` **可以**：退出码 0、`🌈 Shutting down`、`kb.db` 的 98,912 B WAL 真的合并进主库

**这条不改变 v0.1 的交接协议**——Windows 侧热备走只读 URI + `backup()`，本来就不需要停 AstrBot（§13.1 已验 0.042 秒）。它的用途是"这只鱼决定要休眠时关得干净"，也就是 v0.3 的 `molt sleep`。
插件 HTTP 端点自毁（`context.register_web_api()`，见 §6.2）保留作兜底路径。

### 6.5 数据库 checkpoint 必须自己做

**实测**：AstrBot 核心**没有任何 `wal_checkpoint` 调用**（全库 grep 只命中 `agent/message.py` 的 `_checkpoint`，那是业务概念不是 SQLite pragma）。

所以 snapshot 前必须显式：

```sql
PRAGMA wal_checkpoint(TRUNCATE);
```

`TRUNCATE` 会等 checkpoint 完成并把 WAL 截断为零，代价是可能阻塞并发读——但我们要停库了，无所谓。

`data_v4.db` 和 `memory_companion.db` 两个库都要做，因为 plugin 是独立开的连接。

### 6.6 配置的"机器覆写层" —— 已实现并实测 ✅

`cmd_config.json` 里混了**三类**东西：

| 类型 | 例子 | 能否跨机器 |
|---|---|---|
| 可携带 | persona、模型名、平台账号、插件行为参数 | ✅ |
| 机器绑定 | `provider_sources[workbuddy].api_base = http://127.0.0.1:17863/v1`、`dashboard.host/port` | ❌ Windows 上没这个中继 |
| 机密 | `dashboard.jwt_secret` | ❌ **绝不可跨机复用** |

#### 6.6.1 一个真实发生过的险情

第一次在 Windows 上用落位的数据启动 AstrBot 时，它照着 NAS 的配置**监听 `0.0.0.0:6185`**，在一台家用游戏机上把 dashboard 暴露给整个局域网，而且用的是 **NAS 自己的 `jwt_secret`**。

这不是配置错误，是**设计缺口**：把"可携带的状态"和"这台机器的身份"混在同一个文件里，搬过去就必然带着不该带的东西。

#### 6.6.2 设计：纯函数 + 写在代码里的覆写表

**否决了"两份 JSON 合成"**（`base.json` + `overlay.<shell>.json`，启动时合并）这个更常见的做法。理由：

- 合成会在磁盘上留中间文件 → 中间文件变成**新的真相来源** → 有人手改它然后困惑"为什么改了没生效"
- 两份文件意味着两份真相，而"谁是真相"正是这个项目最不能含糊的东西
- 覆写表写在代码里，跟着代码走、能被测试固定住。加一条新覆写规则 = 加一条断言

实现：`molt/molt/overlay.py`（255 行），纯函数 `apply_overlay(config, ...)` 接收关键字形参，`deepcopy` 后改，**不改入参**，返回人类可读的 `notes["changes"]` 清单。

#### 6.6.3 七处覆写，以及为什么每一处都不是可选的

| # | 覆写 | 不做的后果（全部实测过） |
|---|---|---|
| **0** | **`platform[].enable` → `false`（关掉所有平台适配器）** | **两条鱼同时在线**。见 §6.6.7——这是本表里唯一一条"漏了会直接违反公理 0"的 |
| 1 | 删 `provider_sources` 里的 `workbuddy` | 本机没有 `127.0.0.1:17863` 的中继，指向一个死地址 |
| 2 | **连 `provider` 条目一起删** | 只删 source 不删 provider → `provider.manager:372` 抛 `KeyError: 'type'` |
| 3 | **`agent_runner.config.model.provider_id` 重新指向** | **第三处引用**。前两处删完还剩一条 `WARN ... does not match an enabled provider` |
| 4 | `dashboard.host` `0.0.0.0` → `127.0.0.1` | 见 §6.6.1 的险情 |
| 5 | `dashboard.port` `6185` → `6199` | 与 NAS 的端口撞车（同一台机上可以并存时才会暴露） |
| 6 | `dashboard.jwt_secret` 重新生成 | **攻破 Windows 等于攻破 NAS** |

**第 0 条为什么排在第一位**：前面六条都只是"这台机器配置不对"，而它一旦漏掉，后面每一条都失去意义——配置写得再对，只要两个壳同时连上同一个 QQ 机器人，就已经是两条鱼在线了。

**第 3 条是最容易漏的一类**：它不崩（fallback 救了它），所以测试和肉眼都容易放过。它的实际代价是每次启动留一条"配置好像有问题"的警告——**那种警告的长期效果是让人对日志脱敏，真正出事时没人看**。

**删而不是改地址**：把 `api_base` 改成一个同样不存在的地址，只会让启动日志多一条警告，让人每次都怀疑自己配错。删掉是明确表态"这台机器没有这个服务"。

**没有 fallback 时宁可不改**：`_repoint_agent_runner` 在 `fallback_provider_ids` 为空时返回 `None`，保持原样。把主模型设成空会让它**彻底没有模型可用**，那比留一条警告糟糕得多。

**第 0 条只关开关，不删条目**：删掉整个 `platform` 段会连 `appid` / `secret` 一起丢掉，而"这只鱼从这边醒过来"时还需要它们。关是可逆的，删不是。`test_overlay_keeps_platform_credentials` 固定住这一点。

#### 6.6.4 `jwt_secret` 是安全问题，不是洁癖

`jwt_secret` 是 dashboard 的签名密钥。它的用途是"证明这个请求来自已登录的 dashboard 用户"。

如果它跟着 soul 一起搬到 Windows，那么任何能读到 Windows 上这份配置的人（不需要管理员，普通用户就能读 `data/cmd_config.json`），都能**伪造 NAS dashboard 的有效 token**。

**这条被做成默认行为而不是可选项**：`rotate_jwt=True` 是默认值，且 note 里写明理由。

#### 6.6.5 两个编码陷阱

- **`utf-8-sig` 必须带 `-sig`**：`cmd_config.json` 前 3 字节是 `ef bb bf`（BOM）。AstrBot 自己用 `utf-8-sig` 读所以没事，但 Linux 上普通 `json.load()` 打开会直接 `JSONDecodeError: Unexpected UTF-8 BOM`
- **写回必须保留 BOM**：换台机器后 AstrBot 读没事（它用 `utf-8-sig`），但其它读这个文件的工具可能有意见；更重要的是"覆写后的文件应该和覆写前是同一种格式"

#### 6.6.6 实测判决书【2026-11-14 14:00】

45 个顶层键，**只有 3 个键有差异**：`dashboard`（host / port / jwt_secret，jwt 64 字符 → 43 字符）、`provider`（5 条 → 4 条）、`provider_sources`（`['deepseek','workbuddy']` → `['deepseek']`）。其余 42 个键一字未变，**丢失 0 / 新增 0**。

AstrBot 用覆写后的配置启动：`Starting WebUI at http://127.0.0.1:6199`、`Loaded 1 personas.`、`AstrBot started.`；日志里 `workbuddy` 与 `does not match an enabled provider` **零命中**，全量 WARN / ERROR 行数 **0**。

**附带发现：人格不在 `cmd_config.json` 里。** 它的 `persona` 字段是空列表（实测），人格本体在 `data_v4.db` 的 `personas` 表。所以覆写层**物理上碰不到人格**——这既是好事（少一个能被配置搞坏的东西），也印证了"可携带的魂"和"机器绑定的壳"本来就是两类东西。

#### 6.6.7 这个表曾经漏掉第 0 条，而且差一点就出事了

**事故经过【2026-11-14 14:41】**：做 Windows 优雅停机实验时，三次用落位数据启动了真实的 AstrBot。那三份 `cmd_config.json` 的 `platform[0].enable` 全都是 **`True`**（从 NAS 原样搬来的），于是每次启动都真的发起了 QQ 连接：

```
shutdown-test-break-20261114T144130.log
  [14:41:37.426] [platform.manager:144]: Loading IM platform adapter qq_official(default_1234567890) ...
  [14:41:40.288] [qqofficial_platform_adapter:304]: [QQOfficial] Websocket session starting.

shutdown-test-console-20261114T144408.log
  [14:44:14.605] Loading IM platform adapter qq_official(default_1234567890) ...
  [14:44:17.181] [QQOfficial] Websocket session starting.
  [14:44:17.595] Attempting to terminate platform adapter default_1234567890 ...
```

**当时的覆写表只有 6 条，一条都没碰 `platform`。** 而这个字段"从 NAS 搬过来的"这个事实，谁都想不到要去检查——它不在"机器绑定"的直觉清单里（appid 明明是**该**搬的），但 `enable` 不是：**appid 可携带，`enable` 不可**。

**为什么没有真的造成事故**：三个进程分别只在 3 秒、3 秒、60 秒内就被测试脚本杀掉了，QQ websocket 的 `IDENTIFY` 握手都没完成。Windows 侧所有日志里 `FriendMessage` / `GroupMessage` **命中数全为 0**，生产容器 `RestartCount=0`、`StartedAt` 未变、14:40–14:46 期间持续正常收发与回复。所以是"**差几秒**"，不是"发生过"。

**但这不能当作侥幸的理由**——任何一次"在 Windows 上多留几分钟"的手工测试都会变成真的双活。

**修法**：新增 `_disable_platform_adapters()`，作为覆写表第 0 条、排在最前面；两条预设（`overlay_for_windows` / `overlay_for_posix`）都**不给调用方关闭它的便利**；`land` 在非 Windows 分支也照样关（那条路径原来连覆写都不做）。

**验证【实测 14:56】**：用同样的落位流程重新跑一遍，`molt land` 输出新增第一行 `✓ 关闭平台适配器：default_1234567890`；起 AstrBot 后日志里 `Loading IM platform adapter` / `QQOfficial` / `Websocket session` / `platform.manager` **全部零命中**，而库里的它完好（`personas` 1 / `conversations` 2 / `history` 139 / `memories` 71 / `knowledge_nodes` 160）。

**这条留在文档里的原因**：它是一条"检查清单漏项"，而检查清单漏项只有两种发现方式——出事，或者被别人指出来。这次是运气。下次不该再靠运气。


### 6.7 版本一致性

manifest 里必须记：

```json
{
  "gen": 44,
  "sha256": "…",
  "page_count": 2658,
  "schema_version": 12,
  "astrbot_version": "4.29.4",
  "python": "3.12.16",
  "plugins_lock": "sha256:…",
  "created_at": "2026-11-14T03:12:00Z",
  "created_by": "shell:nas"
}
```

**接管的机器版本不匹配就拒绝交接**，宁可这次搬不成，也不要把库喂给一个不认识的 schema。

---

### 6.2.12 心跳：账本里唯一回答「现在谁还在」的东西

账本其余每个字段回答的都是**过去**：`pushed_by` 是谁推的、`target_shell` 是给谁的、
`consumed_by` 是谁取走的。**没有一个回答「现在谁还在跑」。**

这不是理论缺陷，是实测出来的：鱼在 Windows 上被直接拔电之后，`molt status` 依旧显示
`gen 13 | target desktop-h2kgiao | consumed_by desktop-h2kgiao | lease: no-lease` ——
和"它正好好活着、只是没在说话"**完全无法区分**。而 NAS 的 homer 这时已经按公理 0
把自己关掉了（`should_stand_down`），于是**两边都没有鱼，且没有任何一方会报警**。

实现（`molt/warden.py`）：

| 项 | 值 | 理由 |
|---|---|---|
| 存储 | `state.json` 的 `heartbeats` 字典 | 和账本同一份文件、同一次原子写 |
| 保留 | 最近 **8 个**壳 | 这是"当前状态"不是"历史"；留多了 `molt status` 会被早就没了的机器淹掉 |
| 判 stale | `HEARTBEAT_STALE_SECONDS = 600` | 10 分钟。agent 每 10 秒一跳，留这么宽是为了不把一次 GC、一次休眠唤醒误报成"鱼死了" |
| 上报方 | `molt-agent.py` / `molt-homer.py` / `molt-bounce.py` 每轮循环 | 谁在跑谁报，不引入第二个进程 |
| 失败处置 | **只返回 False，绝不抛** | 一次心跳丢了不该影响任何交接 |

`GET /where` 是同一份数据的人读版本（一页纸、零外部资源、`?token=` 白名单**只对这一个
路径**开放）。`/molt where` 在 QQ 里返回同样的信息。

**它不解决什么**：心跳证明"某个壳在报"，不证明"鱼在说话"。两者之间还差一层 ——
`speaking` 字段（控制面 `/status`）。心跳变哑只能说明那一侧没人报了，需要人去判断。

`/molt where` 与 `/molt status` 现在是**两层**，不是两个名字：`where` 只答"在哪"
（位置 + 各壳心跳，且不检查 `enabled` —— 查询是只读的，装上就该能用），
`status` 是它的超集（再加账本 epoch、租约持有人、最新一代的来去）。
早先它们被并成一套五个词一份输出，结果是**名字暗示两种内容、实际只有一份**。

### 6.2.13 落地播报：为什么它必须是一条单独的消息

到了新壳上，它开口的第一句回复之前先单独发一条
`（我刚从 UGREEN-776B 搬过来，这是第 15 代。）`。

需求侧理由：QQ 官方机器人的**资料页、昵称、签名只能在开放平台后台改**，程序没有接口。
"它换了台机器"这件事只有它自己说出来才到得了人眼前。

**第一版做错了，而且是"看起来完全成功"的错**：用
`@filter.on_decorating_result()` 把 `Plain` 插到 `result.chain[0]`。日志里
`[Molt] 第 15 代落地播报：（…）` 打得清清楚楚，`arrival_state.json` 也写好了，
**人却一个字都没看见**。

根因（`G:\molt-p0\astrbot-src`）：

| 证据 | 内容 |
|---|---|
| `pipeline/result_decorate/stage.py:137` | `is_stream = result.result_content_type == ResultContentType.STREAMING_FINISH` |
| `pipeline/result_decorate/stage.py:191-193` | `if is_stream: return` |
| 同一秒日志 | 20 条 `Plugins that depend on the pre-send event hook may not work correctly when streaming output is enabled.` |
| `platform/sources/webchat/request_flags.py:6` | `"enable_streaming": True` |
| `platform/platform_metadata.py:20` | `support_streaming_message: bool = True`（默认值，QQ 官方适配器**没有覆盖**） |

→ 文字早已一块块推给平台了，**往那个 chain 里插东西等于插进一个不会再被发送的对象**。
而且 `state.mark()` 在组装时就执行了，于是"生效了一次，永远不再播"。

修法：换通道，用 `@filter.on_waiting_llm_request()` + `await event.send(event.plain_result(note))`。
三条好处：顺序天然在前、**与流式开关无关**、这句话不会被当成模型自己说的话写进对话历史。

**第二个错，同一个病根（2026-11-15 06:00 修）**：换完通道之后，
`arrival_note()` 仍在**组装**这句话的时候就写了 `announced_generation`，
`announce_arrival()` 也是先置 `_arrival_said = True` 再去 `await event.send()`。

后果不是"偶尔少一句"，而是**一次发送失败 = 这一代永久沉默** ——
而失败恰恰是最该重试的那一次（QQ 官方机器人的主动消息限制、`msg_id` 复用、网络抖动）。

> 这条教训我在"流式"那一节刚写过：「标记时机必须在**真的送到**之后」。
> 隔了一个功能又犯了一遍。**能被复述的教训不算掌握，被测试钉住的才算。**

更糟的是它被测试固定住了：`test_plugin_marks_state_even_when_the_send_fails`
的 docstring 写着「发不出去也不能每分钟重试一次」，断言 `announced_generation == 13`。
**一条把缺陷写成规格的测试，比没有测试更危险** —— 它会让下一个读到它的人
以为这就是想要的行为。

现在的形状：

| 时机 | 动作 |
|---|---|
| 组装出这句话 | 只放进 `_arrival_pending = (代, 那句话)`，**不记账** |
| `event.send()` 抛异常 | 记一条 WARNING，**保留 pending**，下一条回复再试 |
| `event.send()` 正常返回 | `mark(代, "announced")` + `_arrival_said = True`，之后不再说 |
| 那一代太旧（> 30 分钟） | 当场 `mark(代, "stale")` —— 本来就不打算说，没有"等送达"这回事 |

重试时**必须优先用 pending**：`arrival_note()` 已经被 `_arrival_checked` 锁住，
再调它只会返回空串，那句话就永远发不出去了。第一版改漏了这一点，测试立刻抓到。

四条自我约束（缺一条就不要这个功能）：只在一个 30 分钟窗口里说一次 /
来源与代号必须能在账本里查到否则闭嘴 / 问不到账本最多试 3 次就放弃（每次都是带超时的
HTTP，不封顶等于让一次问话先等 5 秒）/ 发不出去也只算一次（重试会刷成复读机）。

**通用教训**：标记"说过了"的时机必须在"真的送到"之后。日志里有一行成功，
和用户屏幕上有一个字，是两件事。

## 7. 机器专属 vs 可共享的边界

这张表是整个项目的"体积预算表"。

### 7.1 决策表（实测数据）

| 路径 | 大小 | 共享 | 同步方式 | 说明 |
|---|---|---|---|---|
| `site-packages/` | **1325 MB / 119 包** | ❌ | 从不同步 | Python 二进制，Win/Linux 不通用。各壳自己装 |
| `plugins/` | 187 MB | ⚠️ | git（锁定 commit） | 实测**无任何 .so/.pyd/.exe/.dll**，纯 py 可跨平台 |
| `data_v4.db` | 10.9 MB | ✅ | snapshot | 核心库 |
| `plugin_data/…/memory_companion.db` | 17.7 MB | ✅ | snapshot | 记忆库，41 张表 |
| `plugin_data/meme_manager/` | **172 MB** | ✅ | blob 增量 | 表情包，大头 |
| `plugin_data/` 其余 | ~4 MB | ✅ | blob 增量 | |
| `attachments/` | 4 MB | ✅ | blob 增量 | |
| `config/` | ~0 | ✅ | overlay 合成 | 见 §6.6 |
| `skills/` | ~0 | ✅ | git | |
| `workspaces/` | ~0 | ✅ | git | |
| `knowledge_base/` | ~0 | ✅ | blob 增量 | |

**总计需要流动：~250–400 MB**（首次）；之后每次交接只动两个 db（约 25 MB）+ 增量 blob。

### 7.1.1 `site-packages` 的真实构成 —— 为什么它绝对不能共享【实测 2026-11-14】

不做这一步实测就会犯一个致命错误：以为"反正 `pip install -r requirements.txt` 装完了，两份环境应该差不多"。

**实际不是。** AstrBot 运行时会往 `data/site-packages/` 里 **pip 安装插件声明的额外依赖**，当前装了 **119 个包 / 1325 MB**，其中：

| 观察 | 数字 | 含义 |
|---|---|---|
| 包数量 | 119 | 远超 `requirements.txt` 的基线 |
| `.so` 文件数 | **122** | 全是 `x86_64-linux-gnu` 二进制 |
| 最大的一块 | `torch`（`libtorch.so`、`libtorch_cpu.so`、`libc10.so`…） | **Linux ELF，Windows 加载即崩** |
| 是否含人格依赖 | 含 `edge_tts` 7.3.1、`whisper` | **人格需要的语音能力在这里** |

**由此得出两条硬规则**：

1. **`data/site-packages/` 必须列入"永不共享"名单**。它是 122 个 Linux `.so`，跨平台复制 = 100% 崩溃。这一点和 `plugins/`（实测纯净）形成鲜明对比——**判断依据不能靠目录名，只能靠"有没有原生二进制"这个实测**。
2. **但人格依赖不能丢**。`edge_tts` / `whisper` 不在源码仓库的 `requirements.txt` 里（实测 grep 无命中），是运行时装的。**新壳必须重新装一遍**，装出来的是 Windows 产物。这就是为什么 manifest 里要记录 `runtime_pip` 清单，而不是指望"同步目录"解决。

> **工程结论**：`shell` 的 Python 环境是**可再生**的（按清单重装），`soul` 的状态是**不可再生**的（必须精确搬运）。混淆这两者是这类项目最常见的翻车点。

### 7.2 为什么 plugins 可以共享但不能乱共享

【实测】`private_companion` 插件 113 MB 里，`astrbot_host/native_extensions` 只有 py 源码 7 MB，**没有任何 `.so` / `.pyd` / `.exe` / `.dll`**。所以跨平台是纯净的。

**但**：

- 版本必须锁定。插件更新会改 schema、改配置字段，两台机器版本不一致会出"读不懂对方的库"
- 用 git submodule 或 lock 文件，manifest 里记 commit sha
- 如果有插件在某台机器上装了原生扩展，那台机器的 plugins 目录就不能参与同步（用 `.moltignore` 标记）

### 7.3 魔术封包怎么发：单播不够，必须三路齐发

**最终实现（已实测 PASS）**：`tools/wol.py`，纯标准库，三路齐发。

```python
targets = [host, f"{prefix}.255", "255.255.255.255"]
```

**为什么不能只发单播** 【实测教训】：机器关机后 **ARP 表项会过期**，
单播报文在 NAS 上直接报 `No route to host`（或静默丢弃），根本到不了网卡。
**定向广播（`192.168.1.255`）由交换机泛洪，才是真正的主力**；
`255.255.255.255` 是兜底（部分交换机不转发 limited broadcast）。

这一点曾被写反：早期版本认为"单播最稳，连 wakeonlan 包都不用装"。
后来为了排查 UDP 9 收不到包，才发现单播在目标关机时根本出不去 —— 见 §7.4.1 的收包实验。

**为什么 Warden 应该跑在 NAS 宿主机上而不是容器里** 【推断，未验证】

Docker 默认 bridge 网络的容器，发往 `192.168.1.255` 的 UDP 广播可能被 NAT 层丢弃（bridge 不转发广播到宿主物理网卡）。
本次 PASS 测试是**在 NAS 宿主机上直接跑的 python**，没有经过容器，所以这条推断仍未证伪。

加固顺序：

1. **首选（已采用）**：Warden 跑在 NAS 宿主机上（不是容器里），用宿主机 socket 发广播 → 自然没问题
2. 备选：容器用 `--network host`（但会影响 AstrBot 的其他隔离）
3. 备选：把发包动作委托给宿主机上的一个小脚本，容器只发 HTTP 请求触发它

**关于 `wakeonlan` / `etherwake` 包**：不需要装。`tools/wol.py` 只有 30 行标准库代码，
封包就是 `b"\xff" * 6 + mac_bytes * 16`（102 字节），已字节级校验通过（§7.4.1）。

### 7.4 WOL：全链路打通，但有一个需要自愈的环节

这一节记录本项目**第一个被完整闭环验证的物理能力**：一台真关机（S5）的 Windows 机器，被 NAS 发出的魔术封包叫醒。

> **⚠️ 读这一节先记住一句话**：唤醒能力**已经验证成立**，但它依赖一个**不跨重启保留**的 OS 状态。
> 所以「能用」和「一直能用」是两件事，后者需要 §7.4 里描述的自愈任务。
> 这是本项目目前最接近"隐藏地雷"的一处，必须在 v0.1 里就处理掉。

**判决表** 【实测 2026-11-14 12:40】

| 关卡 | 状态 | 证据 |
|---|---|---|
| BIOS 固件（ErP / PCI-E 唤醒 / WOL） | ✅ 用户已在现场完成 | `Kernel-Boot BootType=0`，真 S5 冷启动 |
| 快速启动 / 休眠关闭 | ✅ 持久有效 | `HibernateEnabled=0`、`hiberfil.sys` 已删除、`powercfg /a` 报「快速启动：休眠不可用」 |
| 魔术封包格式与网络路径 | ✅ 持久有效 | 102 字节结构校验 + Windows 侧 UDP 收包 |
| **真实关机 → 发包 → 唤醒** | ✅ **23.1 秒 PASS** | 见 §7.4.2 时间线 |
| DHCP 地址保留 | ✅ 持久有效 | 开机 7 秒后仍拿到 `192.168.1.20` |
| 网卡在 OS 唤醒白名单 | ⚠️ **能 arm，但不跨重启保留** | 12:24 / 12:40 两次冷启动后均丢失，需开机自愈（见下） |

**硬件** 【实测】：主板 `MAG B550M MORTAR WIFI (MS-7C94)`，BIOS `1.70`（AMI，2021-05-12）；网卡 Realtek PCIe 2.5GbE `AA-BB-CC-DD-EE-FF`，驱动 `10.54.1111.2021`（2021-11-11）。驱动虽旧，**不影响 WOL**——不需要为这件事去更新驱动。

**网卡驱动层：看起来全开** 【实测】

```
*WakeOnMagicPacket        = 开启
*WakeOnPattern            = 开启
S5WakeOnLan               = 开启     ← 关机后唤醒
WolShutdownLinkSpeed      = 10 Mbps 优先
注册表：PnPCapabilities=8, S5WakeOnLan=1, *WakeOnMagicPacket=1
```

**一个不会自己好的缺口：arm 状态不跨重启保留** 【实测，2026-11-14 12:49 修订过】

> 这一节在 12:40 实测 PASS 之后被我改写过一次。
> 当时的结论是「已修复 ✅」，**那是错的**——12:49 复查发现网卡又掉出了白名单。
> 下面这版是修正后的。留这个痕迹是因为它本身说明了本项目的核心难点：
> **一次成功不等于长期有效。**

修复前的状态：

```
powercfg /devicequery wake_armed
  HID-compliant mouse
  HID Keyboard Device (002) (003) (005)     ← 网卡不在
powercfg /devicequery wake_programmable
  Realtek PCIe 2.5GbE Family Controller     ← 具备能力，但系统未批
root\wmi MSPower_DeviceEnable = ...8125..._0 = False
Get-NetAdapterPowerManagement → 报 Error 31「A device attached to the system is not functioning」
```

含义：**即使 BIOS 全开、封包发得再对，Windows 这一侧会直接拒绝把网卡当唤醒源。**

修复只需一条命令（管理员）：

```powershell
powercfg /deviceenablewake "Realtek PCIe 2.5GbE Family Controller"
# 等价 GUI：设备管理器 → 网卡 → 属性 → 电源管理 → 勾选「允许此设备唤醒计算机」
```

**为什么必须用户手工做**：`EnableLUA=1`（UAC 开启）时，HKLM 下的驱动键
`\Control\Class\{4d36e972-…}\0009` 与设备实例键 `\Enum\PCI\…\Device Parameters`
**均不可写**（报「不允许所请求的注册表访问权」）。Molt 的任何自动化都不能靠猜权限去改注册表。

非管理员执行 `deviceenablewake` 的实测报错（`tools/wol-guard-worker.ps1` 干跑得到）：
```
deviceenablewake 返回码: 1  输出: 你没有启用或禁用设备唤醒的权限。
```

**关键修正：arm 状态不跨重启保留** 【实测，两次复现】

| 时间 | 事件 | `wake_armed` 里有网卡吗 |
|---|---|---|
| 12:26 | 用户管理员跑 `deviceenablewake` | ✅ |
| 12:28 | lead 复查 | ✅ |
| 12:40:03 | 关机（真 S5） | — |
| 12:40:42 | 被魔术封包唤醒（**这次 PASS**） | — |
| 12:49 | lead 复查 | ❌ **没了** |

12:24 那次为进 BIOS 的重启也是一样：重启前有，重启后没有。**两次重启、两次消失。**

也就是说：**"这次唤醒成功"只证明了"这次关机时它是 armed 的"，不证明下次还 armed。**
如果不处理，用户会遇到最难查的一类故障——第一次好使、第二次叫不醒。

**因此 arming 不是一次性安装步骤，而是一个每次开机都要跑的自愈动作。**

解法：计划任务 `Molt-WolGuard`（`tools/install-wol-guard.ps1` + `tools/wol-guard-worker.ps1`）

```
触发器 1：AtStartup          ← 主路径：每次开机重新 arm
触发器 2：每 10 分钟一次      ← 兜底：万一开机那次失败/被跳过
身份   ：SYSTEM, RunLevel Highest, LogonType ServiceAccount（不需要存密码、不需要人登录）
动作   ：powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File wol-guard-worker.ps1
日志   ：tools/logs/wol-guard.log（超 200KB 自动滚存）
```

worker 的行为：已经在白名单里就 `[SKIP]`（幂等），不在就 arm 一次并复查，复查仍失败就记 `[FAIL]` 并以退出码 2 退出。

**Warden 侧的契约（写进 v0.1）**：迁徙前不仅要读 `check-wol.ps1 -Json`，还要能区分两种情况——

| `ready_to_wake` | `nic_in_wake_armed` | 含义 | Warden 动作 |
|---|---|---|---|
| true | true | 一切就绪 | 正常迁徙 |
| **false** | **false** | arm 丢了（重启后常见） | **先尝试自愈**（触发 `Molt-WolGuard` 或直接跑 worker），成功则继续 |
| false | true | 有别的阻塞（休眠没关等） | 拒绝迁徙并告警，不自愈 |

这条区分很重要：**"arm 丢了"是本方案唯一一个可以当场自动修复的阻塞项**，
而"休眠没关""BIOS 没开"都必须人去做。

**自愈闭环实测通过** 【实测 2026-11-14 13:00:34，本关最后一次验证】

装完 `Molt-WolGuard` 之后重启，日志给出了完整证据链：

```
LastBootUpTime : 2026-11-14 13:00:23
13:00:34  --- 开始检查 ---          ← 开机后 11 秒
13:00:34  身份: NT AUTHORITY\SYSTEM
13:00:40  网卡: Realtek PCIe 2.5GbE ... Status Up
13:00:40  arm 状态(改前): False      ← 重启后确实是丢的，不是空转
13:00:40  deviceenablewake 返回码: 0
13:00:40  arm 状态(改后): True
13:00:40  [OK] 已重新 arm
```

**为什么能确定是 `AtStartup` 触发器而不是 10 分钟兜底**：重复触发器从 `00:05` 起每 10 分钟
（`:05 :15 :25 :35 :45 :55`），13:00:34 **不落在网格上**。所以只可能是开机触发器。

`arm 状态(改前): False` 这一行尤其重要——它排除了"本来就在白名单里、worker 跑了个空转"
这种解释。worker 确实走到了需要它干活的分支，然后真的把状态改回来了。

结论：**WOL 这一关从"能唤醒一次"变成"每次都能唤醒"**，且这个保证是自动维持的，
不需要人记得做任何事。

**一个已稳定的长期事实**：修复后 `PnPCapabilities = 8`（不是 24），**WOL 依然可用**。
详见 §7.4.3 —— 这个数字曾经把 lead 带进沟里。

### 7.4.1 网络路径已验证通【实测 2026-11-14 12:11】

这一步必须先做，否则后面所有排查都建立在不牢的基础上。**结论：软件侧全线畅通。**

**发端（NAS）**：写了纯标准库的 `wol.py`（`/mnt/dm-0/astrbot/_molt/tools/wol.py`），
封包结构实测校验通过 —— **102 字节 = 6 × `0xFF` + 16 × MAC**，前后各取 6 字节与 MAC 一致，16 段重复全部匹配。

**收端（Windows）**：在 `0.0.0.0:9` 起 UDP 监听，NAS 单播发两个封包过去：

```
收到 102 字节  来自 192.168.1.10:59883
前6字节: ff ff ff ff ff ff
魔术封包结构正确: True        RESULT: PASS
```

**这一条排除了三个常见误判**：

| 曾经可能的怀疑 | 实测结论 |
|---|---|
| 封包格式写错了 | ❌ 排除，字节级校验通过 |
| 防火墙拦了 UDP 9 | ❌ 排除，包完整收到（注：当前网络档案是 Public，但这次没拦） |
| 路由/ARP 到不了 | ❌ 排除，ARP 表里 `192.168.1.20 … REACHABLE` |

**所以问题被精确收敛到一个点上**：从"关机（S5）"到"网卡仍通电并监听魔术封包"这一段。
这一段**无法从系统内验证**，只能靠实测：关机 → 发封包 → 看机器起不起来。 → 已实测，见 §7.4.2。

### 7.4.2 真实关机唤醒实测：PASS ✅【实测 2026-11-14 12:40】

**观察器必须待在 NAS 上**。这不是设计偏好，是物理约束：发起关机的机器就是被试机器，
观察器如果跑在 Windows 上，会在关机瞬间跟着一起死，永远记录不到"第几秒被叫醒"。

所以 `tools/wol-test.py`（8,099 字节）设计成：

1. 先 ping 确认目标在线
2. 连续 **两次** ping 不通才判定掉线（防抖动，单次超时不算）
3. 判定掉线后开始周期性发魔术封包（默认 7 秒一轮）
4. 持续 ping，记录第几秒目标回来
5. 全过程写 JSON 到 `/mnt/dm-0/astrbot/_molt/wol-test/result-<stamp>.json`

**发包策略的关键修正**：最初只发 `255.255.255.255`，这不够。机器关机后 **ARP 表项会过期，
单播报文根本出不去**。改为三路齐发：

```python
targets = [host, f"{prefix}.255", "255.255.255.255"]
```

定向广播（`192.168.1.255`）由交换机泛洪，才是真正主力。

**实测时间线**（40 分钟窗口，观察器 pid 12011）：

```
[12:40:03] User32 1074      用户点关机
[12:40:23] Kernel-Power 109 内核开始关机转换
[12:40:28] offline          NAS 判定掉线（关机流程走完，网络断）
[12:40:31] packet           第 1 轮，3 个封包
[12:40:38] packet           第 2 轮，累计 6
[12:40:44] packet           第 3 轮，累计 9
[12:40:50] packet           第 4 轮，累计 12
[12:40:42] Windows LastBootUpTime（BIOS POST 完成）
[12:40:52] wake             机器回来了！从掉线到网络恢复 23.1 秒
结论: PASS
```

**这是魔术封包叫醒的，不是人按的电源键** —— 三条独立证据：

| 证据 | 数值 | 说明 |
|---|---|---|
| 发包到上电的间隔 | **11 秒** | 12:40:31 发包 → 12:40:42 `LastBootUpTime`。正好是 BIOS POST + 引导耗时，人按电源键不可能卡在这个位置 |
| 启动类型 | `Kernel-Boot 27 BootType=0` | **冷启动 = 真 S5**。对比当天 `11:08:29 BootType=1`（快速启动/伪关机），证明快速启动确已关闭 |
| 关机流程完整 | `User32 1074` → `Kernel-Power 109` | 是正常关机，不是断电/崩溃 |

**`powercfg /lastwake` 报「唤醒历史记录计数 - 0」是正常的**，别被它骗：
S5 冷启动不计入唤醒历史，只有 S3/S4 睡眠唤醒才计。计数 0 反而佐证了这次是 S5。

**DHCP 保留同步验证** ✅：

```
租约: 11-14 12:40:49 -> 11-15 12:40:49
地址: 192.168.1.20
```

开机后 7 秒拿到地址，仍是 `.63` → 路由器（ASUS，`192.168.1.1`）的
「手动指定 IP 的 DHCP 列表」里那条 `AA-BB-CC-DD-EE-FF → 192.168.1.20` 生效了。

**为什么不建议手动填静态 IP**（这里踩过一次判断）：

| 理由 | 说明 |
|---|---|
| WOL 只需要 MAC，不需要 IP | 魔术封包寻址靠 MAC 广播，IP 无关 |
| 架构上永远是 Windows 主动连 NAS | NAS 是静态的 `.50`，Windows 在 checkout 时上报自己的 IP，Warden 无需预知 |
| 手填静态有撞进 DHCP 池的真实风险 | 路由器可能把同一个地址再分配给别的设备 |

→ **用 DHCP 保留，不要手填静态**。这条写进 §9.5。

### 7.4.3 这个数字曾经把我带进沟里：`PnPCapabilities`

这是本项目**第三次**同类型错误，值得单独记一节。

**现象**：用户进 BIOS 重启后，网卡**掉出了** `wake_armed`。同时发现驱动键
`\Control\Class\{4d36e972-e325-11ce-bfc1-08002be10318}\0009` 里的
`PnPCapabilities` 从 `24`（0x18）变成了 `8`（0x08）。

**lead 的错误推断**：0x10 位 =「允许此设备唤醒计算机」，被 `powercfg /h off` 清掉了，
所以重启后掉出白名单。

**证伪实验**：让用户重跑 `powercfg /deviceenablewake`。结果 ——
`PnPCapabilities` **仍然是 8**，但网卡**回到了** `wake_armed`。

**结论：那个位根本不是判据。** `PnPCapabilities=8` 也能正常 WOL（本次 PASS 就是在 8 的状态下测出来的）。

**唯一可信的判据是 `powercfg /devicequery wake_armed` 的输出本身** ——
这是系统的最终裁决，不是某个注册表位的函数。别去反推位标志。

**三次同型错误清单**（同一个病根：看到一个能解释现象的信号，就当成因果，没做证伪）：

| # | 错误 | 症状 | 正确做法 |
|---|---|---|---|
| 1 | 读 `Session Manager\Power\HiberbootEnabled` 判断休眠 | 它是**陈旧值**，`powercfg /h off` 不会清它，永远为 1，导致误报「快速启动还开着」 | 读 `HKLM:\SYSTEM\CurrentControlSet\Control\Power\HibernateEnabled`；或解析 `powercfg /a` 看「快速启动」落在支持还是不支持段 |
| 2 | `$armed -match 'Realtek'` 当布尔用 | `-match` 用在数组上返回**匹配到的元素数组**（`Object[]`），打印出网卡名而不是 True/False | `[bool]($armed \| Where-Object { $_ -eq $nicDesc })` |
| 3 | 由 `PnPCapabilities` 位推断唤醒开关 | **已被证伪**，8 也能用 | 只认 `wake_armed` 列表 |

**教训一句话**：**给用户看的脚本/判断，必须先在真机上跑一遍并做证伪实验，不能"看起来对"就交付。
Windows 上判断 WOL 状态，只认 `powercfg /devicequery wake_armed` 的输出本身。**

### 7.4.4 一个可复用的诊断脚本：`tools/check-wol.ps1`

为了让这一关可重复验证（而不是每次靠人肉敲四五条命令），写了 `molt/tools/check-wol.ps1`。
它输出一个 `ready_to_wake` 布尔值和 `blocking` 列表，Warden 可以直接消费同一份 JSON。

**当前这台机器的输出（实测，修复后）**：

```
网卡: Realtek PCIe 2.5GbE Family Controller  驱动 10.54.1111.2021 (2021-11-11)
唤醒白名单: HID-compliant mouse / HID Keyboard Device (002)(003)(005) / Realtek PCIe 2.5GbE Family Controller

结论:
  nic_in_wake_armed    = True
  fast_startup_off     = True
  hibernate_off        = True
  nic_can_be_armed     = True
  ready_to_wake        = True      ← blocking 列表为空
```

**踩到的坑（值得写进所有 Windows 脚本的规范）**：PowerShell 5.1 读取**不带 BOM 的 UTF-8 脚本**时按 GBK 解码，
中文注释会让**整个文件解析失败**（报 `Unexpected token`、`Missing closing '}'` 这类完全误导的错误）。
另外本机 `LocalMachine` 执行策略是 `Restricted`，直接 `.\script.ps1` 会被拒。

三条规避：①脚本统一存成 **UTF-8 with BOM**；②用 `powershell -NoProfile -ExecutionPolicy Bypass -File x.ps1` 调用，不改系统设置；③**别用 `Test-Path C:\hiberfil.sys`** —— 非管理员下它对 hiberfil.sys 返回 `False`（权限），要用 `cmd /c dir /a:h` 兜底，否则会得出"休眠文件不存在"的错误结论（本项目踩过一次）。

> 读状态判断逻辑本身的三个坑（陈旧注册表键 / `-match` 返回数组 / `PnPCapabilities` 反推）已单独记在 **§7.4.3**，那里有完整的证伪过程。

**配套的修复脚本 `tools/fix-wol.ps1`**：`check-wol` 只诊断不动手，`fix-wol` 是它的执行版，
自查管理员权限 → 探测有线网卡 → `powercfg /h off` → `powercfg /deviceenablewake <网卡描述>` → 复查并打印两个布尔值。
覆盖了驱动过旧导致 `deviceenablewake` 失败的分支（提示先更新驱动）。两个脚本都做了 `Parser::ParseFile` 语法自检。

**脚本自身的边界（重要）**：`fix-wol.ps1` **必须由用户以管理员身份手动运行**，Molt 无法代劳
（§7.4 已说明 UAC 下 HKLM 不可写）。所以它是"装机时的一次性工具"，不是"运行时的自动化组件"。
运行时的自动化链路是：`check-wol.ps1 -Json` → Warden 读 JSON → 若 `ready_to_wake=false` 则拒绝迁徙并告警。

**快速启动会污染 S5 语义** 【实测，**已修复**】

修复前的状态：

```
HiberbootEnabled = 1        （快速启动开）
C:\hiberfil.sys  6,840,942,592 B   （注意：Get-Item -Force 会因权限读不到，要用 dir /a:h）
powercfg /a → 支持 S3 / 休眠 / 混合睡眠 / 快速启动；不支持 S0ix
Kernel-Boot 27: 11/14 11:08 = 0x1（快速启动）  11/13 23:16 = 0x0（冷启）
```

也就是说**用户点"关机"时，机器实际走的是 hiberboot，不是真 S5**。此时网卡的待机供电行为与真 S5 不同，WOL 会失效。

**已执行的修复**：`powercfg /h off`（关休眠 = 同时关快速启动）。

**修复后的验证** 【实测 2026-11-14 12:24】：

```
HibernateEnabled = 0
hiberfil.sys     已删除（cmd /c dir /a:h 确认）
powercfg /a      「休眠：尚未启用」「快速启动：休眠不可用」「混合睡眠：休眠不可用」
                只剩「待机(S3)」supported
Kernel-Boot 27   2026/11/14 12:24:05  BootType=0  冷启动（真 S5）✅
```

代价是丢掉 6.8 GB 休眠文件和"开机快几秒"，换来 WOL 行为可预期。本项目选择后者，**并且已在 `fix-wol.ps1` 里固化**。

**电源方案现状** 【实测，AC 侧已经是好的】

```
STANDBYIDLE      AC=0（从不睡眠）  DC=0x384
HIBERNATEIDLE    AC=0
VIDEOIDLE        AC=0（从不关显示器）
PROCTHROTTLEMIN  AC=5%  MAX=100%
当前方案 = 平衡 (381b4222-…)
lastwake：唤醒历史计数 1，唤醒源计数 0
```

**BIOS 四项要求 —— 已由用户在现场完成 ✅** 【实测 2026-11-14】

| 项 | 要求 | 为什么 | 状态 |
|---|---|---|---|
| `ErP Ready` | **Disabled** | 开了会在 S5 切断 +5VSB，网卡彻底没电，WOL 100% 失效 | ✅ 已设 |
| `Power On by PCI-E`（或 `Resume by PCI-E Device`） | **Enabled** | S5 唤醒的固件开关，比 OS 侧任何设置都关键 | ✅ 已设 |
| `Wake On LAN` / `Integrated LAN Wake` | Enabled | — | ✅ 已设 |
| `Deep Sleep`（若有） | Disabled | 同上，S5 掉电 | ✅ 已设 |

位置：`Settings > Advanced > Power Management Setup`

**这三项的验证不需要再进 BIOS**：`Kernel-Boot 27 = BootType 0`（真 S5 冷启动）+ 12:40 那次 23.1 秒唤醒，
已经证明固件层配置正确。**结论：BIOS 这一关已关闭，不必再动。**

**现场核验手段（留给换机器/换主板时用）**：关机后看**网口指示灯 / 交换机端口灯是否还亮**。
亮 = +5VSB 有电 = 有希望；灭 = 断电了，回去查 `ErP Ready`。

【实测】NAS 侧网卡：`eth0 98:6e:e8:22:77:6b`、`eth1 …6c`，`Wake-on: g`（支持 `pumbg`）。这是 NAS 被唤醒的能力，本方案暂不需要（NAS 常开）。

---

## 8. 安全模型

### 8.1 绝不做的三件事

| 绝不 | 原因 | 替代 |
|---|---|---|
| 挂 `docker.sock` 进 AstrBot 容器 | 等于给容器 root，一个插件漏洞就是整机沦陷 | Warden 白名单 HTTP API |
| 让 AstrBot 直接写 NAS 的 soul 目录 | 网络文件系统写不可靠 + 权限放大 | 只走 Warden |
| 把 `jwt_secret` / api key 跨机器同步 | 一台泄漏 = 两台泄漏 | 每壳独立生成，密钥不进 snapshot |

### 8.2 认证

- Warden 与壳之间：**HMAC-SHA256**，请求头带 `X-Molt-Epoch`、`X-Molt-Nonce`、`X-Molt-Sig`
- 共享密钥：首次配对时生成，存 `~/.molt/token`（NAS）/ `%LOCALAPPDATA%\molt\token`（Windows），权限 0600 / 仅本用户
- 时钟不参与签名（避免时钟漂移导致全线失败），nonce 防重放由 Warden 侧缓存 5 分钟

### 8.3 网络暴露面

| 服务 | 监听 | 白名单 | 备注 |
|---|---|---|---|
| Warden | NAS `0.0.0.0:<port>` | 只接受 `192.168.1.0/24` | 端口随机高位 |
| molt-agent (Win) | `0.0.0.0:<port>` | 只接受 NAS IP | 供 Warden 探活 |
| AstrBot dashboard | 各壳 | 现状保持 | 不改动 |

**注意现状**：NAS 的 sshd 配置 `PubkeyAuthentication no`、`PermitRootLogin yes`、端口 922、密码认证【实测】。这是个明显弱点，**建议 v0.1 顺手改掉**（开公钥、关密码、关 root 或至少限制来源）。Molt 的 token 如果有机会走 SMB 传输，那 SMB 用户凭证也是攻击面。

---

## 9. Windows 端落地细节

### 9.1 运行时布置

```
C:\Users\<user>\AppData\Local\molt\
  shell\
    astrbot\                     ← ASTRBOT_ROOT
      data\                      ← 本地工作副本（从 snapshot 还原）
      venv\                      ← Python 3.13 venv（实测 3.13.5 / sqlite 3.49.1 ✓）
  token                          ← HMAC 共享密钥
  logs\
  state.json                     ← { last_checked_out_gen, epoch, shell_id }
```

**注意**：不要放在 NAS 的 SMB 映射盘（`M:` / `Y:` / `Z:` 已经存在，但那些是"访问"通道，不是"运行时"位置）。运行时必须是本地磁盘，原因见 §3。

### 9.2 当前这台机器的真实断点（实测）

WOL 能开机，**开机之后什么都不会发生**。这是比 WOL 更严重的问题：

```
Get-Process python, pythonw, obs64   →  0 个进程
```

也就是说重启之后，**没有任何机器人进程会自动恢复**。现有自启只有：

| 项 | 类型 | 问题 |
|---|---|---|
| `\BiliDailyPublish` | 计划任务，**InteractiveToken** | 无人登录时**根本不会执行** |
| `\Coopanion-WorkBuddy-Relay` | 计划任务，At logon | 只在人登录后 15 秒起 |
| HKCU Run ~40 项 / 启动文件夹 2 项 | 登录时 | 同理，依赖登录 |

所以「WOL 唤醒 → 自动干活」这条链，现在断在第二步。

### 9.3 开机自启的三层与"能不能干活"的取舍

**技术上能做 ≠ 体验上可用**。这是最需要用户决策的地方。

| 层 | 做什么 | 需要交互桌面? | 实现 |
|---|---|---|---|
| L1 molt-agent | 租约、同步、起停 AstrBot | ❌ | Windows 服务（SYSTEM） |
| L2 AstrBot | 聊天、听歌、看 B 站 | ❌ | 由 L1 起的子进程（会继承 session） |
| L3 交互式桌面 | 直播（OBS）、打游戏（Steam） | ✅ **必须** | AutoAdminLogon + 禁锁屏 + At logon |

**路线 A1：Windows 服务（推荐给 headless）**

```
sc.exe create MoltAgent binPath= "C:\...\molt-agent.exe" start= auto obj= LocalSystem
sc.exe failure MoltAgent reset= 86400 actions= restart/5000/restart/10000/restart/30000
```

本机已有 LocalSystem 服务先例（`NvContainerLocalSystem`、`ps_service`、`MSI_Central_Service`、`com.volcengine.corplink.service`），**服务路线在这台机器上被证明可行**。

用 WinSW（一个 exe + 一个 xml，无需安装）比 NSSM 更轻。二者本机都**没装**（`NOT FOUND`），需要下载或自带。

**路线 A2：计划任务（兜底）**

```powershell
schtasks /create /tn MoltAgent /tr "C:\...\molt-agent.exe" /sc onstart /ru SYSTEM /rl HIGHEST /f
```

优点：无第三方依赖。缺点：**没有进程级自动重启**，只能靠任务计划自身重试 + `RestartOnFailure`（本机 `Coopanion-WorkBuddy-Relay` 已经这么用了：`Count=3 Interval=PT1M`）。

**路线 A3：AutoAdminLogon（只有需要桌面时才用）**

必须先理解一个硬事实：**session 0 注入不可行**。

- Windows Vista 起服务被隔离在 session 0，没有交互桌面
- `Interactive Services Detection (UI0Detect)` 在 Win10 已移除
- 跨 session 需要 `CreateProcessAsUser` + SYSTEM + `WTSQueryUserToken`，对 D3D/输入注入**不可靠**
- 本机有 `ACE-Tray`（反作弊），这类注入会被**直接判违规**

所以需要桌面的路线只有一条：让机器自己登录进 session 1，然后在这个会话里跑进程。

```
1. AutoAdminLogon=1 + DefaultUserName + DefaultPassword
   （明文风险：HKLM\...\Winlogon\DefaultPassword；更安全用 Sysinternals Autologon，存 LSA Secret）
2. 禁锁屏：ScreenSaveActive=0, ScreenSaverIsSecure=0, DisableLockWorkstation=1
3. 机器人用「计划任务 At logon + InteractiveToken + 最高权限」启动 → 天然在 session 1，能访问 GPU/桌面
```

**为什么必须禁锁屏**：锁屏态下 `pyautogui` 键鼠注入基本失效，OBS 的 `game capture` 黑屏、`monitor capture` 的 DXGI 桌面复制会停。等于机器人"活着但瞎了"。

**RDP 不适合当远程控制通道**：Win10 单会话，RDP 连接会**顶掉** console 会话；断开后进程还在 session 1 但桌面变锁屏 → 又回到上面那个坑。要遥控就自己开 HTTP / OBS WebSocket，别用远程桌面。

【实测】当前状态：`query session` 只有 `services(0,Disc)` 与 `console(2,Active,13553)`；无 RDP 会话；`fDenyTSConnections=1`；`AutoAdminLogon`/`DefaultUserName`/`DefaultPassword` **全空**；`AutoLogonSID=S-1-5-21-...-1001`（曾登录过）；`DisableCAD=1`；`ScreenSaveActive=1`；`LogonUI=0`（当前未锁）。

**建议默认**：**headless 走 A1（服务）**，A2 兜底；desktop 模式作为显式开关，配置里写清明文密码风险。

### 9.4 AstrBot 原生跑 Windows 的可行性 —— **已验证成立** ✅

【实测 2026-11-14 12:02】在真实目标机上完整跑通，全程 **219 秒**：

| 关卡 | 结果 | 证据 |
|---|---|---|
| Python 3.13 venv | ✅ | `py -3.13 -m venv`，3.13.5 / sqlite 3.49.1 |
| `pip install -r requirements.txt` | ✅ **退出码 0** | 218.6 秒装完 178 个包 |
| 有没有包缺 3.13 wheel | ✅ **一个都没有** | 只有 `aiocqhttp`、`jieba` 是纯 Python 包现场构建（无需编译器），其余全部命中 `cp313-win_amd64` 预编译 wheel |
| `numpy / pandas / faiss-cpu` | ✅ | 三个最重的 C 扩展全部有 cp313 win wheel |
| `audioop`（3.13 已移出标准库） | ✅ | 上游已用 `audioop-lts ; python_full_version >= '3.13'` 兜住 |
| `import astrbot` / `astrbot.core` | ✅ | 包本身可导入 |
| 真实启动 | ✅ | 打印 AstrBot v4.29.4 ASCII banner，`AstrBot started.` |
| dashboard | ✅ **HTTP 200** | `WebUI listens on all interfaces`，`0.0.0.0:6185`，返回 3884 字节 index.html |
| 数据库自建 | ✅ | 生成 `data_v4.db` 237 KB + `-shm`/`-wal`、`knowledge_base/kb.db` |
| `ASTRBOT_ROOT` 覆写 | ✅ **关键** | 数据全落在 `G:\molt-p0\root\data\`，源码目录零污染 |

**最关键的一条（整个 Soul/Shell 方案的地基）**：

> `ASTRBOT_ROOT` 环境变量**真的**能把全部可写状态重定向到一个独立目录。
> 这意味着"同一个 AstrBot 程序 + 可替换的 data 目录"在 Windows 上是官方支持的用法，不需要改核心代码、不需要打补丁。

**与容器里的 requirements.txt 的重要差异**（实现时要记住）：

- 源码仓库 `requirements.txt` = **65 行**的普通版本约束（`>=`），可以正常 `pip install`
- 容器里那份 = **3289 行**的 `uv` 哈希锁文件（`--hash` + `-e .`），是 Dockerfile 里 `uv export --frozen` 在 Linux 上生成的
- **Molt 在 Windows 端必须用源码仓库那份**，绝不能拿容器那份去 `pip install`（哈希锁里不含 win 平台产物）

**WOL 之外的第二个好消息**：启动时它自己从 `https://models.dev/api.json` 拉了 4173 个 LLM 元数据、从官方 T2I 端点拿到 2 个端点——说明 Windows 端**网络出站完全正常**，没有被代理/防火墙挡住。

### 9.5 Windows 端的卫生清扫（低成本、高收益）

这些和 Molt 本身无关，但不做会持续制造"灵异故障"：

| 项 | 现状 | 动作 |
|---|---|---|
| 网卡 arm 状态不跨重启保留 | 每次冷启动后 `wake_armed` 里都没有网卡 | **装 `Molt-WolGuard` 计划任务**（`tools/install-wol-guard.ps1`），每次开机自动重新 arm。见 §7.4 |
| Realtek 驱动太旧 | `10.54.1111.2021 / 2021-11-11`，`Get-NetAdapterPowerManagement` 报 Error 31 | **不需要更新**——实测旧驱动 WOL 正常工作，Error 31 只是 WMI 那条路读不了 |
| 快速启动污染 S5 | ~~`HiberbootEnabled=1`，`hiberfil.sys` 6.8 GB~~ | ✅ 已 `powercfg /h off`，`HibernateEnabled=0`，`hiberfil.sys` 已删除，持久有效 |
| 时间会漂 | `w32time Stopped/Manual` | 设为 Automatic 并启动 |
| 网络档案是「公用」 | `ASUS_D4 以太网 Public` | `Set-NetConnectionProfile -InterfaceAlias 以太网 -NetworkCategory Private`（否则 SMB/自定义端口入站被拦） |
| GPU 功耗上限 285 W | 空闲仅 10.6–11.0 W（210 MHz） | 可选 `nvidia-smi -pl 200`，几乎不掉性能 |

**注意这两行的差别**：快速启动那一条是**一次性修完就永久有效**的（改的是持久配置），
而 arm 那一条改的是**易失的运行时状态**，重启即失效。
在 Windows 上排查类似问题时要先分清这两类，否则会把"需要每次做"误当成"做一次就好"——
本项目正是在这里犯过一次错（见 §7.4 与 §7.4.3）。

**功耗账**（0.55 元/kWh）：空闲整机估 90–130 W → 约 **44 元/月**；中等负载 250 W → 约 100 元/月；满载 400 W → 约 159 元/月。

这直接决定"迁徙值不值"：**让 Windows 24h 常开 ≈ 44–100 元/月**，而 NAS 常开只有它的零头。所以 Molt 的价值不只是"能过去"，更是**"用完就让它睡"**——回家之后关机，只在需要 GPU/桌面时才醒来。

但要注意：**「回家」和「关机」是两件事**（§5.4 / §5.5）。省电是关机的理由，
不是把关机塞进交接流程的理由 —— 机器前有人时，鱼照样能回去，只是这台机器今晚不省那点电。

### 9.6 关机与防"关不掉"

**注意：`molt home` 是未来形态（v0.3），而且它内部是两件事，不是一件。**
用户跑一次 `molt home`，它会先做交接、再做关机尝试——但**交接成功与关机成功是两个独立的结论**，
第二个失败不影响第一个（见 §5.4 / §5.5）。

```
molt home（v0.3 设想）：
  phase 1  交接（必须成功）
    drain → stop → snapshot → push → Warden accepted → release
    失败 → 就地停下，报错，鱼留在 Windows（fail-safe = 留在原地）

  phase 2  关机（成功与否都不影响 phase 1 的结果）
    if (!push_accepted)        → 不关机（本来就没到这一步）
    if (有白名单外的前台程序)   → 拒绝关机，改为通知
    if (用户没给 --force 且拒绝) → 不关机，打印怎么手动关
    else                       → shutdown /s /t 60 /c "Molt: 记忆已回传，60 秒后关机"
                                 60 秒缓冲，给用户一个 Ctrl+C 的机会
```

**关键**：phase 2 的任何分支都**不会**回头去动 phase 1 的结论。
鱼回没回家，只看 phase 1；机器关没关，只是省不省电。

**必须有的逃生阀**：如果有人正在用这台机器打游戏，`molt home` 不能直接关机。规则：

- 检测到前台进程属于白名单外（游戏、OBS 录制中、有未保存文档的字处理）→ 拒绝关机，改为通知
- 或者要求 `molt home --force`

---

## 10. 性能预算

全部基于【实测】的热备数据：容器在跑、库在被写的情况下

| 项 | 实测值 | 口径 |
|---|---|---|
| `data_v4.db` online backup | **0.017 s** | 10,887,168 B |
| `memory_companion.db` online backup | **0.025 s** | 17,727,488 B |
| `VACUUM INTO` data_v4.db | **0.015 s** | 产出 6,848,512 B（**小 37%**） |
| 两库 quick_check | ok | |

库体检：

- `data_v4.db`：page_count 2658 / freelist 984
- `memory_companion.db`：page_count 4328 / freelist **3381（78% 是空闲页）** ← VACUUM 收益巨大

**推断的交接耗时预算**：

| 阶段 | 预计 | 主要变量 |
|---|---|---|
| Drain（等在飞消息） | 1–10 s | 有没有长工具调用 |
| AstrBot 优雅停机 | 3–8 s | 插件数量（当前 7 个） |
| checkpoint + VACUUM + integrity_check | < 1 s | 库大小（25 MB） |
| 传输 25 MB（千兆） | 0.3–2 s | SMB 小文件开销 |
| WOL + Windows 开机到 agent 报到 | **30–60 s** | 主板自检、Windows 启动 |
| AstrBot 冷启动到 dashboard 200 | **20–40 s** | 加载 7 个插件 + 1325 MB site-packages |
| **合计（迁徙）** | **60–120 s** | 开机占大头 |

归巢没有开机时间，**预计 15–30 s**。

**优化点**：Windows 可以"预启动"——收到 wake 后先起 agent，但 AstrBot 等到确认收到最新 gen 才起。这样重叠了一部分。

---

## 11. 备选方案与取舍

### 11.1 对比矩阵

| 方案 | 真并发写 | RPO | Windows 可行 | 改代码量 | 结论 |
|---|---|---|---|---|---|
| **Molt（租约 + checkout/checkin）** | ❌ | 0（干净交接） | ✅ | 中 | **✅ 采用** |
| 全部留 NAS，Windows 只远程接入 | ❌ | 0 | ✅ | 低 | 降级/保底模式 |
| 换 Postgres / MySQL | ✅ | 0 | ✅ | 高 | v2 演进方向 |
| rqlite | ✅（Leader 串行） | 0 | ✅ | 高 | v2 候选 |
| dqlite | ✅ | 0 | ❌ **仅 Linux** | 高 | 排除 |
| LiteFS | ❌ | 亚秒 | ❌ FUSE+Consul | 高 | 排除 |
| Litestream | ❌ 单写者 | ~1 s | 只读副本 | 中 | 只做备份 |
| DRBD | ❌ | 0 | ❌ 挂不上 | 高 | 排除 |
| SMB/NFS 直挂共享 | ❌ | — | ❌ | 0 | **禁止** |

### 11.2 为什么不做第 2 个（全部留 NAS）

这是**最稳的选项**：WAL 在本地磁盘，零损坏风险，RPO=0。代价是违反"进程走"的初衷——Windows 只是个瘦客户端。

但对本项目而言，这个降级路径必须保留，理由：

- 如果 v0.1 的 Windows 原生安装失败（§13 第 6 条），用这个兜底仍然能实现"在 Windows 上开直播"（远程桌面 + NAS 上的鱼指挥 OBS）
- 用户不在家时，这是默认模式

**所以 Molt 设计成 `shell` 可插拔的：`nas` / `win` / `remote`。`remote` 模式就是方案 2。**

### 11.3 "两只鱼同时在线"这条需求已经关闭

**用户已明确否决（m00434）**："理论上不应该允许两只鱼同时在线……我希望这是一个整体"。

所以这一节不再是"待决策项"，而是**记录我们为什么坚定地不做**：

| 如果真做了会怎样 | 后果 |
|---|---|
| 两条鱼同时收消息 | 一条消息两份回复。那不是"一个存在"，那是"两个机器人" |
| 两条鱼各自写记忆 | 记忆分裂成两份互相打架的副本。**这正是本项目要解决的问题本身** |
| "它现在在哪？" | 无解。一个存在不能同时在两个地方 |

**技术上能不能做到真并发？能，但那是另一个项目。** 需要：

- 换 Postgres（AstrBot 只有 sqlite 后端：`/AstrBot/astrbot/core/db/` 下只有 `sqlite.py`、`po.py`、`migration/`、`vec_db/`，`self.DATABASE_URL = f"sqlite+aiosqlite:///{db_path}"` 在 `sqlite.py:80`）
- 或者 rqlite（HTTP API，同样要改存储层）
- 且 memory_companion 的 41 张表要重写存储层
- 且还要解决"两条鱼同时回消息"这个产品问题——它无解

**结论**：即使技术门槛降到零，本项目也不会做。见 §2.2 的 C2。

### 11.3.1 互斥如何被强制保证（本方案的核心工程）

公理写在文档里没用，**必须有机制让它成立**。本方案有**五层防线**，任何一层单独失效都不会导致双活：

| 层 | 机制 | 失效会怎样 | 强度 |
|---|---|---|---|
| **L1 平台层** | QQ 官方机器人同一 appid 双实例会互踢（WebSocket `ManagedBotWebSocket`，`qqofficial_platform_adapter.py:303-306`） | 其中一条被踢下线 | ⚠️ **弱**。官方文档查不到互踢明文，只有 `session_start_limit` 配额。**绝不能当 fencing 用**，只能当事后告警 |
| **L2 租约层** | Warden 独占发租约，带 TTL + 单调 epoch | 超时的租约自动回收，新壳可接管 | ✅ 强（但有 GC 停顿风险） |
| **L3 fencing 层** | 每次写 Soul 都带 epoch，**Warden 拒绝 epoch 倒退的写入** | 一个被 GC 暂停的旧壳醒来写盘 → 被拒 | ✅ **强，这是真正的屏障** |
| **L4 顺序层** | 铁律"先死后生"：老壳确认死亡 → 新壳才出生 | 老壳没死透，新壳不启动 | ✅ 强（流程保证） |
| **L5 人工层** | `molt status` 显示"当前在线的是谁、第几代、心跳多久前" | 用户可以肉眼发现异常 | ✅ 兜底 |

**为什么必须有 L3 而不是只靠 L2**：Kleppmann 那篇 `How to do distributed locking`（2016-02-08）里说得最狠的一句是

> "**GC can pause a running thread at any point**"

以及

> "You cannot fix this problem by inserting a check on the lock expiry just before writing back"
>
> "This requires the storage server to take an active role in checking tokens, and **rejecting any writes on which the token has gone backwards**"

**L2 只有"租约没过期"这一个判断，而任何基于时间的判断都可能被一次长时间停顿打穿。L3 才是"存储端主动拒绝"，这是唯一可靠的写法。**

**L2+L3 的实现要点**（Warden 侧）：

```python
# checkout：发租约，epoch 单调递增
epoch = max_stored_epoch + 1
lease = {"epoch": epoch, "shell": shell_id, "expires_at": now + TTL}

# 收到写入：先验 epoch，再落盘
def accept_snapshot(snapshot, claimed_epoch):
    if claimed_epoch < state["current_epoch"]:
        reject("epoch 已过期")          # ← 这是 L3 的核心，一行代码
    if claimed_epoch > state["current_epoch"]:
        reject("epoch 超前")
    # 才轮到 integrity_check / 行数指纹
```

**注意**：`claimed_epoch < current_epoch` 时**必须拒绝并且不覆盖**，而不是"记录一下继续写"。这一行如果写错，整个 fencing 机制就废了。

### 11.3.2 一个真实存在的诱惑：AstrBot 的 restart

AstrBot 有个 `core_lifecycle.restart()`（`core_lifecycle.py:435`），最后会
`threading.Thread(target=restart_process).start()` 重新 exec 自己。

看起来很方便——"让老壳自己重启一下，新壳就能上了"。

**但这是错的**：restart 期间进程还活着，它仍然持有 platform 连接。如果此时新壳启动，
两者会短暂**同时在线**，直接违反 C2。

**只允许在 `terminate()` 完成、进程真正退出之后，才允许新壳启动。** 见 §5.2 的顺序铁律。

### 11.3.3 怎么在一台机器上确认「只有一只鱼」—— 以及我在这里犯过的两个错

#### 判据：看端口，不看进程名

`Get-CimInstance Win32_Process … CommandLine -like "*astrbot*"` **认不出 AstrBot**：
它在 Windows 上是以 `python.exe main.py` 起的，命令行里没有产品名。
所以"我现在有几只鱼"这个问题只能由**端口**回答：

```powershell
Get-NetTCPConnection -LocalPort 6199 -State Listen | Select-Object OwningProcess
```

一个壳里出现两份 `main.py` 时，只有一份能 bind 6199；
**但"端口只被一个占着"不等于"另一个没在说话"** —— 它可能已经把 QQ 的 websocket 建起来了。
所以还要再验一层：`astrbot.log` 里 `Websocket session starting` 在这个时间窗内出现了几次。

#### 错一：把「进程还在」当成「它还在干活」的反面，也错

2026-11-15 04:12 那轮出现过两个 `python.exe main.py`，同一秒启动，其中一个的父进程是另一个。
它们的运行数据差异巨大：

| pid | CPU 时间 | 工作集 | 线程 |
|---|---|---|---|
| 20552 | **0 s** | **3 MB** | **1** |
| 24592 | 47.9 s | 987 MB | 40 |

我据此判断「20552 是尸体」，`Stop-Process -Force` 掉它 —— **结果 24592 也一起没了**：
20552 是 **Job Object 的持有者**，子进程绑在它的 job 里，杀父即杀子。
（`Get-NetTCPConnection` 那一列可以证明 20552 自己确实一个 socket 都没有，
所以"它没在干活"是对的，**错的是"所以可以随便杀它"**。）

> **教训：`0 CPU / 3 MB / 1 thread` 说明它不在干活，不说明它是孤儿。**
> 一个壳里的进程关系比进程状态更重要 —— 杀之前先看 `ParentProcessId` 和 job 归属。

#### 错二：删掉「已经生效」的假设

同一轮还有个更贵的教训：改完服务端代码、把文件拷上去之后，
请求 `POST /backup` 报 `请求体不是合法 JSON（HTTP 400, kind=bad_request）`。
原因不是协议 —— 是**跑着的进程还是旧代码**，路由压根不存在，
请求落进了 `_read_body()` 的 JSON 分支。

> **把代码拷到机器上，和让机器跑那段代码，是两件事。**
> 本项目在插件、三个守护进程、warden 上都踩过同一条，见 §5.7.6。

#### 顺带被验证的：误杀之后它能自己回来【实测 04:40】

误杀把 AstrBot 整个带走之后，Windows 侧 agent 在下一轮就发现了：

```
04:40:34 [idle] 第 15 代我已经落过了
  第 15 代是我取的，但端口没人听 —— 我去把它拉起来
  ✓ 已拉起 pid=16744
04:40:46 [idle] 第 15 代我已经落过了
  167 秒前刚拉过 AstrBot，它可能还在启动。这一轮先不重复拉（免得两只鱼同时在线）
04:41:03 [QQOfficial] Websocket session starting.
```

两件事同时被证明：**自愈真的会动手**（判据是"端口没人听"，不是状态文件），
**冷却真的会拦住第二只**（`BOOT_COOLDOWN_SECONDS = 180`，AstrBot 冷启动到 bind 要 20–30 秒）。
那一轮 `[ERROR]` 行数为 0。

### 11.4 Litestream 能不能用

能，但**只能当备份，不能当主链路**。

- Litestream 是"单写者 + 异步复制 + 只读副本"，RPO ≈ 1 s【查证】
- 官方明确："Multiple applications replicating into the same bucket & path can cause situations where you will be unable to restore"
- 反向用（Windows 写 → NAS 读）在"两台不同时在线"下不成立

**用法**：作为 Warden 之外的**第二层保险**——把 gen 目录持续备份到对象存储或另一块盘，防 NAS 整机挂掉。

---

## 12. 演进路线

| 版本 | 目标 | 验收标准 |
|---|---|---|
| **v0.1** 手工验证 | 证明数据不坏 | ~~手工 `molt checkout` / `molt checkin` 10 轮，`integrity_check` 全 ok~~ → ✅ **已超额完成，见 §13.4** |
| **v0.2** 租约 | 证明互斥 | ~~Warden + epoch；故意制造 split brain，验证旧 epoch 被拒~~ → ✅ **已提前完成（含跨机反例），见 §13.5** |
| **v0.3** 自动化 | 端到端 | QQ 一条命令完成迁徙/归巢，全程 < 2 分钟 |
| **v0.4** 硬化 | 故障注入 | 断电、断网、时钟漂移、库损坏注入，全部安全失败 |
| **v1.0** 开源 | 别人能用 | 文档、Docker 镜像、AstrBot 插件市场、CI |

**v0.1 的最小闭环**：

```
1. Windows 上装 AstrBot（Python 3.13 + requirements）   ✅ 已验（§9.4）
2. 手工拷一份 gen-1 过去，起起来，能聊天                ✅ 数据+人格已验（§13.1/§13.3），QQ 收发未验
3. 手工回传，NAS 起回来，对比对话记录完整性             ✅ 已验（§13.4）
```

**第 2 步曾经是整个项目最大的不确定性，现在只剩"QQ 真的能收发"这一小块没验。**
（2026-11-14 更新：v0.1 与 v0.2 的工程目标都已实测通过，实际进度比原计划快了一个版本。）

---

## 13. 验证清单（v0.1 必须先做）

| # | 待验证 | 为什么重要 | 怎么做 | 失败后怎么办 |
|---|---|---|---|---|
| 1 | ~~`docker kill -s INT` 是否触发 AstrBot 优雅停机~~ | 决定 §6.4 走哪条路 | ✅ **已验 13:41：SIGINT 退出码 0、`🌈 Shutting down AstrBot...`、WAL 41,232 B → 0；而 `docker stop`(SIGTERM) 退出码 137、WAL 原封不动**（§6.4） | — |
| 1b | ~~**Windows 上有没有等价的优雅停机信号**~~ | 决定"关得干净"还是"被硬杀" | ✅ **已验 14:44：有，但不是 `os.kill`。`AttachConsole(pid)` + `GenerateConsoleCtrlEvent(CTRL_C_EVENT, 0)` → 退出码 0、`🌈 Shutting down`、`kb.db` 从 45,056 B + 98,912 B WAL 变 94,208 B 且 sidecar 消失**（§13.10） | 插件 HTTP 端点自毁（§6.2）作兜底 |
| 2 | 容器内能否向 `192.168.1.20:9` 发 UDP | 决定 Warden 部署位置 | python socket sendto 后看包到没到 | Warden 移到宿主机 |
| 3 | Windows AstrBot 冷启动耗时 | 影响交接预算 | 计时到 dashboard 200 | — |
| 4 | NAS `python3.10.7` 够不够跑 Warden | 决定 Warden 依赖 | 只用标准库写，跑一遍 | 装 venv |
| 5 | SMB 上 `CREATE_NEW` 的原子性 | 只做对比实验（不打算依赖） | 两进程并发抢建同名文件 | 无所谓，本来就不用 |
| 6 | ~~**Windows `pip install -r requirements.txt` 是否全绿**~~ | **决定项目是否成立** | ✅ **已验：退出码 0，178 包，零缺 wheel**（§9.4） | ~~走 §11.2 降级~~ |
| 7 | ~~BIOS 的 ErP / Power On by PCI-E~~ | WOL 能不能用 | ✅ **已验：用户在现场完成设置**（§7.4） | — |
| 8 | ~~Windows 关机后网卡是否保持待机供电~~ | 同上 | ✅ **已验：12:40 真 S5 关机后被魔术封包唤醒，23.1 秒** | — |
| 9 | SQLite 3.51.0–3.51.2 的 WAL-reset bug 是否影响 | 双连接并发写时的已知 bug | 查版本；本项目天然单机单写者，风险低 | — |
| 10 | ~~`powercfg /deviceenablewake` 能否成功~~ | WOL 缺口 | ✅ **已验：能成功，`PnPCapabilities=8` 亦可用**；⚠️ 但**不跨重启保留**，需 `Molt-WolGuard` 自愈（§7.4） | — |
| 11 | ~~关快速启动后从真 S5 发魔术封包能否唤醒~~ | `HiberbootEnabled=1` 时"关机"是伪休眠 | ✅ **已验：`BootType=0` 证实真 S5，唤醒 PASS**（§7.4.2） | — |
| 11b | ~~`Molt-WolGuard` 计划任务能否在每次开机后自动恢复 arm~~ | arm 不跨重启保留，决定"能不能一直叫醒" | ✅ **已验 13:00:34：重启后开机第 11 秒自愈，`改前 False → 改后 True`。不在 10 分钟网格上，证明是 `AtStartup` 触发器**（§7.4） | — |
| 12 | AstrBot 在 Windows 上装好后能否正常收发 QQ 消息 | 决定整个方案是否成立 | 起本地实例，用测试号发一条 | 走 §11.2 降级 |
| 13 | 服务（session 0）里起的 AstrBot 能否访问 GPU/OBS | 决定 headless/desktop 边界 | 服务里跑一个 D3D 探测 | 桌面能力必须走 AutoAdminLogon |
| 14 | 计划任务 `InteractiveToken` 在无人登录时是否真的不跑 | 验证 §9.2 的断点判断 | 注销后等触发时间，查 `Last Run Result` | — |
| 15 | ~~能不能用一条命令替代"手工四步"~~ | 租约 TTL 只有 90 秒，手工会中途过期 | ✅ **已验 13:50：`molt migrate` 真机 1.43 秒走完拿租约→热备→打包→登记→还租约**（§13.7） | — |
| 16 | ~~落位后配置能否自动适配新机器~~ | 不做就把 NAS 的 dashboard 和 jwt_secret 搬到 Windows | ✅ **已验 14:00：6 处覆写，45 个键只动 3 个，启动后 WARN/ERROR 为 0**（§6.6.6） | — |
| 17 | ~~**跨机字节传输**~~ | 现在 `checkout` 出来还得人手 scp/共享盘 | ✅ **已验 14:16：`migrate` PUT 到 Warden 中转区 → `land --generation N` GET 下来。真机 two commands、real 1.61s**（§13.8） | — |
| 18 | ~~归档中转区的保留策略（只增不减）~~ | 长期会把 NAS 塞满 | ✅ **已验 14:32：`molt prune`，41 个用例 + 3 个变异全抓。账本里记着的一律不删、外人放的一律不删、默认只看不删**（§13.9） | — |
| 19 | **归档 transfer 的失败路径**（断开/超限/坏 sha） | 一个静默的半成品比失败更危险 | ✅ **已验：20 个用例 + 变异测试**（去掉 `.part` 写法会被"中途断开"抓住，§13.8.4） | — |
| 20 | ~~**迁徙的第一步（把机器叫醒）能不能也进产品**~~ | 需要人手发一个魔术封包，前面所有自动化都白搭 | ✅ **已验 14:23：`molt wake` 进了产品，且 `migrate` 里是「先唤醒 → 再拿租约」，调用顺序有测试固定**（§13.9） | — |
| 21 | 归档中转区的**断点续传** | 现在失败就重传整份 | 4.4 MB 无所谓，30 MB 以上值得做（§13.8.5） | — |
| 22 | **落位后 `platform[].enable` 有没有被关掉** | **漏了就是两条鱼同时在线，直接违反公理 0** | ✅ **已验 14:56：`land` 输出第一行 `✓ 关闭平台适配器`；起 AstrBot 后 `Loading IM platform adapter` / `QQOfficial` / `Websocket session` 全部零命中，而库里的它完好**（§6.6.7） | — |


### 13.1 已经跑通的端到端往返（2026-11-14 12:06）

v0.1 的最小闭环里，第 2 步「手工拷一份 gen-1 过去，起起来，能聊天」的**数据部分已经用真实数据验证通过**。链路：

```
NAS /mnt/dm-0/astrbot/data          ← 容器正在跑、库正在被写
        │  sqlite3 backup API（只读 URI 打开源库，不停止 AstrBot）
        ▼
gen-000001/{data_v4.db, memory_companion.db, manifest.json}
        │  tar czf  →  scp
        ▼
Windows G:\molt-p0\transfer\gen-000001.tar.gz
        │  解压
        ▼
G:\molt-p0\transfer\restore-test\gen-000001\
        │  Python 3.13 + sqlite 3.49.1 打开，读数
        ▼
真实记忆内容可读 ✅
```

**实测数字**：

| 环节 | 结果 |
|---|---|
| 在线备份（容器不停机） | `data_v4.db` 10,887,168 B / **0.270 s**；`memory_companion.db` 17,727,488 B / **0.271 s** |
| 备份产物自检 | 两者 `PRAGMA quick_check` 均为 `ok`；表数 `21 / 41` 与源库**完全一致** |
| 打包 | 25.6 MB → `gen-000001.tar.gz` **4,600,524 B**（压缩率 18%） |
| 跨机传输 | SHA256 两端一致（`e4184660…6918b`） |
| 逐文件内容一致性 | `data_v4.db` = `e87fdef5…1c021`、`memory_companion.db` = `1ce3d62b…d6af9`，**两端完全相同** |
| Windows 侧读库 | `journal_mode` 还原为 `wal`、`quick_check` ok |
| 主库内容 | `conversations` 2 / `platform_message_history` **139** / `personas` 1 |
| 记忆库内容 | `memories` **71** / `knowledge_nodes` **160** / `knowledge_edges` **257** / `identities` 3 / `emotion_events` 6 / `injection_logs` 75 |

**最有说服力的一条**：从还原后的库里能直接读出大肥鱼的真实记忆原文，例如
`2026-11-14T03:17:03` 那条：**「2026-11-14 凌晨两点多，老大还醒着，先跟我打了招呼……」**

### 13.2 这次实测确认了什么、没确认什么

**确认了**（从【推断】升级为【实测】）：

1. **热备不需要停 AstrBot** —— 以 `file:...?mode=ro` 只读 URI 打开正在被写的库，`backup()` 全程成功，25 MB 级别 < 0.3 s。这否掉了"必须先停容器才能备份"的错误假设，也是 `molt` 能做到"无感"的前提。
2. **backup API 产出的确实是自洽快照** —— 目标库 `quick_check` ok 且表数与源库一致，不是"拷到一半的文件"。
3. **跨 Linux → Windows 的字节级一致可以保证** —— SHA256 逐文件相同。这证明了 `manifest.json` 里的 sha256 校验设计是可行的，而不是空想。
4. **Windows 端的 SQLite 能直接打开这份库** —— 3.13.5 / sqlite 3.49.1 打开由容器内（Linux，sqlite 版本不同）产出的库，`journal_mode`、`quick_check`、表结构、中文内容全部正常。**没有字节序、路径、编码问题。**
5. **manifest 的字段设计够用** —— 记录了 `gen`、`created_at`、`source_host`、每个 artifact 的 `bytes/sha256/tables/quick_check/backup_seconds`。这正是 Warden 判"这份快照能不能收"所需的全部信息。

**没有确认**（仍是【待验证】）：

1. ~~AstrBot 自己能不能消费这份还原的 data 目录~~ —— **已确认，见 §13.3**
2. ~~**归巢方向**（Windows 写 → NAS 接）~~ —— **已确认，见 §13.4**
3. **插件运行时的额外状态**——`plugin_data/meme_manager` 172 MB 的表情包、`skills/` 等本次没打包，只验了两个核心库。

### 13.3 AstrBot 用还原的数据启动 —— ✅ 已通过【实测 2026-11-14 12:07】

§13.1 只证明了"sqlite 层面可读"。这一节证明**AstrBot 自己认得这份数据**。

**做法**：把还原的两个库摆成 AstrBot 期望的布局（`data/data_v4.db` + `data/plugin_data/astrbot_plugin_memory_companion/memory_companion.db`），连同一份配置，然后 `ASTRBOT_ROOT=G:\molt-p0\winroot` 起 `main.py`。

**为了守住公理 0，配置做了三处改动**（这也是未来 `$MOLT_SHELL` 覆写层要自动做的事）：

| 改动 | 前 | 后 | 为什么 |
|---|---|---|---|
| `platform[0].enable` | `true` | **`false`** | **绝不让 QQ 平台在测试实例上连接**——同 appid 双实例会互踢，这是真实风险 |
| `provider_sources` 剔掉 `workbuddy` | 2 个 | 1 个 | 它指向 `http://127.0.0.1:17863/v1`，那是 NAS 上的桥，Windows 没有 |
| `dashboard.host` / `port` | `0.0.0.0:6185` | `127.0.0.1:6199` | 避免与真实实例抢端口 |

**结果**：

| 项目 | 结果 |
|---|---|
| 启动 | ✅ `AstrBot v4.29.4` → `AstrBot started.` |
| WebUI | ✅ `Starting WebUI at http://127.0.0.1:6199`、`WebUI is ready`、`HTTP 200`、用户名读到了 `admin` |
| 人格加载 | ✅ `personas` 表读出 `persona_id=default`，`system_prompt` 开头是 **「# 我是谁 / 我叫 DeepSeek 大肥鱼，大家叫我大肥鱼 / ## 性子 / 好奇，什么都想凑过去看一眼……」** |
| 运行后完整性 | ✅ 两个库仍 `quick_check=ok`、`journal_mode=wal`，行数**一字未变**（conversations 2 / platform_message_history 139 / memories 71 / knowledge_nodes 160 / knowledge_edges 257） |

**这是"人格能过桥"最直接的证据**：不是文档里说可以，而是另一台机器、另一个操作系统上，AstrBot 把这条鱼的自我认知读出来了。

**同时暴露了三个必须在 v0.1 处理的真实问题**（不是猜测，是日志里出现过的）：

| # | 现象 | 根因 | 正确处理 |
|---|---|---|---|
| P-1 | `ModuleNotFoundError: No module named 'edge_tts'` / `'whisper'` | 这两个包**不在源码 `requirements.txt`** 里（实测 grep 无命中），是 AstrBot 运行时 pip 装到 `data/site-packages/` 的（见 §7.1.1） | 新壳必须按 manifest 的 `runtime_pip` 清单重装。**不能靠同步目录** |
| P-2 | `KeyError: 'type'`（`provider.manager:372`） | 配置里有 provider 条目缺 `type` 字段（`deepseek/deepseek-flash` 与 `workbuddy/…` 都没有 `type`，只有 `provider_source_id`）。这是**原配置自带**的，不是还原引入的 | 说明"配置文件跨机搬运不保证无痛"。**overlay 层要做的是"删掉本机不可用的 provider"，而不只是改地址** |
| P-3 | `WARN ... provider ID 'workbuddy/deepseek-v4.1-flash' does not match an enabled provider. AstrBot will use 'deepseek/deepseek-flash' as the fallback` | 同 P-2 | **好消息**：AstrBot 自己优雅降级了，没有崩。这证明"provider 缺了不会死"，降低了跨机风险 |

**顺带发现的跨平台细节**：`cmd_config.json` **带 UTF-8 BOM**（前 4 字节 `ef bb bf 7b`）。AstrBot 用 `utf-8-sig` 读所以没事，但任何用 `json.load()` 直接读它的工具都会炸（本次实测 Linux python3.10 报 `JSONDecodeError: Unexpected UTF-8 BOM`）。**Molt 自己读写配置时必须用 `utf-8-sig`，写回时也要保留 BOM**，否则 AstrBot 可能认不出（待验证，保守起见照原样保留）。

### 13.4 归巢方向（Windows → NAS）—— ✅ 已通过【实测 2026-11-14 13:37】

§13.1 只做了单向。归巢要难得多：Windows 上的库是**活的**，而且 NAS 上的 Warden 在另一台机器上。

**做法**：先在 Windows 库里**真的写一条数据**（否则"搬回去"无法证伪——库没变的话，
搬过去也看不出任何差别），然后走完整的反向链路。

| # | 动作 | 结果 |
|---|---|---|
| 1 | Windows AstrBot 用 venv313 起在 6199 | ✅ `AstrBot started.`，但**启动本身不写库**（sha256 不变） |
| 2 | 手动往 `conversations` 插一条标记行 | ✅ `conversations: 2 → 3`，`title="归巢验证 MOLT-RETURN-TRIP-20261114T133640"` |
| 3 | Windows `molt checkout`（走**远端** Warden） | ✅ gen-2 / 27.3 MB / `epoch=3` / 热备 `data_v4.db` **0.042 s**、`memory_companion.db` **0.067 s** |
| 4 | 打包 + 上传（4,624,316 B） | ✅ 两端 sha256 `c2b03d5deb06dc43…9401aa` **一致** |
| 5 | NAS `molt verify` | ✅ 逐文件 sha256 + 表数（21 / 41）全过 |
| 6 | NAS `molt push` | ✅ 登记 gen-2，提交者 `win-shell`，**目标壳 `nas-shell`** |
| 7 | Windows `release` → NAS `acquire` | ✅ **epoch 3 → 4**，先死后生成立 |

**判决性证据**（三方对比同一时刻的三份 `data_v4.db`）：

| | conversations | platform_message_history | personas | `molt_test` 标记行 |
|---|---|---|---|---|
| NAS 原库 | 2 | 139 | 1 | 0 |
| **Windows 归巢快照** | **3** | 139 | 1 | **1** |
| NAS 接管后新拍 | 2 | 139 | 1 | 0 |

**三件事同时被证明**：

1. **Windows 上写的数据真的搬回 NAS 了** —— 多出来的那 1 条 conversations 就是标记行，标题一字不差。
2. **NAS 自己的库没被污染** —— 归巢快照是独立目录，NAS 原库在整条链路中一条记录都没动（`conversations` 仍是 2）。这验证了"快照先行、原地不动"的安全设计。
3. **活库热备在 Windows 上也成立** —— `data_v4.db` 热备只用 0.042 s，比 NAS 侧的 0.27 s 还快（本地 SSD vs NAS RAID0 + 网络）。

**最终账本**（`/mnt/dm-0/astrbot/_molt/warden-root/state.json`）：

```
current_epoch        : 4
last_accepted_epoch  : 3
rejected_write_count : 0
lease                : nas-shell epoch 4
generations          : 2
   gen 1  pushed_by=nas-shell  target=win-shell     epoch=1  27.3 MB
   gen 2  pushed_by=win-shell        target=nas-shell  epoch=3  27.3 MB
```

两条**方向相反**的迁徙记录并排躺在同一个账本里 —— 这就是"往返"的全部含义。

### 13.5 跨机互斥（公理 0）—— ✅ 已通过【实测 2026-11-14 13:30】

§13.4 证明数据能搬。这一节证明**搬的时候不会两只鱼同时活着**。

**反例测试**（这是最容易造假的地方，所以专门构造）：NAS 仍持 `epoch=1` 的租约时，
Windows 直接 `acquire('win-shell')`：

```
被正确拒绝 -> LeaseHeld
租约由 nas-shell 持有，还剩 65.3 秒。
任意时刻只能有一个存在在线（公理 0）。
```

**正例测试**（先死后生）：NAS `release --epoch 1` → Windows `acquire` →

```
接管成功 / epoch 2 / TTL 90 秒
```

断言 `epoch == 2` 通过 —— **新壳拿到的一定是更大的 epoch，不是复用**。

**为什么这条比数据搬运更关键**：数据搬错了可以重搬一次；两只鱼同时在线一次，
用户看到的是"一条消息两个回复"，那一刻这个项目就失去意义了（§2.2 的公理 0）。

**实现上的三层保证**（缺一层都会漏）：

| 层 | 机制 | 本块验证方式 |
|---|---|---|
| 进程内 | `epoch.accept_write()` 三步判定 | 23 个单测 + **6 个变异测试全部被抓住** |
| 进程间（同机） | `_InterProcessLock` + 锁内强制 reload | `test_concurrency.py` 起**两个真 subprocess**，连跑 5 轮每轮恰好一个赢 |
| 跨机 | Warden HTTP 单点 + 文件锁 | 本次反例/正例，两个真机器 |

**中间踩到的真 bug（值得记住）**：第一版 `Warden` 是"读 state.json → 判断 → 写回"，
37 个单测全绿。但两个**真进程**各自 `load()` 时都看到"无租约"，然后各自 `save()`，
后写覆盖先写 —— **两个都拿到了 epoch 1，都写成功了**。
单测测不出来是因为线程共享同一个 Python 对象和 GIL，切换点比真实进程少得多。
修法两条，缺一不可：①加跨进程排他锁 ②**每次进锁后必须重新从磁盘 load()**
（只加锁不重载是无效的：A 持锁期间内存状态可能早被 B 改过）。

### 13.6 v0.1 代码骨架 —— ✅ 已完成，15 模块 + 控制面插件 / 6,946 行 / 414 测试【v0.8 更新于 2026-11-14 22:46】

**15 个核心模块 7,094 行 + `plugin/` 三个文件 666 行 = 7,760 行源码 / 23 个测试文件 7,852 行 / 440 个测试全绿（32.5 秒）/ 测试源码比 1.01**

| 模块 | 大小 | 行 | 职责 |
|---|---|---|---|
| `cli.py` | 76,180 B | 1953 | 13 个子命令 + GBK 降级 + `--warden-url` 全局参数 + `migrate` / `land` / `wake` / `prune` + **`--control-url` 按住源壳**（§6.2.11） |
| `warden.py` | 36,542 B | 739 | 本地控制面：原子写、跨进程文件锁、租约、generation 账本、归档中转区、**保留策略**（§13.9） |
| `server.py` | 24,538 B | 528 | HTTP 控制面（纯标准库，**11 端点 + 归档 PUT/GET 流式**，`X-Molt-Token` 常量时间认证） |
| `shell.py` | 23,795 B | 585 | 身份/能力探测、`preflight()` 三态判决（proceed / self_heal / block） |
| `soul.py` | 18,834 B | 411 | 热备（只读 URI + `backup()`）、sha256、manifest、`collect_runtime_pip()` |
| `transfer.py` | 16,735 B | 349 | **打包/校验/解包/落位**：路径穿越七道检查、sha256 前置、WAL 非空拒绝 |
| `client.py` | 15,613 B | 343 | 壳端客户端（超时、仅连接层重试、kind → 异常映射、归档上下行 + `.part` 保护、`prune()`） |
| `overlay.py` | 15,458 B | 310 | **机器覆写层**：**7 处覆写**纯函数（第 0 条是公理 0 护栏），`utf-8-sig` 保 BOM（§6.6） |
| `wake.py` | 14,272 B | 297 | **魔术封包**：三路齐发、`directed_broadcast()`、`wait_online()` 带预算的探测（§13.9） |
| `backend.py` | 11,845 B | 260 | 抹平本地/远端差异，含归档上传/下载与 `prune()`（§13.8） |
| `control.py` | 11,797 B | 243 | **AstrBot 控制面客户端**：标准库手写 HS256、`drain_and_verify()` 轮询复核（§6.2） |
| `epoch.py` | 9,948 B | 217 | **fencing 核心**：`Lease` / `FencedState` / `accept_write()` / `is_safe_to_start()` |
| `errors.py` | 2,493 B | 71 | 10 个异常类，`kind` 属性驱动 HTTP 状态码与处置策略 |
| `__init__.py` | 1,400 B | 31 | 包文档 + 导出 |
| `version.py` | 719 B | 13 | `__version__` / `PROTOCOL_VERSION` / `REQUIRED_CAPABILITIES` |

**AstrBot 侧插件**（`plugin/`，装进 AstrBot 才是活的）：

| 文件 | 大小 | 行 | 职责 |
|---|---|---|---|
| `main.py` | 19,436 B | 435 | `MoltControlPlugin(Star)`：`drain()` / `resume()` / `platform_snapshot()` / `wal_snapshot()` / `checkpoint()`；`_classified()` 分 outward 与 local_only |
| `api.py` | 6,742 B | 161 | 四端点 `/status` `/drain` `/resume` `/checkpoint`；`speaking` 是给 Warden 的唯一结论字段 |
| `metadata.yaml` | 889 B | 17 | 插件元数据（`astrbot_version: ">=4.24.0"`） |

**测试分布**：`test_soul.py` 541 行 / `test_warden.py` 499 / `test_migrate.py` 490 / `test_transfer.py` 483 /
`test_overlay.py` 448 / `test_plugin_control.py` 401 / `test_archive_transfer.py` 396 / `test_shell.py` 395 /
`test_server.py` 373 / `test_migrate_drain.py` 354 / `test_transport_e2e.py` 338 / `test_epoch.py` 278 /
`test_wake_cli.py` 263 / `test_wake.py` 262 / `test_backend.py` 258 / `test_consumed.py` 245 /
`test_prune.py` 242 / `test_cli.py` 218 / `test_concurrency.py` 202 / `test_prune_cli.py` 178
\+ `conftest.py` 115（AstrBot/quart 的 stub，见下）—— **414 个用例**

**`conftest.py` 存在的理由**（这是本块唯一一个"为测试而造的东西"，值得单独说）：插件第一行就是
`from astrbot.api.star import Context, Star`，而跑测试的 Python 3.13 环境没装 AstrBot、**也不该装**
（装一个带 122 个 `.so` 的运行时进测试环境，会让"跑测试"从 15 秒变成一件需要慎重考虑的事）。
所以 conftest 造了 `astrbot` / `astrbot.api` / `astrbot.api.star` / `astrbot.api.util` 四个 stub 模块，
再加一个 quart 的 `jsonify` stub。**但不为测试把 `api.py` 改成返回裸 dict** —— 那等于测了一个不是插件的插件。

**三条贯穿全程的测试纪律**（这是本项目最有价值的工程产出，不是代码本身）：

1. **变异测试**：23 个 epoch 单测全过，不能说"测过了"。往 `epoch.py` 注入 6 个真实 bug
   （放行超前 epoch / 删倒退检查 / 删过期检查 / 删 shell_id 校验 / epoch 不递增 / 边界错一位），
   **6 个全部被抓住**（分别触发 3/2/2/1/7/1 个失败）。还原后复跑 23 passed。
   → **"测试通过"不等于"测试有牙"。** 同样对 `transfer.py` 的 6 个安全检查逐个拆过，
   对 `wake.py` 的 5 个（封包结构 / 广播计算 / 三路齐发 / 超时语义）逐个拆过，
   对 `prune` 的 3 个默认值与边界逐个拆过，对插件的 10 个（§13.11.4）逐个拆过，
   对 `migrate --control-url` 的 10 个（§13.11.5）逐个拆过。
2. **必须用真进程测并发**：`test_concurrency.py` 用 `subprocess` 起**两个真 Python 进程**，
   用屏障文件让它们同时冲，**连跑 5 轮每轮都恰好一个赢**（单轮通过可能是运气）。
   这条纪律直接抓出了 §13.5 那个"两个都拿到 epoch 1"的真 bug —— 多线程测不出来。
3. **断言安全属性，不断言错误消息**：`test_extract_rejects_dotdot_escape` 只断言"文件没有出现在目录之外"，
   不匹配我方中文措辞。理由：换了措辞测试就该失败，那是测试的错不是代码的错。
4. **判据是"有没有测试失败"，不是"失败够不够多"**：变异脚本第一版有个 `min_fail` 阈值，
   把 1 个失败报成 WEAK。**猜一个数字让脚本变绿，正是本项目反复踩过的坑**，已删掉。

**十五个只有换台机器/换条件才暴露的 bug**（都已修，都有测试固定）：

| # | bug | 现象 | 修法 |
|---|---|---|---|
| 1 | GBK 控制台 | 中文 Windows 上 `print("✓")` → `UnicodeEncodeError: 'gbk' codec can't encode '\u2713'` | 实测 `probe.encode(sys.stdout.encoding)` 的能力，不行就降级成 `[OK]`/`[X]`/`[!]` |
| 2 | 导入名当包名 | `PIL` / `Cryptodome` / `_distutils_hack` 被当成 pip 包名，装不上 | 改读 `*.dist-info/METADATA` 的 `Name:`；61 个"包" → 51 个真包 |
| 3 | sidecar 残留 | 热备副本旁边留下 `-shm`(32768B) 与空 `-wal`，送到新壳像"崩溃现场" | `_drop_empty_sidecars()`；**只对副本调用**（对活库删 sidecar 会直接搞坏它），`-wal` 非空则拒绝 |
| 4 | **裸 sqlite3 异常绕过 Fatal** | `hot_backup` 里 `DatabaseError: file is not a database` 逃出 `except Fatal` → **租约永远收不回来**，白占 90 秒 | `except sqlite3.DatabaseError` → `Fatal(...)`。规律：**凡是 sqlite3 可能抛的地方都要包** |
| 5 | **staging 落在 `/tmp`** | 远端模式快照放 `/tmp/molt-staging`，NAS 重启即消失 → 账本里留一条**指向空气的记录** | 改 `LOCALAPPDATA` / `XDG_DATA_HOME` / `~/.local/share` 下的 `molt/staging` |
| 6 | **`land` 共享时间戳目录** | 同一秒内第二次运行继承上一次的 `manifest.json` → "归档里没有 manifest"检查永远通过，旧文件被落进目标 data | 改 `tempfile.mkdtemp()`（内核保证唯一）；`--to` 指定时目录已存在就拒绝 |
| 7 | **WAL sidecar 被当快照内容** | 校验时只读连接造出 `-shm`/空 `-wal`，落进 gen_dir 后被复制到目标 | `_sidecar_noise()` 过滤；`restore_into` 结尾清理并复查非空 WAL |
| 8 | **下载中途断开把半截文件留在正式文件名上** | `part = dest` 的写法下，sha 不符时 `part.unlink()` 删的正好是 dest，**看起来一样干净**；只有"中途断开"能区分 | 先写 `<dest>.part`，验过 sha256 才 `os.replace(part, dest)`。**这个 bug 是变异测试发现的**：去掉 `.part` 后 19 个测试全过 |
| 9 | **承诺的超时不是真的超时** | `wait_online(timeout=0.6)` 实测跑了 **2.006 秒**——单次探测固定 2 秒，完全不看剩余预算 | 每次探测算 `budget = max(0.05, min(probe_timeout, deadline - now))`。真机上 180 秒变 182 秒没人注意，但探测间隔会完全失真 |
| 10 | **`target_shell` 装成了 `pushed_by`** | 单机测试里两者恰好相同，所以全过；多机下 `molt status` 会说"灵魂在 A 上"而 A 马上要关机 → **直接误导人做关机决定** | 字段拆成 `pushed_by`（必填）+ `target_shell`（默认 `""`），`from_dict` 向后兼容 |
| 11 | **覆写表漏了 `platform[].enable`** | 三次 Windows 启动都真的发起了 QQ websocket（`[QQOfficial] Websocket session starting.`），只因进程数秒内被杀才没变成双活 | `_disable_platform_adapters()` 作为覆写表**第 0 条**；两条预设都不给关闭它的便利（§6.6.7） |
| 12 | **`drain()` 靠配置 id 找适配器** | `WebChatAdapter` 由 `manager.py:116` 直接 append，**从不进 `_inst_map`**，`config.platform` 里也没有它 → `/drain` 什么都没关，返 409 | `_classified()` 改用**实例身份**（`id(ent["inst"])`）作主判据；webchat 归 `local_only`（§6.2.5–6.2.7） |
| 13 | **`utf-8-sig` 写回时给没有 BOM 的文件加了 BOM** | `platform-switch.py` 第一版无条件用 `encoding="utf-8-sig"` 写 → 原本无 BOM 的 `cmd_config.json` 被加了 `ef bb bf` | `had_bom = path.read_bytes()[:3] == b"\xef\xbb\xbf"`，读的时候才是"吃掉 BOM"，**写的时候它是在"加 BOM"，方向相反** |
| 14 | **容器里没有 `ping`，却报「叫不醒」** | 真机 `/molt confirm` 后子进程白等 **180 秒**，然后要报「180 秒内没能连上 …（判据：icmp echo）」——**而 Windows 开着、人正坐在前面**。`OSError: No such file` 被 `except (OSError, SubprocessError): return False` 吞成"不通" | `can_probe_icmp()`（`shutil.which("ping")`）+ `wait_online` 的硬守卫：没 ping 又没端口 → **0.00 秒**说清"是我探测不了，不是它没醒"，并给两条出路。migrate 侧改成**不阻塞**（不让等待挡住交接） |
| 15 | **agent 落位后被自己的一行日志打死** | `self.log(f"  ✓ 落位 gen-{n}")` → `UnicodeEncodeError: 'gbk' codec can't encode character '\u2713'`。它当时正处在「**已经落位、还没启动**」的中间态 —— NAS 上哑了、Windows 上没起来，**鱼两边都不在** | 启动时 `_setup_console()` 把 stdout/stderr 重配成 UTF-8 + `errors="replace"`（同 bug #1）；`_log_line()` 再兜一层，**日志打不出来绝不杀死进程**。救回来的是顺序：`needs_start = True` **先落盘再打日志** |

**第 15 条最值得记**：它同时验证了两件事 —— 一个 bug 会在同一个项目里以同一种形态复发（bug #1 的 GBK 又来了，只是换了入口），以及**「先记账再冒险」的欠账机制真的救了一次**。不是设计得好，是它刚好在被测的那一刻派上了用场。

**第 6 条最值得记**：它不会报错，它只是**默默地用了错的东西**。这类 bug 只能在"同一条命令连跑两次"时暴露。

**第 8 条是"测试通过"最阴的一种形态**：19 个用例覆盖了 sha 不符、缺文件、HTTP 错误，全部通过；但只要老实写 `part = dest`，它们在语义上全都还能过——因为它们断言的是"失败后 dest 不存在"，而直接写 dest 的实现失败时也会删 dest。**唯一能区分两者的是"写到一半连接断了"**。所以这条测试必须自己造一个只吐一半字节的假响应（`_HalfReader` + `http.client.IncompleteRead`）。

**第 11 和第 12 是同一种病的两种形态**：都是"检查范围"错了，不是逻辑写错。
第 11 条查的是"我该改哪些配置项"，漏了一项；第 12 条查的是"哪些实例算对外连接"，判据选错了维度。
两者的共同点是：**实现本身完全正确，它只是回答了一个比你以为的更窄的问题。**

---

### 13.7 一条命令替代手工四步 —— ✅ 已通过【实测 2026-11-14 13:50】

**为什么要做**：手工四步（`checkout` → 打包 → 传输 → `push`）不只是麻烦，它和一个**硬约束**冲突——租约 TTL 只有 **90 秒**。人在这中间泡杯茶，租约就过期了，`push` 会以 `LeaseExpired` 失败。用户看到的现象是"我明明备份成功，却推不上去"。

**实测判决书**（真机 NAS，`python3 -m molt.cli migrate`）：

```
python3 -m molt.cli migrate \
    --data /mnt/dm-0/astrbot/data \
    --shell nas-shell --target win-shell \
    --warden-url http://127.0.0.1:6186 --warden-token $(cat warden.token)

拿到租约 epoch=5
  · 热备 data_v4.db …                ✓ 10.4 MB  0.028s  21 张表  quick_check=ok
  · 热备 .../memory_companion.db …   ✓ 16.9 MB  0.044s  41 张表  quick_check=ok
  · 复制 cmd_config.json …           ✓ 10109 B
  · 打包 …                           ✓ 4.4 MB  4 个文件
generation 5 / 27.3 MB
  归档 G:\...\gen-000005.tar.gz  sha256 0da947d0…
✓ 已登记
✓ 已交还租约
real 1.43s
```

**归档内容只有 4 个文件**：`cmd_config.json` / `data_v4.db` / `manifest.json` / `plugin_data/.../memory_companion.db`。**没有任何 sidecar**。

**租约管理的铁律（写在 `cmd_migrate` 里）**：从 `acquire` 成功那一刻起，**任何失败路径都必须 `release`**。Fatal → 4、MoltError → 7，都在 finally 语义里先还租约再返回。若 release 本身失败，只 warn "90 秒后会自动过期"——**不能让"还租约失败"盖住原始错误**。

**默认自动 release**（`--keep-lease` 可保留）。理由：`migrate` 结束后源壳已经不需要那个 epoch 了，早点释放让别人（包括它自己重跑）能立刻接管。

**这一节留下的工程结论**：把不可中断的动作拆成多段人肉步骤时，**每多一段就多一个"中间态被人打断"的窗口**。租约 TTL 只是把这个问题变成可见的失败，真正的问题是流程本身不该假设人能按时完成机械步骤。

> **批注（v0.5）**：本节写成时 `molt migrate` 仍明确"不做字节传输"——跨机时源端产出归档、人传、目标端 `molt land`。那个边界在 **2026-11-14 14:16 被推翻**了，见 §13.8。理由恰恰就是本节最后那句话：**传输也是人能被打断的一步**，所以它不该由人做。

---

### 13.8 跨机字节传输也自动化了 —— ✅ 已通过【实测 2026-11-14 14:16】

**为什么最终还是做了**：§13.7 里那个"不做字节传输"的边界，在真机上跑过两次之后被证明是错的设计。理由不是"能做得更好"，而是——

> 一次不可中断的动作（拿租约 → 备份 → 传输 → 登记）中间卡了一个人，而租约 TTL 只有 90 秒。人手工 scp 一趟泡杯茶就过期了。人看到的现象是"我明明备份成功了，却推不上去"。

租约铁律说的是"任何失败路径都必须还租约"，但把人塞进 TTL 窗口里，本身就是**在制造失败路径**。所以传输必须自己完成。

#### 13.8.1 设计：为什么走 Warden，而不是 SMB / SCP / SSH

三个候选，只有一个是干净的：

| 方案 | 为什么不用 |
|---|---|
| SMB 共享盘 | 正是 ARCH §3 判定"锁与 mmap 不可靠"的那个东西。**用它搬数据库文件，等于在一条已被否决的路上加一个拐弯** |
| SCP / SSH | 需要第三套凭证（SSH key 或密码），需要 Windows 上装 sshd（当前没有），而且和已认证的 Warden 通道是两套信任模型 |
| **Warden HTTP 中转区** | **复用已有通道**：Warden 已在 NAS 上跑、已有 token 认证、已有协议版本协商。零新增端口、零新增凭证 |

**关键设计决定**：

1. **两个新端点**：`PUT /archive/<gen>`（上传）与 `GET /archive/<gen>`（下载）。**不塞进 JSON**——30 MB 的 tar 走 base64 变 40 MB，且要在内存里同时存在两份解码前后的字节。
2. **中转区在 Warden 侧**（`<warden_root>/archives/`），**不在壳侧**。让 NAS 站着当中转站是唯一不需要第三套凭证的做法。
3. **放在 `archives/` 而不是 `staging/`**：staging 里是"正在生成、可以随时清理"的快照目录；archives 里是"已经打包好、目标壳可能还没来取"的东西。混在一起会让人分不清"这个目录还有没有人要"。
4. **上限 `MAX_ARCHIVE_BYTES = 640 MiB`**：设上限不是省空间，是**别让一个写错的循环把 NAS 的 7.3T 填满**（磁盘满会让 AstrBot 也挂掉）。而且**超限时用 413 直接拒绝且不读 body**——读进来再判断等于先让人把 640 MB 传完再说"不行"。
5. **三步顺序不能变**（上传侧）：算哈希 → 比哈希 → `os.replace` 原子落位。**先落位后校验意味着一个坏归档已经在正确的文件名上**，任何并发读者都会把它当成好的。
6. **临时文件必须与归档区同一个文件系统**（同目录 `.part`）：跨文件系统的 rename 会被退化成复制+删除，那就没有原子性了。
7. **下载侧先写 `<dest>.part`，验过哈希才 `os.replace`**。理由同上，方向相反：**不在校验之前让别人看见这个文件**。
8. **上传插在 `push` 之前**：先让字节到位，再让账本承认这件事。反过来的话，一旦上传失败，账本里就有了一条"登记了但取不到字节"的记录——那正是最难处理的中间态。

#### 13.8.2 命令的两种形状

```bash
# 源端（NAS）：默认远端后端下 transport 自动选 warden
molt migrate --data /mnt/dm-0/astrbot/data --shell nas-shell \
             --target win-shell \
             --warden-url http://127.0.0.1:6186 --warden-token $(cat warden.token)

# 目标端（Windows）：不给 --archive，给 --generation，它自己去取
molt land --generation 5 --data G:\molt-p0\land-final\data \
          --warden-url http://192.168.1.10:6186 --warden-token <token> \
          --shell win-shell --yes
```

`--transport` 的默认值**按后端类型自动选**（远端 → `warden`，本地 → `local`）。理由：真机上没人会记得加那个参数，而"忘了加"的后果是归档留在本机、目标端什么也取不到——**一个静默的半成品**。

`land` 必须且只能给 `--archive` 或 `--generation` 之一。都不给或都给都返回 2。

#### 13.8.3 实测判决书

源端（NAS，`python3 -m molt.cli migrate`，**real 1.61s**）：

```
  裁决者                    远端 http://127.0.0.1:6186
  传输                     warden
· 拿到租约 epoch=7
· 热备 data_v4.db …          ✓ 10.4 MB  0.125s  21 张表  quick_check=ok
· 热备 .../memory_companion.db … ✓ 16.9 MB  0.194s  41 张表  quick_check=ok
· 复制 cmd_config.json …     ✓ 10109 B
· 打包 …                     ✓ 4.4 MB  4 个文件
· 已上传到中转区：gen-000005 4.4 MB
  generation             5
  归档 sha256            3e2dfdbf686c5bf41866c95f59e47bfe29f7e1b00b8ad7355fbc41f9f1797aae
  中转区                    已上传，目标壳可直接取
✓ 已登记。快照现在归新壳管了。
✓ 已交还租约——目标壳现在可以接管了
```

中转区实际落盘：`/mnt/dm-0/astrbot/_molt/warden-root/archives/gen-000005.tar.gz` **4,629,891 B**。

目标端（Windows，`molt land --generation 5`）：

```
从 Warden 中转区取归档
✓ 已取到 4.4 MB sha256 3e2dfdbf686c5bf4…
✓ 解包完成：4 个文件 / 27.3 MB
✓ 快照完整性校验通过
落位完成：cmd_config.json / data_v4.db / plugin_data/.../memory_companion.db
机器覆写层（Windows）：6 处全部生效
! 还需要重装 51 个运行时包（manifest 里有清单）
```

**落位后目标 data 目录精确为 3 个文件，无任何 sidecar**：

| 文件 | 字节 |
|---|---|
| `cmd_config.json` | 9,859 |
| `data_v4.db` | 10,887,168 |
| `plugin_data/astrbot_plugin_memory_companion/memory_companion.db` | 17,727,488 |

**数据核对**（Windows 侧直连 SQLite）：

```
data_v4.db              journal=wal quick_check=ok tables=21
memory_companion.db     journal=wal quick_check=ok tables=41
conversations      : 2
platform_message_history: 139
personas           : 1
memories           : 71
knowledge_nodes    : 160
knowledge_edges    : 257
dashboard.host     : 127.0.0.1
dashboard.port     : 6199
provider_sources   : ['deepseek']
provider 数量      : 4
```

**AstrBot 用这份全自动搬过来的数据启动**：

```
[AstrBot v4.29.4]
[Loaded 1 personas.]
[Starting WebUI at http://127.0.0.1:6199]     ← 覆写生效，不是 0.0.0.0:6185
[AstrBot started.]
库指纹启动前后一字未变：data_v4.db E87FDEF5002CBE5C / memory_companion.db 1CE3D62B82D8672A
```

唯一剩下的报错仍是 `ModuleNotFoundError: No module named 'edge_tts'` / `'whisper'`——**这正是 manifest 里 `runtime_pip`（51 个包）存在的原因**，不是本次引入的问题。

#### 13.8.4 测试：20 个新用例 + 变异验证

新增 `molt/tests/test_archive_transfer.py`（20 个）与 `molt/tests/test_transport_e2e.py`（11 个），全量 **291 passed**。

**变异测试抓到的那个漏洞（值得单独记）**：把下载侧的 `part = dest.with_name(dest.name + ".part")` 改成 `part = dest`（直接写正式文件名），**19 个测试全部通过**。原因是 sha 不符时 `part.unlink()` 删的正好也是 dest，看起来一样干净。

真正的差别只在**中途断开**这一种情况：`http.client.IncompleteRead` 不在重试的 `except` 列表里，会一路逃出去。这时 `.part` 写法留下的是 `x.tar.gz.part`（名字本身就在说"这是残废的"），而直接写 dest 的写法会把半截 tar 留在 `x.tar.gz` 上——**一个看起来完全正常的归档名，下一次 `land` 会毫不犹豫地把它解包**。

补的测试 `test_download_interrupted_leaves_no_file_at_destination` 用一个只吐一半字节的假响应固定住这条，现在那个变异会被抓住。

**这正是本项目反复强调的那类失败：它不会报错，它只是留下了错的东西。**

#### 13.8.5 还没做的

- ~~**归档保留策略**~~ → 已做，见 §13.9。
- **断点续传**：现在失败就重传整份。4.4 MB 无所谓，30 MB 以上值得做。

---

### 13.9 唤醒与清理：把它们从脚本提成产品 —— ✅ 已通过【实测 2026-11-14 14:32】

#### 13.9.1 为什么唤醒必须进产品

在这之前，"把机器叫醒"是一个 `tools/wol-test.py`，只在 WOL 那一关的验证期用过一次
（§7.4）。真正开始迁徙时它暴露了两个结构性问题：

**第一个：迁徙的第一步不是备份，而是"目标机得是开着的"。**
在此之前，`molt migrate` 假设目标壳已经在线，于是整条链子的第一环是人手动发一个
魔术封包。**一个需要人手发封包的"全自动迁徙"，只是在报告里看起来是自动的。**

**第二个：顺序错了会让租约白白浪费。**
租约 TTL 只有 90 秒，而真机从 S5 到网络恢复是 23.1 秒（§7.4.2），到 AstrBot 能干活
还要更久。如果先 `acquire` 再叫醒，那 90 秒里有 23 秒以上在等开机，热备+打包的余量
就被吃掉了；更糟的是**叫醒失败时租约已经发出去了**，白占 90 秒，期间谁都不能开始。

所以现在的顺序是 **先唤醒 → 再 acquire → 再备份**，并且写在测试里固定住
（`test_migrate_wakes_before_acquiring_lease` 断言调用顺序恰为
`["wake", "wait", "acquire"]`）。叫不醒时 `migrate` 直接 `return 5` 退出，
**账本里不会留下一条指向一台没开机的机器的记录**。

#### 13.9.2 `molt/wake.py` 的两条实测教训

**教训一：单播发不出去。** 机器关机后 IP 上没有主机应答 ARP，邻居表项过期，
往 `192.168.1.20:9` 发单播内核直接 `Network is unreachable` / `No route to host`
——**报文没出过网卡**。所以必须三路齐发：

```python
targets = [host, directed_broadcast(host, prefix_len), "255.255.255.255"]
#           单播（机器在线时最快）  定向广播（交换机泛洪，关机时的主力）  兜底
```

这条在 ARCH §7.3 已经写过（那是 WOL 验证时的发现），本节的补充是：
**它现在是代码而不是知识。** 一个 `molt wake --mac ...` 就把它带上了任何机器。

**教训二：判断"能不能被叫醒"只认一个证据。**
`powercfg /devicequery wake_armed` 之外的一切都是猜——
`PnPCapabilities` 的位、`Get-NetAdapterPowerManagement` 的返回值、驱动高级属性，
真机上都出现过"看起来是关的、实际能用"（§7.4.3 记录过 `PnPCapabilities=8` 而 WOL
完全正常）。所以 `wake.py` **只管发**，判断交给 `shell.py` 的
`probe_capabilities()`，而它只读 `check-wol.ps1 -Json` 的输出。

#### 13.9.3 在 wake.py 抓到的真 bug：承诺的超时不是真的超时

第一版 `wait_online()` 里，单次探测固定 `timeout=2.0`，**完全不看剩余预算**。
实测 `timeout=0.6` 的函数跑了 **2.006 秒**。

对一个承诺"最多等 N 秒"的函数，这是错的。而且它的错法很隐蔽：真机上 180 秒变
182 秒没人会注意，但探测间隔会完全失真——`timeout=0.6 interval=0.1` 本该探测 6 次，
实际只探测 1 次。

修法是给每次探测算预算：`budget = max(0.05, min(probe_timeout, deadline - now))`。
并配两条测试固定住：一条量墙钟（`timeout=0.6` 必须 < 1.6 秒跑完），
一条打桩 `_tcp_alive` 直接观察**实际传进去的超时值**。

> 写进纪律清单。这是本项目第 **九** 个"换机器/换参数才暴露"的 bug。

#### 13.9.4 清理：这个系统里唯一会真删文件的地方

`archives/` 在中转区里只增不减（§13.8.5 记过这一条）。加清理时定了四条规则，
按保守程度排序：

| # | 规则 | 为什么 |
|---|---|---|
| 1 | **账本里记着的一律不删** | 那是"已经交接出去、目标壳可能还没来取"的东西。删了它，账本里就留下一条取不到字节的记录 |
| 2 | 文件名不是 `gen-NNNNNN.tar.gz` 的一律不删 | 人手工放进 `archives/` 的东西不归清理逻辑管 |
| 3 | 其余按 `keep_recent` 保留最新几份 | 缓冲"刚上传但 push 还没成功"的那次 |
| 4 | **默认 `dry_run=True`** | 一个会删文件的函数，默认参数下不能动手 |

规则 1 的依据是 `accept_archive` 的**先落位再登记**顺序（§13.8.1）：
文件在盘上但账本里没有，只可能是"上传完但 push 失败"（那次迁徙废了）或
"来自更早的已清理账本"，两种都该回收。

#### 13.9.5 实测判决书

**NAS 真机，中转区有 `gen-000005`（账本里记着）**：

```
$ molt prune --warden-url http://127.0.0.1:6186 --warden-token <token>
中转区
  位置          /mnt/dm-0/astrbot/_molt/warden-root/archives
  保留最近       2 份
✓ 没有可清理的归档 —— 每一份都在账本里，或者都是别人放的
  保留 1 份：
    gen-000005.tar.gz  账本里记着这一代，目标壳可能还没取走
  想真的删：加上 --yes
```

**再放两个账本不认的 + 一个外人的文件**，跑真删：

```
$ molt prune ... --keep-recent 0 --yes
✓ 已删除 2 份，腾出 11.7 KB
  - gen-000098.tar.gz  6.8 KB  账本里没有
  - gen-000099.tar.gz  4.9 KB  账本里没有
  保留 1 份：
    gen-000005.tar.gz  账本里记着这一代，目标壳可能还没取走
```

**盘上结果**（三件事同时成立）：

```
gen-000005.tar.gz      4629891 B   ← 账本里记着，留下
somebody-else.tar.gz        21 B   ← 不是 molt 放的，留下
（gen-000098 / gen-000099 已消失）
```

**Warden 日志**：

```
2026-11-14 14:32:49  [INFO ] 清理归档 gen-000098.tar.gz（7000 字节，账本里没有这一代）
2026-11-14 14:32:49  [INFO ] 清理归档 gen-000099.tar.gz（5000 字节，账本里没有这一代）
2026-11-14 14:15:57  [INFO ] 收到归档 gen-000005 4629891 字节 sha256=3e2dfdbf686c5bf4…
```

清理动作留痕，且 `molt status` 复查后账本完好（`epoch 7` / `generations 5`，零拒绝）。

#### 13.9.6 测试：33 个新用例 + 三个变异全部被抓

`test_wake.py`（13 个）覆盖封包字节逐位校验、`directed_broadcast` 八组参数、
`wait_online` 超时抛 `NeedsHuman` 而不是返回 `False`、以及那条超时不越界的量墙钟测试。
`test_wake_cli.py`（12 个）固定调用顺序与失败即停。
`test_prune.py`（22 个）+ `test_prune_cli.py`（11 个）覆盖四条规则与两个默认值。

**变异验证**（清理是唯一会删用户数据的路径，逐条拆）：

| 变异 | 结果 |
|---|---|
| CLI 里 `dry_run=not args.yes` → `dry_run=False` | **CAUGHT**（1 failed） |
| 端点默认 `body.get("dry_run", True)` → `False` | **CAUGHT**（1 failed） |
| `list_archives` 的 `referenced` 恒为 `False` | **CAUGHT**（2 failed） |
| `magic_packet` 只重复 15 次 | CAUGHT（3 failed） |
| `directed_broadcast` 不取反 | CAUGHT（7 failed） |
| `wait_online` 超时返回而非抛异常 | CAUGHT（4 failed） |

还原后全量 **368 passed in 31.8s**。

#### 13.9.7 现在 `molt` 有 12 个命令

```
shell  status  checkout  verify  push  release
migrate  land  wake  prune  doctor  serve
```

`wake` 和 `prune` 是这条链子首尾两端：**一个负责让它来，一个负责打扫它来过的痕迹。**

---

### 13.10 Windows 上的优雅停机 —— ✅ 已通过【实测 2026-11-14 14:44】

这是 §13 验证清单第 1b 条关掉的最后一块。结论先说：**Windows 有等价的优雅停机信号，但它不是 `os.kill`。**

#### 13.10.1 三个候选，两个是假的

Linux 侧已经实测出结论（§6.4）：`SIGTERM` 直接死，`SIGINT` 走优雅路径。Windows 没有 `SIGTERM`，于是我在真机上把三种发法都试了一遍：

| 方式 | 实际发生的事 | 退出码 | 有 `Shutting down` |
|---|---|---|---|
| `os.kill(pid, CTRL_BREAK_EVENT)`（子进程带 `CREATE_NEW_PROCESS_GROUP`） | Python 把 `CTRL_BREAK` 映射成 `SIGBREAK` 的**默认处理器**，进程被直接杀掉 | `3221225786`（= `0xC000013A` = `STATUS_CONTROL_C_EXIT`） | **否** |
| `os.kill(pid, CTRL_C_EVENT)`（同上） | **Windows 根本不跨进程组递送 `CTRL_C_EVENT`**，60 秒内毫无反应，最后被强杀 | 无（超时） | **否** |
| **`SetConsoleCtrlHandler(None, True)` → `FreeConsole()` → `AttachConsole(pid)` → `GenerateConsoleCtrlEvent(CTRL_C_EVENT, 0)`** | 走 `KeyboardInterrupt` → `asyncio.run` 取消主任务 → `InitialLoader.start()` 的 `except asyncio.CancelledError` → `core_lifecycle.stop()` | **0** | **是** |

`os.kill` 那条路的退出码值得单独看一眼：`0xC000013A` 是"进程因 Ctrl+C 终止"的 Windows 状态码——看起来像"Ctrl+C 生效了"，其实是**默认处理器直接结束进程**，AstrBot 的 `stop()` 一行都没跑。这个假阳性足够骗人，如果只看退出码就会得出相反结论。

#### 13.10.2 为什么 attach 那一版才是真的

`CTRL_C_EVENT` 的语义是"送到某个**控制台**"，不是"送到某个进程"。Windows 不允许一个进程往**别的**控制台发 Ctrl+C，只允许往**自己当前 attach 的那个**控制台发。所以要复现"用户在 AstrBot 窗口里按 Ctrl+C"，必须：

```python
k32.SetConsoleCtrlHandler(None, True)   # 先摘掉自己的处理器，否则这个 Ctrl+C 会同时打到我自己
k32.FreeConsole()                       # 离开自己的控制台
k32.AttachConsole(child_pid)            # 挂到子进程的控制台上（pid 有效即可，不需要是组长）
k32.GenerateConsoleCtrlEvent(0, 0)      # (CTRL_C_EVENT, 整个 attached 控制台)
k32.FreeConsole(); k32.AttachConsole(0xFFFFFFFF)   # 挂回父进程的控制台
k32.SetConsoleCtrlHandler(None, False)  # 恢复
```

前提是子进程**有自己真正的控制台**——`CREATE_NEW_CONSOLE`，不是 `CREATE_NEW_PROCESS_GROUP`。

实测输出：
```
attach 到 pid 18132 的控制台：free=True attach=True（err=0）
GenerateConsoleCtrlEvent(CTRL_C) = True（err=0）

退出码  0
有 Shutting down True
有 Terminating   True

日志里的停机相关行：
  [14:43:50.383] [Core] [INFO] [core.initial_loader:56]: 🌈 Shutting down AstrBot...
  [14:43:50.383] [Core] [INFO] [star.star_manager:1934]: Terminating plugin astrbot ...
  [14:43:50.383] [Core] [INFO] [star.star_manager:1934]: Terminating plugin builtin_commands ...
  [14:43:50.386] [Core] [INFO] [knowledge_base.kb_db_sqlite:173]: 知识库数据库已关闭: .../knowledge_base/kb.db
```

#### 13.10.3 checkpoint 的硬证据：kb.db 从 45 KB 变 94 KB

只看 `Shutting down` 字样不够——那可能只是打了行日志。真正要证明的是 **SQLite 真的被 checkpoint 了**。

`data_v4.db` 在这个场景里不配合：AstrBot 启动后并不写它（P0 那次实测过，启动前后 sha256 一字未变），所以它的 `-wal` 一直是 0，拿它做对照等于什么都没证。

真正会写的是**知识库**。测试前故意造一个有未合并事务的状态：

```
kb.db        45,056 B
kb.db-wal    98,912 B        ← 98 KB 已提交但还压在 WAL 里
kb.db-shm    32,768 B
```

跑完 `console` 那一发之后再查：

```
kb.db        94,208 B        ← 45,056 + 98,912 的数据真的并进主库了
kb.db-wal    (不存在)         ← checkpoint 后被删除
kb.db-shm    (不存在)         ← 同上
```

`45,056 + 98,912 = 143,968`，而主库变成 94,208——不是简单相加（页面复用、freelist），但**量级和方向都对**：数据确实从 WAL 移进了主库，且 sidecar 被正常清除。这只有在 `core_lifecycle.stop()` 跑到 `kb_manager.terminate()` 那一步才会发生。

#### 13.10.4 这条结论怎么用（以及它不改变什么）

**它不改变 v0.1 的交接协议。** Windows 侧热备走 `file:...?mode=ro` + `sqlite3.Connection.backup()`，**本来就不需要停 AstrBot**（§13.1 已验，0.042 秒）。优雅停机在 Windows 上的用途是另一件事：

> 当这只鱼**决定要关机休眠**时，让它把该写的写完、该 checkpoint 的 checkpoint 掉，而不是被 `Stop-Process` 硬杀。
> 后者留下的 WAL 在我们下次 `checkout` 时会被 `_sidecar_noise()` 拦下来（`Fatal: 有 N 字节未合并的事务`）——那是对的，但那种"关不掉"比"关得干净"糟糕。

所以 `shell.py` 的 `Capabilities` 里，Windows 壳现在可以诚实地宣称自己支持优雅停机，实现方式记在 `tools/win-graceful-shutdown-test.py`。**v0.1 不实现自动关机**（那是 v0.3 的 `molt sleep`），但这一块不再是未知数了。

#### 13.10.5 这一关抓到的两个"测试自身"的错

1. **`ASTRBOT_ROOT` 设错层，测的其实是另一个目录。** 第一版把 `ASTRBOT_ROOT` 设成源码目录（`G:\molt-p0\astrbot-src`），于是 AstrBot 在那底下自建了一套全新的空库，而我一直盯着的 `G:\molt-p0\shutdown-test\data` 根本没被用到——`sha256` 前后一致是因为**它压根没被碰过**。正确关系是 `ASTRBOT_ROOT = <data 的父目录>`、`cwd = 源码目录`（`boot-landed.ps1` 第 40 行就是这么写的）。
   踩这个坑的代价是：源码目录被写进去一套 6 MB 的垃圾（`data_v4.db` 237,568 B + `dashboard.zip` 5,377,556 B），已按 `CreationTime` 确认是那次测试新建的并删除。
2. **GBK 控制台又一次崩了。** 脚本 print AstrBot 的日志尾行时撞上 `✨` → `UnicodeEncodeError: 'gbk' codec can't encode character '\u2728'`。这是本项目第 1 号 bug 的第二次出现（`cli.py` 里早就修过）。修法同前：`stream.reconfigure(encoding="utf-8", errors="replace")`，**在 `main()` 第一行做**。

两个错都指向同一条老教训：**测试脚本自己也是被测系统的一部分，"它跑通了"不等于"它测的是你以为的那件事"。**

---

### 13.11 AstrBot 侧控制面 —— ✅ 已通过【实测 2026-11-14 15:23 / 22:43】

#### 13.11.1 它与 §13.10 的区别

§13.10 解决的是"怎么让 AstrBot **结束**"。这一节解决的是"怎么让它**闭嘴但别结束**"。

两件事的差别在代价上：SIGINT 会丢掉调度器、定时任务、正在跑的一轮对话，恢复要重启（重拉几千个
LLM 元数据）。而一次迁徙真正需要的只有一件事：**它别同时对同一个人说话**。

#### 13.11.2 四端点的真机判决

| 端点 | 结果 | 证据 |
|---|---|---|
| `GET /status` | ✅ | `connected: 0`、`connected_ids: []`、`local_only: ["webchat"]`、`speaking: false` |
| `POST /checkpoint` | ✅ | **`kb.db` 的 98,912 B WAL → 0**，另两个库本来就是 0 |
| `POST /drain` | ✅ | `drained: ["molt-loopback-1"]`、`still_connected: 0`、`verified_silent: true` |
| `POST /resume` | ✅ | `resumed: ["molt-loopback-1"]`、`speaking` 回到 `true` |

**`/checkpoint` 这条为什么算判决性证据**：`data_v4.db` 与 `memory_companion.db` 启动后根本不写库，
它们的 `-wal` 恒为 0。**拿一个本来就是 0 的数字做对照，等于什么都没证**。唯一有未合并事务的是
`knowledge_base/kb.db`（98,912 B），`TRUNCATE` 之后归零、sidecar 消失 —— 这才是"它真的写盘了"。

#### 13.11.3 环回适配器：怎么在没有对外平台的情况下验往返

见 §6.2.10。一句话：**自造一个不碰网络的 `Platform` 子类，让它走与 QQ 完全相同的
`load_platform` / `terminate_platform` / `_inst_map` 路径**，这样测的是同一条路，不是同一段代码的仿制品。

**有一条判据我一开始写错了**：第一版 `_verify_control_api.py` 断言"resume 之后 `speaking` 必须为
True"，在真机上它报 FAIL —— 但那不是 bug，是**这个场景下的正确行为**（配置里没有任何 `enable=true`
的对外平台）。强行让它通过，唯一的办法是让代码"偷偷打开平台"，**那正是公理 0 最怕的事**。
脚本已改为按 `enabled_in_config` 是否为空分流，空时输出 `[PARTIAL]`。

> 测试通过不了的时候，先问一句"是不是我的期望本身在教唆代码做错事"。

#### 13.11.4 插件的变异测试：10 个，第一轮只抓到 8 个

`tools/_mutate_plugin.py`。第一轮两个 MISSED 都是真漏洞：

1. `speaking` 算进 `local_only` —— `api.py` 完全没有测试覆盖
2. `_platform_configs()` 只认 `enable=true` —— **这正是真机踩过的那个 bug，而测试没抓到**

第 2 条尤其值得记：**它已经在真机上出过一次事了，我的测试还是没覆盖到它**。
补上 4 条测试（`test_platform_configs_includes_disabled_entries` /
`test_drain_works_when_platforms_were_disabled_by_guardrail` /
`test_status_speaking_reflects_only_outward_connections` /
`test_status_speaking_false_when_nothing_connected`）之后 10/10 CAUGHT。

**判据也改了**：删掉 `min_fail` 阈值，只看"有没有测试失败"。第一版把 1 个失败报成 WEAK，
而"猜一个数字让脚本变绿"正是本项目反复踩过的坑。

#### 13.11.5 接进 `migrate` 之后的变异测试：10 个，抓到第三轮才满

`tools/_mutate_migrate_drain.py`。三轮才 10/10，每一轮 MISSED 都不是脚本错误而是**测试太松**：

| 轮次 | 结果 | 漏掉的那个 | 为什么漏 |
|---|---|---|---|
| 1 | 6/8 | `没给控制地址时不出声` | 断言 `"--control-url" in out` 被下面那行 `dim("    --control-url …")` 满足了 —— **警告删了照样绿** |
| 1 | | `drain 挪到热备之后` | 我把"挪"写成了"删几行输出"，**那根本不是挪** |
| 2 | 9/10 | `drain 之前就把 drained 置真` | **等价变异** —— 失败路径 `return 8` 在 `drained` 被读之前就返回了 |
| 3 | **10/10** | — | 换成真正有意义的 `没按过也告诉用户它哑了`（`if drained:` → `if True:`），并补对应测试 |

两条要记住的：**第一，断言要能被变异打穿**（`in out` 这种到处都成立的写法等于没断言）；
**第二，等价变异不是漏检，是变异本身没意义** —— 该换一个，不是该加测试。

最终 10 个：快照先拍再闭嘴 / 声称 drain 但没调 / 热备失败不 resume / Fatal 不 resume /
MoltError 不 resume / drain 失败也继续 / 不给地址不出声 / 缺凭据时猜一个 / resume 失败假装成功 /
没按过也说哑了。

#### 13.11.6 `migrate --control-url` 的真机端到端【实测 22:43】

跑在装了插件的测试壳上（`G:\molt-p0\ctl-test\`，QQ 那条 `enable=False`），**不碰 NAS 生产容器**：

```
· 拿到租约 epoch=1
· 按住源壳 AstrBot 控制面 http://127.0.0.1:6199/api/plug/astrbot_plugin_molt …
  已关闭的平台                 molt-loopback-1
  仍在对外说话                 否（已复核）
· 热备 data_v4.db …      ✓ 10.4 MB  0.040s  21 张表  quick_check=ok
· 热备 memory_companion.db …  ✓ 16.9 MB  0.051s  41 张表  quick_check=ok
· 打包 …                 ✓ 4.4 MB  4 个文件
  源壳现在是哑的（这是我们按的，不是它坏了）。
✓ 已交还租约——目标壳现在可以接管了
```

退出码 0。迁徙后复查：`speaking = False`、`connected_ids = []`、`local_only = ['webchat']`、
`has_drained = True`；账本是 `current_epoch = 1` / `lease = None` /
`generation 1: pushed_by=win-loopback target=nas-fish epoch=1 bytes=28624617`；
AstrBot 日志多出 `22:43:07.697 Loading IM platform adapter molt_loopback(molt-loopback-1)` 与
`22:43:07.922 Attempting to terminate platform adapter molt-loopback-1`。

**三条独立证据同时成立**（少一条都不算通过）：
① 源壳真的闭嘴了（`speaking` 从 True 变 False，且是**复核过**的，不是"我以为"）
② 快照是闭嘴之后拍的（热备 `0.040s`，在 drain 之后）
③ 租约交还了（`lease = None`），所以目标壳现在可以合法接管

---

### 13.12 真实 QQ 收发 —— ✅ 已通过【实测 2026-11-14 22:53】

这是整个项目**最后一个产品级未知数**，也是唯一一个无法靠单机测试回答的问题：
前面所有关卡都在证明"数据搬得过去""进程起得来""租约守得住"，
但没有一条能回答「**换了个身体之后，它还认识那些来敲门的消息吗**」。

#### 13.12.1 窗口是怎么开的

这一步**必须**让生产容器先停下来。不是谨慎，是公理 0：
生产容器在 NAS 上一直连着 QQ，如果 Windows 壳也打开 `platform[].enable`，
就是两条鱼同时在线——正是 14:41 那次差几秒的事故想避免的情形。

窗口的顺序（**先停后起，先死后生**）：

1. NAS 上挂一根保险丝 `tools/window-guard.py`（10 分钟），见 13.12.4
2. `docker kill -s INT astrbot` → 优雅停机（**不是** `docker stop`，见 §13.10）
3. Windows 壳从 gen-6 的快照落位，把 `platform[].enable` 手动打开
4. `boot-keepalive.py G:\molt-p0\qq-test\data 6199 <log>` 起壳
5. 人在 QQ 里发一句话
6. 停 Windows 壳（`AttachConsole` + `GenerateConsoleCtrlEvent`，见 §13.10）
7. `docker start astrbot` → 生产回来
8. 取消保险丝（创建 `window.done`）

#### 13.12.2 判决性证据

**生产容器停机是干净的**（这是数据安全的前提，不是附带结果）：

```
docker kill -s INT astrbot  → 5 秒后 running=false，exit=0，restarts=0
停机后 /mnt/dm-0/astrbot/data/data_v4.db-wal **不见了**
  （WAL 8,252,392 B → 已 checkpoint 进主库并被删除；data_v4.db 仍是 10,887,168 B）
日志尾部：[22:51:42.825] 🌈 Shutting down AstrBot...
          [22:51:43.130] Attempting to terminate platform adapter default_1234567890 ...
          [22:51:43.131] QQ 官方机器人接口 适配器已被关闭
```

**新壳真的连上了 QQ**（不看日志看 TCP）：

```
python 进程（pid 18148）到 120.241.201.18:443 有 ESTABLISHED
Resolve-DnsName api.sgroup.qq.com → 120.241.201.18 / 112.53.42.141
```

也就是说：**从 NAS 搬过来的那份 `appid`/`secret`，在 Windows 上真的通过了握手**。
这不是配置文件长对，是网络那头认了它。

**消息进来了，也回出去了**：

```
[22:52:06.818] [qqofficial.qqofficial_platform_adapter:304]: [QQOfficial] Websocket session starting.
[22:53:23.400] [core.event_bus:155]: [default] [default_1234567890(qq_official)]
               0123456789ABCDEF0123456789ABCDEF: [At:qq_official] 在吗在吗
[22:53:24.602] [respond.stage:237]: Prepare to send - /0123456789ABCDEF0123456789ABCDEF: 在喵，怎么了？
```

三个要素齐了：**入站事件带平台 ID**（`default_1234567890(qq_official)`）、
**人格起了作用**（回的是"在喵，怎么了？"这种语气，不是通用模板）、
**发送被执行了**（`Prepare to send` 之后没有发送错误）。

**两个 WARN 是已知的、也是预期的**：

```
Provider whisper-local was not found. Its provider or model ID may have been changed.
Provider edge-tts was not found. Its provider or model ID may have been changed.
Session ... has no speech-to-text provider configured.
```

这正是 §13.4 的 P-1（`edge_tts` / `whisper` 不在源码 `requirements.txt` 里），
也正是 `manifest.runtime_pip`（51 个包）存在的原因。**它不影响文字收发**，
只意味着这个壳暂时听不了语音、也合成不了语音。装完那 51 个包就没了。

**停机也是干净的**（Windows 侧）：

```
[22:53:50.838] 🌈 Shutting down AstrBot...
[22:53:50.839] Attempting to terminate platform adapter default_1234567890 ...
[22:53:50.839] QQ 官方机器人接口 适配器已被关闭
[22:53:50.841] 知识库数据库已关闭
```

**生产容器回来了，而且这次它带上控制面**：

```
docker start astrbot → 1 秒 running=true，started=2026-11-14T14:53:56Z，restarts=0
[22:54:08.138] Loading plugin astrbot_plugin_molt ...
[22:54:08.150] [Molt] 控制面已注册 4 个端点（前缀 /api/plug/astrbot_plugin_molt）
[22:54:08.150] [Molt] 控制面已就绪（v0.1.0）…
[22:54:13.481] Starting WebUI at http://0.0.0.0:6185
[22:54:14.012] [QQOfficial] Websocket session starting.
```

#### 13.12.3 生产源壳现在可以被"按住"了 —— ✅【实测 22:54:31】

这一条比 QQ 收发更容易被忽略，但它才是"迁徙"从**演示**变成**产品**的分界：
在此之前，NAS 端要用 `docker kill -s INT` 才能让源壳停下来，
而那意味着丢掉调度器、定时任务和正在跑的一轮对话。

装上控制面插件之后，**生产容器自己也走 drain**：

```
drain 前 speaking = True
drain 返回 (0.02s): ok=True drained=['default_1234567890'] still=0
drain 后 speaking = False
resume 返回: ok=True resumed=['default_1234567890'] skipped=[]
resume 后 speaking = True（1 次轮询后）
最终 connected = ['default_1234567890']
```

容器日志里的两行是判决性的（**同一个 platform id，先 terminate 再 load**）：

```
[22:54:31.647] [platform.manager:300]: Attempting to terminate platform adapter default_1234567890 ...
[22:54:31.648] [qqofficial:1092]: QQ 官方机器人接口 适配器已被关闭
[22:54:32.665] [platform.manager:144]: Loading IM platform adapter qq_official(default_1234567890) ...
```

`molt migrate --control-url http://127.0.0.1:6185 --control-config <cmd_config.json>`
现在在 NAS 上是**可以真的跑通**的——源壳闭嘴 0.02 秒，拍完快照再 `resume`，
AstrBot 进程一次都没重启。

#### 13.12.4 保险丝：为什么窗口需要一个不依赖任何一方的兜底

窗口期间**生产容器是关着的**。那段时间里唯一能保证"鱼会回来"的是人的手，
而人的手会累、会分心、会因为 SSH 掉线而消失。

`tools/window-guard.py` 跑在权威那一侧（NAS 宿主），不需要网络、不需要客户端配合：
超时就把 `docker start <container>`。窗口正常结束时人创建 `window.done`，它看到就退出。

**默认动作是"恢复"而不是"什么都不做"**，理由是两种错误的代价不对称：

| 错误 | 后果 |
|---|---|
| 漏恢复（超时什么都不做） | 鱼一直哑着 —— **静默故障**，没人报错，只是没人回消息 |
| 误恢复（容器多起一次） | 一条日志 |

三个分支都实测过（**用假容器验第三条，绝不拿生产试**）：

| 场景 | 结果 |
|---|---|
| 超时 + 容器还在跑 | `容器已经在跑，不需要恢复。` rc=0，生产 `StartedAt` 未变 |
| 窗口正常结束（有 `window.done`） | `不动容器` rc=0，生产 `StartedAt` 未变 |
| 超时 + 容器停着 | `docker start molt-guard-test → 返回码 0`，`running` false→true |

它**不是** fencing，**不能**防两条鱼同时在线。互斥仍然只由 Warden 的租约
与"先死后生"负责——保险丝只管"别让鱼一直哑着"这一件事。

#### 13.12.5 这一关之后还缺什么

- **51 个运行时包**（`edge_tts` / `whisper` 等）没装 → 语音输入输出不可用，文字收发正常
- ~~`host` 字段在容器里报的是 docker 容器 ID~~ ✅ **已修并实测【2026-11-14 23:01】**
  `host_label()` 的优先级现在是 `MOLT_HOST_LABEL` 环境变量 → 插件目录下的 `host_label.txt`
  → 主机名 → `"unknown"`。第二层是为**已经跑着的容器**加的：改一个文件立刻生效，
  不必为了一个显示名字去重建一个有状态的容器。真机验证：往
  `/mnt/dm-0/astrbot/data/plugins/astrbot_plugin_molt/host_label.txt` 写一行 `UGREEN-776B`，
  优雅重启后 `GET /status` 返回 `host = UGREEN-776B`。
  **为什么这个文件不会被灵魂带走**：`soul.py` 的 `COPY_ARTIFACTS` 只搬 `plugin_data/**`，
  不含 `plugins/**` —— 壳名属于壳，不属于魂。这一点必须是刻意的，
  否则迁完之后新壳会自称是旧壳。
- **断点续传**未做：27.3 MB 靠一次 PUT 没问题，再大就该做了

**实测教训（写在这里给以后的人）**：这个字段的错不是"报错了"，而是**它报了一个看起来像
标识符的东西**。人在 QQ 上问「你现在已经转移到 windows 了」，鱼回答
「我摸了摸自己，脚下还是 Linux，主机名 `container-id`，也没有 C 盘喵。」
——判断是对的（那会儿它确实还在 NAS 上），但那串十六进制让这句话没法读。
**一个"能显示但不通顺"的字段，比一个明显空着的字段更容易被一直留着。**

---

### 13.13 自己用：为什么是"两台机器各敲一条命令"【2026-11-14 23:12】

v0.1 的验收标准是"作者本人能稳定地用起来"，不是"一条命令"。原本想做更省事的形态
（在 Windows 上敲一下就把鱼叫过来，由 Windows 反向驱动 NAS 的容器），实测撞到三个硬约束：

1. NAS 的 `/etc/ssh/sshd_config` 里是 **`PubkeyAuthentication no`**，`PasswordAuthentication yes`
2. NAS 上 **`~/.ssh` 目录根本不存在**（没有 `authorized_keys`）
3. Windows 侧 `~/.ssh` 只有 `known_hosts`、**没有私钥**，而且没有 `sshpass` / `plink`；
   本机 PowerShell 是 5.1（连 `??` 都不支持）

要免密就得改 sshd 并重启它，**而改 sshd 有把远程访问弄丢的风险**——为一个还没被用过几十次的
工具链冒这个风险不划算。所以选了最不刺激的方案：**两台机器各敲一条命令**，
NAS 上的任何系统配置都不动。

**产物**：`docs/SELFUSE.md`（310 行，一页纸：去程 5 步 / 归程 5 步 / 三条铁律 / 退出码表 / 常见症状）
与 `tools/platform-switch.py`（206 行）。

`platform-switch.py` 补的是一个**结构性缺口**：`molt land` 落位时**一定**会把
`platform[].enable` 关掉（§6.6 覆写表第 0 条，公理 0 护栏），而重新打开的唯一途径就是人手改 JSON。
手改有三个坑，这个脚本逐个堵住：

| 坑 | 这里的处理 |
|---|---|
| BOM | 读用 `utf-8-sig`；写之前**先看原文件前 3 字节**，原来有 BOM 才写 BOM |
| 全量重写产生噪音 diff | 只改 `platform[].enable`，`appid` / `secret` / `jwt_secret` / `config_version` 一个不碰，原子替换 |
| 关错对象 / 误开 | `on` 与 `off` **都必须显式 `--yes`**，不给就只报告；写回后**再读一遍复核** |

第三条的措辞是刻意的：**这个脚本能让你的一只鱼开始说话**，所以两个方向都该是人的决定，
不该有"顺手"这个选项。

真机验证：对生产配置只 `show` / dry-run（**生产 `enable` 仍是 True，没被改动**）；
对复制件 `off --yes` 后逐键对比只有 `差异键: ['platform']`，`appid` 与 `jwt_secret` 原样，
BOM `efbbbf → efbbbf`；另造一个无 BOM 样本，`7b0a20 → 7b0a20`（**没被加 BOM**）。
测试 `molt/tests/test_platform_switch.py` 18 用例 + 变异 5/5 全抓。

**bug #13**（这个测试抓出来的）：第一版 `save()` 无条件用 `encoding="utf-8-sig"` 写回，
于是**给本来没有 BOM 的文件加上了一个 BOM**。`utf-8-sig` 在**读**的时候是"吃掉 BOM"，
在**写**的时候是"加上 BOM"——**这两件事方向相反，很容易只记住一半**。

---

### 13.14 NAS 重启之后，那条链路会自己回来 —— ✅ 已通过【实测 2026-11-15 02:20】

**为什么非做不可**：到这一步，整条链路上有三个常驻进程跑在 NAS 上——
Warden（账本 / 发租约）、`molt-homer`（回家接住）、`molt-bounce`（鱼回家之后再把它推出去）。
它们全是手敲 `python3 tools/start-*.py` 起的。**NAS 一重启，三个全没，而断得很安静**：
鱼还在 QQ 上说话，人也没收到任何告警，只是从那一刻起谁都搬不动了。
更糟的是它的反面——`docker` 容器是 `restart=unless-stopped`，
如果哪天守护进程自己起来了、容器也起来了，而鱼正在 Windows 上说话，那就直接撞公理 0。

**这台 NAS 没有 systemd**：`PID 1 = /sbin/procd`（OpenWrt 式），
`/etc/rc.d/S95done` 在开机最末尾 `sh /etc/rc.local`，`/etc/rc.d/S50cron` 拉起 crond。

#### 13.14.1 装法（两处都装，belt and braces）

`/etc/rc.local`（在 `exit 0` 之前）：

```sh
# molt: 三个守护进程（账本 / 回家 / 往返）。S95done 在开机最末尾调这个文件，
# 那时 /mnt/dm-0 已经挂上了。supervise 只补不杀，重复调用安全。
/usr/bin/python3 /mnt/dm-0/astrbot/_molt/pkg/tools/molt-supervise.py --quiet --wait 60 >> ...
```

`crontab -l`：

```
@reboot     .../molt-supervise.py --quiet --wait 60 >> ...
*/2 * * * * .../molt-supervise.py --quiet          >> ...
```

**为什么 `@reboot` 之外还要一条每 2 分钟的**：`@reboot` 由 `S50cron` 触发，
**可能早于 `/mnt/dm-0` 挂载**——那时脚本连自己在哪都不知道。
定时那条是兜底，也是"某个进程半夜挂了"的唯一发现机制。
**为什么不是 `*/5`**：鱼回家之后要等 `bounce` 把它推出去，中间这几分钟里少一个进程，
人看到的现象就是"它怎么不动了"。2 分钟是我能接受的失联上限。

#### 13.14.2 两条设计规矩（写进 `tools/molt-supervise.py` 的 docstring）

1. **只补不杀**。在跑就跳过，绝不重启。
   `start-warden.py` 那个脚本是"先杀掉旧的再起新的"，**绝不能拿来当看门狗**——
   每 5 分钟把 Warden 打断一次，恰好赶上一次迁徙就是数据事故。
2. **光看 pid 存在不算数**。机器重启后 pid 从低位重新分配，旧 pidfile 很可能指到
   一个完全无关的进程上；那时"看 pid 存活"会说"已经在跑"，于是永远不起。
   判据必须是 `/proc/<pid>/cmdline` 里真的出现那个 marker（`molt.cli` / `molt-homer.py` / `molt-bounce.py`）。

**变异测试（不需要重启就能证它有牙）**：停掉 bounce 后 `echo 1 > bounce.pid`——
pid 1 一定活着但完全无关。只看 pid 存活会答"在跑"；`--status` 答的是
`bounce: 没在跑`，随后补齐成 `已启动 pid=28675`，cmdline 核对真的是 `molt-bounce.py --config ...`。

#### 13.14.3 第一次真机重启：三行"已启动"，两个进程已经死了

清空 `supervise.log` 后重启，打开看到的却是：

```
warden: 已启动 pid=8869
homer: 已启动 pid=8870
bounce: 已启动 pid=8871
```

两分钟后 `--status` 说**三个都没在跑**，`netstat` 里 6186 也没有监听；
`homer.log` / `bounce.log` 里躺着 `[Errno 101] Network is unreachable`。
是那条 `*/2` 的 cron 在 02:18 又把它们补了回来——**兜底救了主路径**。

于是这件事的本质露出来了：**"我把一个进程拉起来了"和"那个进程还活着"是两件事**，
而 `supervise.log` 当时只记录了前者。这和本项目反复踩的那一类 bug 完全同形：
**它报成功，但它没做成。**

修法两条，都落在 `tools/molt-supervise.py` 里：

- **等真正的就绪信号**（`--wait N`）。判据是"`/proc/net/route` 里出现默认路由"，
  不是"磁盘挂上了"，也不是"过了 30 秒"。磁盘没挂 → `磁盘未挂载`；磁盘挂了但没路由 →
  `网络未就绪`；两次都能自己说出是哪一种。
- **启动之后回头看它一眼**。`start()` 里 `sleep(START_GRACE=1.5)` 再查一次
  `running(spec)`：还活着才算"已启动"，否则报
  `启动了 pid=N，但它立刻退出了（日志：<最后三行>）`，并把退出码置 1。

**新判据的变异测试**：起一个 `raise SystemExit(3)` 的进程 →
`fake-dies: 启动了 pid=17621，但它立刻退出了（日志：boom: 网络还没起来）`；
起一个 `sleep 30` 的 → `fake-lives: 已启动 pid=17933`。**它现在会为"假成功"报错。**

#### 13.14.4 第二次真机重启：三行，全部还活着

再次清空日志并重启，这次开机后 `supervise.log` 只有三行，且三行都成立：

```
warden: 已启动 pid=7174
homer : 已启动 pid=7280
bounce: 已启动 pid=7328
```

`--status` → 三个 `在跑`；`uptime 0 min` 时 `netstat -ltn` 已有 `0.0.0.0:6186 LISTEN`。

四道复查（都在开机后一分钟内）：

| 查什么 | 结果 |
|---|---|
| 账本还在吗 | `epoch 15 / gens 13 / latest 13 → desktop-h2kgiao / consumed_by desktop-h2kgiao / lease no-lease` ✅ |
| homer / bounce 连得上账本吗 | 两边日志都是 `[idle] 第 13 代是给 desktop-h2kgiao 的，不是我`，**没有 `读不到账本`** ✅ |
| 容器有没有自己起来（公理 0） | `running=false exit=0` ✅ `unless-stopped` 没有把被 SIGINT 显式停掉的容器拉起来 |
| docker 守护进程自己起来了吗 | `20.10.16` ✅（它是容器能不能起的前提，也是上面那条的对照组） |

**这一节的结论只有一句**：`supervise.log` 里写"已启动"不代表什么，
**下一次 `--status` 说"在跑"才算**。所以我把它写成两条：等就绪、回头看一眼。

---

## 14. 已知风险台账

| 风险 | 等级 | 缓解 |
|---|---|---|
| 上游 AstrBot 改生命周期 API | 中 | 接缝集中在 `molt/adapters/astrbot/`，一处改；CI 跑真实容器机型测试 |
| ~~Windows 原生装不上~~ | ~~高~~ **已消除** | ✅ §9.4 已实测：178 包零缺 wheel |
| ~~WOL 唤不醒~~ | ~~高~~ **已消除** | ✅ §7.4.2 已实测：真 S5 → 23.1 秒 PASS |
| ~~**WOL 的 arm 状态重启即丢**~~ | ~~中~~ **已消除** | 第二次冷启动后 `wake_armed` 里就没有网卡了 → 表现为"第一次好使、第二次叫不醒"。**已解**：`Molt-WolGuard` 计划任务在开机第 11 秒自动重新 arm（§7.4 实测 13:00:34，`改前 False → 改后 True`）。保留 `check-wol.ps1 -Json` 作为迁徙前校验 |
| WOL 状态在系统更新/驱动更新后被重置 | 低 | `check-wol.ps1 -Json` 在每次迁徙前必跑，`ready_to_wake=false` 就拒绝迁徙并告警；`Molt-WolGuard` 每 10 分钟兜底一次 |
| ~~停机把 WAL 留在盘上~~ | ~~中~~ **已消除** | ✅ §6.4 实测：`docker stop`(SIGTERM) 退出码 137、WAL 41,232 B 原封不动；**`docker kill -s INT` 退出码 0、WAL 归零**。停机方式定为 SIGINT |
| ~~**Windows 上没有等价的优雅停机信号**~~ | ~~中~~ **已消除** | 已验 14:44（§13.10）：`AttachConsole` + `GenerateConsoleCtrlEvent(CTRL_C_EVENT, 0)` 走完整优雅路径，退出码 0、`kb.db` 的 98,912 B WAL 真的合并进主库。**注意 `os.kill(pid, CTRL_BREAK_EVENT)` 是假阳性**——退出码 `0xC000013A` 看着像生效，其实默认处理器直接杀了进程 |
| NAS 是单点（掉电/磁盘挂） | 中 | Litestream 二级备份；md0 是 RAID0 零冗余，**必须外部备份** |
| 用户忘记 `molt home`，Windows 整夜开机 | 低 | 定时器 + 空闲检测；电量告警 |
| QQ 平台策略变化 | 中 | 平台适配层隔离；支持 aiocqhttp 等其他接入 |
| 交接期间用户发消息丢失 | 低 | drain 超时 180 s；失联期由平台侧缓存（QQ 有离线消息） |
| ~~**传输是手工的**~~ | ~~中~~ **已消除** | ✅ §13.8：跨机字节传输已自动化。源端 `migrate` PUT 归档到 Warden 中转区（`<warden_root>/archives/`），目标端 `land --generation N` GET 下来。**不需要 scp / 共享盘 / 第三套凭证**——复用已有 HTTP 通道与 token。真机两条命令走完，real 1.61s |
| ~~**归档中转区只增不减**~~ | ~~低~~ **已消除** | ✅ §13.9：`molt prune`，四条保守规则 + 默认只看不删。真机实测：`gen-000005`（账本里记着）与 `somebody-else.tar.gz`（不是 molt 放的）都留下，两个账本不认的被回收并留痕。41 个用例 + 3 个变异全抓 |
| 传输失败要整份重传（无断点续传） | 低 | 4.4 MB 无所谓，30 MB 以上值得做（§13.8.5） |
| **机器绑定配置忘了覆写** | 中 | 已由 `overlay.py` 默认处理 **7 处**（§6.6），且有 **30** 个测试固定；新增 provider 时**必须同步检查 `agent_runner` 里有没有第三处引用** |
| ~~**落位后忘了关 `platform[].enable`**~~ | ~~高~~ **已消除** | **2026-11-14 14:41 差一点就出事了**：三次 Windows 启动都真的发起了 QQ websocket 连接（`[QQOfficial] Websocket session starting.`），只因进程数秒内被杀才没变成双活。修法：`_disable_platform_adapters()` 作为覆写表第 0 条，两条预设都不给关闭便利；14:56 复验 `Loading IM platform adapter` / `QQOfficial` **零命中**（§6.6.7）。**这是本项目唯一一条"漏了直接违反公理 0"的检查项** |
| ~~**`drain()` 找不到该关谁**~~ | ~~高~~ **已消除** | `WebChatAdapter` 由 `manager.py:116` 直接 append，**从不进 `_inst_map`**，`config.platform` 里也没有它 → 第一版 `/drain` 什么都没关却返 409。修法：`_classified()` 改用**实例身份**判据，webchat 归 `local_only`（§6.2.5–6.2.7，§13.6 第 12 条）。**17 个用例 + 10 个变异全抓** |
| **`drain` 之后其实还在说话** | 中 | `drain_and_verify()` 轮询复核 `speaking`，仍为真就 `NeedsHuman("不要继续这次迁徙…公理 0")`。**必须复核**：`terminate_platform` 关不掉时只记一条 `logger.warning`，不抛（§6.2.9） |
| **能远程 drain 对方的壳 = 一个能远程关掉它的后门** | 中 | `overlay.py` 落位时**重新生成 `dashboard.jwt_secret`** → 迁完之后源壳无法控制目标壳、反之亦然（§6.2.8）。JWT TTL 只有 300 秒（AstrBot 自己签 7 天） |
| **`resume` 可能顺手把鱼放回线上** | 中 | `resume()` **只恢复配置里 `enable=true` 的**，`skipped_disabled` 显式报告跳过了谁。让这只鱼重新对外说话必须是人的决定，不是 resume 的副作用（§6.2.7） |
| **`-wal` 非空时被当成普通文件搬走** | 中 | 已消除：`_sidecar_noise()` 在打包阶段 `Fatal`，`restore_into` 在落位后复查并清理（§13.6 第 7 条） |
| **下载中途断开，半截文件落在正式文件名上** | 中 | 已消除：下载先写 `<dest>.part`，验过 sha256 才 `os.replace`。**变异测试证明这条不是废话**——去掉 `.part` 后 19 个测试全过，只有"中途断开"能区分（§13.8.4） |
| 快照目录被系统清理（`/tmp`、重启） | 中 | 已消除：staging 从 `/tmp` 改到 `LOCALAPPDATA` / `XDG_DATA_HOME` / `~/.local/share` 下的 `molt/staging`（§13.6 第 5 条） |
| ~~**迁徙第一步依赖人发魔术封包**~~ | ~~中~~ **已消除** | ✅ §13.9：`molt wake` 进了产品，且 `migrate` 里是**先唤醒 → 再拿租约**（调用顺序有测试固定）。叫不醒直接 `return 5`，账本里不会留下指向一台没开机的机器的记录 |
| **`prune` 误删还需要的东西** | 低 | 四条规则里第 1 条是硬边界（账本里记着的一律不删，有专门测试），且默认 `dry_run=True`。真机上真要删必须显式 `--yes` |
| 有人手工往 `archives/` 里放东西 | 低 | `_parse_generation_from_name()` 只认 `gen-NNNNNN.tar.gz`，其余一律不删 —— 清理逻辑不对不认识的文件行使删除权 |
| ~~**交接被「关机」拖住：机器前有人，鱼就回不了家**~~ | ~~高~~ **已消除** | 原设计让 NAS 等「Windows 离线」才接管，把接管判据挂在了**电源状态**上。**2026-11-14 作者本人指出**：机器前坐着人不肯关机，鱼就永远回不来 —— 而它其实在 Warden 说 `accepted` 时就已经到家了。修法：接管判据改成「账本接受了快照」，关机独立成 §5.5（v0.1 里**没有任何一条命令会关机**）。**电源状态不是数据状态的证据** |


---

### 14.1 v0.4 硬化：所有持久化写入必须原子（2026-11-15）

这一版只做一件事：**把「中间态」从系统里消灭掉。**

理由是一句话：**这套系统里最贵的错误全都长得像成功。**

| 看起来像成功的东西 | 实际发生了什么 | 谁会受害 |
|---|---|---|
| `archives/gen-000012.tar.gz` 存在 | 打包写到一半被杀，gzip 头是完整的，`tarfile` 能读出前几个成员 | 目标壳把半份快照当成完整快照落位 |
| 目标 `data/data_v4.db` 存在且大小像样 | 落位复制到第二个文件时失败，第一个文件是新的、第二个是旧的 | 它带着一半新记忆、一半旧记忆醒来 |
| `molt land` 报了个 `OSError` | 账本写盘满，`state.json` 还是上一版完整内容 | 人以为账本记下了，其实没有 |
| `manifest.json` 能 `json.loads` | 它少了 `generation` 字段，`int(d["generation"])` 抛 `KeyError` | 排查的人去翻代码，而问题在那份快照上 |

四条修法（都有故障注入测试钉住，见 `docs/FAULTS.md`）：

1. **打包写 `.part` 再 `os.replace`**（`molt/transfer.py::make_archive`）。`.part` 必须与目标**同目录** —— `os.replace` 只在同一文件系统内原子，跨文件系统会退化成"拷贝 + 删除"，那就没有原子性了。这个正确模式 `server.py::_receive_archive` 早就用了，`make_archive` 漏了。
2. **落位逐文件 `.molt-part` → `os.replace`**，全过程有 `.molt-restore-incomplete` 标记；两个守护进程启动前都读这个标记，读到就不启动。标记**只有被人读到才算数**，所以 `tools/molt-agent.py::do_boot()` 与 `tools/molt-homer.py::do_start()` 各有一道守卫。
3. **`_atomic_write_json` 把非 `MoltError` 包成 `Fatal`**："目标文件还是上一次的完整内容 —— 这次写入没有落盘。" 账本写不进去时，宁可这次操作失败，也不能让它看起来成功。
4. **`manifest_problems()` 先验形状再取值**：缺字段 / 类型错 / 空来源 / 未知 `schema_version` 一律 `Fatal` 并点名字段。理由写进了 docstring：直接 `int(d["generation"])` 报的 `KeyError` / `ValueError` **指向"代码写错了"而不是"这份快照有问题"**，于是会被人当成 bug 而不是被当成拒收理由。

`molt land --generation N` 另加一道交叉校验：**manifest 里的代次必须等于 `N`**（`.molt/cli.py`）。否则就是"拿 A 代的出生证明去描述 B 代的内容"，而这两者都对得上各自的一半 —— 只有交叉比对能发现。

一个容易忽略的细节：守护进程**不能** `import molt.transfer`（它们是被 `python tools/xxx.py` 直接跑起来的脚本，`sys.path` 里没有仓库根），所以 `RESTORE_MARKER_NAME` 这个字符串只能复制。**复制的东西必须有测试盯着** —— `test_hardening_restore_guard.py` 把三处常量钉在一起，改一处不改另两处会红。

### 14.2 v0.5 可安装性：生成器自己认为对不算数（2026-11-15）

v0.4 的教训是「最贵的错误全都长得像成功」。这一版把它用在**配置生成**上。

四条决定：

| 决定 | 为什么 |
|---|---|
| 生成物**落盘前**先交给守护进程自己的 `Settings.load()` 读一遍 | 生成器和加载器是两份代码，两份代码就会漂移。让真加载器当判据，漂移在写盘前暴露 |
| `execute` 默认 `false` | 这个字段为真意味着「这台机器会自己关机」。第一次装上应当只观察 |
| `shell` 必须由人给，缺了就报错 | 账本里的 `target_shell` 是**别人写下的字符串**。猜错，这台机器永远等不到自己那一代，且没有任何报错 |
| 已有配置默认不覆盖，`--force` 才覆盖且留 `.bak` | 一份调通的配置比一份「更新过的」值钱 |

`molt doctor --config` 把三类失败分开报，因为修法完全不同：加载器读不过（改字段）、
连不上（网络/地址）、HTTP 401（token）。之前这三种都会表现成同一句话 ——
「它不动」。

一个副作用值得记下：`validate_config()` 会把加载器说的 `execute 是 false` 从问题
列表里**过滤掉**。加载器把它报成 problem 是对的（它确实不能执行），但对生成器来说
那是刻意的默认 —— **同一个字符串，在两个调用方的语义不同**，所以过滤表的键是那段
前缀文本本身，而不是一个布尔开关。

### 14.3 v1.0 可发布性：证据必须能自己站着（2026-11-15）

v0.5 之前，「它搬过去了」这件事的证据是**我贴的终端输出**。那不算证据，
因为只有我在场时它才存在。v1.0 补的三件事都是为了让证据离开作者也成立：

| 决定 | 理由 |
|---|---|
| 时间轴是**单文件、零外部请求**的 HTML | 它要能被发给别人、在断网的 NAS 旁边、在手机上打开。任何一次外部请求失败，都会让「它搬过几次家」变成一张白屏 |
| 渲染是**纯函数**，CLI 与 HTTP 共用 | 两处各写一遍，迟早出现"命令行的图和网页上的图对不上"这种没法解释的差异 |
| `/timeline` 认 `?token=`，`/history` 不认 | 能改租约的 token 出现在 URL 里会被记进浏览历史、被转发、被截图。所以白名单只留只读页面，且必须窄到逐个列 |
| 镜像**没 token 就拒绝启动**（exit 78） | 容器里"随机生成一个 token"意味着每次重启都换，所有壳一起 401，而症状看起来像网络问题。失败要在启动那一刻 |
| 归档指纹写在**归档旁边**（`*.sha256`） | 上传顺序是"先传字节、后登记 generation"，收归档时账本里还没有这一代。改成账本字段就要引入"待认领指纹"的中间状态 |
| CI 里加两个**会红的** job | `pytest` 绿只说明函数对；「陌生人装得起来」和「镜像能跑」必须真的做一遍，否则它们只是文档里的形容词 |

**这一版的教训**：时间轴做出来才发现账本里那个 `manifest_sha256` **从来没有被写过值**，
所以「指纹」那一列一直是空的。空着的一列比没有这一列更糟 —— 看见"指纹"两个字，
人会以为这里能核对。早期那 19 代仍是空，那是诚实的结果：那时候确实没记。

## 15. 开放问题（需要决策）

| # | 问题 | 选项 | 建议 |
|---|---|---|---|
| Q1 | ~~是否需要"两只鱼同时在线"~~ | **已定案：互斥，且永不真并发** | **用户已否决（m00434）**，见 §2.2 C2 与 §11.3 |
| Q2 | 是否开 AutoAdminLogon | 开 / 不开 | 默认不开（headless 走服务），desktop 模式按需显式开启 |
| Q3 | Windows 端用服务还是计划任务 | 服务 / 任务 | 服务（不依赖登录，能崩溃自启）；计划任务作兜底 |
| Q4 | ~~命名~~ | **已定案：`molt`（蜕壳）** | **用户已定（m00434）**。约束：「大肥鱼」不能当项目名——Coopanion 生态已有 4 个 fatfish/大肥鱼 仓库。它只能当角色名 |
| Q5 | 开源许可证 | MIT / Apache-2.0 / AGPL | Apache-2.0（专利授权 + 商用友好；注意 private_companion 是 **AGPL-3.0-only**，不能直接抄它的代码） |
| Q6 | Windows 是否 24h 常开 | 常开 / 用完就睡 | **用完就睡**（空闲 44 元/月、满载 159 元/月，而 NAS 常开只有零头；Molt 存在的意义就是"用完让它睡"） |
| Q7 | 记忆"冷却期"是否需要 | 需要 / 不需要 | 待定。迁徙后第一次对话是否要显式提示"我换了个身体"——这既是产品问题也是信任问题 |
| Q9 | Windows 网络档案是否改 Private | 改 / 不改 | **改**。当前是 Public，入站更严；将来 MoltAgent 要开端口接收 Warden 指令会被防火墙拦 |

**Q8（新增，P0 已答）**：AstrBot 能不能在 Windows 上原生跑？→ **能，已实测通过**（见 §9.4）。v0.1 不再有"方案可能不成立"的风险。

**Q10（新增，WOL 已答）**：WOL 这一关能不能过？→ **能，已实测通过**（见 §7.4.2，真 S5 关机 → 23.1 秒唤醒）。

### 15.1 术语与学术定位（要抢的词）

**`embodiment handoff`（换体交接）**【查证，2026-11-14】：

- GitHub 搜 `"embodiment handoff"` → **total = 0**
- arXiv 搜 `all:"embodiment handoff"` → **total = 0**

**这个词无人占用，可以抢注。** 但必须主动划清与邻居的边界，否则会被质疑"重复造轮子"：

| 邻近概念 | 重叠 | 区别 |
|---|---|---|
| 数据库主备 / HA | 状态一份、故障时切 | 切的是**无状态读写入口**，不携带人格与连续记忆 |
| Live migration（VM 热迁移） | 运行中搬家、连接不断 | 要求**同构虚拟化层**；我们跨 Linux 容器 → Windows 裸机 |
| Session handoff | 接力 | 交的是**一段上下文**，交完身份就结束 |
| Edge-cloud offloading | 按需把负载放到合适位置 | 优化延迟/成本/算力，角色是任务不是人格。**最该引用也最该划清的邻居** |
| 数字分身 / 数字永生 | 叙事张力 | 那类卖"**复制一个你**"，我们卖"**同一个它换身体**"。复制 vs 迁徙互斥 |

arXiv `abs:"agent migration" AND abs:"state"` 有 3 篇（如 "Adaptive AI Agent Placement and Migration in Edge Intelligence Systems", 2025-08-05）。**必须引用以示区隔，不能宣称首创。**

### 15.2 竞品扫描结论（写进 README 用）

【查证，2026-11-14 快照】**全市场无"同一人格在物理机间热迁移"的实现**：

| 找了什么 | 结果 |
|---|---|
| AstrBot 插件市场全量 3383 条关键词扫描 | **零**个跨物理机人格迁徙插件 |
| 最接近的 `AstrSync` 多机互联 | "多台 bot 互相看见对方说话"，**不是搬家**。0 star / 2 次下载 |
| 生态里的 "failover" 类插件 | 一律指**换 LLM API**，不指换机器 |
| 官方 AstrBot Launcher（≈2.7k star） | 多实例**并存**，各有人格 |
| Coopanion（≈3.1k star） | README 原话 "Copy the whole folder and you have a **second** Coo"——**克隆出第二个独立人格**，正是本项目否定的模型 |
| `kabudu/permeant-os` | 描述几乎逐字同义，**0 star** |
| `agent-soul` / `persona-hub-core` | 2 star / 1 star |

**上游官方留下的正当性缺口**【查证】：AstrBot 官方 Kubernetes 文档原文——

> "由于 AstrBot 当前使用 SQLite 数据库，此部署方案不支持多副本水平扩展。"

不是"不支持"，是"架构决定了支持不了"。这是一个待解的题，不是一个待补的 bug。

**但必须有清醒的认知**：`permeant-os` 概念完全正确、0 star；`agent-soul` 2 star。**概念不带来 star。** 所以路线图里每一版都必须产出可展示物（见 §12）。

### 15.3 最危险的替代品：不是竞品，是那个 30 秒土办法

```
docker stop astrbot
cp -r /mnt/dm-0/astrbot/data  \\windows\share\
# Windows 上解压、改配置、起 AstrBot、重登 QQ
```

**这确实能续上人格。** 所以本项目的价值**绝不能押在"能搬"上**，必须押在：

| 押什么 | 土办法做不到的原因 |
|---|---|
| 自动化 | 手抄 6 步、改 3 处路径、重登 2 次账号 |
| 无感 | 停容器到起容器之间的窗口期，消息全丢（不在线就收不到） |
| 双向回迁 | 回来再来一遍，且没有"记忆变少了"的校验 |
| 人格连续性保证 | 土办法没有任何校验；拷错了、拷一半、行数变少，你不知道 |

**这四条就是护城河。** 一条都不能少，全部已写进 §5 交接协议与 §13 验证清单。

---

## 附录 A：目录结构

```
/mnt/dm-0/molt/                      ← NAS 侧
  soul/
    identity.json
    generations/
      gen-000001.db
      gen-000002.db
      …
    blobs/
    workspace/                       ← git 仓库（人格 / 笔记 / 交接本）
    manifest.json
    lease.json
    ledger.jsonl
  warden/
    warden.py                        ← 单文件，标准库
    warden.toml
  logs/
  bin/
    molt                             ← 壳侧 CLI
```

```
shell 侧（任一壳）
  <shell_root>/
    bin/molt
    adapters/astrbot/                ← 版本敏感接缝，唯一的"贴着上游"层
      plugin/                        ← astrbot_plugin_molt
      lifecycle.py
    core/
      lease.py
      snapshot.py
      transport.py
      manifest.py
    state.json
```

## 附录 B：术语表

| 术语 | 含义 |
|---|---|
| **Soul** | NAS 上的唯一真实来源目录 |
| **Shell** | 一台能跑 AstrBot 的机器 |
| **Warden** | NAS 上的仲裁者 + 唯一写者，发租约、收快照 |
| **Ferryman** | 跑在壳上的摆渡体（AstrBot 插件形态 + CLI 形态） |
| **epoch** | 单调递增的租约代数，即 fencing token |
| **gen** | 快照代数，对应 `gen-NNNNNN.db` 文件 |
| **drain** | 停止接新活、等在飞的做完 |
| **checkout / checkin** | 从 soul 取一份到本地 / 把本地推回 soul |
| **chassis** | 壳的硬件能力声明（有 GPU 吗、有桌面吗、有麦克风吗） |

---

*本文档的证据等级标注请严格遵守。任何"【推断】"在实现前必须升级为"【查证】"或"【实测】"。*
