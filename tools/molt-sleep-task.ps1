#Requires -RunAsAdministrator
<#
.SYNOPSIS
    让这台机器"真的能睡"——一次性，需要管理员。

.DESCRIPTION
    2026-11-15 凌晨在真机上查出来的：这台机器开着「离开模式」
    （HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power\AwayModeEnabled = 1）。

    它的效果是 —— 任何睡眠请求都被翻译成"屏幕关掉、整机继续跑"，
    而 SetSuspendState 依然返回 TRUE。所以：

        · 调用报成功
        · Kernel-Power 里没有「进入睡眠」(42) 事件
        · 只有一条 59「系统正在进入离开模式」
        · 功耗一点没降

    这就是"报成功但其实什么都没做"，比直接失败难查得多。

    关掉它之后，普通的 SetSuspendState 就能真睡，而且**不需要任何权限**：
    我们已经在真机上验证过，普通进程可以自己 AdjustTokenPrivileges 解开
    SeShutdownPrivilege（它在令牌里、只是被禁用了），然后挂起。

    睡眠和关机的区别不是省不省电，是「回来的时候要不要有人在键盘前」：
    睡眠不销毁会话，WOL 唤醒后一切照旧，不需要密码、不需要自动登录；
    真关机则必须能自动登录，否则机器停在登录界面等人。

.PARAMETER ConsoleNoLock
    顺便把「唤醒后要求重新登录」关掉（CONSOLELOCK=0）。
    严格来说不需要 —— 锁屏不影响后台进程继续跑，只是界面要再输一次 PIN。

.PARAMETER Restore
    把离开模式改回 1（撤销）。

.EXAMPLE
    右键这个文件 →「使用 PowerShell 运行」；或者：
    Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','<这个文件的完整路径>'
#>
param(
    [switch]$ConsoleNoLock,
    [switch]$Restore
)

$ErrorActionPreference = 'Stop'
$powerKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power'

if ($Restore) {
    Set-ItemProperty -Path $powerKey -Name 'AwayModeEnabled' -Value 1 -Type DWord
    Write-Host '已把离开模式改回 1（睡眠请求会再次被翻译成"屏幕关掉、整机继续跑"）'
    exit 0
}

Write-Host '=== 关掉「离开模式」==='
$before = (Get-ItemProperty -Path $powerKey -Name 'AwayModeEnabled' -ErrorAction SilentlyContinue).AwayModeEnabled
Write-Host "  改前 AwayModeEnabled = $before"
Set-ItemProperty -Path $powerKey -Name 'AwayModeEnabled' -Value 0 -Type DWord
$after = (Get-ItemProperty -Path $powerKey -Name 'AwayModeEnabled').AwayModeEnabled
Write-Host "  改后 AwayModeEnabled = $after"
if ($after -ne 0) { throw "没改成功（还是 $after）" }

if ($ConsoleNoLock) {
    & powercfg /setacvalueindex SCHEME_CURRENT SUB_NONE CONSOLELOCK 0 | Out-Null
    & powercfg /setactive SCHEME_CURRENT | Out-Null
    Write-Host '  已设置：唤醒后不要求重新登录（CONSOLELOCK=0）'
}

Write-Host ''
Write-Host '好了。现在这台机器能真睡了，而且 molt 的 agent 不需要任何权限就能让它睡。'
Write-Host '撤销：同一个脚本加 -Restore'
