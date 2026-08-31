"""Validate the Kateb index end-to-end.

For every document in the `documents` table, this script checks:
  1.  The DB's `status` field matches the actual data (chars / chunks /
      embeddings) — flags rows that are "indexed" with 0 chunks, etc.
  2.  The document has at least one chunk in `document_chunks`.
  3.  Each chunk has a non-zero embedding of the expected dimension.
  4.  A semantic search using a phrase from the document actually returns
      the document (so we know it's retrievable, not just stored).

Per-file outcomes are written to:
  * `INDEX_VALIDATION.json`
  * `INDEX_VALIDATION.txt` (human-readable)

Exit code: 0 if every `indexed` row passes, 1 if any anomaly is found.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from supabase import create_client  # noqa: E402

from bot.config import get_settings  # noqa: E402
from rag.embeddings import from_env as emb_from_env  # noqa: E402


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("validate_index")


def _status_emoji(s: str) -> str:
    return {
        "indexed": "OK",
        "needs_ocr": "OCR?",
        "needs_doc_conversion": "DOC?",
        "failed": "FAIL",
        "pending": "...",
        "processing": "...",
    }.get(s, s)


def _pick_search_phrase(content: str | None) -> str | None:
    """Pick a short, distinctive Arabic phrase for a search probe.

    We need a phrase that:
      * is non-empty and > 10 chars
      * doesn't include newlines or strange punctuation
      * is from the actual content (not a metadata header)
    """
    if not content:
        return None
    # Strip whitespace and grab a ~50-char chunk from the middle (avoids
    # titles/headers that the chunker might attach repeatedly).
    flat = re.sub(r"\s+", " ", content).strip()
    if len(flat) < 30:
        return flat
    # Skip first 200 chars to avoid title/header repetition
    mid = flat[200:400] if len(flat) > 600 else flat[len(flat) // 2 :]
    # Trim to ~60 chars
    if len(mid) > 60:
        mid = mid[:60]
    return mid


async def _validate_one(supabase, embedder, doc: dict) -> dict:
    """Run the full validation battery on one document."""
    doc_id = doc["id"]
    title = doc["title"]
    status = doc.get("status", "unknown")
    expected_chunks = doc.get("chunk_count", 0) or 0
    expected_embeds = doc.get("embedding_count", 0) or 0

    result = {
        "id": doc_id,
        "title": title,
        "category": doc.get("category"),
        "status": status,
        "extracted_char_count": doc.get("extracted_char_count", 0) or 0,
        "expected_chunk_count": expected_chunks,
        "expected_embedding_count": expected_embeds,
        "checks": {},
        "verdict": "pass",
    }

    # Short-circuit for non-indexed docs
    if status != "indexed":
        result["checks"]["status"] = (
            f"not indexed (status={status}); chunk/embedding checks skipped"
        )
        result["verdict"] = "skipped"
        return result

    # 1. Re-count chunks + embeddings
    try:
        chunks_resp = (
            supabase.table("document_chunks")
            .select("id, chunk_index, embedding, content")
            .eq("document_id", doc_id)
            .execute()
        )
        chunks = chunks_resp.data or []
    except Exception as e:  # noqa: BLE001
        result["checks"]["chunks"] = f"DB error: {e}"
        result["verdict"] = "fail"
        return result

    n_chunks = len(chunks)
    n_with_emb = sum(1 for c in chunks if c.get("embedding"))
    n_zero_emb = 0
    for c in chunks:
        emb = c.get("embedding")
        if not emb:
            continue
        # PostgREST may return the vector as a string like "[0.1,0.2,...]"
        if isinstance(emb, str):
            try:
                emb = [float(x) for x in emb.strip("[]").split(",") if x.strip()]
            except ValueError:
                continue
        # A TRUE placeholder has ALL components near zero. Real 1024-dim
        # embeddings can have a handful of components very close to 0
        # (especially after L2 normalisation), so we test `all` not `any`.
        if emb and all(abs(x) < 1e-9 for x in emb):
            n_zero_emb += 1

    result["actual_chunk_count"] = n_chunks
    result["actual_chunks_with_embedding"] = n_with_emb
    result["actual_zero_embeddings"] = n_zero_emb

    if n_chunks == 0:
        result["checks"]["chunks"] = "0 chunks in document_chunks"
        result["verdict"] = "fail"
    elif n_chunks != expected_chunks:
        result["checks"]["chunks"] = (
            f"expected {expected_chunks} chunks per DB column, found {n_chunks}"
        )
        result["verdict"] = "fail"

    if n_with_emb != n_chunks:
        result["checks"]["embeddings"] = (
            f"{n_chunks - n_with_emb} chunks have no embedding"
        )
        result["verdict"] = "fail"
    elif n_with_emb != expected_embeds:
        result["checks"]["embeddings"] = (
            f"expected {expected_embeds} embeddings, found {n_with_emb}"
        )
        result["verdict"] = "fail"

    if n_zero_emb:
        result["checks"]["zero_embeddings"] = (
            f"{n_zero_emb} chunks have a zero/placeholder embedding — search will skip these"
        )
        result["verdict"] = "fail"

    # 2. Embedding dimension check
    if chunks and chunks[0].get("embedding"):
        first_emb = chunks[0]["embedding"]
        if isinstance(first_emb, str):
            first_emb = [float(x) for x in first_emb.strip("[]").split(",") if x.strip()]
        dim = len(first_emb)
        result["embedding_dim"] = dim
        if dim != embedder.dimension:
            result["checks"]["dim"] = (
                f"chunk dim {dim} != configured {embedder.dimension}"
            )
            result["verdict"] = "fail"

    # 3. Searchability probe
    phrase = _pick_search_phrase(doc.get("content"))
    if not phrase or len(phrase.strip()) < 15:
        result["checks"]["search"] = "no usable probe phrase in content"
        if result["verdict"] != "fail":
            result["verdict"] = "warn"
        return result

    try:
        vec = await embedder.embed_text(phrase)
    except Exception as e:  # noqa: BLE001
        result["checks"]["search"] = f"embed probe failed: {e}"
        result["verdict"] = "fail"
        return result

    try:
        # Use a low threshold so we get a hit if the doc is anywhere
        # in the embedding space.
        rpc = supabase.rpc(
            "match_documents",
            {
                "query_embedding": vec,
                "match_threshold": 0.0,  # we only care about "did we hit this doc?"
                "match_count": 50,
                "filter_category": None,
            },
        ).execute()
        hits = rpc.data or []
        our_doc_hits = [h for h in hits if h.get("document_id") == doc_id]
        result["search_hits_for_phrase"] = len(our_doc_hits)
        if not our_doc_hits:
            # Try category filter
            rpc2 = supabase.rpc(
                "match_documents",
                {
                    "query_embedding": vec,
                    "match_threshold": 0.0,
                    "match_count": 50,
                    "filter_category": doc.get("category"),
                },
            ).execute()
            hits2 = rpc2.data or []
            our_doc_hits2 = [h for h in hits2 if h.get("document_id") == doc_id]
            if our_doc_hits2:
                result["search_hits_for_phrase"] = len(our_doc_hits2)
                result["checks"]["search"] = (
                    f"OK (found in {len(our_doc_hits2)} chunks within category; "
                    f"top similarity={our_doc_hits2[0].get('similarity'):.3f})"
                )
            else:
                result["checks"]["search"] = (
                    f"NOT FOUND — phrase {phrase!r} did not retrieve this document"
                )
                result["verdict"] = "fail"
        else:
            result["checks"]["search"] = (
                f"OK ({len(our_doc_hits)} chunks matched; "
                f"top similarity={our_doc_hits[0].get('similarity'):.3f})"
            )
    except Exception as e:  # noqa: BLE001
        result["checks"]["search"] = f"RPC failed: {e}"
        result["verdict"] = "fail"

    return result


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    supabase = create_client(settings.supabase_url, settings.resolved_service_key())
    embedder = emb_from_env()

    log.info(
        "Supabase: %s | embedder: %s/%s (dim=%d)",
        settings.supabase_url, embedder.provider, embedder.model, embedder.dimension,
    )

    # Fetch all documents
    docs_resp = (
        supabase.table("documents")
        .select(
            "id, title, category, status, content, chunk_count, embedding_count, "
            "extracted_char_count, error_message, source_path, mime_type"
        )
        .order("title")
        .execute()
    )
    docs = docs_resp.data or []
    log.info("Validating %d documents...", len(docs))

    results: list[dict] = []
    for i, d in enumerate(docs, 1):
        r = await _validate_one(supabase, embedder, d)
        results.append(r)
        marker = {"pass": "✓", "fail": "✗", "warn": "!", "skipped": "·"}.get(
            r["verdict"], "?"
        )
        log.info(
            "  [%s] %3d/%d %s  %s  (chunks=%s embeds=%s)",
            marker, i, len(docs), _status_emoji(r["status"]),
            r["title"][:50],
            r.get("actual_chunk_count", "?"),
            r.get("actual_chunks_with_embedding", "?"),
        )

    # Aggregate
    n = len(results)
    n_pass = sum(1 for r in results if r["verdict"] == "pass")
    n_fail = sum(1 for r in results if r["verdict"] == "fail")
    n_warn = sum(1 for r in results if r["verdict"] == "warn")
    n_skip = sum(1 for r in results if r["verdict"] == "skipped")
    log.info(
        "Validation: %d total | %d pass | %d warn | %d fail | %d skipped",
        n, n_pass, n_warn, n_fail, n_skip,
    )

    # Write reports
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "INDEX_VALIDATION.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("Kateb Index Validation Report")
    lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 72)
    lines.append("")
    lines.append(f"Total: {n} | pass: {n_pass} | warn: {n_warn} | fail: {n_fail} | skipped: {n_skip}")
    lines.append("")

    # Per-category status counts
    by_cat: dict[str, dict[str, int]] = {}
    for r in results:
        cat = r.get("category") or "?"
        ver = r["verdict"]
        by_cat.setdefault(cat, {}).setdefault(ver, 0)
        by_cat[cat][ver] += 1
    lines.append("Per-category:")
    for cat in sorted(by_cat):
        d = by_cat[cat]
        lines.append(
            f"  {cat:<22} pass={d.get('pass', 0):>3}  fail={d.get('fail', 0):>3}  "
            f"warn={d.get('warn', 0):>3}  skipped={d.get('skipped', 0):>3}"
        )
    lines.append("")

    lines.append("=" * 72)
    lines.append("Per-file details")
    lines.append("=" * 72)
    lines.append(
        f"{'verdict':<8} {'status':<22} {'category':<22} {'chunks':>6} {'embeds':>6}  title"
    )
    lines.append("-" * 110)
    for r in results:
        title = r["title"]
        if len(title) > 60:
            title = title[:57] + "..."
        lines.append(
            f"{r['verdict']:<8} {r['status']:<22} {(r.get('category') or '?'):<22} "
            f"{r.get('actual_chunk_count', '-'):>6} "
            f"{r.get('actual_chunks_with_embedding', '-'):>6}  {title}"
        )

    # Failures detail
    fails = [r for r in results if r["verdict"] == "fail"]
    if fails:
        lines.append("")
        lines.append("=" * 72)
        lines.append(f"Failures ({len(fails)})")
        lines.append("=" * 72)
        for r in fails:
            lines.append(f"\n[{r['status']}] {r['title']}")
            for k, v in r.get("checks", {}).items():
                lines.append(f"    ✗ {k}: {v}")

    (out_dir / "INDEX_VALIDATION.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8",
    )
    log.info("Wrote %s/INDEX_VALIDATION.json and .txt", out_dir)

    return 0 if n_fail == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate the Kateb index end-to-end.",
    )
    parser.add_argument(
        "--out-dir", default=str(ROOT),
        help="Where to write INDEX_VALIDATION.json / .txt (default: project root).",
    )
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
