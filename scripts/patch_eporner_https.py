"""幂等 patch：yt-dlp Eporner 提取器 http -> https（本网络 http→https 跳转会被重置）。
yt-dlp 升级后需重新执行：./.venv/bin/python scripts/patch_eporner_https.py
"""
import pathlib
ep = pathlib.Path(".venv/lib/python3.14/site-packages/yt_dlp/extractor/eporner.py")
s = ep.read_text(encoding="utf-8")
old = "f'http://www.eporner.com/xhr/video/{video_id}'"
new = "f'https://www.eporner.com/xhr/video/{video_id}'"
if old in s:
    ep.write_text(s.replace(old, new, 1), encoding="utf-8")
    print("patched: http -> https")
elif new in s:
    print("already patched")
else:
    raise SystemExit("pattern not found")
