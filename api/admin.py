"""Admin REST API for the Kateb dashboard.

Routes
======
POST   /api/admin/upload              — multipart upload; creates document +
                                         document_files + processing_jobs
POST   /api/admin/documents            — create a blank document (no file yet)
GET    /api/admin/documents            — list documents (filterable)
GET    /api/admin/documents/{id}       — document detail with all versions
POST   /api/admin/documents/{id}/versions — upload a new version
POST   /api/admin/documents/{id}/reprocess — re-enqueue the current version
POST   /api/admin/documents/{id}/deactivate — soft-delete
POST   /api/admin/documents/{id}/activate   — re-activate
DELETE /api/admin/documents/{id}       — hard-delete (cascades to chunks)
GET    /api/admin/files/{id}           — single file's status + progress
GET    /api/admin/jobs                 — recent processing_jobs (admin view)
GET    /api/admin/stats                — global counters

All routes require an `X-Admin-Token` header that matches
`ADMIN_TOKEN` from the environment (set in .env). For development,
the token defaults to "dev" if not set.

Storage paths MUST be ASCII (Supabase Storage constraint). Arabic
filenames are kept in `document_files.original_filename` and the
storage_path is `<doc_id>/v<n>/<sha256-prefix>.<ext>`.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Optional

from fastapi import APIRouter, File, Form, HTTPException, Header, UploadFile
from pydantic import BaseModel, Field

from bot.config import get_settings
from rag.categories import (
    CATEGORY_TO_BUCKET,
    VALID_CATEGORIES,
    _category_from_path,
)


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/admin", tags=["admin"])


# --- Auth -----------------------------------------------------------------

def _check_admin(x_admin_token: Optional[str]) -> None:
    settings = get_settings()
    expected = os.environ.get("ADMIN_TOKEN", "dev")
    if not x_admin_token or x_admin_token != expected:
        raise HTTPException(401, "invalid or missing X-Admin-Token")


# --- Schemas --------------------------------------------------------------

class DocumentOut(BaseModel):
    id: str
    title: str
    category: str
    bucket: Optional[str] = None
    is_active: bool
    current_version: int
    uploaded_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    latest_status: Optional[str] = None
    latest_progress: dict = Field(default_factory=dict)
    latest_chunk_count: int = 0
    latest_embedding_count: int = 0
    latest_error: Optional[str] = None
    last_processed_at: Optional[str] = None


class FileOut(BaseModel):
    id: str
    document_id: str
    version: int
    storage_bucket: str
    storage_path: str
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    sha256: Optional[str] = None
    original_filename: str
    uploaded_at: Optional[str] = None
    uploaded_by: Optional[str] = None
    status: str
    error_message: Optional[str] = None
    processing_progress: dict = Field(default_factory=dict)
    extracted_char_count: int = 0
    chunk_count: int = 0
    embedding_count: int = 0
    last_processed_at: Optional[str] = None


class JobOut(BaseModel):
    id: str
    document_file_id: str
    status: str
    attempts: int
    max_attempts: int
    scheduled_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    next_retry_at: Optional[str] = None
    error_message: Optional[str] = None
    claimed_by: Optional[str] = None


class StatsOut(BaseModel):
    documents: int
    active_documents: int
    files: int
    indexed_files: int
    pending_files: int
    failed_files: int
    needs_ocr_files: int
    needs_doc_conversion_files: int
    pending_jobs: int
    processing_jobs: int
    dead_jobs: int


# --- Helpers --------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_filename_for_storage(name: str) -> str:
    """Return an ASCII-only filename for use in a Supabase Storage path.

    Strategy: keep the ASCII extension; replace non-ASCII with a
    short hash so paths are always byte-safe. We preserve the
    original (with Arabic) in `original_filename`.
    """
    p = PurePosixPath(name)
    suffix = p.suffix.lower() or ".bin"
    stem = p.stem
    safe_stem = re.sub(r"[^a-zA-Z0-9._-]+", "_", stem).strip("_") or "file"
    if len(safe_stem) > 80:
        safe_stem = safe_stem[:80]
    return f"{safe_stem}{suffix}"


def _doc_to_out(d: dict, latest_file: dict | None) -> DocumentOut:
    return DocumentOut(
        id=d["id"],
        title=d.get("title") or "untitled",
        category=d.get("category") or "other",
        bucket=d.get("bucket"),
        is_active=d.get("is_active", True),
        current_version=d.get("current_version", 1),
        uploaded_by=d.get("uploaded_by"),
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at"),
        latest_status=(latest_file or {}).get("status"),
        latest_progress=(latest_file or {}).get("processing_progress") or {},
        latest_chunk_count=(latest_file or {}).get("chunk_count", 0) or 0,
        latest_embedding_count=(latest_file or {}).get("embedding_count", 0) or 0,
        latest_error=(latest_file or {}).get("error_message"),
        last_processed_at=(latest_file or {}).get("last_processed_at"),
    )


def _file_to_out(f: dict) -> FileOut:
    return FileOut(**{k: f.get(k) for k in FileOut.model_fields.keys()})


def _job_to_out(j: dict) -> JobOut:
    return JobOut(**{k: j.get(k) for k in JobOut.model_fields.keys()})


# --- Routes ---------------------------------------------------------------

@router.get("/health")
async def health():
    """Unauthenticated health check."""
    return {"status": "ok"}


@router.get("/stats", response_model=StatsOut)
async def stats(x_admin_token: Optional[str] = Header(default=None)):
    _check_admin(x_admin_token)
    from supabase import create_client
    s = get_settings()
    sb = create_client(s.supabase_url, s.resolved_service_key())
    docs = sb.table("documents").select("id", count="exact").execute().count or 0
    active = (
        sb.table("documents").select("id", count="exact")
        .eq("is_active", True).execute().count or 0
    )
    files = sb.table("document_files").select("id", count="exact").execute().count or 0
    indexed = (
        sb.table("document_files").select("id", count="exact")
        .eq("status", "indexed").execute().count or 0
    )
    pending = (
        sb.table("document_files").select("id", count="exact")
        .in_("status", ["pending", "processing"]).execute().count or 0
    )
    failed = (
        sb.table("document_files").select("id", count="exact")
        .eq("status", "failed").execute().count or 0
    )
    needs_ocr = (
        sb.table("document_files").select("id", count="exact")
        .eq("status", "needs_ocr").execute().count or 0
    )
    needs_doc = (
        sb.table("document_files").select("id", count="exact")
        .eq("status", "needs_doc_conversion").execute().count or 0
    )
    pending_jobs = (
        sb.table("processing_jobs").select("id", count="exact")
        .eq("status", "pending").execute().count or 0
    )
    processing_jobs = (
        sb.table("processing_jobs").select("id", count="exact")
        .eq("status", "processing").execute().count or 0
    )
    dead_jobs = (
        sb.table("processing_jobs").select("id", count="exact")
        .eq("status", "failed").execute().count or 0
    )
    return StatsOut(
        documents=docs,
        active_documents=active,
        files=files,
        indexed_files=indexed,
        pending_files=pending,
        failed_files=failed,
        needs_ocr_files=needs_ocr,
        needs_doc_conversion_files=needs_doc,
        pending_jobs=pending_jobs,
        processing_jobs=processing_jobs,
        dead_jobs=dead_jobs,
    )


@router.get("/documents", response_model=list[DocumentOut])
async def list_documents(
    category: Optional[str] = None,
    bucket: Optional[str] = None,
    is_active: Optional[bool] = None,
    x_admin_token: Optional[str] = Header(default=None),
):
    _check_admin(x_admin_token)
    from supabase import create_client
    s = get_settings()
    sb = create_client(s.supabase_url, s.resolved_service_key())
    q = sb.table("documents").select("*").order("updated_at", desc=True)
    if category:
        q = q.eq("category", category)
    if bucket:
        q = q.eq("bucket", bucket)
    if is_active is not None:
        q = q.eq("is_active", is_active)
    rows = q.execute().data or []

    # Fetch latest file for each doc in one batch
    doc_ids = [r["id"] for r in rows]
    latest_by_doc: dict[str, dict] = {}
    if doc_ids:
        for d in doc_ids:
            latest = (
                sb.table("document_files").select("*")
                .eq("document_id", d)
                .order("version", desc=True).limit(1).execute().data
            )
            if latest:
                latest_by_doc[d] = latest[0]
    return [_doc_to_out(r, latest_by_doc.get(r["id"])) for r in rows]


@router.get("/documents/{doc_id}", response_model=DocumentOut)
async def get_document(
    doc_id: str, x_admin_token: Optional[str] = Header(default=None)
):
    _check_admin(x_admin_token)
    from supabase import create_client
    s = get_settings()
    sb = create_client(s.supabase_url, s.resolved_service_key())
    d = sb.table("documents").select("*").eq("id", doc_id).limit(1).execute().data
    if not d:
        raise HTTPException(404, "document not found")
    latest = (
        sb.table("document_files").select("*")
        .eq("document_id", doc_id)
        .order("version", desc=True).limit(1).execute().data
    )
    return _doc_to_out(d[0], latest[0] if latest else None)


@router.get("/documents/{doc_id}/files", response_model=list[FileOut])
async def list_document_files(
    doc_id: str, x_admin_token: Optional[str] = Header(default=None)
):
    _check_admin(x_admin_token)
    from supabase import create_client
    s = get_settings()
    sb = create_client(s.supabase_url, s.resolved_service_key())
    files = (
        sb.table("document_files").select("*")
        .eq("document_id", doc_id)
        .order("version", desc=True).execute().data or []
    )
    return [_file_to_out(f) for f in files]


@router.post("/documents/{doc_id}/reprocess")
async def reprocess_document(
    doc_id: str, x_admin_token: Optional[str] = Header(default=None)
):
    """Re-enqueue the current version for processing (worker picks it up).

    This drops ONLY the chunks belonging to the current version's
    file_id — previous versions' chunks stay untouched, so version
    isolation is preserved even mid-reprocessing.
    """
    _check_admin(x_admin_token)
    from supabase import create_client
    s = get_settings()
    sb = create_client(s.supabase_url, s.resolved_service_key())
    d = sb.table("documents").select("id, current_version, is_active").eq(
        "id", doc_id
    ).limit(1).execute().data
    if not d:
        raise HTTPException(404, "document not found")
    if not d[0].get("is_active"):
        raise HTTPException(400, "document is inactive; activate it first")
    ver = d[0].get("current_version", 1)
    f = (
        sb.table("document_files").select("id, status")
        .eq("document_id", doc_id).eq("version", ver).limit(1).execute().data
    )
    if not f:
        raise HTTPException(404, f"version {ver} not found")
    file_id = f[0]["id"]

    # Drop ONLY the current version's chunks (not other versions').
    # The new unique constraint is on (document_file_id, chunk_index),
    # so deleting by file_id is the precise operation.
    sb.table("document_chunks").delete().eq("document_file_id", file_id).execute()

    # Reset the file's status to pending and clear the progress counters
    sb.table("document_files").update({
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
            "updated_at": _now_iso(),
            "reprocess_requested_at": _now_iso(),
        },
    }).eq("id", file_id).execute()

    # Enqueue a new job
    job = (
        sb.table("processing_jobs").insert({
            "document_file_id": file_id,
            "status": "pending",
            "priority": 200,        # higher than normal
        }).execute().data[0]
    )
    return {"ok": True, "job_id": job["id"], "file_id": file_id}


@router.post("/documents/{doc_id}/deactivate")
async def deactivate_document(
    doc_id: str, x_admin_token: Optional[str] = Header(default=None)
):
    _check_admin(x_admin_token)
    from supabase import create_client
    s = get_settings()
    sb = create_client(s.supabase_url, s.resolved_service_key())
    sb.table("documents").update({"is_active": False}).eq("id", doc_id).execute()
    return {"ok": True, "is_active": False}


@router.post("/documents/{doc_id}/activate")
async def activate_document(
    doc_id: str, x_admin_token: Optional[str] = Header(default=None)
):
    _check_admin(x_admin_token)
    from supabase import create_client
    s = get_settings()
    sb = create_client(s.supabase_url, s.resolved_service_key())
    sb.table("documents").update({"is_active": True}).eq("id", doc_id).execute()
    return {"ok": True, "is_active": True}


@router.delete("/documents/{doc_id}")
async def hard_delete_document(
    doc_id: str, x_admin_token: Optional[str] = Header(default=None)
):
    """Hard-delete a document, its files, chunks, and Storage objects.

    The ON DELETE CASCADE on document_files and document_chunks
    cleans up the postgres side. We also remove the files from
    Supabase Storage so the buckets don't grow unbounded.
    """
    _check_admin(x_admin_token)
    from supabase import create_client
    s = get_settings()
    sb = create_client(s.supabase_url, s.resolved_service_key())

    # Collect all storage paths before deleting the rows
    files = (
        sb.table("document_files").select("storage_bucket, storage_path")
        .eq("document_id", doc_id).execute().data or []
    )

    # Delete from postgres (cascades to chunks + processing_jobs)
    sb.table("documents").delete().eq("id", doc_id).execute()

    # Best-effort Storage cleanup
    for f in files:
        try:
            sb.storage.from_(f["storage_bucket"]).remove([f["storage_path"]])
        except Exception as e:  # noqa: BLE001
            logger.warning("storage cleanup failed for %s: %s", f, e)

    return {"ok": True, "deleted_files": len(files)}


@router.post("/upload", response_model=DocumentOut)
async def upload(
    file: UploadFile = File(...),
    title: str = Form(...),
    category: str = Form(...),
    document_id: Optional[str] = Form(None),    # if set: this is a new version
    uploaded_by: Optional[str] = Form(None),
    x_admin_token: Optional[str] = Header(default=None),
):
    """Upload a new document OR a new version of an existing one.

    Behaviour
    ---------
    * If `document_id` is empty: create a new `documents` row + v1
      `document_files` + a `processing_job`.
    * If `document_id` is set: create a new `document_files` with
      `version = current_version + 1`. The new version is queued
      but the document's `current_version` pointer is left alone
      until the worker finishes (no mid-flight version swaps).
    """
    _check_admin(x_admin_token)
    if category not in VALID_CATEGORIES:
        raise HTTPException(400, f"invalid category {category!r}")
    bucket = CATEGORY_TO_BUCKET.get(category, "examples")
    if category not in ("templates", "national_regulations", "internal_policies", "examples"):
        raise HTTPException(400, f"category {category!r} not eligible for upload")

    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(413, "file too large (50MB max)")

    sha = hashlib.sha256(data).hexdigest()
    original_filename = file.filename or "upload.bin"
    mime = file.content_type or "application/octet-stream"
    safe_filename = _safe_filename_for_storage(original_filename)

    from supabase import create_client
    s = get_settings()
    sb = create_client(s.supabase_url, s.resolved_service_key())

    # 1) Create or reuse the document row
    if document_id:
        d = (
            sb.table("documents").select("id, current_version")
            .eq("id", document_id).limit(1).execute().data
        )
        if not d:
            raise HTTPException(404, f"document_id {document_id} not found")
        doc_uuid = d[0]["id"]
        new_version = (d[0].get("current_version") or 0) + 1
    else:
        # Create the document row
        ins = sb.table("documents").insert({
            "title": title,
            "category": category,
            "bucket": bucket,
            "is_active": True,
            "current_version": 1,
            "uploaded_by": uploaded_by,
            "content": "",            # placeholder; chunks live in document_chunks
            "source_uri": f"upload://{original_filename}",
            "processing_progress": {"uploading": True},
        }).execute()
        doc_uuid = ins.data[0]["id"]
        new_version = 1

    # 2) Upload the bytes to Supabase Storage (ASCII-only path)
    storage_path = f"{doc_uuid}/v{new_version}/{safe_filename}"
    sb.storage.from_(bucket).upload(
        storage_path, data,
        {"content-type": mime, "x-upsert": "false"},
    )

    # 3) Create the document_files row
    f = sb.table("document_files").insert({
        "document_id": doc_uuid,
        "version": new_version,
        "storage_bucket": bucket,
        "storage_path": storage_path,
        "mime_type": mime,
        "size_bytes": len(data),
        "sha256": sha,
        "original_filename": original_filename,
        "uploaded_by": uploaded_by,
        "status": "pending",
        "processing_progress": {
            "total_chunks": 0,
            "processed_chunks": 0,
            "failed_chunks": 0,
            "current_batch": 0,
            "retry_count": 0,
            "last_error": None,
            "started_at": None,
            "updated_at": _now_iso(),
        },
    }).execute()
    file_id = f.data[0]["id"]

    # 4) Enqueue the processing job
    job = sb.table("processing_jobs").insert({
        "document_file_id": file_id,
        "status": "pending",
        "priority": 100,
    }).execute()
    job_id = job.data[0]["id"]

    # 5) Return the current document state
    latest = sb.table("document_files").select("*").eq("id", file_id).limit(1).execute().data[0]
    d = sb.table("documents").select("*").eq("id", doc_uuid).limit(1).execute().data[0]
    logger.info(
        "upload: %s v%d → doc=%s file=%s job=%s",
        title, new_version, doc_uuid, file_id, job_id,
    )
    return _doc_to_out(d, latest)


@router.get("/jobs", response_model=list[JobOut])
async def list_jobs(
    status: Optional[str] = None,
    limit: int = 50,
    x_admin_token: Optional[str] = Header(default=None),
):
    _check_admin(x_admin_token)
    from supabase import create_client
    s = get_settings()
    sb = create_client(s.supabase_url, s.resolved_service_key())
    q = sb.table("processing_jobs").select("*").order("scheduled_at", desc=True).limit(limit)
    if status:
        q = q.eq("status", status)
    rows = q.execute().data or []
    return [_job_to_out(j) for j in rows]


@router.get("/files/{file_id}", response_model=FileOut)
async def get_file(
    file_id: str, x_admin_token: Optional[str] = Header(default=None)
):
    _check_admin(x_admin_token)
    from supabase import create_client
    s = get_settings()
    sb = create_client(s.supabase_url, s.resolved_service_key())
    f = (
        sb.table("document_files").select("*")
        .eq("id", file_id).limit(1).execute().data
    )
    if not f:
        raise HTTPException(404, "file not found")
    return _file_to_out(f[0])
