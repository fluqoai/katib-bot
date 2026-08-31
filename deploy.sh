#!/usr/bin/env bash
# =============================================================================
# Kateb — one-shot VPS deploy script
#
# Usage (as root on a fresh Ubuntu 24.04 VPS):
#   git clone https://github.com/fluqoai/katib-bot.git /opt/kateb
#   cd /opt/kateb
#   cp .env.example .env  # then edit
#   bash deploy.sh
#
# This script:
#   1. Installs system deps (Python 3.11, Node, nginx, certbot, LibreOffice)
#   2. Builds the Docker images
#   3. Starts web + worker via docker compose
#   4. Configures nginx as a reverse proxy
#   5. (Optionally) requests a Let's Encrypt cert
#
# Idempotent: re-running is safe.
# =============================================================================
set -euo pipefail

# ---------- Args / config ----------------------------------------------------
DOMAIN="${DOMAIN:-katibai.xyz}"
API_SUBDOMAIN="${API_SUBDOMAIN:-api}"
EMAIL="${EMAIL:-}"  # for Let's Encrypt registration
APP_DIR="${APP_DIR:-/opt/kateb}"
APP_USER="${APP_USER:-kateb}"
HTTP_PORT="${HTTP_PORT:-8000}"
USE_HTTPS="${USE_HTTPS:-true}"

# ---------- Sanity checks ----------------------------------------------------
if [[ $EUID -ne 0 ]]; then
  echo "ERROR: must run as root (sudo bash deploy.sh)" >&2
  exit 1
fi
if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "ERROR: $APP_DIR/.env not found." >&2
  echo "       Run: cp .env.example .env  then edit it." >&2
  exit 1
fi

echo ">>> Kateb deploy starting"
echo "    domain       : $DOMAIN"
echo "    api subdomain: $API_SUBDOMAIN.$DOMAIN"
echo "    app dir      : $APP_DIR"
echo "    app user     : $APP_USER"
echo "    http port    : $HTTP_PORT"
echo "    use https    : $USE_HTTPS"

# ---------- 1. System deps ---------------------------------------------------
echo ">>> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    python3.11 python3.11-venv python3-pip \
    nginx certbot python3-certbot-nginx \
    curl git ca-certificates ufw \
    libreoffice-core libreoffice-writer \
    fonts-noto fonts-noto-extra \
    poppler-utils
apt-get -y autoremove
apt-get -y clean

# ---------- 2. App user (idempotent) -----------------------------------------
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  echo ">>> Creating user $APP_USER"
  adduser --disabled-password --gecos "" "$APP_USER"
  usermod -aG sudo,docker "$APP_USER"
fi

# ---------- 3. Docker -------------------------------------------------------
if ! command -v docker >/dev/null; then
  echo ">>> Installing Docker"
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sh /tmp/get-docker.sh
  usermod -aG docker "$APP_USER"
  systemctl enable --now docker
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: 'docker compose' not available. Install Docker Compose v2." >&2
  exit 1
fi

# ---------- 4. Permissions ---------------------------------------------------
echo ">>> Setting ownership of $APP_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ---------- 5. Build + start --------------------------------------------------
echo ">>> Building Docker images (this takes a few minutes)"
cd "$APP_DIR"
sudo -u "$APP_USER" docker compose build

echo ">>> Starting services"
sudo -u "$APP_USER" docker compose up -d

# ---------- 6. Wait for healthcheck -------------------------------------------
echo ">>> Waiting for /api/health"
for i in {1..30}; do
  if curl -fsS "http://127.0.0.1:${HTTP_PORT}/api/health" >/dev/null 2>&1; then
    echo "    healthy after ${i}s"
    break
  fi
  if [[ $i -eq 30 ]]; then
    echo "ERROR: service did not become healthy in 30s" >&2
    sudo -u "$APP_USER" docker compose logs --tail=50 web
    exit 1
  fi
  sleep 1
done

# ---------- 7. UFW firewall --------------------------------------------------
echo ">>> Configuring UFW"
ufw --force enable
ufw allow OpenSSH
ufw allow 80/tcp   # HTTP (certbot + redirect)
ufw allow 443/tcp  # HTTPS
ufw --force reload

# ---------- 8. nginx reverse proxy -------------------------------------------
echo ">>> Configuring nginx"
cat > /etc/nginx/sites-available/kateb <<EOF
# Upstream: kateb-web container listens on 8000
upstream kateb_web {
    server 127.0.0.1:${HTTP_PORT};
}

# Redirect all HTTP to HTTPS (when certs are present)
server {
    listen 80;
    server_name ${DOMAIN} ${API_SUBDOMAIN}.${DOMAIN};

    # ACME http-01 challenge
    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}

# HTTPS
server {
    listen 443 ssl http2;
    server_name ${DOMAIN};

    client_max_body_size 50M;

    # Static assets — long cache, immutable filenames
    location /assets/ {
        proxy_pass http://kateb_web;
        proxy_set_header Host \$host;
        proxy_cache_valid 200 365d;
        add_header Cache-Control "public, max-age=31536000, immutable";
    }

    # SPA fallback
    location / {
        proxy_pass http://kateb_web;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}

# API on a dedicated subdomain (api.katibai.xyz)
server {
    listen 443 ssl http2;
    server_name ${API_SUBDOMAIN}.${DOMAIN};

    client_max_body_size 50M;

    location / {
        proxy_pass http://kateb_web;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 180s;  # letter generation can take 60-120s
    }
}
EOF

# Disable the default site
rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/kateb /etc/nginx/sites-enabled/kateb
nginx -t && systemctl reload nginx

# ---------- 9. SSL ----------------------------------------------------------
if [[ "$USE_HTTPS" == "true" ]]; then
  if [[ -n "$EMAIL" ]]; then
    echo ">>> Requesting Let's Encrypt cert for ${DOMAIN} and ${API_SUBDOMAIN}.${DOMAIN}"
    certbot --nginx \
      --non-interactive --agree-tos -m "$EMAIL" \
      -d "$DOMAIN" -d "${API_SUBDOMAIN}.${DOMAIN}"
  else
    echo ">>> EMAIL not set, skipping certbot. Run manually:"
    echo "    certbot --nginx -d ${DOMAIN} -d ${API_SUBDOMAIN}.${DOMAIN}"
  fi
fi

# ---------- 10. Done --------------------------------------------------------
cat <<EOF

=================================================================
  Kateb is live.
=================================================================
  Frontend : https://${DOMAIN}              (or http://<vps-ip> if no cert)
  API      : https://${API_SUBDOMAIN}.${DOMAIN}/api/health
  Worker   : docker compose ps              (kateb-worker)
  Logs     : docker compose logs -f

  Next steps:
    1. Point your domain's DNS to this VPS's IP
       (A record: @ → <vps-ip>, A record: api → <vps-ip>)
    2. Wait for DNS propagation, then re-run certbot
    3. Add CORS_ORIGINS=https://${DOMAIN} to .env and restart
    4. Deploy the frontend to Vercel (it will proxy /api/* to
       https://${API_SUBDOMAIN}.${DOMAIN})

EOF
