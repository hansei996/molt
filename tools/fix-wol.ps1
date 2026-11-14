<#
  fix-wol.ps1  --  Molt / 蜕壳
  一键把 Windows 侧 WOL 该开的两件事开掉（第 3 件 BIOS 只能人去）。

  用法（必须管理员）：
    powershell -NoProfile -ExecutionPolicy Bypass -File fix-wol.ps1

  它会做：
    1. powercfg /h off           关闭休眠 + 快速启动，删掉 hiberfil.sys
    2. powercfg /deviceenablewake 把 2.5G 网卡加进唤醒白名单
    3. 复查并打印结论

  它不会做（也不该做）：
    - 不改防火墙、不改网络档案、不重启、不动 BIOS
#>

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Line { param($t) Write-Host $t }

$IsAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
  ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Line ''
Line 'Molt WOL 修复'
Line ('=' * 46)

if (-not $IsAdmin) {
    Line ''
    Line '  这个脚本必须以管理员身份运行。'
    Line '  做法：右键点开始菜单 -> "Windows PowerShell (管理员)"'
    Line '        然后粘贴：'
    Line ''
    Line ('  powershell -NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '"')
    Line ''
    exit 2
}

Line '  管理员: True'

# ---------- 找到那块有线网卡 ----------
$nic = Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
       Where-Object { $_.Status -eq 'Up' -and $_.MediaType -eq '802.3' } |
       Select-Object -First 1
if (-not $nic) {
    $nic = Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
           Where-Object { $_.MediaType -eq '802.3' } |
           Select-Object -First 1
}
if (-not $nic) {
    Line ''
    Line '  [X] 没找到有线网卡，脚本停止。'
    exit 1
}

Line ('  网卡  : ' + $nic.InterfaceDescription)
Line ('  MAC   : ' + $nic.MacAddress)

# ---------- 第 1 步：关休眠 + 快速启动 ----------
Line ''
Line '[1/2] 关闭休眠与快速启动  (powercfg /h off)'
$before = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Power' -Name HibernateEnabled -ErrorAction SilentlyContinue).HibernateEnabled
Line ('      改前 HibernateEnabled = ' + $before)

$out = & powercfg.exe /h off 2>&1
if ($LASTEXITCODE -eq 0) {
    Line '      [OK] 已关闭'
} else {
    Line ('      [!] 返回码 ' + $LASTEXITCODE + ' : ' + ($out -join ' '))
}

$after = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Power' -Name HibernateEnabled -ErrorAction SilentlyContinue).HibernateEnabled
Line ('      改后 HibernateEnabled = ' + $after + '   (0 = 已关)')

# hiberfil 是否还在
# 注意：非管理员下 Test-Path 会对 hiberfil.sys 返回 False（权限），必须用 dir 兜底
$hf = Join-Path $env:SystemDrive 'hiberfil.sys'
$hfExists = $false
if (Test-Path $hf) {
    $hfExists = $true
} else {
    $dirOut = cmd /c "dir `"$hf`" /a:h 2>&1"
    if ($dirOut -match 'hiberfil\.sys') { $hfExists = $true }
}
if ($hfExists) {
    Line '      [i] hiberfil.sys 仍在（可能需重启后才彻底删除）'
} else {
    Line '      [OK] hiberfil.sys 已删除'
}

# ---------- 第 2 步：网卡加白名单 ----------
Line ''
Line '[2/2] 把网卡加入唤醒白名单  (powercfg /deviceenablewake)'

$armed = & powercfg.exe /devicequery wake_armed 2>&1
# 注意：$数组 -match 'x' 返回的是「匹配到的元素数组」，不是布尔。
# 在 if() 里虽然能正确强转，但显式 [bool] + 精确比较更不容易骗人（本项目踩过这个坑三次）。
$alreadyArmed = [bool]($armed | Where-Object { "$_".Trim() -eq $nic.InterfaceDescription })

if ($alreadyArmed) {
    Line '      已经在了，跳过'
} else {
    $out2 = & powercfg.exe /deviceenablewake $nic.InterfaceDescription 2>&1
    if ($LASTEXITCODE -eq 0) {
        Line '      [OK] 已加入'
    } else {
        Line ('      [X] 失败，返回码 ' + $LASTEXITCODE)
        Line ('      ' + ($out2 -join ' '))
        Line ''
        Line '      这基本可以确定是驱动问题（本机驱动 10.54.1111.2021 / 2021-11-11）。'
        Line '      先更新 Realtek PCIe 2.5GbE 驱动再重跑本脚本。'
    }
}

# ---------- 复查 ----------
Line ''
Line '复查'
Line ('-' * 46)

$armed2 = & powercfg.exe /devicequery wake_armed 2>&1
Line '  当前唤醒白名单:'
$armed2 | Where-Object { $_ } | ForEach-Object { Line ('    - ' + "$_".Trim()) }

# 注意：$数组 -match 'x' 返回的是「匹配到的元素数组」，不是布尔，必须 [bool]() 包裹
$nicArmedNow = [bool]($armed2 | Where-Object { "$_".Trim() -eq $nic.InterfaceDescription })

# 注意：powercfg /h off 改的是 Control\Power\HibernateEnabled，
#       Session Manager\Power\HiberbootEnabled 是陈旧值，读它会永远得到 1
$hib = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Power' -Name HibernateEnabled -ErrorAction SilentlyContinue).HibernateEnabled
$fastOff = ($hib -eq 0)

Line ''
Line ('  nic_in_wake_armed = ' + $nicArmedNow)
Line ('  hibernate_off     = ' + $fastOff + '   (HibernateEnabled=' + $hib + ')')

Line ''
if ($nicArmedNow -and $fastOff) {
    Line '  [OK] Windows 侧两件事都做完了。'
    Line ''
    Line '  还剩第 3 件：BIOS（只能人去做）'
    Line '    Settings -> Advanced -> Power Management Setup'
    Line '      ErP Ready          = Disabled'
    Line '      Power On by PCI-E  = Enabled'
    Line '      Wake On LAN        = Enabled'
    Line ''
    Line '  做完之后先别关机，回来告诉我。'
} else {
    Line '  [!] 还没完全就绪，看上面哪一项是 False。'
}
Line ''
exit 0