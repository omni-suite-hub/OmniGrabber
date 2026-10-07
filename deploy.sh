#!/usr/bin/env bash
# ============================================================
# OmniGrabber 服务器部署脚本
# 目标：video.omnipic.cc  ->  HTTPS
# 用法（服务器上以 root 执行）：
#   bash deploy.sh /tmp/omnigrabber-deploy.tar.gz
# ============================================================
set -euo pipefail

APP_DIR=/var/www/omnigrabber
DOMAIN=video.omnipic.cc
PORT=8090
PKG="${1:-/tmp/omnigrabber-deploy.tar.gz}"

log() { echo -e "\n==> $*"; }

# ---------- 1. 解包 ----------
log "[1/8] 解包项目到 $APP_DIR"
if [ ! -f "$PKG" ]; then echo "找不到部署包 $PKG"; exit 1; fi
mkdir -p "$APP_DIR"
TMP=$(mktemp -d)
tar xzf "$PKG" -C "$TMP"
cp -a "$TMP"/OmniGrabber/. "$APP_DIR"/
rm -rf "$TMP"
echo "  文件数: $(find "$APP_DIR" -type f -not -path '*/.venv/*' | wc -l)"
# 数据核验：账号库与抖音会话必须在
test -f "$APP_DIR/omnivideo.db" && echo "  ✓ omnivideo.db 已就位"
test -f "$APP_DIR/downloads/.douyin_session.json" && echo "  ✓ .douyin_session.json 已就位"

# ---------- 2. 系统依赖 ----------
log "[2/8] 安装 python3 / venv / ffmpeg"
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip ffmpeg
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 python3-pip ffmpeg
else
  echo "  未识别的包管理器，请手动安装 python3 / python3-venv / ffmpeg"
fi
ffmpeg -version 2>&1 | head -1
python3 --version

# ---------- 3. Python 环境 ----------
log "[3/8] 建立 venv 并安装依赖"
cd "$APP_DIR"
python3 -m venv .venv
./.venv/bin/pip install -q -U pip
./.venv/bin/pip install -q fastapi uvicorn yt-dlp requests cryptography
./.venv/bin/python -c "import fastapi,uvicorn,yt_dlp,requests,cryptography;print('  ✓ 依赖导入正常')"

# ---------- 4. 环境变量（强随机，替换默认密码）----------
log "[4/8] 生成 .env（强随机 token 与管理员密码）"
ENVF="$APP_DIR/.env"
if [ ! -f "$ENVF" ]; then
  T=$(./.venv/bin/python -c "import secrets;print(secrets.token_urlsafe(32))")
  P=$(./.venv/bin/python -c "import secrets;print(secrets.token_urlsafe(16))")
  printf 'OMNI_TOKEN=%s\nOMNI_USER=admin\nOMNI_PASS=%s\nPORT=%s\n' "$T" "$P" "$PORT" > "$ENVF"
  chmod 600 "$ENVF"
  echo "  已生成 $ENVF"
else
  echo "  已存在，沿用（如需重置请删除该文件后重跑）"
fi
set -a; . "$ENVF"; set +a
echo "  管理员账号: $OMNI_USER"
echo "  管理员密码: $OMNI_PASS"
echo "  访问令牌  : $OMNI_TOKEN"

# ---------- 5. PM2 常驻 ----------
log "[5/8] PM2 常驻进程"
command -v pm2 >/dev/null 2>&1 || npm i -g pm2 >/dev/null 2>&1
pm2 delete omnigrabber >/dev/null 2>&1 || true
cd "$APP_DIR"
OMNI_TOKEN="$OMNI_TOKEN" OMNI_USER="$OMNI_USER" OMNI_PASS="$OMNI_PASS" PORT="$PORT" \
  pm2 start ./.venv/bin/python --name omnigrabber -- backend/main.py
pm2 save >/dev/null 2>&1 || true
pm2 list | grep -E "omnigrabber|name" || true

# ---------- 6. 本地健康检查 ----------
log "[6/8] 本地健康检查"
sleep 4
if curl -fsS "http://127.0.0.1:$PORT/api/health"; then
  echo -e "\n  ✓ 后端已就绪"
else
  echo -e "\n  ✗ 健康检查失败，请查看: pm2 logs omnigrabber"
  exit 1
fi

# ---------- 7. Nginx 反向代理 ----------
log "[7/8] 配置 Nginx  ($DOMAIN -> 127.0.0.1:$PORT)"
NGX=/etc/nginx/conf.d/omnigrabber.conf
cat > "$NGX" <<NGINX
server {
    listen 80;
    server_name $DOMAIN;

    client_max_body_size 20m;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_buffering off;
    }
}
NGINX
nginx -t
systemctl reload nginx
echo "  ✓ Nginx 已加载"

# ---------- 8. HTTPS ----------
log "[8/8] HTTPS 配置"
if [ -d /etc/letsencrypt/live/www.omnipic.cc ]; then
  echo "  检测到已有 www.omnipic.cc 的 Let's Encrypt 证书，为 $DOMAIN 申请同名证书"
  if command -v certbot >/dev/null 2>&1; then
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
      --register-unsafely-without-email --redirect || \
      echo "  ! 证书申请失败（域名可能尚未解析），请先在 Cloudflare 添加 video 记录后重跑本步"
  else
    echo "  ! 未安装 certbot，跳过"
  fi
else
  echo "  未检测到本机 Let's Encrypt 证书，判定为 Cloudflare 代理模式："
  echo "  → 在 Cloudflare 为 $DOMAIN 添加 A 记录指向本机公网 IP，并开启小黄云(Proxied)"
  echo "  → SSL/TLS 模式设为 Full 或 Full(strict)，Cloudflare 会自动签发边缘证书提供 HTTPS"
  echo "  → 若需源站证书，可在 Cloudflare 生成 Origin Certificate 后配置到 Nginx"
fi

echo -e "\n=========== 部署完成 ==========="
echo "  应用目录 : $APP_DIR"
echo "  进程管理 : pm2 restart omnigrabber"
echo "  日志     : pm2 logs omnigrabber"
echo "  本地验证 : curl http://127.0.0.1:$PORT/api/health"
echo "  公网地址 : https://$DOMAIN/"
echo "  登录账号 : $OMNI_USER / $OMNI_PASS"
echo "================================="
