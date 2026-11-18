# 两份演练：陌生人照着敲就能重跑

这两份演练回答两个具体问题：

| 演练 | 它回答什么 | 会不会伤到东西 |
|---|---|---|
| `tools/drill-dsh-chain.py` | 「鱼在 NAS 上还能不能操作那台 Windows 的 DSH」这条链现在通不通 | **不会**。只读 + 一个注定失败的 `session/prompt`（打不存在的会话，一个字都发不出去） |
| `tools/drill-agent-death.py` | 「Windows 上的 agent 死了，会不会有人发现、能不能拉回来」 | **会杀一次本机 agent**（只在你加 `--yes` 时）。不碰电源、不碰防火墙、不碰 NAS、不搬家 |

> 两份都**不会**：关机、重启、改防火墙、搬家、往任何真实会话发消息。
> 这条边界由 `molt/tests/test_drills.py` 钉着 —— 谁往里加一句电源/防火墙操作，测试先红。

---

## 0. 共同前置（30 秒，两条都要）

**① 站在仓库根目录**（不在会报 `can't open file 'tools/drill-...'`）：

```powershell
cd C:\Users\13553\Downloads\DSH_workspace\molt
```

**② 拿到 token**（两份都要；这是密钥，别外传）：

```powershell
(Get-Content tools\molt-agent.json -Raw -Encoding UTF8 | ConvertFrom-Json).warden_token
```

> `-Encoding UTF8` 不能省：这个文件是 UTF-8 带中文注释，PS 5.1 默认按 ANSI 读会报
> 「传入的对象无效」（2026-11-18 实测）。

**③ 确认那台机器上的 agent 活着**（第二节的链全靠它，它死了 6191 就不在）：

```powershell
curl.exe -s -m 5 -o NUL -w "%{http_code}`n" http://127.0.0.1:6191/health     # 期望 200
```

如果返回 `000`（连不上），先把 agent 拉起来，等 3 秒再试：

```powershell
Start-Process wscript.exe -ArgumentList "\"$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\molt-agent.vbs\""
```

> **别用 `| Select-Object -First N` 截断这两份脚本的输出**：管道提前关闭会让
> `$LASTEXITCODE` 变成 `-1`，看起来像"脚本失败了"（2026-11-18 实测）。
> 要看前几行就用 `py ... --plan`（它本来就短），或者把输出重定向到文件再 `Get-Content 文件 -TotalCount 5`。

---

## 1. 演练 A：D18 那条链（14 项）

### 跑

```powershell
$tok = (Get-Content tools\molt-agent.json -Raw -Encoding UTF8 | ConvertFrom-Json).warden_token
py tools\drill-dsh-chain.py --warden-url http://192.168.50.50:6186 --warden-token $tok
```

（想给机器读就加 `--json`。）

### 成功长这样（真机 2026-11-18 08:32，14/14）

```
✓ peer /health —— HTTP 200 {"ok": true, "service": "molt-dshpeer", ...}
✓ peer 无 token → 401 —— HTTP 401
✓ peer session/list —— 184 个会话
✓ peer session/events —— session=session-b0639458-cbc… records=14
✓ peer session/prompt（打不存在的会话） —— 对面小口拒绝了（HTTP 502）… session/not-found
✓ peer session/create → 400 —— HTTP 400 …只允许这三个方法…
✓ peer session/cancel → 400 —— HTTP 400 …
✓ peer 忽略调用方指定的目标 —— HTTP 200（仍回本机结果）
✓ warden 无 token → 401 —— HTTP 401
✓ warden session/create → 400 —— HTTP 400 …
✓ warden session/list —— HTTP 200 184 个会话
✓ warden session/events —— HTTP 200 session=session-b0639458-cbc…
✓ warden session/prompt（不存在的会话） —— HTTP 502 … session/not-found
✓ CLI molt dsh sessions —— rc=0

