$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$py  = "G:\molt-p0\venv313\Scripts\python.exe"
$src = "G:\molt-p0\astrbot-src"
$root = "G:\molt-p0\winroot"
$log = "G:\molt-p0\astrbot-win-login.log"

$env:ASTRBOT_ROOT = $root
$env:PYTHONIOENCODING = "utf-8"

$before = (Get-FileHash "$root\data\data_v4.db" -Algorithm SHA256).Hash.ToLower()
Write-Output "启动前 data_v4.db sha256: $before"

$p = Start-Process -FilePath $py `
    -ArgumentList "main.py" `
    -WorkingDirectory $src `
    -RedirectStandardOutput $log `
    -RedirectStandardError "$log.err" `
    -PassThru -WindowStyle Hidden

Write-Output "pid=$($p.Id)  等待 45 秒让它写点东西 ……"
Start-Sleep -Seconds 45

$alive = -not $p.HasExited
Write-Output "仍在运行: $alive"

# 优雅停止：直接 Stop-Process 会留下 WAL，但这里我们正是要
# 验证「活着的库被热备」这条路径，所以故意让它留着 WAL。
if ($alive) {
    Stop-Process -Id $p.Id -Force
    Start-Sleep -Seconds 2
}

$after = (Get-FileHash "$root\data\data_v4.db" -Algorithm SHA256).Hash.ToLower()
Write-Output "停止后 data_v4.db sha256: $after"
Write-Output "数据变化: $($before -ne $after)"

Write-Output ""
Write-Output "=== data 目录现状 ==="
Get-ChildItem -Path "$root\data" -File | Sort-Object Name | ForEach-Object {
    Write-Output ("  {0,-24} {1,12} B   {2}" -f $_.Name, $_.Length, $_.LastWriteTime.ToString("HH:mm:ss"))
}

Write-Output ""
Write-Output "=== 启动日志尾部 ==="
if (Test-Path $log) {
    Get-Content $log -Encoding UTF8 -Tail 12 | ForEach-Object { Write-Output ("  " + $_) }
}
if (Test-Path "$log.err") {
    $e = Get-Content "$log.err" -Encoding UTF8 -Tail 8
    if ($e) {
        Write-Output "  --- stderr ---"
        $e | ForEach-Object { Write-Output ("  " + $_) }
    }
}
