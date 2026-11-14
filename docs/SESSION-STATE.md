# 会话状态（滚动更新）

> 用途：DSH 就跑在这台 Windows 上，重启会杀掉会话。这份文件保证「人重启回来」
> 之后，无论会话能不能恢复，都能从正确的位置继续，而不是从头再来一遍。

最后更新：2026-11-14 23:02

---

## 一句话现状

**全部关卡已闭环，包括最后一个产品级未知数。** v0.1 代码骨架写完并扩到 440 个测试全绿；
两个方向都端到端跑通，而且已经能用两条命令（`migrate` + `land`）走完，中间没有人的环节。
唤醒（`wake`）和清理（`prune`）也已从临时脚本提成了产品命令；
Windows 侧的优雅停机信号也实测出来了（不是 `os.kill`，是 attach 到控制台）；
能让源壳"闭嘴而不死"——`astrbot_plugin_molt` 四端点 + `migrate --control-url` 真机跑通，
**而且这个插件现在装在 NAS 的生产容器里**（真机 drain 0.02 秒 / resume 一拍回来，进程没重启）。

**2026-11-14 22:53：真实 QQ 收发通过。** 生产容器优雅停机 → Windows 壳用从 NAS 搬来的凭据
连上 `api.sgroup.qq.com`（TCP 到 `120.241.201.18:443` ESTABLISHED）→ 人发「在吗在吗」→
它回「在喵，怎么了？」→ 两个壳都干净停机 → 生产 1 秒拉回。**双活窗口为 0**，全程挂保险丝。

**2026-11-14 23:12：自己用的流程成形了。** `docs/SELFUSE.md` 一页纸（去程 5 步 / 归程 5 步 /
退出码表 / 常见症状）+ `tools/platform-switch.py`（只改 `platform[].enable`，BOM 进 BOM 出，
不带 `--yes` 只报告）。**两台机器各敲一条命令**，不动 NAS 的 sshd —— 见下面「自己用」一节。

剩下的只有「断点续传」（27.3 MB 一次 PUT 没问题，再大才需要）。

---

## WOL：全部关闭 ✅

| # | 关卡 | 结论 |
|---|---|---|
| 1 | BIOS（ErP / Power-On-by-PCI-E / WOL） | ✅ 持久有效，用户现场完成 |
| 2 | 快速启动（伪关机）已关 | ✅ 持久有效，`BootType=0` 证实 |
| 3 | 封包格式与网络路径 | ✅ 102 字节结构校验 + 三路齐发 |
| 4 | 真 S5 关机被唤醒 | ✅ 12:40，发包→上电 11 秒，掉线到恢复 23.1 秒 |
| 5 | DHCP 保留（非手填静态） | ✅ 开机 7 秒仍拿到 `192.168.1.20` |
| 6 | **重启后 arm 自动恢复** | ✅ **13:00:34：开机第 11 秒，`改前 False → 改后 True`，无人干预** |

**自愈闭环的三个关键点**（写进 ARCH §7.4 / WOL-HANDOFF）：
1. `arm 状态(改前): False` —— 排除"空转"，worker 真走到需要干活的分支
2. 13:00:34 不在 `:05/:15/:25/:35/:45/:55` 网格上 → 只能是 `AtStartup` 触发器
3. 全程没人手动跑 `deviceenablewake`

