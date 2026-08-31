"""Kateb async processing worker.

Long-running process that polls `processing_jobs` for pending rows and
runs them through `rag.processor.process_document_file`. Designed to
survive crashes, restarts, and rate limits without losing more than
one embedding batch worth of work.

Resilience story (no human in the loop)
======================================
1. A 2,700-chunk file is interrupted at chunk 1,400 by anything
   (SIGKILL, OOM, 429 storm, network drop):
   a. The DB row is in `processing_jobs.status = 'processing'`,
      `claimed_by = <dead-host>`.
   b. After `STALE_CLAIM_TIMEOUT_S` (5 min) the next claim can
      re-claim it.
   c. The processor's resume step reads already-saved chunks from
      `document_chunks` and continues from chunk 1,400.
2. The embedder hits a 429 mid-batch:
   a. The batch retries with 15s exponential backoff (up to 4 tries).
   b. If all 4 fail, the whole job is re-queued with 30s/60s/120s/240s
      delay; the resume step picks up where the previous batch ended.
3. The user installs Tesseract / LibreOffice after the file was
   marked `needs_ocr` / `needs_doc_conversion`:
   a. The `recheck_external.py` script auto-re-enqueues all such
      files. Or run the worker with `--auto-retry-needs` flag
      (default on for daemon mode) to do this on every poll.

Usage
=====
    # Default: 5s poll interval, 1 session per claim
    python scripts/worker.py

    # Custom poll interval (seconds) and chunk budget per session
    python scripts/worker.py --poll-interval 10 --chunk-budget 500

    # One-shot mode (process one job, then exit) — useful for tests / cron
    python scripts/worker.py --once

    # Disable auto-retry of needs_ocr / needs_doc_conversion files
    python scripts/worker.py --no-auto-retry-needs

Graceful shutdown
=================
Ctrl-C / SIGTERM is caught: the worker finishes the current chunk
batch, persists progress, releases the job lock, and exits.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

from supabase import create_client  # noqa: E402
from bot.config import get_settings  # noqa: E402
from rag.embeddings import from_env as emb_from_env  # noqa: E402
from rag.processor import (  # noqa: E402
    claim_next_job,
    mark_job_dead,
    mark_job_done,
    process_document_file,
    requeue_job,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("worker")


_should_stop = False


# How long without a progress update before a `processing` job is
# considered abandoned and re-queued. This is the user's "max time
# a single file is allowed to stall" SLA. Default 30 minutes.
STALE_PROCESSING_TIMEOUT_S = 1800

# How long without a progress update before a file in `processing`
# (document_files.status) is re-queued for a fresh attempt. We
# read this from the file's `processing_progress.updated_at`.
STALE_FILE_PROGRESS_S = 1800

# How often to run the reaper / auto-retry sweeps.
SWEEP_INTERVAL_S = 60

# How long to wait between auto-retry sweeps for needs_ocr /
# needs_doc_conversion files. Default 5 minutes.
NEEDS_RECHECK_INTERVAL_S = 300


def _install_signal_handlers() -> None:
    def _handler(signum, frame):  # noqa: ARG001
        global _should_stop
        log.info("Received signal %s — will exit after current batch", signum)
        _should_stop = True

    signal.signal(signal.SIGINT, _handler)
    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError):
        # Not the main thread / not supported on Windows for SIGTERM
        pass


# ---------------------------------------------------------------------------
# Background sweeps (stale-job reaper, auto-retry of needs_* files)
# ---------------------------------------------------------------------------

def reap_stale_jobs(supabase, *, now: datetime) -> int:
    """Re-queue processing jobs whose claim has gone stale.

    A worker that dies while holding a claim leaves the job in
    `status='processing'`. After `STALE_PROCESSING_TIMEOUT_S` we
    treat the claim as abandoned and set the job back to `pending`
    so any worker can pick it up.

    Returns the number of jobs reaped.
    """
    cutoff = (now - timedelta(seconds=STALE_PROCESSING_TIMEOUT_S)
              ).strftime("%Y-%m-%dT%H:%M:%SZ")
    resp = (
        supabase.table("processing_jobs")
        .select("id, claimed_by, attempts, max_attempts")
        .eq("status", "processing")
        .lt("claimed_at", cutoff)
        .execute()
    )
    rows = resp.data or []
    n = 0
    for j in rows:
        log.warning(
            "reaper: job %s has stale claim by %s — requeueing",
            j["id"], j.get("claimed_by"),
        )
        # Bump attempts; if we've burnt through max_attempts, mark dead
        attempts = (j.get("attempts") or 0) + 1
        if attempts >= (j.get("max_attempts") or 5):
            mark_job_dead(
                supabase, j["id"],
                error="reaper: claim went stale; max attempts reached",
            )
        else:
            requeue_job(
                supabase, j["id"],
                error="reaper: claim went stale",
                retry_in_s=10,
            )
        n += 1
    return n


def auto_retry_needs_files(supabase) -> int:
    """Re-enqueue any document_file in needs_ocr / needs_doc_conversion.

    This runs every `NEEDS_RECHECK_INTERVAL_S` so that as soon as the
    user installs Tesseract or LibreOffice, the affected files start
    processing automatically. We also flip the file status to
    `pending` and create a fresh job.

    Returns the number of files re-queued.
    """
    resp = (
        supabase.table("document_files")
        .select("id, storage_bucket, storage_path, original_filename, status, "
                "processing_progress")
        .in_("status", ["needs_ocr", "needs_doc_conversion"])
        .execute()
    )
    rows = resp.data or []
    n = 0
    for f in rows:
        # Skip files that were backfilled from the pre-Storage era —
        # their storage_path is a local Windows path, not a real Storage
        # object, so the download would always fail.
        sp = f.get("storage_path") or ""
        if "\\" in sp or sp.startswith("/") or ":" in sp.split("/", 1)[0]:
            log.debug("auto_retry: skipping backfilled file %s (no Storage object)",
                      f["original_filename"])
            continue
        if not f.get("storage_bucket"):
            continue

        # Sanity check: the missing tool may still not be installed.
        # We re-download and try the loader — sub-second for any file.
        from rag.loaders import load_any
        from pathlib import PurePosixPath
        try:
            data = supabase.storage.from_(f["storage_bucket"]).download(f["storage_path"])
        except Exception as e:  # noqa: BLE001
            log.warning("auto_retry: download failed for %s: %s", f["id"], e)
            continue
        import tempfile as _tf
        with _tf.NamedTemporaryFile(
            delete=False, suffix=PurePosixPath(f["storage_path"]).suffix or ".bin",
        ) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        try:
            loaded = load_any(tmp_path)
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        if not loaded.ok:
            # Tool still missing — leave it alone, check again next sweep
            log.debug("auto_retry: %s still needs %s (no tool yet)",
                      f["original_filename"], loaded.status)
            continue
        # Tool is installed — re-queue
        log.info("auto_retry: tool available for %s — requeueing",
                 f["original_filename"])
        supabase.table("document_files").update({
            "status": "pending",
            "error_message": None,
            "processing_progress": {
                "total_chunks": 0,
                "processed_chunks": 0,
                "failed_chunks": 0,
                "current_batch": 0,
                "retry_count": 0,
                "last_error": None,
                "started_at": None,
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "auto_retried_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            "extracted_char_count": 0,
            "chunk_count": 0,
            "embedding_count": 0,
        }).eq("id", f["id"]).execute()
        supabase.table("document_chunks").delete().eq(
            "document_id", _doc_id_for(supabase, f["id"]),
        ).execute()
        supabase.table("processing_jobs").insert({
            "document_file_id": f["id"],
            "status": "pending",
            "priority": 150,        # high — the user just installed the tool
        }).execute()
        n += 1
    return n


def _doc_id_for(supabase, file_id: str) -> str:
    """Tiny helper: document_id for a given document_file id."""
    r = (
        supabase.table("document_files")
        .select("document_id")
        .eq("id", file_id)
        .limit(1)
        .execute()
    )
    return r.data[0]["document_id"] if r.data else ""


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    supabase = create_client(settings.supabase_url, settings.resolved_service_key())
    embedder = emb_from_env()
    log.info(
        "Worker started. Supabase=%s  embedder=%s/%s (dim=%d)  "
        "poll=%ds  budget=%d  auto_retry_needs=%s",
        settings.supabase_url, embedder.provider, embedder.model,
        embedder.dimension, args.poll_interval, args.chunk_budget,
        not args.no_auto_retry_needs,
    )

    last_sweep = 0.0
    last_needs_recheck = 0.0

    while not _should_stop:
        now = datetime.now(timezone.utc)

        # Background sweeps run on a wall-clock schedule, NOT every
        # poll iteration, so they don't add DB pressure.
        if (now.timestamp() - last_sweep) >= SWEEP_INTERVAL_S:
            try:
                n = reap_stale_jobs(supabase, now=now)
                if n:
                    log.info("reaper: re-queued %d stale jobs", n)
            except Exception as e:  # noqa: BLE001
                log.exception("reaper failed: %s", e)
            last_sweep = now.timestamp()

        if (not args.no_auto_retry_needs and
                (now.timestamp() - last_needs_recheck) >= NEEDS_RECHECK_INTERVAL_S):
            try:
                n = auto_retry_needs_files(supabase)
                if n:
                    log.info("auto_retry: re-enqueued %d needs_* files", n)
            except Exception as e:  # noqa: BLE001
                log.exception("auto_retry failed: %s", e)
            last_needs_recheck = now.timestamp()

        # Claim + process a job
        try:
            job = claim_next_job(supabase)
        except Exception as e:  # noqa: BLE001
            log.exception("claim failed: %s", e)
            await asyncio.sleep(args.poll_interval)
            continue

        if job is None:
            if args.once:
                log.info("no pending job — exiting (--once)")
                return 0
            await asyncio.sleep(args.poll_interval)
            continue

        job_id = job["id"]
        file_id = job.get("document_file_id")
        attempts = job.get("attempts", 0)
        max_attempts = job.get("max_attempts", 5)
        log.info("claimed job %s  file=%s  attempt=%d/%d",
                 job_id, file_id, attempts, max_attempts)

        try:
            result = await process_document_file(
                supabase, embedder, file_id, chunk_budget=args.chunk_budget,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("processing crashed for %s: %s", file_id, e)
            if attempts >= max_attempts:
                mark_job_dead(supabase, job_id, error=f"max attempts reached: {e}")
            else:
                requeue_job(
                    supabase, job_id, error=f"{type(e).__name__}: {e}",
                    retry_in_s=min(30 * (2 ** (attempts - 1)), 600),
                )
            if args.once:
                return 1
            continue

        st = result.get("status")
        if st == "indexed":
            mark_job_done(supabase, job_id, final_status="done")
        elif st in ("needs_ocr", "needs_doc_conversion"):
            mark_job_done(supabase, job_id, final_status="done")
            log.info("file %s is %s — reaper will retry when tool is installed",
                     file_id, st)
        elif st == "processing":
            # Partial: more chunks left, requeue immediately
            mark_job_done(supabase, job_id, final_status="pending")
            log.info("file %s partial (%d chunks saved) — requeued",
                     file_id, result.get("chunk_count"))
        else:
            if attempts >= max_attempts:
                mark_job_dead(
                    supabase, job_id, error=result.get("error", "unknown"),
                )
            else:
                requeue_job(
                    supabase, job_id, error=result.get("error", "unknown"),
                    retry_in_s=60,
                )

        if args.once:
            return 0

    log.info("Worker stopped.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Kateb async processing worker.")
    parser.add_argument(
        "--poll-interval", type=int, default=5,
        help="Seconds to wait between polls when the queue is empty (default: 5).",
    )
    parser.add_argument(
        "--chunk-budget", type=int, default=200,
        help="Max chunks to process per session per file (default: 200).",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Process one job (or wait once) then exit. Useful for tests / cron.",
    )
    parser.add_argument(
        "--no-auto-retry-needs", action="store_true",
        help="Disable the auto-retry sweep for needs_ocr / needs_doc_conversion files. "
             "On by default — files in those states are re-enqueued automatically as "
             "soon as Tesseract / LibreOffice is detected.",
    )
    args = parser.parse_args()
    _install_signal_handlers()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
