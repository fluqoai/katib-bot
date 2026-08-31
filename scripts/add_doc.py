"""Add one document to the index. The primary way to populate the corpus
for the non-technical-client deployment.

Usage:

    # Read content from a file:
    python scripts/add_doc.py --title "نموذج طلب شراكة" --category templates --file sample.md

    # Or paste content interactively (end input with a line containing only '.'):
    python scripts/add_doc.py --title "نموذج طلب شراكة" --category templates

    # Override the source_uri (otherwise: manual://<slug-of-title>):
    python scripts/add_doc.py --title "..." --category ... --file ... --source-uri "manual://2026/01/foo"

The script is idempotent on `source_uri`: re-running with the same
`source_uri` updates the existing document and replaces its chunks.
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
from rag.embeddings import from_env as emb_from_env  # noqa: E402
from rag.ingest import VALID_CATEGORIES, IngestError, ingest_document  # noqa: E402


def _slugify(s: str) -> str:
    """Make a filesystem-safe slug out of an arbitrary title."""
    import re
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"[^\w\-]+", "", s, flags=re.UNICODE)
    return s or "doc"


def _read_content(args: argparse.Namespace) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    # Interactive: read from stdin until a single '.' line.
    print(
        "Paste the document text. End with a line containing only '.' and Enter:",
        file=sys.stderr,
    )
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == ".":
            break
        lines.append(line)
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    supabase = create_client(settings.supabase_url, settings.resolved_service_key())
    embedder = emb_from_env()
    print(f"Supabase: {settings.supabase_url} (ref={settings.supabase_project_ref or '?'})")
    print(f"Embeddings: {embedder.provider}/{embedder.model} (dim={embedder.dimension})")

    content = _read_content(args)
    if not content.strip():
        print("❌ Empty content — nothing to ingest.", file=sys.stderr)
        return 1

    source_uri = args.source_uri or f"manual://{_slugify(args.title)}"
    try:
        result = await ingest_document(
            supabase, embedder,
            title=args.title,
            content=content,
            category=args.category,
            source_uri=source_uri,
            metadata={"added_via": "scripts/add_doc.py"},
        )
    except IngestError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    action = result["action"]
    emoji = "✅" if action == "inserted" else "♻️"
    print(
        f"{emoji} {action}: {args.title!r}\n"
        f"   id      = {result['document_id']}\n"
        f"   source  = {source_uri}\n"
        f"   chunks  = {result['chunks']}\n"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add one document to the Kateb index.",
    )
    parser.add_argument("--title", required=True, help="Display title for the document")
    parser.add_argument(
        "--category", required=True, choices=sorted(VALID_CATEGORIES),
        help="One of: templates, national_regulations, internal_policies, examples, other",
    )
    parser.add_argument(
        "--file", help="Read content from this UTF-8 file. If omitted, "
                       "content is read from stdin until a '.' line.",
    )
    parser.add_argument(
        "--source-uri", help="Stable identifier for idempotent re-ingestion. "
                             "Defaults to manual://<slug-of-title>.",
    )
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
