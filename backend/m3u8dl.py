"""m3u8 / HLS 流下载器：解析 playlist、AES-128/256 解密、并发分片下载、ffmpeg 合并为 MP4。

独立模块，不依赖 yt-dlp，作为通用 HLS 站点的兜底下载通道。
"""

from __future__ import annotations
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
MAX_SEGMENTS = 5000          # 分片数量上限，防止恶意/异常流
MAX_SEG_SIZE = 300 * 1024 * 1024  # 单分片大小上限
FETCH_TIMEOUT = 30


class M3U8Error(Exception):
    pass


class _Seg:
    __slots__ = ("uri", "duration", "key_uri", "iv", "seq")


def _headers(url, referer=None):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        h["Referer"] = referer
    return h


def _fetch(session, url, referer=None, timeout=FETCH_TIMEOUT):
    r = session.get(url, headers=_headers(url, referer), timeout=timeout)
    r.raise_for_status()
    return r.text


def parse_playlist(text, base_url):
    """解析 m3u8 文本。

    返回:
      ("master", [(bandwidth, media_playlist_url), ...])  按带宽降序
      ("media",  [segment, ...])
    分片列表元素为 _Seg。
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not any(ln.startswith("#EXT-X-STREAM-INF") for ln in lines):
        return _parse_media(lines, base_url)
    return _parse_master(lines, base_url)


def _parse_master(lines, base_url):
    variants, pending = [], None
    for ln in lines:
        if ln.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", ln)
            pending = int(m.group(1)) if m else 0
        elif ln.startswith("#"):
            continue
        else:
            if pending is not None:
                variants.append((pending, urljoin(base_url, ln)))
            pending = None
    variants = [v for v in variants if v[1]]
    if not variants:
        raise M3U8Error("master playlist 中没有可用的视频流")
    variants.sort(key=lambda x: -x[0])
    return ("master", variants)


def _parse_media(lines, base_url):
    segs, key_uri, iv, seq = [], None, None, 0
    duration = 0.0
    for ln in lines:
        if ln.startswith("#EXT-X-KEY"):
            m = re.search(r'METHOD="?([A-Z0-9\-]+)"?', ln)
            method = m.group(1).upper() if m else "NONE"
            if method in ("AES-128", "AES-256"):
                um = re.search(r'URI="([^"]+)"', ln)
                key_uri = urljoin(base_url, um.group(1)) if um else None
                vm = re.search(r"IV=0x([0-9a-fA-F]{32})", ln)
                iv = bytes.fromhex(vm.group(1)) if vm else None
            else:
                key_uri = None
        elif ln.startswith("#EXTINF"):
            m = re.search(r"#EXTINF:([\d.]+)", ln)
            duration = float(m.group(1)) if m else 0.0
        elif ln.startswith("#EXT-X-MEDIA-SEQUENCE"):
            m = re.search(r":(\d+)", ln)
            if m:
                seq = int(m.group(1))
        elif ln.startswith("#"):
            continue
        else:
            seg = _Seg()
            seg.uri = urljoin(base_url, ln)
            seg.duration = duration
            seg.key_uri = key_uri
            seg.iv = iv
            seg.seq = seq
            seq += 1
            segs.append(seg)
    if not segs:
        raise M3U8Error("playlist 中没有可下载的分片")
    return ("media", segs)


def _get_key(session, key_uri, referer):
    r = session.get(key_uri, headers=_headers(key_uri, referer), timeout=FETCH_TIMEOUT)
    r.raise_for_status()
    data = r.content
    if len(data) not in (16, 32):
        raise M3U8Error(f"AES 密钥长度异常: {len(data)} 字节")
    return data


def _decrypt(data, key, iv):
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    out = dec.update(data) + dec.finalize()
    pad = out[-1]
    if 0 < pad <= 16 and out[-pad:] == bytes([pad]) * pad:
        out = out[:-pad]
    return out


def download_m3u8(url, out_dir, progress_cb=None, cancel_event=None, referer=None, title_hint=None):
    """下载 HLS 流并合并为 mp4。

    progress_cb(fraction: float) 每完成一个分片调用一次。
    返回 (mp4 绝对路径, 标题)。
    """
    session = requests.Session()
    key_cache = {}
    base = url

    try:
        text = _fetch(session, url, referer)
        kind, payload = parse_playlist(text, url)
        if kind == "master":
            best = payload[0]
            base = best[1]
            text = _fetch(session, base, referer)
            _, segs = parse_playlist(text, base)
        else:
            segs = payload
    except requests.RequestException as e:
        raise M3U8Error(f"拉取 m3u8 失败: {e}") from e

    if len(segs) > MAX_SEGMENTS:
        raise M3U8Error(f"分片数量 {len(segs)} 超过上限 {MAX_SEGMENTS}，已中止")

    seg_dir = os.path.join(out_dir, "segs")
    os.makedirs(seg_dir, exist_ok=True)
    mp4 = os.path.join(out_dir, "output.mp4")
    seg_paths = [os.path.join(seg_dir, f"seg_{i:05d}.ts") for i in range(len(segs))]

    done = [0]
    lock = threading.Lock()

    def _one(i):
        seg = segs[i]
        path = seg_paths[i]
        if cancel_event and cancel_event.is_set():
            return
        r = session.get(seg.uri, headers=_headers(seg.uri, referer), timeout=FETCH_TIMEOUT)
        r.raise_for_status()
        data = r.content
        if len(data) > MAX_SEG_SIZE:
            raise M3U8Error(f"分片 {i} 超过大小上限")
        if seg.key_uri:
            key = key_cache.get(seg.key_uri)
            if key is None:
                key = _get_key(session, seg.key_uri, referer)
                key_cache[seg.key_uri] = key
            iv = seg.iv if seg.iv is not None else seg.seq.to_bytes(16, "big")
            data = _decrypt(data, key, iv)
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        with lock:
            done[0] += 1
            if progress_cb:
                progress_cb(done[0] / len(segs))

    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_one, i): i for i in range(len(segs))}
            for fut in as_completed(futs):
                fut.result()  # 任一异常都会向上抛
    except Exception as e:
        raise M3U8Error(f"分片下载失败: {e}") from e

    if cancel_event and cancel_event.is_set():
        raise M3U8Error("任务已取消")

    # ffmpeg 合并
    list_file = os.path.join(seg_dir, "list.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for p in seg_paths:
            f.write(f"file '{p}'\n")

    def _run_ffmpeg(args):
        p = subprocess.run(args, capture_output=True, text=True, timeout=1800)
        return p

    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", mp4]
    p = _run_ffmpeg(cmd)
    if p.returncode != 0:
        cmd2 = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
                "-c:v", "libx264", "-preset", "fast", "-c:a", "aac",
                "-movflags", "+faststart", mp4]
        p2 = _run_ffmpeg(cmd2)
        if p2.returncode != 0:
            raise M3U8Error("ffmpeg 合并失败: " + (p2.stderr or p.stderr)[-400:])

    if not os.path.exists(mp4) or os.path.getsize(mp4) == 0:
        raise M3U8Error("合并产物为空，下载失败")

    title = title_hint or "HLS 视频"
    return mp4, title
