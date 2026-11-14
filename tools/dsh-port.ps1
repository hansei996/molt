<#
.SYNOPSIS
找出 DSH 桌面版真正的 HTTP RPC 端口，并可选地把 AstrBot 的 dsh_connector 配置改对。

.DESCRIPTION
为什么需要这个脚本：DSH 有两类本地端口，性质完全不同。

  1. 「DSH 桌面应用自己起的 host 服务」——端口由桌面壳在启动时决定，
     不写在任何配置文件里，asar 里也没有常量。本机实测是 3082。
     它才是插件要连的那个（POST /api/<namespace>/<method> 会得到
     {"type":"server-response", ...}）。
  2. 「Web GUI 端口」——每次启动都变（今天见过 52043 / 55221 / 58590），
     不带浏览器会话 cookie 一律 401，插件连它没用。

而 DSH 独立版 `dsh web` 的默认端口是 3080（官方配置 `port: !!js ctx.webStartup.port ?? 3080`），
插件的文档默认值也是 3080 —— 但桌面版根本不听 3080，于是插件永远
「远程计算机拒绝网络连接」，很容易被误判成"DSH 没启动"。

所以：不要背端口号，也不要靠猜。跑这个脚本，它去问 DSH 本人。

.PARAMETER Fix
把找到的端口写回插件配置（保留原文件的 BOM 与缩进）。

.PARAMETER ConfigPath
插件配置路径，默认 G:\molt-fish\data\config\astrbot_plugin_dsh_connector_config.json

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File dsh-port.ps1

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File dsh-port.ps1 -Fix
#>
[CmdletBinding()]
param(
    [switch]$Fix,
    [string]$ConfigPath = "G:\molt-fish\data\config\astrbot_plugin_dsh_connector_config.json"
)

$ErrorActionPreference = "Stop"

function Get-DshListeningPorts {
    $procs = Get-Process -Name "DeepSeek Harness" -ErrorAction SilentlyContinue
    if (-not $procs) { return @() }
    $ids = @($procs | ForEach-Object { $_.Id })
    $conns = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $ids -contains $_.OwningProcess }
    return @($conns | Select-Object -ExpandProperty LocalPort -Unique | Sort-Object)
}

function Test-DshGateway {
    param([int]$Port)
    # 判据不是"端口通了"，而是"它答得像 DSH 的 RPC 网关"。
    # 通了的端口可能是 Web GUI（401）、也可能是别的东西。
    try {
        $body = '{"type":"client-request","rpcId":"probe","namespace":"session","method":"list","payload":{}}'
        $r = Invoke-WebRequest "http://127.0.0.1:$Port/api/session/list" `
            -Method Post -ContentType "application/json" -Body $body `
            -UseBasicParsing -TimeoutSec 4
        $text = [string]$r.Content
        if ($text -match '"server-response"') { return "gateway" }
        if ($text -match 'gateway/') { return "gateway" }
        return "http-$($r.StatusCode)"
    } catch {
        $resp = $_.Exception.Response
        if ($resp) {
            $code = [int]$resp.StatusCode
            # 401 = 有鉴权的 Web GUI，不是插件要的
            if ($code -eq 401) { return "unauthorized" }
            if ($code -eq 404) { return "not-found" }
            return "http-$code"
        }
        return "closed"
    }
}

$ports = Get-DshListeningPorts
if ($ports.Count -eq 0) {
    Write-Host "没有找到正在运行的 DeepSeek Harness。" -ForegroundColor Red
    Write-Host "先把它启动起来，再跑这个脚本。" -ForegroundColor DarkGray
    exit 2
}

Write-Host "DeepSeek Harness 在听的端口：" -ForegroundColor Cyan
$gateway = $null
foreach ($p in $ports) {
    $kind = Test-DshGateway -Port $p
    $mark = "  "
    if ($kind -eq "gateway") { $mark = "->"; $gateway = $p }
    Write-Host ("  {0} {1,-6} {2}" -f $mark, $p, $kind)
}

if (-not $gateway) {
    Write-Host ""
    Write-Host "没找到 RPC 网关。可能 DSH 还在启动，或者这一版把网关挪到别处了。" -ForegroundColor Red
    exit 3
}

$base = "http://127.0.0.1:$gateway"
Write-Host ""
Write-Host "插件该写：http_base_url = $base" -ForegroundColor Green

if (-not (Test-Path $ConfigPath)) {
    Write-Host "配置文件不在：$ConfigPath" -ForegroundColor Red
    exit 4
}

$raw = [System.IO.File]::ReadAllBytes($ConfigPath)
$hasBom = ($raw.Length -ge 3 -and $raw[0] -eq 0xEF -and $raw[1] -eq 0xBB -and $raw[2] -eq 0xBF)
$text = [System.IO.File]::ReadAllText($ConfigPath, [System.Text.Encoding]::UTF8)
$current = $null
try {
    $obj = $text | ConvertFrom-Json
    $current = [string]$obj.http_base_url
} catch {
    Write-Host "配置不是合法 JSON，不敢动它：$($_.Exception.Message)" -ForegroundColor Red
    exit 5
}

Write-Host "插件现在写：http_base_url = $current" -ForegroundColor DarkGray

if ($current -eq $base) {
    Write-Host "已经是对的，不用改。" -ForegroundColor Green
    exit 0
}

if (-not $Fix) {
    Write-Host ""
    Write-Host "要改对吗？加 -Fix 重跑一次：" -ForegroundColor Yellow
    Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Fix" -ForegroundColor DarkGray
    exit 0
}

# 只替换这一个字符串值，不重排 JSON —— 重排会把用户手写的顺序和注释（如果有）弄丢。
# 不用 [regex]::Replace 的 ScriptBlock 重载：Windows PowerShell 5.1 上把 scriptblock
# 转成 MatchEvaluator 的行为不稳，手工拼更保险。
$pattern = '("http_base_url"\s*:\s*")[^"]*(")'
$m = [regex]::Match($text, $pattern)
if (-not $m.Success) {
    Write-Host "没替换成功（正则没命中 http_base_url），请手动改。" -ForegroundColor Red
    exit 6
}
$newText = $text.Substring(0, $m.Groups[1].Index + $m.Groups[1].Length) + $base + $text.Substring($m.Groups[2].Index)
if ($newText -eq $text) {
    Write-Host "替换后内容没有变化，请手动检查。" -ForegroundColor Red
    exit 6
}

# 先备份再写，且按原编码写回（有 BOM 就留着）
$backup = "$ConfigPath.bak-before-port-$gateway"
Copy-Item $ConfigPath $backup -Force
$enc = New-Object System.Text.UTF8Encoding($hasBom)
[System.IO.File]::WriteAllText($ConfigPath, $newText, $enc)

Write-Host "已改：$current -> $base" -ForegroundColor Green
Write-Host "备份：$backup" -ForegroundColor DarkGray
Write-Host ""
Write-Host "改完要让 AstrBot 重新加载插件配置，最快是重启壳（或等插件重载）。" -ForegroundColor Yellow
exit 0
