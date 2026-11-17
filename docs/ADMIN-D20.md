# 给主人的一页纸：把 DSH 的 3082 从局域网上收回去

> 背景写在 `docs/DECISIONS.md` 的 **D20**。这份是"照着敲"的版本。
> **我（molt 的开发者）没有执行过其中任何一条** —— 当前账户不是管理员。

---

## 0. 先弄清楚现在到底暴露了什么（30 秒，不用管理员）

在 **NAS** 上跑这三条（只是发请求，不改任何东西）：

```bash
curl -s -m 5 -o /dev/null -w "3082: %{http_code}\n" -X POST http://192.168.50.63:3082/api/session/list -d '{}'
curl -s -m 5 -o /dev/null -w "6191/health: %{http_code}\n" http://192.168.50.63:6191/health
curl -s -m 5 -o /dev/null -w "6191/dsh: %{http_code}\n" -X POST http://192.168.50.63:6191/dsh -d '{"method":"session/list"}'
```

**2026-11-18 04:50 我实测的结果**（你可以对照）：

```
3082: 401        ← 端口对局域网是开的，但要求登录（不是完全裸奔）
6191/health: 200 ← 这个是故意不鉴权的存活探针
6191/dsh: 401    ← 真正干活的那条要 token
```

也就是说：**现在的风险是"端口对外可达 + 依赖鉴权"，不是"谁都能直接操作"。**
仍然值得收 —— 因为鉴权是会退化的，而防火墙不会。

---

## 1. 怎么判断自己是不是管理员

在**普通** PowerShell 里粘这一行：

```powershell
([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator)
```

- `True` → 你已经是管理员终端，跳到第 3 步
- `False` → 需要第 2 步
- 报错 `无法将"Security"识别为...` → 说明你粘进了 cmd 或 Git Bash，换成 PowerShell

---

## 2. 开一个管理员终端（三种都行）

**A. 开始菜单（最稳）**
开始 → 输入 `powershell` → 右键「Windows PowerShell」→ **以管理员身份运行** → UAC 点「是」。

**B. 从当前终端提权**
```powershell
Start-Process powershell -Verb RunAs
```
会弹 UAC。

**C. Win+X**
按 `Win+X` → 「Windows PowerShell (管理员)」/「终端(管理员)」。

> 注意：提权后**当前目录会变**（通常是 `C:\Windows\System32`），这跟你没关系 ——
> 下面所有命令都不依赖当前目录。

---

## 3. 一条命令做完（方案 A：只留回环）

```powershell
New-NetFirewallRule -DisplayName "molt: block DSH 3082 from LAN" `
  -Direction Inbound -Protocol TCP -LocalPort 3082 -Action Block -Profile Any
```

按回车后应该看到一张表格（`DisplayName / Enabled: True / Action: Block / Direction: Inbound`）。
如果报 `需要提升权限` / `Access is denied`，说明第 2 步没成功。

**同一件事，如果你更想只放 NAS 进来（方案 B）：**

```powershell
New-NetFirewallRule -DisplayName "molt: allow DSH 3082 from NAS only" `
  -Direction Inbound -Protocol TCP -LocalPort 3082 -RemoteAddress 192.168.50.50 -Action Allow -Profile Any
New-NetFirewallRule -DisplayName "molt: block DSH 3082 from others" `
  -Direction Inbound -Protocol TCP -LocalPort 3082 -Action Block -Profile Any
```

**顺带把代转小口也收窄（可选，同样一条命令）：**

```powershell
New-NetFirewallRule -DisplayName "molt: allow peer 6191 from NAS only" `
  -Direction Inbound -Protocol TCP -LocalPort 6191 -RemoteAddress 192.168.50.50 -Action Allow -Profile Any
New-NetFirewallRule -DisplayName "molt: block peer 6191 from others" `
  -Direction Inbound -Protocol TCP -LocalPort 6191 -Action Block -Profile Any
```

> 为什么这样不会把代转弄坏：代转那一跳是从 **Windows 本机**打到 `127.0.0.1:3082`，
> 而 Windows 防火墙不过滤回环流量。6191 那条只放 NAS，是因为只有 NAS 会来敲它。

---

## 4. 做完自查（三条，缺一条都不算成）

| # | 在哪跑 | 命令 | 期望 |
|---|---|---|---|
| 1 | **NAS** | `curl -s -m 4 -o /dev/null -w "%{http_code}\n" -X POST http://192.168.50.63:3082/api/session/list -d '{}'` | **000 / 超时**（连接被拒或超时；**不是** 401 或 200） |
| 2 | **Windows** | `curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:3082/api/session/list -d '{}'` | **401 或 200**（只要不是 000 就说明回环没被误伤） |
| 3 | **NAS** | `molt dsh sessions --shell desktop-h2kgiao` | 正常列出会话（说明 6191 → 回环 3082 这条路还通） |

如果第 2 条变成 `000` —— 说明拦到了回环（很少见），**立刻回滚**。

---

## 5. 回滚（一条）

```powershell
Remove-NetFirewallRule -DisplayName "molt: block DSH 3082 from LAN"
```

B 方案回滚：

```powershell
Remove-NetFirewallRule -DisplayName "molt: allow DSH 3082 from NAS only"
Remove-NetFirewallRule -DisplayName "molt: block DSH 3082 from others"
```

回滚后重跑第 4 节第 1 条，应该又回到 `401`（说明回到现状，而不是"更坏"）。

---

## 6. 如果哪天它又开了

```powershell
# 看有没有这条规则
Get-NetFirewallRule -DisplayName "molt: *" | Select-Object DisplayName, Enabled, Action
# 看端口现在绑在哪
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 3082,6191 } |
  ForEach-Object { "$($_.LocalAddress):$($_.LocalPort) pid=$($_.OwningProcess)" }
```

把这两条的输出发我，我能判断是"规则被删了"还是"DSH 换了端口/多开了一个"。
