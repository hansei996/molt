# `--steer` 演练：主人一句话就能跑

> 这一条对应 `docs/VERIFY.md` 的 **G**：`--steer` 打断"正在跑的回合"还没真验过。
> 不验的原因不是做不到，而是**做了会踢掉别人正在干的活**。
> 所以它被做成演练脚本，由你决定什么时候跑、跑在哪个会话上。

---

## 0. 前置条件（两件，都可当场确认）

1. **鱼在哪台机器上，那台机器上 DSH 活着**
   ```bash
   molt machine list          # 看「鱼在这里」
   ```
   期望：能列出两台，其中一台标「鱼在这里」，`地址` 后面写着「DSH 通」。

2. **账本能连上**（下面命令里的 `--warden-url` 就是它）
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" http://192.168.50.50:6186/status
   ```
   期望 `401`（说明服务在，只是要 token）；`000` 说明账本没起。

---

## 1. 先看（只看，不发任何东西）

```bash
python tools/drill-steer.py --peers --warden-url http://192.168.50.50:6186 --warden-token <token>
```

**你会看到**（2026-11-18 07:00 真机输出）：

```
账本里 desktop-h2kgiao 上共 184 个会话，其中正在跑 1 个。
  · session-b0639458-…  在跑  C:\Users\13553\Downloads\DSH_workspace
  · session-51fef645-…  空着  C:\Users\13553\Downloads\DSH_workspace
  · session-0e9151d7-…  空着  C:\Users\13553\Downloads\DSH_workspace
  · session-c637773e-…  空着  C:\Users\13553\Downloads\DSH_workspace
```

（`<token>` 在 `C:\Users\13553\Downloads\DSH_workspace\molt\tools\molt-agent.json` 的
`warden_token` 字段，或 NAS 上 `/mnt/dm-0/astrbot/_molt/warden.token`。）

---

## 2. 看一眼"会发什么"

```bash
python tools/drill-steer.py --plan --warden-url http://192.168.50.50:6186 --warden-token <token>
```

**你会看到**（2026-11-18 07:00 真机输出，不会发出任何东西）：

```
（空转，不会发出任何东西）
  目标壳  desktop-h2kgiao
  会话    session-51fef645-e4d2-438d-9956-6f3492b3fae2   空着
  模式    steer
  内容    【molt steer 演练】这条只用来确认 steer 能插进这条链路，不用做任何事，也不用回复。(07:00:26)

真跑（空会话上）：把 --plan 去掉
真跑（正在跑的回合，需要人工 YES）：再加 --allow-running
```

> 这两步（`--peers` 与 `--plan`）**已经按真机走通并把输出贴在这里了**；
> 第三段「真跑」没有执行 —— 因为它会往一个真实会话里塞一条消息，留给主人按。

---

## 3. 真跑一次（默认只在**空着的**会话上）

```bash
python tools/drill-steer.py --warden-url http://192.168.50.50:6186 --warden-token <token>
```

**成功长这样：**

```
发送 steer → session-51fef645-…
  rc=0  {"accepted": true}
等 8 秒再回读事件流……
  历史里最近的 20 条中，含演练标记的有 1 条。
✓ 看到了。注意：这只证明 steer 被接受并落进历史；
  「打断正在跑的回合」这一次没有被真正演练到。
```

**失败长这样：**

| 输出 | 含义 | 怎么办 |
|---|---|---|
| `✗ 这台机器上没有空着的会话。` + 退出码 3 | 所有会话都在跑 | 等一个回合结束；或人工确认后用 `--session` 指定某个 |
| `✗ 挑中的会话正在跑，而没给 --allow-running` + 退出码 4 | 自动挑到了在跑的 | 这是保护：要么换个空会话，要么人类确认 |
| `✗ 没在历史里看到那条 steer` + 退出码 6 | 发了但没落进历史 | **别当成功记着**，把输出发我 |
| `✗ 这个会话正在跑，而当前不是交互终端` | 想打断但在 CI/管道里 | 到真终端里跑 |

---

## 4. 真验"打断正在跑的回合"（需要你本人）

**这一步会打断那个会话正在做的事**，所以只能人来按：

```bash
python tools/drill-steer.py --allow-running --session <正在跑的那个 id> \
  --warden-url http://192.168.50.50:6186 --warden-token <token>
```

它会停下来问：

```
⚠ 这个会话正在跑：session-xxxx  C:\...
  继续会打断它正在做的活。真的要验，请手打 YES：
>
```

只有手打 `YES`（全大写）才会发。非交互终端（CI、管道）会直接拒绝，不会问。

**成功判据**：同一个输出末尾会写 `「打断正在跑的回合」这一次是被真正演练到。`
**失败判据**：输出 exit code 5（你打了别的）或 6（发了但历史里没有）。
**跑完之后**：如果你打断的是我（DSH 里的一个会话），我会看到一条带 `【molt steer 演练】` 的消息 ——
这就是它真的插进去了的证据。

---

## 5. 它不碰什么

- 不碰电源、不搬家、不改账本里的策略、不改插件配置
- 只调用 `molt dsh send`（白名单内的 `session/prompt`），走的是账本代转的两跳
- 唯一的副作用就是**在目标会话里多一条带标记的消息**；默认还只挑空会话

---

## 6. 这个脚本自己怎么被钉住

`molt/tests/test_drill_steer.py` 有四条：脚本存在、默认挑会话时传的是 `allow_running=args.allow_running`（默认 False）、
打断必须 `isatty()` + 手打 `YES`、`--help` 能跑通。改松这条边界会立刻红。
