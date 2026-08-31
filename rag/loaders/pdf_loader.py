"""PDF loader with text + OCR fallback.

Strategy:
  1. Try PyMuPDF (fitz) for direct text extraction.
  2. If that yields < 50 chars total (typical of scanned/image PDFs),
     try OCR via pytesseract + Tesseract.
  3. If Tesseract is not installed, return status='needs_ocr' with
     a clear install message.

Tesseract install:
  - Windows: download the installer at
    https://github.com/UB-Mannheim/tesseract/wiki
    During install, tick "Additional language data" → Arabic (ara).
  - After install, ensure tesseract.exe is on PATH (default install
    location: C:\Program Files\Tesseract-OCR\tesseract.exe).
  - Verify Arabic is available: `tesseract --list-langs` should include `ara`.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from rag.loaders.types import LoadedDoc


logger = logging.getLogger(__name__)


MIN_TEXT_FOR_OK = 50  # below this, we assume the PDF is image-only and try OCR


def _extract_text_with_pymupdf(path: Path) -> str:
    """Try direct text extraction with PyMuPDF."""
    import fitz

    doc = fitz.open(str(path))
    try:
        parts = [page.get_text() for page in doc]
    finally:
        doc.close()
    return "\n\n".join(parts).strip()


def _tesseract_available() -> tuple[bool, str | None, bool]:
    """Check if Tesseract is installed and if Arabic is available.

    Returns (is_available, tesseract_path, arabic_available).
    """
    exe = shutil.which("tesseract")
    if not exe:
        for p in (
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            r"C:\Users\khayrat\AppData\Local\Programs\Tesseract-OCR\tesseract.exe",
        ):
            if Path(p).exists():
                exe = p
                break
    if not exe:
        return False, None, False

    # Check if Arabic is in the list of supported languages
    try:
        out = subprocess.run(
            [exe, "--list-langs"],
            check=True, capture_output=True, text=True, timeout=10,
        )
        langs = (out.stdout + out.stderr).lower()
        arabic_ok = "ara" in langs
    except Exception:  # noqa: BLE001
        arabic_ok = False

    return True, exe, arabic_ok


def _ocr_with_tesseract(path: Path, tesseract: str) -> str:
    """OCR a PDF via Tesseract.

    Two-step: render each page to PNG with PyMuPDF, then OCR each PNG
    with Tesseract using Arabic + English language packs.
    """
    import fitz
    from PIL import Image
    import io

    # Build Tesseract command prefix
    # `-l ara+eng` assumes both language packs are installed. Fallback to
    # just `ara` if English isn't available.
    lang = "ara+eng"  # Tesseract will use whatever is available

    doc = fitz.open(str(path))
    try:
        page_texts: list[str] = []
        for i, page in enumerate(doc):
            # Render page at 2x for better OCR accuracy
            mat = fitz.Matrix(2.0, 2.0)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img_bytes = pix.tobytes("png")
            img = Image.open(io.BytesIO(img_bytes))
            # Save to a temp file and call tesseract
            tmp_img = Path(tempfile_path := Path.cwd() / f"_kateb_ocr_{i}.png")
            try:
                img.save(tmp_img, format="PNG")
                proc = subprocess.run(
                    [tesseract, str(tmp_img), "-", "-l", lang],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                page_text = proc.stdout.strip()
                if proc.returncode != 0 and not page_text:
                    # Try with just ara
                    proc2 = subprocess.run(
                        [tesseract, str(tmp_img), "-", "-l", "ara"],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    page_text = proc2.stdout.strip()
                if page_text:
                    page_texts.append(page_text)
            finally:
                if tmp_img.exists():
                    tmp_img.unlink()
    finally:
        doc.close()

    return "\n\n".join(page_texts).strip()


def load_pdf(path: Path) -> LoadedDoc:
    """Extract text from a PDF, with OCR fallback for image-only PDFs."""
    mime = "application/pdf"

    # 1) Try direct text extraction
    try:
        text = _extract_text_with_pymupdf(path)
    except Exception as e:  # noqa: BLE001
        return LoadedDoc(
            text="",
            status="error",
            mime_type=mime,
            error_message=f"PyMuPDF failed: {type(e).__name__}: {e}",
            extractor="pdf",
        )

    if len(text) >= MIN_TEXT_FOR_OK:
        return LoadedDoc(
            text=text,
            status="ok",
            mime_type=mime,
            extractor="pdf",
        )

    # 2) Text was too short — try OCR
    available, tesseract, arabic_ok = _tesseract_available()
    if not available:
        return LoadedDoc(
            text="",  # never return a partial result
            status="needs_ocr",
            mime_type=mime,
            error_message=(
                f"This PDF appears to be image-only "
                f"({len(text)} chars of extractable text). Tesseract OCR is not installed.\n\n"
                "Install Tesseract: https://github.com/UB-Mannheim/tesseract/wiki\n"
                "On Windows: download the installer, tick 'Additional language data' → "
                "'Arabic' (ara.traineddata), and ensure tesseract.exe is on PATH.\n"
                "Verify: `tesseract --list-langs` should include `ara`."
            ),
            extractor="pdf",
        )

    if not arabic_ok:
        return LoadedDoc(
            text="",
            status="needs_ocr",
            mime_type=mime,
            error_message=(
                "Tesseract is installed but the Arabic language pack (ara.traineddata) "
                "is missing. Re-run the Tesseract installer and tick "
                "'Additional language data' → 'Arabic'."
            ),
            extractor="pdf",
        )

    try:
        ocr_text = _ocr_with_tesseract(path, tesseract)  # type: ignore[arg-type]
    except Exception as e:  # noqa: BLE001
        return LoadedDoc(
            text="",
            status="error",
            mime_type=mime,
            error_message=f"OCR failed: {type(e).__name__}: {e}",
            extractor="pdf+ocr",
        )

    if not ocr_text.strip():
        return LoadedDoc(
            text="",
            status="needs_ocr",
            mime_type=mime,
            error_message=(
                "Tesseract ran but produced no text. The PDF may be too "
                "low-resolution or the Arabic pack may need a reinstall."
            ),
            extractor="pdf+ocr",
        )

    return LoadedDoc(
        text=ocr_text,
        status="ok",
        mime_type=mime,
        extractor="pdf+ocr",
    )


# -- local helper, avoids circular import with tempfile ----------------
import tempfile  # noqa: E402
