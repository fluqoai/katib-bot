"""Run all acceptance tests and print a single report.

Run from project root:
    python tests/acceptance/run_all.py
"""
from __future__ import annotations

# Force UTF-8 stdout so Arabic print() doesn't crash on Windows cp1252
import sys, os
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import json
import shutil
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tests.acceptance.framework import Suite
import tests.acceptance.test_01_template_match as t1
import tests.acceptance.test_02_no_template as t2
import tests.acceptance.test_03_unsupported_claim as t3
import tests.acceptance.test_04_fully_supported as t4
import tests.acceptance.test_05_upload as t5
import tests.acceptance.test_06_reupload_version as t6
import tests.acceptance.test_07_docx_clean as t7
import tests.acceptance.test_08_pdf_availability as t8
import tests.acceptance.test_09_crash_recovery as t9
import tests.acceptance.test_10_ocr_architecture as t10
import tests.acceptance.test_11_template_optional as t11
import tests.acceptance.test_12_ui_options as t12


def _cleanup_test_05_uploads():
    """Best-effort: deactivate the test document from test 05 so we don't
    pollute the index. We only deactivate (is_active=false) so the row
    stays in document_files for forensics."""
    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env")
        from supabase import create_client
        from bot.config import get_settings
        s = get_settings()
        sb = create_client(s.supabase_url, s.resolved_service_key())
        # Find docs with "دليل قبول المتطوعين" in title that are still active
        r = sb.table("documents").select("id, title").like("title", "%دليل قبول المتطوعين%").eq("is_active", True).execute()
        for d in (r.data or []):
            sb.table("documents").update({"is_active": False}).eq("id", d["id"]).execute()
            print(f"  cleanup: deactivated {d['id']} ({d['title']})")
    except Exception as e:
        print(f"  cleanup warning: {e}")


def _cleanup_test_06_uploads():
    """Test 06 re-uploaded to an existing doc, bumping its current_version
    to 2. We do NOT roll that back here (it would invalidate the version
    isolation invariant). But we mark the test so the user knows."""
    pass


def main() -> int:
    suite = Suite(
        name="Kateb acceptance",
        started_at=datetime.now().isoformat(timespec="seconds"),
    )

    print("=" * 70)
    print(" Kateb acceptance test suite")
    print(f" started: {suite.started_at}")
    print("=" * 70)

    tests = [
        t1, t2, t3, t4, t5, t6, t7, t8, t9, t10, t11, t12,
    ]
    for mod in tests:
        print(f"\n[{mod.__name__.split('.')[-1]}]")
        try:
            r = mod.test()
        except Exception as e:  # noqa: BLE001
            r = type("R", (), {})()
            r.name = mod.__name__
            r.passed = False
            r.actual = f"{type(e).__name__}: {e}"
            r.expected = "(test crashed)"
            r.duration_s = 0.0
            r.findings = []
        suite.add(r)

    # Best-effort cleanup of test 05 uploads
    print()
    print("--- cleanup ---")
    _cleanup_test_05_uploads()
    _cleanup_test_06_uploads()

    # Final report
    print()
    print("=" * 70)
    print(" ACCEPTANCE REPORT")
    print("=" * 70)
    print(f"  started: {suite.started_at}")
    print(f"  ended:   {datetime.now().isoformat(timespec='seconds')}")
    print(f"  summary: {suite.summary()}")
    print()
    for r in suite.results:
        sym = "PASS" if r.passed else "FAIL"
        print(f"  [{sym}] {r.name}  ({r.duration_s:.1f}s)")
        print(f"        {r.description}")
        if not r.passed:
            print(f"        expected: {r.expected}")
            print(f"        actual:   {r.actual}")
        for f in r.findings:
            if f.severity in ("bug", "warn"):
                print(f"        {f.severity:>4}: {f.note}")
            else:
                # info — only show first 2 per test to keep report short
                pass

    # Bug summary
    bugs = [(r, f) for r in suite.results for f in r.findings if f.severity == "bug"]
    print()
    print("=" * 70)
    print(f" BUGS FOUND: {len(bugs)}")
    print("=" * 70)
    for r, b in bugs:
        print(f"  [{r.name}] {b.note}")

    # Save the full report as JSON
    out_dir = PROJECT_ROOT / "tests" / "acceptance" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "suite": suite.name,
            "started_at": suite.started_at,
            "ended_at": datetime.now().isoformat(timespec="seconds"),
            "summary": suite.summary(),
            "results": [
                {
                    "name": r.name,
                    "description": r.description,
                    "passed": r.passed,
                    "expected": r.expected,
                    "actual": r.actual,
                    "duration_s": r.duration_s,
                    "findings": [{"severity": f.severity, "note": f.note} for f in r.findings],
                }
                for r in suite.results
            ],
        }, f, ensure_ascii=False, indent=2)
    print(f"\nfull report: {out_path}")

    return 0 if all(r.passed for r in suite.results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
