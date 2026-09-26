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
set "CURR_ADMIN_KEY="
if exist "account_info.py" (
    for /f "delims=" %%i in ('call "%PY_CMD%" account_info.py 2^>nul') do set "CURR_ACCOUNT=%%i"
    for /f "delims=" %%j in ('call "%PY_CMD%" account_info.py quota 2^>nul') do set "CURR_QUOTA=%%j"
    for /f "delims=" %%k in ('call "%PY_CMD%" account_info.py admin_key 2^>nul') do set "CURR_ADMIN_KEY=%%k"
)
if "%CURR_ACCOUNT%"=="" set "CURR_ACCOUNT=[未登录] 尚未绑定任何账号"
if "%CURR_ADMIN_KEY%"=="" set "CURR_ADMIN_KEY=zcode"

echo ========================================================================
echo                 ZCode 多账号 API 网关交互控制台 (v2.0)
echo ========================================================================
echo  [服务地址] http://127.0.0.1:8080/v1
echo  [管理后台] http://127.0.0.1:8081/admin (按 A 启动 Web 后台，密码: %CURR_ADMIN_KEY%)
echo  [账号状态] %CURR_ACCOUNT%
if defined CURR_QUOTA echo %CURR_QUOTA%
echo  [支持协议] OpenAI (/v1/chat/completions) / Anthropic (/v1/messages)
echo  [可用模型] GLM-5.3 , GLM-5.3-Flash , GLM-5.2 , GLM-5-Turbo 等
echo ------------------------------------------------------------------------
echo  [1] 启动 ZCode 自愈代理服务 (推荐 1 - 实时 Token 吐字监控 + 自动秒切自愈 + 存活心跳)
echo  [P] 启动调试模式原生代理 (zcode-proxy.exe serve debug 逐请求诊断输出)
echo  [U] 启动交互式原生终端 (zcode-proxy.exe 官方完整 TUI 控制台)
echo  [2] 登录新账号 (智谱 BigModel - 浏览器授权并自动入库多账号池)
echo  [3] 登录新账号 (Z.AI 全球平台 - 浏览器授权并自动入库多账号池)
echo  [4] 导入/保存 ZCode 账号 (一键捕获本地 ZCode 与 Z-Accounts 全部账号)
echo  [5] 粘贴 URL 登录 (手动粘贴授权重定向地址加入账号池)
echo  [6] 查看账号池全部账号状态 (多账号列表、额度看板、可用性明细)
echo  [7] 多账号池维护与管理 (删除指定账号或清空凭据)
echo  [8] 检测并一键领取全部账号体验套餐配额 (Claim Packages)
echo  [9] 清理 8080 端口占用 (杀掉残留旧进程)
echo  [M] 查看与修改后台管理密码 (当前密码: %CURR_ADMIN_KEY%)
echo  [S] 切换当前活动账号 (查看全部账号列表 / 手动切换 / 自动选优)
echo  [A] 启动 Web 多账号可视化管理后台 (独立端口 8081)
echo  [0] 退出控制台
========================================================================
set "choice=1"
set /p choice="请输入选项编号 [默认 1，直接回车启动服务]: "
set "choice=%choice: =%"

if "%choice%"=="1" goto :START_SERVICE
if /i "%choice%"=="P" goto :RUN_NATIVE_PROXY
if /i "%choice%"=="U" goto :RUN_TUI_PROXY
if "%choice%"=="2" goto :LOGIN_BIGMODEL
if "%choice%"=="3" goto :LOGIN_ZAI
if "%choice%"=="4" goto :IMPORT_CONFIG
if "%choice%"=="5" goto :PASTE_LOGIN
if "%choice%"=="6" goto :CHECK_STATUS
if "%choice%"=="7" goto :LOGOUT_ACCOUNT
if "%choice%"=="8" goto :CLAIM_PACKAGES
if "%choice%"=="9" goto :KILL_PORT
if /i "%choice%"=="M" goto :CHANGE_PASSWORD
if /i "%choice%"=="S" goto :SWITCH_ACCOUNT
if /i "%choice%"=="A" goto :OPEN_ADMIN
if "%choice%"=="0" exit /b 0

