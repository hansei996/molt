# WOL 状态快照 —— 供 Molt 的 Warden 判断"能不能叫醒这台机器"
#
# 用法：
#   只读诊断（普通权限即可）：
#     powershell -NoProfile -ExecutionPolicy Bypass -File check-wol.ps1
#   输出 JSON 供 NAS 侧读取：
#     powershell -NoProfile -ExecutionPolicy Bypass -File check-wol.ps1 -Json
#
# 判据（三条全 True 才算 ready_to_wake）：
#   1. 网卡在 wake_armed 白名单里
#   2. 快速启动已关闭       （powercfg /a 里"快速启动"落在"不支持"段）
#   3. 休眠已关闭           （Control\Power\HibernateEnabled = 0）
#
# 两个已知的读错陷阱（本项目踩过，别再用）：
#   - Session Manager\Power\HiberbootEnabled 是陈旧值，powercfg /h off 不会清它
#   - $数组 -match "x" 返回匹配的元素数组，不是布尔，必须 [bool]() 包裹

param([switch]$Json)

$ErrorActionPreference = 'SilentlyContinue'

function Get-SleepStateMap {
    # 解析 powercfg /a，返回 @{ 状态名 = 'supported'|'notSupported' }
    $map = @{}
    $lines = & powercfg.exe /a 2>&1
    $inNot = $false
    foreach ($raw in $lines) {
        $line = "$raw"
        if ($line -match '此系统上没有以下睡眠状态') { $inNot = $true;  continue }
        if ($line -match '此系统上有以下睡眠状态')   { $inNot = $false; continue }
        $t = $line.Trim()
        if (-not $t) { continue }
        if ($t -match '^(待机\s*\(S\d\)|待机\(S\d[^)]*\)|休眠|混合睡眠|快速启动)') {
            $map[$Matches[1]] = if ($inNot) { 'notSupported' } else { 'supported' }
        }
    }
    return $map
}

$report = [ordered]@{
    generated_at      = (Get-Date).ToString('o')
    machine           = $env:COMPUTERNAME
    user              = "$env:USERDOMAIN\$env:USERNAME"
    is_admin          = $false
    nic               = [ordered]@{}
    wake_armed        = @()
    wake_programmable = @()
    power             = [ordered]@{}
    verdict           = [ordered]@{}
}

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$report.is_admin = ([Security.Principal.WindowsPrincipal]$id).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

# --- 网卡（优先有线且 Up 的）---
$nic = Get-NetAdapter -Physical | Where-Object { $_.MediaType -eq '802.3' -and $_.Status -eq 'Up' } |
       Select-Object -First 1
if (-not $nic) {
    $nic = Get-NetAdapter -Physical | Where-Object { $_.MediaType -eq '802.3' } | Select-Object -First 1
}
if ($nic) {
    $report.nic = [ordered]@{
        name        = $nic.Name
        description = $nic.InterfaceDescription
        mac         = $nic.MacAddress
        status      = "$($nic.Status)"
        link_speed  = "$($nic.LinkSpeed)"
        driver_ver  = "$($nic.DriverVersion)"
        driver_date = "$($nic.DriverDate)"
    }
}

# --- 唤醒白名单 ---
$armed = @(& powercfg.exe /devicequery wake_armed 2>$null | ForEach-Object { "$_".Trim() } | Where-Object { $_ })
$prog  = @(& powercfg.exe /devicequery wake_programmable 2>$null | ForEach-Object { "$_".Trim() } | Where-Object { $_ })
$report.wake_armed        = $armed
$report.wake_programmable = $prog

# --- 休眠 / 快速启动（用权威来源）---
$hibernateEnabled = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Power' -Name HibernateEnabled).HibernateEnabled
$hiberbootStale   = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' -Name HiberbootEnabled).HiberbootEnabled
$sleep = Get-SleepStateMap
$fastStartupState = $sleep['快速启动']

$report.power = [ordered]@{
    hibernate_enabled   = $hibernateEnabled
    fast_startup_state  = "$fastStartupState"
    sleep_states        = $sleep
    hiberboot_stale     = $hiberbootStale
}

# --- 结论 ---
$nicDesc  = if ($nic) { $nic.InterfaceDescription } else { '' }
$nicArmed = [bool]($armed | Where-Object { $_ -eq $nicDesc })
$nicProg  = [bool]($prog  | Where-Object { $_ -eq $nicDesc })

# 快速启动：优先用 powercfg /a 的语义；拿不到就退回"休眠关了 => 快速启动必然不可用"
if ($fastStartupState) {
    $fastOff = ($fastStartupState -eq 'notSupported')
} else {
    $fastOff = ($hibernateEnabled -eq 0)
}

$report.verdict = [ordered]@{
    nic_in_wake_armed = $nicArmed
    fast_startup_off  = [bool]$fastOff
    hibernate_off     = [bool]($hibernateEnabled -eq 0)
    nic_can_be_armed  = $nicProg
    ready_to_wake     = [bool]($nicArmed -and $fastOff)
    blocking          = @(
        if (-not $nicArmed) { '网卡不在 wake_armed：需要管理员跑 powercfg /deviceenablewake' }
        if (-not $fastOff)  { '快速启动/休眠仍开启：需要管理员跑 powercfg /h off' }
    )
}

if ($Json) {
    $report | ConvertTo-Json -Depth 6
    exit 0
}

Write-Host '=== WOL 状态 ===' -ForegroundColor Cyan
Write-Host ("管理员: {0}" -f $report.is_admin)
Write-Host ("网卡  : {0}" -f $nicDesc)
Write-Host ("驱动  : {0} ({1})" -f $report.nic.driver_ver, $report.nic.driver_date)
Write-Host ''
Write-Host '唤醒白名单:' -ForegroundColor Yellow
$armed | ForEach-Object { Write-Host "  - $_" }
Write-Host ''
Write-Host '电源状态:' -ForegroundColor Yellow
Write-Host ("  休眠(HibernateEnabled) = {0}   (0=已关闭)" -f $hibernateEnabled)
Write-Host ("  快速启动               = {0}" -f $fastStartupState)
Write-Host ''
Write-Host '结论:' -ForegroundColor Cyan
foreach ($k in @('nic_in_wake_armed','fast_startup_off','hibernate_off','nic_can_be_armed','ready_to_wake')) {
    $v = $report.verdict[$k]
    $c = if ($v) { 'Green' } else { 'Red' }
    Write-Host ("  {0,-18} = {1}" -f $k, $v) -ForegroundColor $c
}
Write-Host ''
if ($report.verdict.blocking.Count -gt 0) {
    Write-Host '卡在:' -ForegroundColor Red
    $report.verdict.blocking | ForEach-Object { Write-Host "  ! $_" -ForegroundColor Red }
} else {
    Write-Host '  [OK] 三项齐全，可以试唤醒了' -ForegroundColor Green
}
exit 0