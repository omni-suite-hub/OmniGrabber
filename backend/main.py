"""OmniVideo 服务入口：FastAPI + 下载引擎 + 前端页面托管。"""

from __future__ import annotations
import asyncio
import ipaddress
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
from pydantic import BaseModel, Field

from engine import Downloader
import db

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
DOWNLOADS_DIR = BASE_DIR / "downloads"
TOKEN = os.environ.get("OMNI_TOKEN", "").strip()
OMNI_USER = os.environ.get("OMNI_USER", "admin").strip()
OMNI_PASS = os.environ.get("OMNI_PASS", "omnivideo2026").strip()
MAX_URL_LEN = 2048

app = FastAPI(title="OmniVideo", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# 数据库先行：Downloader 启动恢复依赖 tasks 表
db.init_db()
db.ensure_admin(OMNI_USER, OMNI_PASS)
downloader = Downloader(str(DOWNLOADS_DIR), tasks_file=str(BASE_DIR / "tasks.json"))


SESSION_COOKIE = "omni_session"


def _current_user(request: Request):
    """从 Cookie / 旧 token / 查询参数解析当前登录用户，返回 {user_id, username, is_admin} 或 None。"""
    c = request.cookies.get(SESSION_COOKIE)
    if c:
        if TOKEN and c == TOKEN:  # 兼容旧 Cookie（值为管理员 token）
            row = db.get_user_by_name(OMNI_USER)
            if row:
                return {"user_id": row["id"], "username": row["username"],
                        "is_admin": True, "status": "approved", "active": True}
        uid = db.check_session(c)
        if uid is not None:
            row = db.get_user_by_id(uid)
            if row:
                is_adm = bool(row["is_admin"])
                st = "approved" if is_adm else (row["status"] or "pending")
                return {"user_id": row["id"], "username": row["username"],
                        "is_admin": is_adm, "status": st,
                        "active": is_adm or st == "approved"}
    q = request.query_params.get("token")
    h = request.headers.get("x-omni-token")
    if TOKEN and (q == TOKEN or h == TOKEN):
        row = db.get_user_by_name(OMNI_USER)
        if row:
            return {"user_id": row["id"], "username": row["username"],
                    "is_admin": True, "status": "approved", "active": True}
    # 插件专用：X-Omni-Session 长会话 token（/api/plugin/login 签发）
    hs = request.headers.get("x-omni-session")
    if hs:
        uid = db.check_session(hs)
        if uid is not None:
            row = db.get_user_by_id(uid)
            if row:
                is_adm = bool(row["is_admin"])
                st = "approved" if is_adm else (row["status"] or "pending")
                return {"user_id": row["id"], "username": row["username"],
                        "is_admin": is_adm, "status": st,
                        "active": is_adm or st == "approved"}

    # 本机回环免鉴权：仅在显式开启 OMNI_LOCAL_TRUST=1 时生效
    # （供桌面端 Chrome 扩展零摩擦直连本机后端；生产经 nginx 反代，不应开启）
    if os.environ.get("OMNI_LOCAL_TRUST") == "1" and request.client \
            and request.client.host in ("127.0.0.1", "::1", "localhost"):
        row = db.get_user_by_name(OMNI_USER)
        if row:
            return {"user_id": row["id"], "username": row["username"],
                    "is_admin": True, "status": "approved", "active": True}

    return None

    return None


def _require_user(request: Request):
    u = _current_user(request)
    if not u:
        raise HTTPException(401, "未登录或访问令牌无效")
    return u


def _require_active_user(request: Request):
    """必须已登录且账号已审核通过（管理员直接放行），否则 403。"""
    u = _require_user(request)
    if not u.get("active"):
        if u.get("status") == "rejected":
            raise HTTPException(403, "账号未通过审核，请联系管理员")
        raise HTTPException(403, "账号审核中，暂不可使用下载功能，请等待管理员审核通过")
    return u


def _require_admin(request: Request):
    u = _require_user(request)
    if not u.get("is_admin"):
        raise HTTPException(403, "仅管理员可操作")
    return u


def _set_session_cookie(resp, token: str):
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=db.SESSION_DAYS * 86400,
        httponly=True, samesite="lax", path="/",
    )


