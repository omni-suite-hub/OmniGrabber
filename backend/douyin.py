"""抖音专用下载通道。

yt-dlp 的抖音支持已被平台反爬破坏（detail API 需要 a_bogus 签名，且要求浏览器 cookies）。
本模块走社区验证过的匿名通道：
  1. 注册 ttwid（无登录，字节公开注册接口）+ 生成 s_v_web_id / verifyFp / msToken
  2. 用这些匿名 cookies 直接调 aweme/v1/web/aweme/detail 拿无水印直链
  3. 由 engine 负责下载与 H.264 兼容
cookies 缓存在磁盘，TTL 6 小时后自动重新注册。
"""

from __future__ import annotations
import json
import os
import random
import re
import string
import time

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36")
BASE_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.douyin.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
TTWID_URL = "https://ttwid.bytedance.com/ttwid/union/register/"
DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
TTWID_PAYLOAD = (
    '{"region":"cn","aid":1128,"needFid":false,"service":"www.douyin.com",'
    '"migrate_info":{"ticket":"","source":"node"},"cbUrlProtocol":"https","union":true}'
)
CACHE_TTL = 6 * 3600
_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _gen_verify_fp():
    """verifyFp / s_v_web_id 生成（移植自 Evil0ctal VerifyFpManager）"""
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    n = int(round(time.time() * 1000))
    base36 = ""
    while n > 0:
        base36 = _BASE36[n % 36] + base36
        n //= 36
    o = [""] * 36
    o[8] = o[13] = o[18] = o[23] = "_"
    o[14] = "4"
    for i in range(36):
        if not o[i]:
            x = int(random.random() * len(chars))
            if i == 19:
                x = (3 & x) | 8
            o[i] = chars[x]
    return "verify_" + base36 + "_" + "".join(o)


def _gen_ms_token():
    return "".join(random.choices(string.ascii_letters + string.digits, k=126)) + "=="


class DouyinError(RuntimeError):
    pass


class DouyinChannel:
    def __init__(self, cache_path=None):
        self.cache_path = cache_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "downloads", ".douyin_session.json")
        self._session = None

    # ---------- cookies ----------
    def _load_cache(self):
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                d = json.load(f)
            if time.time() - d.get("ts", 0) < CACHE_TTL and d.get("cookies"):
                return d["cookies"]
        except Exception:
            pass
        return None

    def _save_cache(self, cookies):
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "cookies": cookies}, f)
        except Exception:
            pass

    def _register_cookies(self):
        s = requests.Session()
        s.headers.update(BASE_HEADERS)
        try:
            r = s.post(TTWID_URL, data=TTWID_PAYLOAD,
                       headers={**BASE_HEADERS, "Content-Type": "application/json"}, timeout=15)
            ttwid = r.cookies.get("ttwid", "")
            if not ttwid:
                raise DouyinError("抖音 ttwid 注册失败")
        except requests.RequestException as e:
            raise DouyinError(f"抖音 cookies 获取失败：{e}") from e
        cookies = {
            "ttwid": ttwid,
            "s_v_web_id": _gen_verify_fp(),
            "verifyFp": _gen_verify_fp(),
            "msToken": _gen_ms_token(),
        }
        self._save_cache(cookies)
        return cookies

    def session(self):
        if self._session is not None:
            return self._session
        cookies = self._load_cache() or self._register_cookies()
        s = requests.Session()
        s.headers.update(BASE_HEADERS)
        for k, v in cookies.items():
            s.cookies.set(k, v, domain=".douyin.com")
        self._session = s
        return s

    # ---------- 解析 ----------
    def extract_aweme_id(self, url):
        m = re.search(r"douyin\.com/(?:video|note)/(\d+)", url)
        if m:
            return m.group(1)
        m = re.search(r"modal_id=(\d+)", url)
        if m:
            return m.group(1)
        m = re.search(r"iesdouyin\.com/share/video/(\d+)", url)
        if m:
            return m.group(1)
        if "v.douyin.com" in url:
            try:
                r = requests.get(url, headers={"User-Agent": UA}, allow_redirects=True, timeout=20)
                m = re.search(r"douyin\.com/(?:video|note)/(\d+)", r.url)
                if m:
                    return m.group(1)
            except requests.RequestException:
                pass
        raise DouyinError("无法从链接中解析抖音视频 ID，请粘贴 www.douyin.com/video/xxx 或分享短链")

    def resolve(self, url):
        """返回 (title, play_url)；play_url 为无水印直链。"""
        vid = self.extract_aweme_id(url)
        s = self.session()
        try:
            r = s.get(DETAIL_URL, params={"aweme_id": vid}, timeout=20)
            r.raise_for_status()
            d = r.json()
        except (requests.RequestException, ValueError) as e:
            raise DouyinError(f"抖音详情请求失败：{e}") from e
        ad = d.get("aweme_detail")
        if not ad:
            reason = (d.get("filter_detail") or {}).get("filter_reason") or "视频不存在或不可见"
            raise DouyinError(f"抖音未返回视频数据（{reason}）")
        title = ad.get("desc") or ""
        video = ad.get("video") or {}
        pa = video.get("play_addr") or {}
        urls = pa.get("url_list") or []
        if not urls:
            raise DouyinError("抖音未返回播放地址")
        play_url = urls[0].replace("playwm", "play")
        return title, play_url


channel = DouyinChannel()
