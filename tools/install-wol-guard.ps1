# 文件名：install-wol-guard.ps1
# 用途：把 wol-guard-worker.ps1 装成计划任务 Molt-WolGuard，
#       让它在每次开机、以及每 10 分钟，自动把网卡重新加入唤醒白名单。
#
# 为什么需要它：
#   实测证明「arm 状态不跨重启保留」——12:24 和 12:40 两次冷启动后，
#   网卡都掉出了 wake_armed。如果不在每次开机时重新 arm，
#   那么"这次能唤醒"和"下次能不能唤醒"就没有关系了。
#
# 必须以管理员身份运行。

$ErrorActionPreference = 'Stop'

# ---- 管理员检查 ----
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host ''
    Write-Host '  [X] 需要管理员权限。' -ForegroundColor Red
    Write-Host ''
    Write-Host '  右键点开始菜单 → 「终端(管理员)」或「Windows PowerShell (管理员)」，然后重跑：'
    Write-Host ('     powershell -NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '"')
    Write-Host ''
    exit 2
}

$taskName = 'Molt-WolGuard'
$worker   = Join-Path $PSScriptRoot 'wol-guard-worker.ps1'

if (-not (Test-Path $worker)) {
    Write-Host ('  [X] 找不到 worker 脚本: ' + $worker) -ForegroundColor Red
    exit 1
}

Write-Host ''
Write-Host '=== 安装 Molt-WolGuard ===' -ForegroundColor Cyan
Write-Host ('  worker : ' + $worker)
Write-Host ('  task   : ' + $taskName)

# ---- 已存在就先删掉 ----
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host '  [i] 已存在同名任务，先删除'
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

# ---- 动作：跑 worker ----
$argLine = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $worker
$action  = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argLine

# ---- 触发器 1：开机 ----
$trigBoot = New-ScheduledTaskTrigger -AtStartup

# ---- 触发器 2：每 10 分钟重复一次（兜底：万一开机那次失败）----
$trigRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(5) `
                -RepetitionInterval (New-TimeSpan -Minutes 10)

# ---- 以 SYSTEM 身份运行：不需要用户登录，也不需要存密码 ----
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

# ---- 设置 ----
#  ExecutionTimeLimit = 0 → 不限时
#  StartWhenAvailable  → 错过也补跑
#  MultipleInstances   → 忽略重复实例，避免撞车
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $taskName `
    -Action $action `
    -Trigger @($trigBoot, $trigRepeat) `
    -Principal $principal `
    -Settings $settings `
    -Description 'Molt: 每次开机把有线网卡重新加入 OS 唤醒白名单（arm 状态不跨重启保留）' | Out-Null

Write-Host '  [OK] 已注册' -ForegroundColor Green

# ---- 立即跑一次验证 ----
Write-Host ''
Write-Host '=== 立即执行一次 ===' -ForegroundColor Cyan
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 6

$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Host ('  LastRunTime  : ' + $info.LastRunTime)
Write-Host ('  LastTaskResult: ' + $info.LastTaskResult + '   (0 = 成功)')

Write-Host ''
Write-Host '=== 当前唤醒白名单 ===' -ForegroundColor Cyan
& powercfg.exe /devicequery wake_armed | ForEach-Object { Write-Host ('  - ' + $_.Trim()) }

Write-Host ''
if ($info.LastTaskResult -eq 0) {
    Write-Host '  [OK] 装好了。以后每次开机会自动重新 arm。' -ForegroundColor Green
} else {
    Write-Host '  [!] 任务跑完了但返回非 0，看日志：' -ForegroundColor Yellow
    Write-Host ('      ' + (Join-Path $PSScriptRoot 'logs\wol-guard.log'))
}
Write-Host ''
