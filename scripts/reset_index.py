"""Reset all documents in the index so a re-ingest starts clean.

This:
  * Sets every document's status to 'pending'.
  * Clears the chunk_count, embedding_count, extracted_char_count counters
    so the validation script doesn't get fooled by stale numbers.
  * Clears error_message and last_processed_at.

Does NOT delete the document rows or their chunks (a fresh ingestion will
overwrite the chunks anyway via the upsert path).

Usage:
    python scripts/reset_index.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

from supabase import create_client  # noqa: E402
from bot.config import get_settings  # noqa: E402


def main() -> int:
    s = get_settings()
    sb = create_client(s.supabase_url, s.resolved_service_key())
    n = (
        sb.table("documents")
        .select("id", count="exact")
        .execute()
        .count
        or 0
    )
    print(f"Found {n} document(s). Resetting...")
    sb.table("documents").update({
        "status": "pending",
        "error_message": None,
        "chunk_count": 0,
        "embedding_count": 0,
        "extracted_char_count": 0,
        "last_processed_at": None,
    }).neq("status", "pending").execute()
    print("Reset done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
