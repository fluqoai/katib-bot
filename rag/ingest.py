"""Single source of truth for adding a document to the index.

Used by:
- `scripts/ingest_local.py` (folder walker, recommended path)
- `scripts/add_doc.py` (one-off developer path)
- `drive/sync.py` (optional Google-Drive path)

The function understands the full status taxonomy:
  * `ok`             — text was extracted, chunked, embedded, saved
  * `needs_ocr`      — scanned PDF that needs Tesseract (not installed)
  * `needs_doc_conversion` — old .doc that needs LibreOffice (not installed)
  * `empty` / `error`— nothing useful was extracted

The DB row's `status` column mirrors the loader's outcome, so the
validation script can classify each file without re-running extraction.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Any

from supabase import Client

from rag.chunker import chunk_document
from rag.embeddings import EmbeddingClient
from rag.loaders import LoadedDoc
from rag.loaders.types import LoaderStatus


logger = logging.getLogger(__name__)


# Categories the skill requires, plus 'other' as a catch-all.
VALID_CATEGORIES: frozenset[str] = frozenset({
    "templates",
    "national_regulations",
    "internal_policies",
    "examples",
    "other",
})


# Map loader status → DB status
_LOADER_TO_DB_STATUS: dict[str, str] = {
    "ok": "indexed",
    "needs_ocr": "needs_ocr",
    "needs_doc_conversion": "needs_doc_conversion",
    "empty": "failed",
    "error": "failed",
}


class IngestError(ValueError):
    """Raised when a document is rejected (bad category, etc.)."""


def _content_hash(text: str) -> str:
    """Stable hash of the extracted text — used for dedup."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:32]


def _derive_title(source_path: str, fallback: str = "") -> str:
    """Use the file's stem as a clean title, falling back to a hint.

    Handles both Windows-style paths (with backslashes) and POSIX paths.
    """
    from pathlib import PurePosixPath
    # Normalise Windows backslashes to forward slashes so PurePosixPath
    # parses all components correctly.
    norm = source_path.replace("\\", "/")
    stem = PurePosixPath(norm).stem
    return stem or fallback