@app.post("/api/login")
async def login(request: Request):
    """账号密码 或 管理员令牌 登录，写入会话 Cookie。"""
    body = None
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        body = {}
    user = str(body.get("user", "") or "")
    pwd = str(body.get("pass", "") or "")
    token = body.get("token")
    if token is None:
        token = request.query_params.get("token")
    # 管理员 token 直连（API/插件兼容）
    if token is not None and TOKEN and token == TOKEN:
        resp = JSONResponse({"ok": True, "mode": "token"})
        _set_session_cookie(resp, TOKEN)
        return resp
    # 账号密码登录
    ok, msg, uid = db.check_login(user, pwd)
    if not ok:
        raise HTTPException(401, msg)
    resp = JSONResponse({"ok": True, "mode": "user", "user": user})
    _set_session_cookie(resp, db.create_session(uid))
    return resp


@app.post("/api/register")
async def register(request: Request):
    """注册新用户并自动登录。"""
    body = None
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        body = {}
    user = str(body.get("user", "") or "")
    pwd = str(body.get("pass", "") or "")
    ok, msg = db.create_user(user, pwd)
    if not ok:
        raise HTTPException(400, msg)
    _, _, uid = db.check_login(user, pwd)
    resp = JSONResponse({"ok": True, "user": user})
    _set_session_cookie(resp, db.create_session(uid))
    return resp


@app.post("/api/plugin/login")
async def plugin_login(request: Request):
    """插件专用登录：账号密码换取长会话 token（插件持久化，请求头 X-Omni-Session 携带）。"""
    body = None
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        body = {}
    user = str(body.get("user", "") or "")
    pwd = str(body.get("pass", "") or "")
    ok, msg, uid = db.check_login(user, pwd)
    if not ok:
        raise HTTPException(401, msg)
    token = db.create_session(uid)
    resp = JSONResponse({"ok": True, "token": token, "user": user})
    _set_session_cookie(resp, token)
    return resp


@app.post("/api/logout")
def logout(request: Request):
    c = request.cookies.get(SESSION_COOKIE)
    if c:
        db.delete_session(c)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


def _check_url(url: str):
    if not url or len(url) > MAX_URL_LEN:
        raise HTTPException(400, "链接为空或过长")
    if not re.match(r"^https?://", url, re.I):
        raise HTTPException(400, "仅支持 http/https 链接")
    host = urlparse(url).hostname
    if not host:
        raise HTTPException(400, "链接缺少域名")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise HTTPException(400, "域名无法解析")
    ips = sorted({info[4][0] for info in infos})
    if not ips:
        raise HTTPException(400, "域名无法解析")
    # 仅当所有解析地址均为内网/回环/链路本地/组播时才拒绝（避免公网 IPv6 隧道前缀误伤）
    if all(ipaddress.ip_address(ip).is_private or ipaddress.ip_address(ip).is_loopback
           or ipaddress.ip_address(ip).is_link_local or ipaddress.ip_address(ip).is_multicast
           for ip in ips):
        raise HTTPException(400, "不允许下载内网/保留地址的内容")


class UrlIn(BaseModel):
    url: str = Field(..., max_length=MAX_URL_LEN)
    quality: str = "compat"


QUALITY_OPTIONS = {"compat", "best", "audio"}


@app.get("/api/me")
def me(request: Request):
    """返回当前登录用户信息（未登录返回 null）。"""
    u = _current_user(request)
    if not u:
        return {"user": None}
    return {"user": u["username"], "user_id": u["user_id"],
            "is_admin": u["is_admin"], "status": u.get("status", "pending"),
            "active": bool(u.get("active"))}


@app.get("/api/health")
def health():
    return {"ok": True, "time": int(time.time()), "downloads": db.task_list(None, 1, 1)[1]}


# ---------- 管理员审核 ----------

@app.get("/api/admin/pending")
def admin_pending(request: Request):
    """待审核用户列表（管理员）。"""
    _require_admin(request)
    return {"users": db.list_pending_users(), "count": db.count_pending()}


@app.get("/api/admin/users")
def admin_users(request: Request):
    """全部用户列表（管理员）。"""
    _require_admin(request)
    return {"users": db.list_all_users()}


@app.post("/api/admin/users/{uid}/approve")
def admin_approve(uid: int, request: Request):
    _require_admin(request)
    if not db.set_user_status(uid, "approved"):
        raise HTTPException(404, "用户不存在或为管理员")
    return {"ok": True, "status": "approved"}


@app.post("/api/admin/users/{uid}/reject")
def admin_reject(uid: int, request: Request):
    _require_admin(request)
    if not db.set_user_status(uid, "rejected"):
        raise HTTPException(404, "待审核用户不存在")
    return {"ok": True, "status": "rejected"}


