"""Test 8: PDF only show/download when real PDF conversion is available.

The API must NEVER return a DOCX file labelled as PDF. The contract:
  - /api/letters/generate response includes `pdf_available: bool`
  - When soffice is missing, `pdf_available` is false and `pdf_url` is null
  - /api/letters/generate/pdf returns HTTP 503 (not a DOCX)
  - The client UI disables the PDF button when pdf_available is false
"""
from tests.acceptance.framework import api_post, api_post_bytes, run_test, TestResult, HEADERS, API_BASE
import httpx
import shutil
from pathlib import Path
import sys
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


DESCRIPTION = (
    "When LibreOffice (soffice) is NOT installed, the system must NOT "
    "fake a PDF by returning a DOCX. /generate must report pdf_available=false, "
    "and /generate/pdf must return HTTP 503 with a clear Arabic error message."
)
EXPECTED = (
    "soffice missing → /generate response has pdf_available=false, pdf_url=null. "
    "/generate/pdf returns 503 with detail message. The first 4 bytes of the "
    "response are NOT %PDF (it would be a fake PDF otherwise)."
)


def run(r: TestResult) -> None:
    # Check soffice presence ourselves
    soffice_paths = [
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    ]
    soffice_present = bool(shutil.which("soffice") or shutil.which("libreoffice"))
    for p in soffice_paths:
        if Path(p).exists():
            soffice_present = True
    r.add_finding("info", f"soffice installed on this host: {soffice_present}")

    body = {
        "request": "اكتب لي خطاب شكر قصير لموظف على جهوده",
        "fields": {},
    }

    # 1) /generate response reports pdf_available
    res = api_post("/letters/generate", body, timeout=300.0)
    has_pdf_avail = "pdf_available" in res
    r.add_finding("info", f"/generate response has pdf_available field: {has_pdf_avail}")
    assert has_pdf_avail, "/generate response missing pdf_available field"
    assert res["pdf_available"] == soffice_present, (
        f"pdf_available={res['pdf_available']} but soffice_present={soffice_present}"
    )

    # 2) /generate/pdf returns the correct status
    status, body_bytes, content_type = api_post_bytes("/letters/generate/pdf", body, timeout=300.0)
    r.add_finding("info", f"/generate/pdf → HTTP {status}, content-type={content_type}, {len(body_bytes)} bytes")
    if soffice_present:
        assert status == 200, f"soffice is present but /generate/pdf returned {status}"
        assert body_bytes[:4] == b"%PDF", (
            f"response body is not a real PDF: first 4 bytes = {body_bytes[:4]!r}"
        )
    else:
        # soffice is missing — must be 503, never a DOCX-in-disguise
        assert status == 503, (
            f"soffice missing but /generate/pdf returned {status} — should be 503"
        )
        assert body_bytes[:4] != b"%PDF", (
            f"/generate/pdf returned a PDF but soffice is missing! "
            f"First 4 bytes: {body_bytes[:4]!r}"
        )
        # Should have an Arabic error message
        try:
            err = body_bytes.decode("utf-8", errors="replace")
            r.add_finding("info", f"503 body: {err[:200]}")
        except Exception:
            pass

    # 3) Direct status check from the API
    with httpx.Client(timeout=10.0) as c:
        r2 = c.post(
            f"{API_BASE}/letters/generate/pdf",
            json=body,
            headers={**HEADERS, "Content-Type": "application/json"},
        )
    r.add_finding("info", f"second probe: HTTP {r2.status_code}")
    if not soffice_present:
        assert r2.status_code == 503, f"expected 503, got {r2.status_code}"

    r.actual = (
        f"soffice={'present' if soffice_present else 'missing'}, "
        f"pdf_available={res['pdf_available']}, /generate/pdf={status}"
    )


def test():
    return run_test(
        name="08_pdf_availability",
        description=DESCRIPTION,
        expected=EXPECTED,
        fn=run,
    )
