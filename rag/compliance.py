"""Independent compliance review.

The compliance reviewer is a SEPARATE LLM call that re-reads the
draft with the same evidence bundle, and flags:

  * Claims that are present in the draft but missing from the
    citations.
  * Citations that don't actually appear in the draft.
  * Invented numbers / articles / clauses (no source can back them).
  * Unverified placeholders that the generator wrote as
    "لم يتم العثور على مصدر يثبت هذا" (correct behaviour, but the
    reviewer can sometimes suggest a less-strict fix).

Output: a `ComplianceReport` with a verdict and a list of findings.
The letter pipeline uses the verdict to decide whether to:
  * accept the draft as-is (verdict='ok'),
  * apply the suggested corrections and re-emit (verdict='fixable'),
  * reject the draft and ask the user to provide more info
    (verdict='unverifiable').
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from bot.config import get_settings
from rag.evidence import EvidenceBundle, format_bundle_for_prompt
from rag.generator import GeneratedDraft, _CITATION_LINE, _coerce_content, _split_sources_block


logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
أنت مراجع امتثال مستقل في جمعية أهلية سعودية. مهمتك: مراجعة
مسودة خطبة تم توليدها بدقة عالية، والتأكد من أنها:
  * مكتوبة بشكل احترافي
  * خالية من التكرار
  * خالية من الأخطاء الموضعية
  * مدعومة بالمصادر المرفقة
  * لا تحتوي على ادعاءات مخترعة

قواعد المراجعة (طبّقها كلها، لا تتجاوز أياً منها):

1. **تكرار**: إذا وجدت فقرة أو جملة مكررة في المسودة، ضعها في
   `duplicate_segments` وقلل verdict إلى 'fixable' على الأقل.
2. **جمل ناقصة**: إذا وجدت جملة غير منتهية (بدون فاعل، أو
   بدون خاتمة، أو منتهية بـ"...")، ضعها في `incomplete_sentences`
   وقلل verdict.
3. **placeholders غير مقصودة**: إذا كانت المسودة تحتوي على placeholder
   يشبه `{{...}}` أو نص بين أقواس مربعة (مثل `[يُستكمل]`) لم
   يطلبه المستخدم، ضعها في `unintended_placeholders` وقلل verdict.
   (placeholders الصريحة التي يطلبها النظام، مثل `(يُستكمل الاسم)`،
   مقبولة.)
4. **ادعاءات بلا مصدر**: لكل ادعاء فعلي (خاصة أرقام المواد، أو
   اللوائح، أو الأحكام)، تحقق من وجوده في المصادر المرفقة. أي
   ادعاء لا يدعمه مصدر ضعه في `unverified_claims` وقلل verdict.
5. **ادعاءات غير مدعومة بسياق**: لا تكتفِ بوجود اسم الوثيقة في
   المصدر، بل تحقق أن النص يدعم الادعاء المحدد.
6. **تناقضات**: إذا تناقض ادعاء في المسودة مع نص صريح في
   المصادر، ضعه في `contradictions` وقلل verdict.
7. **أسماء خاطئة**: إذا كان اسم الجهة المُراسَلة، أو اسم
   الجمعية، أو اسم المشروع في المسودة مختلفاً عمّا ذكره
   المستخدم، ضعه في `wrong_names`.
8. **تواريخ/أرقام/أسماء مشكوك فيها**: أي تاريخ، أو رقم، أو
   اسم ورد في المسودة ولم يرد في طلب المستخدم ولا في المصادر،
   ضعه في `unsupported_facts`.
9. **أسلوب غير مهني**: أي عبارة عامية، أو حشو، أو لهجة غير
   رسمية، ضعها في `unprofessional_phrasing`.

الـ verdict النهائي:
* 'ok'              — لا مشاكل إطلاقاً. كن متشدداً: أي ملاحظة
  مما سبق يجب أن تُخرج الـ verdict من 'ok'.
* 'fixable'         — مشاكل يمكن إصلاحها بتعديل طفيف (تكرار
  محدود، أو placeholder مقصود، أو جملة ناقصة يمكن إكمالها).
* 'unverifiable'    — ادعاءات جوهرية بلا مصدر، أو تناقضات
  واضحة، أو أسماء خاطئة، أو أرقام مشكوك فيها لا يمكن إصلاحها
  بأمان. يجب أن يراجع الإنسان قبل الإرسال.

مهم جداً:
- لا تُضعف الـ threshold. إذا شككت في أي شيء، اختر verdict
  أكثر صرامة. هدفنا أن يكون كل خطاب صادر إلينا آمناً للاستخدام
  الرسمي.
- إذا كانت هناك فرصة أن يكون الاسم/الرقم/التاريخ خطأً، ضعه
  في القائمة ولا تمرّره.

أعد JSON صالحاً فقط، بدون أي شرح أو كلام خارجي.
"""


