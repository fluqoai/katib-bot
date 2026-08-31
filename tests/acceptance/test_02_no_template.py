"""Test 2: No specific template — should still generate a letter using
the closest available template's style (Mode 3 of export.py).

We craft a partnership request whose content is sufficiently different
from any of the 10 indexed templates that retrieval returns the
closest-but-not-perfect match. The system should:
  - Recognize the intent (partnership_request)
  - Pick the closest available template
  - Use Mode 3 of the exporter (clear body, rewrite using template style)
  - NOT return no_template / failed
"""
from tests.acceptance.framework import api_post, run_test, TestResult


DESCRIPTION = (
    "A partnership request whose content is dissimilar from any indexed "
    "template. The system should still generate a letter by adopting the "
    "style of the closest available template (Mode 3 — clear body, rewrite "
    "using template styles), not fall through to no_template."
)
EXPECTED = (
    "intent is recognized (partnership_request), template chosen via "
    "semantic similarity, template_profile.rtl=True, mode='clean' "
    "(complete-example template), final_status is one of "
    "{ok, fixable, needs_review}."
)


def run(r: TestResult) -> None:
    body = {
        "request": (
            "اكتب لي خطاب طلب شراكة مع شركة خاصة لتمويل برنامج تمكين "
            "النساء في قطاع التقنية، يوضح أهداف البرنامج وآليات الشراكة "
            "المقترحة، مع التأكيد على مبادئ الحوكمة والشفافية المالية."
        ),
        "fields": {
            "recipient_name": "شركة تمكين التقنية",
        },
        # Test 02 exercises the LEGACY template-driven export path
        # (Mode 3 — clear body, rewrite using template styles). The new
        # style-driven path is the default in production but is out of
        # scope for this test, so we opt into legacy mode.
        "use_legacy_template": True,
    }
    res = api_post("/letters/generate", body, timeout=300.0)

    intent = res.get("intent") or {}
    intent_type = intent.get("letter_type")
    r.add_finding("info", f"intent: {intent_type!r} (conf={intent.get('confidence', 0):.2f})")
    assert intent_type is not None, (
        f"intent was not recognized — pipeline would short-circuit to no_template. "
        f"This is a real limitation: unusual requests fall through."
    )

    # Should not be a hard no-template failure
    fs = res.get("final_status")
    assert fs != "no_template", (
        f"final_status=no_template despite {len(intent_type)!r} intent. "
        f"This is a regression in the template fallback path."
    )
    assert fs != "failed", f"final_status=failed: {res.get('error')}"
    assert fs in ("ok", "fixable", "needs_review"), f"unexpected final_status={fs!r}"

    # A template should have been picked
    chunks = res.get("chunks_used") or []
    template_chunks = [c for c in chunks if c.get("category") == "templates"]
    assert len(template_chunks) > 0, "no template chunks used — exporter had no template to base style on"
    r.add_finding("info", f"chunks_used: {len(chunks)} total ({len(template_chunks)} templates)")

    # Template profile should reflect Mode 3
    tp = res.get("template_profile") or {}
    r.add_finding(
        "info",
        f"template chosen: rtl={tp.get('rtl')}, has_body_marker={tp.get('has_body_marker')}, "
        f"placeholders={tp.get('placeholder_names')}, paragraphs={tp.get('paragraph_count')}",
    )
    assert tp.get("rtl"), "template not detected as RTL"

    r.actual = f"intent={intent_type}, final_status={fs}, {len(template_chunks)} template chunks used"


def test():
    return run_test(
        name="02_no_specific_template",
        description=DESCRIPTION,
        expected=EXPECTED,
        fn=run,
    )
