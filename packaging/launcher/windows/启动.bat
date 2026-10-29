@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title audio-transcribe

cd /d "%~dp0"

echo.
echo ==========================================
echo   audio-transcribe
echo ==========================================
echo.

REM ---- 1. 检查运行时是否完整
if not exist "python\python.exe" (
    echo [错误] 找不到 python\python.exe
    echo.
    echo 压缩包可能没解压完整。请重新解压，注意要
    echo 「全部解压」，不能只把 exe 拖出来。
    echo.
    pause
    exit /b 1
)

if not exist "app\web\server.py" (
    echo [错误] 找不到 app\web\server.py，压缩包不完整。
    echo 请重新解压。
    echo.
    pause
    exit /b 1
)

REM ---- 2. 数据目录跟着包走
set "AUDIO_TRANSCRIBE_HOME=%~dp0data"
if not exist "%AUDIO_TRANSCRIBE_HOME%" mkdir "%AUDIO_TRANSCRIBE_HOME%"

set "PATH=%~dp0bin;%~dp0python;%PATH%"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

REM ---- 3. 端口检查
set "PORT=8787"
netstat -ano 2>nul | findstr /r /c:"LISTENING" | findstr ":%PORT% " >nul
if not errorlevel 1 (
    echo [提示] 端口 %PORT% 已被占用。
    echo.
    echo 可能是上次的窗口没关，或者别的程序占了。
    echo 处理办法（任选一个）：
    echo   1. 关掉之前那个黑色窗口
    echo   2. 换个端口：右键 启动.bat → 编辑，
    echo      把下面这行的 8787 改成 8788 等
    echo.
    pause
    exit /b 1
)

echo 正在启动服务，请稍等...
echo 数据保存在：%AUDIO_TRANSCRIBE_HOME%
echo.

REM ---- 4. 后台起服务
set "LOG=%~dp0data\server.log"
start "" /b "python\python.exe" "app\web\server.py" --host 127.0.0.1 --port %PORT% --no-browser > "%LOG%" 2>&1

REM ---- 5. 等它就绪（最多 60 秒）
set /a TRIES=0
:WAITLOOP
set /a TRIES+=1
if %TRIES% gtr 60 goto FAILED

REM 用 python 自己探测，避免依赖 curl
"python\python.exe" -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%PORT%/api/health',timeout=1).status==200 else 1)" >nul 2>&1
if errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto WAITLOOP
)

REM ---- 6. 打开浏览器
echo 服务已就绪，正在打开浏览器...
start "" "http://127.0.0.1:%PORT%/"
echo.
echo ==========================================
echo   已启动：http://127.0.0.1:%PORT%/
echo ==========================================
echo.
echo   这个窗口不要关！
echo   用完点 停止.bat，或者直接关掉本窗口。
echo.
echo   浏览器没自动弹出？手动在地址栏输入上面那行。
echo.
echo   再点一次「开始」不会开新窗口，放心。
echo.

REM 保活：服务进程退出后本窗口才结束
:IDLE
timeout /t 3600 /nobreak >nul
goto IDLE

:FAILED
echo.
echo [错误] 启动超时（60 秒），服务没起来。
echo.
echo 最后几行日志：
echo ------------------------------------------
if exist "%LOG%" (
    powershell -NoProfile -Command "Get-Content -Path '%LOG%' -Tail 25" 2>nul || type "%LOG%"
) else (
    echo （没有日志文件）
)
echo ------------------------------------------
echo.
echo 常见原因：
echo   1. 解压不完整 —— 重新完整解压
echo   2. 杀毒软件拦截 —— 加白名单后重试
echo   3. 路径有中文或空格 —— 换到 D:\audio-transcribe 再试
echo   4. 端口被占用 —— 按上面提示换端口
echo.
pause
exit /b 1
