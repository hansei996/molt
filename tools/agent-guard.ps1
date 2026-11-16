# 看着 molt-agent，死了就往账本里告一条警（**不拉起它**）。
#
# 为什么要有这个脚本
# ------------------
# 2026-11-17 凌晨，Windows 上的 molt-agent 死了两次（01:19 死 25 分钟、
# 02:06 前又死一次）。它是"早上 9 点那条自动搬家策略能不能触发"的唯一前提，
# 而它死的时候没有任何东西会响：agent.log 停在一行普通的 [idle]，
# 账本那边只是心跳慢慢变旧。
#
# 这个脚本只做两件事：**判断**、**告警**。它绝不重启进程 ——
# 能不能自动拉起要人拍板（见 docs/DECISIONS.md D15 的 E4）。
#
# 判据不写在这里：真正的判决在 molt/agentwatch.py 里（有桩测试），
# 这个脚本只负责读数（进程在不在 / 状态文件多久没动 / 心跳多久没报）
# 然后把结论喂给它。想改判据，去改那边的常量和测试，别在这里加 if。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\agent-guard.ps1 -DryRun
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\agent-guard.ps1
#
# 安装成计划任务（每 5 分钟一次）见 tools\install-agent-guard.ps1。

[CmdletBinding()]
param(
    [string]$ConfigPath = '',
    [switch]$DryRun,
    [switch]$Force,
    [double]$DedupeSeconds = 1800,
    [double]$StateSilentSeconds = 180,
    [double]$HeartbeatSilentSeconds = 180
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Write-Line([string]$Text) {
    Write-Output ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $Text)
}

if (-not $ConfigPath) {
    # PS 5.1 在 param 默认值里拿不到 $PSScriptRoot（是空的），只能等进了函数体再算。
    $ConfigPath = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Definition) 'molt-agent.json'
}

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Write-Line ("找不到配置：{0}" -f $ConfigPath)
    exit 2
}

$cfg = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$shell = [string]$cfg.shell
$wardenUrl = ([string]$cfg.warden_url).TrimEnd('/')
$token = [string]$cfg.warden_token
$configDir = Split-Path -Parent (Resolve-Path -LiteralPath $ConfigPath)
$statePath = Join-Path $configDir 'molt-agent-state.json'
$guardStatePath = Join-Path $configDir 'molt-agent-guard-state.json'

# ---- 1. 进程在不在 -------------------------------------------------------
# 只认 cmdline 里带 molt-agent.py 的 python 进程。按端口认人会认错
# （AstrBot 自己也占 6199），按名字认人会认到别的脚本。
$pidAlive = $null
try {
    $procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction Stop |
        Where-Object { $_.CommandLine -and $_.CommandLine -like '*molt-agent.py*' })
    $pidAlive = ($procs.Count -gt 0)
}
catch {
    $pidAlive = $null
    Write-Line ("读不到进程列表（{0}）—— 交给时间戳判断" -f $_.Exception.Message)
}

# ---- 2. 状态文件多久没更新 ------------------------------------------------
$stateAge = $null
if (Test-Path -LiteralPath $statePath) {
    $stateAge = [math]::Round(((Get-Date) - (Get-Item -LiteralPath $statePath).LastWriteTime).TotalSeconds, 1)
}

# ---- 3. 账本上的心跳多久没报 ----------------------------------------------
$heartbeatAge = $null
try {
    $headers = @{ 'X-Molt-Token' = $token }
    $status = Invoke-RestMethod -Uri ("{0}/status" -f $wardenUrl) -Headers $headers -TimeoutSec 10 -Method Get
    $hb = $status.status.heartbeats.$shell
    if ($hb -and $null -ne $hb.age_seconds) {
        $heartbeatAge = [double]$hb.age_seconds
    }
}
catch {
    Write-Line ("账本没读到心跳（{0}）" -f $_.Exception.Message)
}

# ---- 4. 判决（用仓库里的 molt.agentwatch，判据只有一份）--------------------
$moltPkg = [string]$cfg.molt_pkg
$judgeScript = @"
import json, sys
sys.path.insert(0, r'''$moltPkg''')
from molt import agentwatch
got = agentwatch.judge(
    pid_alive=$(if ($null -eq $pidAlive) { 'None' } elseif ($pidAlive) { 'True' } else { 'False' }),
    state_age=$(if ($null -eq $stateAge) { 'None' } else { "$stateAge" }),
    heartbeat_age=$(if ($null -eq $heartbeatAge) { 'None' } else { "$heartbeatAge" }),
    state_limit=$StateSilentSeconds,
    heartbeat_limit=$HeartbeatSilentSeconds,
)
kind = agentwatch.alert_kind(got)
message = agentwatch.alert_message(got, shell=r'''$shell''')
out_path = sys.argv[1]
with open(out_path, 'w', encoding='utf-8') as fh:
    json.dump({'state': got['state'], 'reason': got['reason'], 'kind': kind, 'message': message}, fh, ensure_ascii=False)
