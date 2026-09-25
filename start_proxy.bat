@echo off
chcp 65001 >nul
title ZCode API Gateway (http://127.0.0.1:8080)
cd /d "%~dp0proxy"

if not exist "zcode-proxy.exe" (
    echo [错误] 未在 proxy 目录找到 zcode-proxy.exe！
    pause
    exit /b 1
)

if not exist "config.yaml" (
    echo [错误] 未在 proxy 目录找到 config.yaml！
    pause
    exit /b 1
)

:: 寻找可用的 Python 解释器
set "PY_CMD=python"
if exist "%~dp0venv\Scripts\python.exe" set "PY_CMD=%~dp0venv\Scripts\python.exe"

:MAIN_MENU
cls
set "CURR_ACCOUNT="
set "CURR_QUOTA="
if exist "account_info.py" (
    for /f "delims=" %%i in ('call "%PY_CMD%" account_info.py 2^>nul') do set "CURR_ACCOUNT=%%i"
    for /f "delims=" %%j in ('call "%PY_CMD%" account_info.py quota 2^>nul') do set "CURR_QUOTA=%%j"
)
if "%CURR_ACCOUNT%"=="" set "CURR_ACCOUNT=[未登录] 尚未绑定任何账号"

echo ========================================================================
echo                      ZCode API Gateway 交互控制台
echo ========================================================================
echo  [服务地址] http://127.0.0.1:8080/v1
echo  [当前账号] %CURR_ACCOUNT%
if defined CURR_QUOTA echo %CURR_QUOTA%
echo  [支持协议] OpenAI (/v1/chat/completions) ^| Anthropic (/v1/messages)
echo  [可用模型] GLM-5.3 , GLM-5.3-Flash , GLM-5.2 , GLM-5-Turbo 等
echo ------------------------------------------------------------------------
echo  [1] 启动代理服务 (默认直接回车启动)
echo  [2] 登录新账号 (智谱 BigModel - 浏览器一键授权 [国内])
echo  [3] 登录新账号 (Z.AI 平台 - 浏览器一键授权 [国外])
echo  [4] 导入已有配置 (从 ~/.zcode/v2/config.json 自动导入)
echo  [5] 粘贴 URL 登录 (无头/远程服务器手动粘贴授权地址)
echo  [6] 查看当前登录账号详细信息 (额度看板、有效时间、模型配额)
echo  [7] 退出当前账号登录 (Auth Logout)
echo  [8] 检测并领取体验套餐配额 (Claim Packages)
echo  [9] 清理 8080 端口占用 (杀掉残留旧进程)
echo  [0] 退出控制台
echo ========================================================================
set "choice=1"
set /p choice="请输入选项编号 [默认 1，直接回车启动服务]: "
set "choice=%choice: =%"

if "%choice%"=="1" goto :START_SERVICE
if "%choice%"=="2" goto :LOGIN_BIGMODEL
if "%choice%"=="3" goto :LOGIN_ZAI
if "%choice%"=="4" goto :IMPORT_CONFIG
if "%choice%"=="5" goto :PASTE_LOGIN
if "%choice%"=="6" goto :CHECK_STATUS
if "%choice%"=="7" goto :LOGOUT_ACCOUNT
if "%choice%"=="8" goto :CLAIM_PACKAGES
if "%choice%"=="9" goto :KILL_PORT
if "%choice%"=="0" exit /b 0

echo [提示] 输入无效，请重新选择。
ping 127.0.0.1 -n 2 >nul
goto :MAIN_MENU

:START_SERVICE
echo.
echo 正在检查 8080 端口...
netstat -ano | findstr ":8080 " | findstr "LISTENING" >nul
if %errorlevel% neq 0 goto :RUN_NOW

echo ------------------------------------------------------------------------
echo [提示] 检测到 8080 端口已被占用（已有代理在后台运行）
set "reopt=1"
set /p reopt="是否强制结束旧进程并重新启动？[1=是，2=返回菜单，默认 1]: "
set "reopt=%reopt: =%"
if "%reopt%"=="2" goto :MAIN_MENU

echo 正在清理旧的 zcode-proxy 进程...
taskkill /f /im zcode-proxy.exe >nul 2>&1
ping 127.0.0.1 -n 2 >nul

:RUN_NOW
echo.
echo ========================================================================
echo  代理服务正在运行中...
echo  - 当前账号:       %CURR_ACCOUNT%
if defined CURR_QUOTA echo %CURR_QUOTA%
echo  - OpenAI 接口:    http://127.0.0.1:8080/v1
echo  - Anthropic 接口: http://127.0.0.1:8080/v1
echo  按 Ctrl+C 可停止代理并返回控制台
echo ========================================================================
echo.
zcode-proxy.exe serve config.yaml
echo.
echo [提示] 代理服务已停止。
pause
goto :MAIN_MENU

:LOGIN_BIGMODEL
echo.
echo ========================================================================
echo  准备登录 智谱 BigModel 账号【国内】
echo  程序将自动打开浏览器进行 OAuth 授权，完成后将自动保存凭据。
echo ========================================================================
echo.
zcode-proxy.exe auth login bigmodel
echo.
pause
goto :MAIN_MENU

:LOGIN_ZAI
echo.
echo ========================================================================
echo  准备登录 Z.AI 平台账号【国外/国际版】
echo  程序将自动打开浏览器进行 OAuth 授权，完成后将自动保存凭据。
echo ========================================================================
echo.
zcode-proxy.exe auth login zai
echo.
pause
goto :MAIN_MENU

:IMPORT_CONFIG
echo.
echo ========================================================================
echo  正在从本地 ~/.zcode/v2/config.json 导入配置...
echo ========================================================================
echo.
zcode-proxy.exe auth login bigmodel --import
echo.
pause
goto :MAIN_MENU

:PASTE_LOGIN
echo.
echo ========================================================================
echo  手动粘贴授权重定向 URL 登录
echo ------------------------------------------------------------------------
echo  [1] 智谱 BigModel 【国内】
echo  [2] Z.AI 平台     【国外】
echo  [0] 返回上级菜单
echo ========================================================================
set "popt=1"
set /p popt="请选择平台 [1 或 2，默认 1]: "
set "popt=%popt: =%"
if "%popt%"=="0" goto :MAIN_MENU
if "%popt%"=="2" (
    echo.
    zcode-proxy.exe auth login zai --paste
) else (
    echo.
    zcode-proxy.exe auth login bigmodel --paste
)
echo.
pause
goto :MAIN_MENU

:CHECK_STATUS
echo.
if exist "account_info.py" (
    call "%PY_CMD%" account_info.py detail
) else (
    zcode-proxy.exe auth status
)
echo.
pause
goto :MAIN_MENU

:LOGOUT_ACCOUNT
echo.
echo ========================================================================
echo  退出账号登录
echo ========================================================================
set "lopt=N"
set /p lopt="确认要清除当前本地已保存的登录凭据吗？[Y/N，默认 N]: "
set "lopt=%lopt: =%"
if /i "%lopt%"=="Y" (
    zcode-proxy.exe auth logout
    echo [OK] 凭据已清理。
) else (
    echo [已取消]
)
echo.
pause
goto :MAIN_MENU

:CLAIM_PACKAGES
echo.
echo ========================================================================
echo  正在检测并领取体验套餐配额...
echo ========================================================================
echo.
zcode-proxy.exe claim now
echo.
pause
goto :MAIN_MENU

:KILL_PORT
echo.
echo 正在清理旧的 zcode-proxy 进程...
taskkill /f /im zcode-proxy.exe >nul 2>&1
echo [OK] 清理完毕。
ping 127.0.0.1 -n 2 >nul
goto :MAIN_MENU
