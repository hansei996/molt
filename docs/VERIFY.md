# 核对清单：哪些真机验过、哪些只有单测、哪些还没验

这份文件只回答一个问题：**"这条到底算什么程度的证据？"**

三档，别混用：

| 档 | 含义 | 能拿来对外说什么 |
|---|---|---|
| **真机** | 在两台真实机器上跑过，有输出/时间戳可引 | "它工作" |
| **单测** | 只有 pytest 覆盖，跑的是假对象或临时目录 | "这段逻辑按预期分支" |
| **未验** | 既没真机也没单测，或者只验了一半 | 什么都不能说 |

**当前计数（数出来的，不是估的）**：真机 **24** 条 = 13 + 4（A–D）+ 2（E–F）+ 2（H–I）+ 1（K）+ 2（L–M）；未验 **2** 条（G、J）。

**最近一次全量回归**：`1320 passed in 156.26s`（2026-11-18 09:12，本地 Windows / Python 3.13）。
这个数字由 `molt/tests/test_verify_doc.py` 钉住 —— 加行不改数字，测试会红。

最后有「怎么复跑」一节，照着敲就能重放。

---

## D18 · 跨机操作另一台的 DSH（两跳）

链路：`NAS warden :6186 /dsh` → `Windows agent :6191 /dsh` → `127.0.0.1:3082`

### 真机验过（2026-11-18，两台真机）

| # | 项 | 证据 |
|---|---|---|
| 1 | peer `GET /health` | 200，`{"ok":true,"service":"molt-dshpeer","methods":[...]}` |
| 2 | `session/list`（本机 peer） | 200，**184** 个会话 |
| 3 | `session/events`（本机 peer） | 200，`records=14`、`hasMore=true` |
| 4 | `session/prompt`（本机 peer） | `accepted: true`，且那条消息**真的作为用户消息出现在会话里**（02:39，内容一字不差） |
| 5 | 不带 token / 带错 token | **都 401**，同一句 `token 不对`（不提示差在哪） |
| 6 | 白名单外（`session/create`、`session/cancel`） | **400**，一句中文理由列出三个允许的方法；**两跳都一样** |
| 7 | 调用方试图指定目标（body 塞 `address`/`upstream`/`target` 指向 `10.255.255.1:9`） | **被忽略**，仍 200 且命中本机 3082 |
| 8 | 请求体不是合法 JSON | 400 `请求体不是合法 JSON` |
| 9 | NAS 那一跳 `session/list` / `session/events` | `ok=true`，184 / 14 条 |
| 10 | NAS 上 `molt dsh sessions` / `history` / `send --dry-run` | 三条都跑通；`--dry-run` 只打印目标/模式/内容，不发 |
| 11 | **agent 被杀之后小口回不回来** | 杀 agent → 6191 立刻 `http_code=000`；用 Startup 的 vbs 拉起 → **t+3s `/health` 200** |
| 12 | **真超时**（黑洞 `127.0.0.1:6300`，accept 后不回话） | 库层 **2.04s** 报 `连不上小口 …：timed out`；端到端 **32024ms** 后 `✗ Fatal: 连不上 Warden（…），试了 3 次。最后一处错误：timed out`，**退出码 2** |
| 13 | **缺 address 的报错** | `✗ 账本里没有 box 的地址。先声明一次：molt declare --shell box --address <IP>`，**退出码 2**，无 traceback |

第 13 条一开始是 `AttributeError: 'LocalBackend' object has no attribute 'dsh'` **且退出码 0** ——
已修，并加了一条**起真子进程、不替换任何模块**的测试钉住（断言：退出码非 0 / 有一句人话 / 没有 Traceback）。

### 真机验过（2026-11-18 04:40 那一轮补的，原先只有单测的四条）