USER_PROMPT_TEMPLATE = """\
## المسودة المُراجَعة
{draft_body}

## المصادر المتاحة
{evidence_block}

## المطلوب
JSON بالشكل التالي تماماً (مفاتيح بالإنجليزية، نصوص بالعربية).
قائمة فارغة `[]` تعني أن المشكلة غير موجودة. إذا كانت هناك
أي مشكلة، اذكرها بالتفصيل في القائمة المناسبة ولا تُخفف
الـ verdict:
{{
  "verdict": "ok" | "fixable" | "unverifiable",
  "unverified_claims":     ["ادعاء لا يدعمه أي مصدر"],
  "dangling_citations":    ["وسم [source:] لمصدر غير موجود في المرفقات"],
  "placeholder_claims":    ["placeholder مقبول يطلبه النظام"],
  "duplicate_segments":    ["فقرة/جملة مكررة"],
  "incomplete_sentences":  ["جملة ناقصة أو منتهية بـ..."],
  "unintended_placeholders":["{{...}} أو [...]] غير مقصود"],
  "contradictions":        ["ادعاء يخالف نصاً صريحاً في المصادر"],
  "wrong_names":           ["اسم جهة/شخص/جمعية خاطئ"],
  "unsupported_facts":     ["تاريخ/رقم/اسم لم يرد في طلب المستخدم أو المصادر"],
  "unprofessional_phrasing":["عبارة غير رسمية أو حشو"],
  "suggested_corrections": "اقتراحات تصحيح محددة (نص حر بالعربية)",
  "summary_ar":            "ملخص قصير بالعربية"
}}
"""


@dataclass(slots=True)
class ComplianceReport:
    verdict: str                  # 'ok' | 'fixable' | 'unverifiable'
    unverified_claims:        list[str] = field(default_factory=list)
    dangling_citations:       list[str] = field(default_factory=list)
    placeholder_claims:       list[str] = field(default_factory=list)
    duplicate_segments:       list[str] = field(default_factory=list)
    incomplete_sentences:     list[str] = field(default_factory=list)
    unintended_placeholders:  list[str] = field(default_factory=list)
    contradictions:           list[str] = field(default_factory=list)
    wrong_names:              list[str] = field(default_factory=list)
    unsupported_facts:        list[str] = field(default_factory=list)
    unprofessional_phrasing:  list[str] = field(default_factory=list)
    suggested_corrections: str = ""
    summary_ar:          str = ""
    raw_response:        dict[str, Any] = field(default_factory=dict)
    model:               str = ""
    prompt_tokens:       int = 0
    completion_tokens:   int = 0

    def is_ok(self) -> bool:
        return self.verdict == "ok"

    def needs_correction(self) -> bool:
        return self.verdict == "fixable"

    def is_unverifiable(self) -> bool:
        return self.verdict == "unverifiable"

    def all_findings(self) -> list[str]:
        """Concatenate every issue found, for logging / persistence."""
        all_lists = (
            self.unverified_claims, self.dangling_citations,
            self.placeholder_claims, self.duplicate_segments,
            self.incomplete_sentences, self.unintended_placeholders,
            self.contradictions, self.wrong_names, self.unsupported_facts,
            self.unprofessional_phrasing,
        )
        out: list[str] = []
        for lst in all_lists:
            out.extend(lst)
        return out


