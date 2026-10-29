@echo off
chcp 65001 >nul
title 停止 audio-transcribe

echo 正在停止服务...

REM 关掉占用 8787 端口的 python 进程
for /f "tokens=5" %%a in ('netstat -ano ^^| findstr ":8787 " ^^| findstr "LISTENING"') do (
    taskkill /f /pid %%a >nul 2>&1
)

echo.
echo 已停止。
echo.
timeout /t 2 >nul