全部 14 项通过：NAS 账本 → agent:6191 → 127.0.0.1:3082 这条链是通的。
```

### 失败长什么样 / 怎么恢复

| 你看到 | 意思 | 怎么办 |
|---|---|---|
| `✗ peer /health —— HTTP None` | 6191 没了：agent 不在 | 用 §0 ③ 那条 `Start-Process wscript.exe …molt-agent.vbs` 拉起来，等 3 秒重跑 |
| `✗ peer 无 token → 401 —— HTTP 200` | **鉴权坏了**（比连不上严重） | 别忽略：说明 peer 放行了匿名请求，去看 `molt/dshpeer.py` 的 `_authorized` 与测试 |
| `✗ warden … 401` 而 peer 是好的 | token 不对/过期 | 重新取 token（§0 ②）；账本那台机器的 `/mnt/dm-0/astrbot/_molt/warden.token` 才是源头 |
| `✗ CLI molt dsh sessions —— rc=2` 且输出「连不上」 | 两跳里有一跳断 | 先看上面哪一行 ✗；若 peer 好而 warden 报超时，多半是账本上那台的 `dsh_relay` 指错了端口（应为 `6191`） |
| 整条命令 30 秒以上才失败 | 目标地址不可达（黑洞/关机） | 这是**正确**的失败方式；确认那台机器是不是真的关着 |

---

## 2. 演练 B：agent 死了会怎样（8 项）

### 先看它想干什么（不动手，默认行为）

```powershell
py tools\drill-agent-death.py --plan
```

### 真跑（**会杀掉本机 agent，然后拉回来**）

```powershell
py tools\drill-agent-death.py --yes
```

`--yes` 之外它一律不动手 —— 杀进程这件事不该由脚本替人决定。

### 成功长这样（真机 2026-11-18 08:35，8/8）

```
✓ 前置 peer /health —— HTTP 200
✓ 前置 agent 进程 —— pid=[22772]
✓ 杀掉之后 peer 不可达 —— HTTP None
✓ 杀掉之后 agent 进程消失 —— pid=[]
✓ 断链时 CLI 报错能看懂 —— rc=1 ✗ Fatal: 连不上 Warden（…），试了 3 次。最后一处错误：timed out
✓ 守卫报 agent-dead —— 这一轮新告警 kinds=['agent-dead']
✓ 拉起后 peer /health 200 —— 0.5 秒（含进程启动）
✓ 守卫报 agent-back —— 这一轮新告警 kinds=['agent-dead', 'agent-back']
```

### 失败长什么样 / 怎么恢复

| 你看到 | 意思 | 怎么办 |
|---|---|---|
| `✗ 前置 peer /health —— HTTP None` | 一开始就没跑起来 | 先按 §0 ③ 拉起 agent，再演练；否则"杀掉"这一步没有意义 |
| `✗ 前置 agent 进程 —— pid=[]` | 同上 | 同上 |
| `✗ 守卫报 agent-dead —— kinds=[]` | 告警没写进账本 | 看该行后面的 `guard 输出`：若是 `写账本失败：…` → 网络/token 问题；若是 `同一类告警 N 秒前刚报过` → 去重生效，等 30 分钟或手工删 `tools/molt-agent-guard-state.json` 再跑 |
| `✗ 拉起后 peer /health 200 —— 90 秒内没回来` | agent 起不来 | 手动 `Start-Process wscript.exe …molt-agent.vbs`，然后 `tail G:\molt-fish\logs\agent.log.err` 看栈 |
| 演练中途断电/中断 | 可能留下"agent 已杀、没拉起" | **它是本机唯一会造成这种状态的演练**：手动跑 §0 ③ 那条命令，再 `curl …/health` 确认 200 |

> 恢复判断只有一条：**`curl http://127.0.0.1:6191/health` 回 200**。别的都是辅助。
> 如果连守卫也没了：`Start-Process wscript.exe "…\Startup\molt-guard.vbs"`。

---

## 3. 一张表：出问题先看哪里

| 症状 | 先看 |
|---|---|
| 鱼在 NAS 上"操作不了那台机器" | 演练 A；`curl 127.0.0.1:6191/health` |
| 早上 9 点那条自动策略没动 | 账本 `/status` 的 `heartbeats`（agent 心跳）与 `attempts` |
| 机器"叫不醒/关不掉" | `molt machine list`；`docs/ADMIN-D20.md`；`docs/DECISIONS.md` 的 D19/E4 |
| 演练本身报错 | 先看这张表上面的"失败长什么样"，再看 `docs/VERIFY.md` 的 L / M 两行 |

**红线（两份演练都不越）**：不关机、不断电、不碰防火墙、不搬家、不往真实会话发消息。