async def ingest_loaded_doc(
    supabase: Client,
    embedder: EmbeddingClient,
    loaded: LoadedDoc,
    *,
    title: str | None = None,
    category: str,
    source_uri: str,
    source_path: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ingest a `LoadedDoc` produced by the loader package.

    Populates the full status taxonomy in the DB. Returns a diagnostic
    dict so the caller can build a per-file report.

    Returns: {
      "action": "inserted" | "updated" | "skipped",
      "document_id": str | None,
      "status": "indexed" | "needs_ocr" | "needs_doc_conversion" | "failed",
      "extracted_char_count": int,
      "chunk_count": int,
      "embedding_count": int,
      "content_hash": str | None,
      "mime_type": str | None,
      "extractor": str | None,
      "error_message": str | None,
    }
    """
    if category not in VALID_CATEGORIES:
        raise IngestError(
            f"Invalid category {category!r}. "
            f"Must be one of: {sorted(VALID_CATEGORIES)}"
        )

    db_status = _LOADER_TO_DB_STATUS.get(loaded.status, "failed")
    err = loaded.error_message if loaded.status != "ok" else None
    char_count = len(loaded.text) if loaded.text else 0
    title = (title or _derive_title(source_path)).strip()

    base_payload = {
        "title": title,
        "category": category,
        "source_uri": source_uri,
        "source_path": source_path,
        "mime_type": loaded.mime_type,
        "metadata": {**(metadata or {}), "extractor": loaded.extractor},
        "status": db_status,
        "error_message": err,
        "extracted_char_count": char_count,
        "chunk_count": 0,
        "embedding_count": 0,
        "last_processed_at": _now_iso(),
    }

    if not loaded.ok:
        # Non-fatal outcome: still record the doc so the user can see it in
        # the validation report. NO chunks, NO embeddings.
        return await _upsert_doc_only(
            supabase, base_payload, source_uri,
            action_label="skipped",
        )

    # Compute content hash for dedup
    base_payload["content_hash"] = _content_hash(loaded.text)
    # Also store the full text in `content` (the table already has this column)
    base_payload["content"] = loaded.text

    # Chunk + embed
    try:
        chunks, stats = chunk_document(loaded.text)
    except Exception as e:  # noqa: BLE001
        logger.exception("Chunker crashed for %s", source_uri)
        base_payload.update({
            "status": "failed",
            "error_message": f"chunker: {type(e).__name__}: {e}",
        })
        return await _upsert_doc_only(
            supabase, base_payload, source_uri, action_label="skipped",
        )

    if not chunks:
        base_payload.update({
            "status": "failed",
            "error_message": "chunker produced 0 chunks (text too short?)",
        })
        return await _upsert_doc_only(
            supabase, base_payload, source_uri, action_label="skipped",
        )

    try:
        vectors = await embedder.embed_texts(chunks)
    except Exception as e:  # noqa: BLE001
        logger.exception("Embedding failed for %s", source_uri)
        base_payload.update({
            "status": "failed",
            "error_message": f"embedding: {type(e).__name__}: {e}",
            "chunk_count": len(chunks),
        })
        return await _upsert_doc_only(
            supabase, base_payload, source_uri, action_label="skipped",
        )

    if len(vectors) != len(chunks):
        # Defensive: the embedder should return one vector per chunk
        base_payload.update({
            "status": "failed",
            "error_message": (
                f"embedder returned {len(vectors)} vectors for {len(chunks)} chunks"
            ),
            "chunk_count": len(chunks),
        })
        return await _upsert_doc_only(
            supabase, base_payload, source_uri, action_label="skipped",
        )

    # Persist document + chunks
    base_payload.update({
        "chunk_count": len(chunks),
        "embedding_count": len(vectors),
    })

    # Upsert document row
    doc_id, action = await _upsert_document(supabase, base_payload, source_uri)

    # Replace chunks (delete + insert) on update; on insert just insert.
    if action == "updated":
        await asyncio.to_thread(
            lambda: supabase.table("document_chunks").delete()
            .eq("document_id", doc_id).execute()
        )

    chunk_rows = [
        {
            "document_id": doc_id,
            "chunk_index": i,
            "content": c,
            "embedding": vec,
        }
        for i, (c, vec) in enumerate(zip(chunks, vectors))
    ]
    # Insert in batches of 50 to stay under the REST row limit
    BATCH = 50
    for off in range(0, len(chunk_rows), BATCH):
        await asyncio.to_thread(
            lambda rows=chunk_rows[off : off + BATCH]: supabase.table(
                "document_chunks"
            ).insert(rows).execute()
        )

    logger.info(
        "ingest_loaded_doc: %s %r → %d chunks (id=%s, %d tokens max)",
        action, title, len(chunk_rows), doc_id, stats.max_tokens,
    )
    return {
        "action": action,
        "document_id": doc_id,
        "status": "indexed",
        "extracted_char_count": char_count,
        "chunk_count": len(chunks),
        "embedding_count": len(vectors),
        "content_hash": base_payload["content_hash"],
        "mime_type": loaded.mime_type,
        "extractor": loaded.extractor,
        "error_message": None,
        "max_chunk_tokens": stats.max_tokens,
    }


async def _upsert_document(
    supabase: Client, payload: dict[str, Any], source_uri: str
) -> tuple[str, str]:
    """Insert or update the document row. Returns (doc_id, action)."""
    existing = await asyncio.to_thread(
        lambda: supabase.table("documents")
        .select("id")
        .eq("source_uri", source_uri)
        .limit(1)
        .execute()
    )
    if existing.data:
        doc_id = existing.data[0]["id"]
        await asyncio.to_thread(
            lambda: supabase.table("documents").update(payload)
            .eq("id", doc_id).execute()
        )
        return doc_id, "updated"
    inserted = await asyncio.to_thread(
        lambda: supabase.table("documents").insert(payload).execute()
    )
    return inserted.data[0]["id"], "inserted"


async def _upsert_doc_only(
    supabase: Client, payload: dict[str, Any], source_uri: str,
    *, action_label: str,
) -> dict[str, Any]:
    """Persist a no-chunks document row (for failed/needs_*/empty)."""
    # The DB has `content text not null`. For failed/needs_* rows we have
    # no extracted text, so we store an empty string (not NULL) so the
    # INSERT doesn't violate the constraint. Future re-ingests will
    # overwrite this once the file is successfully processed.
    payload["content"] = payload.get("content") or ""
    try:
        doc_id, action = await _upsert_document(supabase, payload, source_uri)
        return {
            "action": action,
            "document_id": doc_id,
            "status": payload["status"],
            "extracted_char_count": payload.get("extracted_char_count", 0),
            "chunk_count": 0,
            "embedding_count": 0,
            "content_hash": payload.get("content_hash"),
            "mime_type": payload.get("mime_type"),
            "extractor": payload.get("metadata", {}).get("extractor"),
            "error_message": payload.get("error_message"),
        }
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to persist no-chunks doc for %s", source_uri)
        return {
            "action": action_label,
            "document_id": None,
            "status": "failed",
            "extracted_char_count": payload.get("extracted_char_count", 0),
            "chunk_count": 0,
            "embedding_count": 0,
            "content_hash": None,
            "mime_type": payload.get("mime_type"),
            "extractor": payload.get("metadata", {}).get("extractor"),
            "error_message": f"db: {type(e).__name__}: {e}",
        }


def _now_iso() -> str:
    """ISO 8601 UTC timestamp (no microseconds, with 'Z')."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- legacy wrapper --------------------------------------------------------
# `ingest_document` was the old API (took a `content: str` directly). Keep it
# for back-compat with `drive/sync.py` and `scripts/add_doc.py` so callers
# don't break. New code should use `ingest_loaded_doc`.

async def ingest_document(
    supabase: Client,
    embedder: EmbeddingClient,
    *,
    title: str,
    content: str,
    category: str,
    source_uri: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper: synthesises a LoadedDoc from raw text."""
    loaded = LoadedDoc(
        text=content or "",
        status="ok" if (content and content.strip()) else "empty",
        mime_type=metadata.get("mime_type") if metadata else None,
        error_message=None,
        extractor="text-direct",
    )
    return await ingest_loaded_doc(
        supabase, embedder, loaded,
        title=title,
        category=category,
        source_uri=source_uri,
        source_path=metadata.get("source_path", source_uri) if metadata else source_uri,
        metadata=metadata,
    )


async def delete_document(supabase: Client, document_id: str) -> bool:
    """Delete a document by id. Returns True if something was deleted."""
    resp = await asyncio.to_thread(
        lambda: supabase.table("documents")
        .delete().eq("id", document_id).execute()
    )
    return bool(resp.data)


async def list_documents(
    supabase: Client,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """List documents, optionally filtered by category. Newest first."""
    q = supabase.table("documents").select(
        "id, title, category, source_uri, created_at, updated_at, metadata, "
        "status, chunk_count, embedding_count, extracted_char_count, error_message"
    )
    if category:
        q = q.eq("category", category)
    resp = await asyncio.to_thread(lambda: q.order("created_at", desc=True).execute())
    return resp.data or []


__all__ = [
    "VALID_CATEGORIES",
    "IngestError",
    "ingest_document",
    "ingest_loaded_doc",
    "delete_document",
    "list_documents",
]
