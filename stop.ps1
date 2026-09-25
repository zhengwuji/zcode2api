[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "              ZCode2API 停止脚本                        " -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan

$PidFile = Join-Path $ScriptDir "zcode2api.pid"
$PortFile = Join-Path $ScriptDir "zcode2api.port"
$Stopped = $false

# 1. 按 PID 终止进程树
if (Test-Path $PidFile) {
    $TargetPid = (Get-Content $PidFile -ErrorAction SilentlyContinue).Trim()
    if ($TargetPid) {
        Write-Host "[*] 正在停止记录的进程 (PID: $TargetPid)..." -ForegroundColor Yellow
        try {
            taskkill /F /T /PID $TargetPid 2>$null | Out-Null
            $Stopped = $true
            Write-Host "[*] 已终止进程树 PID: $TargetPid" -ForegroundColor Green
        } catch {}
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

# 2. 按端口检查残留
$PortsToCheck = @()
if (Test-Path $PortFile) {
    $RecordedPort = (Get-Content $PortFile -ErrorAction SilentlyContinue).Trim()
    if ($RecordedPort) { $PortsToCheck += [int]$RecordedPort }
    Remove-Item $PortFile -Force -ErrorAction SilentlyContinue
}
$PortsToCheck += 3335
$PortsToCheck = $PortsToCheck | Select-Object -Unique

foreach ($p in $PortsToCheck) {
    try {
        $conns = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
        foreach ($conn in $conns) {
            $owningPid = $conn.OwningProcess
            if ($owningPid -and $owningPid -gt 4) {
                Write-Host "[*] 正在释放端口 $p (残留进程 PID: $owningPid)..." -ForegroundColor Yellow
                taskkill /F /T /PID $owningPid 2>$null | Out-Null
                $Stopped = $true
            }
        }
    } catch {}
}

Write-Host ""
if ($Stopped) {
    Write-Host "[OK] ZCode2API 服务已完全停止！" -ForegroundColor Green
} else {
    Write-Host "[*] 未发现正在运行的 ZCode2API 服务。" -ForegroundColor Yellow
}
Write-Host "========================================================" -ForegroundColor Cyan