回看这份状态，只需一条命令（不需要 AI）：
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\13553\Downloads\DSH_workspace\molt\tools\after-reboot.ps1"
```

---

## v0.1 代码：15 模块 + AstrBot 插件 / 7,094 + 666 行 / 440 测试全绿【2026-11-14 23:15 实测】

目录：`C:\Users\13553\Downloads\DSH_workspace\molt\molt\`（核心）、`molt\plugin\`（AstrBot 侧插件）

| 文件 | 行 | 状态 | 内容 |
|---|---|---|---|
| `cli.py` | 1971 | ✅ | 13 个子命令：`shell` / `status` / `checkout` / `verify` / `push` / `release` / `migrate` / `land` / `wake` / `prune` / `serve` / `doctor`，外加 **`migrate --control-url` 按住源壳**（第 0.5 步，三处失败路径都 resume 回滚） |
| `warden.py` | 899 | ✅ | 本地控制面：原子写 state.json、跨进程文件锁、租约、generation 账本、归档中转区、保留策略、消费登记（`mark_consumed`） |
| `server.py` | 589 | ✅ | HTTP 控制面（标准库，含 `PUT/GET /archive/<gen>`、`POST /prune`、`POST /generations/<n>/consumed`，`X-Molt-Token` 常量时间认证） |
| `shell.py` | 651 | ✅ | 身份/能力探测、`preflight()` 判决（proceed / self_heal / block） |
| `soul.py` | 484 | ✅ | 只读 URI + `backup()` 热备、sha256、manifest、`collect_runtime_pip()` |
| `transfer.py` | 400 | ✅ | **打包/校验/解包/落位**：7 道路径穿越检查、sha256 前置、WAL 非空拒绝、sidecar 清理 |
| `client.py` | 383 | ✅ | 壳端 HTTP 客户端（超时、仅连接层重试、kind → 异常映射、流式上传/下载、`prune()`、`mark_consumed()`） |
| `overlay.py` | 372 | ✅ | **机器覆写层**：**7 处**覆写纯函数（第 0 条是公理 0 护栏：关掉所有平台适配器），`utf-8-sig` 保 BOM |
| `wake.py` | 353 | ✅ | **魔术封包**：三路齐发、`directed_broadcast()`、`wait_online()` 带预算的探测 |
| `backend.py` | 303 | ✅ | 抹平本地/远端差异，`upload_archive` / `download_archive` / `archives` / `prune` / `mark_consumed` |
| `control.py` | 281 | ✅ | 插件控制面客户端：`dashboard.jwt_secret` 自签 HS256（不引 PyJWT）、`AstrBotControl`、**`drain_and_verify()` 轮询复核** |
| `epoch.py` | 260 | ✅ | **fencing 核心**：`Lease`、`FencedState`、`accept_write()`、`is_safe_to_start()` |
| `errors.py` | 90 | ✅ | 10 个异常类，`kind` 属性驱动 HTTP 状态码与处置方式 |
| `__init__.py` / `version.py` | 58 | ✅ | 包文档 + `PROTOCOL_VERSION=1` |

**AstrBot 侧插件** `molt\plugin\`（装进 AstrBot 才是活的）：

| 文件 | 行 | 状态 | 内容 |
|---|---|---|---|
| `main.py` | 481 | ✅ | `MoltControlPlugin(Star)`：`drain()` / `resume()` / `platform_snapshot()` / `wal_snapshot()` / `checkpoint()`；**`_classified()` 按实例身份分 outward 与 `local_only`** |
| `api.py` | 161 | ✅ | 四端点 `/status` `/drain` `/resume` `/checkpoint`；`speaking` 是给 Warden 的唯一结论字段 |
| `metadata.yaml` | 24 | ✅ | `astrbot_version: ">=4.24.0"` / `support_platforms` 五个 |

**测试**：23 个测试文件（含 `conftest.py`）**7,852 行 / 440 passed in 32.5s**，测试源码比 **1.01**。
逐文件分布会随重构漂移，别抄文档里的数字，要看就现算：

```powershell
& $py -m pytest -q --collect-only 2>&1 | Select-String "::" | Measure-Object
```

**三条贯穿全程的测试纪律**：
1. **变异测试** —— "测试通过"不等于"测试有牙"。对 `epoch.py` 注入 6 个真 bug 全部被抓；对 `transfer.py` 的 6 个安全检查逐个拆过；对 `server.py` 的 5 个归档安全检查逐个拆过；对 `wake.py` 的 5 个（封包结构 / 广播计算 / 三路齐发 / 超时语义）逐个拆过；对 `prune` 的 3 个默认值与边界逐个拆过；对**平台护栏的 5 个**逐个拆过；对**插件的 10 个**逐个拆过；对 **`migrate --control-url` 的 10 个**逐个拆过。**其中三个变异逼出了新测试**：下载直接写正式文件名（19 个测试全过 → `test_download_interrupted_leaves_no_file_at_destination`）；`disable_platforms` 默认值改 False（30 个测试全过 → `test_apply_overlay_defaults_to_disabling_platforms`，因为两条预设都显式传了 True，默认值从没被走过）；`_platform_configs()` 只认 enable=true（**真机上已经踩过这个 bug，测试却没抓到** → 补 4 条）。
2. **必须用真进程测并发** —— 多线程测不出"两个都拿到 epoch 1"，只有 `subprocess` 能。
3. **断言安全属性，不断言错误消息** —— 换措辞不该让测试失败。
4. **判据是"有没有测试失败"，不是"失败够不够多"** —— 变异脚本第一版有个 `min_fail` 阈值，把 1 个失败报成 WEAK。**猜一个数字让脚本变绿，正是本项目反复踩过的坑**，已删掉。

---

## 两个方向都端到端跑通了 ✅，而且已能**两条命令走完、中间没有人的环节**

### 去程 NAS → Windows（epoch 1 → 2）

| 步 | 动作 | 结果 |
|---|---|---|
| 1 | NAS `checkout` | gen-1，27.3 MB，3 工件，51 个运行时包，0.8–1.1s |
| 2 | 打包 + 下载 | 4,604,611 B，两端 sha256 一致 |
| 3 | Windows `verify` | 逐文件 sha256 + 表数全过 |
| 4 | Windows `push` | 登记成功，`target_shell=win-shell` |
| 5 | NAS `release` → Windows `acquire` | **epoch 1 → 2**，先死后生成立 |

### 归巢 Windows → NAS（epoch 3 → 4）

| 步 | 动作 | 结果 |
|---|---|---|
| 1 | Windows 库写入标记行 | `conversations: 2 → 3`，`MOLT-RETURN-TRIP-20261114T133640` |
| 2 | Windows `checkout`（远程 Warden） | gen-2，27.3 MB，`epoch=3`，热备 0.042s |
| 3 | 上传 + NAS `verify` | 4,624,316 B，sha256 `c2b03d5d…` 两端一致，逐文件通过 |
| 4 | NAS `push` | 登记 gen-2，提交者 `win-shell`，目标壳 `nas-shell` |
| 5 | Windows `release` → NAS `acquire` | **epoch 3 → 4** |

**归巢的判决性证据**（三方对比 `data_v4.db`）：

| | conversations | history | personas | `molt_test` 标记行 |
|---|---|---|---|---|
| NAS 原库 | 2 | 139 | 1 | 0 |
| **Windows 归巢快照** | **3** | 139 | 1 | **1** |
| NAS 接管后新拍 | 2 | 139 | 1 | 0 |

→ Windows 上写的标记**真的搬回来了**（多 1 条），而且 NAS 自己的库没被污染。

### 一条命令（`molt migrate`）—— 真机 1.43 秒 ✅

```bash
cd /mnt/dm-0/astrbot/_molt/pkg && PYTHONPATH=. python3 -m molt.cli migrate \
    --data /mnt/dm-0/astrbot/data --shell nas-shell \
    --target win-shell \
    --warden-url http://127.0.0.1:6186 \
    --warden-token $(cat /mnt/dm-0/astrbot/_molt/warden.token)
