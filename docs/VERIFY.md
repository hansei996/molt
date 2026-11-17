# 核对清单：哪些真机验过、哪些只有单测、哪些还没验

这份文件只回答一个问题：**"这条到底算什么程度的证据？"**

三档，别混用：

| 档 | 含义 | 能拿来对外说什么 |
|---|---|---|
| **真机** | 在两台真实机器上跑过，有输出/时间戳可引 | "它工作" |
| **单测** | 只有 pytest 覆盖，跑的是假对象或临时目录 | "这段逻辑按预期分支" |
| **未验** | 既没真机也没单测，或者只验了一半 | 什么都不能说 |

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

### 只有单测

| # | 项 | 为什么没上真机 |
|---|---|---|
| A | `SEND_TIMEOUT=60` 这一档 | 真机造的是**读**超时（30 秒那档）；发消息超时要让 DSH 卡在写路径，没造 |
| B | `MAX_BODY = 1 MiB` 的截断/拒绝 | 需要造 1 MiB 以上的请求体，风险与收益不成比例 |
| C | 黑洞恢复后代理口的状态（半开连接是否会堆积） | 只看了单次结果，没做长时间压测 |
| D | token 轮换期间的行为（peer 用旧 token 会 401 多久） | 需要动两边的 token 文件，属于运维动作 |

### 未验 / 已知缺口

| # | 项 | 说明 |
|---|---|---|
| E | 从 **NAS 容器内部**打这条链 | 目前验的是 NAS 宿主机上的 CLI。容器里能不能连 `127.0.0.1:6186` 取决于它是不是 host 网络 —— 容器当前是停的（鱼在 Windows），等下次起容器再补 |
| F | `session/prompt` 的 `--steer` 模式 | 只真机验了默认的 `queue`；`steer` 会打断正在跑的回合，没敢在别人会话上试 |
| G | **DSH 的 3082 仍绑 `0.0.0.0`** | 这是一个**未修的洞**：局域网里任何人都能直连那台机器的 DSH。代转链路绕开了它，但没有关掉它 |

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
python -m pytest -q                       # 当前基线：1309 passed

# 2) 本机 peer（Windows 侧）
curl -s http://127.0.0.1:6191/health

# 3) 从 NAS 代转（在 NAS 上）
export PYTHONPATH=/mnt/dm-0/astrbot/_molt/pkg
T="$(cat /mnt/dm-0/astrbot/_molt/warden.token)"
python3 -m molt.cli --no-color dsh sessions --shell desktop-h2kgiao \
  --warden-url http://127.0.0.1:6186 --warden-token "$T"

# 4) 白名单外必须被拒（两跳都该是 400）
python3 -m molt.cli --no-color dsh call --shell desktop-h2kgiao \
  --method session/create --args '{}' \
  --warden-url http://127.0.0.1:6186 --warden-token "$T"

# 5) 缺地址文案（本地模式，随便一个临时账本）
python -m molt.cli --no-color declare --shell box --role desktop --root /tmp/molt-box
python -m molt.cli --no-color dsh sessions --shell box --root /tmp/molt-box   # 期望 rc=2
```

**红线（任何一轮都适用）**：真机搬家、关机、断电一律留待办；电源相关测试只走桩；
`session/prompt` 真发只发到自己的会话、内容标明"测试"。
