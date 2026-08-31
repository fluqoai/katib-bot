"""Old-format .doc loader via LibreOffice (headless).

Approach:
  1. Detect if `soffice` (LibreOffice) is available.
  2. If yes, convert the .doc to .docx in a temp directory and run
     the .docx loader on the result. The original file is NOT modified.
  3. If no, return status='needs_doc_conversion' with a clear error
     message so the caller can report it.

LibreOffice download (for the user to install):
  https://www.libreoffice.org/download/download-libreoffice/
  After install, `soffice` is on PATH (Windows: typically
  `C:\Program Files\LibreOffice\program\soffice.exe`).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from rag.loaders.docx_loader import load_docx
from rag.loaders.types import LoadedDoc


logger = logging.getLogger(__name__)


def _find_soffice() -> str | None:
    """Return the path to soffice, or None if not installed."""
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if exe:
        return exe
    # Common Windows install locations
    for p in (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    ):
        if Path(p).exists():
            return p
    return None


def _convert_doc_to_docx(doc_path: Path, soffice: str) -> Path:
    """Use headless LibreOffice to convert .doc → .docx in a temp dir.

    Returns the path to the converted .docx. Raises on failure.
    """
    out_dir = Path(tempfile.mkdtemp(prefix="kateb-doc-"))
    # --outdir + --convert-to docx
    cmd = [
        soffice,
        "--headless",
        "--convert-to", "docx",
        "--outdir", str(out_dir),
        str(doc_path),
    ]
    logger.info("Converting %s -> docx via soffice", doc_path)
    proc = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"soffice returned {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    # Find the produced file
    candidates = list(out_dir.glob("*.docx"))
    if not candidates:
        raise RuntimeError(f"soffice did not produce a .docx in {out_dir}")
    return candidates[0]


def load_doc(path: Path) -> LoadedDoc:
    """Load a .doc file. Requires LibreOffice (soffice) to be installed.

    The original file is read-only — we never write back to it. The
    converted .docx lives in a temp dir that the OS will eventually
    clean up.
    """
    soffice = _find_soffice()
    if not soffice:
        return LoadedDoc(
            text="",
            status="needs_doc_conversion",
            mime_type="application/msword",
            error_message=(
                "This is an old .doc file. The .doc loader needs LibreOffice "
                "(soffice) on PATH, but it was not found.\n\n"
                "Install LibreOffice: https://www.libreoffice.org/download/download-libreoffice/\n"
                "On Windows, after install, ensure C:\\Program Files\\LibreOffice\\program\\soffice.exe is on PATH, "
                "or just re-run the ingestion script after install."
            ),
            extractor="doc→docx",
        )

    try:
        converted = _convert_doc_to_docx(path, soffice)
    except Exception as e:  # noqa: BLE001
        return LoadedDoc(
            text="",
            status="error",
            mime_type="application/msword",
            error_message=f"LibreOffice conversion failed: {e}",
            extractor="doc→docx",
        )

    # Run the .docx loader on the converted file
    result = load_docx(converted)
    # Tag the result so logs make sense
    return LoadedDoc(
        text=result.text,
        status=result.status,
        mime_type="application/msword",
        error_message=result.error_message,
        extractor="doc→docx (via LibreOffice)",
    )