```

拿租约 epoch=5 → 热备 `data_v4.db` 0.028s / `memory_companion.db` 0.044s → 打包 4.4 MB /
4 个文件（**零 sidecar**）→ 登记 generation → 还租约。**real 1.43s**。

### 跨机字节传输也自动化了 —— 真机 1.61 秒 ✅【实测 14:16】

源端（NAS）一条命令，**归档直接 PUT 到 Warden 的中转区**：

```bash
molt migrate --data /mnt/dm-0/astrbot/data --shell nas-shell \
             --target win-shell \
             --warden-url http://127.0.0.1:6186 --warden-token <token>
#   · 已上传到中转区：gen-000005 4.4 MB
#   gen-5 / sha256 3e2dfdbf686c5bf4… / real 1.61s
# 中转区实际落盘：/mnt/dm-0/astrbot/_molt/warden-root/archives/gen-000005.tar.gz 4,629,891 B
```

目标端（Windows）一条命令，**自己从 Warden 取下来**：

```bash
molt land --generation 5 --data G:\molt-p0\land-final\data \
          --warden-url http://192.168.1.10:6186 --warden-token <token> \
          --shell win-shell --yes
# ✓ 已取到 4.4 MB sha256 3e2dfdbf686c5bf4…
# ✓ 解包完成：4 个文件 / 27.3 MB
# ✓ 快照完整性校验通过
# 机器覆写层（Windows）：6 处全部生效
```

**落位后目标 data 精确为 3 个文件，零 sidecar**：
`cmd_config.json` 9,859 B / `data_v4.db` 10,887,168 B / `memory_companion.db` 17,727,488 B。

**数据核对**：`conversations 2` / `history 139` / `personas 1` / `memories 71` /
`knowledge_nodes 160` / `knowledge_edges 257` / `dashboard.host 127.0.0.1` / `dashboard.port 6199`。

**AstrBot 用这份全自动搬过来的数据启动**：`Loaded 1 personas.` / `Starting WebUI at http://127.0.0.1:6199` /
`AstrBot started.` / 库指纹启动前后一字未变（`E87FDEF5002CBE5C` / `1CE3D62B82D8672A`）。

**为什么不做 SMB/SCP 而走 Warden HTTP**：SMB 正是 ARCH §3 判定"锁与 mmap 不可靠"的那个东西；
SCP 要第三套凭证 + Windows 上还没装 sshd。走 Warden 复用已有通道，**零新增端口、零新增凭证**。

### 落位 + 机器覆写（`molt land`）—— 6 处改写，45 个键只动 3 个 ✅

```
✓ 移除 provider_sources：workbuddy（本机没有这些服务）
✓ 移除引用它们的 provider：workbuddy/deepseek-v4.1-flash（留着会抛 KeyError: 'type'）
✓ agent_runner 主模型：workbuddy/deepseek-v4.1-flash → deepseek/deepseek-flash
✓ dashboard.host：0.0.0.0 → 127.0.0.1
✓ dashboard.port：6185 → 6199
✓ dashboard.jwt_secret：已重新生成（原值是随 soul 一起搬来的，绝不能跨机复用）
```

覆写后启动 AstrBot：`Starting WebUI at http://127.0.0.1:6199`、`Loaded 1 personas.`、
`AstrBot started.`，**全量 WARN / ERROR 行数为 0**。

---

## 十三个只有换机器/换条件才暴露的 bug（都已修，都有测试固定）

