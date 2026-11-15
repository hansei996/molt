# 十分钟装一台壳

> 这份文档的目标：**从零开始，让人在自己机器上把 molt 跑起来并看见它动。**
> 每一步都是可以直接复制粘贴的命令。卡住的地方，`molt doctor` 会告诉你卡在哪。

假设你有两台机器：

- **甲**（`192.168.50.50`）—— 一直开着的那台，比如 NAS。它当**账本的看守**。
- **乙**（`192.168.50.63`）—— 你平时用、会关机的那台，比如游戏机/直播机。
  它当**新壳**（`agent` 角色）。

真正跑起来的形态里还有个 `homer`（在甲上，等鱼回家），本文最后再说。

---

## 0. 装代码（两台都要）

```bash
git clone https://github.com/hansei996/molt
cd molt
pip install -e .          # 装出 `molt` 命令；或者直接用 `python -m molt.cli`
molt --version
```

> 只要 Python ≥ 3.10 和标准库。Warden 那台机器**不需要**装任何第三方包 ——
> 这是刻意的：账本要能在"什么都还没装好"的机器上活着。

---

## 0.5 账本用 docker 跑（可选，最省事）

如果你只是想要一个账本，不想管 python 环境：

```bash
docker run -d --name molt-warden -p 6186:6186 \
  -v molt-data:/var/lib/molt \
  -e MOLT_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')" \
  ghcr.io/hansei996/molt:latest
```

- 没设 `MOLT_TOKEN` 时容器会**拒绝启动**（退出码 78）并打印怎么配。这不是故障。
- 数据在 `molt-data` 这个 named volume 里；换机器时搬它。
- 自己构建：`docker build -t molt:dev .`（构建过程不需要网络）。

壳那侧仍然按下面第 1、2、3 步走 —— 壳跑在你要搬去的那台机器上，不是容器里。

---

## 1. 甲：先把账本立起来

```bash
molt init --role warden --root /var/lib/molt
```

它会做两件事：

1. 生成一个 token，写进 `/var/lib/molt/warden.token`（权限 `600`）
2. 打印一行 `serve` 命令，把那个 token 带在里面

照抄它打印的那行：

```bash
python3 -m molt.cli serve --root /var/lib/molt --host 0.0.0.0 --port 6186 --token <它打印的 token>
```

验一下（在**另一台**机器上跑，这样才能证明它真的对局域网开放了）：

```bash
curl -sS http://192.168.50.50:6186/status -H "X-Molt-Token: <那个 token>"
```

应该看到一段 JSON，里面有 `"current_epoch": 0` 和 `"generation_count": 0`。

不想手抄 token？

```bash
cat /var/lib/molt/warden.token
```

或者让乙直接问甲要 —— 但**不要**把 token 写进 shell 历史：

```bash
read -rs TOKEN && echo   # 粘贴，回车
```

两个令牌约定：

- `X-Molt-Token` 头是**所有**端点都要的（除了下面说的 `/where`）
- `?token=` 查询参数**只**在 `GET /where` 上认 —— 它是个只读的页面，方便你在
  手机浏览器里看一眼「鱼现在在哪」。能改账本的 token 不该出现在 URL 里
  （会被记进浏览历史、会被转发、会被截图）

---

## 2. 乙：生成新壳的配置

```bash
molt init --role agent \
  --out ./molt-agent.json \
  --shell my-windows-box \
  --data /path/to/astrbot/data \
  --warden-url http://192.168.50.50:6186 \
  --token <甲的那个 token>
```

三件必须由你决定、它绝不替你猜的事：

| 参数 | 为什么不能猜 |
|---|---|
| `--shell` | 账本里的 `target_shell` 是**别人写下的字符串**。猜错，这台机器就永远等不到自己的那一代，而且**不会报错** |
| `--data` | 那是 AstrBot 的 data 目录。指错了，落位会写到别处 |
| `--token` | 猜不出来，只能从甲那里拿 |

生成出来的配置里有两个字段值得看一眼：

- `"execute": false` —— **刻意如此**。第一次装上只观察、不动手。这个字段为真意味着
  「这台机器会自己关机」。
- `"backup_interval": 600` —— 每十分钟往账本推一份中途快照，把 RPO 从「一整晚」
  压到「十分钟」。

会拒绝你的三种情况（都是故意的）：

```
✗ 没有壳名，我不猜            → 加 --shell
✗ 还没有账本地址，我不知道该往哪儿推   → 加 --warden-url
✗ 还没有 token                → 加 --token
```

已经有一份调通的配置时，它**不会覆盖**：

```
✗ ./molt-agent.json 已经存在。一份调通的配置比一份「更新过的」配置值钱，所以不覆盖。
  要覆盖就加 --force（旧的会留成 .bak）
```

---

## 3. 先自检，再让它动手

```bash
molt doctor --config ./molt-agent.json --role agent
```

它问三件事，分开答，因为修法完全不同：

| 输出 | 意思 | 怎么修 |
|---|---|---|
| `✓ 守护进程的加载器读得过` | 这份配置守护进程自己能读 | —— |
| `✗ 连不上` | 账本没起来 / 地址错 / 防火墙 | `curl -sS -o /dev/null -w '%{http_code}' http://192.168.50.50:6186/status` |
| `✗ 连上了，但 token 不对（HTTP 401）` | 地址对了，凭据错 | 从甲的 `<root>/warden.token` 抄 |