@app.post("/api/download")
def start_download(body: UrlIn, request: Request):
    u = _require_active_user(request)
    url = body.url.strip()
    _check_url(url)
    quality = body.quality if body.quality in QUALITY_OPTIONS else "compat"
    tid = downloader.create(url, quality=quality, user_id=u["user_id"])
    return downloader.get(tid, u["user_id"])


@app.get("/api/tasks")
def list_tasks(request: Request, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    u = _require_user(request)
    target_uid = None if u.get("is_admin") else u["user_id"]
    tasks, total = downloader.list(target_uid, page, page_size)
    return {"tasks": tasks, "total": total, "page": page, "page_size": page_size}


@app.get("/api/tasks/{tid}")
def get_task(tid: str, request: Request):
    u = _require_user(request)
    target_uid = None if u.get("is_admin") else u["user_id"]
    t = downloader.get(tid, target_uid)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t


@app.delete("/api/tasks/{tid}")
def delete_task(tid: str, request: Request):
    u = _require_user(request)
    target_uid = None if u.get("is_admin") else u["user_id"]
    if not downloader.delete(tid, target_uid):
        raise HTTPException(404, "任务不存在")
    return {"ok": True, "deleted": True}


@app.post("/api/tasks/{tid}/retry")
def retry_task(tid: str, request: Request):
    u = _require_user(request)
    target_uid = None if u.get("is_admin") else u["user_id"]
    t = downloader.retry(tid, target_uid)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t


@app.post("/api/tasks/clear-done")
def clear_done_tasks(request: Request):
    u = _require_user(request)
    target_uid = None if u.get("is_admin") else u["user_id"]
    count = db.task_clear_finished(target_uid)
    return {"ok": True, "cleared": count}


@app.api_route("/api/files/{tid}", methods=["GET", "HEAD"])
def get_file(tid: str, request: Request):
    u = _require_active_user(request)
    target_uid = None if u.get("is_admin") else u["user_id"]
    t = downloader.get(tid, target_uid)
    if not t:
        raise HTTPException(404, "任务不存在")
    if t["status"] != "done" or not t.get("file_path") or not os.path.exists(t["file_path"]):
        raise HTTPException(409, "文件尚未就绪")
    fpath = t["file_path"]
    ext = os.path.splitext(fpath)[1].lower()
    media_type = "audio/mpeg" if ext == ".mp3" else "video/mp4"
    return FileResponse(
        fpath,
        filename=t.get("file_name") or os.path.basename(fpath),
        media_type=media_type,
    )


class OpenLocalIn(BaseModel):
    path: str
    action: str = "play"  # "play" or "locate"


@app.post("/api/tasks/{tid}/play")
def play_task(tid: str, request: Request):
    """使用本地系统默认播放器打开已完成的视频文件"""
    u = _require_user(request)
    target_uid = None if u.get("is_admin") else u["user_id"]
    t = downloader.get(tid, target_uid)
    if not t:
        raise HTTPException(404, "任务不存在")
    fp = t.get("file_path")
    if not fp or not os.path.exists(fp):
        task_dir = os.path.join(DOWNLOAD_DIR, tid)
        if os.path.isdir(task_dir):
            for f in os.listdir(task_dir):
                if not f.startswith(".") and f.endswith((".mp4", ".mkv", ".webm", ".mp3", ".m4a", ".mov", ".ts")):
                    fp = os.path.join(task_dir, f)
                    break
    if not fp or not os.path.exists(fp):
        raise HTTPException(404, "本地视频文件不存在")
    try:
        sys_name = platform.system().lower()
        if sys_name == "darwin":
            subprocess.Popen(["open", fp])
        elif sys_name == "windows":
            os.startfile(fp)
        else:
            subprocess.Popen(["xdg-open", fp])
        return {"ok": True, "message": "已在系统默认播放器中打开", "path": fp}
    except Exception as e:
        raise HTTPException(500, f"启动播放器失败: {e}")


@app.post("/api/tasks/{tid}/locate")
def locate_task(tid: str, request: Request):
    """在 macOS 访达 (Finder) 中高亮定位已完成的视频文件"""
    u = _require_user(request)
    target_uid = None if u.get("is_admin") else u["user_id"]
    t = downloader.get(tid, target_uid)
    if not t:
        raise HTTPException(404, "任务不存在")
    fp = t.get("file_path")
    if not fp or not os.path.exists(fp):
        task_dir = os.path.join(DOWNLOAD_DIR, tid)
        if os.path.isdir(task_dir):
            for f in os.listdir(task_dir):
                if not f.startswith(".") and f.endswith((".mp4", ".mkv", ".webm", ".mp3", ".m4a", ".mov", ".ts")):
                    fp = os.path.join(task_dir, f)
                    break
    if not fp or not os.path.exists(fp):
        raise HTTPException(404, "本地视频文件不存在")
    try:
        sys_name = platform.system().lower()
        if sys_name == "darwin":
            subprocess.Popen(["open", "-R", fp])
        elif sys_name == "windows":
            norm = os.path.normpath(fp)
            subprocess.Popen(["explorer.exe", f"/select,{norm}"])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(fp)])
        return {"ok": True, "message": "已在系统文件管理器中定位", "path": fp}
    except Exception as e:
        raise HTTPException(500, f"打开文件管理器失败: {e}")


