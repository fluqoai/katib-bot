# Kateb — deployment guide

This document describes how to deploy Kateb to production. The architecture
is **split**: the React frontend is hosted on **Vercel** (CDN), and the
FastAPI backend + worker run on a **single VPS** (Hetzner / DigitalOcean).

```
                          ┌──────────────────────────┐
   user ── HTTPS ────────▶│ Vercel CDN               │
                          │ katibai.xyz              │
                          │   (React SPA)            │
                          └──────────┬───────────────┘
                                     │ /api/* rewrite
                                     ▼
                          ┌──────────────────────────┐
                          │ VPS (Hetzner CX22 €4.85) │
                          │ api.katibai.xyz          │
                          │  ┌─ nginx (TLS)          │
                          │  ├─ FastAPI (uvicorn)    │──▶ Supabase
                          │  └─ worker.py (systemd)  │     (Postgres + Storage)
                          │                          │
                          │                          │──▶ OpenRouter (LLM)
                          └──────────────────────────┘
```

---

## 1. DNS (GoDaddy)

The split is **Vercel for the frontend, VPS for the API** — so DNS points
each role to the right host:

| Type | Host | Value | Notes |
|------|------|-------|-------|
| `A` | `@` | `76.76.21.21` | **Vercel** (frontend) — use whatever your Vercel domain card shows; `76.76.21.21` is the default anycast IP |
| `A` | `api` | `<VPS_IP>` | **VPS** (API) |
| `CNAME` | `www` | `katibai.xyz` | Optional — `www` → apex (so both work) |

**Important:** the apex `A` record must point at **Vercel**, not the VPS.
Vercel is what serves the React SPA. The VPS only serves the API on the
`api.` subdomain, and Vercel's `vercel.json` rewrites `/api/*` to it.

**Wait for DNS propagation** (1–5 min for new records, up to 30 min for
changes, depending on the resolver's TTL). Use `Resolve-DnsName
katibai.xyz -Server 1.1.1.1` to check from a fresh resolver; the
default Windows resolver often caches the old value longest.

---

## 2. VPS provisioning

A small Ubuntu VPS with at least 2 vCPUs and 4 GB RAM is enough. Tested on:
- **Hetzner CX22** (€4.85/month) — recommended
- **Vultr regular cloud** (similar specs) — what we deployed on (Atlanta, Ubuntu 26.04)
- DigitalOcean, Linode, AWS Lightsail — should all work

```bash
# 1. Create the VM: Ubuntu 24.04 LTS or 26.04 LTS, any region
# 2. SSH in as root:
ssh root@<VPS_IP>

# 3. (Optional) Create a non-root user and harden SSH
adduser kateb --disabled-password --gecos ""
usermod -aG sudo kateb
# Add your public key to /home/kateb/.ssh/authorized_keys
# Disable root login + password auth in /etc/ssh/sshd_config, then:
systemctl restart sshd
```

The one-shot `deploy.sh` installs everything else (Python, nginx, certbot, Docker) and creates the `kateb` user if missing — so step 3 is optional.

---

## 3. Deploy the backend on the VPS

```bash
# As root (or any user with sudo)
git clone https://github.com/fluqoai/katib-bot.git /opt/kateb
cd /opt/kateb

# Configure
cp .env.example .env
nano .env   # fill in SUPABASE_*, OPENROUTER_API_KEY, CORS_ORIGINS
# Production .env MUST set:
#   CORS_ORIGINS=https://katibai.xyz,https://www.katibai.xyz
#   FRONTEND_ONLY=true   (Vercel serves the UI, so skip Vite build in Docker)

# One-shot install + start
EMAIL=you@example.com bash deploy.sh
```

`deploy.sh` will:
1. Install system packages (Python, nginx, certbot, fonts, poppler)
2. Create the `kateb` user (idempotent)
3. Install Docker + Compose (idempotent)
4. Build the Docker images (web + worker)
5. Start them via `docker compose up -d`
6. Wait for the `/api/health` healthcheck
7. Configure UFW (22, 80, 443)
8. Write the nginx reverse-proxy config (with TLS placeholders)
9. Request a Let's Encrypt cert via the webroot challenge
10. Reload nginx

After it finishes, the API is live at `https://api.katibai.xyz/api/health`.

If you need to re-run certbot later (e.g. cert is about to expire), the
webroot approach is used so it works regardless of the current nginx
state:

```bash
EMAIL=you@example.com certbot certonly --webroot -w /var/www/html \
  --non-interactive --agree-tos -m you@example.com \
  -d katibai.xyz -d api.katibai.xyz
systemctl reload nginx
```

---

## 4. Deploy the frontend on Vercel

1. Go to [vercel.com](https://vercel.com) → Sign up with GitHub
2. **Add New Project** → import `fluqoai/katib-bot`
3. Vercel auto-detects the Vite framework. **Override** (do NOT rely on auto-detect):
   - **Install command:** `cd client && npm ci`
   - **Build command:** `cd client && npm run build`
   - **Output directory:** `client/dist`
4. **Environment variables:** (none needed for the client)
5. **Deploy** → Vercel gives you a `<hash>.vercel.app` URL
6. **Add domain** → `katibai.xyz` (and `www.katibai.xyz`)
7. Vercel auto-issues a Let's Encrypt cert for the Vercel side

> **Note:** `npm --prefix client ...` does NOT work the way you'd expect
> in Vercel's shell — it doesn't actually `cd` into the directory. Use
> `cd client && ...` instead.

The `vercel.json` at the repo root already configures:
- `/api/*` rewrite → `https://api.katibai.xyz/api/*`
- SPA fallback (every unknown path → `index.html`)
- Security headers + 1-year immutable cache for `/assets/*`

---

## 5. Wire CORS on the backend

The backend needs to allow the Vercel frontend origin. Edit `.env` on the VPS:

```bash
CORS_ORIGINS=https://katibai.xyz,https://www.katibai.xyz
```

Then restart:

```bash
cd /opt/kateb
sudo docker compose restart web
```

---

## 6. Verify

- Frontend: https://katibai.xyz (should load the React SPA)
- API health: https://api.katibai.xyz/api/health (should return `{"status":"ok",...}`)
- CORS: open browser dev tools → Network → make a request → check the `Access-Control-Allow-Origin` header

---

## 7. Updates

```bash
# On the VPS
cd /opt/kateb
git pull
sudo docker compose build
sudo docker compose up -d

# Frontend
# Just `git push` — Vercel auto-deploys on every commit to main.
```

---

## Cost

| Item | Provider | Cost |
|------|----------|------|
| VPS (CX22) | Hetzner | €4.85/month (~$5.30) — recommended |
| VPS (2 vCPU / 8 GB) | Vultr | ~$5–24/month — also fine, what we deployed on |
| Domain (katibai.xyz) | GoDaddy | ~$12/year |
| SSL | Let's Encrypt | Free |
| DDoS / DNS proxy | Cloudflare (optional) | Free |
| Supabase (Pro) | Supabase | $25/month (only if you outgrow free tier) |
| OpenRouter (LLM) | OpenRouter | pay per token, ~$5–50/month at MVP scale |
| Vercel | Vercel | Free tier is fine for MVP |

**Total: ~$5/month + LLM usage (~< $50/month).**
