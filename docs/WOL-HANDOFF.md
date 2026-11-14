# WOL 这一关：已彻底闭环 ✅（含自愈，且自愈本身也验证过）

> **2026-11-14 最终状态**：这一关**全部关闭**。
>
> - **能叫醒**：真 S5 关机 → NAS 发魔术封包 → **23.1 秒**唤醒，PASS（12:40）
> - **能一直叫醒**：重启后网卡会自动掉出 `wake_armed`，`Molt-WolGuard` 在**开机第 11 秒**
>   自动把它加回来（13:00:34 实测：`改前 False → 改后 True`，无人干预）
>
> 两份记录都在下面，以及 [`ARCH.md`](ARCH.md) §7.4。

---

## 当时的判决

| # | 关卡 | 结论 | 证据 |
|---|---|---|---|
| 1 | 网卡不在 OS 唤醒白名单 | ✅ **已解决**（曾以为是"修复即永久"，实为易失状态，现由 `Molt-WolGuard` 每次开机自愈） | 见下方「一个测完才发现的坑」与「自愈闭环」 |
| 2 | 「关机」其实是伪休眠 | ✅ 已修复，持久有效 | `HibernateEnabled=0`、`hiberfil.sys` 已删除、`Kernel-Boot 27` 的 `BootType` 从 1 变 0 |
| 3 | BIOS 里的 S5 供电开关 | ✅ 用户现场完成 | `BootType=0` + 真关机唤醒成功，已证明固件层正确 |
| 4 | 封包格式与网络路径 | ✅ 通过 | NAS → Windows `0.0.0.0:9` 收到 102 字节，结构逐字节校验通过 |
| 5 | **真关机 + 真唤醒** | ✅ **PASS** | 见下方时间线 |
| 6 | **重启后 arm 自动恢复** | ✅ **PASS** | 见下方「自愈闭环 · 13:00:34」 |

### 判决书 · 2026-11-14 12:40

```
[12:40:03] User32 1074       用户点"关机"
[12:40:23] Kernel-Power 109  内核开始关机转换
[12:40:28] offline           NAS 判定掉线（连续两次 ping 不通）
[12:40:31] packet            第 1 轮，3 个封包（单播 + 定向广播 + 255.255.255.255）
[12:40:42] LastBootUpTime    BIOS POST 完成
[12:40:52] wake              网络恢复 —— 从掉线到回来 23.1 秒
```

**这是魔术封包叫醒的、不是你按的电源键**，三条独立证据：

1. **发包（12:40:31）到上电（12:40:42）只差 11 秒** —— 正好是 BIOS POST + 引导耗时。人不可能卡着这个位置按。
2. **`Kernel-Boot 27` 的 `BootType=0`（冷启动 / 真 S5）** —— 对照当天 `11:08:29` 的 `BootType=1`（快速启动 / 伪关机）。快速启动确实关掉了。
3. **`User32 1074` → `Kernel-Power 109`** —— 完整的关机流程，没有 `Kernel-Power 41`（异常断电），没有电源按钮事件。

> `powercfg /lastwake` 报「唤醒历史记录计数 - 0」是**正常的**。
> 那个计数器只统计**睡眠唤醒**，S5 冷启动不计入。当时差点又被它带偏。

**顺带验证**：路由器 DHCP 保留生效，开机 7 秒后仍拿到 `192.168.1.20`（新租约 `11-14 12:40:49 → 11-15 12:40:49`）。

---

## ⚠️ 一个测完才发现的坑：arm 状态不跨重启保留

12:40 测完，我写下了「已修复 ✅」。**12:49 复查，网卡又不在 `wake_armed` 里了。**

| 时间 | 事件 | 在 `wake_armed` 里吗 |
|---|---|---|
| 12:26 | 管理员跑 `deviceenablewake` | ✅ |
| 12:28 | 复查 | ✅ |
| 12:40:42 | 被魔术封包唤醒（PASS） | — |
| **12:49** | **复查** | ❌ **没了** |

