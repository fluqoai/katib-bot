"""Similarity search over Supabase `document_chunks` via the match_documents RPC."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from supabase import Client, create_client

from rag.embeddings import EmbeddingClient
from rag.types import Category, RetrievedContext, SearchResult


class DocumentSearcher:
    """Wraps the Supabase `match_documents` RPC.

    The skill requires three categories to be searched on every request:
    `templates`, `national_regulations`, `internal_policies`. This class
    runs the same query for each category, in parallel, and groups the
    results so the prompt builder can present them by category.
    """

    def __init__(
        self,
        supabase_client: Client,
        embedding_client: EmbeddingClient,
        match_threshold: float = 0.65,
        match_count: int = 5,
    ) -> None:
        self.client = supabase_client
        self.embeddings = embedding_client
        self.match_threshold = match_threshold
        self.match_count = match_count

    async def search(
        self,
        query: str,
        categories: list[Category] | None = None,
    ) -> RetrievedContext:
        """Embed `query` and run `match_documents` once per category."""
        if categories is None:
            categories = ["templates", "national_regulations", "internal_policies"]
        vector = await self.embeddings.embed_text(query)

        ctx = RetrievedContext()
        for cat in categories:
            rows = await self._rpc(vector, cat)
            setattr(ctx, cat, [self._to_result(r) for r in rows])
        return ctx

    async def _rpc(self, vector: list[float], category: Category) -> list[dict[str, Any]]:
        """Call the match_documents RPC for one category."""
        # supabase-py is sync; offload to a thread to keep the bot responsive.
        import asyncio
        return await asyncio.to_thread(
            self._rpc_sync, vector, category,
        )

    def _rpc_sync(self, vector: list[float], category: Category) -> list[dict[str, Any]]:
        resp = (
            self.client.rpc(
                "match_documents",
                {
                    "query_embedding": vector,
                    "match_threshold": self.match_threshold,
                    "match_count": self.match_count,
                    "filter_category": category,
                },
            )
            .execute()
        )
        return resp.data or []

    @staticmethod
    def _to_result(row: dict[str, Any]) -> SearchResult:
        return SearchResult(
            id=row["id"],
            document_id=row["document_id"],
            chunk_index=int(row["chunk_index"]),
            content=row["content"],
            category=row["category"],
            title=row["title"],
            source_uri=row.get("source_uri"),
            similarity=float(row["similarity"]),
        )


def from_env() -> tuple[Client, DocumentSearcher]:
    """Construct a Supabase client + DocumentSearcher from environment.

    Key preference order:
    1. SUPABASE_SERVICE_ROLE_KEY  (legacy JWT — works with supabase-py 2.10)
    2. SUPABASE_SECRET_KEY         (new sb_secret_... — requires supabase-py >= 2.20)

    When the legacy key is set, we use it (it's the most compatible).
    """
    import os
    url = os.environ["SUPABASE_URL"]
    key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_SECRET_KEY")
    )
    client = create_client(url, key)
    from rag.embeddings import from_env as _emb_from_env
    embedder = _emb_from_env()
    return client, DocumentSearcher(client, embedder)


# Re-export for convenience
__all__ = ["DocumentSearcher", "SearchResult", "from_env"]
