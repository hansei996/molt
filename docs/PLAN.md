# Molt · 蜕壳 —— 开源规划书

> 旧壳留下，魂走。
>
> **记忆不流动。流动的是「谁有资格读它、写它」。**

| 项 | 值 |
|---|---|
| 文档版本 | v0.1（2026-11-14） |
| 状态 | 设计定稿，待 v0.1 验证 |
| 配套文档 | `ARCH.md`（技术方案）· `architecture.html`（架构图） |
| 目标上游 | AstrBot 4.29.4 |
| 目标平台 | Linux（NAS，Docker）+ Windows 10/11（裸机） |
| 许可证建议 | Apache-2.0 |

---

## 0. 一句话

**让你的 AI 伙伴从一个身体，走进另一个身体——记忆不断，人格不散，旧的那个可以睡去。**

具体到本项目的第一个场景：

> 一条住在 NAS 上的 QQ 机器人（AstrBot + 记忆插件），
> 在你需要的时候，自己走进你的 Windows 游戏机，
> 在那里开直播、陪你打游戏；
> 玩够了，再自己走回 NAS，把 Windows 关掉。

---

## 1. 为什么这件事值得做

### 1.1 一个真实存在的、无人填补的缝隙

**上游官方自己承认了这件事做不了。** 【查证】

> "由于 AstrBot 当前使用 SQLite 数据库，此部署方案不支持多副本水平扩展。"
> —— AstrBot 官方 Kubernetes 部署文档（`docs.astrbot.app/deploy/astrbot/kubernetes.html`）

这句话是**官方留下的正当性缺口**：不是"我们不支持"，而是"架构决定了我们支持不了"。这不是一个待补的 bug，是一个待解的题。

**而这个题，全网没人做。** 【查证，2026-11-14 快照】

| 找了什么 | 结果 |
|---|---|
| AstrBot 插件市场全量 3383 条，关键词扫描 | **零**个"同一人格跨物理机迁徙"插件 |
| 最接近的 `AstrSync` 多机互联 | 是"多台 bot 互相看见对方说话"，**不是 bot 搬家**。0 star / 2 次下载 |
| 生态里的 "failover" 类插件 | 一律指**换 LLM API**，不指换机器 |
| 官方 AstrBot Launcher（≈2.7k star） | 多实例**并存**，各有人格，不是换身体 |
| Coopanion（≈3.1k star） | README 原话："Copy the whole folder and you have a **second** Coo"——**复制出第二个独立人格**，正是本项目要否定的模型 |
| GitHub 搜 `"embodiment handoff"` | **total = 0** |
| arXiv 搜 `all:"embodiment handoff"` | **total = 0** |
| 概念最接近的先例 `kabudu/permeant-os` | 描述几乎逐字同义（"Portable state infrastructure for AI agents, enabling live migration across runtimes, clouds, and hardware"），**0 star** |

**结论**：概念空白是真的，但——
**"概念正确"不带来 star。permeant-os 0 star、agent-soul 2 star、persona-hub-core 1 star，全都因为"只有哲学，没有可跑的东西"。**

这是本项目最重要的一条教训，直接决定了下面的路线图怎么排。

### 1.2 为什么"复制 data 目录"不算解决

必须诚实面对：**这个项目最危险的替代品，不是任何竞品，是那个 30 秒的土办法。**

```
docker stop astrbot
cp -r /mnt/dm-0/astrbot/data  \\windows\share\
# Windows 上解压、改配置、起 AstrBot、重新登录 QQ
```

它确实能续上人格。所以本项目的价值**绝不能押在"能搬"上**，必须押在这四件事上：

| 押什么 | 土办法做不到的原因 |
|---|---|
| **自动化** | 手抄命令要记 6 步、改 3 处路径、重登 2 次账号 |
| **无感** | 停容器到起容器之间的窗口期，消息全丢（QQ 机器人不在线就收不到） |
| **双向回迁** | 回来还要再来一遍，且没有"记忆变少了"的校验 |
| **人格连续性保证** | 土办法没有任何校验——拷错了、拷了一半、行数变少了，你不知道 |

**这四条就是产品的护城河。** 一条都不能少。

