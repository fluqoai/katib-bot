"""Generate the final ingestion report in the user's exact requested format.

Output goes to:
  * C:/Users/khayrat/Desktop/MyProjects/kateb/INDEX_STATUS.txt  (UTF-8)
  * C:/Users/khayrat/Desktop/MyProjects/kateb/INDEX_STATUS.json (machine-readable)

The format is:

  TOTAL SOURCE FILES:
  TOTAL DOCUMENTS:
  SUCCESSFULLY INDEXED:
  FAILED:
  NEEDS OCR:

  Templates:
    Indexed:
    Failed:

  National regulations:
    Indexed:
    Failed:

  Internal policies:
    Indexed:
    Failed:

  + per-file table: filename | category | status | extracted chars | chunks | embeddings | error
"""
import sys
from pathlib import Path
from collections import Counter, defaultdict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from supabase import create_client
from bot.config import get_settings


def main() -> int:
    s = get_settings()
    sb = create_client(s.supabase_url, s.resolved_service_key())

    # Source files (from the folder walker perspective)
    src_dir = ROOT / "reference_docs"
    src_files = sorted(
        p for p in src_dir.rglob("*")
        if p.is_file()
        and p.suffix.lower() in {".md", ".markdown", ".txt", ".docx", ".doc", ".pdf"}
    )
    src_count = len(src_files)

    # DB rows
    rows = (
        sb.table("documents")
        .select(
            "id, title, category, status, chunk_count, embedding_count, "
            "extracted_char_count, error_message, source_path"
        )
        .order("category")
        .order("title")
        .execute()
        .data
    )

    # Aggregate
    by_status: Counter = Counter()
    by_cat_status: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        st = r.get("status") or "unknown"
        by_status[st] += 1
        by_cat_status[r.get("category") or "?"][st] += 1

    # Per-category indexed / failed
    indexed = by_status.get("indexed", 0)
    failed = by_status.get("failed", 0)
    needs_ocr = by_status.get("needs_ocr", 0)
    needs_doc = by_status.get("needs_doc_conversion", 0)
    other = sum(v for k, v in by_status.items() if k not in ("indexed", "failed", "needs_ocr", "needs_doc_conversion"))

    cat_order = ["templates", "national_regulations", "internal_policies"]

    lines: list[str] = []
    lines.append("Kateb Ingestion Report")
    lines.append(f"Generated: {Path(__file__).name}  (Supabase: {s.supabase_url})")
    lines.append("=" * 70)
    lines.append("")
    lines.append(f"TOTAL SOURCE FILES: {src_count}")
    lines.append(f"TOTAL DOCUMENTS: {len(rows)}")
    lines.append(f"SUCCESSFULLY INDEXED: {indexed}")
    lines.append(f"FAILED: {failed}")
    lines.append(f"NEEDS OCR: {needs_ocr}")
    if needs_doc:
        lines.append(f"NEEDS DOC CONVERSION: {needs_doc}")
    if other:
        lines.append(f"OTHER (pending/processing): {other}")
    lines.append("")

    # Per-category breakdown in the user's exact format
    def cat_section(name: str, key: str) -> None:
        n_indexed = by_cat_status[key].get("indexed", 0)
        n_failed = by_cat_status[key].get("failed", 0)
        n_ocr = by_cat_status[key].get("needs_ocr", 0)
        n_doc = by_cat_status[key].get("needs_doc_conversion", 0)
        lines.append(f"{name}:")
        lines.append(f"  Indexed: {n_indexed}")
        lines.append(f"  Failed: {n_failed}")
        if n_ocr:
            lines.append(f"  Needs OCR: {n_ocr}")
        if n_doc:
            lines.append(f"  Needs Doc Conversion: {n_doc}")
        lines.append("")

    cat_section("Templates", "templates")
    cat_section("National regulations", "national_regulations")
    cat_section("Internal policies", "internal_policies")

    # Per-file table
    lines.append("=" * 70)
    lines.append("Per-file results")
    lines.append("=" * 70)
    lines.append(
        f"{'filename':<46} {'category':<22} {'status':<22} "
        f"{'chars':>8} {'chunks':>6} {'embeds':>6}  error"
    )
    lines.append("-" * 130)
    for r in rows:
        title = r.get("title") or "?"
        if len(title) > 45:
            title = title[:42] + "..."
        cat = (r.get("category") or "?")[:21]
        st = (r.get("status") or "?")[:21]
        chars = r.get("extracted_char_count", 0) or 0
        chunks = r.get("chunk_count", 0) or 0
        embeds = r.get("embedding_count", 0) or 0
        err = r.get("error_message") or ""
        if err:
            err = err.splitlines()[0][:60]
        lines.append(
            f"{title:<46} {cat:<22} {st:<22} {chars:>8} {chunks:>6} {embeds:>6}  {err}"
        )

    out_path = ROOT / "INDEX_STATUS.txt"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
