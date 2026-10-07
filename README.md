# OmniGrabber · 轻载视频下载服务

输入链接即可把 B站 / 抖音 / YouTube / 任意 m3u8 视频下载到本地并合并为 MP4 的网页服务。

## 项目结构

```
OmniGrabber/
├── backend/
│   ├── main.py      # FastAPI 服务：API 路由、鉴权、SSRF 防护、自动清理
│   ├── engine.py    # 下载引擎：yt-dlp 优先，m3u8 兜底；并发控制、任务管理
│   └── m3u8dl.py    # m3u8 下载器：playlist 解析、AES-128/256 解密、并发分片、ffmpeg 合并
├── frontend/
│   └── index.html   # 前端页面（轨道动画 + 真实接口）
├── downloads/       # 下载产物目录（>24h 自动清理）
└── .venv/           # Python 虚拟环境
```

## 启动

```bash
cd /Users/eden/workspace/OmniGrabber
./run.sh start    # 后台启动
./run.sh stop     # 停止服务
./run.sh restart  # 重启服务
./run.sh status   # 查看服务状态
./run.sh logs     # 实时日志
```

- `OMNI_TOKEN`：访问令牌（API 直调用；页面登录后无需）。`OMNI_USER` / `OMNI_PASS`：页面登录账号（默认 `admin` / `omnivideo2026`，**务必修改**）。**登录机制**：首次打开页面显示登录门，输入账号密码（或带 `?token=` 访问自动登录），成功后写入 30 天 Cookie，之后直接访问 `http://localhost:8090/` 免登录；新浏览器/手机首次登录一次即可。API 鉴权支持 Cookie / `?token=` / `X-Omni-Token` / `X-Omni-Session`。
- `PORT`：监听端口，默认 8090，绑定 `0.0.0.0`（局域网可访问）。

## 访问地址

- 本机：`http://localhost:8090/?token=<TOKEN>`
- 局域网（同一 WiFi 下手机/平板）：`http://<本机局域网IP>:8090/?token=<TOKEN>`

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| POST | `/api/plugin/login` | 插件/API 登录获取 Session Token |
| POST | `/api/download` | 创建下载任务 `{"url": "...", "quality": "best"}` |
| GET | `/api/tasks` | 任务列表（支持分页） |
| GET | `/api/tasks/{id}` | 任务详情（轮询进度用） |
| POST | `/api/tasks/{id}/retry` | 重新下载失败的任务 |
| DELETE | `/api/tasks/{id}` | 取消/物理删除任务及文件 |
| POST | `/api/tasks/clear-done` | 清理已完成或失败的历史任务 |
| GET | `/api/files/{id}` | 下载或播放完成的 MP4 文件 |

## 安全特性

- **鉴权**：设置 `OMNI_TOKEN` 后所有写接口与文件接口均需令牌。
- **SSRF 防护**：拒绝内网 / 保留地址 / 非 http(s) 链接。
- **资源上限**：单文件 ≤ 2GB；m3u8 分片数量 ≤ 5000；同时最多 2 个下载任务；产物 24h 自动清理。

## 发布到公网

本机网络环境的出网受限，cloudflared 快速隧道无法回连。发布公网有两种方式：

1. **网络允许时**（办公室/家宽直连）：`cloudflared tunnel --url http://localhost:8090`，会输出一个 `https://xxx.trycloudflare.com` 地址。
2. **云服务器部署**：把整个目录拷到一台有公网 IP 的服务器，`PORT=8090 OMNI_TOKEN=xxx nohup ./.venv/bin/python backend/main.py &`，再用 Nginx 反代或 cloudflared 域名隧道即可。

## 自测记录（2026-09-18）

- ✅ 公开 MP4 直链（yt-dlp）：下载成功，ffprobe 校验有效
- ✅ B站视频（yt-dlp bilibili）：「Never Gonna Give You Up」MV 下载成功，212s / 20.5MB
- ✅ 公开 HLS m3u8（m3u8dl）：635s / 465MB 分片下载 + ffmpeg 合并成功
- ✅ 鉴权：无 token 返回 401；带 token 正常
- ✅ SSRF：内网地址被拒
- ✅ 文件下载接口、删除清理、局域网端到端均通过


## 自测记录（2026-09-18 全平台回归）

自动化回归：逐平台真实下载 → ffprobe 验证 H.264+AAC 可播 → 清理。

| 场景 | 结果 | 产物 |
|---|---|---|
| YouTube 普通视频 | PASS | 1920x1080 H.264+AAC 161.4MB |
| YouTube Shorts | PASS | 1080x1920 H.264+AAC 26.9MB |
| 哔哩哔哩（最高清模式） | PASS | 1920x1080 H.264+AAC 77.7MB |
| 抖音 | PASS | 1024x576 H.264+AAC 1.7MB |
| HLS m3u8 | PASS | 1920x1080 H.264+AAC 465.6MB |
| MP4 直链（带音轨） | PASS | H.264+AAC 44.9MB |

### 抖音说明（重要）
yt-dlp 对抖音的支持已被平台反爬破坏（detail API 需 a_bogus 签名 + 浏览器 cookies）。
本项目内置自研抖音通道 `backend/douyin.py`：
1. 调用字节公开接口注册匿名 `ttwid`（无需登录），并生成 `s_v_web_id / verifyFp / msToken`
2. 用匿名 cookies 调 `aweme/v1/web/aweme/detail` 获取无水印直链（CDN 为 365yg.com）
3. 直接流式下载，H.264+AAC 原样保留
cookies 缓存于 `downloads/.douyin_session.json`，6 小时自动续期。
支持链接格式：`douyin.com/video/xxx`、`iesdouyin.com/share/video/xxx`、`v.douyin.com/xxx/` 短链。
若平台风控升级导致失败，任务会显示明确错误。
