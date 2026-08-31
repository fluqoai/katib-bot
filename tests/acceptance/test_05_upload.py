"""Test 5: Upload a DOCX/PDF through /admin — should automatically
enter processing and become searchable without terminal commands.

This tests the dashboard upload flow:
  POST /api/admin/upload  (multipart)
  → returns a document row
  → the document starts in pending/processing
  → after the worker picks it up, it becomes indexed
  → its chunks appear in match_documents

SELF-ISOLATING: this test cleans up before AND after itself so it
doesn't interact with other runs of itself. The pre-flight step
deactivates any leftover docs from prior Test 5 runs by matching on
`document_files.original_filename` (a stable per-file identifier
that doesn't get overwritten when the worker activates a version).
The post-step deletes the document's chunks, files, and row.

The retrieval assertion uses match_count=20 and checks the doc
appears in the result set (not that it ranks in the top 3) — this
is robust against multiple test_05 docs being present at the same
time and avoids false negatives on tie-breaking in the embedding
ranking. We do NOT touch retrieval, embeddings, production ranking,
or any other production behavior.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from supabase import create_client
from bot.config import get_settings

from tests.acceptance.framework import (
    api_post_multipart, run_test, TestResult,
    make_minimal_docx,
)


# Stable identifier: every Test 5 run uploads a file with this name.
# Used by pre-flight + post-cleanup to find test_05 docs without
# depending on the Arabic title (which the worker overwrites).
TEST_ORIGINAL_FILENAME = "test_volunteer_policy.docx"
TEST_CATEGORY = "internal_policies"


DESCRIPTION = (
    "Upload a synthetic DOCX to /api/admin/upload, then poll the admin "
    "endpoints and the database to verify the document automatically "
    "enters processing and its chunks become searchable. No terminal, no "
    "manual scripts — just the dashboard flow. Self-isolating: pre-flight "
    "deactivates prior test_05 docs (by document_files.original_filename) "
    "and post-cleanup deletes the test doc's chunks, files, and row."
)
EXPECTED = (
    "Pre-flight: any prior test_05 docs (matched by "
    "document_files.original_filename='test_volunteer_policy.docx') are "
    "deactivated. Upload returns 200 with a document_id. The document's "
    "status transitions to 'indexed' and chunk_count > 0. "
    "match_documents with match_count=20 returns the uploaded document. "
    "Post-cleanup: this run's document_chunks, document_files, and "
    "documents rows are deleted."
)


def run(r: TestResult) -> None:
    sb = _supabase()

    # ----------------------------------------------------------------------
    # Phase 0: pre-flight — deactivate any leftover test_05 docs from
    # prior runs. Match by document_files.original_filename (a stable
    # per-file identifier, not the documents.title which the worker
    # overwrites to the filename anyway).
    # ----------------------------------------------------------------------
    deactivated = _deactivate_prior_test_docs(sb)
    if deactivated:
        r.add_finding("info",
            f"pre-flight: deactivated {len(deactivated)} prior test_05 doc(s): {deactivated}")
    else:
        r.add_finding("info", "pre-flight: no prior test_05 docs found")

    # Build a unique synthetic DOCX
    title = f"دليل قبول المتطوعين — اختبار {_int_now()}"
    body_text = (
        "تشترط الجمعية في قبول المتطوعين أن يكونوا قد أتموا الثامنة عشرة "
        "من العمر، وأن يقدموا ما يثبت خلوّ سجلهم من السوابق، وأن يلتزموا "
        "بمبادئ نزاهة الأعمال والامتثال للأنظمة المعمول بها في المملكة."
    )
    docx_bytes = make_minimal_docx(title, body_text)
    test_path = PROJECT_ROOT / "tests" / "acceptance" / "fixtures" / TEST_ORIGINAL_FILENAME
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_bytes(docx_bytes)
    r.add_finding("info", f"created synthetic DOCX: {test_path.name} ({len(docx_bytes)} bytes)")

    # ----------------------------------------------------------------------
    # Phase 1: upload
    # ----------------------------------------------------------------------
    upload_res = api_post_multipart(
        "/admin/upload",
        file_path=test_path,
        fields={"category": TEST_CATEGORY, "title": title},
        timeout=120.0,
    )
    doc_id = upload_res.get("id")
    assert doc_id, f"upload did not return id: {upload_res}"
    r.add_finding("info", f"upload OK, document_id={doc_id}")

    # ----------------------------------------------------------------------
    # Phase 2: poll until indexed
    # ----------------------------------------------------------------------
    deadline = time.time() + 180
    final_status = None
    chunk_count = 0
    while time.time() < deadline:
        r2 = sb.table("documents").select("status, chunk_count, current_version, is_active").eq("id", doc_id).single().execute()
        d = r2.data
        if d:
            final_status = d.get("status")
            chunk_count = d.get("chunk_count") or 0
            r.add_finding("info", f"  status={final_status} chunks={chunk_count} version={d.get('current_version')} active={d.get('is_active')}")
            if final_status in ("indexed", "ready") and chunk_count > 0:
                break
        time.sleep(4)
    else:
        raise AssertionError(
            f"document did not become 'indexed' within 180s. last status={final_status!r}, chunks={chunk_count}"
        )

    # ----------------------------------------------------------------------
    # Phase 3: retrieval — assert the doc appears in match_documents
    # within the top 20 (not top 3 — the test is robust to other
    # similar docs being present). We do NOT assert rank; we only
    # assert presence.
    # ----------------------------------------------------------------------
    embed = _get_embedder()
    import asyncio
    qv = asyncio.run(embed.embed_texts([title]))[0]
    res = sb.rpc("match_documents", {
        "query_embedding": qv,
        "match_threshold": 0.0,
        "match_count": 20,
        "filter_category": TEST_CATEGORY,
    }).execute()
    found = [h for h in (res.data or []) if h.get("document_id") == doc_id]
    if not found:
        # Defensive: dump what we got so the failure message is useful
        top_ids = [(h.get("document_id"), h.get("file_id"), round(h.get("similarity", 0), 3)) for h in (res.data or [])[:5]]
        raise AssertionError(
            f"document_id={doc_id} not found in match_documents results "
            f"(match_count=20). Top 5 hits: {top_ids}. This means the "
            f"upload is NOT searchable from the dashboard."
        )
    r.add_finding("info",
        f"match_documents found the new doc within top 20 "
        f"(sim={found[0].get('similarity', 0):.3f}, rank {next(i for i, h in enumerate(res.data or [], 1) if h.get('document_id') == doc_id)}/{len(res.data or [])})")

    r.actual = f"uploaded id={doc_id}, status={final_status}, chunks={chunk_count}, searchable=YES"

    # ----------------------------------------------------------------------
    # Phase 4: post-cleanup — delete this run's chunks, files, and doc
    # so the next run starts from a clean state. We use the document_id
    # we just uploaded; we do NOT depend on the Arabic title.
    # ----------------------------------------------------------------------
    try:
        # chunks first (in case there's no FK cascade), then files, then doc
        sb.table("document_chunks").delete().eq("document_id", doc_id).execute()
        sb.table("document_files").delete().eq("document_id", doc_id).execute()
        sb.table("documents").delete().eq("id", doc_id).execute()
        r.add_finding("info", f"post-cleanup: removed doc {doc_id} and its chunks/files")
    except Exception as e:  # noqa: BLE001
        r.add_finding("warn", f"post-cleanup failed: {e!r}")


def _deactivate_prior_test_docs(sb) -> list[str]:
    """Find all documents that have at least one document_file with
    `original_filename = TEST_ORIGINAL_FILENAME` and mark them
    `is_active = False`. Returns the list of doc_ids that were
    deactivated (empty if none).
    """
    try:
        r = sb.table("document_files").select("document_id").eq(
            "original_filename", TEST_ORIGINAL_FILENAME).execute()
        doc_ids = list({f["document_id"] for f in (r.data or [])})
        for doc_id in doc_ids:
            sb.table("documents").update({"is_active": False}).eq("id", doc_id).execute()
        return doc_ids
    except Exception as e:  # noqa: BLE001
        print(f"  pre-flight warning: {e!r}")
        return []


def _supabase():
    s = get_settings()
    return create_client(s.supabase_url, s.resolved_service_key())


def _get_embedder():
    from rag.embeddings import from_env
    return from_env()


def _int_now() -> int:
    return int(time.time())


def test():
    return run_test(
        name="05_upload_dashboard_flow",
        description=DESCRIPTION,
        expected=EXPECTED,
        fn=run,
    )