| # | bug | 核心教训 |
|---|---|---|
| 1 | GBK 控制台 `UnicodeEncodeError` | 靠**实测编码能力**降级，不靠猜平台 |
| 2 | 导入名当包名（`PIL`→`pillow`） | 读 `*.dist-info/METADATA` 的 `Name:`，不猜目录名 |
| 3 | 热备副本留下 `-shm`/空 `-wal` | **只对副本**调清理函数；对活库删 sidecar 会直接搞坏它 |
| 4 | 裸 `sqlite3.DatabaseError` 绕过 `Fatal` | **租约永远收不回来**。凡是 sqlite3 可能抛的地方都要包 |
| 5 | staging 落在 `/tmp` 被重启清掉 | 账本里会留一条**指向空气的记录** |
| 6 | `land` 共享时间戳目录继承旧 manifest | **它不会报错，它只是默默地用了错的东西** |
| 7 | WAL sidecar 被当快照内容搬走 | 送到新壳等于人为制造"看起来像崩溃现场" |
| 8 | 下载中途断开把半截文件留在正式文件名上 | **变异测试才发现的**：去掉 `.part` 写法后 19 个测试全过，只有"中途断开"能区分 |
| 9 | `wait_online(timeout=0.6)` 实测跑了 2.006 秒 | **承诺与行为不符**：单次探测固定 2 秒、不看剩余预算。真机 180→182 秒没人注意，但探测间隔完全失真 |
| 10 | `os.kill(pid, CTRL_BREAK_EVENT)` 退出码 `0xC000013A` 看着像 Ctrl+C 生效 | **假阳性**：默认处理器直接杀进程，`stop()` 一行没跑。真信号要 `AttachConsole` + `GenerateConsoleCtrlEvent` |
| 11 | **覆写表漏了 `platform[].enable`** | **不是"配置错了"，是差几秒就违反公理 0**：`appid` 该搬走，`enable` 不该。见下节 |
| 12 | **`drain()` 靠配置 id 找适配器，漏了 `WebChatAdapter`** | **不是逻辑写错，是判据选错了维度**：它在 `platform_insts` 里但不在 `_inst_map` 里（`manager.py:116` 直接 append）。改用**实例身份**（`id(ent["inst"])`）作主判据 |
| 13 | **`platform-switch.py` 用 `utf-8-sig` 写回，给原本没有 BOM 的文件加上了 BOM** | `utf-8-sig` 在**读**时是「吃掉 BOM」，在**写**时是「加上 BOM」——**方向相反，很容易只记住一半**。改法：先看原文件前 3 字节，原来有才写 |

**第 11 和 12 是同一种病**：实现本身完全正确，它只是**回答了一个比你以为的更窄的问题**。
一个查"我该改哪些配置项"漏了一项，一个查"哪些实例算对外连接"选错了维度。

---

## 差一点出事的两次事故（都只差几秒）

### 事故一：落位后 `platform[].enable` 还是 `True`【2026-11-14 14:41】

做 Windows 优雅停机实验时，三次用落位数据启动了真实的 AstrBot。那三份 `cmd_config.json` 的 `platform[0].enable` 全是 **`True`**（从 NAS 原样搬来的），于是每次启动都真的发起了 QQ 连接：

```
shutdown-test-break-20261114T144130.log
  [14:41:37.426] Loading IM platform adapter qq_official(default_1234567890) ...
  [14:41:40.288] [QQOfficial] Websocket session starting.

shutdown-test-console-20261114T144408.log
  [14:44:14.605] Loading IM platform adapter qq_official(default_1234567890) ...
  [14:44:17.181] [QQOfficial] Websocket session starting.
  [14:44:17.595] Attempting to terminate platform adapter default_1234567890 ...
```

**当时覆写表有 6 条，一条都没碰 `platform`。** 而这个字段"从 NAS 搬过来"这件事谁都不容易想到去查——它不在"机器绑定"的直觉清单里：`appid` 明明是**该**搬的，但 **`appid` 可携带、`enable` 不可**。

**为什么没真的出事**：三个进程分别在 3 秒、3 秒、60 秒内就被测试脚本杀了，QQ websocket 的 `IDENTIFY` 握手都没走完（Windows 侧所有日志里 `FriendMessage` / `GroupMessage` 命中数全为 0）。生产容器 `RestartCount=0`、`StartedAt` 未变、14:40–14:46 期间持续正常收发与回复。所以是"**差几秒**"，不是"发生过"。

**修法**：`_disable_platform_adapters()` 作为覆写表**第 0 条**排最前面；两条预设（windows / posix）都**不给调用方关闭它的便利**；`land` 的非 Windows 分支也照样关（那条路径原来连覆写都不做）。

**复验【14:56】**：同样流程重跑，`land` 输出第一行 `✓ 关闭平台适配器：default_1234567890`；起 AstrBot 后 `Loading IM platform adapter` / `QQOfficial` / `Websocket session` / `platform.manager` **全部零命中**，而库里的它完好（`personas` 1 / `conversations` 2 / `history` 139 / `memories` 71 / `knowledge_nodes` 160）。

### 事故二：见上一节的 bug 10（`os.kill` 假阳性）

**两次的共同点**：都不是"代码写错了"，而是**检查清单里少了一条**。而检查清单少一条只有两种发现方式——出事，或者运气好。这两次都是运气好。

---

## 唤醒与清理也进了产品【实测 14:32】

### `molt wake` —— 迁徙的第一步不是备份，是"目标机得是开着的"

之前这一步是人手发一个魔术封包，**一个需要人手发封包的"全自动迁徙"只是在报告里看起来是自动的**。

两条实测教训都进了代码：
- **单播发不出去**：机器关机后 ARP 表项过期，单播报文根本出不了网卡 → 三路齐发 `[host, 定向广播, 255.255.255.255]`
- **能不能被叫醒只认一个证据**：`powercfg /devicequery wake_armed`。`PnPCapabilities` 的位、`Get-NetAdapterPowerManagement` 的返回值都不能当判据（真机出现过 `PnPCapabilities=8` 而 WOL 完全正常）

**顺序铁律**：`migrate` 里是**先唤醒 → 再拿租约**（测试断言调用顺序恰为 `["wake", "wait", "acquire"]`）。
理由：租约 TTL 只有 90 秒，叫醒要 23 秒以上——先拿租约会让那 90 秒里有三分之一在等开机。
叫不醒直接 `return 5`，**账本里不会留下指向一台没开机的机器的记录**。

### `molt prune` —— 这个系统里唯一会真删文件的地方

四条规则，按保守程度排序：

