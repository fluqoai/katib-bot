"""One-shot ingestion from a Google Drive folder into Supabase.

Usage:
    python scripts/ingest_drive.py --folder <DRIVE_FOLDER_ID>
    python scripts/ingest_drive.py              # uses GOOGLE_DRIVE_ROOT_FOLDER_ID from .env
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from supabase import create_client  # noqa: E402

from drive.client import DriveClient  # noqa: E402
from drive.sync import ingest_folder  # noqa: E402
from rag.embeddings import from_env as emb_from_env  # noqa: E402


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("ingest")


async def run(folder_id: str) -> int:
    from bot.config import get_settings
    settings = get_settings()

    drive = DriveClient(settings.service_account_path)
    log.info("Drive auth OK as %s", drive.service_account_email)

    supabase = create_client(settings.supabase_url, settings.resolved_service_key())
    log.info("Supabase client OK at %s", settings.supabase_url)

    embedder = emb_from_env()
    log.info("Embedding model: %s (dim=%d)", embedder.model, embedder.dimension)

    stats = await ingest_folder(drive, supabase, embedder, folder_id)
    log.info(
        "Done. seen=%d inserted=%d updated=%d chunks=%d errors=%d",
        stats.documents_seen, stats.documents_inserted,
        stats.documents_updated, stats.chunks_inserted, len(stats.errors),
    )
    if stats.errors:
        log.warning("Errors during ingestion:")
        for e in stats.errors:
            log.warning("  - %s", e)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--folder", default=None,
        help="Drive folder ID to ingest. Defaults to GOOGLE_DRIVE_ROOT_FOLDER_ID from .env.",
    )
    args = parser.parse_args()

    import os
    folder_id = args.folder or os.environ.get("GOOGLE_DRIVE_ROOT_FOLDER_ID")
    if not folder_id:
        parser.error(
            "No folder ID provided. Pass --folder <id> or set "
            "GOOGLE_DRIVE_ROOT_FOLDER_ID in .env."
        )
    return asyncio.run(run(folder_id))


if __name__ == "__main__":
    raise SystemExit(main())
