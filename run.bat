@echo off
chcp 65001 >nul
title OmniVideo 本地私有化视频下载站 (Windows)
echo ============================================================
echo   🚀 OmniVideo 本地私有化下载站 - Windows 一键启动器
echo   🛡️ 100%% 本地运行 · 不消耗任何云端服务器 · 隐私安全
echo ============================================================
echo.

cd /d "%~dp0"

:: 检查 Python 是否安装
where python >nul 2>nul
if %errorlevel% neq 0 (
    where py >nul 2>nul
    if %errorlevel% neq 0 (
        echo [错误] 未检测到 Python，请先前往 https://www.python.org/ 下载并安装 Python 3.9+。
        echo [提示] 安装时请务必勾选 "Add python.exe to PATH"！
        echo.
        pause
        exit /b 1
    ) else (
        set PY_BIN=py
    )
) else (
    set PY_BIN=python
)

echo [1/3] 检测到 Python 运行环境:
%PY_BIN% --version

echo.
echo [2/3] 正在启动跨平台服务调度器...
%PY_BIN% start_server.py

if %errorlevel% neq 0 (
    echo.
    echo [!] 服务异常退出，请检查上方报错信息。
    pause
)
