"""Shared dataclasses for the RAG pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


Category = Literal[
    "templates",
    "national_regulations",
    "internal_policies",
    "examples",
    "other",
]


@dataclass(slots=True)
class SearchResult:
    """A single hit from `match_documents`."""

    id: str
    document_id: str
    chunk_index: int
    content: str
    category: Category
    title: str
    source_uri: str | None
    similarity: float


@dataclass(slots=True)
class RetrievedContext:
    """The chunks grouped by category, ready to be injected into the prompt."""

    templates: list[SearchResult] = field(default_factory=list)
    national_regulations: list[SearchResult] = field(default_factory=list)
    internal_policies: list[SearchResult] = field(default_factory=list)
    examples: list[SearchResult] = field(default_factory=list)
    other: list[SearchResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(
            len(getattr(self, cat))
            for cat in ("templates", "national_regulations",
                        "internal_policies", "examples", "other")
        )

    def is_empty(self) -> bool:
        return self.total == 0

    def by_category(self, category: Category) -> list[SearchResult]:
        return getattr(self, category)


@dataclass(slots=True)
class PlaceholderField:
    """A `{{…}}` field the bot flagged in the draft for the user to fill."""

    field: str
    required: bool = True
    hint: str | None = None


@dataclass(slots=True)
class GeneratedDraft:
    """The LLM's response, after parsing for placeholders + sources."""

    body: str
    placeholders: list[PlaceholderField] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    raw_model_response: str | None = None
