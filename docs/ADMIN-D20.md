# 给主人的一页纸：把 DSH 的 3082 从局域网上收回去

> 判定依据在 `docs/DECISIONS.md` **D20**。这份是"照着敲"的版本。
> **我（molt 的开发者）没有执行过这里任何一条防火墙命令** —— 当前账户不是管理员。
>
> 全文只有两种终端，每一节都标了在哪跑：
> **【NAS】** = SSH 进 NAS 的 shell；**【Windows】** = 这台台式机的 PowerShell。

---

## 0. 先看清楚现在是什么状态（全部只读）

### 0.1 在【NAS】上跑

先用 SSH 进去（终端里敲，密码是你的 NAS root 密码）：

```bash
ssh -p 922 root@192.168.50.50
```

进去之后粘这三条（只发请求，不改任何东西）：

```bash
curl -s -m 5 -o /dev/null -w "3082:            %{http_code}\n" -X POST http://192.168.50.63:3082/api/session/list -d '{}'
curl -s -m 5 -o /dev/null -w "6191/health:     %{http_code}\n" http://192.168.50.63:6191/health
curl -s -m 5 -o /dev/null -w "6191/dsh(无token): %{http_code}\n" -X POST http://192.168.50.63:6191/dsh -d '{"method":"session/list"}'
```

**2026-11-18 05:00 我实测的结果**（给你的对照基线）：

```
3082:             401    ← 端口对局域网开着，但要求登录
6191/health:      200    ← 存活探针，故意不鉴权
6191/dsh(无token): 401    ← 真正干活那条要 token
```

### 0.2 在【Windows】上跑（注意：必须写 `curl.exe`）