| # | 规则 | 为什么 |
|---|---|---|
| 1 | **账本里记着的一律不删** | 那是"已经交接出去、目标壳可能还没来取"的东西。删了它账本就指向空气 |
| 2 | 文件名不是 `gen-NNNNNN.tar.gz` 的一律不删 | 人手工放进 `archives/` 的东西不归清理逻辑管 |
| 3 | 其余按 `keep_recent` 保留最新几份 | 缓冲"刚上传但 push 还没成功"的那次 |
| 4 | **默认 `dry_run=True`** | 一个会删文件的函数，默认参数下不能动手 |

**NAS 真机实测**：中转区有 `gen-000005`（账本里记着）+ 两个账本不认的 + 一个外人放的：

```
$ molt prune --keep-recent 0 --yes
✓ 已删除 2 份，腾出 11.7 KB
  - gen-000098.tar.gz  6.8 KB  账本里没有
  - gen-000099.tar.gz  4.9 KB  账本里没有
  保留 1 份：
    gen-000005.tar.gz  账本里记着这一代，目标壳可能还没取走

盘上剩：gen-000005.tar.gz（4,629,891 B）+ somebody-else.tar.gz（21 B）
```

清理动作写进 Warden 日志；`molt status` 复查账本完好（`epoch 7` / 5 次迁徙 / 零拒绝）。
**变异测试**：把 CLI 的 `dry_run=not args.yes` 改成 `False`、端点的默认值改成 `False`、`referenced` 恒 `False`——三个全被抓。

---

## Windows 侧的优雅停机 —— 有，但不是 `os.kill`【实测 14:44】

Linux 侧早就定了：`SIGTERM` 直接死（WAL 8 MB 原封不动），`SIGINT` 走优雅路径。Windows 没有 `SIGTERM`，三种发法都试过：

| 方式 | 退出码 | 有 `Shutting down` | 判决 |
|---|---|---|---|
| `os.kill(pid, CTRL_BREAK_EVENT)` | `3221225786`（`0xC000013A`） | 否 | **假阳性**——默认处理器直接杀了进程，`stop()` 一行没跑 |
| `os.kill(pid, CTRL_C_EVENT)` | 无（60 秒超时） | 否 | Windows 不跨进程组递送 |
| **`AttachConsole(pid)` + `GenerateConsoleCtrlEvent(CTRL_C_EVENT, 0)`** | **0** | **是** | ✅ 真的 |

**checkpoint 硬证据**（只看 `Shutting down` 字样不够）：

```
跑之前：  kb.db  45,056 B   kb.db-wal 98,912 B   kb.db-shm 32,768 B
跑之后：  kb.db  94,208 B   kb.db-wal (不存在)    kb.db-shm (不存在)
```

那 98 KB 已提交但压在 WAL 里的事务**真的合并进主库了**，sidecar 被正常清除。
这只有 `core_lifecycle.stop()` 跑到 `kb_manager.terminate()` 才会发生。

**不改变 v0.1 的交接协议**：Windows 侧热备走只读 URI + `backup()`，本来就不需要停 AstrBot（0.042 秒已验）。
它的用途是"这只鱼决定要休眠时关得干净"，也就是 v0.3 的 `molt sleep`。

**这一关抓到的两个"测试自身"的错**（都值得记）：
1. `ASTRBOT_ROOT` 设成源码目录 → AstrBot 在 `astrbot-src/data/` 下自建了一套空库，而我一直盯的目录**压根没被碰过**（`sha256` 一致是因为没动它）。正确关系是 `ASTRBOT_ROOT = <data 的父目录>`。垃圾已删。
2. GBK 控制台又被 `✨` 打崩（`UnicodeEncodeError: 'gbk' codec can't encode character '\u2728'`）——**1 号 bug 的第二次出现**。修法同前：`stream.reconfigure(encoding="utf-8", errors="replace")`，必须在 `main()` 第一行。

---

## 消费登记：账本终于知道灵魂落到谁手上了【2026-11-14 17:10】

之前账本只有 `pushed_by`（谁推的）和 `target_shell`（**打算**交给谁）。目标壳一直没开机、
或者取归档失败，账本对此一无所知——于是 `molt status` 只能替人猜"灵魂现在在哪台机器上"。
猜错的代价不是"显示难看"，而是有人在错误的机器上启动第二个实例。

现在把"消费"变成一个必须显式登记的事实：

| 位置 | 改动 |
|---|---|
| `warden.py:GenerationRecord` | 新增 `consumed_by` / `consumed_at`；`consumed_at_readable` 在未消费时是**空串**（不能打印 1970，那看起来像"很久以前被取走了"） |
| `warden.py:Warden.mark_consumed()` | 锁内 `_reload_locked()` → 找代 → 判定 → `save()`。同壳重复标记**幂等且不刷新时间**（否则时间线会撒谎）；异壳标记同一代 → `NeedsHuman`（两条鱼在打架，不替人挑赢家）；账本里没有这一代 → `Fatal`（两边认知已不一致，不是可重试错误） |
| `server.py` | `POST /generations/<n>/consumed`，缺 `shell_id` / 非数字都是 400 |
| `backend.py` | 本地/远端各一个 `mark_consumed`，返回形状统一成 dict |
| `cli.py:cmd_land` | 落位成功、且这份快照是从 Warden 取的（`--generation`）才登记。**登记失败只 warn、绝不让 land 失败**：字节已经进 data/ 是不可逆的事实，账本只是索引；把一次成功的落位报成失败会让人重跑 land，把刚恢复好的 data/ 再覆盖一遍 |
| `cli.py:cmd_status` | 有消费就显示"消费方 / 消费时间"，没有就如实写"还没人取走"，**不拿 `target_shell` 冒充事实** |

