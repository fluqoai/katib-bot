"""List all documents currently in the index.

Usage:
    python scripts/list_docs.py
    python scripts/list_docs.py --category templates
    python scripts/list_docs.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from supabase import create_client  # noqa: E402

from bot.config import get_settings  # noqa: E402
from rag.ingest import list_documents  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    supabase = create_client(settings.supabase_url, settings.resolved_service_key())
    docs = await list_documents(supabase, category=args.category)

    if args.json:
        json.dump(docs, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0

    if not docs:
        print("No documents in the index yet.")
        print("Add one with:  python scripts/add_doc.py --title ... --category ... --file ...")
        return 0

    # Pretty table
    id_w = 36
    cat_w = 22
    title_w = max(20, min(60, max(len(d["title"]) for d in docs)))
    print(f"{'ID':<{id_w}} {'CATEGORY':<{cat_w}} {'TITLE':<{title_w}}  CREATED")
    print("-" * (id_w + cat_w + title_w + 28))
    for d in docs:
        title = d["title"]
        if len(title) > title_w:
            title = title[: title_w - 1] + "…"
        print(
            f"{d['id']:<{id_w}} {d['category']:<{cat_w}} {title:<{title_w}}  {d['created_at']}"
        )
    print(f"\nTotal: {len(docs)} document(s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="List Kateb index documents.")
    parser.add_argument("--category", help="Filter by category")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
