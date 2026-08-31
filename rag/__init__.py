"""RAG pipeline for the Kateb bot.

The bot receives a draft request from Telegram, runs retrieval against
Supabase `pgvector`, then calls the LLM with the system prompt defined in
`prompts.py` (which wraps the `arabic-official-correspondence` skill).
"""

from rag.search import DocumentSearcher, SearchResult
from rag.embeddings import EmbeddingClient
from rag.generator import (
    LetterGenerator as DraftGenerator,    # legacy name (chat endpoint)
    LetterGenerator,
    GeneratedDraft,
)
from rag.prompts import load_skill_body, build_system_prompt

__all__ = [
    "DocumentSearcher",
    "SearchResult",
    "EmbeddingClient",
    "LetterGenerator",
    "DraftGenerator",        # alias kept for the chat endpoint
    "GeneratedDraft",
    "load_skill_body",
    "build_system_prompt",
]