**证据**（都是实测，不是"应该能行"）：

- `pytest -q` → **403 passed in 32.20s**（新增 `molt/tests/test_consumed.py` 11 个）
- 真套接字冒烟（临时 root → `molt serve` → 客户端 mark）：第 1 次写入成功；
  第 2 次同壳 `consumed_at` 不变；第 3 次异壳抛 `NeedsHuman`；
  **换一个进程读盘**，账本里确实有 `consumed_by=win-abc`
- `molt status` 两种措辞都验过：已消费显示「消费方 win-abc / 消费时间 2026-11-14 17:10:16」，
  未消费显示「消费方 还没人取走」

**还没做的**：`consumed` 目前**只是事实登记，不参与 fencing 判定**（谁能不能写灵魂仍由
epoch + 租约决定）；`molt handoff` 这种"一条命令走完 migrate → wake → land → 验收"的编排器也还没写。
下一块就从这里接。

## 仓库与 CI【2026-11-14 17:02】

`C:\Users\13553\Downloads\DSH_workspace\molt` 现在是一个**本地 git 仓库**（没有 remote）：

- 首次提交 `3f7eccd`「chore: 初始化仓库，固化 v0.1 当前状态」，64 个文件，仓库 400 KB
- 标签 `v0.1.0`，与 `molt/version.py` 的 `__version__` 对齐
- `.gitignore` 只挡运行期产物（`__pycache__`、`.pytest_cache`、`*.log`、`*.pid`、`tools/logs/`、
  `_molt-pkg.tar`、`data/`、`state/`、`*.bak-*`）；`tools/` 下 shutdown 测试留下的 `.json`
  证据**故意入库**——它们是结论凭据，不是垃圾
- `core.autocrlf=false` + `.gitattributes` 强制 `eol=lf`：这项目靠 sha256 判断「字节有没有
  搬家成功」，工具链按平台改写换行会直接污染这个凭据
- `.github/workflows/tests.yml`：Windows + Linux × Python 3.12/3.13 跑 `python -m pytest -q`
  （本地没 remote，所以还没跑过；将来推上去才有意义）

建库前扫过 `*.py/*.json/*.md/*.yaml`：**没有真实密钥**，命中的全是文档叙述
（`ARCH.md` 讲 `dashboard.jwt_secret` 不可跨机复用）与测试夹具假值。

## 插件控制面：AstrBot 那边的四个端点【15:06 起，22:43 完成真机端到端】

`plugin/` 是 AstrBot 插件 `astrbot_plugin_molt` 0.1.0（666 行），把「让鱼自己闭嘴」做成四个端点：
`/status` `/drain` `/resume` `/checkpoint`。

- `molt/control.py`（243 行）是客户端：复用 `dashboard.jwt_secret` 自签 HS256，
  **不引入 PyJWT**——为了一个 header 装一个依赖不划算
- `tools/astrbot_plugin_molt_loopback` 是不连外网的假平台适配器，专供演练
- **`_classified()` 用实例身份而不是 id 字符串**做主判据。为什么：`WebChatAdapter` 由
  `manager.py:116` 直接 append 进 `platform_insts`，**从不进 `_inst_map`**，`config.platform`
  里也没有它 —— 第一版 `/drain` 因此**什么都没关却返 409**（`connected_ids: ["WebChatAdapter"]`
  而 `enabled_in_config: []`）。webchat 归 `local_only`：**可见、不隐藏、不关**
  （它每个实例都必然存在，算进 `speaking` 就等于 `speaking` 恒真）
- live 证据（第一轮）：测试实例监听 `127.0.0.1:6199`，`terminate → load → terminate` 走完一轮
  drain/resume；测试 data 在 `G:\molt-p0\ctl-test\data`
- **`migrate --control-url` 真机端到端 PASS【22:43】**：`speaking` True→False（轮询复核过）、
  热备在闭嘴之后拍（0.040s）、`lease = None`（已交还）、归档 `gen-000001.tar.gz 4,628,724 B`、
  AstrBot 日志多出 `Attempting to terminate platform adapter molt-loopback-1`
- 变异测试：插件 **10/10**（第一轮 8/10，两个 MISSED 都是真漏洞）；
  `migrate --control-url` **10/10**（三轮才满，两轮 MISSED 都是测试太松）

---

## 自己用：两台机器各敲一条命令【2026-11-14 23:12】

**为什么不是一条命令**：本想做得更省事（在 Windows 上敲一下就把鱼叫过来），但实测撞到三个硬约束 ——
NAS 的 `/etc/ssh/sshd_config` 是 **`PubkeyAuthentication no`**、`~/.ssh` 目录根本不存在、
Windows 侧也没有私钥而且没有 `sshpass` / `plink`。要免密就得改 sshd 并重启，
**而改 sshd 有把远程访问弄丢的风险**。所以选了最不刺激的方案：**两台机器各敲一条命令**，不动 NAS 的任何系统配置。

**产物**：

