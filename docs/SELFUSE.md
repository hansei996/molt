# 自己用：一页纸

这份文档只解决一件事：**你自己要用它的时候，敲哪几条命令。**

设计文档在 [ARCH.md](ARCH.md)，开源规划在 [PLAN.md](PLAN.md)。这里不解释原理，
只给命令、给现象、给怎么收场。

---

## 两台机器

| | 谁 | 在哪 | 角色 |
|---|---|---|---|
| 家 | `UGREEN-776B` | `192.168.1.10`，容器 `astrbot` | 24 小时开着，**默认住这儿** |
| 游戏机 | `DESKTOP-1A2B3C4D` | `192.168.1.20` | 要直播/打游戏时才叫过来 |

| 东西 | NAS 上的位置 | Windows 上的位置 |
|---|---|---|
| 它的 data | `/mnt/dm-0/astrbot/data` | `G:\molt-fish\data` |
| molt 代码 | `/mnt/dm-0/astrbot/_molt/pkg` | `C:\Users\13553\Downloads\DSH_workspace\molt` |
| 账本（Warden） | `http://127.0.0.1:6186` | `http://192.168.1.10:6186` |
| 账本的 token | `molt-dev-token-CHANGEME` | 同左 |
| AstrBot 源码 | 容器里 | `G:\molt-p0\astrbot-src` |
| AstrBot 的 python | 容器里 | `G:\molt-p0\venv313\Scripts\python.exe` |

> NAS 上要跑 molt，先进目录并设 `PYTHONPATH`：
> ```sh
> cd /mnt/dm-0/astrbot/_molt/pkg && PYTHONPATH=.
> ```
> 后面所有 NAS 命令都假设你已经 `cd` 过去了。

---

## 三条铁律

1. **同一时刻只能有一只鱼在线。** 不是"最好只有一个"，是必须。
   你发一条消息收到两份回复、记忆分裂成两份打架的副本——这个项目就不成立了。
2. **`molt land` 落位之后，平台是关着的。** 这是故意的：`appid` 能跟着灵魂走，
   但"要不要开始说话"不能。所以每次落位之后都要手动打开一次（下面第 4 步）。
3. **`molt status` 说"没有租约"不等于"没有壳在跑"。** 租约只在迁徙那几十秒里存在。
   确认"现在真的没有别的壳在说话"，要看容器/进程，不能只看账本。

---

## 去程：把它叫到 Windows

### ① NAS 上：先让它优雅闭嘴（不是 kill -9，不是 docker stop）

```sh
docker kill -s INT astrbot
```

等 5 秒，确认它真的退出了，并且 WAL 已经归零：

```sh
docker inspect astrbot --format 'running={{.State.Running}} exit={{.State.ExitCode}}'
ls -la /mnt/dm-0/astrbot/data/data_v4.db*
```

要看到 `running=false exit=0`，并且 `data_v4.db-wal` 是 **0 字节或不存在**。
日志里应该有 `🌈 Shutting down AstrBot...`。

> **为什么不是 `docker stop`**：实测 `docker stop`（SIGTERM）退出码 137、
> `-wal` 41 KB 原封不动 —— 未合并的事务留在文件里，等于把"半句话"搬过去。
> **为什么不是 `docker kill`**：那是 SIGKILL，一样不 checkpoint。

### ② NAS 上：拍快照、交给新壳

```sh
PYTHONPATH=. python3 -m molt.cli migrate \
  --data /mnt/dm-0/astrbot/data \
  --shell nas-shell \
  --target win-shell \
  --transport warden \
  --warden-url http://127.0.0.1:6186 \
  --warden-token molt-dev-token-CHANGEME
```

**记下输出里的 `generation N`**，下一步要用。

正常输出长这样（真机实测 1.4–1.7 秒）：

```
· 拿到租约 epoch=9
· 热备 data_v4.db  10.4 MB  0.129s  21 张表 quick_check=ok
· 热备 memory_companion.db  16.9 MB  0.219s  41 张表 quick_check=ok
· 打包 4.6 MB 4 个文件
· generation 7  sha256 b0c0fa50…
✓ 已登记
✓ 已交还租约——目标壳现在可以接管了
```

> 会有一条警告「没有给 `--control-url`：这次迁徙不会让源壳停下来」。
> **这条警告在这里是对的**：你已经把容器杀了，源壳本来就不在说话。
> `--control-url` 是给"不想杀进程、只想让它闭嘴"的场合用的。

### ③ Windows 上：落位

```powershell
cd C:\Users\13553\Downloads\DSH_workspace\molt
$env:PYTHONPATH="."

py -3.13 -m molt.cli land --generation 7 --data G:\molt-fish\data `
  --warden-url http://192.168.1.10:6186 --warden-token molt-dev-token-CHANGEME `
  --shell win-shell
```

**先不带 `--yes` 跑一遍**，它会告诉你"会移动哪些文件"。看过没问题再加 `--yes`：

```powershell
py -3.13 -m molt.cli land --generation 7 --data G:\molt-fish\data `
  --warden-url http://192.168.1.10:6186 --warden-token molt-dev-token-CHANGEME `
  --shell win-shell --yes
```

你会看到「机器覆写层（Windows）」的 7 行，其中第一行是：

```
✓ 关闭平台适配器：default_1234567890（这个壳刚接过 soul，还不能确定自己是唯一在线的那个；确认后再手动打开）
```

> `py -3.13` 是本机那个 3.13.5（sqlite 3.49.1）。用 Python313 的绝对路径也行：
> `C:\Users\13553\AppData\Local\Programs\Python\Python313\python.exe`

### ④ Windows 上：让它开口

```powershell
py -3.13 tools\platform-switch.py on G:\molt-fish\data\cmd_config.json --yes
```

不带 `--yes` 是 dry-run，只告诉你会改什么。输出 `✓ 已改：default_1234567890 → enable=True` 才算成。

> 这一步为什么不能自动：**"现在开始对外说话"必须是人的决定。**
> 落位那一刻你还不能确定自己在 NAS 上真的关干净了——第 ① 步就是让你确定这件事。

### ⑤ Windows 上：起壳

```powershell
& "G:\molt-p0\venv313\Scripts\python.exe" tools\boot-keepalive.py `
    "G:\molt-fish\data" 6199 "G:\molt-fish\logs\astrbot.log"
```

