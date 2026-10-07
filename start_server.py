#!/usr/bin/env python3
"""
OmniVideo / OmniGrabber 跨平台极速启动脚本
支持操作系统：macOS / Windows / Linux
运行方式：python start_server.py
"""

import os
import sys
import shutil
import platform
import subprocess
import webbrowser
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
BACKEND_DIR = BASE_DIR / "backend"
PORT = int(os.environ.get("PORT", "8090"))
HOST = os.environ.get("HOST", "0.0.0.0")

def print_banner():
    os_name = platform.system()
    print("=" * 60)
    print("  🚀 OmniVideo 本地私有化视频下载站 (Self-Hosted Server)")
    print(f"  💻 运行环境: {os_name} {platform.release()} ({platform.machine()})")
    print(f"  🐍 Python 版本: {sys.version.split()[0]}")
    print("  🛡️  架构原则: 100% 本地运行 · 零云端中转 · 绝不消耗作者服务器")
    print("=" * 60)

def check_dependencies():
    """检查并自动安装所需 Python 依赖"""
    required_packages = ["fastapi", "uvicorn", "pydantic", "yt_dlp", "requests"]
    missing = []
    
    for pkg in required_packages:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg if pkg != "yt_dlp" else "yt-dlp")
            
    if missing:
        print(f"[*] 检测到缺少以下核心依赖: {', '.join(missing)}")
        print("[*] 正在为您自动安装依赖，请稍候...")
        req_file = BASE_DIR / "requirements.txt"
        pip_cmd = [sys.executable, "-m", "pip", "install", "-r", str(req_file)]
        
        # 国内镜像加速（如果需要）
        pip_cmd.extend(["-i", "https://pypi.tuna.tsinghua.edu.cn/simple"])
        
        try:
            subprocess.check_call(pip_cmd)
            print("[✓] 依赖安装完成！")
        except Exception as e:
            print(f"[!] 依赖安装失败，尝试官方源重试: {e}")
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(req_file)])

def check_ffmpeg():
    """检测系统中是否存在 ffmpeg，如果不存在给予友好跨平台指引"""
    ffmpeg_bin = shutil.which("ffmpeg")
    is_win = platform.system().lower() == "windows"
    
    # 也可以在根目录查找 ffmpeg.exe 或 ffmpeg
    if not ffmpeg_bin:
        cand = BASE_DIR / ("ffmpeg.exe" if is_win else "ffmpeg")
        if cand.exists():
            ffmpeg_bin = str(cand)
            os.environ["PATH"] = str(BASE_DIR) + os.pathsep + os.environ.get("PATH", "")

    if ffmpeg_bin:
        print(f"[✓] 已检测到 FFmpeg 引擎: {ffmpeg_bin}")
    else:
        print("[-] 提示: 未检测到系统 FFmpeg。基础下载可正常运行，但音画合流与切片转码推荐安装 FFmpeg:")
        if is_win:
            print("    Windows 安装推荐: 在终端运行 `winget install Gyan.FFmpeg` 或下载 ffmpeg.exe 放入本程序目录。")
        elif platform.system().lower() == "darwin":
            print("    Mac 安装推荐: 运行 `brew install ffmpeg`。")
        else:
            print("    Linux 安装推荐: 运行 `sudo apt-get install ffmpeg`。")

def start_server():
    """启动 FastAPI 服务并自动在浏览器中打开"""
    os.environ["OMNI_LOCAL_TRUST"] = "1"
    
    # 切换当前工作目录至项目根目录
    os.chdir(str(BASE_DIR))
    
    # 将 backend 目录加入 sys.path
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
        
    url = f"http://localhost:{PORT}/"
    print(f"\n[+] 正在启动服务: {url}")
    print(f"[+] 下载文件将保存在: {BASE_DIR / 'downloads'}")
    print("[+] 按 Ctrl+C 可随时停止服务。\n")
    
    # 异步在 1.5 秒后自动打开默认浏览器
    import threading
    def open_browser():
        time.sleep(1.5)
        try:
            webbrowser.open(url)
        except Exception:
            pass
            
    threading.Thread(target=open_browser, daemon=True).start()
    
    # 启动 uvicorn
    import uvicorn
    uvicorn.run("backend.main:app", host=HOST, port=PORT, reload=False)

if __name__ == "__main__":
    print_banner()
    check_dependencies()
    check_ffmpeg()
    start_server()
