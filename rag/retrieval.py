"""Typed retrieval for the Kateb AI letter-generation pipeline.

This is the only module that talks to `match_documents`. It guarantees
two things the rest of the pipeline relies on:

1. **Version isolation** — every hit comes from the document's
   CURRENT version. Old versions' chunks never appear, even when
   they share document_chunks rows.
2. **Category separation** — the three call sites (`template`,
   `policy`, `regulation`) each request a single category so the
   generator cannot mix sources by accident.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from supabase import Client

from bot.config import get_settings


logger = logging.getLogger(__name__)


CategoryKind = Literal["templates", "internal_policies", "national_regulations"]


class RetrievalHit:
    """A single chunk returned by the match_documents RPC."""

    __slots__ = (
        "id", "document_id", "file_id", "version", "chunk_index",
        "content", "category", "title", "source_uri", "similarity",
    )

    def __init__(self, row: dict[str, Any]) -> None:
        self.id            = row["id"]
        self.document_id   = row["document_id"]
        self.file_id       = row.get("file_id") or row.get("document_file_id")
        self.version       = row.get("version", 0)
        self.chunk_index   = row["chunk_index"]
        self.content       = row["content"]
        self.category      = row["category"]
        self.title         = row["title"]
        self.source_uri    = row.get("source_uri")
        self.similarity    = row.get("similarity", 0.0)

    def to_citation(self) -> dict[str, Any]:
        """A serialisable citation object for the generator's output."""
        return {
            "document_id": self.document_id,
            "file_id":     self.file_id,
            "version":     self.version,
            "chunk_index": self.chunk_index,
            "title":       self.title,
            "category":    self.category,
            "similarity":  round(float(self.similarity), 4),
        }

    def __repr__(self) -> str:
        return f"<Hit {self.title!r} v{self.version} sim={self.similarity:.3f}>"


class RetrievalResult:
    """All hits for one retrieval call."""

    def __init__(self, kind: CategoryKind, query: str, hits: list[RetrievalHit]) -> None:
        self.kind   = kind
        self.query  = query
        self.hits   = hits

    @property
    def top(self) -> RetrievalHit | None:
        return self.hits[0] if self.hits else None

    def top_n(self, n: int) -> list[RetrievalHit]:
        return self.hits[:n]

    def all_citations(self) -> list[dict[str, Any]]:
        return [h.to_citation() for h in self.hits]


async def _embed_one(embedder, text: str) -> list[float]:
    return await embedder.embed_text(text)


async def _rpc(
    supabase: Client,
    embedding: list[float],
    *,
    category: CategoryKind,
    threshold: float,
    top_k: int,
) -> list[RetrievalHit]:
    """Call match_documents and return typed hits."""
    resp = supabase.rpc("match_documents", {
        "query_embedding": embedding,
        "match_threshold":  threshold,
        "match_count":      top_k,
        "filter_category":  category,
    }).execute()
    rows = resp.data or []
    return [RetrievalHit(r) for r in rows]


async def retrieve_templates(
    embedder,
    supabase: Client,
    query: str,
    *,
    embedding: list[float] | None = None,
    top_k: int = 5,
    threshold: float = 0.20,
) -> RetrievalResult:
    """Find templates whose content best matches the user's request.

    The threshold here is deliberately low (0.20) — the Liquid LFM
    embedding model produces lower cross-encoder-style similarities
    for Arabic than for English, and the user wants to see AT LEAST
    one template even when the wording is loose. The generator
    picks the best one from the top few hits.
    """
    vec = embedding if embedding is not None else await _embed_one(embedder, query)
    hits = await _rpc(
        supabase, vec,
        category="templates", threshold=threshold, top_k=top_k,
    )
    return RetrievalResult("templates", query, hits)


async def retrieve_policies(
    embedder,
    supabase: Client,
    query: str,
    *,
    embedding: list[float] | None = None,
    top_k: int = 8,
    threshold: float = 0.25,
) -> RetrievalResult:
    """Find internal policies relevant to the user's request."""
    vec = embedding if embedding is not None else await _embed_one(embedder, query)
    hits = await _rpc(
        supabase, vec,
        category="internal_policies", threshold=threshold, top_k=top_k,
    )
    return RetrievalResult("internal_policies", query, hits)


async def retrieve_regulations(
    embedder,
    supabase: Client,
    query: str,
    *,
    embedding: list[float] | None = None,
    top_k: int = 8,
    threshold: float = 0.25,
) -> RetrievalResult:
    """Find national regulations relevant to the user's request."""
    vec = embedding if embedding is not None else await _embed_one(embedder, query)
    hits = await _rpc(
        supabase, vec,
        category="national_regulations", threshold=threshold, top_k=top_k,
    )
    return RetrievalResult("national_regulations", query, hits)


async def retrieve_all_three(
    embedder,
    supabase: Client,
    query: str,
    *,
    templates_k: int = 5,
    policies_k: int = 8,
    regulations_k: int = 8,
) -> dict[str, RetrievalResult]:
    """Convenience: run the three retrievals in parallel."""
    t, p, r = await asyncio.gather(
        retrieve_templates(embedder, supabase, query, top_k=templates_k),
        retrieve_policies(embedder, supabase, query, top_k=policies_k),
        retrieve_regulations(embedder, supabase, query, top_k=regulations_k),
    )
    return {"templates": t, "policies": p, "regulations": r}


__all__ = [
    "CategoryKind",
    "RetrievalHit",
    "RetrievalResult",
    "retrieve_templates",
    "retrieve_policies",
    "retrieve_regulations",
    "retrieve_all_three",
]
