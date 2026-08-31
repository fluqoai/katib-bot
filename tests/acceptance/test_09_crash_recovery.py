"""Test 9: Worker/API restart during processing — must resume without
duplicate chunks or corrupted documents.

We can't actually crash the API in a test (it would break the rest of
the suite), so we simulate the contract by:
  1. Checking that the worker is designed to recover (code inspection)
  2. Checking that a long-running pipeline call (the letter generator)
     does NOT write to DB until the export stage, so a crash mid-flight
     is safe
  3. Checking the resumable processor invariants in the DB schema
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tests.acceptance.framework import run_test, TestResult, api_post


DESCRIPTION = (
    "If the worker or API restarts mid-processing, the job must resume "
    "without leaving duplicate chunks or half-written documents. We check "
    "the architectural contracts that make this true."
)
EXPECTED = (
    "1) document_chunks has UNIQUE constraint on (document_file_id, "
    "chunk_index) so re-runs can't double-insert. "
    "2) processing_jobs has a stale-claim reaper (scripts/worker.py). "
    "3) The letter pipeline's _stage_persist is the ONLY writes-to-DB "
    "step and runs LAST, so a crash before export = no orphan draft."
)


def run(r: TestResult) -> None:
    # 1) Verify the DB unique constraint
    sb_url_match = _get_supabase_url()
    import psycopg2
    import os
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute("""
                select conname, pg_get_constraintdef(c.oid)
                  from pg_constraint c
                  join pg_class t on t.oid = c.conrelid
                 where t.relname = 'document_chunks'
                   and c.contype = 'u'
            """)
            uqs = cur.fetchall()
            names = [u[0] for u in uqs]
            defs = [u[1] for u in uqs]
            r.add_finding("info", f"document_chunks unique constraints: {names}")
            assert any("document_file_id" in d and "chunk_index" in d for d in defs), (
                f"document_chunks missing unique (document_file_id, chunk_index) — "
                f"resumable ingest could double-insert. Constraints: {defs}"
            )
    finally:
        conn.close()

    # 2) Verify the worker has the stale-claim reaper
    worker_py = (PROJECT_ROOT / "scripts" / "worker.py").read_text(encoding="utf-8")
    has_reaper = "reap_stale_jobs" in worker_py and "STALE" in worker_py.upper()
    assert has_reaper, "scripts/worker.py missing reap_stale_jobs (stale-claim recovery)"
    r.add_finding("info", "scripts/worker.py has reap_stale_jobs")

    # 3) Verify the letter pipeline's _stage_persist is the only DB writer
    lp = (PROJECT_ROOT / "rag" / "letter_pipeline.py").read_text(encoding="utf-8")
    persist_section = re.search(r"async def _stage_persist.*?(?=\n    async def|\nclass )", lp, re.DOTALL)
    assert persist_section, "_stage_persist not found"
    persist_text = persist_section.group(0)
    insert_count = persist_text.count("table(") + persist_text.count(".table(")
    r.add_finding("info", f"_stage_persist uses {insert_count} table insert(s)")
    assert "drafts" in persist_text, "_stage_persist must write to drafts table"

    # Confirm NO OTHER stage writes to drafts
    other_writes = re.findall(r"\.table\(\"drafts\"\)", lp)
    other_writes_count = len(other_writes)
    r.add_finding("info", f"total drafts-table calls in pipeline: {other_writes_count}")
    assert other_writes_count == 1, (
        f"letter_pipeline.py has {other_writes_count} writes to drafts — "
        f"a crash before _stage_persist should be safe (no partial write)"
    )

    # 4) Verify the schema has the processing_jobs table + has stale timeout
    conn2 = psycopg2.connect(os.environ["SUPABASE_DB_URL"])
    try:
        with conn2.cursor() as cur:
            cur.execute("""
                select column_name from information_schema.columns
                 where table_schema='public' and table_name='processing_jobs'
                 order by ordinal_position
            """)
            cols = [r[0] for r in cur.fetchall()]
            r.add_finding("info", f"processing_jobs columns: {cols}")
            for needed in ("status", "claimed_at", "claimed_by"):
                assert needed in cols, f"processing_jobs missing column {needed!r}"
    finally:
        conn2.close()

    # 5) Verify the upload contract: documents has the columns the worker relies on
    conn3 = psycopg2.connect(os.environ["SUPABASE_DB_URL"])
    try:
        with conn3.cursor() as cur:
            cur.execute("""
                select column_name from information_schema.columns
                 where table_schema='public' and table_name='documents'
                 order by ordinal_position
            """)
            cols = [r[0] for r in cur.fetchall()]
            r.add_finding("info", f"documents columns: {[c for c in cols if 'process' in c or c in ('current_version', 'is_active', 'status')]}")
            # Note: the actual column is `status`, not `processing_status`
            for needed in ("status", "processing_progress", "current_version", "is_active"):
                assert needed in cols, f"documents missing column {needed!r}"
            # Verify status check constraint allows the values the worker writes
            cur.execute("""
                select pg_get_constraintdef(oid) from pg_constraint
                 where conrelid = 'public.documents'::regclass
                   and conname = 'documents_status_check'
            """)
            row = cur.fetchone()
            check_def = row[0] if row else None
            r.add_finding("info", f"documents status check: {check_def}")
            assert check_def, "no status check constraint on documents"
            for needed in ("pending", "processing", "indexed", "needs_ocr", "needs_doc_conversion", "failed"):
                assert needed in check_def, (
                    f"documents.status check missing {needed!r}"
                )
    finally:
        conn3.close()

    r.actual = (
        "all crash-recovery contracts in place: "
        "UNIQUE(document_file_id, chunk_index), "
        "reap_stale_jobs in worker.py, "
        "drafts write only in _stage_persist (last), "
        "processing_jobs has claimed_at/claimed_by"
    )


def _get_supabase_url():
    import os
    return os.environ.get("SUPABASE_URL", "")


def test():
    return run_test(
        name="09_crash_recovery",
        description=DESCRIPTION,
        expected=EXPECTED,
        fn=run,
    )
