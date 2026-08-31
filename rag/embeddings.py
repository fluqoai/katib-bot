"""Embedding client — supports OpenAI and Google Generative AI."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Iterable

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from rag.types import Category  # noqa: F401  (re-exported)


logger = logging.getLogger(__name__)


class EmbeddingClient:
    """Provider-agnostic embedding client.

    Pick the provider at construction time via `provider='openai' | 'google'`.
    The dimension of the returned vectors is provider/model specific and is
    exposed via the `dimension` attribute so the schema can be validated.
    """

    OPENAI_DIMENSIONS = {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "text-embedding-ada-002": 1536,
    }
    GOOGLE_DIMENSIONS = {
        "text-embedding-004": 768,
        "embedding-001": 768,
    }
    # OpenRouter routes many embedding models. We don't hardcode them all —
    # `OPENROUTER_EMBEDDING_DIM` (env) overrides, otherwise we call the
    # model once to discover the dimension.
    OPENROUTER_KNOWN = {
        "liquid/lfm-2.5-embedding-350m:free": 1024,
    }

    # Cap the number of texts sent to the embedding API in one HTTP
    # call. OpenRouter + Liquid LFM rejects very large batches
    # (HTTP 400) so we chunk the work. 25 is conservative and safe.
    _EMBED_BATCH = 20
    # Pause between batches to avoid OpenRouter free-tier rate limiting
    # (HTTP 429). 0.8s keeps us under the free tier's ~20 req/min
    # ceiling even for back-to-back 1.5MB docs.
    _BATCH_SLEEP_S = 0.8
    # On a 429, sleep this long before continuing with the next batch.
    _RATE_LIMIT_BACKOFF_S = 15.0

    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str,
        dimension: int | None = None,
    ) -> None:
        if provider not in ("openai", "google", "openrouter"):
            raise ValueError(f"Unsupported embedding provider: {provider}")
        self.provider = provider
        self.model = model
        self.api_key = api_key
        if dimension is not None:
            self.dimension = dimension
        else:
            self.dimension = self._resolve_dimension()
        self._client = httpx.AsyncClient(timeout=30.0)

    def _resolve_dimension(self) -> int:
        if self.provider == "openai":
            try:
                return self.OPENAI_DIMENSIONS[self.model]
            except KeyError as e:
                raise ValueError(
                    f"Unknown OpenAI embedding model {self.model!r}. "
                    f"Known: {list(self.OPENAI_DIMENSIONS)}"
                ) from e
        if self.provider == "google":
            try:
                return self.GOOGLE_DIMENSIONS[self.model]
            except KeyError as e:
                raise ValueError(
                    f"Unknown Google embedding model {self.model!r}. "
                    f"Known: {list(self.GOOGLE_DIMENSIONS)}"
                ) from e
        if self.model in self.OPENROUTER_KNOWN:
            return self.OPENROUTER_KNOWN[self.model]
        # Unknown OpenRouter model — caller must set OPENROUTER_EMBEDDING_DIM
        raise ValueError(
            f"Unknown OpenRouter embedding model {self.model!r}. "
            f"Set OPENROUTER_EMBEDDING_DIM in .env, or add it to "
            f"EmbeddingClient.OPENROUTER_KNOWN."
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def embed_text(self, text: str) -> list[float]:
        """Embed a single text. Returns a `dimension`-length vector.

        Bypasses the batch path so a single text doesn't recurse into
        a fallback inside itself (which caused `RecursionError` in
        earlier runs).
        """
        if not text or not text.strip():
            raise ValueError("Cannot embed empty text")
        if self.provider == "openai":
            out = await self._embed_openai([text])
        elif self.provider == "google":
            out = await self._embed_google([text])
        elif self.provider == "openrouter":
            out = await self._embed_openrouter([text])
        else:
            raise ValueError(f"Unsupported provider: {self.provider}")
        return out[0]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Order is preserved.

        Chunks that would fail the whole batch (e.g. too long for the
        model) are split off and embedded one-by-one. The good ones
        still go through.

        Batching: we never send more than `_EMBED_BATCH` texts in one
        API call. The OpenRouter + Liquid LFM endpoint silently
        returns HTTP 400 for very large batches (~1000+ inputs), so
        we chunk the work.
        """
        if not texts:
            return []

        # Pre-filter oversized texts (best-effort heuristic: > 4000
        # chars is almost certainly over the Liquid LFM 512-token limit).
        safe: list[tuple[int, str]] = []  # (original_index, text)
        oversized: list[tuple[int, str]] = []
        for i, t in enumerate(texts):
            if len(t) > 4000:
                oversized.append((i, t))
            else:
                safe.append((i, t))

        results: list[list[float] | None] = [None] * len(texts)

        # Batched safe path
        if safe:
            for batch_start in range(0, len(safe), self._EMBED_BATCH):
                batch = safe[batch_start : batch_start + self._EMBED_BATCH]
                batch_texts = [t for _, t in batch]
                # Retry this single batch a few times on 429 before giving up.
                vecs = await self._embed_batch_with_backoff(batch_texts)
                if len(vecs) != len(batch):
                    raise ValueError(
                        f"embedder returned {len(vecs)} vectors for "
                        f"{len(batch)} texts in batch"
                    )
                for (orig_idx, _), vec in zip(batch, vecs):
                    results[orig_idx] = vec
                # Gentle pace to avoid OpenRouter free-tier 429s
                if self._BATCH_SLEEP_S > 0 and (batch_start + self._EMBED_BATCH) < len(safe):
                    await asyncio.sleep(self._BATCH_SLEEP_S)

        # Per-text fallback for oversized ones
        for idx, t in oversized:
            try:
                results[idx] = await self.embed_text(t)
            except Exception as e:  # noqa: BLE001
                logger.error("Could not embed oversized chunk %d (%d chars): %s", idx, len(t), e)
                results[idx] = [0.0] * self.dimension  # placeholder; the search will skip it

        return [r if r is not None else [0.0] * self.dimension for r in results]

    async def _embed_openai(self, texts: list[str]) -> list[list[float]]:
        url = "https://api.openai.com/v1/embeddings"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {"model": self.model, "input": texts}
        r = await self._client.post(url, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
        return [item["embedding"] for item in data["data"]]

    async def _embed_google(self, texts: list[str]) -> list[list[float]]:
        # Google Generative AI embedContent — batch via single calls per text
        # (the public API does not support batching reliably for all models).
        results: list[list[float]] = []
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:batchEmbedContents?key={self.api_key}"
        )
        payload = {
            "requests": [
                {
                    "model": f"models/{self.model}",
                    "content": {"parts": [{"text": t}]},
                }
                for t in texts
            ]
        }
        r = await self._client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()
        for emb in data["embeddings"]:
            results.append(emb["values"])
        return results

    async def _embed_openrouter(self, texts: list[str]) -> list[list[float]]:
        """OpenRouter's /embeddings endpoint — OpenAI-compatible."""
        url = "https://openrouter.ai/api/v1/embeddings"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "input": texts if len(texts) > 1 else texts[0],
            "encoding_format": "float",
        }
        r = await self._client.post(url, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
        results = [item["embedding"] for item in data["data"]]
        # Sanity check: the dimension must match what we declared
        if results and len(results[0]) != self.dimension:
            raise ValueError(
                f"OpenRouter returned {len(results[0])}-dim vectors but the "
                f"schema/embedder is configured for {self.dimension}. Update "
                f"OPENROUTER_EMBEDDING_DIM in .env (and the schema column)."
            )
        return results

    async def _embed_batch_with_backoff(
        self, texts: list[str], max_attempts: int = 4, _depth: int = 0
    ) -> list[list[float]]:
        """Embed one batch with manual backoff for 429/5xx errors.

        The default `tenacity` retry is for the whole `embed_texts` call,
        which means a single bad batch kills all the work. Here we retry
        just the offending batch with an exponential pause.

        On HTTP 400 (e.g. one input is too long for the model), we
        recursively split the batch in half and try each half separately
        so the bad input is isolated and embedded with a smaller batch.
        _depth caps the recursion to avoid infinite splits.
        """
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                if self.provider == "openai":
                    return await self._embed_openai(texts)
                if self.provider == "google":
                    return await self._embed_google(texts)
                if self.provider == "openrouter":
                    return await self._embed_openrouter(texts)
                raise ValueError(f"Unsupported provider: {self.provider}")
            except httpx.HTTPStatusError as e:
                code = e.response.status_code if e.response is not None else 0
                last_exc = e
                if code in (429, 500, 502, 503, 504):
                    wait = self._RATE_LIMIT_BACKOFF_S * attempt
                    logger.warning(
                        "embed batch got HTTP %d (attempt %d/%d) — sleeping %.1fs",
                        code, attempt, max_attempts, wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                if code == 400 and len(texts) > 1 and _depth < 4:
                    # Likely one input is too long; split and recurse
                    mid = len(texts) // 2
                    logger.warning(
                        "embed batch got HTTP 400 (depth %d) — splitting %d into %d + %d",
                        _depth, len(texts), mid, len(texts) - mid,
                    )
                    left = await self._embed_batch_with_backoff(
                        texts[:mid], max_attempts, _depth=_depth + 1
                    )
                    right = await self._embed_batch_with_backoff(
                        texts[mid:], max_attempts, _depth=_depth + 1
                    )
                    return [*left, *right]
                raise
        raise last_exc  # type: ignore[misc]

    async def aclose(self) -> None:
        await self._client.aclose()


def from_env() -> EmbeddingClient:
    """Construct an EmbeddingClient from environment variables."""
    provider = os.environ.get("EMBEDDING_PROVIDER", "openai")
    if provider == "openai":
        return EmbeddingClient(
            provider="openai",
            model=os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
            api_key=os.environ["OPENAI_API_KEY"],
        )
    if provider == "google":
        return EmbeddingClient(
            provider="google",
            model=os.environ.get("GOOGLE_EMBEDDING_MODEL", "text-embedding-004"),
            api_key=os.environ["GOOGLE_API_KEY"],
        )
    if provider == "openrouter":
        # Honour explicit OPENROUTER_EMBEDDING_DIM, otherwise look up the
        # built-in table, otherwise ask the constructor to fail loudly.
        dim_env = os.environ.get("OPENROUTER_EMBEDDING_DIM")
        dimension = int(dim_env) if dim_env else None
        return EmbeddingClient(
            provider="openrouter",
            model=os.environ.get(
                "OPENROUTER_EMBEDDING_MODEL",
                "liquid/lfm-2.5-embedding-350m:free",
            ),
            api_key=os.environ["OPENROUTER_API_KEY"],
            dimension=dimension,
        )
    raise ValueError(f"Unsupported EMBEDDING_PROVIDER: {provider!r}")
