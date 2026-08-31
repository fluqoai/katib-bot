"""Test 12: UI options (Phase 4) — use_legacy_template + style_overrides.

Verifies the new Phase 4 API contract end-to-end:
  * When the caller does NOT set use_legacy_template, the style-driven
    path is used; the response includes a non-null `letter_style`
    and a null `template_profile`.
  * When the caller sets style_overrides, the resolved style reflects
    them.
  * When the caller sets use_legacy_template=True, the legacy path
    is used; the response includes a non-null `template_profile`
    and a null `letter_style`.
  * Unknown style_overrides keys are silently dropped (forward
    compatibility).
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from tests.acceptance.framework import api_post, run_test, TestResult


DESCRIPTION = (
    "The /generate endpoint accepts the new Phase 4 options "
    "(use_legacy_template, style_overrides) and exposes the resolved "
    "LetterStyle in the response so the client UI can show the user "
    "what formatting was applied."
)
EXPECTED = (
    "Default → style-driven (letter_style present, template_profile null); "
    "with style_overrides → overrides applied; with use_legacy_template=True "
    "→ template_profile present, letter_style null; unknown override keys → "
    "silently dropped, defaults applied."
)


def run(r: TestResult) -> None:
    # --- Case 1: default (style-driven) ----------------------------------
    res = api_post("/letters/generate", {
        "request": "اكتب لي خطابًا رسميًا لدعوة أعضاء الجمعية العمومية",
        "fields": {},
        "upload_outputs": False,
    }, timeout=300.0)
    assert res.get("template_profile") is None, "default path should not have template_profile"
    ls = res.get("letter_style")
    assert ls is not None, "default path must include letter_style"
    assert ls["include_basmala"] is True, "default include_basmala should be True"
    assert ls["include_date_row"] is True, "default include_date_row should be True"
    assert ls["include_recipient_block"] is True, "default include_recipient_block should be True"
    assert ls["include_signature_block"] is True, "default include_signature_block should be True"
    r.add_finding("info", "default path: style-driven, letter_style present, all sections on")

    # --- Case 2: style_overrides (turn off basmala + signature) ---------
    res = api_post("/letters/generate", {
        "request": "اكتب لي خطابًا رسميًا لدعوة أعضاء الجمعية العمومية",
        "fields": {},
        "upload_outputs": False,
        "style_overrides": {
            "include_basmala": False,
            "include_signature_block": False,
        },
    }, timeout=300.0)
    ls = res.get("letter_style")
    assert ls is not None, "override path must include letter_style"
    assert ls["include_basmala"] is False, "include_basmala should be overridden to False"
    assert ls["include_signature_block"] is False, "include_signature_block should be overridden to False"
    assert ls["include_date_row"] is True, "include_date_row should remain True (default)"
    assert ls["include_recipient_block"] is True, "include_recipient_block should remain True (default)"
    r.add_finding("info", "overrides: basmala=False, signature=False, others=True")

    # --- Case 3: use_legacy_template=True --------------------------------
    res = api_post("/letters/generate", {
        "request": "اكتب لي خطاب طلب شراكة مع وزارة الثقافة",
        "fields": {"recipient_name": "وزارة الثقافة"},
        "upload_outputs": False,
        "use_legacy_template": True,
    }, timeout=300.0)
    assert res.get("template_profile") is not None, "legacy path should have template_profile"
    assert res.get("letter_style") is None, "legacy path should NOT expose letter_style"
    r.add_finding("info", f"legacy path: template_profile={res['template_profile']['paragraph_count']} paras, letter_style=null")

    # --- Case 4: unknown style_overrides key (silently dropped) ----------
    res = api_post("/letters/generate", {
        "request": "اكتب لي خطابًا رسميًا لدعوة أعضاء الجمعية العمومية",
        "fields": {},
        "upload_outputs": False,
        "style_overrides": {
            "include_basmala": False,
            "unknown_key_for_future_release": "ignore me",
        },
    }, timeout=300.0)
    ls = res.get("letter_style")
    assert ls is not None, "unknown-key path must still include letter_style"
    assert ls["include_basmala"] is False, "known override should still be applied"
    assert "unknown_key_for_future_release" not in ls, "unknown key must not appear in style"
    r.add_finding("info", "unknown key dropped, known override applied")


def test():
    return run_test(
        name="12_ui_options",
        description=DESCRIPTION,
        expected=EXPECTED,
        fn=run,
    )
