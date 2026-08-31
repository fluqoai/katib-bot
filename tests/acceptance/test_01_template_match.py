"""Test 1: Existing template match — should generate using the correct template."""
from tests.acceptance.framework import api_post, run_test, TestResult


DESCRIPTION = (
    "A request for a partnership letter should match an existing template "
    "(نماذج الشراكات) and use it as the style source for the DOCX."
)
EXPECTED = (
    "intent=partnership_request, template chosen from 'templates' category, "
    "compliance verdict either ok/fixable OR needs_review (if model flagged "
    "unverified content like festival details). NOT no_template and NOT failed."
)


def run(r: TestResult) -> None:
    body = {
        "request": "اكتب لي خطاب طلب شراكة مع وزارة الثقافة لتنظيم مهرجان أدبي",
        "fields": {
            "recipient_name": "وزارة الثقافة",
            "partner_org":    "جمعية البر الخيرية بالمظيلف",
        },
        # Test 01 exercises the LEGACY template-driven export path.
        # Phase 2 made the new style-driven path the default; this test
        # still asserts template behaviour, so we opt into legacy mode.
        "use_legacy_template": True,
    }
    res = api_post("/letters/generate", body, timeout=300.0)

    # 1) Intent is partnership
    intent = res.get("intent") or {}
    intent_type = intent.get("letter_type")
    r.add_finding("info", f"intent detected: {intent_type} (conf={intent.get('confidence', 0):.2f})")
    assert intent_type == "partnership_request", f"intent was {intent_type!r}, expected partnership_request"

    # 2) A template was found and used
    tp = res.get("template_profile") or {}
    assert tp, "no template_profile returned — template was not used"
    r.add_finding("info", f"template profile: rtl={tp.get('rtl')}, paragraphs={tp.get('paragraph_count')}, placeholders={tp.get('placeholder_names')}")

    # 3) Chunks were used (proves retrieval ran)
    chunks = res.get("chunks_used") or []
    assert len(chunks) > 0, "no chunks_used — retrieval returned nothing"
    template_chunks = [c for c in chunks if c.get("category") == "templates"]
    assert len(template_chunks) > 0, "no template chunks used — template was not selected"
    r.add_finding("info", f"chunks_used: {len(chunks)} total ({len(template_chunks)} templates)")

    # 4) Final status is one of the acceptable outcomes
    fs = res.get("final_status")
    assert fs in ("ok", "fixable", "needs_review"), f"final_status={fs!r}, expected one of ok/fixable/needs_review"
    r.add_finding("info", f"final_status={fs}, needs_review={res.get('needs_review')}")

    # 5) DOCX is non-trivial
    docx_bytes = (res.get("docx_url") and None)  # not available without 'generated' bucket
    # we don't check the bytes here — that's test 7. But we confirm a draft exists.
    assert res.get("draft") is not None, "no draft returned"
    r.add_finding("info", f"draft body length: {len(res['draft'].get('body_only') or '')}")

    r.actual = (
        f"intent={intent_type}, final_status={fs}, "
        f"chunks_used={len(chunks)} (templates={len(template_chunks)})"
    )


def test():
    return run_test(
        name="01_existing_template_match",
        description=DESCRIPTION,
        expected=EXPECTED,
        fn=run,
    )