会弹一个控制台窗口，那就是它。看到这些才算活：

```
Starting WebUI at http://127.0.0.1:6199
Loading plugin astrbot_plugin_molt ...
[Molt] 控制面已注册 4 个端点
[QQOfficial] Websocket session starting.
AstrBot started.
```

**然后给它发一条 QQ 消息。** 有回复就是真的成了——不是"配置正确"，是网络那头认了它。

自检：

```powershell
py -3.13 -c "from molt.control import AstrBotControl; c=AstrBotControl.from_config('http://127.0.0.1:6199','G:/molt-fish/data/cmd_config.json'); s=c.status(); print('host=',s['host'],'speaking=',s['speaking'],'platforms=',s['platforms']['connected_ids'])"
```

> 路径用正斜杠 `G:/…`。写成 `'G:\molt-fish\…'` 会触发
> `SyntaxWarning: invalid escape sequence '\m'` —— 不影响结果，但那一行会直接报上去，
> 让人以为是出了别的问题。

应该看到 `host= DESKTOP-1A2B3C4D speaking= True platforms= ['default_1234567890']`。

没有壳在跑的时候它会明确说「连不上 AstrBot 控制面……那个壳在跑吗？端口对吗？插件装了吗？」——
**不会含糊地报一个超时**。

---

## 归程：送它回 NAS

### ① Windows 上：让它优雅停机

**切到那个控制台窗口，按 `Ctrl+C`。**

要看到 `🌈 Shutting down AstrBot...` 和 `知识库数据库已关闭`，窗口自己关掉。

> 不要直接点窗口右上角的 ×，也不要 `Stop-Process`。那两个等于 SIGKILL，
> WAL 不会 checkpoint。按 Ctrl+C 才是它自己能处理的信号。

确认 WAL 归零：

```powershell
dir G:\molt-fish\data\data_v4.db*
```

`-wal` 应该是 0 字节或不存在。

### ② Windows 上：拍快照

```powershell
cd C:\Users\13553\Downloads\DSH_workspace\molt
$env:PYTHONPATH="."

py -3.13 -m molt.cli migrate --data G:\molt-fish\data `
  --shell win-shell --target nas-shell `
  --transport warden `
  --warden-url http://192.168.1.10:6186 --warden-token molt-dev-token-CHANGEME
```

**记下 `generation N`。**

### ③ NAS 上：把容器停掉（**这一步不能省**）

`/molt confirm` 只让 NAS 这只鱼**闭嘴**，容器本身一直好好跑着 —— 它还开着数据库连接。

```sh
docker kill -s INT astrbot
docker inspect astrbot --format '{{.State.Running}} {{.State.ExitCode}}'
```

要看到 `false 0`。

> **不是 `docker stop`。** SIGTERM 在它那儿不触发优雅停机（实测退出码 137、`-wal` 41 KB 原封不动）。
> `-s INT` 才会走到 `🌈 Shutting down AstrBot...` 和 `知识库数据库已关闭`。

为什么这一步必须做：容器活着的时候 `data_v4.db-shm` 还在（WAL 的共享内存索引，只有活着的连接会建它）。
这时候落位会把文件换掉，而那个进程继续往**被移走的旧 inode** 上写 —— 账本说第 8 代已落地，
运行中的进程还在读写第 6 代。**这不是崩溃，是两边各说各话。**

`land` 现在会自己查这件事，查到了就拒绝并告诉你怎么办。所以如果你看到这句，就是漏了这步：

```
✗ 目标 data 目录看起来正在被一个活着的 AstrBot 使用：
    data_v4.db-shm 还在（32768 字节）—— WAL 的共享内存索引，只有活着的连接才会建它
```

### ④ NAS 上：落位

```sh
cd /mnt/dm-0/astrbot/_molt/pkg
PYTHONPATH=. python3 -m molt.cli land --generation 8 --data /mnt/dm-0/astrbot/data --yes
```

这里不需要 `--warden-url`（Warden 就在本机，本地路径直接读中转区）。

落位时它会顺手做**机器覆写**：把 `platform[].enable` 关掉、dashboard 改回 `0.0.0.0:6185`、
换成新的 `jwt_secret`。**所以下一步必须手动把平台打开** —— 这是刻意的，"要不要开始说话"是人的决定。

### ⑤ NAS 上：让它开口

```sh
cd /mnt/dm-0/astrbot/_molt/pkg
PYTHONPATH=. python3 tools/platform-switch.py on \
    /mnt/dm-0/astrbot/data/cmd_config.json --yes
```

> 如果 `pkg/tools/` 里没有这个脚本，把仓库的 `tools/platform-switch.py` 拷一份过去：
> `scp -P 922 tools/platform-switch.py root@192.168.1.10:/mnt/dm-0/astrbot/_molt/pkg/tools/`

### ⑥ NAS 上：起容器

```sh
docker start astrbot
```
```

18 秒左右 WebUI 起来。确认它在说话：

```sh
cd /mnt/dm-0/astrbot/_molt/pkg && PYTHONPATH=. python3 -c "
from molt.control import AstrBotControl
c = AstrBotControl.from_config('http://127.0.0.1:6185', '/mnt/dm-0/astrbot/data/cmd_config.json')
s = c.status()
print('host=', s['host'], 'speaking=', s['speaking'], 'platforms=', s['platforms']['connected_ids'])
"
```

`speaking= True`、`platforms= ['default_1234567890']` 就对了。

### 到这里就结束了 —— 关机是另一件事

鱼已经在 NAS 上活着了。**Windows 关不关，跟它回没回家没有关系。**

- 想省电、想让它睡 → 关掉。以后要用再说「叫醒它」（`molt wake`，实测 23 秒）
- 机器前有人正在打游戏 / 直播 / 写着没存的文件 → **就别关**，鱼已经完全不受影响
- 什么时候关都行，今天关、明天关、一直开着，都不影响上面这 6 步的结果

