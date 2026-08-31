"""Shared test framework for the acceptance suite."""
from __future__ import annotations

import io
import json
import os
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from supabase import create_client
from bot.config import get_settings

API_BASE = "http://127.0.0.1:8000/api"
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "dev")
HEADERS = {"X-Admin-Token": ADMIN_TOKEN}


def _supabase():
    s = get_settings()
    return create_client(s.supabase_url, s.resolved_service_key())


# ---------------------------------------------------------------------------
# Report data
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    severity: str  # "info" | "warn" | "bug"
    note: str


@dataclass
class TestResult:
    name: str
    description: str
    passed: bool
    expected: str
    actual: str
    findings: list[Finding] = field(default_factory=list)
    duration_s: float = 0.0

    def add_finding(self, severity: str, note: str) -> None:
        self.findings.append(Finding(severity, note))


@dataclass
class Suite:
    name: str
    started_at: str
    results: list[TestResult] = field(default_factory=list)
    environment: dict = field(default_factory=dict)

    def add(self, r: TestResult) -> None:
        self.results.append(r)
        sym = "PASS" if r.passed else "FAIL"
        print(f"  [{sym}] {r.name}  ({r.duration_s:.1f}s)")
        if not r.passed:
            print(f"        expected: {r.expected}")
            print(f"        actual:   {r.actual}")
        for f in r.findings:
            print(f"        {f.severity:>4}: {f.note}")

    def summary(self) -> str:
        total = len(self.results)
        passed = sum(1 for r in self.results if r.passed)
        failed = total - passed
        bugs = sum(1 for r in self.results for f in r.findings if f.severity == "bug")
        return f"{passed}/{total} passed, {failed} failed, {bugs} bugs found"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def api_post(path: str, json_body: dict | None = None, timeout: float = 300.0) -> dict:
    """POST JSON to the API, return parsed JSON response."""
    with httpx.Client(timeout=timeout) as c:
        r = c.post(
            f"{API_BASE}{path}",
            json=json_body or {},
            headers={**HEADERS, "Content-Type": "application/json"},
        )
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {path}: {r.text[:300]}")
    if not r.content:
        return {}
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text[:500]}


def api_post_bytes(path: str, json_body: dict | None = None, timeout: float = 300.0) -> tuple[int, bytes, str]:
    """POST JSON and return raw bytes (for DOCX/PDF downloads)."""
    with httpx.Client(timeout=timeout) as c:
        r = c.post(
            f"{API_BASE}{path}",
            json=json_body or {},
            headers={**HEADERS, "Content-Type": "application/json"},
        )
    return r.status_code, r.content, r.headers.get("content-type", "")


def api_get(path: str, timeout: float = 30.0) -> dict:
    with httpx.Client(timeout=timeout) as c:
        r = c.get(f"{API_BASE}{path}", headers=HEADERS)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {path}: {r.text[:300]}")
    return r.json()


def api_post_multipart(path: str, file_path: Path, fields: dict[str, str], timeout: float = 300.0) -> dict:
    # Detect the right content-type from the file extension so the
    # Storage bucket's allowed-MIME list accepts the upload.
    suffix = file_path.suffix.lower()
    mime = {
        ".pdf":  "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc":  "application/msword",
        ".txt":  "text/plain",
        ".md":   "text/markdown",
        ".png":  "image/png",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
    }.get(suffix, "application/octet-stream")
    with httpx.Client(timeout=timeout) as c:
        with open(file_path, "rb") as f:
            r = c.post(
                f"{API_BASE}{path}",
                headers=HEADERS,
                data={**fields},
                files={"file": (file_path.name, f, mime)},
            )
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {path}: {r.text[:300]}")
    return r.json()


# ---------------------------------------------------------------------------
# Small test artifacts (synthetic DOCX / PDF) for upload tests
# ---------------------------------------------------------------------------

def make_minimal_docx(title: str, body: str) -> bytes:
    """Build a minimal valid .docx (no python-docx needed)."""
    # DOCX is a zip with the right file structure
    body_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body>'
        f'<w:p><w:r><w:t xml:space="preserve">{title}</w:t></w:r></w:p>'
        f'<w:p><w:r><w:t xml:space="preserve">{body}</w:t></w:r></w:p>'
        '</w:body>'
        '</w:document>'
    ).encode("utf-8")
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    ).encode("utf-8")
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '</Relationships>'
    ).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", body_xml)
    return buf.getvalue()


def make_minimal_pdf_with_arabic(text: str) -> bytes:
    """Build a minimal valid PDF with text. We don't render real Arabic glyphs
    (that needs a font), but the test OCR-fallback check looks at the
    code path, not the OCR result.
    """
    body = (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
        b"4 0 obj\n<< /Length 60 >>\nstream\n"
        b"BT /F1 12 Tf 50 750 Td (Hello Arabic Letter) Tj ET\nendstream\nendobj\n"
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
        b"xref\n0 6\n0000000000 65535 f\n"
        b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n0\n%%EOF\n"
    )
    return body


# ---------------------------------------------------------------------------
# Test helper
# ---------------------------------------------------------------------------

def run_test(
    name: str,
    description: str,
    expected: str,
    fn: Callable[[TestResult], None],
) -> TestResult:
    """Run a test function, capture result, return TestResult."""
    r = TestResult(
        name=name,
        description=description,
        passed=False,
        expected=expected,
        actual="(not run)",
    )
    t0 = time.time()
    try:
        fn(r)
        if r.actual == "(not run)":
            r.actual = "ok (no failure recorded)"
        r.passed = True
    except AssertionError as e:
        r.actual = f"AssertionError: {e}"
    except Exception as e:  # noqa: BLE001
        r.actual = f"{type(e).__name__}: {e}"
        r.add_finding("bug", f"unexpected exception: {e!r}")
    r.duration_s = time.time() - t0
    return r
