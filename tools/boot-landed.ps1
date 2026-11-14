# 用 molt land 落位后的数据启动 AstrBot，确认它认得自己。
#
# 这是整条链路最后一环的验收：migrate -> 传 -> land -> **它醒过来还是它**。
# 前三步只证明了"字节搬家成功"，只有这一步才证明"那个存在没断"。

param(
    [Parameter(Mandatory = $true)][string]$DataDir,
    [string]$Root = "G:\molt-p0\astrbot-src",
    [string]$Python = "G:\molt-p0\venv313\Scripts\python.exe",
    [int]$WaitSeconds = 60,
    [int]$Port = 6199
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$DataDir = (Resolve-Path $DataDir).Path
Write-Host "=== 用落位数据启动 AstrBot ===" -ForegroundColor Cyan
Write-Host "  data    : $DataDir"
Write-Host "  源码    : $Root"
Write-Host "  python  : $Python"

if (-not (Test-Path $Python)) {
    Write-Host "  [X] python 不存在：$Python" -ForegroundColor Red
    exit 2
}

# 记下启动前两个库的指纹与行数
$dbMain = Join-Path $DataDir "data_v4.db"
$dbMem = Join-Path $DataDir "plugin_data\astrbot_plugin_memory_companion\memory_companion.db"
Write-Host ""
Write-Host "启动前：" -ForegroundColor Yellow
foreach ($p in @($dbMain, $dbMem)) {
    if (Test-Path $p) {
        $h = (Get-FileHash $p -Algorithm SHA256).Hash.Substring(0, 16)
        Write-Host ("  {0,-24} {1,12:N0} B  {2}" -f (Split-Path $p -Leaf), (Get-Item $p).Length, $h)
    }
}

$env:ASTRBOT_ROOT = (Split-Path $DataDir -Parent)
$logOut = Join-Path $env:TEMP "molt-boot-stdout.log"
$logErr = Join-Path $env:TEMP "molt-boot-stderr.log"
Remove-Item $logOut, $logErr -Force -ErrorAction SilentlyContinue

$proc = Start-Process -FilePath $Python `
    -ArgumentList "-m", "astrbot.cli", "--help" `
    -WorkingDirectory $Root `
    -RedirectStandardOutput $logOut `
    -RedirectStandardError $logErr `
    -PassThru -WindowStyle Hidden -ErrorAction SilentlyContinue

# 直接用 main.py 更可靠
if ($proc) { try { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } catch {} }

$proc = Start-Process -FilePath $Python `
    -ArgumentList "main.py" `
    -WorkingDirectory $Root `
    -RedirectStandardOutput $logOut `
    -RedirectStandardError $logErr `
    -PassThru -WindowStyle Hidden

Write-Host ""
Write-Host ("  已启动 pid={0}，等 {1} 秒…" -f $proc.Id, $WaitSeconds) -ForegroundColor Yellow

# 边等边看 dashboard 有没有起来
$ready = $false
$deadline = (Get-Date).AddSeconds($WaitSeconds)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    if ($proc.HasExited) { break }
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
}

Write-Host ""
if ($proc.HasExited) {
    Write-Host ("  [X] 进程已退出，exit={0}" -f $proc.ExitCode) -ForegroundColor Red
} elseif ($ready) {
    Write-Host "  [OK] WebUI 响应 200" -ForegroundColor Green
} else {
    Write-Host "  [!] 超时未见 200（可能还在装依赖/初始化）" -ForegroundColor Yellow
}

# 读日志里的关键行
Write-Host ""
Write-Host "=== 日志关键行 ===" -ForegroundColor Cyan
foreach ($lf in @($logOut, $logErr)) {
    if (-not (Test-Path $lf)) { continue }
    Get-Content $lf -Encoding UTF8 -ErrorAction SilentlyContinue |
        Where-Object { $_ -match 'AstrBot v|AstrBot started|WebUI|Username|Shutting down|Error|Traceback|ModuleNotFound|persona|Loaded' } |
        Select-Object -First 25 |
        ForEach-Object { Write-Host ("  " + $_.TrimEnd()) }
}

# 收尾
if (-not $proc.HasExited) {
    Write-Host ""
    Write-Host "  停掉（Stop-Process）…" -ForegroundColor Yellow
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

Write-Host ""
Write-Host "启动后：" -ForegroundColor Yellow
foreach ($p in @($dbMain, $dbMem)) {
    if (Test-Path $p) {
        $h = (Get-FileHash $p -Algorithm SHA256).Hash.Substring(0, 16)
        Write-Host ("  {0,-24} {1,12:N0} B  {2}" -f (Split-Path $p -Leaf), (Get-Item $p).Length, $h)
    }
}

# 直接查库确认人格与记忆
Write-Host ""
Write-Host "=== 库里的它（这是最终证据）===" -ForegroundColor Cyan
$probe = Join-Path $env:TEMP "molt-probe.py"
@'
import sqlite3, sys, json
data = sys.argv[1]
out = {}
try:
    c = sqlite3.connect("file:" + (data + "/data_v4.db").replace("\\", "/") + "?mode=ro", uri=True)
    cur = c.cursor()
    cur.execute("SELECT persona_id, substr(system_prompt,1,160) FROM personas")
    out["personas"] = cur.fetchall()
    cur.execute("SELECT count(*) FROM conversations"); out["conversations"] = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM platform_message_history"); out["history"] = cur.fetchone()[0]
    c.close()
except Exception as e:
    out["main_error"] = str(e)
try:
    c = sqlite3.connect("file:" + (data + "/plugin_data/astrbot_plugin_memory_companion/memory_companion.db").replace("\\", "/") + "?mode=ro", uri=True)
    cur = c.cursor()
    cur.execute("SELECT count(*) FROM memories"); out["memories"] = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM knowledge_nodes"); out["knowledge_nodes"] = cur.fetchone()[0]
    c.close()
except Exception as e:
    out["mem_error"] = str(e)
print(json.dumps(out, ensure_ascii=False, indent=2))
'@ | Set-Content -Path $probe -Encoding UTF8

& $Python $probe $DataDir