| # | 项 | 怎么造的 | 结果 |
|---|---|---|---|
| A | `SEND_TIMEOUT = 60` | 黑洞 `127.0.0.1:6300`（accept 后不回话），直接调 `dshrelay.send_prompt(...)`，不给 timeout 参数（走默认） | **实测 60.0 秒**后 `DshRelayError`：`连不上 http://127.0.0.1:6300：timed out` |
| B | `MAX_BODY = 1 MiB` | 往 peer 塞 **1,536,092 字节**（1.5 MiB）的 body | **HTTP 400**，0.03 秒返回，文案 `请求体为空或太大` |
| C | 黑洞之后连接是否残留 | 上面那次 60 秒超时之后，数 `Get-NetTCPConnection -RemotePort 6300`；再杀掉黑洞、等 5 秒，再数 | 超时后 `FinWait2=1`；杀黑洞 5 秒后 **`no conns`**；随后正常 peer `/health` 仍 200（链路没被污染） |
| D | token 轮换期间的行为 | 用 `dshpeer.serve(token="rot-a", port=6305)` 起临时 peer → 用 rot-b 打；`stop()` 后换成 `serve(token="rot-b")` 再打 | 换之前：rot-a **200** / rot-b **401**；重启成 rot-b 后：rot-a **401** / rot-b **200** → **token 是启动时读的，轮换必须重启 peer**，旧 token 立刻失效 |

### 真机验过（原先列为「未验」的两条）

| # | 项 | 怎么造的 | 结果 |
|---|---|---|---|
| E | **从 NAS 容器内部**打这条链 | `docker run --rm --network host python:3.12-slim` 打 `http://127.0.0.1:6186/dsh`；再用**默认 bridge** 打 `http://172.17.0.1:6186/dsh`（插件真实的 `warden_url` 就是这个） | 两条都 **HTTP 200**、`ok=True`、**184 个会话** —— 容器内外都能通，不需要 host 网络 |
| F | `--steer` 模式 | 真发两条：`molt dsh send --steer "【molt steer 模式测试】…不用回复"`，分别打进两个空闲会话 | **`{'accepted': True}`**；事件流里看到插入记录 `"target": "next-step"`，且其中一个会话真的回了一句「收到，steer 消息已到达本链路…」 |

### 真机验过（暴露面：从 NAS 打过去，2026-11-18 04:50）

| # | 项 | 怎么打的 | 结果 |
|---|---|---|---|
| H | **DSH 的 3082 对局域网到底开着没有** | NAS 上 `POST http://192.168.50.63:3082/api/session/list -d '{}'`（不带凭据） | **401** `{"error":"unauthorized","message":"需要访问认证，请先登录"}` —— 端口可达、但要登录。**纠正**：我 11-15 写过它"无鉴权"，11-18 已不成立 |
| I | **6191 的鉴权边界** | NAS 上分别打 `/health`（无 token）、`/dsh`（无 token / 错 token / 正确 token） | `/health` **200**；`/dsh` 无 token 与错 token 都 **401 `token 不对`**；正确 token **200**（0.07 秒、184 个会话）→ 鉴权有效，但**端口确实对局域网可达** |
| K | **同一个端口、两个地址的差别** | 在 **Windows 本机**分别打 `127.0.0.1` 与 `192.168.50.63`：3082 的 `session/list`、6191 的 `/health` 与 `/dsh`（无 token）各一次 | `127.0.0.1:3082` → **200**（回环免登录）；`192.168.50.63:3082` → **401**（走局域网地址就要登录）→ **同一个服务按"目标地址"判，不看来源**；6191：两个地址的 `/health` 都是 **200**，`/dsh` 无 token 都是 **401**（鉴权与地址无关） |

### 真机验过（两条可以重跑的演练，2026-11-18 08:35）

| # | 项 | 怎么造的 | 结果 |
|---|---|---|---|
| L | **agent 静默死亡 → 6191 消失 → 跨机路径全断 → 守卫报警 → 拉回来** | `py tools/drill-agent-death.py --yes`（杀本机 agent，不碰电源/防火墙/NAS） | **8/8 通过**：peer `/health` 从 200 → `None`；CLI 断链时 `rc=1` 且报「连不上 Warden…timed out」、无 Traceback；账本出现 `agent-dead`（08:35:05）→ `wscript …molt-agent.vbs` 拉起 → **0.5 秒** `/health` 回 200 → 账本出现 `agent-back`（08:35:06） |
| M | **D18 那条链的一条命令回归（14 项）** | `py tools/drill-dsh-chain.py --warden-url http://192.168.50.50:6186 --warden-token <token>` | **14/14 通过**：peer `/health`、无 token 401、`session/list` 184、`session/events` 14 条、`session/prompt` 打不存在的会话得 `session/not-found`（说明在白名单里且**一个字没发出去**）、`session/create`/`cancel` 都 400 且带白名单全文、调用方塞 `address` 被忽略、NAS 那一跳同矩阵全通、CLI `dsh sessions` rc=0 |