**这一条是刻意的设计**：交接的判据是「账本接受了那份快照」，不是「Windows 关机了」。
如果把两者绑在一起，那台电脑只要有人用着，鱼就回不了家 —— 而它其实早就到家了。
（详见 `ARCH.md` §5.4 / §5.5。）

---

## 它现在在哪

以前这个问题只能"问它"。现在有三条路，都不用问。

### ① 在 QQ 里敲一句

```
/molt where
```

回的是这种：

```
我现在在 **DESKTOP-H2KGIAO**。

· desktop-h2kgiao：在跑，最后心跳 4 秒前
· ugreen-776b-8b7896df：在跑，最后心跳 7 秒前
```

`where`（`where` / `在哪` / `在哪呢`）**只回答"在哪"**。

想看账本就说：

```
/molt status
```

它在这上面再加三段 —— 账本第几代、租约在谁手上、最新一代是谁推给谁的：

```
账本 epoch 15，已登记 13 代。
现在没有租约——没有任何壳在搬家的路上。
最新一代 13：desktop-h2kgiao → ugreen-776b-8b7896df，28624617 字节。
```

> 早先这两个词被并成一套（五个词一份输出），结果是**名字暗示两种内容、
> 实际只有一份** —— 一个自称 `where` 却回了一大段账本数字的命令，
> 比没有这个命令更让人困惑。现在 `status` 是 `where` 的超集，各自算数。
> 另外 `enabled=false` 只关"搬家"，不关"看一眼"：查询是只读的，装上就该能用。

### ② 手机浏览器打开一个页面

```
http://<NAS 的地址>:6186/where?token=<warden.token 的内容>
```

一页纸，没有任何外部资源（不会因为一次加载失败变成白屏）：

| 壳 | 状态 | 最后心跳 | 代 | 说明 |
|---|---|---|---|---|
| ugreen-776b-8b7896df | 在跑 | 3 秒前 | 0 | homer |
| desktop-h2kgiao | 在跑 | 1 秒前 | 13 | agent |

心跳超过 **10 分钟**没更新就显示「哑了」—— 阈值故意放宽，是为了不把
一次 GC、一次休眠唤醒误报成"鱼死了"。

> `/where` 是唯一一个可以用 `?token=` 的端点，而且**只认这一个路径**。
> 一个能改租约的 token 出现在 URL 里会被记进浏览历史、被转发、被截图，
> 所以这个口子必须窄到只剩一个只读页面。

### ③ 在命令行看账本

```
molt status
```

## 每十分钟留一份底：`backup` / `backups` / `recover`

迁徙只在两个时刻发生，而鱼在这中间说了一整晚的话 —— 那些话只写进**当前这台机器**的磁盘。
如果 Windows 被拔电，能回到的最新状态是「它落地那一刻」。

这三条命令把那段空窗切成十分钟一段。

```
# 现在就存一份（三个守护进程也会按 backup_interval 自动做，默认 600 秒）
molt backup --data <这台机器的 data 目录> \
            --warden-url http://<NAS>:6186 --warden-token <token>

# 看看手上有哪些底
molt backups --warden-url http://<NAS>:6186 --warden-token <token>

# 回到某一份（它会先下载、校验指纹，然后走和 land 完全相同的落位路径）
molt recover --backup backup-20261114T204024-desktop-h2kgiao.tar.gz \
             --data <目标 data 目录> --yes
```

三个必须知道的事：

1. **备份不是交接。** 它不进 `generations`，不占租约，也不会出现在 `molt status`
   的迁徙历史里。`molt backups` 里能看到，账本里看不到 —— 这是刻意的：
   一份"目标是空"的交接记录会让壳侧的 `decide()` 卡住，或者被误当成自己的落位任务，
   **拿去覆盖它正在写的那个库**。
2. **默认只留最近 12 份**（约 54 MB）。更旧的会被自动清掉，目录是
   `<warden-root>/backups/`，每份旁边有一个同名 `.json` 记着字节数、指纹、来自哪个壳。
3. **`recover` 和 `land` 是同一条路**。它只是多了"下载 + 校验"两步，
   后面原封不动交给 `land` —— 落位是这套系统里最危险的一次操作，不该有第二种实现。

真机上两份底长这样：

```
backup-20261114T203912-ugreen-776b-8b7896df.tar.gz   6.9 MB   来自 ugreen-776b-8b7896df
backup-20261114T204024-desktop-h2kgiao.tar.gz        7.3 MB   来自 desktop-h2kgiao
一共：2 份 / 14.2 MB
```

第二份比第一份大 0.5 MB —— 那正是它在 Windows 上落地之后又多说的那些话。

## 心跳是什么，为什么需要它

账本原来的每个字段回答的都是**过去**："谁取走了第几代"。
没有任何一个字段回答"现在谁还在跑"。

结果是鱼被拔电之后，`molt status` 看起来一切正常 —— 它分不清
**"鱼死了"和"鱼闲着"**。而 NAS 的 homer 这时已经按公理 0 把自己
关掉了，于是**两边都没有鱼，且没有任何一方会报警**。

现在三个守护进程每轮循环都往账本报一次"我还在"（`POST /heartbeat`），
账本只保留最近 8 个壳、每个壳只有一条记录。它失败不抛异常 ——
**一次心跳丢了不该影响任何交接**。

## 搬完之后，它会自己说一句

到了新壳上，它开口说的第一句话之前会先单独发一条：

```
（我刚从 UGREEN-776B 搬过来，这是第 15 代。）
```

然后才是正常回复。为什么值得加这一条：QQ 官方机器人的**资料页、昵称、签名
只能在开放平台后台改**，程序没有接口。所以"它现在换了台机器"这件事，
只有它自己说出来才到得了你眼前。

四条约束，缺一条就不要这个功能：

- **只在一个 30 分钟的窗口里说一次**。第二天早上还说"我刚搬来"是噪音。
- **落地那一代的来源与代号必须能在账本里查到**。查不到就闭嘴 ——
  编一个"我刚从 NAS 搬过来"是伪造。
- **问不到账本也闭嘴**，最多试 3 次就放弃。每一次都是带超时的 HTTP 请求，
  不封顶的话最坏情况是你说一句话先等 5 秒。