### 1.3 为什么不用 CRIU / Firecracker【查证】

第一波 reviewer 一定会用这四个词质疑。必须先写清楚：

| 技术 | 为什么用不了 |
|---|---|
| **CRIU / docker checkpoint** | Linux 内核态检查点，**无法恢复到 Windows**。且 Docker 里需 `seccomp:unconfined`、TTY 未启用（PR 38405）、建立中的 TCP 要手写 `/etc/criu/runc.conf`。官方页首直接推荐改用 Podman |
| **DMTCP** | 用户态检查点，纯 Linux；桌面会话**必须借 TightVNC 才带得走**；最新版 2.6.0 **停在 2019-08-14** |
| **Firecracker snapshot** | 原文 "Snapshots must be resumed on a software and hardware configuration which is **identical**"（不能跨 CPU）；"not guaranteed that the state of the network connections survives"；**不支持 Windows guest** |

**这三条路的共同死因**：它们搬的是**进程状态**，而进程状态里塞满了不可移植的东西（内核 ABI、CPU 特性、socket 句柄、GPU 上下文）。

> **本项目的技术论点就是：把不可移植的进程状态，降级为可移植的数据状态，在目标机上重新长出来。**
>
> 这不是妥协。这是唯一可能，也是这个项目的全部技术主张。

### 1.4 学术上站得住的位置

**抢注术语：`embodiment handoff`**（换体交接）。GitHub / arXiv 双 0 命中。

但必须在 README 里主动划清与学术邻居的边界，否则会被质疑"重复造轮子"：

| 邻近概念 | 重叠 | 区别 |
|---|---|---|
| **数据库主备 / HA** | 状态一份、故障时切到另一处 | 切的是**无状态读写入口**，不携带人格与连续记忆 |
| **Live migration（VM 热迁移）** | 运行中搬家、连接不断 | 要求**同构虚拟化层**；我们跨 Linux 容器 → Windows 裸机 |
| **Session handoff（会话交接）** | 接力 | 交接的是**一段上下文**，交完身份就结束；我们交的是**持续存在的同一个存在** |
| **Edge-cloud offloading** | 按需把负载放到合适位置 | 优化延迟/成本/算力，角色是任务不是人格。**这是最该引用、也最该划清的邻居** |
| **数字分身 / 数字永生** | 叙事张力 | 那类卖"**复制一个你**"，我们卖"**同一个它换身体**"。复制 vs 迁徙是两条互斥的产品哲学 |

arXiv 上 `abs:"agent migration" AND abs:"state"` 有 3 篇（如 "Adaptive AI Agent Placement and Migration in Edge Intelligence Systems", 2025-08-05）。**必须引用以示区隔，不能宣称首创。**

---

## 2. 产品是什么

### 2.1 形态

三个交付物，分层实现：

```
molt（Python 包 + CLI）
  ├─ warden         NAS 上的仲裁者（后端，唯一真实来源的守门人）
  ├─ agent          壳上的执行体（Windows / 任意 Linux）
  └─ adapters/      AstrBot 接缝（版本敏感，唯一贴着上游的层）
       └─ astrbot_plugin_molt    ← AstrBot 插件市场分发形态
```

### 2.2 用户看到的四件事

```bash
molt init                      # 首次：声明 Soul 在哪、这个壳是谁
molt migrate win               # 迁徙：去 Windows
molt home                      # 归巢：回 NAS + 关 Windows
molt status                    # 现在魂在哪、哪一代、有多新
```

以及在 QQ 里直接对鱼说：

```
> 鱼，去我电脑那边
鱼：好喵，等我一下（正在收尾 2 条对话…）
鱼：（约 40 秒后，从另一台机器冒出来）我到了喵，这边有张显卡，好热
```

**这个 40 秒的对话，就是本项目的全部营销素材。**

### 2.3 关键设计承诺

