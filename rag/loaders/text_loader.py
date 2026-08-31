"""Plain-text / Markdown loader."""

from __future__ import annotations

from pathlib import Path

from rag.loaders.types import LoadedDoc


def load_text(path: Path) -> LoadedDoc:
    """Read a .md, .markdown, or .txt file as UTF-8."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return LoadedDoc(
            text="",
            status="empty",
            mime_type="text/plain",
            error_message="file is empty",
            extractor="text",
        )
    return LoadedDoc(
        text=text,
        status="ok",
        mime_type="text/plain",
        extractor="text",
    )
