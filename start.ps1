[CmdletBinding()]
param(
    [switch]$Background
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "              ZCode2API 启动脚本                        " -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan

# 1. 检查 Python 虚拟环境
$VenvPy = Join-Path $ScriptDir "venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Write-Host "[*] 未检测到虚拟环境，正在创建 venv..." -ForegroundColor Yellow
    python -m venv venv
    if ($LASTEXITCODE -ne 0) {
        Write-Error "创建虚拟环境失败，请确认系统已安装 Python 3.10+"
        exit 1
    }
    Write-Host "[*] 正在安装 Python 依赖库..." -ForegroundColor Yellow
    & $VenvPy -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Error "安装依赖失败，请检查网络设置"
        exit 1
    }
}

# 2. 检查 captcha_node 依赖
$CaptchaNodeModules = Join-Path $ScriptDir "captcha_node\node_modules"
if (-not (Test-Path $CaptchaNodeModules)) {
    Write-Host "[*] 正在安装 captcha_node 依赖 (npm install)..." -ForegroundColor Yellow
    Push-Location (Join-Path $ScriptDir "captcha_node")
    npm install
    Pop-Location
}

# 3. 检查是否已有运行中的实例
$PidFile = Join-Path $ScriptDir "zcode2api.pid"
$PortFile = Join-Path $ScriptDir "zcode2api.port"

if (Test-Path $PidFile) {
    $OldPid = (Get-Content $PidFile -ErrorAction SilentlyContinue).Trim()
    if ($OldPid -and (Get-Process -Id $OldPid -ErrorAction SilentlyContinue)) {
        $ActivePort = if (Test-Path $PortFile) { (Get-Content $PortFile).Trim() } else { "3335" }
        Write-Host "[!] 服务已在运行中 (PID: $OldPid)!" -ForegroundColor Yellow
        Write-Host "[*] 后台管理 : http://127.0.0.1:$ActivePort/admin" -ForegroundColor Green
        Write-Host "[*] 登录页面 : http://127.0.0.1:$ActivePort/admin/login" -ForegroundColor Green
        Write-Host "[*] 对话端点 : http://127.0.0.1:$ActivePort/v1/messages" -ForegroundColor Green
        Write-Host "[*] 如需停止或重启，请先运行 stop.bat 或 .\stop.ps1" -ForegroundColor Yellow
        exit 0
    }
}

# 4. 启动服务
if ($Background) {
    Write-Host "[*] 正在后台启动 ZCode2API 服务 (默认端口 3335，若占用将自动递增顺延)..." -ForegroundColor Green
    $proc = Start-Process -FilePath $VenvPy -ArgumentList @("main.py", "serve") -WorkingDirectory $ScriptDir -PassThru

    # 等待服务就绪并读取端口
    $Timeout = 12
    while ($Timeout -gt 0 -and (-not (Test-Path $PortFile))) {
        Start-Sleep -Seconds 1
        $Timeout--
    }

    $Port = if (Test-Path $PortFile) { (Get-Content $PortFile).Trim() } else { "3335" }
    $PidVal = if (Test-Path $PidFile) { (Get-Content $PidFile).Trim() } else { $proc.Id }

    Write-Host ""
    Write-Host "========================================================" -ForegroundColor Green
    Write-Host "  ZCode2API 服务启动成功！" -ForegroundColor Green
    Write-Host "  - 进程 PID : $PidVal" -ForegroundColor White
    Write-Host "  - 运行端口 : $Port" -ForegroundColor White
    Write-Host "  - 后台管理 : http://127.0.0.1:$Port/admin" -ForegroundColor Cyan
    Write-Host "  - 登录页面 : http://127.0.0.1:$Port/admin/login" -ForegroundColor Cyan
    Write-Host "  - 对话端点 : http://127.0.0.1:$Port/v1/messages" -ForegroundColor Cyan
    Write-Host "  - 默认密码 : zcode" -ForegroundColor White
    Write-Host "  - 停止命令 : 运行 stop.bat 或 .\stop.ps1" -ForegroundColor Yellow
    Write-Host "========================================================" -ForegroundColor Green
} else {
    Write-Host "[*] 正在启动 ZCode2API 服务 (按 Ctrl+C 退出)..." -ForegroundColor Green
    Write-Host "  - 后台管理 : http://127.0.0.1:3335/admin" -ForegroundColor Cyan
    Write-Host "  - 默认密码 : zcode" -ForegroundColor White
    Write-Host ""
    & $VenvPy main.py serve
}
