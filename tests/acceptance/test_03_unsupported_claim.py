"""Test 3: Request containing an unsupported/legal claim — MUST become
needs_review (compliance must not silently approve an unverified claim).

The request uses partnership keywords (so intent is recognized), but
the body embeds a specific invented regulation article, a specific
invented date, and a specific invented approval number that are NOT
in our indexed materials. The compliance reviewer must flag these and
the final status MUST be needs_review.
"""
from tests.acceptance.framework import api_post, run_test, TestResult


DESCRIPTION = (
    "A partnership-style request (so the intent detector recognizes it) "
    "but with embedded invented claims: a specific regulation article "
    "number, a specific date, and a specific approval number that are "
    "NOT in our indexed materials. The compliance reviewer must flag them "
    "and final_status MUST be needs_review (not silently ok or fixable)."
)
EXPECTED = (
    "intent is recognized as partnership_request (or similar). "
    "final_status=needs_review. compliance verdict=fixable OR unverifiable. "
    "duplicate_segments OR wrong_names OR unsupported_facts OR unverified_claims "
    "should be non-empty (the reviewer identified the bad claims)."
)


def run(r: TestResult) -> None:
    body = {
        "request": (
            "اكتب لي خطاب طلب شراكة مع وزارة الثقافة، مع الإشارة إلى "
            "أننا نستند في ذلك إلى المادة 47 من نظام الجمعيات الأهلية "
            "الصادر بموجب المرسوم الملكي رقم م/44 بتاريخ 12/04/1438هـ "
            "وأن مجلس إدارة الجمعية وافق على هذه الشراكة بموجب القرار "
            "رقم 2024/88 بتاريخ 15/06/2025م."
        ),
        "fields": {},
    }
    res = api_post("/letters/generate", body, timeout=300.0)

    # 1) Intent must be recognized
    intent = res.get("intent") or {}
    intent_type = intent.get("letter_type")
    r.add_finding("info", f"intent: {intent_type!r}")
    assert intent_type is not None, (
        f"intent not recognized — pipeline short-circuited. "
        f"Test cannot evaluate the compliance stage without a recognized intent."
    )

    # 2) Final status MUST be needs_review
    fs = res.get("final_status")
    assert fs == "needs_review", (
        f"CRITICAL: final_status={fs!r} for a request with invented "
        f"regulation article / date / approval number — expected needs_review. "
        f"This would be a SILENT APPROVAL of unverified content."
    )

    # 3) The compliance review must have flagged something
    comp = res.get("compliance") or {}
    re_comp = res.get("re_compliance") or {}
    findings = []
    for c in (comp, re_comp):
        for key in ("duplicate_segments", "incomplete_sentences",
                    "unintended_placeholders", "contradictions",
                    "wrong_names", "unsupported_facts",
                    "unprofessional_phrasing", "unverified_claims"):
            for item in (c.get(key) or []):
                findings.append((key, item))
    assert findings, (
        f"compliance verdict={comp.get('verdict')!r} re={re_comp.get('verdict')!r} "
        f"but no findings reported. The system marked the letter needs_review "
        f"without identifying what's wrong."
    )
    r.add_finding("info", f"compliance flagged {len(findings)} issue(s)")
    for key, item in findings[:3]:
        r.add_finding("info", f"  {key}: {item[:120]}")

    # 4) The letter should be saved to DB with the provenance
    assert res.get("draft_id"), "no draft_id — provenance not persisted"

    r.actual = (
        f"intent={intent_type}, final_status={fs}, needs_review={res.get('needs_review')}, "
        f"compliance verdict={comp.get('verdict')!r} → re={re_comp.get('verdict')!r}, "
        f"{len(findings)} finding(s)"
    )


def test():
    return run_test(
        name="03_unsupported_legal_claim",
        description=DESCRIPTION,
        expected=EXPECTED,
        fn=run,
    )