@app.post("/api/open-local")
def open_local(body: OpenLocalIn, request: Request):
    """通过系统级调用打开或定位任意本地已落盘文件"""
    u = _require_user(request)
    path = body.path.strip()
    if not path or not os.path.exists(path):
        raise HTTPException(404, f"路径不存在: {path}")
    if os.path.isfile(path) and os.path.getsize(path) == 0:
        raise HTTPException(400, "文件仍在下载写入中（体积为 0 字节），请等待下载完成后再打开")
    action = body.action.lower()
    try:
        sys_name = platform.system().lower()
        if action == "locate":
            if sys_name == "darwin":
                subprocess.Popen(["open", "-R", path])
            elif sys_name == "windows":
                norm = os.path.normpath(path)
                subprocess.Popen(["explorer.exe", f"/select,{norm}"])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
            return {"ok": True, "action": "locate", "path": path}
        else:
            if sys_name == "darwin":
                subprocess.Popen(["open", path])
            elif sys_name == "windows":
                os.startfile(path)
            else:
                subprocess.Popen(["xdg-open", path])
            return {"ok": True, "action": "play", "path": path}
    except Exception as e:
        raise HTTPException(500, f"打开失败: {e}")


@app.get("/api/resolve")
def resolve_media(request: Request, url: str = Query(..., max_length=MAX_URL_LEN), quality: str = Query("compat")):
    """纯元数据解析：0 磁盘写入，秒级获取媒体信息及直链"""
    u = _require_active_user(request)
    url = url.strip()
    _check_url(url)
    q = quality if quality in QUALITY_OPTIONS else "compat"
    try:
        meta = downloader.resolve(url, quality=q)
        return {"ok": True, "data": meta}
    except Exception as e:
        raise HTTPException(400, f"解析失败: {e}")


@app.api_route("/api/stream", methods=["GET", "HEAD"])
def stream_media(request: Request, url: str = Query(..., max_length=MAX_URL_LEN), quality: str = Query("compat")):
    """0 磁盘实时管道流：通过 ffmpeg pipe:1 边拉取边封装边向用户本地磁盘推流"""
    u = _require_active_user(request)
    url = url.strip()
    _check_url(url)
    q = quality if quality in QUALITY_OPTIONS else "compat"

    try:
        gen, filename, mime, filesize = downloader.stream_pipe(url, quality=q)
    except Exception as e:
        raise HTTPException(400, f"启动流管道失败: {e}")

    encoded_name = quote(filename)
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
        "Content-Type": mime,
    }
    if filesize:
        headers["X-Approx-Size"] = str(filesize)

    if request.method == "HEAD":
        return Response(status_code=200, headers=headers)

    return StreamingResponse(
        gen,
        media_type=mime,
        headers=headers,
    )


@app.get("/")
def index(request: Request):
    return FileResponse(FRONTEND_DIR / "index.html")


# ---------- 后台清理：删除超过 24h 的下载产物 ----------
async def _cleanup_loop():
    while True:
        await asyncio.sleep(3600)
        try:
            cutoff = time.time() - 24 * 3600
            page = 1
            while True:
                rows, total = downloader.list(None, page, 100)
                if not rows:
                    break
                for t in rows:
                    if t["created_at"] < cutoff and t["status"] in ("done", "failed"):
                        downloader.delete(t["id"])
                if page * 100 >= total:
                    break
                page += 1
            for name in os.listdir(DOWNLOADS_DIR):
                p = DOWNLOADS_DIR / name
                if p.is_dir() and p.stat().st_mtime < cutoff:
                    shutil_rmtree(p)
        except Exception:
            pass


def shutil_rmtree(p):
    import shutil
    shutil.rmtree(p, ignore_errors=True)


@app.on_event("startup")
async def _startup():
    db.init_db()
    db.ensure_admin(OMNI_USER, OMNI_PASS)
    asyncio.create_task(_cleanup_loop())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8090")))
