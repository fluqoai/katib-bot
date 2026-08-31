"""Migrate the existing 41 files in `reference_docs/` to the new
Supabase Storage + processing_jobs pipeline.

For each file:
  1. Upload the bytes to the right bucket (templates / policies / regulations)
  2. Create a new document_file row pointing at the storage path
  3. Enqueue a processing job
The Python worker picks the job up and processes it.

The existing documents / chunks stay intact (so the old search still
works during the migration). When the new versions finish, the
worker activates them by updating `documents.current_version` and
`documents.is_active = true`.

Usage:
    python scripts/migrate_legacy.py
    python scripts/migrate_legacy.py --dry-run
    python scripts/migrate_legacy.py --bucket-override templates-test-1
"""
import argparse
import hashlib
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from supabase import create_client
from bot.config import get_settings
from rag.categories import CATEGORY_TO_BUCKET


CATEGORY_FROM_FOLDER = {
    "templates": "templates",
    "national_regulations": "national_regulations",
    "internal_policies": "internal_policies",
}

SUPPORTED = {".pdf", ".docx", ".doc", ".txt", ".md", ".markdown"}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--bucket-override",
        help="Use this bucket name instead of the category-based default (handy while the real buckets are being cleaned up).",
    )
    p.add_argument(
        "--limit", type=int, default=0,
        help="Stop after N files (0 = no limit).",
    )
    args = p.parse_args()

    s = get_settings()
    sb = create_client(s.supabase_url, s.resolved_service_key())

    src_root = ROOT / "reference_docs"
    if not src_root.exists():
        print(f"no reference_docs/ folder at {src_root}")
        return 1

    files = sorted(
        p for p in src_root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED
    )
    if args.limit:
        files = files[: args.limit]

    print(f"migrating {len(files)} files to Supabase Storage…")
    n_queued = 0
    n_skipped = 0
    n_error = 0
    for f in files:
        try:
            rel = f.relative_to(src_root)
            parts = rel.parts
            category = CATEGORY_FROM_FOLDER.get(parts[0].lower(), "other")
            bucket = args.bucket_override or CATEGORY_TO_BUCKET.get(category, "examples")
            if category not in CATEGORY_FROM_FOLDER:
                print(f"  skip {f.name} (category {category!r} not eligible)")
                n_skipped += 1
                continue

            data = f.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            mime = _guess_mime(f)

            # ASCII-only storage path
            from rag.categories import _category_from_path
            from pathlib import PurePosixPath
            import re
            stem = re.sub(r"[^a-zA-Z0-9._-]+", "_", PurePosixPath(f.name).stem)[:80] or "file"
            ext = f.suffix.lower() or ".bin"
            safe_name = f"{stem}-{sha[:8]}{ext}"

            # Reuse existing document by title (case-insensitive stem)
            existing = (
                sb.table("documents").select("id, current_version")
                .ilike("title", PurePosixPath(f.name).stem.replace("%", r"\%"))
                .limit(1).execute().data
            )
            if existing:
                doc_id = existing[0]["id"]
                next_version = (existing[0].get("current_version") or 1) + 1
            else:
                # Create new
                ins = sb.table("documents").insert({
                    "title": PurePosixPath(f.name).stem,
                    "category": category,
                    "bucket": bucket,
                    "is_active": True,
                    "current_version": 1,
                    "content": "",
                    "source_uri": f"local://{rel.as_posix()}",
                }).execute()
                doc_id = ins.data[0]["id"]
                next_version = 1

            storage_path = f"{doc_id}/v{next_version}/{safe_name}"

            if args.dry_run:
                print(f"  [dry] {rel} → {bucket}/{storage_path}")
                n_queued += 1
                continue

            # Upload bytes
            sb.storage.from_(bucket).upload(
                storage_path, data, {"content-type": mime, "x-upsert": "false"},
            )
            # Insert file row
            f_row = sb.table("document_files").insert({
                "document_id": doc_id,
                "version": next_version,
                "storage_bucket": bucket,
                "storage_path": storage_path,
                "mime_type": mime,
                "size_bytes": len(data),
                "sha256": sha,
                "original_filename": f.name,
                "status": "pending",
                "processing_progress": {
                    "total_chunks": 0,
                    "processed_chunks": 0,
                    "failed_chunks": 0,
                    "current_batch": 0,
                    "retry_count": 0,
                    "last_error": None,
                    "started_at": None,
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            }).execute()
            file_id = f_row.data[0]["id"]
            # Enqueue
            sb.table("processing_jobs").insert({
                "document_file_id": file_id,
                "status": "pending",
                "priority": 50,
            }).execute()
            n_queued += 1
            print(f"  ✓ {rel}  →  {bucket}/v{next_version}  (job queued)")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {f.name}: {e}")
            n_error += 1

    print()
    print(f"done. queued={n_queued}  skipped={n_skipped}  errors={n_error}")
    return 0 if n_error == 0 else 1


def _guess_mime(p: Path) -> str:
    return {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".markdown": "text/markdown",
    }.get(p.suffix.lower(), "application/octet-stream")


if __name__ == "__main__":
    raise SystemExit(main())
