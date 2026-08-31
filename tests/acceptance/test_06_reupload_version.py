"""Test 6: Re-upload a newer version of an existing document — old
version must remain in version history, while only the new active
version is used for retrieval.

This is a SELF-ISOLATING test: it creates a brand-new document on
each run (no reuse of pre-existing docs that may have accumulated
versions from earlier test runs), uploads V1 then V2, verifies the
version-isolation contract at the DB and retrieval layer, and cleans
up the document at the end.

The test does NOT change any production code. It only:
  - creates a fresh document via the same API the dashboard uses
  - uploads V2 via the same upload endpoint
  - verifies the database state (no UI/API surface checks alone)
  - cleans up by deactivating + soft-deleting the test document

Pass conditions are checked at the DB layer (counts, version pointers,
file_ids) AND the retrieval layer (match_documents must return V2
chunks only, with no V1 leakage). An `indexed` status from the API
is NOT sufficient on its own.
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


DESCRIPTION = (
    "Each run creates a brand-new internal policy, uploads V1, then "
    "uploads V2 to the same document. We verify at the DB level: (1) "
    "document_files has both V1 and V2 rows with DISTINCT file_ids, "
    "(2) documents.current_version = 2 only AFTER V2 is fully "
    "indexed, (3) V1 chunks are preserved (count unchanged from "
    "after-V1 snapshot), (4) V2 has all its chunks actually inserted "
    "in document_chunks under V2's file_id, (5) "
    "document_files.chunk_count matches the actual count in "
    "document_chunks for BOTH V1 and V2 (no double-count, no "
    "phantom counts), (6) V2 starts from chunk_index=0 (i.e. V1's "
    "chunks do not satisfy V2's insert), (7) match_documents for "
    "the V2 body returns ONLY V2 chunks (no V1 leakage). At the end "
    "of the test the document is deactivated and its chunks are "
    "deleted so the next run starts clean."
)
EXPECTED = (
    "Two distinct document_files rows (V1, V2); V1 file_id != V2 "
    "file_id; current_version=2; V1 has the same number of chunks "
    "as right after V1 finished; V2 has >0 chunks in document_chunks "
    "under V2's file_id; document_files.chunk_count for V1 and V2 "
    "equals the actual COUNT(document_chunks) per file_id; "
    "match_documents for V2 body returns hits ONLY with version=2 "
    "and V2's file_id (not V1's); document cleaned up at end."
)


def run(r: TestResult) -> None:
    import asyncio
    from rag.embeddings import from_env

    sb = _supabase()
    s = get_settings()
    unique_id = int(time.time())
    category = "internal_policies"

    # ----------------------------------------------------------------------
    # Phase 1: upload V1 (no document_id → creates a fresh document)
    # ----------------------------------------------------------------------
    v1_title = f"سياسة اختبار العزل — V1 ({unique_id})"
    v1_body = (
        "النسخة الأولى من سياسة اختبار العزل. تشترط الجمعية في الإصدار "
        "الأول أن يكون عمر المتطوع ثمانية عشر عاماً وأن يقدّم إثبات خلوّ "
        "سجلّه من السوابق وفق اللائحة التنفيذية."
    )
    v1_path = PROJECT_ROOT / "tests" / "acceptance" / "fixtures" / f"v1_isolation_{unique_id}.docx"
    v1_path.parent.mkdir(parents=True, exist_ok=True)
    v1_path.write_bytes(make_minimal_docx(v1_title, v1_body))

    r.add_finding("info", f"--- Phase 1: upload V1 (fresh document) ---")
    v1_upload = api_post_multipart(
        "/admin/upload",
        file_path=v1_path,
        fields={"category": category, "title": v1_title},
        timeout=120.0,
    )
    v1_doc_id = v1_upload.get("id")
    assert v1_doc_id, f"V1 upload did not return id: {v1_upload}"
    r.add_finding("info", f"V1 uploaded: doc_id={v1_doc_id}, title={v1_title!r}")

    # Find the V1 file_id and wait for it to be indexed
    v1_file_id = _wait_for_indexed(r, sb, v1_doc_id, expected_version=1,
                                   phase="V1", timeout_s=180)
    r.add_finding("info", f"V1 indexed: file_id={v1_file_id}")

    # Snapshot V1 chunk count and the ACTUAL count in document_chunks
    f1 = sb.table("document_files").select("id, version, chunk_count").eq("id", v1_file_id).single().execute()
    v1_meta_count = (f1.data or {}).get("chunk_count") or 0
    v1_real_chunks = sb.table("document_chunks").select("id", count="exact").eq(
        "document_file_id", v1_file_id).execute()
    v1_real_count = v1_real_chunks.count or 0
    r.add_finding("info", f"V1 document_files.chunk_count={v1_meta_count}, "
                          f"actual COUNT(document_chunks WHERE file_id=V1)={v1_real_count}")
    # If these don't match already, the worker has a counting bug for V1 too.
    assert v1_meta_count == v1_real_count, (
        f"V1 chunk_count column ({v1_meta_count}) does not match actual "
        f"count in document_chunks ({v1_real_count}) — worker counting bug"
    )
    assert v1_real_count > 0, "V1 has 0 chunks in document_chunks after indexed"

    # ----------------------------------------------------------------------
    # Phase 2: upload V2 to the same document
    # ----------------------------------------------------------------------
    v2_title = f"سياسة اختبار العزل — V2 ({unique_id})"
    v2_body = (
        "النسخة الثانية المحدّثة من سياسة اختبار العزل. تشمل التعديلات: "
        "تخفيض سن المتطوع إلى ستة عشر عاماً بشرط موافقة ولي الأمر "
        "كتابياً، والسماح للمتطوعين غير السعوديين بالمشاركة الموسمية، "
        "وتحديث إجراءات التسجيل وفق اللائحة التنفيذية."
    )
    v2_path = PROJECT_ROOT / "tests" / "acceptance" / "fixtures" / f"v2_isolation_{unique_id}.docx"
    v2_path.write_bytes(make_minimal_docx(v2_title, v2_body))

    r.add_finding("info", f"--- Phase 2: upload V2 to the same document ---")
    v2_upload = api_post_multipart(
        "/admin/upload",
        file_path=v2_path,
        fields={"category": category, "title": v2_title, "document_id": v1_doc_id},
        timeout=120.0,
    )
    assert v2_upload.get("id") == v1_doc_id, f"V2 upload wrong doc id: {v2_upload}"
    r.add_finding("info", f"V2 uploaded to doc_id={v1_doc_id}")

    v2_file_id = _wait_for_indexed(r, sb, v1_doc_id, expected_version=2,
                                   phase="V2", timeout_s=180)
    assert v2_file_id != v1_file_id, "V2 file_id must differ from V1 file_id"
    r.add_finding("info", f"V2 indexed: file_id={v2_file_id} (distinct from V1)")

    # ----------------------------------------------------------------------
    # Phase 3: verify version-isolation invariants at the DB layer
    # ----------------------------------------------------------------------
    r.add_finding("info", f"--- Phase 3: verify isolation (DB-level) ---")

    # 3a) document_files has both versions with distinct file_ids
    files = sb.table("document_files").select("id, version, chunk_count").eq(
        "document_id", v1_doc_id).order("version").execute()
    versions = sorted({f["version"] for f in (files.data or [])})
    r.add_finding("info", f"document_files versions: {versions}")
    assert versions == [1, 2], (
        f"expected exactly versions [1, 2] for the test document, got {versions}"
    )

    # 3b) documents.current_version = 2 (only after V2 completes)
    d2 = sb.table("documents").select("current_version, is_active").eq(
        "id", v1_doc_id).single().execute().data or {}
    assert d2.get("current_version") == 2, f"current_version should be 2, got {d2}"
    r.add_finding("info", f"documents.current_version = {d2.get('current_version')}, "
                          f"is_active={d2.get('is_active')}")

    # 3c) V1 chunks preserved (count unchanged) AND under V1's file_id
    v1_chunks_now = sb.table("document_chunks").select("id", count="exact").eq(
        "document_file_id", v1_file_id).execute()
    v1_now_count = v1_chunks_now.count or 0
    r.add_finding("info", f"V1 chunks (file_id={v1_file_id}) after V2: {v1_now_count} "
                          f"(was {v1_real_count} after V1)")
    assert v1_now_count == v1_real_count, (
        f"V1 chunks were modified/lost under V1's file_id: was {v1_real_count}, "
        f"now {v1_now_count}"
    )

    # 3d) V2 has its OWN chunks under V2's file_id (this is the bug that
    # used to be hidden: V2 used to have 0 chunks because V1's chunk_index=0
    # satisfied the resume check)
    v2_chunks = sb.table("document_chunks").select("id, chunk_index", count="exact").eq(
        "document_file_id", v2_file_id).execute()
    v2_real_count = v2_chunks.count or 0
    v2_chunk_indexes = sorted({c["chunk_index"] for c in (v2_chunks.data or [])})
    r.add_finding("info", f"V2 chunks (file_id={v2_file_id}): {v2_real_count}, "
                          f"chunk_indexes={v2_chunk_indexes}")
    assert v2_real_count > 0, (
        f"V2 has 0 chunks in document_chunks under V2's file_id — the "
        f"resume check is wrongly treating V1's chunk_index=0 as V2's"
    )
    # V2 must start from chunk_index=0 (its own index space, not V1's)
    assert 0 in v2_chunk_indexes, (
        f"V2's chunk_indexes must include 0 (its own index space); got {v2_chunk_indexes}"
    )

    # 3e) document_files.chunk_count for V2 equals actual count for V2
    f2 = sb.table("document_files").select("chunk_count").eq("id", v2_file_id).single().execute()
    v2_meta_count = (f2.data or {}).get("chunk_count") or 0
    r.add_finding("info", f"V2 document_files.chunk_count={v2_meta_count}, "
                          f"actual COUNT(document_chunks WHERE file_id=V2)={v2_real_count}")
    assert v2_meta_count == v2_real_count, (
        f"V2 chunk_count column ({v2_meta_count}) does not match actual "
        f"count in document_chunks ({v2_real_count}) — worker counting bug"
    )

    # 3f) V1.chunk_count column is still accurate (no double-counting drift)
    f1_after = sb.table("document_files").select("chunk_count").eq("id", v1_file_id).single().execute()
    v1_meta_after = (f1_after.data or {}).get("chunk_count") or 0
    r.add_finding("info", f"V1 document_files.chunk_count after V2: {v1_meta_after} "
                          f"(was {v1_meta_count} after V1)")
    assert v1_meta_after == v1_real_count, (
        f"V1 chunk_count column drifted: was {v1_meta_count} then {v1_meta_after}, "
        f"actual count is {v1_real_count}"
    )

    # 3g) match_documents for V2 body returns ONLY V2 hits (version=2,
    # document_file_id=V2's), with NO V1 leakage.
    embed = from_env()
    qv = asyncio.run(embed.embed_texts([v2_body]))[0]
    res = sb.rpc("match_documents", {
        "query_embedding": qv,
        "match_threshold": 0.0,
        "match_count": 3,
        "filter_category": category,
    }).execute()
    hits_for_doc = [h for h in (res.data or []) if h.get("document_id") == v1_doc_id]
    r.add_finding("info", f"match_documents returned {len(hits_for_doc)} hits for our doc")
    assert len(hits_for_doc) > 0, (
        f"match_documents returned 0 hits for our test doc. The V2 file "
        f"has {v2_real_count} chunks in document_chunks — the retrieval "
        f"layer (which filters by version=current_version) should have "
        f"found them. Either semantic similarity is too low for the "
        f"2-line synthetic body, or match_documents is mis-filtered."
    )
    versions_returned = {h.get("version") for h in hits_for_doc}
    file_ids_returned = {h.get("file_id") for h in hits_for_doc}
    r.add_finding("info", f"versions in hits: {versions_returned}, file_ids: {file_ids_returned}")
    assert versions_returned == {2}, (
        f"isolation broken: match_documents returned versions "
        f"{versions_returned} for our test doc, expected ONLY {{2}}"
    )
    assert v1_file_id not in file_ids_returned, (
        f"isolation broken: V1 file_id {v1_file_id} leaked into "
        f"current-version retrieval hits {file_ids_returned}"
    )
    assert v2_file_id in file_ids_returned, (
        f"isolation broken: V2 file_id {v2_file_id} did NOT appear in "
        f"current-version retrieval hits {file_ids_returned}"
    )

    r.actual = (
        f"V1 file_id={v1_file_id} ({v1_real_count} chunks preserved), "
        f"V2 file_id={v2_file_id} ({v2_real_count} chunks inserted, "
        f"indexes={v2_chunk_indexes}), current_version=2, "
        f"chunk_count columns match real counts for both files, "
        f"match_documents returns V2-only hits (no V1 leakage)"
    )

    # ----------------------------------------------------------------------
    # Phase 4: cleanup — deactivate + soft-delete the test document
    # ----------------------------------------------------------------------
    r.add_finding("info", f"--- Phase 4: cleanup test document {v1_doc_id} ---")
    try:
        # Deactivate the document (so it doesn't show in admin UIs)
        sb.table("documents").update({"is_active": False}).eq(
            "id", v1_doc_id).execute()
        # Delete document_files (cascades to document_chunks via FK)
        sb.table("document_files").delete().eq("document_id", v1_doc_id).execute()
        # Finally delete the document row
        sb.table("documents").delete().eq("id", v1_doc_id).execute()
        r.add_finding("info", f"cleanup OK: test document {v1_doc_id} removed")
    except Exception as e:  # noqa: BLE001
        r.add_finding("warn", f"cleanup failed: {e!r}")


def _wait_for_indexed(
    r: TestResult, sb, doc_id: str, expected_version: int, *, phase: str, timeout_s: int
) -> str:
    """Poll the DB until the file at `expected_version` is fully
    indexed (status='indexed' AND chunk_count > 0). Returns the
    file_id.
    """
    deadline = time.time() + timeout_s
    file_id = None
    final_status = None
    chunk_count = 0
    while time.time() < deadline:
        d = sb.table("documents").select("status, current_version, chunk_count").eq(
            "id", doc_id).single().execute().data or {}
        f_resp = sb.table("document_files").select("id, version, chunk_count").eq(
            "document_id", doc_id).eq("version", expected_version).limit(1).execute()
        if f_resp.data:
            file_id = f_resp.data[0]["id"]
            final_status = d.get("status")
            chunk_count = f_resp.data[0].get("chunk_count") or 0
            r.add_finding("info",
                f"  [{phase}] doc.status={final_status} cur_v={d.get('current_version')} "
                f"file_v{expected_version}.chunks={chunk_count}")
            if (final_status == "indexed"
                    and d.get("current_version") == expected_version
                    and chunk_count > 0):
                return file_id
        time.sleep(4)
    raise AssertionError(
        f"{phase} did not become indexed within {timeout_s}s. "
        f"last status={final_status}, current_version={d.get('current_version')}, "
        f"chunks={chunk_count}"
    )


def _supabase():
    s = get_settings()
    return create_client(s.supabase_url, s.resolved_service_key())


def test():
    return run_test(
        name="06_reupload_version_isolation",
        description=DESCRIPTION,
        expected=EXPECTED,
        fn=run,
    )