```powershell
curl.exe -s -m 5 -o NUL -w "127.0.0.1:3082 → %{http_code}`n" -X POST http://127.0.0.1:3082/api/session/list -d "{}"
curl.exe -s -m 5 -o NUL -w "192.168.50.63:3082 → %{http_code}`n" -X POST http://192.168.50.63:3082/api/session/list -d "{}"
```

**基线（我实测）**：

```
127.0.0.1:3082      → 200   ← 本机回环：不需要登录
192.168.50.63:3082  → 401   ← 同一台机器，走局域网地址：要登录
```

> ⚠️ **两个坑，先看这里**
>
> 1. **PowerShell 里 `curl` 不是 curl** —— 它是 `Invoke-WebRequest` 的别名，`-X` / `-d` / `-o` 全都不认，会报一堆"参数无法识别"。**必须写 `curl.exe`**。
> 2. **"本机能不能连"要用 `127.0.0.1` 测，不能用 `192.168.50.63`** —— 同一台机器走局域网地址，走的是另一条路（要登录）。拿错地址会得出"本机也被挡了"的错误结论。

---

## 1. 怎么判断自己是不是管理员

在【Windows】的**普通** PowerShell 里粘这一行：

```powershell
([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
```

- `True` → 这个窗口已经是管理员，跳到第 3 节
- `False` → 需要第 2 节
- 报"无法将 Security 识别为…" → 你粘进了 cmd 或 Git Bash，换成 PowerShell

---

## 2. 开一个管理员终端

**A. 开始菜单（最稳）**
`Win` → 输入 `powershell` → 右键「Windows PowerShell」→ **以管理员身份运行** → UAC 点「是」。

**B. 从当前窗口提权**

```powershell
Start-Process powershell -Verb RunAs
```

会弹 UAC；点「是」后**会开一个新窗口**，后面所有命令都在**新窗口**里敲。

**C. Win+X**
`Win+X` → 「Windows PowerShell(管理员)」/「终端(管理员)」。

> 提权后新窗口的**当前目录**通常是 `C:\Windows\System32`，这**不影响**下面的命令（都不依赖当前目录）。
> 先在新窗口里重跑第 1 节那一行，确认真的是 `True`。

---

## 3. 一条命令做完（方案 A：只留回环）

在【Windows】的**管理员** PowerShell 里：

```powershell
New-NetFirewallRule -DisplayName "molt: block DSH 3082 from LAN" -Direction Inbound -Protocol TCP -LocalPort 3082 -Action Block -Profile Any
```

成功的样子：打印一张表，`DisplayName` = `molt: block DSH 3082 from LAN`、`Enabled` = `True`、`Action` = `Block`。
失败常见原因：`Access is denied` / `需要提升权限` → 第 2 节没做对。

**如果你更想让 NAS 仍能直连（方案 B）：**

```powershell
New-NetFirewallRule -DisplayName "molt: allow DSH 3082 from NAS only" -Direction Inbound -Protocol TCP -LocalPort 3082 -RemoteAddress 192.168.50.50 -Action Allow -Profile Any
New-NetFirewallRule -DisplayName "molt: block DSH 3082 from others" -Direction Inbound -Protocol TCP -LocalPort 3082 -Action Block -Profile Any
```

**可选：把代转小口 6191 也收窄到只允许 NAS**

```powershell
New-NetFirewallRule -DisplayName "molt: allow peer 6191 from NAS only" -Direction Inbound -Protocol TCP -LocalPort 6191 -RemoteAddress 192.168.50.50 -Action Allow -Profile Any
New-NetFirewallRule -DisplayName "molt: block peer 6191 from others" -Direction Inbound -Protocol TCP -LocalPort 6191 -Action Block -Profile Any
```

> 为什么代转不会被弄坏：代转那一跳是从 **Windows 本机**打到 `127.0.0.1:3082`（回环），
> Windows 防火墙不过滤回环流量；6191 只留给 NAS，是因为只有 NAS 会来敲它。

---

## 4. 做完自查（三条，缺一条都不算成）

| # | 在哪 | 命令 | 期望 |
|---|---|---|---|
| 1 | **【NAS】** | `curl -s -m 4 -o /dev/null -w "%{http_code}\n" -X POST http://192.168.50.63:3082/api/session/list -d '{}'` | **`000`**（超时/拒绝）—— 关键：**从 401 变成 000** 才算挡上了 |
| 2 | **【Windows】** | `curl.exe -s -m 5 -o NUL -w "%{http_code}`n" -X POST http://127.0.0.1:3082/api/session/list -d "{}"` | **`200`**（回环没被误伤） |
| 3 | **【NAS】** | `molt dsh sessions --shell desktop-h2kgiao` | 正常列出会话（代转这条路还通） |

**最容易搞错的两点：**

- 第 1 条**原本就是 401**，别把 401 当成"已经挡住了"。挡住的标志是 **000/超时**。
- 第 2 条必须用 `127.0.0.1`。用 `192.168.50.63` 测会得到 401，那是正常的。

如果第 2 条变成 `000` → 拦到回环了（少见），**立刻回滚**。

---

## 5. 回滚（一条）

在【Windows】管理员 PowerShell：

```powershell
Remove-NetFirewallRule -DisplayName "molt: block DSH 3082 from LAN"
```

B 方案回滚：

```powershell
Remove-NetFirewallRule -DisplayName "molt: allow DSH 3082 from NAS only"
Remove-NetFirewallRule -DisplayName "molt: block DSH 3082 from others"
```

回滚后回【NAS】重跑第 4 节第 1 条，应又回到 **401**（说明回到原状，而不是更坏）。

---

## 6. 如果哪天它又开了

在【Windows】上：

```powershell
Get-NetFirewallRule -DisplayName "molt: *" | Select-Object DisplayName, Enabled, Action, Direction
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 3082,6191 } | ForEach-Object { "$($_.LocalAddress):$($_.LocalPort) pid=$($_.OwningProcess)" }
```

把两段输出发我：前者能看出"规则被删/被禁用"，后者能看出"DSH 换了端口或多开了一个"。

---

## 附：三条方案的取舍（一句话版）

- **A** 关掉整扇门、只留回环 —— 推荐。回环不受防火墙影响，代转链路照常。
- **B** 门留着、只给 NAS 一把钥匙 —— 少一层中转，但换机器/改 IP 要同步改规则。
- **C** 把 DSH 改绑 `127.0.0.1` —— **现在做不到**：`app.asar`、`Preferences`、`cordis*.yml`、命令行里都没有 bind 开关（2026-11-15 查过）。
