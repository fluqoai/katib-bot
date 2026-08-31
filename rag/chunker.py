"""Token-aware, paragraph/heading-aware chunker.

Designed for the Liquid LFM embedding model (via OpenRouter) which has a
**512-token** input limit. We leave a ~22% safety margin (target 320, hard
max 400) to absorb:
  * Arabic text tokenises at ~0.43 tokens/char on average, but the ratio
    can spike to ~0.6 on dense legal vocabulary.
  * Tokeniser boundary effects (a long word may split).
  * The model sometimes adds a small prompt overhead.

Strategy
========
1.  **Block-splitting** — split the document into logical blocks:
      * Markdown-style headings (#, ##, …)
      * Arabic heading patterns (الباب, الفصل, المادة, الفرع, الباب الأول, …)
      * Numbered sections (1. , 1- , 1/ , (1))
      * Blank-line paragraph boundaries
    Headings stay attached to the *next* paragraph (so a chunk never starts
    with an orphan heading).
2.  **Greedy packing** — accumulate blocks into a buffer until adding the next
    block would push us over `target_tokens`. If a single block is itself
    over `target_tokens`, flush the buffer (if any), then split that block
    at sentence/word boundaries.
3.  **Hard cap** — any single chunk larger than `hard_max_tokens` is hard-split
    at word boundaries (last resort).
4.  **Overlap** — the last `overlap_tokens` worth of words from the previous
    chunk is *prefixed* onto the next chunk so semantic context carries
    across boundaries. Overlap is bounded to never violate the hard cap.

This is intentionally simple — no language detection, no embeddings-aware
chunks, no sliding window. Predictable and debuggable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import tiktoken


# -- tuning constants ------------------------------------------------------

# Defaults — overridden by env / caller. Values chosen to be safe under
# Liquid LFM's 512-token per-input limit, with margin for the fact that
# tiktoken (cl100k_base) and the model's own BPE tokenizer disagree by
# up to ~50% on dense Arabic legal vocabulary. We hard-cap at 256 even
# though the model allows 512, so a tokenizer mismatch never trips the
# limit.
DEFAULT_TARGET_TOKENS = 200
DEFAULT_OVERLAP_TOKENS = 40
DEFAULT_HARD_MAX_TOKENS = 256


# -- the chunker -----------------------------------------------------------


@dataclass(slots=True)
class ChunkStats:
    """Returned by `chunk_document` for diagnostics."""

    num_blocks: int
    num_chunks: int
    max_tokens: int
    total_tokens: int


def _get_encoder() -> tiktoken.Encoding:
    """cl100k_base is what OpenAI / most modern embedding models use."""
    return tiktoken.get_encoding("cl100k_base")


# Arabic + Persian + Urdu chapter markers and numbered-section patterns.
# These are kept on a single line for easy editing.
_ARABIC_HEADING_RE = re.compile(
    r"^\s*(?:"
    # Arabic ordinal headings: الباب الأول, الفصل الثاني, المادة الثالثة, …
    r"(?:الباب|الفصل|الفرع|المادة|المادة\s+الرقم|المادة\s+رقم|البند|الفقرة|القسم|الجزء)"
    r"(?:\s+(?:الأول(?:ة)?|الثاني(?:ة)?|الثالث(?:ة)?|الرابع(?:ة)?|الخامس(?:ة)?|"
    r"السادس(?:ة)?|السابع(?:ة)?|الثامن(?:ة)?|التاسع(?:ة)?|العاشر(?:ة)?))?"
    r"\s*[:\.\-]?"
    r"|"
    # Markdown-style headings
    r"#{1,6}\s+"
    r"|"
    # Decimal / Arabic-Indic numbered sections: 1. , 1- , 1/ , (1) , ١. , ١-
    r"\(?\s*\d{1,3}\s*[\.\-/]\s*[\)\.]?\s*"
    r")",
    flags=re.MULTILINE,
)


def _split_into_blocks(text: str) -> list[str]:
    """Split text into logical blocks (paragraphs + headings).

    A heading always stays at the START of the next paragraph — never
    as its own block — so the chunker never emits an orphan heading.
    """
    # 1) Normalise whitespace: keep paragraph breaks (double newlines),
    #    collapse runs of spaces/tabs, strip control chars.
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []

    # 2) Split on blank lines first — these are the strongest paragraph
    #    boundaries in DOCX/PDF exports.
    raw_paragraphs = re.split(r"\n\s*\n", text)
    blocks: list[str] = []
    for p in raw_paragraphs:
        p = p.strip()
        if not p:
            continue
        # If a paragraph has internal line breaks (single \n), keep them
        # — they're often sub-clauses in formal Arabic documents. We
        # re-join with single spaces so the chunker sees one logical unit.
        p = re.sub(r"\s*\n\s*", " ", p)
        blocks.append(p)

    # 3) Detach heading prefixes so they attach to the *next* paragraph.
    #    Walk through blocks; if a block looks like a heading AND the
    #    next block exists, prepend the heading to the next block.
    final: list[str] = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if _looks_like_heading(b) and (i + 1) < len(blocks):
            # Glue the heading to the next paragraph
            glued = b.rstrip(" :.-") + "\n" + blocks[i + 1]
            final.append(glued)
            i += 2
        else:
            final.append(b)
            i += 1
    return final


def _looks_like_heading(block: str) -> bool:
    """Heuristic: is this a heading-like block (short, no terminal punctuation)?

    Catches:
      * "الباب الأول" / "الفصل الثاني" / "المادة 5"
      * "# Heading" / "## Subheading"
      * "1. Section title" / "(2) Another"
      * "1- Section"
    """
    if not block or len(block) > 200:
        return False
    first_line = block.splitlines()[0].strip()
    if _ARABIC_HEADING_RE.match(first_line):
        return True
    # A short line that ends with no terminal punctuation
    # and starts with a heading marker.
    if len(first_line) < 80 and not first_line.endswith((".", "؟", "!", ".", "?")):
        if re.match(r"^#{1,6}\s+\S", first_line):
            return True
    return False


def _count_tokens(enc: tiktoken.Encoding, text: str) -> int:
    """Token count. Empty text = 0 tokens (avoids [] overhead)."""
    if not text:
        return 0
    return len(enc.encode(text, disallowed_special=()))


def _split_block_at_words(
    enc: tiktoken.Encoding, block: str, max_tokens: int
) -> list[str]:
    """Hard-split a block that exceeds `max_tokens` at word boundaries."""
    words = block.split(" ")
    if not words:
        return [block]
    parts: list[str] = []
    buf: list[str] = []
    for w in words:
        candidate = " ".join(buf + [w]).strip()
        if _count_tokens(enc, candidate) > max_tokens and buf:
            parts.append(" ".join(buf).strip())
            buf = [w]
        else:
            buf.append(w)
    if buf:
        parts.append(" ".join(buf).strip())
    return [p for p in parts if p]


def _word_tail(text: str, n: int) -> str:
    """Return the last ~`n` *words* of `text` (used to seed overlap)."""
    words = text.split(" ")
    if len(words) <= n:
        return text
    return " ".join(words[-n:])


def chunk_document(
    text: str,
    *,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    hard_max_tokens: int = DEFAULT_HARD_MAX_TOKENS,
    encoder: tiktoken.Encoding | None = None,
) -> tuple[list[str], ChunkStats]:
    """Split `text` into overlapping chunks, each ≤ `hard_max_tokens` tokens.

    Returns (chunks, stats). `chunks` is a list of non-empty strings.
    """
    if hard_max_tokens <= 0:
        raise ValueError("hard_max_tokens must be > 0")
    if target_tokens > hard_max_tokens:
        raise ValueError("target_tokens cannot exceed hard_max_tokens")
    if overlap_tokens < 0:
        raise ValueError("overlap_tokens must be >= 0")

    enc = encoder or _get_encoder()

    # Normalise + block-split
    blocks = _split_into_blocks(text)
    if not blocks:
        return [], ChunkStats(0, 0, 0, 0)

    chunks: list[str] = []
    buf_words: list[str] = []
    buf_tokens = 0

    def flush() -> None:
        """Append the current buffer as a chunk (if non-empty)."""
        nonlocal buf_words, buf_tokens
        if buf_words:
            chunks.append(" ".join(buf_words).strip())
            buf_words = []
            buf_tokens = 0

    for block in blocks:
        block_tokens = _count_tokens(enc, block)
        block_words = block.split(" ")

        # Case A: the entire block fits in the target with the current buf.
        if buf_tokens + block_tokens <= target_tokens:
            buf_words.extend(block_words)
            buf_tokens += block_tokens
            continue

        # Case B: the block itself is bigger than `target_tokens` on its own.
        # We must hard-split it, but first flush any buffered content so the
        # next chunk starts at a clean boundary.
        if block_tokens > target_tokens:
            flush()
            # If even the hard-split block > hard_max_tokens, recursively split.
            sub_parts: list[str] = []
            for sub in _split_block_at_words(enc, block, target_tokens):
                if _count_tokens(enc, sub) > hard_max_tokens:
                    sub_parts.extend(_split_block_at_words(enc, sub, hard_max_tokens))
                else:
                    sub_parts.append(sub)
            # Add overlap seed from previous chunk to first sub-part if possible
            if chunks and overlap_tokens > 0:
                tail = _word_tail(chunks[-1], 60)  # rough word cap
                seed = tail + " " + sub_parts[0]
                if _count_tokens(enc, seed) <= hard_max_tokens:
                    sub_parts[0] = seed
            chunks.extend(p for p in sub_parts if p.strip())
            continue

        # Case C: block fits alone but not with the buffer — flush the
        # buffer, then add the block as the seed of the next chunk.
        flush()
        buf_words.extend(block_words)
        buf_tokens = block_tokens

        # If the buffer is already at the hard cap, flush it once more
        # (defensive — should not normally trigger because case B
        # would have triggered first).
        if buf_tokens > hard_max_tokens:
            flush()

    # Tail
    flush()

    # Apply overlap between consecutive chunks (in addition to the
    # heading-attachment already done during block split). We do this in a
    # second pass so the packing logic stays simple.
    if overlap_tokens > 0 and len(chunks) > 1:
        overlapped: list[str] = []
        for i, c in enumerate(chunks):
            if i == 0:
                overlapped.append(c)
                continue
            prev = chunks[i - 1]
            tail = _word_tail(prev, 80)  # rough cap on words
            seed = tail + " " + c
            if _count_tokens(enc, seed) <= hard_max_tokens:
                overlapped.append(seed)
            else:
                # Trim tail further until it fits
                tail_words = tail.split(" ")
                while tail_words and _count_tokens(enc, " ".join(tail_words) + " " + c) > hard_max_tokens:
                    tail_words.pop(0)
                if tail_words:
                    overlapped.append(" ".join(tail_words) + " " + c)
                else:
                    overlapped.append(c)
        chunks = overlapped

    # Final safety net: nothing exceeds hard_max_tokens.
    safe: list[str] = []
    for c in chunks:
        if _count_tokens(enc, c) > hard_max_tokens:
            safe.extend(_split_block_at_words(enc, c, hard_max_tokens))
        else:
            safe.append(c)
    chunks = [c for c in safe if c.strip()]

    max_t = max((_count_tokens(enc, c) for c in chunks), default=0)
    total_t = sum(_count_tokens(enc, c) for c in chunks)
    return chunks, ChunkStats(
        num_blocks=len(blocks),
        num_chunks=len(chunks),
        max_tokens=max_t,
        total_tokens=total_t,
    )


__all__ = [
    "ChunkStats",
    "DEFAULT_TARGET_TOKENS",
    "DEFAULT_OVERLAP_TOKENS",
    "DEFAULT_HARD_MAX_TOKENS",
    "chunk_document",
]
