"""下载引擎：yt-dlp 优先，m3u8 分片下载兜底；任务持久化 + 并发控制。"""

from __future__ import annotations
import glob
import json
import os
import re
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
import shutil
from urllib.parse import urlparse

import yt_dlp
import requests

import db

from douyin import DouyinChannel, DouyinError

from m3u8dl import download_m3u8, M3U8Error

def find_node_runtime():
    """查找系统中可用的 Node.js 运行路径，供 yt-dlp 执行签名解密脚本。"""
    candidates = [
        shutil.which("node"),
        "/Users/eden/.local/bin/node",
        "/usr/local/bin/node",
        "/opt/homebrew/bin/node",
    ]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None

MAX_FILE_SIZE = 8 * 1024 * 1024 * 1024  # 单文件 8GB 上限（支持 4K）
PLATFORM_RE = [
    ("bilibili", r"bilibili\.com|b23\.tv"),
    ("douyin", r"douyin\.com|iesdouyin\.com"),
    ("youtube", r"youtube\.com|youtu\.be"),
    ("eporner", r"eporner\.com"),
]

def normalize_url(url: str) -> str:
    """标准化 URL：将 CDN 临时播放地址自动还原为主站网页地址，以调用专属提取器避免 403。"""
    if not url:
        return url
    if "eporner.com" in url:
        if "/video-" in url:
            return url
        m = re.search(r"eporner\.com/(?:[a-zA-Z0-9_\-]+/)?([a-zA-Z0-9]{10,12})(?:/|\.mp4|\?|$)", url)
        if m:
            return f"https://www.eporner.com/video-{m.group(1)}/"
    return url

_STATUS = ("queued", "parsing", "downloading", "merging", "done", "failed")


def detect_platform(url):
    norm = normalize_url(url)
    for name, pat in PLATFORM_RE:
        if re.search(pat, norm, re.I):
            return name
    if ".m3u8" in norm.lower():
        return "hls"
    return "other"


def _is_m3u8_url(url):
    try:
        p = urlparse(url)
        return ".m3u8" in p.path.lower() or ".m3u8" in p.query.lower()
    except Exception:
        return ".m3u8" in url.lower()


