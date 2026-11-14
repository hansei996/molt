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
