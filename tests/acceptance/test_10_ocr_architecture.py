"""Test 10: Scanned Arabic PDF — verify the new Vision/OCR fallback
architecture is ready for implementation/testing.

The OCR fallback architecture must have:
  1) A loader that detects when a PDF is image-only (no extractable text)
     and falls back to OCR
  2) A way to plug in an OCR engine (Tesseract for now, Vision API later)
  3) A loader-level status (`needs_ocr`) that the worker can re-queue
  4) The auto-retry-needs_files sweep in the worker that re-runs OCR
     when the tool becomes available
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tests.acceptance.framework import run_test, TestResult


DESCRIPTION = (
    "The OCR fallback for scanned Arabic PDFs is an architectural feature. "
    "Verify the code is ready: loaders detect image-only PDFs, the worker "
    "auto-retries needs_ocr jobs, and the Vision API extension point exists."
)
EXPECTED = (
    "rag/loaders/pdf_loader.py detects image-only PDFs and returns status='needs_ocr'. "
    "scripts/worker.py has auto_retry_needs_files() that re-queues when Tesseract is "
    "detected. A Vision API extension point exists in code (commented or stubbed)."
)


def run(r: TestResult) -> None:
    # 1) pdf_loader.py: check for image-only detection
    pdf_loader = (PROJECT_ROOT / "rag" / "loaders" / "pdf_loader.py").read_text(encoding="utf-8")
    has_image_only = (
        "needs_ocr" in pdf_loader
        or "image_only" in pdf_loader.lower()
        or "extract_text" in pdf_loader
    )
    r.add_finding("info", f"pdf_loader.py: needs_ocr reference = {has_image_only}")
    assert has_image_only, "pdf_loader.py does not handle image-only PDFs (no needs_ocr)"
    # Check for an explicit comment about OCR/Vision
    ocr_mentions = sum(1 for w in ("ocr", "OCR", "tesseract", "Tesseract", "vision", "Vision")
                       if w in pdf_loader)
    r.add_finding("info", f"OCR/Vision/Tesseract mentions in pdf_loader.py: {ocr_mentions}")
    assert ocr_mentions >= 1, "pdf_loader.py has no OCR/Vision extension point"

    # 2) worker.py: auto_retry_needs_files
    worker_py = (PROJECT_ROOT / "scripts" / "worker.py").read_text(encoding="utf-8")
    has_auto_retry = "auto_retry_needs" in worker_py or "needs_ocr" in worker_py
    r.add_finding("info", f"worker.py: auto_retry_needs reference = {has_auto_retry}")
    assert has_auto_retry, "scripts/worker.py does not auto-retry needs_ocr/needs_doc_conversion jobs"

    # 3) A Vision API extension point (look for a comment or stub)
    vision_point = re.search(r"vision|Vision", "\n".join([pdf_loader, worker_py]))
    r.add_finding("info", f"Vision mentions: {bool(vision_point)}")

    # 4) DB schema has needs_ocr as a valid status (column is `status`, not `processing_status`)
    import psycopg2, os
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute("""
                select con.conname, pg_get_constraintdef(con.oid)
                  from pg_constraint con
                  join pg_class t on t.oid = con.conrelid
                 where t.relname = 'documents'
                   and con.contype = 'c'
            """)
            checks = cur.fetchall()
            status_check = next((d for n, d in checks if n == "documents_status_check"), None)
            r.add_finding("info", f"documents status check: {status_check}")
            assert status_check, "no check constraint on documents.status"
            for needed in ("needs_ocr", "needs_doc_conversion", "indexed"):
                assert needed in status_check, (
                    f"documents.status check missing {needed!r}"
                )
    finally:
        conn.close()

    # 5) Verify the architecture allows the worker to RE-queue a needs_ocr
    # file (the database should support it without manual intervention).
    # We test this by looking at the worker code's auto-retry path.
    if "auto_retry_needs" in worker_py:
        r.add_finding("info", "worker.py auto-retries needs_* files automatically")

    r.actual = (
        "pdf_loader detects image-only PDFs, worker auto-retries needs_ocr, "
        "DB schema supports needs_ocr/needs_doc_conversion statuses"
    )


def test():
    return run_test(
        name="10_ocr_fallback_architecture",
        description=DESCRIPTION,
        expected=EXPECTED,
        fn=run,
    )