class Downloader:
    def __init__(self, downloads_dir, tasks_file=None, max_workers=2):
        self.downloads_dir = downloads_dir
        self.tasks_file = tasks_file
        os.makedirs(downloads_dir, exist_ok=True)
        self.sem = threading.BoundedSemaphore(max_workers)
        self.lock = threading.RLock()
        self.tasks = {}
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dl")
        self._load()

    # ---------- 任务 API（SQLite 持久化）----------
    def _load(self):
        db.task_mark_running_failed()
        # 迁移旧 tasks.json 记录（仅首次，表空时）
        if self.tasks_file and os.path.exists(self.tasks_file):
            try:
                if db.task_user_total(1) == 0:
                    with open(self.tasks_file, encoding="utf-8") as f:
                        records = json.load(f)
                    if records:
                        db.task_import(records, user_id=1)
            except Exception:
                pass

    def create(self, url, title=None, quality="compat", user_id=None):
        url = normalize_url(url)
        tid = uuid.uuid4().hex[:12]
        task = {
            "id": tid,
            "user_id": user_id or 1,
            "url": url,
            "platform": detect_platform(url),
            "quality": quality if quality in ("best", "compat", "audio") else "compat",
            "title": title or "解析中…",
            "status": "queued",
            "progress": 0.0,
            "message": "等待下载",
            "size": None,
            "file_path": None,
            "file_name": None,
            "error": None,
            "height": None,
            "created_at": int(time.time()),
            "finished_at": None,
        }
        db.task_create(task)
        self._executor.submit(self._run, tid)
        return tid

    def get(self, tid, user_id=None):
        return db.task_get(tid, user_id)

    def list(self, user_id=None, page=1, page_size=20):
        return db.task_list(user_id, page, page_size)

    def delete(self, tid, user_id=None):
        """物理删除：数据库记录 + 文件 + 任务目录（含残留 .part 等）。"""
        t = db.task_get(tid, user_id)
        removed = db.task_delete(tid, user_id)
        if t and t.get("file_path") and os.path.exists(t["file_path"]):
            try:
                os.remove(t["file_path"])
            except OSError:
                pass
        workdir = os.path.join(self.downloads_dir, tid)
        if os.path.isdir(workdir):
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)
        return removed

    def _update(self, tid, **kw):
        db.task_update(tid, **kw)

    def retry(self, tid, user_id=None):
        """重新提交失败的任务（原记录重试，不新增数据）"""
        t = self.get(tid, user_id)
        if not t:
            return None
        norm_url = normalize_url(t["url"])
        workdir = os.path.join(self.downloads_dir, tid)
        if os.path.isdir(workdir):
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)
        self._update(
            tid,
            url=norm_url,
            platform=detect_platform(norm_url),
            status="queued",
            progress=0.0,
            message="排队等待重试",
            error=None,
            finished_at=None,
            file_path=None,
            file_name=None,
            size=None,
            height=None,
        )
        self._executor.submit(self._run, tid)
        return self.get(tid, user_id)

    # ---------- 下载 ----------
    def _run(self, tid):
        t = self.get(tid)
        if not t:
            return
        if not self.sem.acquire(timeout=1):
            self._update(tid, status="queued", message="排队中，等待下载名额")
            self.sem.acquire()
        try:
            if str(t["url"]).strip().startswith("blob:"):
                raise RuntimeError("不支持 blob: 内存地址。请使用视频页面的实际网页链接进行下载。")
            if t["platform"] == "douyin":
                self._dl_douyin(tid)
            elif _is_m3u8_url(t["url"]):
                self._dl_m3u8(tid)
            else:
                self._dl_ytdlp(tid)
        except Exception as e:
            err_msg = str(e)
            if "Connection reset by peer" in err_msg:
                err_msg = "目标网站连接被重置，可能触发了反爬机制或需要代理"
            elif "Requested format is not available" in err_msg:
                err_msg = "未能匹配所选画质格式，请尝试切换画质重新下载"
            elif "Unsupported URL" in err_msg:
                u_str = str(t.get("url") or "")
                if "/tag/" in u_str or "/search/" in u_str or "/category/" in u_str or "/channel/" in u_str:
                    err_msg = "该链接为分类/标签列表页，非具体视频播放页。请在网页中点击进入具体视频后再下载。"
                else:
                    err_msg = "该链接暂不支持解析，请确认是否为单个视频播放页面"
            self._update(tid, status="failed", error=err_msg[:600], message="失败")
        finally:
            self.sem.release()

    def _dl_douyin(self, tid):
        t = self.get(tid)
        workdir = os.path.join(self.downloads_dir, tid)
        os.makedirs(workdir, exist_ok=True)
        self._update(tid, status="parsing", progress=0.02, message="解析抖音视频…")
        title, play_url = DouyinChannel().resolve(t["url"])
        if not title or title in ("解析中…", "HLS 视频", "未知"):
            title = f"抖音_视频_{tid[:8]}"
        self._update(tid, status="downloading", progress=0.05, message="下载中…")

        mp4 = os.path.join(workdir, "video.mp4")
        s = DouyinChannel().session()
        try:
            with s.get(play_url, stream=True, timeout=60) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                with open(mp4, "wb") as f:
                    for chunk in r.iter_content(1024 * 512):
                        if chunk:
                            f.write(chunk)
                            done += len(chunk)
                            if total:
                                self._update(tid, status="downloading",
                                             progress=0.05 + 0.9 * (done / total),
                                             message=f"下载中 {int(done / total * 100)}%")
        except requests.RequestException as e:
            raise RuntimeError(f"抖音视频下载失败：{e}") from e

        mp4 = self._ensure_h264(mp4, tid)
        self._update(tid, title=title, file_path=mp4, file_name=f"{self._safe_name(title)}.mp4",
                     size=round(os.path.getsize(mp4) / 1024 / 1024, 1),
                     height=self._probe_height(mp4),
                     status="done", progress=1.0, message="完成")

    def _dl_ytdlp(self, tid):
        t = self.get(tid)
        workdir = os.path.join(self.downloads_dir, tid)
        os.makedirs(workdir, exist_ok=True)
        outtmpl = os.path.join(workdir, "%(id)s.%(ext)s")

        def hook(d):
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes", 0)
                self._update(tid, status="downloading", progress=min(done / total, 1.0) if total else 0,
                             message=f"下载中 {d.get('_percent_str', '').strip()}")
            elif d["status"] == "finished":
                self._update(tid, status="merging", progress=1.0, message="合并音视频…")

        is_audio = t.get("quality") == "audio"
        if is_audio:
            format_opt = "ba/b"
            format_sort = ["acodec:m4a", "abr"]
        elif t.get("quality") == "best":
            format_opt = "bv*+ba/b"
            format_sort = ["res", "vcodec:avc", "acodec:m4a"]
        else:
            format_opt = "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4][vcodec^=avc1]/bv*[ext=mp4]+ba/b"
            format_sort = ["res", "vcodec:avc", "acodec:m4a"]

        opts = {
            "format": format_opt,
            "format_sort": format_sort,
            "merge_output_format": "mp4",
            "outtmpl": outtmpl,
            "noplaylist": True,
            "progress_hooks": [hook],
            "retries": 5,
            "fragment_retries": 5,
            "concurrent_fragment_downloads": 4,
            "max_filesize": MAX_FILE_SIZE,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": True,
            "ignoreerrors": False,
            "socket_timeout": 30,
        }
        node_bin = find_node_runtime()
        if node_bin:
            opts["js_runtimes"] = {"node": {"path": node_bin}}

        self._update(tid, status="parsing", progress=0.02, message="解析音视频信息…")
        target_url = normalize_url(t["url"])
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target_url, download=True)
            if info is None:
                raise RuntimeError("yt-dlp 未能解析该链接")
            title = info.get("title") or t["title"]
            if info.get("_type") == "playlist":
                raise RuntimeError("不支持下载播放列表，请粘贴单个视频链接")
        if not title or title in ("解析中…", "HLS 视频", "未知"):
            title = f"{t['platform']}_{'音频' if is_audio else '视频'}_{tid[:8]}"

        files = glob.glob(os.path.join(workdir, "*"))
        files = [f for f in files if os.path.isfile(f) and not f.endswith(".part")]
        if not files:
            raise RuntimeError("下载完成但未找到输出文件")
        final = max(files, key=os.path.getsize)

        if is_audio:
            self._update(tid, status="merging", progress=0.95, message="转码为 MP3 音频…")
            mp3 = os.path.join(workdir, "audio.mp3")
            subprocess.run(
                ["ffmpeg", "-y", "-i", final, "-vn", "-acodec", "libmp3lame", "-q:a", "2", mp3],
                capture_output=True, timeout=180
            )
            if not os.path.exists(mp3):
                raise RuntimeError("音频提取转码失败")
            self._update(
                tid,
                title=title,
                file_path=mp3,
                file_name=f"{self._safe_name(title)}.mp3",
                size=round(os.path.getsize(mp3) / 1024 / 1024, 1),
                height=None,
                status="done",
                progress=1.0,
                message="完成",
            )
            return

        mp4 = os.path.join(workdir, "video.mp4")
        os.replace(final, mp4)
        mp4 = self._ensure_h264(mp4, tid)
        self._update(tid, title=title, file_path=mp4, file_name=f"{self._safe_name(title)}.mp4",
                     size=round(os.path.getsize(mp4) / 1024 / 1024, 1),
                     height=self._probe_height(mp4),
                     status="done", progress=1.0, message="完成")

    def _dl_m3u8(self, tid):
        t = self.get(tid)
        workdir = os.path.join(self.downloads_dir, tid)
        os.makedirs(workdir, exist_ok=True)
        self._update(tid, status="parsing", progress=0.02, message="解析 HLS 流…")

        def cb(frac):
            self._update(tid, status="downloading", progress=0.03 + 0.92 * frac,
                         message=f"分片下载 {int(frac * 100)}%")

        mp4, title = download_m3u8(
            t["url"], workdir, progress_cb=cb,
            referer=urlparse(t["url"]).scheme + "://" + (urlparse(t["url"]).netloc or "localhost"),
            title_hint=t["title"],
        )
        if not title or title in ("解析中…", "HLS 视频", "未知"):
            title = f"HLS_视频_{tid[:8]}"
        final = os.path.join(workdir, "video.mp4")
        os.replace(mp4, final)
        final = self._ensure_h264(final, tid)
        self._update(tid, title=title, file_path=final, file_name=f"{self._safe_name(title)}.mp4",
                     size=round(os.path.getsize(final) / 1024 / 1024, 1),
                     height=self._probe_height(final),
                     status="done", progress=1.0, message="完成")

    @staticmethod
    def _probe_height(mp4):
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=height", "-of", "default=noprint_wrappers=1:nokey=1", mp4],
                capture_output=True, text=True, timeout=30)
            h = out.stdout.strip()
            return int(h) if h.isdigit() else None
        except Exception:
            return None

    def _ensure_h264(self, mp4, tid=None):
        """确保产物为 QuickTime / Apple 完美兼容格式（H.264/HEVC 视频 + AAC 音频）。"""
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1", mp4],
                capture_output=True, text=True, timeout=30,
            )
            vcodec = out.stdout.strip()
        except Exception:
            vcodec = ""

        try:
            out_a = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1", mp4],
                capture_output=True, text=True, timeout=30,
            )
            acodec = out_a.stdout.strip()
        except Exception:
            acodec = ""

        # 诊断：如果源文件没有视频流（例如视频流被大小限制中断），给出明确提示
        if not vcodec:
            raise RuntimeError("源文件无视频流（仅音频）：视频流可能被下载中断或源站仅提供音频，请重试；若为超大视频，现已支持 8GB 上限")

        is_v_compat = vcodec in ("h264", "hevc", "h265")
        is_a_compat = (not acodec) or (acodec in ("aac", "mp4a", "alac"))

        # 如果音视频均已原生兼容 QuickTime，直接返回
        if is_v_compat and is_a_compat:
            return mp4

        # 4K/8K 超高清或用户选用 best 画质时，保留原码（AV01/VP9等现代原生格式），避免数小时的 CPU 软转码卡顿
        h = self._probe_height(mp4)
        t = self.get(tid) if tid else None
        keep_video = ((h and h > 1080) or vcodec in ("av01", "vp9", "vp09")) and (not t or t.get("quality") != "compat")

        if keep_video or is_v_compat:
            # 仅音频需要转码为 aac（极速，通常 1-2 秒完成）
            if not is_a_compat:
                if tid:
                    self._update(tid, status="merging", progress=0.98, message="转码音频为 AAC 兼容格式…")
                tmp = mp4 + ".aactmp.mp4"
                res = subprocess.run(
                    ["ffmpeg", "-y", "-i", mp4, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", tmp],
                    capture_output=True, timeout=180
                )
                if res.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 1000:
                    os.replace(tmp, mp4)
            return mp4
        dur = 0.0
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", mp4],
                capture_output=True, text=True, timeout=30,
            )
            dur = float(out.stdout.strip() or 0)
        except Exception:
            pass
        tmp = mp4 + ".tmp.mp4"
        if tid:
            self._update(tid, status="merging", progress=0.93,
                         message="转码为 H.264 兼容格式…")
        # 用软件编码器（libx264）：可靠，产出可验证；本机 videotoolbox 曾产出损坏文件
        enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
        proc = subprocess.Popen(
            ["ffmpeg", "-y", "-i", mp4, *enc, "-c:a", "aac",
             "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", tmp],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        last = time.time()
        for line in proc.stdout or []:
            if line.startswith("out_time_ms="):
                try:
                    ms = int(line.split("=", 1)[1].strip())
                except ValueError:
                    continue
                if dur > 0 and tid and time.time() - last > 0.8:
                    frac = min(0.93 + 0.06 * (ms / 1_000_000 / dur), 0.99)
                    self._update(tid, progress=frac,
                                 message=f"转码 H.264 {int(min(ms / 1_000_000 / dur * 100, 100))}%")
                    last = time.time()
        proc.wait(timeout=3600)
        if proc.returncode != 0:
            raise RuntimeError("视频编码不兼容，转码 H.264 失败")
        os.replace(tmp, mp4)
        # 校验产物有效（防止坏编码器产出无流文件）
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1", mp4],
                capture_output=True, text=True, timeout=30,
            )
            if not out.stdout.strip():
                raise RuntimeError("转码产物无效（缺少视频流）")
        except RuntimeError:
            raise
        except Exception:
            pass
        return mp4

    @staticmethod
    def _safe_name(title):
        s = re.sub(r"[\\/:*?\"<>|\s]+", "_", title or "video").strip("_")
        return (s[:80] or "video")

    def resolve(self, url: str, quality: str = "compat") -> dict:
        """纯元数据与流地址解析：0 磁盘写入，秒级返回媒体信息及可直链/混流地址。"""
        url = normalize_url(url)
        plat = detect_platform(url)
        is_audio = quality == "audio"
        ext = "mp3" if is_audio else "mp4"
        mime = "audio/mpeg" if is_audio else "video/mp4"

        if plat == "douyin":
            title, play_url = DouyinChannel().resolve(url)
            if not title or title in ("解析中…", "HLS 视频", "未知"):
                title = f"douyin_{int(time.time())}"
            return {
                "platform": "douyin",
                "title": title,
                "ext": ext,
                "mime": mime,
                "type": "single",
                "video_url": play_url,
                "audio_url": None,
                "video_headers": {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
                "audio_headers": {},
                "filename": f"{self._safe_name(title)}.{ext}",
                "filesize_approx": None,
            }

        if _is_m3u8_url(url):
            title = f"hls_{int(time.time())}"
            return {
                "platform": "hls",
                "title": title,
                "ext": ext,
                "mime": mime,
                "type": "hls",
                "video_url": url,
                "audio_url": None,
                "video_headers": {},
                "audio_headers": {},
                "filename": f"{self._safe_name(title)}.{ext}",
                "filesize_approx": None,
            }

        # 通用 yt-dlp 解析
        if is_audio:
            format_opt = "ba/b"
            format_sort = ["acodec:m4a", "abr"]
        elif quality == "best":
            format_opt = "bv*+ba/b"
            format_sort = ["res", "vcodec:avc", "acodec:m4a"]
        else:
            format_opt = "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4][vcodec^=avc1]/bv*[ext=mp4]+ba/b"
            format_sort = ["res", "vcodec:avc", "acodec:m4a"]

        opts = {
            "format": format_opt,
            "format_sort": format_sort,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 3,
            "socket_timeout": 20,
        }
        node_bin = find_node_runtime()
        if node_bin:
            opts["js_runtimes"] = {"node": {"path": node_bin}}

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise RuntimeError("未能解析该视频地址")
            if info.get("_type") == "playlist":
                raise RuntimeError("暂不支持播放列表，请粘贴单视频链接")
            title = info.get("title") or f"{plat}_{int(time.time())}"
            formats = info.get("requested_formats")
            if formats and len(formats) >= 2:
                v_item = formats[0]
                a_item = formats[1]
                return {
                    "platform": plat,
                    "title": title,
                    "ext": ext,
                    "mime": mime,
                    "type": "dash",
                    "video_url": v_item.get("url"),
                    "audio_url": a_item.get("url"),
                    "video_headers": v_item.get("http_headers") or {},
                    "audio_headers": a_item.get("http_headers") or {},
                    "vcodec": v_item.get("vcodec"),
                    "acodec": a_item.get("acodec"),
                    "filename": f"{self._safe_name(title)}.{ext}",
                    "filesize_approx": (v_item.get("filesize_approx") or v_item.get("filesize") or 0) +
                                       (a_item.get("filesize_approx") or a_item.get("filesize") or 0) or None,
                }
            else:
                single_url = info.get("url") or (formats[0].get("url") if formats else None)
                single_headers = info.get("http_headers") or (formats[0].get("http_headers") if formats else {})
                single_vcodec = info.get("vcodec") or (formats[0].get("vcodec") if formats else None)
                single_acodec = info.get("acodec") or (formats[0].get("acodec") if formats else None)
                return {
                    "platform": plat,
                    "title": title,
                    "ext": ext,
                    "mime": mime,
                    "type": "single",
                    "video_url": single_url,
                    "audio_url": None,
                    "video_headers": single_headers or {},
                    "audio_headers": {},
                    "vcodec": single_vcodec,
                    "acodec": single_acodec,
                    "filename": f"{self._safe_name(title)}.{ext}",
                    "filesize_approx": info.get("filesize_approx") or info.get("filesize"),
                }

    def stream_pipe(self, url: str, quality: str = "compat"):
        """0 磁盘实时管道流：通过 ffmpeg pipe:1 边拉取边封装边向客户端推流。
        返回 (generator, filename, mime, filesize)
        """
        meta = self.resolve(url, quality=quality)
        is_audio = quality == "audio"
        filename = meta["filename"]
        mime = meta["mime"]
        filesize = meta.get("filesize_approx")

        if meta["platform"] == "douyin" and not is_audio and meta["video_url"]:
            def direct_gen():
                s = DouyinChannel().session()
                headers = meta.get("video_headers") or {}
                with s.get(meta["video_url"], headers=headers, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            yield chunk
            return direct_gen(), filename, mime, filesize

        cmd = ["ffmpeg", "-y", "-nostats", "-loglevel", "error"]
        # 视频编码兼容性：源为 VP9/AV1 等非 QuickTime 原生编码时，转码为 H.264，保证 macOS/手机/浏览器都能播
        _vcodec = (meta.get("vcodec") or "").lower()
        _v_compat = (not _vcodec) or _vcodec.startswith("avc1") or _vcodec.startswith("h264") or _vcodec.startswith("hevc") or _vcodec.startswith("h265")
        video_codec_flags = ["-c:v", "copy"] if _v_compat else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
        if is_audio:
            a_url = meta.get("audio_url") or meta.get("video_url")
            a_hdr = meta.get("audio_headers") or meta.get("video_headers") or {}
            if a_hdr:
                cmd.extend(["-headers", "".join(f"{k}: {v}\r\n" for k, v in a_hdr.items())])
            cmd.extend(["-i", a_url, "-vn", "-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3", "pipe:1"])
        elif meta["type"] == "dash":
            v_hdr = meta.get("video_headers") or {}
            a_hdr = meta.get("audio_headers") or {}
            if v_hdr:
                cmd.extend(["-headers", "".join(f"{k}: {v}\r\n" for k, v in v_hdr.items())])
            cmd.extend(["-i", meta["video_url"]])
            if a_hdr:
                cmd.extend(["-headers", "".join(f"{k}: {v}\r\n" for k, v in a_hdr.items())])
            cmd.extend(["-i", meta["audio_url"]])

            acodec = (meta.get("acodec") or "").lower()
            if not acodec or "mp4a" in acodec or "aac" in acodec:
                audio_codec_flags = ["-c:a", "copy"]
            else:
                audio_codec_flags = ["-c:a", "aac", "-b:a", "192k"]

            cmd.extend([
                *video_codec_flags,
                *audio_codec_flags,
                "-f", "mp4",
                "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                "pipe:1"
            ])
        else:
            v_hdr = meta.get("video_headers") or {}
            if v_hdr:
                cmd.extend(["-headers", "".join(f"{k}: {v}\r\n" for k, v in v_hdr.items())])
            cmd.extend(["-i", meta["video_url"]])

            acodec = (meta.get("acodec") or "").lower()
            if not acodec or "mp4a" in acodec or "aac" in acodec:
                audio_codec_flags = ["-c:a", "copy"]
            else:
                audio_codec_flags = ["-c:a", "aac", "-b:a", "192k"]

            cmd.extend([
                *video_codec_flags,
                *audio_codec_flags,
                "-f", "mp4",
                "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                "pipe:1"
            ])

        def ffmpeg_gen():
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=1024 * 1024
            )
            try:
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait()
                    except Exception:
                        pass

        return ffmpeg_gen(), filename, mime, filesize