- **发失败要重试，但成功了就绝不再说**。这句话记的账是"送达"，不是"组装"：
  平台挡下来（主动消息限制、`msg_id` 复用、网络抖动）时保留在下一次开口重试，
  一旦发出去就永久不再重复。早先的写法是"组装好就算说过"，
  结果一次失败 = 这一代永久沉默 —— 而失败恰恰是最该重试的那一次。

> 这条不是"插进它回复的第一行"，是**单独一条消息**。
> 早先确实插进过回复开头，日志里也打了"已播报"，但人一个字都没看见 ——
> AstrBot 的 `result_decorate` stage 在流式输出下走的是
> `if is_stream: return`，而流式是默认开着的（WebChat 显式开启，
> QQ 官方适配器没关）。文字早就一块块推给平台了，
> **往那个 chain 里插东西，等于插进一个不会再被发送的对象**。
> 教训：标记"说过了"的时机，必须在"真的送到"之后。
> 同一个病根后来又在同一条链上犯了一次（组装时就记账），
> 而且是**被一条把缺陷写成规格的测试固定住的** ——
> 那条测试叫 `test_plugin_marks_state_even_when_the_send_fails`，
> docstring 写着"发不出去也不能每分钟重试一次"。已经删掉换成相反断言的用例。

**真机验到了（2026-11-15 06:57 · QQ 私聊）**：

![它自己说了一句：我刚从 UGREEN-776B 搬过来，这是第 19 代](assets/qq-arrival-announce-2026-11-15.png)

同一时刻 `data/plugins/astrbot_plugin_molt/arrival_state.json` 是：

```json
{ "announced_generation": 19, "announced_at": "2026-11-15 06:57:33", "last_detail": "announced" }
```

两件事对得上才算验过：**人看见了那句话**（上面这张截图），
**账本记的是"送达"不是"组装"**（`mark()` 现在只在 `event.send()` 正常返回之后才执行）。
只有前者，说明不了它是被正确记账的；只有后者，说明不了它真的到了人眼前 ——
这正是这条功能前后错过两次的地方。


## 它能自己说「我想搬」了

以前搬家只可能由你发起：/molt go → /molt confirm。它自己**一句话都插不上** ——
真机上它就是这么答的：「不会。我自己动不了……这个过程中我插不上手。」

现在它有了**提议权**（默认仍然是关的）：

`json
{ enabled: true, llm_tool_enabled: true }
`

打开 llm_tool_enabled 之后，模型可以在对话里调 molt_propose_go_home，
于是你问「你想不想回家」，它真的能回答想并且把提议写下来：

`
我想搬去 **ugreen-776b-8b7896df**。
理由：这边要关机了，NAS 本来就该常开。
你说一句 /molt confirm，我就动身；/molt cancel 就算了。
这条提议 30 分钟内有效 —— 你说要搬，才是搬。
`

四条规矩，缺一条就不要这个口子：

- **目标必须在账本里真的存在**。它编一个名字出去，结果是那台机器永远等不到自己那一代，
  而且**不会报错**。拒绝时它会把账本里现在有哪几台列出来。
- **提议里记住目标**。它挑的是 A、配置里写的是 B 时，confirm 必须搬去 A ——
  只记有一个提议会让它搬去 B，而两边都不报错。
- **有别人持着租约时它不插队**。
- **执行那一步一个字没改**：仍然要你说 /molt confirm，仍然要管理员权限。
  模型能造成的最大后果是问了你一句。

> 现在这一层只到提议。/molt confirm 仍然必须由你说 ——
> 「它自己挑时间动身、你事后才知道」是 L2，还没做，也不会在没有你拍板前做：
> 见 docs/V0.6-PLAN.md 里那三个必须由你定的取舍（触发策略、否决窗口、出事谁负责）。

## 它记得住，但不会主动提起（这是刻意的）

搬完之后它会往 memory_companion 写一条自己的观察：

> ot_observed_activity · source_kind observed · owner self · 第 N 代 · 落在哪台机器

这条记录**永久、可追溯、归属正确**。但它**不会在聊天里突然提起来** —— 因为那一类记忆在
memory_companion 的合同里是 isibility=bot_self / sayability=indirect（私人档案，
按设计不注入对话）。真机上能直接看到：

`
selected_memory_ids: []
blocked_reasons: [{reason:prefiltered_out_of_search_range:89;...}]
injection_chars: 0
`

这是一个**产品决定，不是缺陷**（2026-11-15 定案 A，见 docs/ARCH.md §14.4）：

- **A 接受为档案**（采纳）：记录的价值在可追溯，不在被复述。它认账，但不念叨。
- B 抬进检索档位（否决）：要改对方插件的行为，副作用是它可能在不相干的对话里突然提起自己搬过家。

想让它说的时候，直接问就行 —— 「你还记得自己是怎么到这台机器上的吗」这类问题它会去翻档案；
答案常常是记着这件事但说不出细节，那是档案与对话的区别，不是它撒谎。

这条决定有一条测试钉着：molt/tests/test_memory_tier.py 断言我们**永远不覆盖**
isibility / sayability。谁想改成 B，必须先改那个文件 —— 因为不做某件事的决定
最容易在以后被顺手做掉。

## 到了新壳上：管理页面、插件

### 管理页面：一样的界面，不一样的地址

| 在哪只壳上 | 打开这个 |
|---|---|
| NAS（容器里） | `http://<NAS 的地址>:6185` |
| Windows（裸机） | **`http://127.0.0.1:6199`** —— 只能在那台 Windows 上开 |

**账号密码不用改**：`dashboard.username` 和 `pbkdf2_password` 是随灵魂一起搬过来的，原来那个密码直接能用。

**但要重新登录一次**：`jwt_secret` 在每台新壳上都会被重新生成（覆写表第 6 条）。旧 cookie 在这边无效 ——
共用同一个 secret 意味着攻破 Windows 就等于攻破 NAS，不值得图那点方便。

**Windows 上只监听 `127.0.0.1`，这是刻意的**：
NAS 上写 `0.0.0.0` 安全，是因为它在容器网络里隔离着；一台裸 Windows 上写 `0.0.0.0`
等于把管理页开给全家 —— 而**这个页面能 drain 掉鱼、能看到它的全部记忆**。