| 承诺 | 含义 | 怎么保证 |
|---|---|---|
| **RPO = 0** | 不丢任何一条已提交的对话 | SQLite backup API / `VACUUM INTO` 生成的快照是**事务一致**的；不是"拷到一半的文件" |
| **人格不断片** | 换完身体，它还记得昨天说的话 | 记忆放在 NAS 上的同一个目录，**不复制、只交接** |
| **失败必安全** | 出问题时，鱼还在原地活着 | 「先死后生」+「归巢 ack 不到就不关机」 |
| **可回滚** | 新壳起不来，能立刻退回旧壳 | `generations/` 保留最近 N 代，任一代都能独立打开 |

---

## 3. 技术立场（给 reviewers 看的）

### 3.1 一句话

**不用共享数据库。用"唯一真实来源 + 租约 + 快照交接"。**

### 3.2 为什么不能共享一个 SQLite 文件（两条硬证据）

**证据一**——SQLite 官方文档 `wal.html` Overview 第一条缺点，原文：

> "All processes using a database must be on the same host computer; **WAL does not work over a network filesystem**. This is because WAL requires all processes to share a small amount of memory…"

**证据二**——微软官方文档 `locking-and-unlocking-byte-ranges-in-files`，原文：

> "**Byte range locks are ignored when using memory mapped files.**"

这两条一起看就是完整的损坏说明书：WAL 用 `-shm` 文件 **mmap 成共享内存** 来协调锁状态，而 Windows 上 **mmap 与 LockFileEx 互不感知**。跨主机时各自 mmap 各自的页缓存，锁状态与 read-mark 必然失同步。

**旁证**——SQLite 自己给出的建议（`useovernet.html`）第一条就是：

> "implement a proxy that runs on the database machine"

**本项目的 Warden 就是那个 proxy。** 这不是我们发明的，是照着官方建议做的。

### 3.3 为什么租约必须带 fencing token

Kleppmann 那篇 `How to do distributed locking`（2016-02-08）里说得最狠的一句：

> "**GC can pause a running thread at any point**"

以及：

> "You cannot fix this problem by inserting a check on the lock expiry just before writing back"
>
> "This requires the storage server to take an active role in checking tokens, and **rejecting any writes on which the token has gone backwards**"

**所以**：任何基于"租约还没过期"的判断都可能被一次长时间停顿打穿。每次租约给一个单调递增的 **epoch**，写操作带上它，**存储端拒绝 epoch 倒退的写入**。这才是真的互斥。

### 3.4 明确不做的事

| 不做 | 因为 |
|---|---|
| 挂 `docker.sock` 进 AstrBot | 等于把 NAS 的 root 交给下游模型的每一次幻觉。改为宿主机白名单摆渡服务 |
| 跨机同步 `jwt_secret` | 那是把两把钥匙配成一把 |
| 把 Soul 目录挂成 SMB 盘再打开 | 见 §3.2 |
| 依赖 "QQ 互踢" 做互斥判断 | 官方文档**查不到**"同 appid 双实例互踢"的明文条款，只有 `session_start_limit` 配额。互踢只能当事后告警，**绝不能当 fencing** |
| 允许"两只鱼同时在线" | **这是产品公理，不是技术取舍**（用户 m00434 已定案）。见 §2.2 与 §7 之后的说明 |

---

## 4. 路线图

### 4.1 一个残酷的现实

**AstrBot 插件市场的 star 天花板约 550，但下载量可达 19 万。** 【查证】

| 项目 | star | 下载量 |
|---|---|---|
| AstrBot 本体 | ≈47k（46865） | — |
| `meme_manager`（市场中头部） | 556 | **191,023** |
| `private_companion` | 419 | **82,135** |
| 某插件（Zhalslar） | **0** | 90,000+ |

**这个生态的 star 与使用量严重脱钩。** 所以：
- 不要用 AstrBot 本体的 47k 来预期自己
- **真正的分发渠道是插件市场页面，不是 GitHub trending**

### 4.2 版本计划

> **2026-11-15 更新**：括号里是实际状态。**v0.1–v0.4 已经全部完成**（v0.4 的
> 16 个故障场景全过，见 [`docs/FAULTS.md`](FAULTS.md)；证据见 `docs/ARCH.md`
> 与 `docs/SESSION-STATE.md`）。**下一个版本是 v0.5 可安装可自检**：
> `molt init` 生成配置（落盘前过真加载器）+ `molt doctor --config` 自检 +
> [INSTALL.md](INSTALL.md) 十分钟路径。原定的「通用化（第二宿主适配器）」推到
> v0.6，理由见 [V0.5-PLAN.md](V0.5-PLAN.md) §0：第二宿主没有真实消费者，
> 而 v1.0 的验收标准（一条命令装起来）此前一条都没做。

