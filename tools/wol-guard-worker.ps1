# 文件名：wol-guard-worker.ps1
# 用途：由计划任务 Molt-WolGuard 在每次开机和周期触发时调用，
#       把有线网卡重新加入 OS 的唤醒白名单（powercfg /deviceenablewake），并记录结果。
#
# 为什么需要这个文件：
#   实测发现 arm 状态不跨重启保留。所以"修好一次"不等于"长期有效"，
#   每次开机都必须重新 arm，否则下一次关机后 WOL 会失效。
#
# 本脚本必须以 SYSTEM（或管理员）身份运行。

$ErrorActionPreference = 'Continue'

# ---- 日志 ----
$logDir = Join-Path $PSScriptRoot 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$logFile = Join-Path $logDir 'wol-guard.log'

function W {
    param([string]$Msg)
    $line = '{0}  {1}' -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $Msg
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

# 日志超过 200KB 就滚存一份，避免无限增长
if ((Test-Path $logFile) -and ((Get-Item $logFile).Length -gt 200KB)) {
    Move-Item -Path $logFile -Destination ($logFile + '.old') -Force
}

W '--- 开始检查 ---'
W ('身份: ' + [System.Security.Principal.WindowsIdentity]::GetCurrent().Name)

# ---- 找到有线网卡 ----
# 优先按已知型号匹配，找不到就退回"第一个 Up 的物理以太网卡"
$nic = $null
$allEth = Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
          Where-Object { $_.MediaType -eq '802.3' }

$nic = $allEth | Where-Object { $_.InterfaceDescription -match 'Realtek PCIe 2\.5GbE' } | Select-Object -First 1
if (-not $nic) {
    $nic = $allEth | Where-Object { $_.Status -eq 'Up' } | Select-Object -First 1
}
if (-not $nic) {
    W '[FAIL] 没找到任何物理以太网卡'
    exit 1
}
W ('网卡: ' + $nic.InterfaceDescription + '  MAC ' + $nic.MacAddress + '  Status ' + $nic.Status)

# ---- 是否已经在白名单里 ----
# 注意：$数组 -match 返回的是「匹配到的元素数组」而不是布尔，必须显式 [bool] + 精确比较。
#       本项目在这个坑上栽过一次，别改回 -match。
function Test-Armed {
    param([string]$Desc)
    $armed = & powercfg.exe /devicequery wake_armed 2>&1
    return [bool]($armed | Where-Object { "$_".Trim() -eq $Desc })
}

$armedBefore = Test-Armed -Desc $nic.InterfaceDescription
W ('arm 状态(改前): ' + $armedBefore)

if ($armedBefore) {
    W '[SKIP] 已在白名单，无需操作'
} else {
    $out = & powercfg.exe /deviceenablewake $nic.InterfaceDescription 2>&1
    $rc  = $LASTEXITCODE
    W ('deviceenablewake 返回码: ' + $rc + '  输出: ' + ($out -join ' '))
    $armedAfter = Test-Armed -Desc $nic.InterfaceDescription
    W ('arm 状态(改后): ' + $armedAfter)
    if (-not $armedAfter) {
        W '[FAIL] 重新 arm 失败 —— 下次关机后 WOL 会用不了'
        exit 2
    }
    W '[OK] 已重新 arm'
}

# ---- 顺带记录休眠/快速启动状态，便于事后回溯 ----
try {
    $hib = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Power' -Name HibernateEnabled -ErrorAction Stop).HibernateEnabled
    W ('HibernateEnabled = ' + $hib + '  (0 = 休眠/快速启动已关)')
} catch {
    W 'HibernateEnabled = <读不到>'
}

W '--- 结束 ---'
exit 0