> 想从别的机器访问，别改配置。用 SSH 隧道或者就直接在那台机器上开。

### 插件：代码不跟着走，数据跟着走

这是最容易踩的一脚。迁到新壳以后，`data/plugins/` 里**几乎是空的**。

因为搬运清单（`soul.py` 的 `COPY_ARTIFACTS`）是：

```
cmd_config.json  mcp_server.json  skills  workspaces  attachments  knowledge_base  plugin_data
```

**没有 `plugins/**`。** 插件代码被当作"壳"（跟这台机器、这个平台绑定的东西），
不是"魂"。而 `plugin_data/**` 是魂 —— 所以记忆库本体（比如
`plugin_data/astrbot_plugin_memory_companion/memory_companion.db`，17.7 MB）**是搬过来了的**，
只是**读它的那个插件没跟过来**。

现象：鱼有人格、有记忆数据，但没有那个负责注入记忆的插件 —— 它记得的少了，而且不报错。

**补法**（任选）：

```powershell
# 从旧壳拷一份（在 NAS 上打包 → 下到 Windows → 解到 data\plugins\）
# 很多插件是纯标准库的，拷过去就能跑；有额外依赖的要看它自己的 requirements
```

或者直接在管理页面的插件页里装。**但注意：这边装的东西，下次回 NAS 也不会跟着走** ——
同样因为 `plugins/**` 不在清单里。

> 已知的缺口，不是意外：插件代码动辄上百 MB、还常有平台相关的依赖，
> 全量搬过去很脆弱。但"缺了哪个插件新壳不会自己知道"这件事确实是个问题 ——
> 至少应该让 manifest 记下插件清单，让新壳能告诉你「你少了这几个」。
> v0.1 还没做。



任意一台机器上，问账本：

```powershell
py -3.13 -m molt.cli status --warden-url http://192.168.1.10:6186 --warden-token molt-dev-token-CHANGEME
```

```sh
PYTHONPATH=. python3 -m molt.cli status --warden-url http://127.0.0.1:6186 --warden-token molt-dev-token-CHANGEME
```

看到 `· 没有租约` + `最新 generation N 消费方 xxx` —— 意思是最后一份快照被谁取走了。
**它不告诉你"现在谁在说话"**，那个要自己看：

| 想知道 | 去看 |
|---|---|
| NAS 上那只在不在说话 | `docker inspect astrbot --format '{{.State.Running}}'` + 控制面 `/status` |
| Windows 上那只在不在说话 | 那个控制台窗口还在不在 + `http://127.0.0.1:6199` 的 `/status` |
| 两只都在说话（**出事了**） | 立刻关掉一只。关哪只都行，但**只关一只**。 |

---

## 回家也可以只敲一条命令（NAS 侧守护进程）

`/molt go` 让鱼**离开** NAS 是自动的。回来原本要敲四条命令 —— 现在也可以交给一个常驻程序。

**它比 Windows 侧那个多做了哪一件事**：落位前必须先把容器停掉。
`/molt confirm` 只让 NAS 这只鱼**闭嘴**（drain），容器还开着、数据库连接还在。
这时候落位会把文件换掉，而那个活着的进程继续往**被移走的旧 inode** 写 ——
账本说第 N 代落地了，它还在读写第 N-2 代。**这不是崩溃，是两边各说各话。**

### NAS 上：配置

`/mnt/dm-0/astrbot/_molt/pkg/tools/molt-homer.json`：

```json
{
  "execute": false,
  "shell": "nas-shell",
  "container": "astrbot",
  "data_dir": "/mnt/dm-0/astrbot/data",
  "molt_pkg": "/mnt/dm-0/astrbot/_molt/pkg",
  "platform_switch": "/mnt/dm-0/astrbot/_molt/pkg/tools/platform-switch.py",
  "warden_url": "http://127.0.0.1:6186",
  "warden_token": "molt-dev-token-CHANGEME",
  "interval": 10
}
```

**`execute` 默认是 `false`，先让它只报告。** 一个会在你没盯着的时候把生产容器停掉、
换掉数据库、再拉起来的程序，默认必须是"只说不做"。
先跑 `--once` 看它对不对，确认了再把 `execute` 改成 `true`。

```sh
cd /mnt/dm-0/astrbot/_molt/pkg
python3 tools/molt-homer.py --once        # 看一眼它想干什么
python3 tools/molt-homer.py               # 常驻
```

### 它每一轮的判决

| 账本状态 | 它做什么 |
|---|---|
| 有欠账（上一轮停下了但没起来） | **先起容器** —— 晚一代也认，不能让它两边都不在 |
| 还没有任何一代快照 | 什么都不做 |
| 最新一代不是给这台机器的 | 什么都不做 |
| 这一代我已经落过了 | 什么都不做（幂等） |
| 别人正在消费 / 有人攥着租约 | 等，不抢（公理 0） |
| 是给我的，且没人持租约 | 停容器 → 落位 → 打开平台 → 起容器 |

**落位失败时它仍然会把容器拉起来**，但**不会打开平台** ——
平台开着、库是旧的，等于鱼在说话但说的是上一代的事。开门之前先确认灵魂真的到了。

**它不做"关机"**：回家的判据是账本接受了快照，不是那台 Windows 关机了（`ARCH.md` §5.5）。

## NAS 重启之后，它自己会回来

NAS 上有一个 `tools/molt-supervise.py`，负责让三个常驻进程一直活着：

| 进程 | 干什么 | 死了会怎样 |
|---|---|---|
| `warden` | 账本，发租约 | 谁都搬不动了（但鱼还在 QQ 上说话，所以没人会发现） |
| `molt-homer` | 鱼回了 NAS，它负责停容器 → 落位 → 开平台 → 起容器 | 鱼回到家却是哑的 |
| `molt-bounce` | 鱼回家之后，等一会儿再把它推出去 | 关机之后机器醒不过来 |

**它装在两个地方**（两处都装是故意的）：