12:24 那次为进 BIOS 的重启也一样：重启前有，重启后没有。**两次重启、两次丢失。**

含义：**「这次唤醒成功」只证明了「这次关机时它是 armed 的」，不证明下次还 armed。**
不处理的话会撞上最难查的故障形态——**第一次好使、第二次叫不醒**。

### 解法：一个开机自愈任务

**以管理员身份**跑一次：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\13553\Downloads\DSH_workspace\molt\tools\install-wol-guard.ps1"
```

它会注册计划任务 `Molt-WolGuard`：

| 项 | 值 |
|---|---|
| 触发器 1 | `AtStartup` —— 主路径，每次开机重新 arm |
| 触发器 2 | 每 10 分钟一次 —— 兜底，万一开机那次失败 |
| 身份 | `SYSTEM`，`RunLevel Highest`，`LogonType ServiceAccount`（不需要存密码，不需要人登录） |
| 动作 | `powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File wol-guard-worker.ps1` |
| 日志 | `tools/logs/wol-guard.log`（超 200KB 自动滚存） |

worker 的行为是**幂等**的：已经在白名单里就 `[SKIP]`，不在就 arm 一次再复查，复查仍失败记 `[FAIL]` 并以退出码 2 退出。

### ✅ 自愈闭环 · 2026-11-14 13:00:34（已通过）

装完重启，日志给出了完整证据链：

```
LastBootUpTime : 2026-11-14 13:00:23
13:00:34  --- 开始检查 ---        ← 开机后 11 秒
13:00:34  身份: NT AUTHORITY\SYSTEM
13:00:40  网卡: Realtek PCIe 2.5GbE ... Status Up
13:00:40  arm 状态(改前): False    ← 重启后确实是丢的，不是空转
13:00:40  deviceenablewake 返回码: 0
13:00:40  arm 状态(改后): True
13:00:40  [OK] 已重新 arm
13:00:40  HibernateEnabled = 0
```

**三个关键点**：

1. **`改前 False`** —— 排除了"本来就在白名单里、跑了个空转"这种解释。worker 确实走到了需要它干活的分支。
2. **13:00:34 不落在 10 分钟网格上** —— 重复触发器从 `00:05` 起每 10 分钟（`:05 :15 :25 :35 :45 :55`），
   所以这次只能是 `AtStartup` 触发器干的。
3. **全程无人干预** —— 没有手动跑 `deviceenablewake`。

worker 的行为是**幂等**的：已经在白名单里就 `[SKIP]`，不在就 arm 一次再复查，复查仍失败记 `[FAIL]` 并以退出码 2 退出。

> **为什么不能靠 UAC 自动提权**：实测 `EnableLUA=1` 时，HKLM 的驱动键与设备实例键都不可写，
> 非管理员跑 `deviceenablewake` 直接报「你没有启用或禁用设备唤醒的权限。」
> 所以这个计划任务必须由你以管理员装一次；装完之后它自己每次开机跑，就不需要你了。

### 以后想自己确认一次（不需要 AI）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\13553\Downloads\DSH_workspace\molt\tools\after-reboot.ps1"
```

它会把 `LastBootUpTime` 和 `tools\logs\wol-guard.log` 对齐，直接告诉你本次开机自愈有没有跑。

---

## 将来重跑这个流程（换机器/重装系统时用）

### 第 1 步 · 两件事一条命令

**右键点开始菜单** → **「Windows PowerShell (管理员)」**，然后：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\13553\Downloads\DSH_workspace\molt\tools\fix-wol.ps1"
```

它做三件事：

1. `powercfg /h off` —— 关休眠 + 关快速启动（顺带腾出 ~6.4 GB）
2. `powercfg /deviceenablewake "Realtek PCIe 2.5GbE Family Controller"` —— 把网卡加进唤醒白名单
3. 复查并打印 `nic_in_wake_armed` / `fast_startup_off`

只想看状态、不想改任何东西的话，用这个（**Warden 也是调它**）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\13553\Downloads\DSH_workspace\molt\tools\check-wol.ps1"
# 加 -Json 输出机器可读结果
```