> 为什么"加载器读得过"要单独验一次？因为生成器自己认为对**不算数**。这一条是
> v0.4 那课的延续：最贵的错误全都长得像成功。

---

## 4. 空跑一轮（不动手）

```bash
molt-agent.py --config ./molt-agent.json --once
```

（脚本在仓库的 `tools/` 下；Windows 上用 `python tools\molt-agent.py …`。）

`execute: false` 的时候它只读账本、只打印判断，不会 land、不会开机、不会关机。
你会看到类似：

```
[once] 账本说：第 3 代，目标是 nas-shell，取走的是 nas-shell
[once] 这一代不是给我的
```

看到它**真的读到了账本**（而不是 `读不到账本：账本返回 HTTP 401`），就可以往下走。

---

## 5. 让它常驻

### Linux（甲这类机器）

交给 `tools/molt-supervise.py`，它**只补不杀**，可以放心进 cron：

```bash
*/2 * * * * /usr/bin/python3 /path/to/molt/tools/molt-supervise.py --quiet
```

（还要来一条 `@reboot` 兜底：`@reboot` 可能比你的数据盘挂载更早触发。）

### Windows（乙这类机器）

把它放进启动项：

```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\molt-agent.vbs
```

里面一行：

```vbscript
CreateObject("WScript.Shell").Run """G:\molt-fish\molt-agent.cmd""", 0, False
```

`molt-agent.cmd` 里是：

```bat
G:\venv\Scripts\python.exe C:\path\to\molt\tools\molt-agent.py --config C:\path\to\molt-agent.json >> G:\logs\agent.log 2>> G:\logs\agent.err
```

启动项只会在**有人登录之后**跑。如果你要让它在你不在的时候自己起来，得开自动登录
（`tools/molt-autologon.ps1`）—— 这是这台机器的取舍，不是 molt 的决定。

---

## 6. 第一次真正迁徙

在**鱼正在说话的那台机器**上（QQ 里）：

```
/molt go
/molt confirm
```

> 注意：`molt land` 一定会把 `platform[].enable` 关掉 —— 这是公理 0 的护栏，
> 不是 bug。落位之后要让它重新对外说话，必须由人显式打开：
> `tools/platform-switch.py on --config <data>/cmd_config.json --yes`

或者不用聊天层，直接命令行：

```bash
molt migrate --data <data> --shell my-windows-box \
  --warden-url http://192.168.50.50:6186 --warden-token <token>
```

新壳那边落地：

```bash
molt land --generation N --data <data> --warden-url … --warden-token …
```

---

## 7. 它现在在哪？（不用问它）

三条路，随便挑一条：

```bash
# 1) 命令行
molt status --warden-url http://192.168.50.50:6186 --warden-token <token>

# 2) 手机浏览器（只读，认 ?token=）
#    http://192.168.50.50:6186/where?token=<token>

# 3) 在 QQ 里对它说
/molt where
```

心跳超过 **10 分钟**没更新就算「哑了」。心跳证明某个壳还在报，**不证明鱼在说话**。

---

## 8. 常见症状（按"看起来像什么"排）

| 你看到的 | 真正的原因 | 修法 |
|---|---|---|
| 它不动，也不报错 | 壳名对不上（`target_shell` 是别人写下的字符串） | `molt status` 看账本里的目标壳是谁；`molt doctor --config …` 看你这台自己叫什么 |
| `✗ 连不上` / `读不到账本：账本返回 HTTP 401` | 地址或 token | `molt doctor --config …` |
| 落位后谁都不回消息 | `land` 关掉了平台适配器（护栏） | `tools/platform-switch.py on --config <data>/cmd_config.json --yes` |
| `上一次落位没有做完…` | 真的有一份半成品，被标记拦住了 | **不要手工删标记**。重跑一次 `molt land`，它会收尾 |
| 命令全失败、像服务没启动 | DSH 网关端口变了（桌面版 DSH 每次启动自己决定端口） | `powershell -File tools/dsh-port.ps1`（`-Fix` 自动改）；agent 每轮也会自己纠偏 |
| `它现在在哪` 里只有一个壳 | 另一台的守护进程没起来 | `tools/molt-supervise.py --status` |

---

## 9. 关于 `homer`（在甲上等鱼回家）

甲这台机器不只要当账本，还要**接住回家的鱼**：

```bash
molt init --role homer \
  --out ./molt-homer.json \
  --shell nas-box \
  --data /path/to/astrbot/data \
  --warden-url http://127.0.0.1:6186 \
  --token <token>
```

它比 `agent` 多做一步、且必须多做的那一步是：**落位之前先停掉容器。**
活的 AstrBot 进程 fd 还指着被移走的旧 inode；不停容器就落位，结果是账本说这一代
已落地，而它还在读写上一代 —— 这不是崩溃，是两边各说各话。

`homer` 的 `dashboard_host` 默认是 `0.0.0.0`：容器里监听 `127.0.0.1` 等于没监听。