| 版本 | 目标 | 验收标准 | 预估 | 状态 |
|---|---|---|---|---|
| **v0.1** 手工验证 | 证明数据不坏、Windows 能跑 | 手工 checkout/checkin 10 轮，`integrity_check` 全 ok；Windows 上 AstrBot 能收发 QQ | 1–2 周 | ✅ 完成 |
| **v0.2** 租约 | 证明互斥 | Warden + epoch；故意制造 split brain，验证旧 epoch 被拒 | 1 周 | ✅ 完成 |
| **v0.3** 自动化 | 端到端可用 | QQ 一条命令完成迁徙/归巢，全程 < 2 分钟 | 2 周 | ✅ 完成（真机 20 秒） |
| **v0.4** 硬化 | 故障注入 | 断电、断网、时钟漂移、库损坏注入，全部安全失败 | 2 周 | ✅ 完成（16 场景全过，见 [`docs/FAULTS.md`](FAULTS.md)） |
| **v0.5** 可安装可自检 | 陌生人装得起来 | `molt init` 生成三种角色配置（过真加载器才落盘）+ `molt doctor --config` 自检 + [INSTALL.md](INSTALL.md) 十分钟路径 | 1 周 | ✅ 完成（814 passed；陌生人演练 [rehearse-install.py](../tools/rehearse-install.py) 可重跑） |
| **v0.6** 自主迁徙 | 它自己决定去哪 | L1 提议权：插件里的 `molt_propose_go_home` 工具 + 30 分钟有效的提议（目标必须在账本里真的存在） | 1 周 | 🔧 L1 完成（871 passed），L2 的触发策略 / 否决窗口 / 出事谁负责三个取舍等你拍板，见 [V0.6-PLAN.md](V0.6-PLAN.md) |
| **v1.0** 可开源发布 | 别人能用 | 完整文档、Docker 镜像、插件市场上架、CI、迁移日志可视化 | 2 周 | ✅ 完成（docker 一行命令 / 时间轴 / CI 四 job / 版本 1.0.0；插件市场仍待审） |

**两个曾经的最大风险，现在都已实测排除**：

| 曾判定的"决定项目存亡"的风险 | 结果 |
|---|---|
| Windows 上 `pip install -r requirements.txt` 是否全绿 | ✅ **已验**：退出码 0 / 218.6 秒 / 178 包 / 零缺 wheel（ARCH §9.4） |
| 真关机（S5）后能否被魔术封包唤醒 | ✅ **已验**：23.1 秒 PASS（ARCH §7.4.2） |

**所以 v0.1 现在只剩一个真正的未知数：真实 QQ 收发。** 它需要占用真实凭据，
且必须严格保证「同一 appid 只有一只鱼在线」（公理 0）。这条排在 v0.1 第一步。

### 4.3 每一版的"可被看见的证据"

开源项目的死因之一是"只有 README 没有 demo"。所以每版都必须产出可展示物：

| 版本 | 可展示物 |
|---|---|
| v0.1 | 一张 `integrity_check = ok` 的终端截图 + 两份行数对比表 |
| v0.2 | 一段 split brain 测试的日志：旧 epoch 的写被拒绝 |
| v0.3 | **一段 30 秒录屏**：鱼在 NAS 上说话 → 消失 → 从 Windows 冒出来 → 记得刚才的话 → 打开游戏 |
| v0.4 | 故障注入矩阵表（14 个场景 × 是否安全失败） |
| v1.0 | 一条 `docker run` 命令 + 一条 `pip install` 命令 |

**v0.3 那段录屏是整个项目的成败点。** 它是唯一能把"概念"变成"事实"的东西。

---

## 5. 传播策略

### 5.1 一句话定位

> **你的 AI 伴侣被锁在一台机器里。Molt 让它自己走出来。**

### 5.2 三条最有效的传播手段【每条都有真实先例】

