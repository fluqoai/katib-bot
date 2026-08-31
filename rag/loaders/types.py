"""Shared type for the loader package."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional


LoaderStatus = Literal[
    "ok",              # text extracted, ready to embed
    "needs_ocr",       # scanned PDF or image-only — needs Tesseract
    "needs_doc_conversion",  # old .doc file — needs LibreOffice
    "empty",           # file is empty or has no extractable text
    "error",           # unexpected failure
]


@dataclass(slots=True)
class LoadedDoc:
    text: str
    status: LoaderStatus
    mime_type: Optional[str] = None
    error_message: Optional[str] = None
    extractor: Optional[str] = None  # which sub-loader ran ("docx", "pdf+ocr", "doc→docx", ...)

    @property
    def ok(self) -> bool:
        return self.status == "ok" and bool(self.text and self.text.strip())

    @property
    def needs_external_tool(self) -> bool:
        return self.status in ("needs_ocr", "needs_doc_conversion")
