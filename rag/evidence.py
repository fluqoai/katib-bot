"""Evidence assembly for the Kateb AI letter pipeline.

After the three retrieval stages have run, this module takes the
hits and assembles a structured `EvidenceBundle` that the generator
will ground every claim in.

The contract is: every claim in the draft must trace back to a
`SourceChunk` here, and the citation object must include the chunk
ID so the compliance reviewer can re-verify the claim later.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rag.retrieval import RetrievalHit, RetrievalResult


@dataclass(slots=True)
class SourceChunk:
    """One cited source — a single chunk from one document.

    The generator uses these to build the `## Sources` block of the
    draft. The compliance reviewer uses them to re-verify every
    claim.
    """
    document_id:   str
    file_id:       str
    version:       int
    chunk_index:   int
    title:         str
    category:      str
    similarity:    float
    content:       str
    citation_label: str = ""     # e.g. "سياسة-الاحتفاظ-بالوثائق-وإتلافها v1 #2"

    def to_citation(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "file_id":     self.file_id,
            "version":     self.version,
            "chunk_index": self.chunk_index,
            "title":       self.title,
            "category":    self.category,
            "similarity":  round(self.similarity, 4),
        }


@dataclass(slots=True)
class EvidenceBundle:
    """The structured context for the generator + compliance reviewer."""
    user_request:    str
    template_hits:   list[SourceChunk] = field(default_factory=list)
    policy_hits:     list[SourceChunk] = field(default_factory=list)
    regulation_hits: list[SourceChunk] = field(default_factory=list)

    def has_template(self) -> bool:
        return bool(self.template_hits)

    def best_template(self) -> SourceChunk | None:
        return self.template_hits[0] if self.template_hits else None

    def all_sources(self) -> list[SourceChunk]:
        return self.template_hits + self.policy_hits + self.regulation_hits

    def to_citations(self) -> list[dict[str, Any]]:
        return [s.to_citation() for s in self.all_sources()]


def _to_chunk(h: RetrievalHit, citation_label: str) -> SourceChunk:
    return SourceChunk(
        document_id=h.document_id,
        file_id=h.file_id or "",
        version=h.version,
        chunk_index=h.chunk_index,
        title=h.title or "",
        category=h.category or "",
        similarity=float(h.similarity or 0.0),
        content=h.content or "",
        citation_label=citation_label,
    )


def _label(h: RetrievalHit) -> str:
    return f"{h.title or 'untitled'} v{h.version} #{h.chunk_index}"


def build_evidence(
    user_request: str,
    templates: RetrievalResult,
    policies: RetrievalResult,
    regulations: RetrievalResult,
    *,
    templates_cap: int = 3,
    policies_cap: int = 6,
    regulations_cap: int = 6,
) -> EvidenceBundle:
    """Take the three RetrievalResults and produce an EvidenceBundle.

    The caps limit how much context we pass to the LLM. The
    top-N-by-similarity is always the right answer — the rest is
    noise.
    """
    return EvidenceBundle(
        user_request=user_request,
        template_hits=[
            _to_chunk(h, _label(h))
            for h in templates.top_n(templates_cap)
        ],
        policy_hits=[
            _to_chunk(h, _label(h))
            for h in policies.top_n(policies_cap)
        ],
        regulation_hits=[
            _to_chunk(h, _label(h))
            for h in regulations.top_n(regulations_cap)
        ],
    )


def format_bundle_for_prompt(bundle: EvidenceBundle) -> str:
    """Render the bundle as a markdown block for the LLM prompt.

    The LLM is explicitly told: every claim must be followed by a
    `[source: ...]` tag. This is what the compliance reviewer will
    parse to verify the draft.
    """
    lines: list[str] = []
    if not bundle.has_template():
        lines.append("## Templates")
        lines.append("_(لم يتم العثور على قالب مطابق — اطلب من المستخدم توضيح نوع الخطاب)_")
        lines.append("")

    if bundle.template_hits:
        lines.append("## Templates (use the closest match as a structural skeleton)")
        for s in bundle.template_hits:
            lines.append(f"### {s.citation_label}  (similarity={s.similarity:.3f})")
            lines.append(s.content.strip())
            lines.append("")

    if bundle.policy_hits:
        lines.append("## Internal policies (cite as `[source: ...]`)")
        for s in bundle.policy_hits:
            lines.append(f"### {s.citation_label}  (similarity={s.similarity:.3f})")
            lines.append(s.content.strip())
            lines.append("")

    if bundle.regulation_hits:
        lines.append("## National regulations (cite as `[source: ...]`)")
        for s in bundle.regulation_hits:
            lines.append(f"### {s.citation_label}  (similarity={s.similarity:.3f})")
            lines.append(s.content.strip())
            lines.append("")

    return "\n".join(lines).strip()


__all__ = ["SourceChunk", "EvidenceBundle", "build_evidence", "format_bundle_for_prompt"]