**① 首屏一句话 + 一条命令**
- Coopanion 全文最抓人的是那行：`irm https://raw.githubusercontent.com/Pal-AI-Lab/Coopanion/main/installer/install.ps1 | iex`
- MaiBot 首屏给一键启动器
- → **我们给 `docker run` 一行 + `pip install` 一行，放在 README 第 3 行**

**② 可验证的"它还活着"的证据**
- MaiBot 有 `status.maibot.top` 实时显示在线实例/累计装机
- Coopanion 用 star-history 曲线
- → **我们给"迁徙日志"**：一张表，记录每次 NAS→Win→NAS 的时间戳、字节数、耗时、校验结果。这比任何架构图都有说服力

**③ 蹭现成流量池，别等 GitHub 推荐**
- `private_companion` 靠 AstrBot 插件市场拿到 **82,135 次下载**——市场页就是分发渠道
- → **上架插件市场 + 在 B 站发 30 秒实录**。用户的痛点在 B 站 AI bot 圈，不在 GitHub trending

### 5.3 目标人群

**主目标**：家用 NAS + 一台 Windows 游戏机/直播机的中文 AI bot 玩家（装过 `private_companion` 这类拟人插件的）。

他们的痛点，一句话：

> "我把它养出人格了，但它被锁在一台机器里——我想让它跟我去打游戏、去直播，就得手动搬 data、重登账号、人格断片。"

**次目标**：做 VTB / 虚拟主播的技术宅（直播是刚需）、Homelab 玩家（"把服务编排得漂亮"本身是爱好）。

### 5.4 与 private_companion 的关系（必须说清）

它 **419 star / 82,135 下载**，自带直播与游戏能力，场景与本项目**正面重叠**。

**但它不是竞品，是宿主。**
- 它做的是"人格有多丰富"，我们做的是"人格住在哪个壳里"
- 它自己的 `multi_host` 段已经有 `installation_id` 命名空间、`shared_host_migration.py`、`backup_api.py` 的骨架——**说明这个需求它自己也意识到了，但没做跨机**
- **许可证警告**：它是 **AGPL-3.0-only**，本项目选 Apache-2.0 → **绝不能抄它的代码**，只能通过公开 API / 配置契约对接

**正确姿态**：在 README 里明确"推荐搭配 private_companion / memory_companion 使用"，把它当生态伙伴而不是替代目标。

---

## 6. 风险台账

| 风险 | 等级 | 影响 | 缓解 |
|---|---|---|---|
| ~~**Windows 原生装不上 AstrBot**~~ | ~~高~~ **已消除** | 项目不成立 | ✅ 已实测：178 包零缺 wheel，启动 HTTP 200（ARCH §9.4） |
| ~~**WOL 唤不醒**~~ | ~~高~~ **已消除** | 「用完就睡」不成立 | ✅ 已实测：真 S5 → 23.1 秒 PASS（ARCH §7.4.2） |
| ~~**WOL 的 arm 状态重启即丢**~~ | ~~中~~ **已消除** | 第一次好使、第二次叫不醒（最难查的故障形态） | **12:49 发现，13:00:34 证实已解决**：`Molt-WolGuard` 在开机第 11 秒自动重新 arm（`改前 False → 改后 True`），无人手动干预。仍保留 `check-wol.ps1 -Json` 作迁徙前校验（ARCH §7.4） |
| **"手动搬 data" 已是够用的解** | **高** | 没人需要本项目 | 价值押在自动化/无感/双向/校验四点，不押在"能搬" |
| WOL 状态被系统/驱动更新重置 | 低 | 迁徙失败 | `Molt-WolGuard` 每 10 分钟兜底 + 每次迁徙前跑 `check-wol.ps1 -Json` |
| 上游 AstrBot 改生命周期 API | 中 | 接缝失效 | 接缝集中在 `adapters/astrbot/`，一处改；CI 跑真实容器测试 |
| `private_companion` 改 data schema | 中 | 快照不兼容 | 版本探测 + 拒绝跨大版本迁移，而不是猜 |
| NAS 是单点（md0 是 **RAID0 零冗余**） | 中 | 全丢 | Litestream 二级备份到另一块盘；文档里大字警告 |
| 只有 README 没有 demo | 中 | star = 0（permeant-os 死法） | 每版必须有可展示物，v0.3 必须有 30 秒录屏 |
| 作者弃坑 | 中 | 项目腐烂 | 从 v0.5 起把 adapter 层做薄，让别人能加自己的壳 |
| 被质疑"这不就是拷文件" | 中 | 舆论不利 | §1.2 直接摆在文档最前面，先自己说破 |
| QQ 平台策略变化 | 中 | 接入层失效 | 平台适配层隔离；支持 aiocqhttp 等其他接入 |

