"""Resumable ingestion for a single file — designed for very large docs.

Why this exists
===============
The OpenRouter free tier rate-limits embedding requests to ~20/min. A
file that produces 1000+ chunks (e.g. the 1.5MB
`نماذج-الموارد-البشرية.docx` with 2700 chunks) would need 135+
embedding calls in one run, which blows past the limit and 429-loops
the whole ingest.

This script does the same work as `ingest_local.py` for ONE file, but
processes it in batches of `BATCH_CHUNKS` chunks at a time, saving
the partial result to Supabase between batches. If the run fails or
is interrupted, the next invocation resumes from where it left off
(skipping chunks already in `document_chunks`).

Usage
=====
    python scripts/ingest_resumable.py reference_docs/internal_policies/نماذج-الموارد-البشرية.docx
    python scripts/ingest_resumable.py <file> --batch 300
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

from supabase import create_client  # noqa: E402
from bot.config import get_settings  # noqa: E402
from rag.categories import _category_from_path  # noqa: E402
from rag.embeddings import from_env as emb_from_env  # noqa: E402
from rag.loaders import load_any  # noqa: E402
from rag.chunker import chunk_document  # noqa: E402


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("ingest_resumable")


# Embedding batch per HTTP call. OpenRouter is fine with 20.
EMBED_BATCH = 20
# Sleep between embed calls (seconds). OpenRouter free tier is ~20
# req/min, so 3.0s between calls keeps us safely under.
BATCH_SLEEP_S = 3.0
# How many chunks to process per "session" before persisting and
# exiting. The next invocation resumes from the saved offset.
CHUNKS_PER_SESSION = 200


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    supabase = create_client(settings.supabase_url, settings.resolved_service_key())
    embedder = emb_from_env()
    log.info(
        "Supabase: %s | embedder: %s/%s (dim=%d)",
        settings.supabase_url, embedder.provider, embedder.model, embedder.dimension,
    )

    file_path = Path(args.file).resolve()
    if not file_path.exists():
        log.error("File not found: %s", file_path)
        return 1
    rel = file_path.relative_to(ROOT)
    source_uri = f"local://{rel.as_posix()}"
    category = _category_from_path(str(rel))
    log.info("→ %s  [%s, %d bytes]", rel, category, file_path.stat().st_size)

    # Load + chunk
    loaded = load_any(file_path)
    if not loaded.ok:
        log.error("Loader failed: %s", loaded.error_message)
        # Persist the doc as failed/needs_* so the report reflects it
        from rag.ingest import ingest_loaded_doc  # local import
        await ingest_loaded_doc(
            supabase, embedder, loaded,
            category=category,
            source_uri=source_uri,
            source_path=str(file_path),
        )
        return 1

    chunks, stats = chunk_document(loaded.text)
    log.info("Chunker: %d blocks → %d chunks (max %d tokens, %d total tokens)",
             stats.num_blocks, stats.num_chunks, stats.max_tokens, stats.total_tokens)
    if not chunks:
        log.error("Chunker produced 0 chunks")
        return 1

    # Upsert the document row (creates if missing, updates if present)
    title = file_path.stem
    doc_payload = {
        "title": title,
        "category": category,
        "source_uri": source_uri,
        "source_path": str(file_path),
        "mime_type": loaded.mime_type,
        "metadata": {"extractor": loaded.extractor, "size_bytes": file_path.stat().st_size},
        "status": "processing",
        "error_message": None,
        "extracted_char_count": len(loaded.text),
        "content_hash": __import__("hashlib").sha256(
            loaded.text.encode("utf-8", errors="replace")
        ).hexdigest()[:32],
        "content": loaded.text,
        "last_processed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    existing = (
        supabase.table("documents")
        .select("id")
        .eq("source_uri", source_uri)
        .limit(1)
        .execute()
        .data
    )
    if existing:
        doc_id = existing[0]["id"]
        supabase.table("documents").update(doc_payload).eq("id", doc_id).execute()
        log.info("Reusing existing doc %s", doc_id)
    else:
        ins = supabase.table("documents").insert(doc_payload).execute()
        doc_id = ins.data[0]["id"]
        log.info("Created new doc %s", doc_id)

    # Find already-saved chunk indexes so we can resume
    saved = (
        supabase.table("document_chunks")
        .select("chunk_index")
        .eq("document_id", doc_id)
        .execute()
        .data
    )
    done = {r["chunk_index"] for r in saved}
    log.info("Already have %d chunks in DB; resuming from there", len(done))

    # Plan the chunks to embed this run
    todo = [i for i in range(len(chunks)) if i not in done]
    log.info("Need to embed %d more chunks (this session limit: %d)",
             len(todo), args.batch)
    todo = todo[: args.batch]
    if not todo:
        log.info("Nothing to do — all chunks already saved.")
        # Mark indexed with the correct totals
        supabase.table("documents").update({
            "status": "indexed",
            "chunk_count": len(chunks),
            "embedding_count": len(chunks),
        }).eq("id", doc_id).execute()
        return 0

    # Embed in small groups with sleeps
    total_embedded_this_run = 0
    started_at = time.perf_counter()
    for off in range(0, len(todo), EMBED_BATCH):
        batch_idx = todo[off : off + EMBED_BATCH]
        batch_texts = [chunks[i] for i in batch_idx]
        log.info("Embedding %d chunks (%d/%d of this session)...",
                 len(batch_idx), off + len(batch_idx), len(todo))
        vecs = await embedder.embed_texts(batch_texts)
        if len(vecs) != len(batch_idx):
            log.error("Vector count mismatch: %d vecs for %d chunks",
                      len(vecs), len(batch_idx))
            return 1
        rows = [
            {
                "document_id": doc_id,
                "chunk_index": i,
                "content": chunks[i],
                "embedding": vec,
            }
            for i, vec in zip(batch_idx, vecs)
        ]
        supabase.table("document_chunks").insert(rows).execute()
        total_embedded_this_run += len(rows)
        if off + EMBED_BATCH < len(todo):
            await asyncio.sleep(BATCH_SLEEP_S)

    elapsed = time.perf_counter() - started_at
    log.info("Embedded %d chunks in %.1fs", total_embedded_this_run, elapsed)

    # Recount + decide status
    saved_now = (
        supabase.table("document_chunks")
        .select("id", count="exact")
        .eq("document_id", doc_id)
        .execute()
    )
    n_chunks_saved = saved_now.count or 0
    supabase.table("documents").update({
        "chunk_count": n_chunks_saved,
        "embedding_count": n_chunks_saved,
    }).eq("id", doc_id).execute()

    if n_chunks_saved >= len(chunks):
        log.info("✓ ALL %d chunks saved — doc is now fully indexed.", n_chunks_saved)
        supabase.table("documents").update({"status": "indexed"}).eq("id", doc_id).execute()
    else:
        log.info(
            "Session complete: %d/%d chunks saved. Re-run the same command "
            "to continue from chunk %d.",
            n_chunks_saved, len(chunks), n_chunks_saved,
        )
        supabase.table("documents").update({"status": "processing"}).eq("id", doc_id).execute()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resumable single-file ingestion for very large docs.",
    )
    parser.add_argument("file", help="Path to the file (relative to project root or absolute).")
    parser.add_argument(
        "--batch", type=int, default=CHUNKS_PER_SESSION,
        help=f"Max chunks to embed per session (default: {CHUNKS_PER_SESSION}).",
    )
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())