### 第 2 步 · BIOS 三项（`Del` 进 BIOS）

```
Settings → Advanced → Power Management Setup
```

| 项目 | 设成 | 为什么 |
|---|---|---|
| **ErP Ready** | **Disabled** | 开了它会在 S5 切断 +5VSB，**网卡彻底没电，WOL 必死** |
| **Power On by PCI-E** | **Enabled** | 允许 PCIe 设备唤醒 |
| **Wake On LAN** / Integrated LAN Wake | **Enabled** | 网卡唤醒总开关 |

`F10` 保存退出。这块板子 BIOS 是 `1.70 / 2021-05-12`，选项名可能略有不同：
记住含义就行——**不要断电、允许 PCIe 唤醒、打开网卡唤醒**。

### 第 3 步 · 实测一次

观察器**必须待在 NAS 上**——因为发起关机的就是被试机器，观察者留在同一台机器上会跟着一起死。
NAS 上没有 `nohup` / `setsid` / `screen` / `tmux`，所以 `start-wol-watch.py`
用 `subprocess.Popen(start_new_session=True)` 脱离 SSH 会话：

```bash
python3 /mnt/dm-0/astrbot/_molt/tools/start-wol-watch.py --minutes 40
# 然后去 Windows 正常关机（不是重启、不是睡眠）
python3 /mnt/dm-0/astrbot/_molt/tools/stop-wol-watch.py
```

结果落在 `/mnt/dm-0/astrbot/_molt/wol-test/result-<stamp>.json`。

**怎么判断**：

- **起来了** → PASS，看 `wake` 那条日志距 `offline` 多少秒
- **没起来，网口灯还亮** → 卡在 BIOS 或驱动，回第 1/2 步
- **没起来，网口灯是灭的** → S5 下网卡没电，多半 `ErP Ready` 没关

---

## 如果哪天真的不行了

不是死路。备选按优先级（Warden 的 `wake_backend` 本来就留了第二个实现的位置）：

| 方案 | 怎么做 | 代价 |
|---|---|---|
| **智能插座 + BIOS 来电自启** | NAS 给插座通电 → 主板「恢复供电自动开机」 | 买个插座（几十块）；**最可靠，完全不依赖网卡** |
| **S3 睡眠代替关机** | 不关机改睡眠，S3 下网卡供电更可靠 | 待机 3–8 W，仍比开机省很多 |
| **换 Intel i225/i226 网卡** | 这类网卡 WOL 支持普遍比 Realtek 稳 | 几十块钱 |
| **降级 R2 模式** | 不叫醒 Windows，鱼始终在 NAS，Windows 只当遥控台 | 违背初衷，但功能不残 |

> 「智能插座 + 来电自启」比 WOL 更原始，也更可靠。很多家用 AIO 就是这么远程开机的。

---

## 这一关真正留下的东西

**不是"能用"，是一条工程纪律。**

排查过程中同一个错误犯了三次——把「一个能解释现象的信号」当成因果，三次都被证伪或误报：

1. 读了**陈旧的** `HiberbootEnabled` 判断休眠状态（`powercfg /h off` 不会清它，永远是 1）
2. 把 `-match` 返回的**元素数组**当布尔用
3. 由 `PnPCapabilities` 的位**反推**唤醒开关（**已证伪：值 8 也能用**）

> **Windows 上判断 WOL 状态，只认 `powercfg /devicequery wake_armed` 的输出本身。**
> 给用户看的脚本，判断逻辑必须先在本机真跑一遍并做证伪实验，不能"看起来对"就交付。
