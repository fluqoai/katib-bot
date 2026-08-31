"""Resumable file processor for the Kateb async pipeline.

This module is the heart of the new ingestion pipeline. It processes
ONE `document_file` at a time, splitting the work into small embedding
batches and persisting progress after each batch so a crash, rate
limit, or process restart never loses more than one batch of work.

Public surface
==============
* `claim_next_job(supabase)`                          — worker-side claim
* `process_document_file(supabase, embedder, file_id)` — the main loop
* `get_progress(supabase, file_id)`                    — for the dashboard
* `cancel_job(supabase, file_id)`                      — admin cancel

Resumable contract
==================
For a file with N chunks and a worker that embeds B chunks per HTTP
call, the worker:

  1. Reads the file from Supabase Storage (one-shot download).
  2. Runs the loader (text/docx/pdf/ocr) → text.
  3. Runs the chunker (256-token cap, paragraph/heading aware) → chunks.
  4. Counts already-saved chunks in `document_chunks` for this file.
  5. For each batch of `BATCH_SIZE` chunks not yet saved:
       a. Embed the batch (with the rate-limit-aware embedder).
       b. Insert chunks with `on conflict (document_id, chunk_index) do nothing`.
       c. Update `document_files.processing_progress` JSONB snapshot.
       d. Commit (the supabase-py wrapper auto-flushes; we just do
          sequential calls).
  6. When all chunks are saved, set `document_files.status = 'indexed'`
     and `processing_jobs.status = 'done'`.

A crash between step 5b and 5c is safe: the chunks are already in
`document_chunks`; on the next run step 4 sees them and skips. A
crash before 5b is also safe: those chunks just aren't saved yet;
the next run retries them.

The worker is also defensive against:
  * 429 (rate limit) — handled by the embedder's exponential backoff.
  * 400 (input too long) — handled by the embedder's batch-split.
  * Loader failures (needs_ocr, needs_doc_conversion) — recorded as
    status on `document_files` and the job marked `done` (no
    requeue — the user must install the missing tool and click
    "Reprocess").
  * Embedding failures (e.g. upstream model outage) — `attempts` is
    incremented and the job is requeued with `next_retry_at` set.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import socket
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from supabase import Client

from rag.chunker import chunk_document
from rag.embeddings import EmbeddingClient
from rag.loaders import load_any


logger = logging.getLogger(__name__)


# How many chunks to embed per HTTP call to the embedding API.
# OpenRouter's Liquid LFM rejects very large batches; 20 is safe.
EMBED_BATCH = 20

# How many embed batches to do before yielding (and persisting) progress.
# Each session processes up to SESSION_CHUNK_BUDGET chunks. The next
# invocation picks up where this one left off.
SESSION_CHUNK_BUDGET = 200

# How long to sleep between embed batches (seconds). 0.8s keeps us
# safely under OpenRouter's free-tier ~20 req/min ceiling.
INTER_BATCH_SLEEP_S = 0.8

# Stale-job claim timeout: a worker that died without releasing its
# claim can be re-claimed by another worker after this many seconds.
STALE_CLAIM_TIMEOUT_S = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Job claim / lifecycle
# ---------------------------------------------------------------------------

def _worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty_progress() -> dict[str, Any]:
    return {
        "total_chunks": 0,
        "processed_chunks": 0,
        "failed_chunks": 0,
        "current_batch": 0,
        "retry_count": 0,
        "last_error": None,
        "started_at": None,
        "updated_at": None,
    }


def claim_next_job(
    supabase: Client,
    *,
    worker_id: str | None = None,
) -> dict[str, Any] | None:
    """Atomically claim the next pending `processing_job`.

    Returns the claimed job row, or `None` if nothing to do.

    The claim uses `claimed_by` + `claimed_at` for soft locking:
    - Pending jobs: claimed if `claimed_at` is null OR older than
      STALE_CLAIM_TIMEOUT_S (the previous worker died).
    - The same worker that claimed a job is the only one that can
      update its progress.
    """
    wid = worker_id or _worker_id()
    now = _now_iso()
    stale_cutoff = datetime.fromtimestamp(
        time.time() - STALE_CLAIM_TIMEOUT_S, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        # Find the oldest pending job (with stale-claim re-claim)
        resp = (
            supabase.table("processing_jobs")
            .select("*, document_files(*)")
            .eq("status", "pending")
            .or_(f"claimed_at.is.null,claimed_at.lt.{stale_cutoff}")
            .order("priority", desc=True)
            .order("scheduled_at")
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            return None
        job = rows[0]
        job_id = job["id"]
        # Atomic-ish claim: UPDATE WHERE id = job_id AND status = 'pending'
        upd = (
            supabase.table("processing_jobs")
            .update({
                "status": "processing",
                "claimed_by": wid,
                "claimed_at": now,
                "started_at": now,
                "attempts": (job.get("attempts") or 0) + 1,
            })
            .eq("id", job_id)
            .eq("status", "pending")
            .execute()
        )
        if not upd.data:
            # Someone else claimed it between our SELECT and UPDATE
            return None
        return upd.data[0]
    except Exception as e:  # noqa: BLE001
        logger.exception("claim_next_job failed: %s", e)
        return None


def mark_job_done(
    supabase: Client, job_id: str, *, final_status: str = "done"
) -> None:
    supabase.table("processing_jobs").update({
        "status": final_status,
        "completed_at": _now_iso(),
        "claimed_by": None,
    }).eq("id", job_id).execute()


def requeue_job(
    supabase: Client, job_id: str, *, error: str, retry_in_s: int = 60
) -> None:
    """Mark the job as failed-attempt; requeue for another try later."""
    from datetime import timedelta
    next_at = (datetime.now(timezone.utc) + timedelta(seconds=retry_in_s)
               ).strftime("%Y-%m-%dT%H:%M:%SZ")
    supabase.table("processing_jobs").update({
        "status": "pending",
        "error_message": error[:1000],
        "next_retry_at": next_at,
        "claimed_by": None,
    }).eq("id", job_id).execute()


def mark_job_dead(
    supabase: Client, job_id: str, *, error: str
) -> None:
    """Mark the job as terminally failed (no more retries)."""
    supabase.table("processing_jobs").update({
        "status": "failed",
        "completed_at": _now_iso(),
        "error_message": error[:1000],
        "claimed_by": None,
    }).eq("id", job_id).execute()


# ---------------------------------------------------------------------------
# File download
# ---------------------------------------------------------------------------

def _download_from_storage(
    supabase: Client, bucket: str, path: str
) -> bytes:
    """Download a file from Supabase Storage as bytes."""
    resp = supabase.storage.from_(bucket).download(path)
    if not resp:
        raise RuntimeError(f"empty response from storage: {bucket}/{path}")
    return resp


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# The main processing function
# ---------------------------------------------------------------------------

def _update_progress(
    supabase: Client,
    file_id: str,
    *,
    total_chunks: int | None = None,
    processed_chunks: int | None = None,
    failed_chunks: int | None = None,
    current_batch: int | None = None,
    last_error: str | None = None,
    **extra: Any,
) -> None:
    """Persist a progress snapshot to `document_files.processing_progress`."""
    # Read current snapshot (cheap because the row is hot in the page cache)
    cur = (
        supabase.table("document_files")
        .select("processing_progress")
        .eq("id", file_id)
        .limit(1)
        .execute()
    )
    snap = (cur.data[0].get("processing_progress") or {}) if cur.data else {}
    snap = {**_empty_progress(), **snap}
    if total_chunks is not None:
        snap["total_chunks"] = total_chunks
    if processed_chunks is not None:
        snap["processed_chunks"] = processed_chunks
    if failed_chunks is not None:
        snap["failed_chunks"] = failed_chunks
    if current_batch is not None:
        snap["current_batch"] = current_batch
    if last_error is not None:
        snap["last_error"] = last_error[:500]
    snap["retry_count"] = snap.get("retry_count", 0) + (1 if last_error else 0)
    snap["updated_at"] = _now_iso()
    snap.update(extra)
    supabase.table("document_files").update({
        "processing_progress": snap,
    }).eq("id", file_id).execute()


def _set_file_status(
    supabase: Client,
    file_id: str,
    *,
    status: str,
    error_message: str | None = None,
    extracted_char_count: int | None = None,
    chunk_count: int | None = None,
    embedding_count: int | None = None,
) -> None:
    payload: dict[str, Any] = {"status": status, "last_processed_at": _now_iso()}
    if error_message is not None:
        payload["error_message"] = error_message[:1000]
    if extracted_char_count is not None:
        payload["extracted_char_count"] = extracted_char_count
    if chunk_count is not None:
        payload["chunk_count"] = chunk_count
    if embedding_count is not None:
        payload["embedding_count"] = embedding_count
    supabase.table("document_files").update(payload).eq("id", file_id).execute()


def _activate_version(supabase: Client, file_row: dict[str, Any]) -> None:
    """Point the parent document at this version and mark active."""
    supabase.table("documents").update({
        "current_version": file_row["version"],
        "is_active": True,
        "title": file_row.get("original_filename") or file_row.get("title", "untitled"),
        "bucket": file_row["storage_bucket"],
        "processing_progress": {
            "current_file_id": file_row["id"],
            "current_version": file_row["version"],
            "activated_at": _now_iso(),
        },
    }).eq("id", file_row["document_id"]).execute()


# ---------------------------------------------------------------------------
# The public entry point
# ---------------------------------------------------------------------------

async def process_document_file(
    supabase: Client,
    embedder: EmbeddingClient,
    file_id: str,
    *,
    chunk_budget: int = SESSION_CHUNK_BUDGET,
) -> dict[str, Any]:
    """Process ONE document_file. Returns a small status dict.

    The function is safe to call multiple times for the same file: it
    picks up where it left off based on the chunks already in
    `document_chunks`.
    """
    # 1) Load the file metadata
    resp = (
        supabase.table("document_files")
        .select("*")
        .eq("id", file_id)
        .limit(1)
        .execute()
    )
    if not resp.data:
        raise RuntimeError(f"document_file {file_id} not found")
    f = resp.data[0]
    log = logger.getChild(file_id[:8])
    log.info("→ %s v%d (%s, %s bytes, %s)",
             f.get("original_filename"), f["version"],
             f["storage_bucket"], f.get("size_bytes"), f.get("mime_type"))

    # 2) Download the bytes (one shot, no streaming yet — the loader needs random access)
    try:
        data = _download_from_storage(
            supabase, f["storage_bucket"], f["storage_path"]
        )
    except Exception as e:  # noqa: BLE001
        log.exception("download failed: %s", e)
        _set_file_status(supabase, file_id, status="failed", error_message=f"download: {e}")
        return {"status": "failed", "stage": "download", "error": str(e)}

    # 3) Sanity: did the sha match what the uploader claimed?
    if f.get("sha256") and _sha256(data) != f["sha256"]:
        msg = "sha256 mismatch on download — file changed in storage?"
        log.error(msg)
        _set_file_status(supabase, file_id, status="failed", error_message=msg)
        return {"status": "failed", "stage": "verify", "error": msg}

    # 4) Load + extract text
    # The loader wants a file path. We write the bytes to a temp file
    # with the right extension so the dispatcher can pick the right
    # loader. Tesseract/LibreOffice subprocesses can read this path.
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="kateb-proc-")) / f.get("original_filename", f"{file_id}.bin")
    tmp.write_bytes(data)
    try:
        loaded = load_any(tmp)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
        try:
            tmp.parent.rmdir()
        except OSError:
            pass

    if not loaded.ok:
        # Non-fatal: needs_ocr, needs_doc_conversion, empty, or loader error.
        # We record the status on the file and stop. The user can install
        # the missing tool and click Reprocess.
        log.info("loader returned non-ok: %s — %s", loaded.status, loaded.error_message)
        _set_file_status(
            supabase, file_id,
            status=("needs_ocr" if loaded.status == "needs_ocr"
                    else "needs_doc_conversion" if loaded.status == "needs_doc_conversion"
                    else "failed"),
            error_message=loaded.error_message,
            extracted_char_count=len(loaded.text or ""),
        )
        return {
            "status": loaded.status,
            "stage": "load",
            "extracted_char_count": len(loaded.text or ""),
            "error": loaded.error_message,
        }

    char_count = len(loaded.text)

    # 5) Chunk
    chunks, stats = chunk_document(loaded.text)
    if not chunks:
        _set_file_status(
            supabase, file_id,
            status="failed",
            error_message="chunker produced 0 chunks",
            extracted_char_count=char_count,
        )
        return {
            "status": "failed",
            "stage": "chunk",
            "extracted_char_count": char_count,
            "error": "chunker produced 0 chunks",
        }

    log.info("chunked: %d blocks → %d chunks (max %d tokens)",
             stats.num_blocks, stats.num_chunks, stats.max_tokens)
    _update_progress(supabase, file_id, total_chunks=stats.num_chunks,
                     processed_chunks=0, current_batch=0,
                     started_at=_now_iso())

    # 6) Find already-saved chunk indexes (resume support).
    # Scope: per-FILE. `chunk_index` is unique within a (document_file_id,
    # chunk_index) pair (migration 006). Looking at the whole document
    # would wrongly count V1's chunk_index=0 as "done" for V2, skipping
    # V2's actual chunk insert.
    saved = (
        supabase.table("document_chunks")
        .select("chunk_index")
        .eq("document_file_id", file_id)
        .execute()
        .data
    )
    done_idx = {r["chunk_index"] for r in saved}
    log.info("resume: %d/%d chunks already in DB", len(done_idx), stats.num_chunks)

    # 7) Walk chunks not yet saved, in batches
    todo = [i for i in range(len(chunks)) if i not in done_idx]
    todo = todo[:chunk_budget]   # this session's budget
    processed = 0
    failed = 0
    for off in range(0, len(todo), EMBED_BATCH):
        batch_idx = todo[off : off + EMBED_BATCH]
        batch_texts = [chunks[i] for i in batch_idx]
        current_batch = (off // EMBED_BATCH) + 1
        log.info("embed batch %d: %d chunks (%.0f%% of this session)",
                 current_batch, len(batch_idx),
                 100 * (off + len(batch_idx)) / max(1, len(todo)))
        try:
            vecs = await embedder.embed_texts(batch_texts)
        except Exception as e:  # noqa: BLE001
            logger.exception("embed batch %d failed: %s", current_batch, e)
            _update_progress(
                supabase, file_id,
                processed_chunks=processed,
                failed_chunks=failed,
                current_batch=current_batch,
                last_error=f"embed: {type(e).__name__}: {e}",
            )
            raise

        if len(vecs) != len(batch_idx):
            raise RuntimeError(
                f"embedder returned {len(vecs)} vectors for {len(batch_idx)} chunks"
            )

        # 8) Insert chunks — idempotent on (document_file_id, chunk_index)
        # for safety against partial retries. The DB also has a unique
        # constraint on (document_file_id, chunk_index) so a true duplicate
        # insert would fail. We pre-filter to avoid that.
        rows = [
            {
                "document_id": f["document_id"],
                "document_file_id": file_id,
                "version": f["version"],
                "chunk_index": i,
                "content": chunks[i],
                "embedding": vec,
            }
            for i, vec in zip(batch_idx, vecs)
        ]
        # Per-file dedup for crash safety: re-query the existing chunk
        # indexes for THIS file (not the whole document) so we never
        # insert a duplicate (document_file_id, chunk_index) pair. The
        # initial `done_idx` was used to skip chunks already in DB at
        # resume time, but we also re-check per-batch so a race or
        # partial retry can't double-insert.
        existing_file_idx = set(
            r["chunk_index"] for r in (
                supabase.table("document_chunks")
                .select("chunk_index")
                .eq("document_file_id", file_id)
                .execute()
                .data or []
            )
        )
        try:
            for row in rows:
                if row["chunk_index"] in existing_file_idx:
                    continue
                supabase.table("document_chunks").insert(row).execute()
                existing_file_idx.add(row["chunk_index"])
                # NOTE: do NOT add to `done_idx` here. `done_idx` is the
                # snapshot of chunks already in DB at the start of this
                # session (used for the resume-skip logic in step 7). The
                # `processed` counter tracks new inserts this session;
                # `total_saved = len(done_idx) + processed` then correctly
                # counts "initially-existing" + "newly-inserted" without
                # double-counting.
                processed += 1
        except Exception as e:  # noqa: BLE001
            logger.exception("chunk insert failed at idx %s: %s", rows[0]["chunk_index"] if rows else "?", e)
            _update_progress(
                supabase, file_id,
                processed_chunks=processed,
                failed_chunks=failed + 1,
                current_batch=current_batch,
                last_error=f"db insert: {type(e).__name__}: {e}",
            )
            raise

        _update_progress(
            supabase, file_id,
            processed_chunks=processed,
            current_batch=current_batch,
        )

        # Gentle pace for the free tier
        if off + EMBED_BATCH < len(todo):
            await asyncio.sleep(INTER_BATCH_SLEEP_S)

    # 9) Decide status
    total_saved = len(done_idx) + processed
    if total_saved >= stats.num_chunks:
        log.info("✓ all %d chunks saved — file is fully indexed", total_saved)
        _set_file_status(
            supabase, file_id,
            status="indexed",
            error_message=None,
            extracted_char_count=char_count,
            chunk_count=total_saved,
            embedding_count=total_saved,
        )
        _activate_version(supabase, f)
        # Also flip the parent document's status so the dashboard /
        # acceptance tests can see the file is ready without having to
        # join through document_files. Without this sync, the document
        # row stays at 'pending' forever and the admin UI shows a
        # misleading "processing" state.
        try:
            supabase.table("documents").update({
                "status": "indexed",
                "chunk_count": total_saved,
            }).eq("id", f["document_id"]).execute()
        except Exception as e:  # noqa: BLE001
            log.warning("could not sync documents.status to indexed: %s", e)
        return {
            "status": "indexed",
            "stage": "done",
            "extracted_char_count": char_count,
            "chunk_count": total_saved,
            "embedding_count": total_saved,
        }

    # 10) Partial — there are more chunks left for the next session
    log.info("session done: %d/%d chunks saved. %d more to go.",
             total_saved, stats.num_chunks, stats.num_chunks - total_saved)
    _set_file_status(
        supabase, file_id,
        status="processing",
        error_message=None,
        extracted_char_count=char_count,
        chunk_count=total_saved,
        embedding_count=total_saved,
    )
    return {
        "status": "processing",
        "stage": "partial",
        "extracted_char_count": char_count,
        "chunk_count": total_saved,
        "embedding_count": total_saved,
        "remaining": stats.num_chunks - total_saved,
    }


__all__ = [
    "EMBED_BATCH",
    "SESSION_CHUNK_BUDGET",
    "claim_next_job",
    "mark_job_done",
    "requeue_job",
    "mark_job_dead",
    "process_document_file",
]