- `docs/SELFUSE.md`（310 行）—— 一页纸操作手册，不解释原理，只给命令 / 现象 / 收场：
  去程 5 步、归程 5 步、三条铁律、退出码表、常见症状。
- `tools/platform-switch.py`（206 行）—— `molt land` 落位时**一定**会关掉 `platform[].enable`
  （`overlay.py` 覆写表第 0 条），重新打开的唯一途径就是人手改 JSON。这个脚本只改**那一个字段**：
  `show` / `on` / `off`，**不给 `--yes` 就只报告不写**，写回后**再读一遍复核**，
  而且**看原文件前 3 字节决定写不写 BOM**（才不会给本来没有 BOM 的文件加一个）。
  `on` 和 `off` 都必须显式给 `--yes`：**这个脚本能让你的一只鱼开始说话**，两个方向都该是人的决定。

**真机验证**：`land` 对第 6 代 dry-run → `exit=0`，输出「快照完整性校验通过」与备份目录提示；
`platform-switch.py` 对生产配置只 `show` / dry-run（**生产 `enable` 仍是 True，没被改动**），
对复制件 `off --yes` 成功、逐键对比 `差异键: ['platform']`、`appid` 与 `jwt_secret` 原样、
BOM `efbbbf → efbbbf` 保住；另造无 BOM 样本验证 `7b0a20 → 7b0a20`（没被加 BOM）。

**测试**：`molt/tests/test_platform_switch.py`（258 行 / 18 用例）+ 变异 5/5 全抓
（写回永远不带 BOM / `off` 不带 `--yes` 也写 / 顺手改 `secret` / 顺手改 `jwt_secret` / 编码降级失效）。

## 一条消息让它自己走（2026-11-14 23:30）

**用户要的**：「能不能发消息让它迁移」。实现分两半，**一半已装、一半待你开**。

### NAS 侧（已装、已验证到指令能读账本）
- `plugin/command.py`（23,702 B，零 AstrBot 依赖）+ `main.py` 里两片薄层：
  `@filter.command("molt")` + `@filter.after_message_sent()`
- 三道闸：`@permission_type(ADMIN)` / 两步确认 + 120 秒时效 / `enabled` 默认 false
- 参数**只认整词**（`/molt 我不想回家` 不搬）
- 执行走**子进程**：`migrate` 会 `POST /drain` 到本进程，同事件循环里同步等它＝自己等自己，必然死锁
- 部署：`data/plugins/astrbot_plugin_molt/` 下 `main.py` 26,224 / `command.py` 23,702 /
  `api.py` 6,742 / `metadata.yaml` 889 / `host_label.txt` / `molt_command.json` /
  `_vendor/molt/*.py` 16 个（**必须 vendor**：`/mnt/dm-0/astrbot/_molt/pkg` 不在容器 bind mount 里）
- 容器内实测：`Settings.problems() == []`、`/molt status` 读到
  「账本 epoch 8，已登记 6 代 / 现在没有租约」、`/molt go` 产出确认语、`/molt cancel` 清提议
- 优雅重启（`docker kill -s INT` → exit 0，`-wal` 归零）后 `Loading plugin astrbot_plugin_molt` +
  四端点注册，**零 traceback**

### Windows 侧（已写好、**execute 仍是 false**）
- `tools/molt-agent.py`（纯标准库）：每 10 秒问一次 Warden `/status`
- 五种判决 `idle / wait / land / start / error`，全部由纯函数 `decide()` 给出
- 三条线：不是给我的不动 / 有人攥租约不落位 / 本机端口在听就不起第二只
- `needs_start` 债务机制：落位成功但启动失败会记住，下一轮先还债
  （否则账本显示 `consumed_by=我` 一切正常，而鱼其实没在说话）
- **真机 dry-run 实测**：连到 192.168.1.10:6186，判决 `[idle] 第 6 代我已经落过了`

### 数字
`531 passed in 32.78s`（原 499 + 30 agent + 2 spawn 断言）。变异 12/12 CAUGHT
（第一轮 11/12，漏的那条逼出了 `create_task` 间谍测试）。

### 还没验的
**这个循环没有在真机上完整跑通过一次。** NAS 侧到"指令能读账本"为止是真的，
Windows 侧到"判决逻辑 dry-run 正确"为止是真的，中间那段（land → 打开平台 → 起 AstrBot）
只在单元测试里跑过。第一次真跑请两边同时盯日志。

## 还没验的（按优先级）

1. ~~**真实 QQ 收发**~~ ✅ **已通过【实测 2026-11-14 22:53】**

   窗口顺序（这一步必须让生产容器先停，否则就是两条鱼同时在线）：
   ① NAS 挂保险丝 `tools/start-window-guard.py --minutes 10`
   ② `docker kill -s INT astrbot`（退出码 0，WAL 8,252,392 B 已 checkpoint 并删除）
   ③ `G:\molt-p0\qq-test\data` 从 gen-6 落位，手动把 `platform[].enable` 改成 `true`
   ④ `boot-keepalive.py G:\molt-p0\qq-test\data 6199 <log>` 起壳
   ⑤ 人在 QQ 发一句话 → ⑥ 停 Windows 壳（`AttachConsole`）→ ⑦ `docker start astrbot` → ⑧ `--cancel`

   判决性证据（三条）：
   - **TCP**：Windows 上 python 进程到 `120.241.201.18:443` 有 ESTABLISHED，
     `Resolve-DnsName api.sgroup.qq.com` 就是这个地址 —— 凭据真的通过了握手
   - **入站**：`[22:53:23.400] [core.event_bus:155]: [default] [default_1234567890(qq_official)] 0123456789ABCDEF…: [At:qq_official] 在吗在吗`
   - **出站**：`[22:53:24.602] [respond.stage:237]: Prepare to send - /0123456789ABCDEF…: 在喵，怎么了？`
     （人格起了作用，不是通用模板；之后没有发送错误）

   两个 WARN 是预期的：`Provider whisper-local was not found` / `Provider edge-tts was not found`
   —— 就是 §13.4 的 P-1，`runtime_pip` 那 51 个包没装。**不影响文字收发。**

