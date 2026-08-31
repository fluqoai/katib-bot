"""Delete a document from the index by id or source_uri.

Usage:
    python scripts/delete_doc.py --id <uuid>
    python scripts/delete_doc.py --source-uri manual://some-slug
    python scripts/delete_doc.py --id <uuid> --yes     # skip confirmation
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from supabase import create_client  # noqa: E402

from bot.config import get_settings  # noqa: E402
from rag.ingest import delete_document  # noqa: E402


async def _lookup(supabase, args) -> dict | None:
    if args.id:
        resp = await asyncio.to_thread(
            lambda: supabase.table("documents")
            .select("id, title, category, source_uri")
            .eq("id", args.id).limit(1).execute()
        )
    elif args.source_uri:
        resp = await asyncio.to_thread(
            lambda: supabase.table("documents")
            .select("id, title, category, source_uri")
            .eq("source_uri", args.source_uri).limit(1).execute()
        )
    else:
        return None
    return resp.data[0] if resp.data else None


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    supabase = create_client(settings.supabase_url, settings.resolved_service_key())

    doc = await _lookup(supabase, args)
    if doc is None:
        print("No document matches that id/source_uri.")
        return 1

    print(
        f"Will delete:\n"
        f"  id          = {doc['id']}\n"
        f"  title       = {doc['title']}\n"
        f"  category    = {doc['category']}\n"
        f"  source_uri  = {doc['source_uri']}\n"
    )

    if not args.yes:
        confirm = input("Confirm delete? (y/N) ").strip().lower()
        if confirm != "y":
            print("Cancelled.")
            return 0

    ok = await delete_document(supabase, doc["id"])
    if ok:
        print("✅ Deleted. All chunks cascade-removed.")
        return 0
    print("❌ Nothing was deleted (maybe already gone?).")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete a Kateb index document.")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--id", help="Document UUID")
    g.add_argument("--source-uri", help="Or delete by stable source_uri")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