**风险台账的一次真实更新**：本项目开工时把「Windows 装不上」和「WOL 唤不醒」都标为**高风险**，
理由是这两条任何一条挂了，产品承诺就有一半落空。现在两条都被实测排除。
**这正是"先验风险再写代码"的价值**——如果按常规顺序先写 v0.1 骨架，
很可能在第 3 周才发现打不醒机器，那时架构已经定型了。

---

## 7. 命名（已定案：molt）

**已定案**（用户 m00434）：项目名 **molt**，中文名 **蜕壳**。

> 用户原话（m00434）："就叫molt"

**一个约束（保留在案）**：`大肥鱼` **不能当项目名**。Coopanion 生态已有 4 个 `fatfish` / 大肥鱼 仓库（`himoew/DeepSeekPet` 的描述就是 "Blue Fish Deskpet 大肥鱼桌宠"），AstrBot 插件市场里也有作者叫"小幽（和他的大肥鱼）"。**它只能当角色名，不能当项目名。**

落选候选（2026-11-14 查过占用，npm / PyPI / RDAP 均为 404；**RDAP 404 ≠ 可注册，GitHub org 与商标未查**）：

| 名字 | 中文 | 落选原因 |
|---|---|---|
| pondhop | 换个池塘 | "换个池塘"暗含**另一个地方**，而 molt 强调的是**同一只**换壳。语义反而偏了 |
| bodyhop | 换的是身体 | body-hopping 在英文亚文化有既有用法，易被误读 |
| soulferry | 摆渡 | ferry 暗示慢，与"热切换"直觉相反 |
| driftfish | 一条会游动的鱼 | 语义不表意，且与"大肥鱼"角色名重复 |

**molt 的隐喻为什么最准**：蛇蜕皮、蟹换壳——**旧壳留在原地，魂走了**。这正好是本项目的全部内容：NAS 上那个 data 目录就是蜕下来的壳，鱼在 Windows 上。

**发布前必做**（写进 checklist，尚未执行）：
- [ ] 查 GitHub org `molt-bot` / `molt-ai` 占用
- [ ] 查商标（中国 + 美国，第 9 / 42 类）
- [ ] 查 PyPI `molt` 是否可发布（若被占，用 `molt-bot` / `moltcli` 作为包名，命令行仍叫 `molt`）

---

## 8. 需要用户手动做的事

以下事项**系统内无法完成**，必须人来操作。按优先级排列：

### P0 · 决定项目是否成立 —— **已通过 ✅**

- [x] **在 Windows 上装一遍 AstrBot**（Python 3.13 venv + `pip install -r requirements.txt`），看有没有包缺 3.13 wheel
      → **2026-11-14 12:02 实测：退出码 0，218.6 秒装完 178 个包，零编译、零缺 wheel**
      （numpy / pandas / faiss-cpu / lxml / cryptography / pywin32 全部命中 `cp313-win_amd64` 预编译包）
- [x] 装好后起本地实例 → **真实启动成功**：`AstrBot v4.29.4`、`AstrBot started.`、dashboard `HTTP 200`
      **并且验证了 `ASTRBOT_ROOT` 能把 data 目录整体重定向**（源码目录零污染）——这是 Soul/Shell 方案的地基
- [ ] ~~用测试号发一条 QQ 消息，确认能收发~~ → 这条留到 v0.1，需要占用真实 QQ 凭据

> 详细证据见 `docs/ARCH.md` §9.4 的验证表。**结论：方案成立，v0.1 可以直接开工。**