### 仍未验

| # | 项 | 为什么现在验不了 | 需要什么条件才能验 |
|---|---|---|---|
| G | **`--steer` 打断"正在跑的回合"** | 打断的代价不是"代码出错"，而是**把别人正在做的那件事踢掉**（可能是主人正在等的回答）。这个代价不能由我替他承担，也不该用"反正会重跑"来糊弄 | ①主人明确说"就这次，打断它" ②指定一个他同意被打断的会话 ③一个**交互终端**（脚本要求手打 `YES`，CI/管道一律拒绝）<br>命令模板见 `docs/DRILL-STEER.md` §4；**当前只允许走 `--plan`** |
| J | **D20 的收口动作本身（建防火墙规则）** | 建/删规则要管理员；当前账户 `is_admin=False`，`Register-ScheduledTask` 都报 `0x80070005`。而且改防火墙属于**改环境**，主人没点头我不该动 | ①一个**管理员** PowerShell ②主人拍 D20 选 A 还是 B ③执行后按 `docs/ADMIN-D20.md` §4 的三条自查（NAS 打 3082 应从 401 变 000、本机 `127.0.0.1` 仍 200、`molt dsh sessions` 仍正常） |

### 「主人一同意就能跑」：G 和 J 的清单

这两条不是"我做不了"，是"需要你点头 + 一个我这边没有的东西（交互终端 / 管理员）"。
所以先把要点头的话、谁来点、怎么回滚都写死，你说一句就能动。

#### G · 真打断一次正在跑的回合

| 要素 | 内容 |
|---|---|
| **你要说的原话** | `G，就这次，打断 <会话名> 那个回合`（不给会话名也行：我先把"在跑的、非 subagent"候选列给你，你挑一个再回） |
| **谁来点** | **你本人**，在一个**交互终端**里敲 —— 脚本要求手打 `YES`，管道 / CI 一律拒绝。命令我准备好，你只负责回车 |
| **我动手前先给你看** | 那个会话现在在做什么（最近 3 条）+ 打断会丢掉什么（正在生成的那一段） |
| **跑法** | `py tools/drill-steer.py --steer --session <会话> --allow-running --warden-url … --warden-token …`（参数以 `docs/DRILL-STEER.md` §4 为准） |
| **回滚** | **没有撤回** —— `steer` 投的是一条已经进队列的用户消息。唯一补救是**让那个会话把活重跑一遍**（我再发一句「继续」）。这正是为什么这一步必须你点头 |
| **事后** | 结果写进本表 G 行：谁点的 / 打断了什么 / 有没有重跑 |

#### J · 建一条防火墙规则（D20）

| 要素 | 内容 |
|---|---|
| **你要说的原话** | `J，按 D20 选 A 执行` 或 `J，按 D20 选 B 执行` |
| **谁来点** | **你本人**，在一个**管理员 PowerShell** 里敲（我这边 `is_admin=False`，连 `Register-ScheduledTask` 都报 `0x80070005`） |
| **跑法** | `docs/ADMIN-D20.md` §3 的命令：A 一条 / B 两条，照抄 |
| **验收（三条，缺一不算）** | ① NAS 打 `192.168.50.63:3082` 从 **401** 变 **000** ② 本机 `127.0.0.1:3082` **不是 401**（仍可用）③ `molt dsh sessions` 仍通 |
| **回滚（管理员窗口）** | A：`Remove-NetFirewallRule -DisplayName "molt: block DSH 3082 from LAN"`<br>B：`Remove-NetFirewallRule -DisplayName "molt: allow DSH 3082 from NAS only"` 与 `"molt: block DSH 3082 from others"` |
| **注意** | `Remove-NetFirewallRule` 连 `-WhatIf` 都会报 `Access is denied`（非管理员），所以回滚同样得在管理员窗口里做 |

