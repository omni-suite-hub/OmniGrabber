# OmniVideo Windows PowerShell Launcher
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  🚀 OmniVideo 本地私有化视频下载站 (Windows PowerShell)" -ForegroundColor Green
Write-Host "  🛡️ 100% 本地运行 · 零云端中转 · 绝不消耗作者服务器" -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Cyan

Set-Location -Path $PSScriptRoot

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    $pythonCmd = Get-Command py -ErrorAction SilentlyContinue
}

if (-not $pythonCmd) {
    Write-Host "[错误] 系统中未找到 Python。请访问 https://www.python.org/ 安装并添加到 PATH。" -ForegroundColor Red
    pause
    exit 1
}

Write-Host "[1/2] 正在调用 Python 启动器..." -ForegroundColor Gray
& $pythonCmd.Source start_server.py