2. **断点续传**（低优先）。当前传输失败要整份重传；4.4 MB 无所谓，30 MB 以上值得做。

3. **新壳的 51 个运行时包装不装**（`edge_tts` / `whisper` / `bilibili-api-python` …）。
   装了才有语音；不装只掉语音，文字照常。

4. ~~**`host` 字段在容器里报 docker 容器 ID**~~ ✅ **已修并真机实测【2026-11-14 23:01】**

   `host_label()` 现在是 `MOLT_HOST_LABEL` → 插件目录下的 `host_label.txt` → 主机名 → `unknown`。
   生产容器已经写了 `host_label.txt`（内容 `UGREEN-776B`），`GET /status` 返回 `host = UGREEN-776B`。
   加文件这一层的原因是**不为一个显示名字去重建有状态的容器**；文件改完立刻生效（每次请求重读）。
   下次 `docker run` 时仍建议加 `-e MOLT_HOST_LABEL=UGREEN-776B`（更"硬"、更显式）。

## 打包（开源前收尾）

- `pyproject.toml` + `LICENSE`（Apache-2.0，与 PLAN.md 选型一致）已补齐。
- 实测：`pip wheel . --no-deps` → `molt-0.1.0-py3-none-any.whl`（121,118 B），
  里面**只有 15 个模块 + dist-info**（`plugin/` 与 `tools/` 一个都没漏进去）；
  装进一个临时 venv 后，**从仓库外的目录**跑 `molt shell` / `molt doctor` 正常
  → 证明不靠 cwd 也不靠 `PYTHONPATH`。
- **构建需要 setuptools**（本机两个 python 都没装，是标准的 build 依赖）：
  离线环境先 `pip install setuptools wheel`，或让 pip 用联网的 build isolation。
  运行时仍然是**零第三方依赖**，这一点没有变。


## 待用户操作的杂项

- `Set-NetConnectionProfile -InterfaceAlias "以太网" -NetworkCategory Private`
  （否则将来 MoltAgent 开端口会被防火墙拦；当前是 Public）

---

## 怎么重新起 Warden（NAS 上）

```bash
# 启动（脱离 SSH 会话，BusyBox 没有 nohup/setsid）
python3 /mnt/dm-0/astrbot/_molt/tools/start-warden.py
# 端口 6186，token 在 /mnt/dm-0/astrbot/_molt/warden.token，日志 warden.log
# ⚠️ 改了代码后必须重启它 —— Python 已经把旧模块加载进内存了
```

Windows 侧用法：
```powershell
$py = "G:\molt-p0\venv313\Scripts\python.exe"   # 或 C:\Users\13553\...\Python313\python.exe
cd C:\Users\13553\Downloads\DSH_workspace\molt
& $py -m molt.cli status --warden-url http://192.168.1.10:6186 --warden-token <token>
```

**注意两个 python 不能混用**：
- 跑 pytest：`C:\Users\13553\AppData\Local\Programs\Python\Python313\python.exe`（装了 pytest 9.2.0）
- 跑 AstrBot：`G:\molt-p0\venv313\Scripts\python.exe`（178 包，**没有 pytest**）

---

## 部署到 NAS 的步骤（每次改代码后）

```powershell
# 1. 打包（--exclude 必须在 -cf 之前！）
cd C:\Users\13553\Downloads\DSH_workspace\molt
tar.exe --exclude="__pycache__" --exclude="*.pyc" --exclude=".pytest_cache" -cf _molt-pkg.tar molt docs tools
# 2. ssh_upload 到 /mnt/dm-0/astrbot/_molt/_molt-pkg.tar
# 3. NAS 上解包，包根是 pkg/molt（不是 pkg！）
```

---

## 这个项目最重要的一句话（别忘了）

> **这个存在，在任何时刻只能有一个。**

WOL 的 arm 状态、租约 TTL、QQ 互踢，都只是这条铁律的实现细节；
真正的屏障是 **epoch fencing：存储端主动拒绝倒退的写入**。


## NAS 侧守护进程已部署（2026-11-14 23:54，execute=false）

- 	ools/molt-homer.py 21,268 B 已上传到 /mnt/dm-0/astrbot/_molt/pkg/tools/，
  配置 molt-homer.json（execute: false 只报告）。
- 两个分支在真机上各验一次：
  - [idle] 第 7 代是给 win-shell 的，不是我
  - 塞一个 
eeds_start 欠账 → [land] 上一轮停下了但没起来（补上） + (dry-run) 我会：停容器 → 落位 → 打开平台 → 起容器
- 生产容器全程未被碰：	rue 0 2026-11-14T15:28:16.818309273Z。