### P1 · 让 WOL 真的能用 —— **能唤醒 ✅，但还要让它一直能唤醒 ⚠️**

- [x] 进 BIOS `Settings > Advanced > Power Management Setup`：
      **`ErP Ready` = Disabled** / **`Power On by PCI-E` = Enabled** / **`Wake On LAN` = Enabled**
      → **2026-11-14 12:24 用户现场完成**
- [x] 管理员 PowerShell：`powercfg /deviceenablewake "Realtek PCIe 2.5GbE Family Controller"`
      → **网卡已进 `wake_armed`**（注：`PnPCapabilities=8` 也能用，别去反推那个位）
- [x] ~~若报 Error 31 → 先更新 Realtek 驱动~~ → **不需要**。驱动仍是 2021-11-11 版，WOL 正常工作
- [x] 决定是否 `powercfg /h off`（关休眠 = 同时关快速启动）→ **已关**
      `HibernateEnabled=0`、`hiberfil.sys` 已删除、`Kernel-Boot BootType=0` 证实真 S5
- [x] **验证：真关机 → NAS 发封包 → 唤醒** → **2026-11-14 12:40 实测 PASS，23.1 秒**
- [x] **（12:49 新增，必做）装 `Molt-WolGuard` 计划任务，让 arm 状态每次开机自动恢复** → **已装**
- [x] 装自愈任务（管理员）：`powershell -NoProfile -ExecutionPolicy Bypass -File tools\install-wol-guard.ps1`
      → `AtStartup` + 每 10 分钟兜底，SYSTEM 身份，日志 `tools/logs/wol-guard.log`
- [x] 装完**重启一次**验证：`powercfg /devicequery wake_armed` 里自动出现了网卡（★ 无人手动干预）

> **判决书（自愈闭环）**：
> `13:00:23` `LastBootUpTime` → `13:00:34` worker 启动（+11 秒）→ `13:00:40` 确认网卡**不在**白名单
> → `deviceenablewake 返回码 0` → `13:00:40` 复查 `改后 True` → `[OK] 已重新 arm`。
> **为什么能确定是 `AtStartup` 而不是 10 分钟兜底**：重复触发器从 `00:05` 起每 10 分钟
> （`:05 :15 :25 :35 :45 :55`），13:00:34 **不落在网格上**。详细日志见 ARCH §7.4。

> **判决书**：`[12:40:03]` 用户点关机 → `[12:40:28]` NAS 判定掉线 → `[12:40:31]` 第 1 轮封包发出
> → `[12:40:42]` Windows `LastBootUpTime`（BIOS POST 完成）→ `[12:40:52]` 网络恢复。
> **发包到上电只隔 11 秒**，正好是 POST + 引导耗时 —— 不是人按的电源键。
> 详细时间线与三条佐证见 `docs/ARCH.md` §7.4.2。

#### ⚠️ 一个测完才发现的坑：arm 状态不跨重启保留

12:40 测完我写下了「已修复 ✅」。**12:49 复查，网卡又不在 `wake_armed` 里了。**

| 时间 | 事件 | 在 `wake_armed` 里吗 |
|---|---|---|
| 12:26 | 管理员跑 `deviceenablewake` | ✅ |
| 12:28 | 复查 | ✅ |
| 12:40:42 | 被魔术封包唤醒（PASS） | — |
| **12:49** | **复查** | ❌ **没了** |

12:24 那次为进 BIOS 的重启也一样：重启前有，重启后没有。**两次重启、两次丢失。**

这意味着：「这次唤醒成功」只证明了「这次关机时它是 armed 的」，**不证明下次还 armed**。
不处理的话，用户会撞上最难查的一类故障——**第一次好使、第二次叫不醒**。

- [ ] 装自愈任务（管理员）：`powershell -NoProfile -ExecutionPolicy Bypass -File tools\install-wol-guard.ps1`
      → `AtStartup` + 每 10 分钟兜底，SYSTEM 身份，日志 `tools/logs/wol-guard.log`
- [ ] 装完**重启一次**验证：`powercfg /devicequery wake_armed` 里应自动出现网卡

