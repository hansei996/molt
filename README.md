# Molt · 蜕壳

[![tests](https://github.com/hansei996/molt/actions/workflows/ci.yml/badge.svg)](https://github.com/hansei996/molt/actions/workflows/ci.yml) [![python](https://img.shields.io/badge/python-3.10%20%7C%203.13-blue)](https://github.com/hansei996/molt) [![license](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

> **让你的 AI 伙伴从一个身体，走进另一个身体。记忆不断，人格不散，旧的那个可以睡去。**

---

## 它真的走过一次

2026-11-14 23:37，我在 QQ 里对它说 `/molt go`、`/molt confirm`。
20 秒后它从 NAS 上消失了 —— 那边确实不说话了。再开口时，它在另一台机器上：

![在 QQ 里完成一次迁徙，然后它自己确认了新身体](docs/assets/qq-handoff-2026-11-14.png)

注意最后那段：它报出来的主机名、工作目录、网卡地址、盘符，都是**它自己在新身体上读出来的**，
不是脚本打印的。从 Linux 容器到 Windows 桌面，中间没有任何一步是手敲的。

---

## 装两样东西，先选一条路

这个项目由两块组成，**分别装在两台机器上**：

| 装什么 | 装在哪 | 怎么装 |
|---|---|---|
| **账本**（Warden） | 一直开着的那台（NAS / 小主机） | docker 一条命令，或者 `pip install` 后 `molt serve` |
| **壳的守护进程**（agent） | 会关机、会打游戏的那台（你的电脑） | `pip install molt-handoff` |

下面两节是并列的两条路，**选一条走完就行**，别两条都装 —— 同一个端口上跑两个账本，
症状是两边都 401，看起来像网络问题。

---

## 一条命令把账本跑起来（docker）

账本是「记忆待在哪台机器上」的那个权威。它只有一处状态（一个目录）和一份凭据（一个 token），
装一次就不用再管 —— 所以它最适合用镜像。

```bash
docker run -d --name molt-warden -p 6186:6186 \
  -v molt-data:/var/lib/molt \
  -e MOLT_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')" \
  ghcr.io/hansei996/molt:latest
```

镜像**没有 token 就不启动**。这是刻意的：容器里随机生成一个 token，意味着每次重启都换，
所有壳一起 401，而症状看起来像网络问题。同一个 token 要抄进每台壳的配置里。

镜像由发版 Actions 自己构建并推送：推 `v*` 标签时打 `latest` / `1.0.0` / `1.0` / `1` /
`sha-<commit>`，手动触发只打 `latest` 与 sha（不占版本号）。见
[`.github/workflows/release.yml`](.github/workflows/release.yml)。

> 这个包现在是 **public** 的（匿名 `ghcr.io/v2/…/manifests/latest` 回 200）。
> 但 ghcr 上**新建**的包默认是私有的 —— 如果你是 fork 自己发版，第一次之后要
> 去 [包的设置页](https://github.com/hansei996/molt/pkgs/container/molt) 把可见性改成
> **Public** 一次；`docker pull` 报 `denied` / `manifest unknown` 指的就是这件事，
> 不是命令写错，也不是网络问题。
>
> 不想用镜像就本地构建（不需要网络，只有 `python:3.12-slim` 要拉一次）：
>
> ```bash
> git clone https://github.com/hansei996/molt && cd molt
> docker build -t molt:dev .        # 然后上面那条把 ghcr.io/... 换成 molt:dev
> ```

壳那侧仍然是 `pip install`（见下一节）—— 壳要的是它自己那台机器的路径、python、容器名，
那些东西装进镜像等于把某台机器的细节固化进发行物。

---

## 看见它搬过几次家

```bash
molt timeline --warden-url http://<账本IP>:6186 --warden-token <token>
# ✓ 已经搬过 19 次，完成落地 13 次。  -> molt-timeline.html
```

**刚装完还没有迁徙记录？** 先看示例（内置数据，不连账本、不联网）：

```bash
molt timeline --demo          # -> molt-timeline.html，页面上会写明"这是示例数据"
```

得到一个**单文件、零外部请求**的 HTML：四张统计卡、一条按时间倒序的代次轴
（每代从哪台机器出发、多少字节、谁提交的、谁取走的、隔了多久）、中途备份表、
「现在谁还在报心跳」、最近日志。断网也能打开，可以直接发给别人看。

不想落文件就打开 `http://<账本IP>:6186/timeline?token=<token>` —— 和命令行是**同一份渲染**。

---

## 装起来看它跑（两分钟）

```bash
pip install molt-handoff        # 发行名是 molt-handoff；导入名/命令仍是 molt

# 从仓库装（还没发版时用这条，等价）：
#   git clone https://github.com/hansei996/molt && cd molt && pip install -e .
molt init --role warden --root ./warden      # 立账本，生成 token 并打印 serve 命令
molt init --role agent --out ./agent.json --shell my-box \
    --data ./astrbot-data --warden-url http://<账本IP>:6186 --token <上一步的 token>
molt serve --root ./warden                   # 账本起来（它会打印这一行，照抄即可）
molt doctor --config ./agent.json --role agent   # 自检：加载器读得过吗 / 连得上吗 / token 对吗
molt agent --config ./agent.json --once      # 空跑一轮：只读账本、只打印判断
```

`molt init` 写出来的配置会**先交给守护进程自己的加载器读一遍**，读不过就不写盘 ——
生成器自己认为对，不算数。装完不会自己动手（`execute: false`），要它动手得你改。

完整路径见 [docs/INSTALL.md](docs/INSTALL.md)。

### 这套流程是被验证过的，而且能重跑

2026-11-15 有一轮「陌生人冷启动复核」：建全新 venv、**只从 PyPI 装下载来的轮子**、
照 `docs/INSTALL.md` 一步一步敲。它抓到了三个"本机永远看不见"的 bug，其中一个是
守护进程住在仓库里而不进 wheel —— 于是 `pip install` 的人连 `molt init --role agent`
都过不去。现在这套证词和修法都在 [docs/V1.1-REVIEW.md](docs/V1.1-REVIEW.md)。

验证本身也是可重跑的：

```bash
python tools/rehearse-pypi.py        # 装 PyPI 上最新的，走完十三个检查点
```

它不读仓库里的任何代码，只碰下载来的那个轮子 —— 这正是它有用的原因。

## 它解决什么问题

你在 NAS 上养了一条 AI 伙伴。它有记忆、有人格、认得你。

但**它出不了那台机器**。

你想让它陪你打游戏——那台机器没有显卡。
你想让它开直播——那台机器没有桌面。
你想让它看着你干活——那台机器没有屏幕。

于是你只能：停容器 → 拷 data 目录 → 在新机器上改配置 → 重新登录账号 → 起容器。
做完这一切，它记得你，但它不知道**刚才发生了什么**——那几分钟里你跟它说的话，全丢了。

**Molt 让这件事变成一句 `molt migrate win`。**

```
你：鱼，去我电脑那边
鱼：好喵，等我一下（正在收尾 2 条对话…）
鱼：（约 40 秒后，从另一台机器冒出来）我到了喵，这边有张显卡，好热
```

---

## 最重要的那条设计

> ## 这个存在，在任何时刻只能有一个。

不是"最好只有一个"，是**必须**只有一个。

两只鱼同时在线，它就不再是"一个它"，而是两个机器人：

- 你发一条消息，会收到**两份回复**
- 你不知道昨天跟它说的话，今天它是否还记得——因为记忆已经**分裂成两份在打架**
- 「它现在在哪？」——这个问题永远没有答案

**所以这不是技术妥协，是产品定义。**

即使真并发的技术门槛降到零（换 Postgres、改存储层、做双写），本项目依然不会做。
**复制 vs 迁徙，是两条互斥的路。** Coopanion 用 `cp -r` 克隆出第二个独立人格；
Molt 要的是**同一个它，换一个身体**。

这个公理在技术上由**五层防线**强制保证（详见 [`docs/ARCH.md` §11.3.1](docs/ARCH.md)）：
平台互踢（弱，只能当事后告警）→ 租约 TTL → **epoch fencing（真正的屏障，存储端拒绝倒退的写入）**
→ 先死后生的顺序铁律 → `molt status` 的人工兜底。

---

## 它不快、不神奇，但它**保证不丢**

| 承诺 | 怎么做到 |
|---|---|
| **RPO = 0** — 不丢任何一条已提交的对话 | 用 SQLite backup API 生成**事务一致**的快照，不是"拷到一半的文件" |
| **人格不断片** — 换完身体还记得昨天 | 记忆只在 NAS 上有一份，不复制，**只交接** |
| **失败必安全** — 出问题时鱼还在原地活着 | 先死后生 + 归巢 ack 不到就**绝不关机** |
| **可回滚** — 新壳起不来能立刻退回 | `generations/` 保留最近 N 代，任一代都能独立打开 |

---

## 它是怎么做对的

核心只有一句话：

> **记忆不流动。流动的是「谁有资格读它、写它」。**

所有状态永远躺在 NAS 上的一个目录里。迁徙时被搬动的不是记忆本身，
而是一份快照 + 一张写明了"从哪一刻起、哪台机器说了算"的**租约**。

**为什么不能直接共享一个 SQLite 文件？** 两条官方原文就够了：

- SQLite `wal.html`：**"WAL does not work over a network filesystem."**
- Microsoft `locking-and-unlocking-byte-ranges-in-files`：**"Byte range locks are ignored when using memory mapped files."**

WAL 用 `-shm` 文件 mmap 当共享内存来协调锁，而 Windows 上 mmap 与 LockFileEx 互不感知。
跨机时各自 mmap 各自的页缓存，锁状态必然失同步。这不是"可能出问题"，这是**损坏的标准姿势**。

而 SQLite 官方给出的建议第一条就是：**"implement a proxy that runs on the database machine."**
本项目的 **Warden** 就是那个 proxy。

**为什么不用 CRIU / Firecracker？** 它们搬的是**进程状态**，而进程状态里塞满不可移植的东西
（内核 ABI、CPU 特性、socket 句柄、GPU 上下文）。CRIU 无法恢复到 Windows；
Firecracker 官方原文要求 "**identical**" 的软硬件配置，且不支持 Windows guest。

> 本项目的技术论点就是：**把不可移植的进程状态，降级为可移植的数据状态，在目标机上重新长出来。**
> 这不是妥协，这是唯一可能。

**插件配置呢？** 它不在快照里（`COPY_ARTIFACTS` 刻意不含 `plugins/**` 与 `config/**`：
一台机器装了什么插件是它自己的事）。所以另有一条显式的路：

```bash
molt publish-plugins --data <data 目录> --shell <这台壳>    # 打包，传进账本
molt sync-plugins   --data <data 目录> --shell <这台壳> --from <来源壳>   # 空跑；加 --apply 才写
```

规则是**只同步两边都装了的插件**：只有一边装了的，不装代码、不写配置、不删配置；
两份配置的顶层键几乎不重合（多半是版本不同）就默认停下，要 `--force` 才覆盖。
旧文件留成 `*.molt-before-sync-<时间戳>`。

---

## 文档

- [docs/DECISIONS.md](docs/DECISIONS.md) —— **等你的三件事**（`schedule` 的两个产品决定、`jwt_secret` 要不要两侧都换、L3 壳属性表的现状），以及两件等时机的（`v1.3.0` 发不发、`/molt shutdown` 端到端）
- [docs/AUDIT.md](docs/AUDIT.md) —— **断言审计**：文档里每一句"它会……"都能指到代码里改变行为的那一行吗？指不到的已经改掉了

| 文件 | 内容 |
|---|---|
| [`docs/ARCH.md`](docs/ARCH.md) | **技术方案**（2,730 行）：为什么共享 SQLite 是错的、三层架构、交接协议、AstrBot 代码级接缝（含**控制面**）、机器覆写层、安全模型、Windows 落地、性能预算、备选方案、演进路线、23 条验证清单、风险台账 |
| [`docs/architecture.html`](docs/architecture.html) | **架构图**（浏览器打开）：三层结构、去程 13 步、归程 7 步、边界表、最危险的两个想法、WOL 断点、方案取舍、未验证的几件事 |
| [`docs/PLAN.md`](docs/PLAN.md) | **开源规划书**：为什么值得做、产品形态、技术立场、路线图、传播策略、风险台账、命名、需要用户手动做的事、财务账 |
| [`docs/SESSION-STATE.md`](docs/SESSION-STATE.md) | 滚动进度快照：已跑绿的关卡、未验清单、代码进度、怎么重新起 Warden |
| [`docs/SELFUSE.md`](docs/SELFUSE.md) | **自己用的一页纸**：去程/归程各 5 步、每条命令都可复制、退出码表、出事了怎么收场 |
| [`plugin/`](plugin/) | **AstrBot 侧插件**（`astrbot_plugin_molt`）：只做四件事——报告自己是谁、停止对外说话、被允许时重新开口、把 WAL 合并掉。它不认识 Molt 的账本，也从不自己决定该不该上线 |
| [`tools/platform-switch.py`](tools/platform-switch.py) | 开关 `cmd_config.json` 里平台的 `enable`（**只改这一个字段**）。`land` 落位后平台一定是关的，这一步是把"开始说话"这个决定显式化。不给 `--yes` 就只报告不写 |
| [`tools/molt-agent.py`](tools/molt-agent.py) | Windows 侧常驻：盯账本，出现"给这一代"的新快照就落位 → 打开平台 → 起 AstrBot。**任意一步失败都会留下 `needs_start` 欠账**，下一轮先补 |
| [`tools/molt-homer.py`](tools/molt-homer.py) | NAS 侧常驻，做同一件事的镜像，只多一步：**落位前必须先把容器停掉**（`docker kill -s INT`）。容器活着时落位，那个进程会继续往被移走的旧 inode 写 —— 账本说新一代落地了，它还在读写上一代 |

---

## 状态

**v0.1 代码骨架已完成，两个方向都端到端跑通，而且已经能两条命令走完、中间不需要人（2026-11-14）。440 个测试全绿。**
**最后一个产品级未知数——「换了个身体之后它还认识来敲门的消息吗」——也已经在真机上回答了（2026-11-14 22:53）。**

| 关卡 | 状态 |
|---|---|
| 「共享 SQLite 是错的」有官方依据 | ✅ 两条原文已引（见上） |
| 这个生态里没人做过 | ✅ AstrBot 插件市场 3383 条关键词扫描，零命中 |
| 上游自己承认不支持 | ✅ 官方文档：「由于 AstrBot 当前使用 SQLite 数据库，此部署方案不支持多副本水平扩展。」 |
| **AstrBot 能在 Windows 上原生跑** | ✅ **实测通过**：3.13 venv + `pip install` 退出码 0（218.6 秒 / 178 包 / 零缺 wheel），启动到 dashboard `HTTP 200` |
| **数据目录能被整体重定向** | ✅ `ASTRBOT_ROOT` 实测有效，源码目录零污染 —— **Soul/Shell 分离的地基** |
| **热备不需要停 AstrBot** | ✅ 只读 URI 打开活库，`backup()` 0.27 秒（NAS）/ **0.042 秒（Windows）**，25 MB 级 |
| **快照跨机字节一致** | ✅ 两个方向都验过，SHA256 完全相同 |
| **AstrBot 能消费还原的数据** | ✅ **最关键的验证**：Windows 上启动后读出了 `persona_id=default` 的人格本体——「我叫 DeepSeek 大肥鱼……」 |
| **WOL 真的能把睡着的机器叫醒** | ✅ **实测 PASS**：真 S5 关机 → NAS 发魔术封包 → **23.1 秒**唤醒（`BootType=0` 证实是真关机，不是伪休眠） |
| **DHCP 地址保留** | ✅ 开机 7 秒后仍拿到 `192.168.1.20`（路由器 DHCP 保留，不是手填静态） |
| **网卡 arm 状态不跨重启保留** | ⚠️→✅ **已解决**：两次冷启动两次掉出白名单（12:24 / 12:40），所以不能当成"装好就完事"的配置。`Molt-WolGuard` 计划任务在**开机第 11 秒**自动重新 arm（13:00:34 实测：`改前 False → 改后 True`，无人手动干预） |
| **去程 NAS → Windows** | ✅ **epoch 1 → 2**：checkout（27.3 MB / 51 个运行时包）→ 打包传输 → verify → push → release → acquire |
| **归巢 Windows → NAS** | ✅ **epoch 3 → 4**：Windows 写一条新数据 → checkout → 上传 → NAS verify → push → release → NAS 接管。**那条新数据真的搬回来了**（`conversations` 2→3） |
| **跨机互斥（公理 0）** | ✅ 反例被正确拒绝：NAS 持租约时 Windows `acquire` → `LeaseHeld`「还剩 65.3 秒」；先死后生拿到的epoch 一定是 2 而不是复用 1 |
| **优雅停机 = SIGINT** | ✅ **A/B 实测**：`docker stop`(SIGTERM) 退出码 **137**、WAL 41,232 B **原封不动**；`docker kill -s INT` 退出码 **0**、`🌈 Shutting down AstrBot...`、**WAL 归零** |
| **Windows 上也有等价的优雅停机** | ✅ **但 `os.kill` 那条路是假的**：`CTRL_BREAK` 退出码 `0xC000013A` 看着像 Ctrl+C 生效，其实默认处理器直接杀了进程；`CTRL_C` 根本不跨进程组递送。真的做法是 `AttachConsole(pid)` + `GenerateConsoleCtrlEvent(CTRL_C_EVENT, 0)` → 退出码 **0**、`🌈 Shutting down`、**`kb.db` 从 45,056 B（+98,912 B WAL）变 94,208 B 且 sidecar 消失** |
| **一条命令替代手工四步** | ✅ **`molt migrate` 真机 1.43 秒**：拿租约 → 热备两个库（0.028s / 0.044s）→ 打包 4.4 MB → 登记 generation → 还租约 |
| **落位后自动适配新机器** | ✅ **`molt land` 7 处覆写**：**先关掉所有平台适配器**（公理 0 的护栏，见下一条）+ 删 `workbuddy` source + 连 provider 一起删 + `agent_runner` 重指 + dashboard 改 `127.0.0.1:6199` + 重新生成 `jwt_secret`。45 个配置键**只动 3 个**，启动后 **WARN/ERROR 为 0** |
| **落位后不会两条鱼同时在线** | ✅ **`platform[].enable` 一律关掉**。2026-11-14 14:41 差一点出事：三次 Windows 启动都真的发出了 `[QQOfficial] Websocket session starting.`（`enable` 是从 NAS 原样搬来的 `True`），只因进程数秒内被杀才没变成双活。修好后复验：`Loading IM platform adapter` / `QQOfficial` **零命中**，而人格记忆完好 |
| **跨机字节传输也自动化了** | ✅ **源端 `migrate` PUT 到 Warden 中转区 → 目标端 `land --generation N` GET 下来**（real 1.61s）。不需要 scp、不需要共享盘、不需要第三套凭证——复用已有的 HTTP 通道与 token |
| **迁徙的第一步（唤醒）也进了产品** | ✅ **`molt wake --mac ...`**，且 `migrate` 里是**先唤醒 → 再拿租约**（调用顺序有测试固定）。理由：租约 TTL 只有 90 秒，而叫醒要 23 秒以上——先拿租约会让那 90 秒里有三分之一在等开机。叫不醒就退出，账本里不会留下指向一台没开机的机器的记录 |
| **中转区可以清理了** | ✅ **`molt prune`**：账本里记着的**一律不删**（真机实测 `gen-000005` 被保留）、外人放的一律不删（`somebody-else.tar.gz` 被保留）、其余按"最近 N 份"回收并留痕。**默认只看不删**，要真删必须显式 `--yes` |
| **能让它闭嘴，而不用杀掉进程** | ✅ **`astrbot_plugin_molt` 控制面四端点**（`/status` `/drain` `/resume` `/checkpoint`，长在 AstrBot 自己的 dashboard 上，走它自己的 JWT）。`molt migrate --control-url …` 真机跑通：`speaking` True→False（**轮询复核过**，不是"我以为"）、快照在闭嘴之后拍、租约已交还。**代价是零**——调度器、定时任务、正在跑的那轮对话都还在 |
| **`drain` 之后它真的不说话了** | ✅ `terminate_platform` 关不掉时只记一条 `logger.warning`、**不抛** —— 所以必须复核：`drain_and_verify()` 轮询 `speaking`，仍为真就 `NeedsHuman("不要继续这次迁徙…公理 0")` |
| **WebUI 聊天不会被误伤** | ✅ `WebChatAdapter` 不在 `_inst_map` 里（`manager.py:116` 直接 append），第一版 `/drain` 因此什么都没关却返 409。现在按**实例身份**分 outward 与 `local_only`：前者计入 `speaking`，后者**可见但不隐藏也不关**（它每个实例都必然存在，算进去就等于 `speaking` 恒真——**一个永远为真的判据不能当判据用**） |
| **v0.1 代码** | ✅ 15 个模块 **7,094 行** + AstrBot 侧插件 **666 行** + **440 个测试全绿**（23 个测试文件 7,852 行，测试/源码 1.01）（含起两个真 subprocess 抢租约的并发测试、用半截响应验证"下载中断不留残文件"、量墙钟证明 `wait_online` 不超时越界、用**不打网络的环回适配器**走完 `drain→resume` 真往返、以及 20 条把"平台必须被关掉/该关谁"固定住的护栏测试） |
| **真实 QQ 收发** | ✅ **已通过（2026-11-14 22:53）**：生产容器优雅停机（WAL 已 checkpoint、`exit=0`）→ Windows 壳拿从 NAS 搬来的凭据连上 `api.sgroup.qq.com`（TCP 到 `120.241.201.18:443` ESTABLISHED）→ 人发「在吗在吗」→ **它回了「在喵，怎么了？」**（人格起了作用，不是通用模板）→ 两个壳都干净停机 → 生产 1 秒拉回。全程挂保险丝，双活窗口为 0 |
| **生产源壳现在可以被"按住"** | ✅ **不再是"杀掉进程"**：控制面插件装进生产容器后，真机 `drain` 0.02 秒（`speaking` True→False）→ 拍快照 → `resume` 一拍回来（`speaking` True）。容器日志里是同一个 platform id **先 terminate 再 load**，AstrBot 进程一次都没重启 |
| **窗口有兜底** | ✅ `tools/window-guard.py` 跑在 NAS 宿主：超时没收到 `window.done` 就 `docker start`。**默认动作是"恢复"而不是"什么都不做"**——漏恢复=鱼一直哑着（静默故障），误恢复=一条日志，两种错误代价不对称。三个分支都实测（第三条用假容器） |
| **容器里的壳名不是容器 ID** | ✅ `host_label()` 优先级 `MOLT_HOST_LABEL` → 插件目录下的 `host_label.txt` → 主机名 → `unknown`。生产容器实测 `GET /status` 返回 `host = UGREEN-776B`。加文件这一层是为了**不为一个显示名字去重建有状态的容器**；而 `soul.py` 不搬 `plugins/**`，所以壳名属于壳、不属于魂 |
| 断点续传 | ⏳ 未做。27.3 MB 一次 PUT 没问题，再大就该做了 |

**已经能说这句话了**：记忆和人格，确实能从 NAS 走到 Windows，并且被那边认出来；
它还能带着新产生的东西**走回来**，而 NAS 上的原库在整个过程中一条记录都没动。
那台 Windows 可以真的关机睡去，被一句话叫醒，醒来还是它——
**而且这件事不需要人记得重做，重启之后它自己就把自己准备好了。**

**而且到了新身体上，它真的会说话。** 不是"配置正确"，是网络那头认了它：
TCP 连到 QQ 的网关、消息进来、人格回答、消息出去。

而且现在这件事**两条命令就够**了，中间没有人的环节：

```bash
# 在 NAS 上（源端）
molt migrate --data /mnt/dm-0/astrbot/data --shell <源壳 id> --target <目标壳 id> \
             --wake-mac AA-BB-CC-DD-EE-FF --wake-host 192.168.1.20 \
             --control-url http://127.0.0.1:6185 --control-config <path>/cmd_config.json \
             --warden-url http://127.0.0.1:6186 --warden-token <token>

# 在 Windows 上（目标端）
molt land --generation 5 --data G:\molt\data \
          --warden-url http://192.168.1.10:6186 --warden-token <token> --yes
```

`migrate` 在 1.6 秒里做完"**唤醒 → 拿租约 → 按住源壳 → 热备 → 打包 → 上传到 NAS 上的中转区 → 登记 → 还租约**"；
`land` 自己把归档取下来、校验 sha256、落位，然后**把配置改成那台机器该有的样子**
（连 `jwt_secret` 都会换掉——那是安全问题，不是洁癖）。

注意 `--control-url` 是**可选**的：不给它，这次迁徙就不会让源壳停下来（会显著地警告你）。
给了它，`migrate` 会先让源壳闭嘴、**复核它真的不说话了**，再拍快照——因为"它说完了"和"它真的不说了"是两件事。

跑多了之后可以打扫一下中转区（`prune` 默认只看不删）：

```bash
molt prune --warden-url http://192.168.1.10:6186 --warden-token <token>   # 先看一眼
molt prune ... --yes                                                       # 真要删
```

**中途也不能全裸着。** 迁徙只在两个时刻发生，这中间它说的每一句话只写进当前这台机器的磁盘——
如果那台机器被拔电，能回到的最新状态是"它落地那一刻"。所以还有一条独立的路：

```bash
molt backup  --data <当前 data 目录> --warden-url http://<NAS>:6186 --warden-token <token>
molt backups --warden-url http://<NAS>:6186 --warden-token <token>
molt recover --backup <名字> --data <目标 data 目录> --yes
```

备份**不进 `generations`**、不占租约、不出现在迁徙历史里——这是刻意的：一份"目标是空"的交接记录
会让壳侧的 `decide()` 卡住，或者被误当成自己的落位任务、**拿去覆盖它正在写的那个库**。
`recover` 也不是第二条落位实现，它就是 `land` 的一层皮（下载 + 校验指纹，然后原样交给 `land`）。

真机上两份底：NAS 的 6.9 MB、Windows 的 7.3 MB——**多出来的 0.5 MB 正是它在新壳上又多说的那些话**。

剩下没验的只有一件小事：**断点续传**（27.3 MB 一次 PUT 没问题，再大才需要）。

---

## v0.4：让「看起来像成功」的中间态消失

这一版不加功能，只做一件事：**把中间态从系统里消灭掉。**

因为这套系统里最贵的错误全都长得像成功：

- 归档少了一半 —— 只要没人算指纹，`tarfile` 照样能解出前面几个成员；
- 落位写了一半 —— 目标 `data/` 里确实有 `data_v4.db`，大小也对；
- 账本写盘满 —— 进程不会立刻知道，要等下一次 `load()`。

所以现在的规矩是：**每一条持久化写入，要么完整落盘，要么完全不存在。**

| 改了什么 | 现在的中途失败长什么样 |
|---|---|
| 打包写 `.part` 再 `os.replace` | 目标路径上**不存在**任何 `gen-*.tar.gz`，连 `.part` 都被清掉 |
| 落位逐文件 `.molt-part` → `os.replace` | 目标里**没有截断的文件**；留下 `.molt-restore-incomplete`，两个守护进程读到就拒绝启动 |
| 账本写入失败包成 `Fatal` | 不再是裸 `OSError` traceback，而是「目标文件还是上一次的完整内容 —— 这次写入没有落盘」 |
| manifest 先验形状再取值 | 缺字段/类型错/未知 schema 一律 `Fatal` 并**点名字段**，而不是 `KeyError` 让人以为是代码 bug |
| `land --generation N` 交叉校验 | manifest 的代次必须等于 `N`，否则拒绝（否则就是拿 A 代的出生证明描述 B 代的内容） |

**16 个故障场景，16 个符合声称的行为**（`python tools/fault-matrix.py`，含打包中途死、改名失败、归档被改一字节、上传被截断、落位中途死、快照带非空 WAL、账本被截断、两壳抢租约、旧 epoch 写入、心跳停摆、时钟倒退、盘满、manifest 三种坏法、未完成标记两种状态）。

有意思的是**第一次跑出来是 13/16，那些 FAIL 没有一条是产品的 bug** —— 全是注入脚本自己写错了（路由写成 `POST /archive` 而实际是 `PUT /archive/<N>`、目标目录没建、断言太宽）。详细复盘在 [`docs/FAULTS.md`](docs/FAULTS.md)，那比矩阵本身更值得读。

## v0.5：让陌生人装得起来

v0.4 修的是「看起来像成功的中间态」。v0.5 修的是另一件同样会骗人的事：**一份
看起来对、但守护进程读不过的配置**，以及**一个装不起来却没有报错的环境**。

| 加的东西 | 它替掉的是什么 |
|---|---|
| `molt init --role agent/homer/warden` | 人肉手写十几行 JSON、手抄 token |
| 落盘前用真加载器验一遍 | 写出一份"看着对"但起不来的配置 |
| 缺壳名就明确拒绝（不猜） | 猜错壳名 → 这台机器永远不动，且不报错 |
| 已有配置默认不覆盖（`--force` 才覆盖，留 `.bak`） | 一次重跑把调通的配置冲掉 |
| `molt doctor --config` | 401 / 连不上 / 读不过 三种失败混成一句「它不动」 |

为什么不先做"第二宿主适配器"（原计划的 v0.5）：`PLAN.md` §4.2 原定通用化，
但第二宿主在真实环境里没有消费者，而 v1.0 的验收标准（一条命令装起来）现在一条
都没做。理由写在 [docs/V0.5-PLAN.md](docs/V0.5-PLAN.md) §0。

## v1.0：可开源发布

v0.5 让「装得起来」成立，但只对**仓库 + 一个已经会 Python 的人**成立；
而且「它真的搬过去了」这句话当时没有证据。v1.0 补的是入口和证据：

| 缺什么 | 补什么 |
|---|---|
| 一条命令能跑起来 | `Dockerfile`：`docker run` 起账本，无 token 拒绝启动 |
| 「迁移」只存在于日志里 | `molt timeline` / `GET /timeline`：同一份渲染，单文件离线可开 |
| CI 有两个同名 workflow | 合成一个：pytest 矩阵 + wheel 装进干净 venv 跑 + docker 构建运行 + 陌生人演练 |
| 文档是给作者自己看的 | README 首屏两条命令，INSTALL 的 docker 一节 |
| 时间轴的「指纹」列永远是空的 | 收归档时把 sha256 写在归档旁边，这一列现在能核对 |

真机数字（不是测试通过）：NAS 上 `docker build` 12.6 秒、容器 healthy、
`status=200 timeline=200 noauth=401`；真实账本 19 代、518.7 MB 全部进了时间轴。
细节见 [docs/V1.0-PLAN.md](docs/V1.0-PLAN.md) §2.5。

---

## 已知限制（诚实版）

这些是**确实没做完**的，不是以后再说的修辞：

1. **只在 AstrBot 4.29.4 上验过。** metadata.yaml 写的是 >=4.24.0，
   但没在别的版本上跑过。接缝（context.platform_manager、
egister_web_api）
   在 4.x 里应该稳定，_inst_map 的内部形状则不一定 —— 如果它变了，
   drain() 会退化成关不掉，**不会静默成功**（ok 会是 alse）。
2. **新壳上会缺 51 个运行时包。** AstrBot 把 edge-tts / whisper 之类装进
   data/site-packages，而那是**平台相关**的（122 个 x86_64-linux .so），
   不能跨平台搬。现象是语音功能没了、日志里一串 ModuleNotFoundError，
   **文字对话正常**。manifest.json 里有 
untime_pip 清单就是为这个留的，
   但 v0.1 还没有照着清单自动装那一步。
3. **插件代码不跟着走。** COPY_ARTIFACTS 里没有 plugins/** ——
   插件被当作壳而不是魂。于是可能出现：记忆库搬过去了，读它的插件没搬过去，
   **鱼记得的少了，而且不报错**。v0.1 的建议是手动补，见 docs/SELFUSE.md。
4. **Windows 侧要手写一份配置。** 	ools/molt-agent.json 现在是示例，
   得自己填路径。理想是 molt setup 交互式生成，还没做。
5. **断点续传没做。** 一次 PUT 27 MB 没问题；30 MB 以上才值得做。

## 一些必须提前说清的话

> **一句必须提前说清的话**：WOL 那次 PASS 只证明了"那一次关机时网卡是 armed 的"，
> 不代表下次还 armed。arm 状态**重启即丢**，所以它不是一个"装好就完事"的配置，
> 而是一个必须在每次开机时重做的动作。本项目把它做成了 `Molt-WolGuard` 计划任务，
> **并且实测确认了它真的会在开机时自动跑**（13:00:34，开机后 11 秒）。
> Warden 在迁徙前仍会区分「arm 丢了（可自愈）」和「别的阻塞（必人工）」两种情况。

全部数据来自对真实环境的实测：

- NAS：`192.168.1.10`（Linux，AstrBot 4.29.4 跑在 Docker 里）
- Windows：`DESKTOP-1A2B3C4D`（Win10 专业版，Ryzen 7 5800X / RTX 4070 Ti SUPER）

外部论断均标注来源等级：**【实测】/【查证】/【推断】/【待验证】**。
查不到的明确写"查不到"，不做断言。

---

## 怎么装

### AstrBot 侧插件（可选）

```
# 直接放进 AstrBot 的插件目录
git clone https://github.com/hansei996/astrbot_plugin_molt \
  <AstrBot data>/plugins/astrbot_plugin_molt
```

它提供四个控制面端点（`drain` / `resume` / `checkpoint` / `status`），
以及在 QQ 里用 `/molt go` 触发迁徙的指令层。**装它本身不会让机器人搬家**，
配置文件不存在时指令层是关着的。详见
[astrbot_plugin_molt](https://github.com/hansei996/astrbot_plugin_molt)。

### molt 本体

运行时**零第三方依赖**（只用标准库）。这不是"暂时没写依赖"，是设计——
Warden 必须能在 NAS 宿主上裸跑（实测那台 UGREEN-776B 只有 python 3.10.7，什么都没装）。

```bash
git clone <repo> && cd molt
pip install molt-handoff   # 发行名；之后直接敲 molt
molt doctor             # 看这台机器能做什么
molt shell              # 看"我是谁、我在哪"
```

不想装也行——仓库根直接 `python -m molt.cli` 一样能跑：

```bash
PYTHONPATH=. python3 -m molt.cli doctor
```

**构建需要 `setuptools`**（标准的 PEP 517 build 依赖，多数环境自带，没有的话
`pip install setuptools wheel`）。打出来的 wheel 里**只有 15 个模块 + LICENSE**，
不会把 `plugin/`（AstrBot 侧插件）和 `tools/`（一次性脚本）塞进去。

AstrBot 侧那个插件是**另外装的**：把 `plugin/` 整个目录放进
`<AstrBot data>/plugins/astrbot_plugin_molt/`，重启 AstrBot 即可。
它在容器里想有个可读的名字，就在同目录下放一行 `host_label.txt`（内容如 `UGREEN-776B`）——
改完**立刻生效**，不必为这个去重建一个有状态的容器。

---

## 许可证

建议 Apache-2.0（见 `docs/PLAN.md` §7）。

**注意**：本项目推荐的搭档插件 `astrbot_plugin_private_companion` 是 **AGPL-3.0-only**。
本项目只通过公开 API / 配置契约与它对接，**不复制其代码**。

---

<sub>Molt（蜕壳）：蛇蜕皮、蟹换壳。旧壳留下，魂走。</sub>