```sh
# /etc/rc.local 里（开机最末尾由 S95done 调）
/usr/bin/python3 /mnt/dm-0/astrbot/_molt/pkg/tools/molt-supervise.py --quiet --wait 60 >> ...
# crontab 里
@reboot     ... --quiet --wait 60 >> ...
*/2 * * * * ... --quiet           >> ...
```

那条每 2 分钟的是兜底：`@reboot` 有可能早于 `/mnt/dm-0` 挂载，而且"某个进程半夜挂了"
只有它会发现。

**两条规矩**（改这个脚本之前先读）：

1. **只补不杀**。在跑就跳过。别拿 `start-warden.py` 当看门狗——那个是"先杀掉旧的再起新的"，
   每 5 分钟打断一次 Warden，赶上一次迁徙就是数据事故。
2. **光看 pid 存在不算数**。重启后 pid 会重新分配，旧 pidfile 可能指到一个无关进程；
   所以它用 `/proc/<pid>/cmdline` 确认真的是那一个。

**它也会为"假成功"报错**：启动之后等 1.5 秒回头看，还活着才说"已启动"，
否则说"启动了 pid=N，但它立刻退出了（日志：…）"。这一条是真机重启时踩出来的——
第一次重启后日志写着三行"已启动"，两分钟后 `--status` 说三个都没在跑。

### 手动看一眼

```sh
ssh nas
python3 /mnt/dm-0/astrbot/_molt/pkg/tools/molt-supervise.py --status
```

输出应该是三行 `在跑 pid=…`。想看它在忙什么：

```sh
tail -n 20 /mnt/dm-0/astrbot/_molt/supervise.log
tail -n 20 /mnt/dm-0/astrbot/_molt/homer.log
tail -n 20 /mnt/dm-0/astrbot/_molt/bounce.log
```

### 重启 NAS 之后该确认的四件事

1. `--status` 三个都在跑
2. `netstat -ltn | grep 6186` 有监听
3. `curl -H 'X-Molt-Token: …' http://127.0.0.1:6186/status` 里 `epoch` / `latest_generation` 还在
4. **`docker inspect astrbot` 的 `running` 必须还是 `false`**
   （`unless-stopped` 不该把被 SIGINT 显式停掉的容器拉起来；它要是自己起来了，
   而你正在 Windows 上说话，那就是两条鱼同时在线）

## 出事了怎么办

### 退出码

| 码 | 意思 | 该做什么 |
|---|---|---|
| 0 | 成了 | — |
| 2 | 参数或环境不对（路径不存在、缺 token、transport 用错） | 看它打印的那一行，通常是路径打错 |
| 3 | 租约被别人占着 | 等 90 秒，或先查清另一只在干什么 |
| 4 | 热备或传输失败 | 看是不是有进程正拿着库、或网断了 |
| 5 | 叫不醒目标机器 / 协议版本不匹配 | 先 `molt wake` 单独试 |
| 6 | 这不是一份 molt 归档（或 sha256 对不上） | **别重试，先查归档是从哪来的** |
| 7 | 账本拒绝了（epoch 倒退等） | 说明有人拿着更旧的账本在写——查清是谁 |
| 8 | 没能按住源壳 | 已经中止，**没有**继续落位。源壳现在是哑的，用 `resume` 叫回来 |

### 常见症状

**「WebUI 能打开，但 QQ 不回消息」**
平台没打开。跑 `platform-switch.py show <data>/cmd_config.json` 看 `enable`。
如果是 `False`，这就是落位后的正常状态，`on` 一下然后重启壳。

**「两条命令都成功了，但谁都不说话」**
两只都哑了。检查两边的 `enable` 都是什么。

**「migrate 成功了，land 说 sha256 对不上」**
别硬来。取到的是坏归档，重跑 `migrate`（租约 90 秒，等它过期或者用 `--keep-lease`）。

**「Windows 上 AstrBot 起不来，日志里有 edge_tts / whisper 的 ModuleNotFoundError」**
这是**预期的**，不影响文字收发。新壳上那 51 个运行时包没装，掉的是语音。
真要语音，按 manifest 里的 `runtime_pip` 装一遍。

---

## 还可以更省事（但现在没有）

现在每台机器每次要敲 3 条。能进一步压缩，但都有代价：

| 想省掉 | 办法 | 代价 |
|---|---|---|
| `docker kill` / `Ctrl+C` 这两步 | 用控制面 `--control-url` 让 migrate 自己按住源壳 | 源壳进程不退出，只是哑了。少一层"它真的死了"的证据 |
| `platform-switch.py on` | 给 `land` 加一个 `--i-am-the-only-one` 参数 | 把"人的决定"变成"脚本的默认"，公理 0 少一道闸 |
| 两台之间手工搬 | Windows 配 SSH 密钥，一条命令打通两台 | NAS 的 sshd 现在是 `PubkeyAuthentication no`，要改配置 + 重启 sshd |

这三条都还没做。**先按现在这个流程跑几次，觉得哪一步真的烦了再动它。**

---

## 让它自己走：一条 QQ 消息（已实现，**还需要你在 Windows 上把看门人打开**）

到这一节为止，前面两条路径都是"你自己敲命令"。这一节是**你不敲命令**的版本。

### 它是怎么串起来的

```
你在 QQ 里: /molt go        →  鱼: "搬去 win-shell？120 秒内回 /molt confirm"
你在 QQ 里: /molt confirm   →  鱼: "我走了"（这条消息发完它才真的开始）
                              ↓ 两秒后
                            子进程跑 molt migrate：
                              唤醒 Windows（WOL，23 秒）
                              → 拿租约
                              → 把自己 drain 掉（闭嘴，进程还在）
                              → 热备 + 打包 + 上传
                              → 交还租约
                              ↓ Windows 上的 molt-agent 每 10 秒看一眼账本
                              → 发现"这一代是给我的"且没人持租约
                              → land → platform-switch on → 起 AstrBot
                              ↓
                            鱼在 Windows 上开口了
```

### NAS 这一侧（已经装好了）

插件在 `/mnt/dm-0/astrbot/data/plugins/astrbot_plugin_molt/`，配置在同目录的
`molt_command.json`：

