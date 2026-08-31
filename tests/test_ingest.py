"""Unit tests for rag/ingest.py — no live Supabase needed.

We use `asyncio.run` inside each test so the test runner doesn't need
the pytest-asyncio plugin (which is finicky on Python 3.10 here).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from rag.ingest import (
    IngestError,
    VALID_CATEGORIES,
    delete_document,
    ingest_document,
    list_documents,
)


pytestmark = pytest.mark.unit


def _embedder_with_vectors(vectors: list[list[float]]):
    """Mock EmbeddingClient that returns the given vectors."""
    e = MagicMock()
    e.dimension = 1536

    async def _embed_texts(_texts):
        return vectors

    e.embed_texts = _embed_texts
    return e


def _mock_supabase(existing_id: str | None = None):
    """Mock supabase Client.

    `existing_id=None` → no existing row → insert path.
    `existing_id="..."` → existing row → update path.
    """
    client = MagicMock()

    # SELECT ... LIMIT 1
    select_resp = MagicMock()
    select_resp.data = [{"id": existing_id}] if existing_id else []
    (
        client.table.return_value
        .select.return_value
        .eq.return_value
        .limit.return_value
        .execute.return_value
    ) = select_resp

    # UPDATE
    (
        client.table.return_value
        .update.return_value
        .eq.return_value
        .execute.return_value
    ) = MagicMock(data=[{"id": existing_id}] if existing_id else None)

    # DELETE
    (
        client.table.return_value
        .delete.return_value
        .eq.return_value
        .execute.return_value
    ) = MagicMock(data=None)

    # INSERT — for the insert path we get a new row back
    (
        client.table.return_value
        .insert.return_value
        .execute.return_value
    ) = MagicMock(
        data=[{"id": "00000000-0000-0000-0000-000000000001"}]
        if not existing_id else None
    )

    return client


# ---- VALID_CATEGORIES ------------------------------------------------------

def test_valid_categories_match_skill_taxonomy():
    expected = {
        "templates", "national_regulations", "internal_policies",
        "examples", "other",
    }
    assert VALID_CATEGORIES == expected


# ---- ingest_document: rejection paths --------------------------------------

def test_ingest_rejects_invalid_category():
    supabase = _mock_supabase()
    embedder = _embedder_with_vectors([[0.0] * 1536])
    with pytest.raises(IngestError, match="Invalid category"):
        asyncio.run(ingest_document(
            supabase, embedder,
            title="x", content="hello", category="bogus", source_uri="manual://x",
        ))


def test_ingest_rejects_empty_title():
    supabase = _mock_supabase()
    embedder = _embedder_with_vectors([[0.0] * 1536])
    with pytest.raises(IngestError, match="title"):
        asyncio.run(ingest_document(
            supabase, embedder,
            title="   ", content="hello", category="templates", source_uri="manual://x",
        ))


def test_ingest_rejects_empty_content():
    supabase = _mock_supabase()
    embedder = _embedder_with_vectors([[0.0] * 1536])
    with pytest.raises(IngestError, match="no content"):
        asyncio.run(ingest_document(
            supabase, embedder,
            title="x", content="   \n\n   ", category="templates", source_uri="manual://x",
        ))


# ---- ingest_document: insert path ------------------------------------------

def test_ingest_insert_path_calls_insert_for_document_and_chunks():
    supabase = _mock_supabase(existing_id=None)
    embedder = _embedder_with_vectors([[0.1] * 1536, [0.2] * 1536])
    content = "فقرة أولى.\n\nفقرة ثانية طويلة بما يكفي." * 30
    result = asyncio.run(ingest_document(
        supabase, embedder,
        title="doc", content=content, category="templates",
        source_uri="manual://doc",
    ))
    assert result["action"] == "inserted"
    assert result["chunks"] >= 1
    # Two inserts: one for the document row, one for the chunks
    assert supabase.table.return_value.insert.call_count == 2


# ---- ingest_document: update path ------------------------------------------

def test_ingest_update_path_does_not_insert_document_row():
    supabase = _mock_supabase(existing_id="11111111-1111-1111-1111-111111111111")
    embedder = _embedder_with_vectors([[0.1] * 1536])
    result = asyncio.run(ingest_document(
        supabase, embedder,
        title="doc", content="some text", category="templates",
        source_uri="manual://doc",
    ))
    assert result["action"] == "updated"
    # One insert only (chunks) — the document row was updated, not inserted
    assert supabase.table.return_value.insert.call_count == 1
    # update was called for the document row
    assert supabase.table.return_value.update.call_count == 1


# ---- delete_document -------------------------------------------------------

def test_delete_document_returns_true_when_removed():
    supabase = _mock_supabase()
    supabase.table.return_value.delete.return_value.eq.return_value.execute.return_value = (
        MagicMock(data=[{"id": "x"}])
    )
    result = asyncio.run(delete_document(supabase, "x"))
    assert result is True


def test_delete_document_returns_false_when_missing():
    supabase = _mock_supabase()
    supabase.table.return_value.delete.return_value.eq.return_value.execute.return_value = (
        MagicMock(data=[])
    )
    result = asyncio.run(delete_document(supabase, "x"))
    assert result is False


# ---- list_documents --------------------------------------------------------

def test_list_documents_passes_optional_category_filter():
    supabase = MagicMock()
    # End-of-chain: q.order(...).execute() returns a known payload
    end = MagicMock(data=[{"id": "a", "title": "x", "category": "templates"}])
    # .select(...) is the start of the chain
    select_mock = MagicMock()
    # .eq() is the next step in the chain (returns something we can keep
    # chaining off of)
    eq_mock = MagicMock()
    # After .eq(), we .order().execute() — the order mock is what calls execute
    select_mock.eq.return_value = eq_mock
    eq_mock.order.return_value.execute.return_value = end
    supabase.table.return_value.select.return_value = select_mock

    out = asyncio.run(list_documents(supabase, category="templates"))
    assert len(out) == 1
    assert eq_mock.order.called
    select_mock.eq.assert_called_once_with("category", "templates")


def test_list_documents_without_filter_skips_eq():
    supabase = MagicMock()
    end = MagicMock(data=[])
    select_mock = MagicMock()
    # Without a filter, .order().execute() is called directly on select_mock
    select_mock.order.return_value.execute.return_value = end
    supabase.table.return_value.select.return_value = select_mock

    asyncio.run(list_documents(supabase))
    assert select_mock.order.called
    # .eq should NOT have been called when no filter is given
    assert not select_mock.eq.called
