# Deployment

Three production-ready deployment paths for Kateb. All three keep the
**Python worker as a long-running service** with automatic restart on
failure, so the client never has to think about it.

Pick the one that matches your environment.

| Path | When to use |
|------|-------------|
| **Docker Compose** | You have a Linux server (cloud VPS, on-prem). The cleanest, most portable path. |
| **Windows Service (NSSM)** | You're hosting on Windows (Azure VM, on-prem). The worker runs as a real Windows service. |
| **systemd unit** | You're on a Linux server with systemd (Ubuntu/Debian/CentOS/etc.) and want tighter OS integration than Docker. |

All three use the same code and the same `.env` file. The client
upload flow is **identical** — they just differ in how the two
long-running processes (web + worker) are supervised.

## 0. Prerequisites (all paths)

1. The Kateb project is checked out at a stable path on the server
   (e.g. `/opt/kateb` or `C:\Kateb`).
2. A Python 3.11+ virtualenv is set up and `pip install -r
   requirements.txt` has been run.
3. A `.env` file exists with the Supabase, OpenRouter, and other
   credentials (see `docs/setup.md` for the full list).
4. The 3 Supabase Storage buckets (`templates`, `policies`,
   `regulations`) exist and are operational. See
   `tests/SETUP_BUCKETS.md` for the one-time manual step.
5. (Optional) Tesseract with Arabic pack and LibreOffice are
   installed for OCR / .doc support. The pipeline handles their
   absence gracefully — affected files just wait as `needs_ocr` /
   `needs_doc_conversion` until the tools are installed and the
   worker's `auto-retry-needs` sweep picks them up.

## 1. Docker Compose (recommended for cloud VPS)

```bash
cd /opt/kateb
cp .env .env
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f worker
```

That's it. `docker compose` will:

* start `kateb-web` (FastAPI, port 8000, dashboard at `/admin`)
* start `kateb-worker` (the long-running processor)
* restart either if it crashes (`restart: unless-stopped`)
* on boot, both come back up automatically (systemd / Docker autostart)

To scale processing throughput, run multiple workers:

```bash
docker compose up -d --scale worker=3
```

Multiple workers are safe because the `processing_jobs.claimed_by`
soft lock prevents two workers from picking up the same file.

To start the optional Telegram bot:

```bash
docker compose --profile bot up -d
```

## 2. Windows Service (NSSM)

The project ships with `scripts/install_windows_service.ps1` that
wraps the Python worker as a real Windows service.

1. **Install NSSM** (Non-Sucking Service Manager) from
   <https://nssm.cc/download>. Extract to e.g. `C:\Program Files\nssm\`
   and add that folder to `PATH`.
2. **Create a venv and install dependencies** in the project root:
   ```powershell
   cd C:\Kateb
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
3. **Run the installer from an elevated PowerShell**:
   ```powershell
   .\scripts\install_windows_service.ps1
   ```
4. The service is installed and started. It will:
   * auto-start when Windows boots
   * restart automatically if the worker crashes (NSSM default)
   * log to `C:\ProgramData\Kateb\logs\worker.out.log` and
     `worker.err.log` (rotated at 10MB)

Useful commands:

```powershell
sc query KatebWorker
nssm stop KatebWorker
nssm start KatebWorker
nssm restart KatebWorker
nssm remove KatebWorker confirm
Get-Content C:\ProgramData\Kateb\logs\worker.out.log -Wait
```

To run the web server (chat + dashboard) on Windows, either:

* install a second NSSM service for `scripts\run_web.py` (port 8000),
  or
* just run `python scripts\run_web.py` in a console session — it's
  auto-recovering but won't survive a reboot unless you also install
  it as a service.

## 3. systemd unit (Linux without Docker)

```ini
# /etc/systemd/system/kateb-worker.service
[Unit]
Description=Kateb async processing worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=kateb
Group=kateb
WorkingDirectory=/opt/kateb
Environment="PYTHONUNBUFFERED=1"
Environment="PYTHONIOENCODING=utf-8"
ExecStart=/opt/kateb/.venv/bin/python /opt/kateb/scripts/worker.py --poll-interval 5 --chunk-budget 200
Restart=always
RestartSec=5
StandardOutput=append:/var/log/kateb/worker.out.log
StandardError=append:/var/log/kateb/worker.err.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo useradd -r -s /bin/false kateb
sudo mkdir -p /var/log/kateb && sudo chown kateb:kateb /var/log/kateb
sudo systemctl daemon-reload
sudo systemctl enable --now kateb-worker
sudo systemctl status kateb-worker
sudo journalctl -u kateb-worker -f
```

Repeat for `kateb-web.service` with `ExecStart=/opt/kateb/.venv/bin/uvicorn
api.main:app --host 0.0.0.0 --port 8000 --workers 2`.

## 4. What "fully automatic" means

The client only ever sees the dashboard. Behind the scenes, the
pipeline is self-healing on every failure mode:

| Failure | Recovery |
|---|---|
| Worker crashes mid-batch | Soft lock expires after 5 min; another worker re-claims. The processor's resume step reads already-saved chunks and continues from where it left off. No progress is lost. |
| 2,700-chunk file interrupted at chunk 1,400 | The DB row stays at `processed_chunks=1400`; the next worker run picks up at chunk 1,401. Zero re-work. |
| Embedding API returns 429 | Per-batch backoff: 15s → 30s → 45s → 60s. If all 4 retries fail, the job is re-queued with exponential delay. |
| Embedding API returns 400 (one input too long) | The embedder recursively splits the batch in half and tries again, isolating the bad chunk. |
| Tesseract / LibreOffice is missing | File is parked in `needs_ocr` / `needs_doc_conversion`. The worker's `auto-retry-needs` sweep (every 5 min) re-enqueues as soon as the tool is detected. No client action needed. |
| Server reboot | `restart: unless-stopped` (Docker) / `Restart=always` (systemd) / NSSM restart (Windows) bring the worker back up. The stale-claim reaper (`reap_stale_jobs`) re-queues any jobs left in `processing` by a worker that didn't get to release the lock. |
| The user uploads a new version of an existing document | The old chunks stay in `document_chunks` (linked to the document, not the file). The new version builds its chunks in parallel. When the worker finishes, it sets `documents.current_version` to the new version and `is_active=true`. Search switches to the new version atomically. |
| The user deactivates a document | `documents.is_active = false`. `match_documents` RPC filters it out — it never appears in AI retrieval again, even though the chunks stay in the DB. |
| Network drop during upload | The Supabase Storage client retries. The dashboard shows progress; if the user closes the browser, the partial upload is cleaned up by Supabase. |

## 5. Edge Function — optional

The `supabase/functions/storage-webhook/` Edge Function is a
**future** entry point for uploads coming from the Supabase JS SDK
directly (e.g., a custom mobile app or another tool). The dashboard
flow does NOT depend on it — uploads go through `POST /api/admin/upload`
which handles everything.

If you don't need the Edge Function entry point, you can ignore it.
The dashboard works without it.