"@
$judgePath = Join-Path $env:TEMP ('molt-agent-guard-judge-{0}.py' -f $PID)
$judgeOut = Join-Path $env:TEMP ('molt-agent-guard-verdict-{0}.json' -f $PID)
[System.IO.File]::WriteAllText($judgePath, $judgeScript, (New-Object System.Text.UTF8Encoding($false)))
try {
    # 结论走**文件**而不是 stdout：`& python ... | Out-String` 会用控制台代码页
    # （这台机器上是 GBK）解码子进程输出，中文在拿到手之前就已经变成 ?????? 了
    # （2026-11-17 03:07 真机抓到 —— 账本里那条 agent-dead 的 message 全是问号）。
    & ([string]$cfg.python) $judgePath $judgeOut | Out-Null
    $raw = Get-Content -LiteralPath $judgeOut -Raw -Encoding UTF8
}
finally {
    Remove-Item -LiteralPath $judgePath -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $judgeOut -ErrorAction SilentlyContinue
}
try {
    $verdict = $raw | ConvertFrom-Json
}
catch {
    Write-Line ("判决脚本没给出结论：{0}" -f $raw.Trim())
    exit 3
}

Write-Line ("pid_alive={0} state_age={1} heartbeat_age={2} => {3}" -f $pidAlive, $stateAge, $heartbeatAge, $verdict.state)
if ($verdict.reason) { Write-Line ("  {0}" -f $verdict.reason) }

# 读上一次报出去的是什么（用于去重，也用于"它又起来了"）
$last = $null
if (Test-Path -LiteralPath $guardStatePath) {
    try { $last = Get-Content -LiteralPath $guardStatePath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $last = $null }
}
$now = [DateTimeOffset]::Now.ToUnixTimeSeconds()

if (-not $verdict.kind) {
    # 活着或看不出来：正常情况下什么都不做 —— 但**如果上一轮报过它死了/哑了**，
    # 这一轮要补一句"它又回来了"。否则那条 agent-dead 会永远挂在 `/where` 上，
    # 而人一旦学会忽略这个红条，下一次真出事就没人在看了。
    $wasBad = $last -and ([string]$last.kind -eq 'agent-dead' -or [string]$last.kind -eq 'agent-silent')
    if ($wasBad -and -not $DryRun) {
        $back = @{
            kind    = 'agent-back'
            message = ("这台机器上的 molt-agent 又跑起来了（上一轮报的是 {0}）。" -f $last.kind)
            shell   = $shell
            detail  = @{ previous_kind = [string]$last.kind; by = 'agent-guard' }
        } | ConvertTo-Json -Depth 5 -Compress
        try {
            Invoke-RestMethod -Uri ("{0}/alert" -f $wardenUrl) -Method Post -Headers @{
                'X-Molt-Token' = $token
                'Content-Type' = 'application/json; charset=utf-8'
            } -Body ([System.Text.Encoding]::UTF8.GetBytes($back)) -TimeoutSec 10 | Out-Null
            Write-Line '已写进账本：agent-back'
        }
        catch {
            Write-Line ("写 agent-back 失败：{0}" -f $_.Exception.Message)
        }
        @{ kind = 'agent-back'; at = $now } | ConvertTo-Json -Compress |
            Set-Content -LiteralPath $guardStatePath -Encoding UTF8
    }
    exit 0
}

# ---- 5. 去重（同一个 kind 在窗口内只报一次）--------------------------------
if (-not $Force -and $last -and [string]$last.kind -eq [string]$verdict.kind) {
    $age = $now - [double]$last.at
    if ($age -lt $DedupeSeconds) {
        Write-Line ("同一类告警 {0} 秒前刚报过，这次不重复" -f [int]$age)
        exit 0
    }
}

if ($DryRun) {
    Write-Line ("[dry-run] 本来要报一条 {0}：{1}" -f $verdict.kind, $verdict.message)
    exit 0
}

# ---- 6. 写进账本（只告警，不重启、不动电源）--------------------------------
$body = @{
    kind    = [string]$verdict.kind
    message = [string]$verdict.message
    shell   = $shell
    detail  = @{
        pid_alive     = $pidAlive
        state_age     = $stateAge
        heartbeat_age = $heartbeatAge
        by            = 'agent-guard'
    }
} | ConvertTo-Json -Depth 6 -Compress

try {
    # 必须自己转成 UTF-8 字节再发：直接传字符串时 PowerShell 会按本地代码页
    # 编码请求体，账本那边就只剩一串 ????（2026-11-17 03:06 真机抓到）。
    $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    $resp = Invoke-RestMethod -Uri ("{0}/alert" -f $wardenUrl) -Method Post -Headers @{
        'X-Molt-Token'  = $token
        'Content-Type'  = 'application/json; charset=utf-8'
    } -Body $bodyBytes -TimeoutSec 10
    Write-Line ("已写进账本：{0}" -f $verdict.kind)
}
catch {
    Write-Line ("写账本失败：{0}" -f $_.Exception.Message)
    exit 4
}

@{
    kind = [string]$verdict.kind
    at   = $now
} | ConvertTo-Json -Compress | Set-Content -LiteralPath $guardStatePath -Encoding UTF8
exit 0