echo [提示] 输入无效，请重新选择。
ping 127.0.0.1 -n 2 >nul
goto :MAIN_MENU

:START_SERVICE
echo.
call "%PY_CMD%" account_info.py check-and-auto-switch
call "%PY_CMD%" guardian.py
echo.
echo [提示] 代理服务已停止。
pause
goto :MAIN_MENU

:RUN_NATIVE_PROXY
echo.
echo ========================================================================
echo  启动调试模式原生代理 (zcode-proxy.exe serve debug)
echo  可实时显示每次调用的上游端点、延迟、Token 统计与详细诊断。
echo ========================================================================
echo.
call "%PY_CMD%" account_info.py check-and-auto-switch
zcode-proxy.exe serve debug config.yaml
echo.
echo [提示] 代理服务已停止。
pause
goto :MAIN_MENU

:RUN_TUI_PROXY
echo.
echo ========================================================================
echo  启动交互式原生终端 (zcode-proxy.exe)
echo  进入官方交互式 TUI 界面，可直观查看运行与请求状态。
echo ========================================================================
echo.
call "%PY_CMD%" account_info.py check-and-auto-switch
zcode-proxy.exe config.yaml
echo.
echo [提示] 代理服务已停止。
pause
goto :MAIN_MENU

:LOGIN_BIGMODEL
echo.
echo ========================================================================
echo  准备登录 智谱 BigModel 账号【国内】
echo  程序将自动打开浏览器进行 OAuth 授权，完成后将自动保存并并入多账号池。
echo ========================================================================
echo.
zcode-proxy.exe auth login bigmodel
call "%PY_CMD%" account_info.py sync_current_to_pool
echo.
pause
goto :MAIN_MENU

:LOGIN_ZAI
echo.
echo ========================================================================
echo  准备登录 Z.AI 平台账号【国外/国际版】
echo  程序将自动打开浏览器进行 OAuth 授权，完成后将自动保存并并入多账号池。
echo ========================================================================
echo.
zcode-proxy.exe auth login zai
call "%PY_CMD%" account_info.py sync_current_to_pool
echo.
pause
goto :MAIN_MENU

:IMPORT_CONFIG
echo.
call "%PY_CMD%" account_info.py import
echo.
pause
goto :MAIN_MENU

:PASTE_LOGIN
echo.
echo ========================================================================
echo  手动粘贴授权重定向 URL 登录并加入多账号池
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
call "%PY_CMD%" account_info.py sync_current_to_pool
echo.
pause
goto :MAIN_MENU

:CHECK_STATUS
echo.
call "%PY_CMD%" account_info.py detail
echo.
pause
goto :MAIN_MENU

:LOGOUT_ACCOUNT
echo.
call "%PY_CMD%" account_info.py manage
echo.
pause
goto :MAIN_MENU

:CLAIM_PACKAGES
echo.
call "%PY_CMD%" account_info.py claim
echo.
pause
goto :MAIN_MENU

:KILL_PORT
echo.
call "%PY_CMD%" account_info.py kill-port
ping 127.0.0.1 -n 2 >nul
goto :MAIN_MENU

:CHANGE_PASSWORD
echo.
call "%PY_CMD%" account_info.py passwd
echo.
pause
goto :MAIN_MENU

:SWITCH_ACCOUNT
echo.
call "%PY_CMD%" account_info.py switch
echo.
pause
goto :MAIN_MENU

:OPEN_ADMIN
echo.
echo 正在启动 Web 管理后台服务 (http://127.0.0.1:8081/admin)...
start "ZCode Admin Web" /min "%PY_CMD%" "%~dp0main.py" serve --port 8081
ping 127.0.0.1 -n 2 >nul
start http://127.0.0.1:8081/admin/accounts
goto :MAIN_MENU