```json
{
  "enabled": true,
  "shell": "nas-shell",
  "target": "win-shell",
  "data_dir": "/AstrBot/data",
  "molt_pkg": "/AstrBot/data/plugins/astrbot_plugin_molt/_vendor",
  "warden_url": "http://172.17.0.1:6186",
  "warden_token": "...",
  "control_url": "http://127.0.0.1:6185",
  "control_config": "/AstrBot/data/cmd_config.json",
  "wake_mac": "AA-BB-CC-DD-EE-FF",
  "wake_host": "192.168.1.20"
}
```

**三道闸**，缺一不可：

| 闸 | 是什么 | 为什么 |
|---|---|---|
| 权限 | `@permission_type(ADMIN)` | 群里任何人不该能搬你的鱼 |
| 两步确认 | `go` 只记提议，`confirm` 才动；提议 **120 秒**过期且只存内存 | 昨天随口一句，今天不该突然生效 |
| `enabled` | 默认 `false`，文件不存在就是没开 | 漏开 = 一条日志；漏关 = 它在你没盯着时搬了 |

指令：`/molt go`、`/molt confirm`、`/molt cancel`、`/molt status`、`/molt help`。
唤醒前缀是你 `cmd_config.json` 里的 `wake_prefix`（现在是 `/`）。

> **参数只认整词。**`/molt 我不想回家` 不会搬——宁可你多敲一个字，也不在这种事上猜。

### Windows 这一侧（**还没替你打开**）

文件：`C:\Users\13553\Downloads\DSH_workspace\molt\tools\molt-agent.json`。
现在 `"execute": false` —— 也就是**只看不做**。第一次先这样跑，确认它看懂了：

```powershell
cd C:\Users\13553\Downloads\DSH_workspace\molt
py -3.13 tools\molt-agent.py --once
```

它会打印一行判决，可能长这样：

```
  · execute 是 false（只观察，不动作）
23:30:37 [idle] 第 6 代我已经落过了
```

五种判决：`idle`（没我的事）/ `wait`（是我的，但别人还攥着租约）/
`land`（该落位）/ `start`（落过位但没起来，补启动）/ `error`（读不到账本，什么都不做）。

确认无误后，把 `execute` 改成 `true`，然后让它常驻：

```powershell
# 前台跑，看输出
py -3.13 tools\molt-agent.py

# 或者挂后台（关掉这个窗口也不影响）
Start-Process -WindowStyle Hidden py -ArgumentList "-3.13","C:\Users\13553\Downloads\DSH_workspace\molt\tools\molt-agent.py"
```

### 三条它绝不越过的线

| 线 | 意思 |
|---|---|
| 不是给我的，我不动 | `target_shell` 不等于 `win-shell` 就只是看着 |
| 有人攥着租约，我不落位 | 那次交接还没走完，现在落位等于在别人松手前抢东西 |
| 本机已经在跑，我不起第二只 | 端口在听就直接跳过——**一个壳里绝不允许有两只** |

### 出问题的时候看哪里

| 现象 | 看什么 |
|---|---|
| 发了 `/molt` 没反应 | 你不在 `admins_id` 里；或 `molt_command.json` 的 `enabled` 不是 true |
| 鱼说"我走了"然后没动静 | NAS 上 `docker logs astrbot \| tail -50` 找 `[Molt]`；再看 `/mnt/dm-0/astrbot/data/plugins/astrbot_plugin_molt/last-migrate.json` |
| 迁徙失败后鱼不说话了 | 它本来会自己 `resume` 回来；如果没回来，`curl -X POST` 那个 `/resume` 端点 |
| Windows 上鱼没出现 | 跑 `py -3.13 tools\molt-agent.py --once` 看判决；`wait` 说明租约没还 |
| 两个都在说话 | **立刻只关一个**（关 Windows 那个），然后查账本 |

> ⚠️ **这个循环还没在真机上完整跑通过一次。**NAS 侧的三道闸和指令层已经在容器里跑起来了
> （`/molt status` 能正确读到账本），Windows 侧的 agent 只在 dry-run 下验证过判决逻辑，
> 没有真的让它 land 过一代。第一次真跑时请盯着两边的日志。

## 关机 / 睡眠：这两件事差在哪

鱼走了以后，这台机器要不要关？先说结论：**区别不是省不省电，是"回来的时候要不要有人在键盘前"。**

| | 关机（`shutdown /s`） | 睡眠（S3） |
|---|---|---|
| 会话 | 销毁 | 保留 |
| 回来时要不要登录 | **要**（否则停在登录界面，Startup 不跑） | 不要 |
| 要不要先配好自动登录 | **要** | 不要 |
| 唤醒 | 魔术封包，实测 23.1 秒（S5） | 魔术封包，S3 这条待真机验 |
| 功耗 | 0 | 几瓦 |

**普通用户就能关机**（实测 `shutdown /s /t 600` 排定成功、`shutdown /a` 撤销成功），
但你**没有**权限直接调 `SetSuspendState` 睡眠 —— 不是特权缺失，是这台机器开着「离开模式」：

```
HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power
    AwayModeEnabled = 1
```

开着它，每一次睡眠请求都会被翻译成"屏幕关掉、整机继续跑"，而 API 依然返回 `TRUE`。
表现为：调用报成功、`Kernel-Power` 里没有 42（进入睡眠）事件、只有一条 59
「系统正在进入"离开模式"」、功耗一点没降。**这是"报成功但其实什么都没做"，比直接失败难查。**

一次性关掉（管理员）：

```
pwsh -File tools\molt-sleep-task.ps1
```

关掉之后，普通进程就能自己睡 —— agent 里的 `_suspend_now()` 会先
`AdjustTokenPrivileges` 解开 `SeShutdownPrivilege`（它在令牌里、只是被禁用了），再挂起。
`shutdown.exe` 干的就是这件事。

### 如果就是要关机

得先能自动登录，否则开机停在登录界面，agent 跑不起来。

```
powershell -File tools\molt-autologon.ps1 -Check      # 先看现在是什么状态
powershell -File tools\molt-autologon.ps1 -Disable    # 清掉（撤销）
powershell -File tools\molt-autologon.ps1 -Account "you@example.com" -Password "……"
```

