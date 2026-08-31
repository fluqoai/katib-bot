"""Ingest a local folder of reference documents into the Kateb index.

This is the **recommended** way to populate the corpus. The folder
structure maps to categories — subfolder names like `templates`,
`نماذج`, `لوائح`, `سياسات`, etc. are auto-detected.

Usage:
    # Whole folder (categories inferred from subfolder names)
    python scripts/ingest_local.py reference_docs

    # Force one category for everything
    python scripts/ingest_local.py reference_docs/templates --category templates

    # Only ingest files whose name contains "لائحة"
    python scripts/ingest_local.py reference_docs --filter لائحة

Pipeline
========
For every supported file (`.md`, `.txt`, `.docx`, `.doc`, `.pdf`):
  1.  Extract text via the appropriate loader (handles text-PDF vs
      scanned-PDF vs old-`.doc` gracefully).
  2.  Chunk with a token-aware, paragraph/heading-aware splitter
      (`rag.chunker`).
  3.  Embed each chunk via the configured provider.
  4.  Upsert the document row + chunks into Supabase.

Per-file outcomes are recorded in the DB's `status` column:
  * `indexed`              — fully ingested, search-ready
  * `needs_ocr`            — scanned PDF, Tesseract not installed
  * `needs_doc_conversion` — old .doc, LibreOffice not installed
  * `failed`               — anything else (with `error_message` filled in)

A summary report is written to the workspace root:
  * `INDEX_STATUS.json`  — machine-readable per-file outcomes
  * `INDEX_STATUS.txt`   — human-readable summary (UTF-8, opens in Notepad)

The script is idempotent on `source_uri` (= `local://<relative-path>`).
Re-running for an unchanged folder replaces the document and its chunks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
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
from rag.ingest import IngestError, ingest_loaded_doc  # noqa: E402
from rag.loaders import SUPPORTED_EXTS, load_any  # noqa: E402


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("ingest_local")


# -- the worker ------------------------------------------------------------

async def _ingest_one(
    file_path: Path,
    rel_path: Path,
    *,
    supabase,
    embedder,
    force_category: str | None,
) -> dict:
    """Ingest a single file; always returns a result dict (never raises)."""
    start = time.perf_counter()
    category = force_category or _category_from_path(str(rel_path))
    source_uri = f"local://{rel_path.as_posix()}"
    file_size = file_path.stat().st_size

    log.info("→ %s  [%s, %d bytes]", rel_path, category, file_size)
    try:
        loaded = load_any(file_path)
    except Exception as e:  # noqa: BLE001
        log.exception("Loader crashed for %s", file_path.name)
        return {
            "filename": file_path.name,
            "rel_path": str(rel_path),
            "category": category,
            "source_uri": source_uri,
            "status": "failed",
            "extracted_char_count": 0,
            "chunk_count": 0,
            "embedding_count": 0,
            "content_hash": None,
            "mime_type": None,
            "extractor": None,
            "error_message": f"loader crashed: {type(e).__name__}: {e}",
            "elapsed_s": round(time.perf_counter() - start, 2),
            "size_bytes": file_size,
        }

    if not loaded.ok:
        log.warning(
            "  ! %s → %s (%d chars): %s",
            file_path.name, loaded.status, len(loaded.text or ""),
            (loaded.error_message or "").splitlines()[0][:120] if loaded.error_message else "",
        )
        # Persist the row so it appears in the validation report.
        try:
            r = await ingest_loaded_doc(
                supabase, embedder, loaded,
                title=None,
                category=category,
                source_uri=source_uri,
                source_path=str(file_path),
                metadata={"size_bytes": file_size, "extractor": loaded.extractor},
            )
        except IngestError as e:
            log.error("  ! ingest refused %s: %s", file_path.name, e)
            r = {
                "action": "skipped",
                "status": "failed",
                "extracted_char_count": 0,
                "chunk_count": 0,
                "embedding_count": 0,
                "content_hash": None,
                "mime_type": loaded.mime_type,
                "extractor": loaded.extractor,
                "error_message": str(e),
                "document_id": None,
            }
        return {
            "filename": file_path.name,
            "rel_path": str(rel_path),
            "category": category,
            "source_uri": source_uri,
            **r,
            "elapsed_s": round(time.perf_counter() - start, 2),
            "size_bytes": file_size,
        }

    # Happy path
    try:
        r = await ingest_loaded_doc(
            supabase, embedder, loaded,
            title=None,
            category=category,
            source_uri=source_uri,
            source_path=str(file_path),
            metadata={"size_bytes": file_size, "extractor": loaded.extractor},
        )
    except IngestError as e:
        log.error("  ! ingest refused %s: %s", file_path.name, e)
        r = {
            "action": "skipped",
            "status": "failed",
            "extracted_char_count": len(loaded.text),
            "chunk_count": 0,
            "embedding_count": 0,
            "content_hash": None,
            "mime_type": loaded.mime_type,
            "extractor": loaded.extractor,
            "error_message": str(e),
            "document_id": None,
        }
    except Exception as e:  # noqa: BLE001
        log.exception("  ! ingest crashed for %s", file_path.name)
        r = {
            "action": "skipped",
            "status": "failed",
            "extracted_char_count": len(loaded.text),
            "chunk_count": 0,
            "embedding_count": 0,
            "content_hash": None,
            "mime_type": loaded.mime_type,
            "extractor": loaded.extractor,
            "error_message": f"ingest crashed: {type(e).__name__}: {e}",
            "document_id": None,
        }

    log.info(
        "  ✓ %s → %s | %d chars → %d chunks, %d embeddings (%.1fs)",
        file_path.name, r.get("status"),
        r.get("extracted_char_count", 0),
        r.get("chunk_count", 0),
        r.get("embedding_count", 0),
        time.perf_counter() - start,
    )
    return {
        "filename": file_path.name,
        "rel_path": str(rel_path),
        "category": category,
        "source_uri": source_uri,
        **r,
        "elapsed_s": round(time.perf_counter() - start, 2),
        "size_bytes": file_size,
    }


async def walk_and_ingest(
    root: Path,
    force_category: str | None = None,
    file_filter: str | None = None,
) -> list[dict]:
    """Walk `root`, ingest every supported file. Returns per-file results."""
    settings = get_settings()
    supabase = create_client(settings.supabase_url, settings.resolved_service_key())
    embedder = emb_from_env()
    log.info(
        "Supabase: %s | embedder: %s/%s (dim=%d)",
        settings.supabase_url, embedder.provider, embedder.model, embedder.dimension,
    )

    if not root.exists():
        log.error("Path does not exist: %s", root)
        return []

    all_files = [
        p for p in root.rglob("*")
        if p.is_file()
        and p.suffix.lower() in SUPPORTED_EXTS
        and "__pycache__" not in p.parts
        and not any(part.startswith(".") for part in p.parts)
    ]
    if file_filter:
        all_files = [p for p in all_files if file_filter in p.name]

    log.info("Found %d supported file(s) under %s", len(all_files), root)

    results: list[dict] = []
    for f in sorted(all_files):
        try:
            rel = f.relative_to(root if root.is_dir() else root.parent)
        except ValueError:
            rel = Path(f.name)
        results.append(
            await _ingest_one(
                f, rel,
                supabase=supabase, embedder=embedder,
                force_category=force_category,
            )
        )

    # Summary
    n = len(results)
    by_status: dict[str, int] = {}
    for r in results:
        s = r.get("status") or "unknown"
        by_status[s] = by_status.get(s, 0) + 1
    log.info("Done. total=%d  by_status=%s", n, by_status)
    return results


# -- CLI -------------------------------------------------------------------

def write_reports(results: list[dict], out_dir: Path) -> None:
    """Write the JSON + TXT report files in the workspace root."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "INDEX_STATUS.json"
    txt_path = out_dir / "INDEX_STATUS.txt"

    json_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Human-readable summary
    total = len(results)
    by_status: dict[str, list[dict]] = {}
    for r in results:
        by_status.setdefault(r.get("status", "unknown"), []).append(r)

    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("Kateb Ingestion Report")
    lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 72)
    lines.append("")
    lines.append(f"TOTAL SOURCE FILES: {total}")
    lines.append("")

    by_cat: dict[str, dict[str, int]] = {}
    for r in results:
        by_cat.setdefault(r["category"], {}).setdefault(
            r.get("status", "unknown"), 0
        )
        by_cat[r["category"]][r.get("status", "unknown")] += 1

    lines.append("Per-status totals:")
    for s, lst in sorted(by_status.items()):
        lines.append(f"  {s:<22}  {len(lst):>3}")
    lines.append("")

    lines.append("Per-category breakdown:")
    for cat in sorted(by_cat):
        lines.append(f"  {cat}:")
        for s, n in sorted(by_cat[cat].items()):
            lines.append(f"    {s:<22}  {n}")
    lines.append("")

    # Per-file table
    lines.append("=" * 72)
    lines.append("Per-file results")
    lines.append("=" * 72)
    lines.append(
        f"{'filename':<46} {'category':<22} {'status':<22} {'chars':>8} {'chunks':>6} {'embeds':>6}"
    )
    lines.append("-" * 110)
    for r in results:
        name = r["filename"]
        if len(name) > 45:
            name = name[:42] + "..."
        lines.append(
            f"{name:<46} {r['category']:<22} {r.get('status', '?'):<22} "
            f"{r.get('extracted_char_count', 0):>8} {r.get('chunk_count', 0):>6} {r.get('embedding_count', 0):>6}"
        )

    # Errors block
    errs = [r for r in results if r.get("status") not in ("indexed",)]
    if errs:
        lines.append("")
        lines.append("=" * 72)
        lines.append("Errors / non-indexed")
        lines.append("=" * 72)
        for r in errs:
            lines.append(f"\n[{r.get('status')}] {r['filename']}")
            err = r.get("error_message") or "(no message)"
            for ln in err.splitlines():
                lines.append(f"    {ln}")

    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    log.info("Wrote %s and %s", json_path, txt_path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest a local folder of reference docs into Kateb.",
    )
    parser.add_argument(
        "folder", help="Folder to walk (default category inferred from subfolder name)",
    )
    parser.add_argument(
        "--category", default=None,
        help="Override the inferred category for ALL files in the folder.",
    )
    parser.add_argument(
        "--filter", default=None,
        help="Only ingest files whose name contains this substring.",
    )
    parser.add_argument(
        "--out-dir", default=str(ROOT),
        help="Where to write INDEX_STATUS.json / INDEX_STATUS.txt (default: project root).",
    )
    args = parser.parse_args()

    folder = Path(args.folder).resolve()
    out_dir = Path(args.out_dir).resolve()
    results = asyncio.run(walk_and_ingest(folder, args.category, args.filter))
    write_reports(results, out_dir)
    # Exit non-zero if any non-`indexed` outcome (caller can decide)
    return 0 if all(r.get("status") == "indexed" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