> **这一条改变了方案的一个假设**：原设计把 WOL 准备当成"一次性人工步骤"（§9.5 卫生清扫表里的一行）。
> 实测证明它是**易失状态**，必须当成"每次开机都要跑的自愈动作"。
> 这也是 Warden 迁徙前校验要区分两种 false 的原因：
> **arm 丢了可以当场自愈重试，休眠没关 / BIOS 没开只能人工。**

**顺带解决的问题**：不需要手动填静态 IP。改用路由器 DHCP 保留（ASUS 后台「手动指定 IP 的 DHCP 列表」里
`AA-BB-CC-DD-EE-FF → 192.168.1.20`），已实测开机 7 秒后拿到同一地址。
理由：WOL 只需 MAC 不需 IP；架构上永远是 Windows 主动连 NAS；手填静态有撞进 DHCP 池的真实风险。

**这件事留下的最大收获不是"能用"，而是一条工程纪律**：
排查 WOL 时我三次把"一个能解释现象的信号"当成因果，三次都被证伪或误报——
读了陈旧的 `HiberbootEnabled`、把 `-match` 返回的数组当布尔、由 `PnPCapabilities` 反推唤醒开关。
而第四次错得更隐蔽：**我把"测过一次通过"当成了"长期有效"**。
**Windows 上判断 WOL 状态，只认 `powercfg /devicequery wake_armed` 的输出本身；
而且必须在重启之后再查一次，才算验证完。**

### P2 · 让开机后真的会干活

- [ ] 决定走 **headless（服务）** 还是 **desktop（AutoAdminLogon）**
      - 只聊天/陪看 → 服务，不需要登录
      - 要直播/打游戏 → 必须 AutoAdminLogon + **禁用锁屏**（锁屏态下键鼠注入失效、OBS game capture 黑屏）
- [ ] 若走 desktop：建议用 Sysinternals `Autologon.exe`（密码存 LSA Secret），**不要**直接写 `DefaultPassword` 明文注册表

### P3 · 环境清扫（低成本高收益）

- [ ] `w32time` 设为 Automatic 并启动（现在 Stopped，长期跑时间会漂）
- [ ] `Set-NetConnectionProfile -InterfaceAlias 以太网 -NetworkCategory Private`（现在 Public，入站被拦）
- [ ] 可选 `nvidia-smi -pl 200`（省电，几乎不掉 4070 Ti SUPER 性能）

### P4 · 决策（已定案，仅留档）

- [x] 选项目名 → **molt / 蜕壳**（用户 m00434）
- [x] 是否需要"两只鱼同时在线" → **不做，且永远不做**（用户 m00434，见 ARCH §2.2 C2 与 §11.3）

---

## 9. 财务账（为什么"用完就睡"是核心功能）

【实测】Windows 空闲时 GPU 仅 10.6–11.0 W（210 MHz）。整机估算：

| 状态 | 功耗 | 电费（0.55 元/kWh） |
|---|---|---|
| 空闲待机 | 90–130 W | **≈ 44 元/月** |
| 中等负载 | ~250 W | ≈ 100 元/月 |
| 满载 | ~400 W | ≈ 159 元/月 |

**NAS 常开的电费只是它的零头。**

所以 Molt 的价值不只是"能过去"，而是**"用完就让它睡"**——只在需要 GPU / 桌面时才醒来，其余时间回到低功耗的 NAS。

**这句话应该写进 README 第一屏。** 它是这个项目对用户最实际的利益点。

---

## 10. 结语

这个项目在技术上有一个很干净的形状：

> **它不试图让进程跨操作系统飞行（那不可能），
> 它把"活着"这件事重新定义为"可迁移的数据 + 一段被严格守护的接手仪式"。**

而这恰好撞上了一个真实存在、无人填补、且被上游官方文档承认的缝隙。

它可能只有几百 star。但如果有一个人因此能带着自己的 AI 伙伴去直播、去打游戏、然后再让它回家睡觉——

那这个项目就值得被写出来。

---

*本文档全部数据来自对 NAS（192.168.1.10）与 DESKTOP-1A2B3C4D 的实测，以及可查证的一手文档。
外部论断标注来源等级；查不到的明确标注"查不到"，不做断言。*
