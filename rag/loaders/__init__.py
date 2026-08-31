"""Document loaders for Kateb.

Each loader returns a `LoadedDoc` describing what it found:

- `text`           : extracted text (may be empty)
- `status`         : one of "ok" | "needs_ocr" | "needs_doc_conversion" | "empty" | "error"
- `mime_type`      : detected MIME type
- `error_message`  : populated when status is "error"
- `extractor`      : which loader handled it (e.g. "docx", "pdf+ocr", "doc→docx")

The `load_any(path)` dispatcher picks the right loader based on extension
and falls back gracefully (no crash) when external tools like Tesseract
or LibreOffice are not installed — it just sets the status to "needs_ocr"
or "needs_doc_conversion" so the caller can report it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from rag.loaders.types import LoadedDoc

# File extensions the dispatcher knows how to handle. Used by the folder
# walker to filter down to supported files only.
SUPPORTED_EXTS: frozenset[str] = frozenset({
    ".md", ".markdown", ".txt",
    ".docx",
    ".doc",
    ".pdf",
})


__all__ = ["LoadedDoc", "load_any", "SUPPORTED_EXTS"]


logger = logging.getLogger(__name__)


# -- the dispatcher --------------------------------------------------------

def load_any(path: str | Path) -> LoadedDoc:
    """Pick a loader based on file extension. Never raises — returns a
    `LoadedDoc` with an error status if extraction failed."""
    p = Path(path)
    if not p.exists():
        return LoadedDoc(
            text="",
            status="error",
            mime_type=None,
            error_message=f"file not found: {p}",
            extractor=None,
        )

    ext = p.suffix.lower()
    try:
        if ext in (".md", ".markdown", ".txt"):
            from rag.loaders.text_loader import load_text
            return load_text(p)
        if ext == ".docx":
            from rag.loaders.docx_loader import load_docx
            return load_docx(p)
        if ext == ".doc":
            from rag.loaders.doc_loader import load_doc
            return load_doc(p)
        if ext == ".pdf":
            from rag.loaders.pdf_loader import load_pdf
            return load_pdf(p)
    except Exception as e:  # noqa: BLE001
        logger.exception("Loader crashed for %s", p)
        return LoadedDoc(
            text="",
            status="error",
            mime_type=None,
            error_message=f"{type(e).__name__}: {e}",
            extractor=ext,
        )

    return LoadedDoc(
        text="",
        status="error",
        mime_type=None,
        error_message=f"unsupported file extension: {ext!r} (file: {p.name})",
        extractor=ext,
    )