> 这两条都**不会**在没有你明确说话的情况下执行 —— 没有任何定时任务、后台循环或自动策略会碰它们
> （`molt/tests/test_drills.py` 里有一条测试专门扫源码，禁止演练脚本出现电源/防火墙写操作）。

---

## D19 · 「300 秒没人碰键鼠才关」的闸 —— **等主人拍板，未实现任何改动**

| 选项 | 做法 | 代价 |
|---|---|---|
| **A**（我推荐） | 300 秒 → **60 秒**，并允许按策略配 `power_min_idle_seconds` | 刚摸完键盘一分钟就出门 → 它会关 |
| B | 到点就关，完全不看键鼠 | 9:00 正在用电脑也会关 |
| C | 保持 300 秒（现状） | 你在用电脑时只记一条 `skipped`，机器不关 |

现状的证据等级：**单测**（`test_auto_migrate.py` 里"有人在用不放票 / 安静够久放票 / 读不到不放票"三条）
—— 真机没有在 9:00 触发过一次真实关机（`morning-home` 的首跑还没发生）。

---

## 落地播报瘦身（2026-11-18 主人定）

「我刚从 X 搬过来」之外的那句「XX 还开着 —— 要关它就回 /molt shutdown」已从**代码里删除**
（连同 `compose_power_question` / `should_ask_about_power` 两个函数与它们的 9 条单测）。

| 项 | 等级 | 证据 |
|---|---|---|
| 播报文本不含那两句 | **单测** | `test_the_arrival_note_never_asks_about_the_power`：断言发出的话里没有 `还开着` / `/molt shutdown` / `/molt keep` |
| 代码里也不再有那个模板 | **单测（扫源码）** | 同一条测试读 `plugin/arrival.py`，断言 `要关它就回` 与 `compose_power_question` 都不存在 |
| 打包产物与线上副本 | **真机（文件比对）** | `dist/plugin-bundle.tar` 里 `arrival.py` 该串出现 **0** 次；`G:\molt-fish\data\plugins\astrbot_plugin_molt\arrival.py` 同为 **0** |
| 真机上发一条播报看它到底说了什么 | **未验** | 播报只在"真的搬一次家"时出现；按限制这一轮不搬 |

---

## 怎么复跑

```bash
# 1) 单元与集成（不需要两台机器）
python -m pytest -q                       # 当前基线：1315 passed

# 2) 本机 peer（Windows 侧）
curl -s http://127.0.0.1:6191/health

# 3) 一条命令跑完 D18 那条链（14 项：三个白名单方法 + 拒绝项 + 两跳）
py tools/drill-dsh-chain.py --warden-url http://192.168.50.50:6186 --warden-token <token>

# 4) 演练「agent 死了会怎样」（先看步骤，再加 --yes 真跑；只动本机 agent）
py tools/drill-agent-death.py --plan
py tools/drill-agent-death.py --yes

# 5) 从 NAS 代转（在 NAS 上；与第 3 步等价，只是换了个位置跑）
export PYTHONPATH=/mnt/dm-0/astrbot/_molt/pkg
T="$(cat /mnt/dm-0/astrbot/_molt/warden.token)"
python3 -m molt.cli --no-color dsh sessions --shell desktop-h2kgiao \
  --warden-url http://127.0.0.1:6186 --warden-token "$T"

# 6) 白名单外必须被拒（两跳都该是 400）
python3 -m molt.cli --no-color dsh call --shell desktop-h2kgiao \
  --method session/create --args '{}' \
  --warden-url http://127.0.0.1:6186 --warden-token "$T"

# 7) 缺地址文案（本地模式，随便一个临时账本）
python -m molt.cli --no-color declare --shell box --role desktop --root /tmp/molt-box
python -m molt.cli --no-color dsh sessions --shell box --root /tmp/molt-box   # 期望 rc=2
```

**红线（任何一轮都适用）**：真机搬家、关机、断电一律留待办；电源相关测试只走桩；
`session/prompt` 真发只发到自己的会话、内容标明"测试"。