⚠️ 这个脚本会把密码**明文**写进 `HKLM\...\Winlogon\DefaultPassword`。
更安全的做法是自己跑 `netplwiz`，取消「要使用本计算机，用户必须输入用户名和密码」，
然后自己输密码 —— 效果一样，密码存在 LSA secret 里而不是注册表。

⚠️ 另外：这台机器登的是**微软账户**，自动登录要的是**账户密码**，不是开机 PIN。
PIN 在 Winlogon 里没用（`net use /user:<用户> <PIN>` 返回 1326）。

## DSH 的端口：为什么插件的默认值 3080 在这里是错的

DSH 在本机有三类端口，性质完全不同，混起来会得出很荒谬的结论：

| 是谁 | 端口 | 会不会变 | 拿来干什么 |
|---|---|---|---|
| DSH 桌面应用自己起的 host | `0.0.0.0:3082` | 桌面壳启动时决定，**不写在任何配置文件里** | `POST /api/<namespace>/<method>` 的 RPC 网关 —— 插件要的是这个 |
| Web 界面 | `127.0.0.1:5859x` | **每次启动都变**（今天见过 52043 / 55221 / 58590） | 不带浏览器会话 cookie 一律 `401`，插件连它没用 |
| 独立版 `dsh web`（如果单独起） | 默认 `3080` | 固定 | 只有你真敲过 `dsh web --port 3080` 才存在 |

插件的文档默认值是 `http://127.0.0.1:3080`，因为它的作者假设你跑的是**独立版
`dsh web --port 3080`**。而这台机器跑的是**桌面版**，桌面版压根不听 3080 ——
于是插件一直报「远程计算机拒绝网络连接」，而这个报错长得非常像"DSH 没启动"。

**不要背端口号，去问 DSH 本人：**

```
powershell -NoProfile -ExecutionPolicy Bypass -File tools\dsh-port.ps1
```

它会枚举 DeepSeek Harness 在听的所有端口，逐个 `POST /api/session/list`，
只有真的答出 `{"type":"server-response", ...}` 的那个才算网关（`401` 的是 Web 界面，
`404` 的是静态资源），然后告诉你插件该写什么。加 `-Fix` 就直接改配置文件
（保留 BOM、先备份）。

判据是"它答得像不像 DSH 的 RPC 网关"，不是"端口通不通" ——
**通了的端口可能是别的东西**，而这个错误的表现是"连上了但每一条命令都失败"，
比连不上更难查。

改完配置**必须重启壳**：`AstrBotConfig` 在内存里缓存着，改文件不会热生效
（实测改完 `/dsh status` 还是报 3080）。重启后应当看到：

```
✅ HTTP 已连接：deepseek-account / deepseek-flash (version 0.2+ (Typert))
事件桥：已连接；待授权请求：0
```

### 现在它会自己纠偏

`tools/molt-agent.py` 每一轮（10 秒）都做一次上面那件事的**自动化版本**：
先按"答得像不像 RPC 网关"找出真正的端口，再看插件配置写的是不是它；
不一样就改回来，**并且重启壳**（否则内存里的旧地址还在用）。

真机实测（2026-11-15 07:05，把配置故意写回 3080 之后重启 agent）：

```
✓ dsh_connector 端口已改成 3082（原来写的是 http://127.0.0.1:3080）
· 壳已经按新配置停下，下一轮自愈会把它拉起来
第 19 代是我取的，但端口没人听 —— 我去把它拉起来
✓ 已拉起 pid=4108
```

三条设计约束：

- **保留 BOM**：这个配置文件是 `utf-8-sig`，写回去丢掉 BOM 会让 AstrBot
  下次把它当成另一个文件（这个坑本项目栽过一次）；
- **600 秒冷却**：发现不对 → 改 → 重启壳，这个过程本身会让壳短暂消失，
  没有冷却就会来回抖；
- **改不成就只抱怨**：探测不到网关、配置读不动、写不进去，都只记一行日志，
  不影响交接。端口对不对不决定鱼在哪。

顺带一提：`3082` 绑的是 `0.0.0.0` 而不是回环地址，等于把这个 RPC 网关对整个
局域网开放了。要收紧就在防火墙加一条只允许本机访问 3082 的入站规则 ——
**molt 没替你动防火墙**，那是你自己的机器。


---

## 如果它忽然不肯起来了：`.molt-restore-incomplete`

这是一个**故意的**拦阻。落位（`molt land` / 回家 / 出门）如果在中途断掉
（断电、盘满、AstrBot 的库被别人占着），目标 `data/` 里会留一个标记文件：

```
G:\molt-fish\data\.molt-restore-incomplete
```

只要它还在，agent 和 homer 都**拒绝启动壳**。因为那时候目标目录里可能是
"一半新、一半旧"—— 让它在那种状态下开口，它会带着半份记忆说话，而且
没有人看得出来哪里不对。

日志里会写成这样：

```
上一次落位没有做完（第 13 代，开始于 2026-11-15 06:39:49）：…\.molt-restore-incomplete 还在。
  现在起 AstrBot 会让它去读半份的库。先重跑一次 `molt land`
  （或让守护进程再落一次），收尾之后标记会自己消失。
```

**正确的处理是重跑一次落位**（同一份快照、同一个 `--data`），它会接着收尾，
收尾成功标记自己就没了。**不要手工删那个标记** —— 删掉等于告诉系统
"上次那半途而废的落位是完整的"，而它并不知道。

要确认现在是什么状态：

```bash
molt status                      # 看最近一代是谁取走的
ls -a <data 目录> | grep molt-   # 看标记还在不在
```

## 关于"原子"这件事，一句话

打包与落位现在都写 `.part` / `.molt-part`，校验通过才 `os.replace` 换成正式名字。
所以**中途失败不会留下一个"看着像成功"的文件**：

- 归档：要么完整存在，要么根本不存在（不会有半截的 `gen-000012.tar.gz`）；
- 落位：目标里每个文件要么没写、要么与快照逐字节相同（不会有截断的库）。

16 个故障场景的实测结果在 [`docs/FAULTS.md`](FAULTS.md)。