class ComplianceReviewer:
    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        s = get_settings()
        # Default to the same model the generator used; the reviewer is
        # called with the same evidence bundle, not the same prompt, so
        # independence is preserved.
        self.api_key = api_key or s.openrouter_api_key
        self.model   = model or s.openrouter_model
        # Read the OPENROUTER_REASONING env var (default: off). Some
        # models (z-ai/glm-flash, deepseek-r1) spend the entire token
        # budget on internal chain-of-thought and return content=null
        # unless reasoning is explicitly disabled.
        self.reasoning_enabled = bool(s.openrouter_reasoning)
        self._client = httpx.AsyncClient(timeout=120.0)

    async def review(
        self,
        draft: GeneratedDraft,
        bundle: EvidenceBundle,
    ) -> ComplianceReport:
        evidence_block = format_bundle_for_prompt(bundle)
        user_prompt = USER_PROMPT_TEMPLATE.format(
            draft_body=draft.body,
            evidence_block=evidence_block,
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 4000,
            "response_format": {"type": "json_object"},
            # See generator.py — reasoning models require this param.
            "reasoning": {"effort": "low"},
        }
        resp = await _post_with_backoff(
            self._client,
            api_key=self.api_key,
            payload=payload,
        )
        data = resp.json()
        text = _coerce_content(data["choices"][0]["message"]["content"])
        # Strip code fences if the model added them
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()
        try:
            parsed = json.loads(text)
        except Exception as e:  # noqa: BLE001
            logger.error("compliance review returned non-JSON: %s\n%s", e, text[:500])
            return ComplianceReport(
                verdict="unverifiable",
                summary_ar=f"فشل المراجعة: {e}",
                raw_response=data,
                model=self.model,
            )
        return ComplianceReport(
            verdict=str(parsed.get("verdict", "unverifiable")),
            unverified_claims=list(parsed.get("unverified_claims", [])),
            dangling_citations=list(parsed.get("dangling_citations", [])),
            placeholder_claims=list(parsed.get("placeholder_claims", [])),
            duplicate_segments=list(parsed.get("duplicate_segments", [])),
            incomplete_sentences=list(parsed.get("incomplete_sentences", [])),
            unintended_placeholders=list(parsed.get("unintended_placeholders", [])),
            contradictions=list(parsed.get("contradictions", [])),
            wrong_names=list(parsed.get("wrong_names", [])),
            unsupported_facts=list(parsed.get("unsupported_facts", [])),
            unprofessional_phrasing=list(parsed.get("unprofessional_phrasing", [])),
            suggested_corrections=str(parsed.get("suggested_corrections", "")),
            summary_ar=str(parsed.get("summary_ar", "")),
            raw_response=data,
            model=self.model,
            prompt_tokens=data.get("usage", {}).get("prompt_tokens", 0),
            completion_tokens=data.get("usage", {}).get("completion_tokens", 0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def from_env(model: str | None = None) -> ComplianceReviewer:
    return ComplianceReviewer(model=model)


# -- backoff helper ---------------------------------------------------------

import asyncio as _asyncio


async def _post_with_backoff(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    payload: dict[str, Any],
    max_attempts: int = 6,
) -> httpx.Response:
    """POST to OpenRouter chat/completions with retry on 429 / 5xx.

    Backoff: 1.5s, 3s, 6s, 12s, 24s, 30s (capped). Reads the
    `Retry-After` header when the server sends one.
    """
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    delay = 1.5
    last_exc: httpx.HTTPStatusError | None = None
    for attempt in range(1, max_attempts + 1):
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code < 400:
            return resp
        # 429 or 5xx -> backoff
        if resp.status_code in (429, 500, 502, 503, 504, 408):
            retry_after = resp.headers.get("retry-after")
            try:
                wait = float(retry_after) if retry_after else delay
            except (TypeError, ValueError):
                wait = delay
            wait = min(max(wait, 0.5), 30.0)
            logger.warning(
                "openrouter HTTP %d (attempt %d/%d) — sleeping %.1fs",
                resp.status_code, attempt, max_attempts, wait,
            )
            await _asyncio.sleep(wait)
            delay = min(delay * 2.0, 30.0)
            last_exc = httpx.HTTPStatusError(
                f"{resp.status_code}", request=resp.request, response=resp,
            )
            continue
        # Any other 4xx — fail fast (the request itself is bad)
        resp.raise_for_status()
    if last_exc is not None:
        raise last_exc
    # Unreachable: loop must have returned or raised
    raise RuntimeError("openrouter: exhausted retries without a response")


__all__ = ["ComplianceReport", "ComplianceReviewer", "from_env"]
