# 把 agent-guard 装成一个每 5 分钟跑一次的计划任务。
#
# 注意它**只告警，不拉起**：能不能自动拉起 agent 要人拍板（D15/E4）。
# 装完之后，agent 死掉 3 分钟内账本里就会出现一条 agent-dead，
# `/where` 会显示红条，`molt status` 也会念出来。
#
# 用法（管理员）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\install-agent-guard.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\install-agent-guard.ps1 -Uninstall
#
# 非管理员跑 → Get-ScheduledTask 会被吞成"不存在"、Get-ScheduledTaskInfo 报
# Access is denied（0x80070005）。所以这个脚本第一件事就是检查自己是不是管理员。

[CmdletBinding()]
param(
    [string]$TaskName = 'Molt-AgentGuard',
    [int]$EveryMinutes = 5,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
# 非管理员也能装 —— 只是这条任务变成"当前用户登录后才跑"（Interactive）。
# 本机开着自动登录（netplwiz 那个勾），所以和 SYSTEM 的差别基本只剩"注销时停跑"。
# 想要机器级（SYSTEM、开机就跑、不挑登录）就用管理员再跑一次，脚本会自动切。

$script = Join-Path $PSScriptRoot 'agent-guard.ps1'
if (-not (Test-Path -LiteralPath $script)) {
    Write-Output ("找不到 {0}" -f $script)
    exit 2
}

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output ("已删除计划任务 {0}" -f $TaskName)
    }
    else {
        Write-Output ("没有这个计划任务：{0}" -f $TaskName)
    }
    exit 0
}

$argument = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $script
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argument -WorkingDirectory $PSScriptRoot
$triggers = @(
    (New-ScheduledTaskTrigger -AtStartup),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(2) `
        -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
        -RepetitionDuration (New-TimeSpan -Days 3650))
)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
if ($isAdmin) {
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $scope = 'SYSTEM（开机就跑，不看登录）'
}
else {
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    $scope = ("当前用户 {0}（登录后跑；注销就停）" -f $env:USERNAME)
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Settings $settings -Principal $taskPrincipal -Force | Out-Null
Write-Output ("已注册计划任务 {0}：每 {1} 分钟跑一次（只告警，不拉起）；身份：{2}" -f $TaskName, $EveryMinutes, $scope)

# 立刻跑一次，确认装完就能用（不做就只能等 5 分钟）。
$run = Start-ScheduledTask -TaskName $TaskName -PassThru
Start-Sleep -Seconds 20
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Output ("上次运行 {0}，结果 {1}" -f $info.LastRunTime, $info.LastTaskResult)
