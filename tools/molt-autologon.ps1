<#
.SYNOPSIS
    管这台机器的自动登录（真关机之后，谁来开机并把 DSH 拉起来）。

.DESCRIPTION
    背景：`shutdown /s` 是普通用户就能做的（实测 `shutdown /s /t 600` 排定成功、
    `shutdown /a` 撤销也成功）。但**关机之后，开机时没有人输密码**，
    AstrBot 和 DSH 就都不会起来 —— 因为 Startup 文件夹是"登录之后"才跑的。

    所以想让「关机 → 被叫醒 → 自己干活」这条路走通，必须先能自动登录。

    这台机器登的是微软账户（`PrincipalSource: MicrosoftAccount`），
    所以自动登录要的是**账户密码**，不是开机 PIN。PIN 在 Winlogon 里没用
    （实测 `net use /user:13553 <PIN>` 和 `/user:MicrosoftAccount\<邮箱> <PIN>`
    都是 1326）。

    ⚠️ 代价，说清楚再用：
      · DefaultPassword 在注册表里是**明文**，任何能读 HKLM 的管理员都看得见
      · 开了自动登录，走到这台机器前的人就是它了
      · 如果你乐意，用 `netplwiz` 自己输密码更安全（存在 LSA secret 里，不是明文）

.PARAMETER Account
    账户名。微软账户用邮箱（`you@example.com`），本地账户用用户名。

.PARAMETER Password
    账户密码。注意是**密码**，不是开机 PIN。

.PARAMETER Disable
    关掉自动登录并清掉存着的密码（推荐先用它把状态清干净）。

.PARAMETER Check
    只看现在是什么状态，不改任何东西。

.EXAMPLE
    # 先看清楚现在是什么样（尤其如果之前存错过密码）
    .\molt-autologon.ps1 -Check

.EXAMPLE
    # 清掉（取消自动登录、删掉存的密码）
    .\molt-autologon.ps1 -Disable

.EXAMPLE
    # 开上
    .\molt-autologon.ps1 -Account "you@example.com" -Password "……"
#>
param(
    [string]$Account = '',
    [string]$Password = '',
    [switch]$Disable,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
$key = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'

function Show-State {
    $k = Get-ItemProperty -Path $key
    $hasPwd = $k.PSObject.Properties.Name -contains 'DefaultPassword'
    Write-Host '=== 现在的状态 ==='
    Write-Host ("  AutoAdminLogon   = " + $(if ($k.PSObject.Properties.Name -contains 'AutoAdminLogon') { $k.AutoAdminLogon } else { '(未设置)' }))
    Write-Host ("  DefaultUserName  = " + $(if ($k.PSObject.Properties.Name -contains 'DefaultUserName') { $k.DefaultUserName } else { '(未设置)' }))
    Write-Host ("  DefaultDomainName= " + $(if ($k.PSObject.Properties.Name -contains 'DefaultDomainName') { $k.DefaultDomainName } else { '(未设置)' }))
    Write-Host ("  DefaultPassword  = " + $(if ($hasPwd) { '已存在（明文，长度 ' + $k.DefaultPassword.Length + '）' } else { '(没有)' }))
    if ($hasPwd) {
        Write-Host ''
        Write-Host '  ⚠️ 存着密码。如果不是你自己写的，用 -Disable 清掉。' -ForegroundColor Yellow
    }
}

function Assert-Admin {
    # -Check 只需要读，不该逼人先提权；改东西才要。
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw '这个操作要改 HKLM，得用管理员身份跑（-Check 不需要）。'
    }
}

if ($Check -or ($Account -eq '' -and $Password -eq '' -and -not $Disable)) {
    Show-State
    exit 0
}

if ($Disable) {
    Assert-Admin
    Show-State
    Set-ItemProperty -Path $key -Name 'AutoAdminLogon' -Value '0' -Type String
    foreach ($name in 'DefaultPassword', 'DefaultUserName', 'DefaultDomainName') {
        $exists = (Get-ItemProperty -Path $key).PSObject.Properties.Name -contains $name
        if ($exists) { Remove-ItemProperty -Path $key -Name $name -Force }
    }
    Write-Host ''
    Write-Host '已关闭自动登录，并删掉存着的账户名/密码。下次开机会正常停在本机登录界面。'
    exit 0
}

if ($Account -eq '' -or $Password -eq '') {
    throw '要开自动登录，得同时给 -Account 和 -Password（这是账户密码，不是开机 PIN）。只想清掉就用 -Disable。'
}

Assert-Admin
Set-ItemProperty -Path $key -Name 'AutoAdminLogon' -Value '1' -Type String
Set-ItemProperty -Path $key -Name 'DefaultUserName' -Value $Account -Type String
Set-ItemProperty -Path $key -Name 'DefaultPassword' -Value $Password -Type String

if ($Account -match '@') {
    Set-ItemProperty -Path $key -Name 'DefaultDomainName' -Value 'MicrosoftAccount' -Type String
} else {
    Set-ItemProperty -Path $key -Name 'DefaultDomainName' -Value $env:COMPUTERNAME -Type String
}

Write-Host '已写入自动登录。'
Show-State
Write-Host ''
Write-Host '⚠️ 密码现在在注册表里是明文。要撤：.\molt-autologon.ps1 -Disable'
Write-Host '   更安全的做法是用 netplwiz 自己输（LSA secret，不是明文）——效果一样。'
