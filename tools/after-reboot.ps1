<#
重启之后跑这一个脚本就够了：不需要 DSH，不需要 AI。

用法：
  powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\13553\Downloads\DSH_workspace\molt\tools\after-reboot.ps1"

它只回答一个问题：这次开机，Molt-WolGuard 有没有自动把网卡重新加回唤醒白名单？
#>

$ErrorActionPreference = 'Continue'
$toolsDir = $PSScriptRoot
$logPath  = Join-Path $toolsDir 'logs\wol-guard.log'

$os   = Get-CimInstance Win32_OperatingSystem
$boot = $os.LastBootUpTime

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Write-Output "=== 开机信息 ==="
Write-Output ("  LastBootUpTime : " + $boot.ToString('yyyy-MM-dd HH:mm:ss'))
Write-Output ("  已运行         : " + [string]((Get-Date) - $boot))
Write-Output ("  管理员         : " + $isAdmin)

Write-Output ""
Write-Output "=== 自愈任务这次开机有没有跑 ==="
$postBootRuns = 0
if (Test-Path $logPath) {
    $lines = Get-Content $logPath -Encoding UTF8
    foreach ($ln in $lines) {
        if ($ln -match '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})') {
            $ts = [datetime]::ParseExact($matches[1], 'yyyy-MM-dd HH:mm:ss', $null)
            if (($ln -match '开始检查') -and ($ts -ge $boot.AddMinutes(-2))) { $postBootRuns++ }
        }
    }
    Write-Output ("  日志文件           : " + $logPath)
    Write-Output ("  本次开机后运行次数 : " + $postBootRuns)
    Write-Output "  日志最后 24 行:"
    $lines | Select-Object -Last 24 | ForEach-Object { Write-Output ("    " + $_) }
} else {
    Write-Output ("  [WARN] 日志不存在: " + $logPath)
    Write-Output "         说明 worker 从来没跑过，自愈没生效。"
}

Write-Output ""
Write-Output "=== 当前 WOL 判据（check-wol.ps1）==="
$checker = Join-Path $toolsDir 'check-wol.ps1'
if (Test-Path $checker) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File $checker 2>&1 | ForEach-Object { Write-Output ("  " + $_) }
} else {
    Write-Output "  [WARN] 找不到 check-wol.ps1"
}

Write-Output ""
Write-Output "=== 判决 ==="
if ($postBootRuns -gt 0) {
    Write-Output ("  [OK] 自愈任务在本次开机后确实运行了（{0} 次）。" -f $postBootRuns)
    Write-Output "       若上面 nic_in_wake_armed = True，这一关就彻底关闭了。"
} else {
    Write-Output "  [!]  本次开机后没有找到自愈任务的运行记录。"
    Write-Output "       可能原因：任务没注册成功 / 触发器没生效 / 权限不足。"
    Write-Output "       请以管理员身份重新跑 install-wol-guard.ps1。"
}