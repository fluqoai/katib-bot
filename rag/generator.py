"""Letter generator — strict citation enforcement.

The non-negotiable rule: every factual claim in the draft must be
followed by a `[source: ...]` tag pointing to a specific source from
the evidence bundle. Any claim the LLM cannot ground in a source
MUST be written as `> لم يتم العثور على مصدر يثبت هذا` (no source
found) — never invented.

The output is a structured `GeneratedDraft` object, not raw text.
The compliance reviewer (`rag.compliance`) re-reads it and verifies
every claim against the bundle.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import asyncio
import httpx

from bot.config import get_settings
from rag.evidence import EvidenceBundle


logger = logging.getLogger(__name__)

FIXED_CLOSING = "وتفضلوا بقبول خالص الشكر والتقدير،،"
FIXED_FOOTER = "جمعية الدعوة وتوعية الجاليات بمحافظة القطيف"


SYSTEM_PROMPT = """\
أنت كاتب خطابات إدارية محترف في جمعية أهلية سعودية.
مهمتك: توليد مسودة خطاب رسمي بناءً على طلب المستخدم، مع الاستناد
حصراً إلى المصادر المرفقة.

قواعد صارمة (لا يمكن كسرها):

1. **الاستناد الحصري للمصادر**: كل ادعاء فعلي في المسودة يجب أن
   يُتبع فوراً بوسم `[source: <label>]` يشير إلى إحدى المصادر
   المرفقة. لا تخترع أرقام مواد، أو لوائح، أو سياسات، أو أحكام.
2. **معلومة بلا مصدر**: إذا لم تجد دعماً لمعلومة في المصادر، اكتبها
   بصيغة: `> لم يتم العثور على مصدر يثبت هذا.`
3. **لا تخمين**: لا تضف أسماء أشخاص، أو أرقام، أو تواريخ، أو
   جهات من عندك حتى لو بدت واضحة. اكتب placeholder واضحاً
   مثل: (يُستكمل)، أو (يُضاف التاريخ)، أو (اسم الجهة) حسب السياق.
4. **الافتتاحية حسب نوع المخاطَب**:
   - إذا كان `entity_type=individual` فاكتب حرفياً:
     `إلى سعادة [اسم الشخص / المسمى الوظيفي] حفظه الله`
   - إذا كان `entity_type=organization` فاكتب حرفياً:
     `المكرمون شركة / [اسم الشركة أو المؤسسة] سلمهم الله`
5. **جمل كاملة**: كل جملة يجب أن تكون كاملة (فاعل + فعل +
   مفعول/متمم) ومنتهية بعلامة ترقيم. لا تترك جملاً ناقصة
   أو تنتهي بـ"..." إلا إذا كان المعنى واضحاً أن هناك متتمّ
   مطلوب.
6. **لا تكرار مطلقاً**: لا تكرّر أي فقرة، ولا تكرّر أي جملة
   في الخطاب. اكتب كل فكرة مرة واحدة فقط. إذا كنت تعيد
   صياغة فكرة، ادمجها في فقرة واحدة أو احذف التكرار.
7. **لا حشو**: لا تضف عبارات عامة لا تخدم المعنى مثل "في إطار
   سعينا الدؤوب نحو تحقيق رؤيتنا...". اكتب مباشرة.
8. **لا مخرجات نموذجية**: لا تطبع JSON أو شيفرة. اكتب نص الخطاب فقط.
9. **هيكل إلزامي**: التزم بهذا الترتيب حصراً، من دون ترويسة مسودة أو
   بسملة أو تاريخ أو توقيع شخصي أو أي قسم إضافي داخل جسم الخطاب:

   [الافتتاحية حسب قاعدة entity_type]

   الموضوع: [موضوع الخطاب]

   السلام عليكم ورحمة الله وبركاته،

   أما بعد،

   [المتن المستند إلى المصادر]

   وتفضلوا بقبول خالص الشكر والتقدير،،

   جمعية الدعوة وتوعية الجاليات بمحافظة القطيف
10. **الخاتمة ثابتة**: لا تغيّر عبارة الختام ولا اسم الجمعية مطلقاً.
11. **تنظيم المتن الرسمي**: اكتب المتن في فقرات نثرية رسمية مترابطة
    ومكتملة، من ثلاث إلى خمس فقرات موجزة بحسب الحاجة. لا تستخدم قوائم
    نقطية أو عناوين فرعية أو حقولاً معلّقة إلا إذا كانت طبيعة الطلب
    تستلزم ذلك فعلاً.

خطوات قبل الإرسال (مراجعة ذاتية):
- اقرأ المسودة كاملة. هل يوجد تكرار؟ احذف المكرر.
- هل كل جملة كاملة؟ أكمل الناقص.
- هل كل ادعاء له مصدر؟ أضف الوسم أو ضع placeholder "لم يتم العثور على مصدر".
- هل الأسماء والتواريخ من المستخدم/المصادر فقط؟ إن لا، استبدل بـ placeholder.

أنتج فقط جسم الخطاب، بدون شروحات أو مقدمات، ومن دون عناوين فرعية
داخل متن الخطاب.

بعد جسم الخطاب مباشرة، أضف قسماً:

## المصادر
- لكل مصدر استشهدت به، اكتب: `[<label>]: <title> — chunk #<n> — similarity=<s>`
  حيث يكون `<label>` مطابقاً لما استخدمته في `[source: ...]`.
"""


USER_PROMPT_TEMPLATE = """\
## طلب المستخدم
{user_request}

## بيانات النموذج
{letter_type_ar}
الحقول المطلوبة: {fields_list}

## الحقول المعروفة من المستخدم
{known_fields}

## المصادر المتاحة
{evidence_block}

## التعليمات
- اكتب جسم الخطاب مع `[source: <label>]` بعد كل ادعاء.
- استخدم `entity_type` من الحقول المعروفة لتحديد الافتتاحية.
- التزم بالترتيب الرسمي المحدد في رسالة النظام حرفياً.
- ثبّت الختام باسم: جمعية الدعوة وتوعية الجاليات بمحافظة القطيف.
- في نهاية الخطاب، اكتب قسم `## المصادر` مع كل وسام استخدمته.
- إذا لم تجد دعماً في المصادر، اكتب `> لم يتم العثور على مصدر يثبت هذا.`
"""


CORRECTION_SYSTEM_PROMPT = """\
أنت مراجع لغوي وقانوني محترف. مهمتك: تعديل مسودة خطاب إداري
بحيث تعالج ملاحظات المراجع الأولى، مع الحفاظ على الاستناد الحصري
للمصادر المرفقة.

قواعد صارمة (لا يمكن كسرها):

1. عالج كل ملاحظة من ملاحظات المراجع. إذا طلب حذف ادعاء، احذفه.
   إذا طلب إعادة صياغة، أعد الصياغة مع نفس الاستناد.
2. كل ادعاء فعلي يبقى في المسودة يجب أن يُتبع فوراً بوسم
   `[source: <label>]` يشير إلى إحدى المصادر المرفقة.
3. لا تخترع أرقام مواد، أو لوائح، أو سياسات، أو أحكام.
4. إذا لم تجد دعماً لمعلومة، اكتبها بصيغة:
   `> لم يتم العثور على مصدر يثبت هذا.`
5. لا تضف معلومات من عندك حتى لو بدت واضحة.
6. طبّق افتتاحية `individual` أو `organization` المحددة في قالب النظام.
7. أنهِ الخطاب حرفياً بـ"وتفضلوا بقبول خالص الشكر والتقدير،،" ثم
   "جمعية الدعوة وتوعية الجاليات بمحافظة القطيف".

قواعد إضافية خاصة بالتصحيح (لا تكسرها):
8. **لا تكرار مطلقاً**: لا تكرّر أي فقرة أو جملة. إذا كانت المسودة
   الأصلية تحتوي على فقرة مكررة، احذف المكرر مع إبقاء واحدة فقط.
9. **جمل كاملة**: كل جملة يجب أن تكون كاملة ومنتهية بعلامة ترقيم.
   أكمل أي جملة ناقصة، أو احذفها إن كانت بلا معنى.
10. **لا حشو**: لا تضف عبارات عامة لا تخدم المعنى.
11. **placeholder واضح**: إذا كان هناك حقل مطلوب لا تعرفه
    (اسم، تاريخ، رقم)، اكتب (يُستكمل) أو (يُضاف) حسب السياق، ولا
    تخمّن.
12. حافظ على الترتيب الحرفي: الافتتاحية، الموضوع، السلام، أما بعد،
    المتن، عبارة الختام الثابتة، ثم اسم الجمعية الثابت.
13. نظّم المتن في فقرات نثرية رسمية مترابطة ومكتملة، وتجنّب القوائم
    النقطية والعناوين الفرعية ما لم تستلزمها طبيعة الطلب فعلاً.

خطوات قبل الإرسال (مراجعة ذاتية):
- اقرأ المسودة كاملة. هل يوجد تكرار؟ احذف المكرر.
- هل كل جملة كاملة؟ أكمل الناقص أو احذفه.
- هل كل ادعاء له مصدر؟ أضف الوسم أو ضع placeholder "لم يتم العثور على مصدر".
- هل الأسماء والتواريخ من المستخدم/المصادر فقط؟ إن لا، استبدل بـ placeholder.

أنتج فقط جسم الخطاب المعدّل، بدون شروحات. بعد جسم الخطاب
مباشرة، أضف قسماً:

## المصادر
- لكل مصدر استشهدت به، اكتب: `[<label>]: <title> — chunk #<n> — similarity=<s>`
"""


CORRECTION_USER_PROMPT = """\
## نوع الخطاب
{letter_type_ar}

## الحقول المعروفة
{known_fields}

## ملاحظات المراجع (التزم بها)
{suggestions}

## المسودة الأصلية
{draft_body}

## المصادر المتاحة
{evidence_block}

## المطلوب
أعد كتابة المسودة معالجةً كل ملاحظة، مع الإبقاء على وسوم
`[source: <label>]` بعد كل ادعاء مدعوم، وقسم `## المصادر` في
النهاية. اكتب فقط جسم الخطاب وقسم المصادر، بدون مقدمات.
"""


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class GeneratedDraft:
    body: str                          # the full LLM output, including the `## المصادر` block
    body_only: str                     # just the letter body, without the sources block
    sources_block: str                # the `## المصادر` block
    parsed_citations: list[dict[str, str]] = field(default_factory=list)  # list of {label, title, chunk_index, similarity}
    raw_response: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def claim_count(self) -> int:
        """Count of `[source: <label>]` tags in the body."""
        return len(re.findall(r"\[source:\s*[^\]]+\]", self.body_only))

    def has_unverified_claims(self) -> bool:
        return "لم يتم العثور على مصدر" in self.body_only


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------

class LetterGenerator:
    """Thin wrapper around the OpenRouter chat completions API."""

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        s = get_settings()
        self.api_key = api_key or s.openrouter_api_key
        self.model   = model or s.openrouter_model
        # Read the OPENROUTER_REASONING env var (default: off). Some
        # models (z-ai/glm-flash, deepseek-r1) spend the entire token
        # budget on internal chain-of-thought and return content=null
        # unless reasoning is explicitly disabled.
        self.reasoning_enabled = bool(s.openrouter_reasoning)
        self._client = httpx.AsyncClient(timeout=120.0)

    async def generate(
        self,
        user_request: str,
        bundle: EvidenceBundle,
        *,
        letter_type_ar: str | None = None,
        fields: list[tuple[str, str]] | None = None,
        known_fields: dict[str, str] | None = None,
        # Phase 3: when True, the templates section of the prompt is
        # dropped and the LLM is told NOT to imitate any template's
        # structure. The letter is then grounded solely in policies +
        # regulations + the user's request.
        no_template_mode: bool = False,
    ) -> GeneratedDraft:
        """Call the LLM with the formatted evidence and return the draft.

        Phase 3 — ``no_template_mode``:

        When True, the generator's prompt is rebuilt to:
          * drop the templates section (the LLM is not given any
            template's body to imitate)
          * add a system instruction that says "write a Saudi
            administrative letter using the standard structure
            (header → date → recipient → body → closing → signature)"
          * ground every claim in policies + regulations as before

        This is a defense-in-depth measure: even if ``retrieve_templates``
        returns a wrong template (e.g. محضر instead of دعوة), the
        generator no longer sees its content and cannot imitate its
        structure.
        """
        fields = fields or []
        known_fields = known_fields or {}

        # If no_template_mode is on and the bundle has template hits,
        # build a "scrubbed" bundle with the templates stripped out, so
        # format_bundle_for_prompt renders no Templates section.
        if no_template_mode and bundle.template_hits:
            from rag.evidence import EvidenceBundle
            bundle = EvidenceBundle(
                user_request=bundle.user_request,
                template_hits=[],   # intentionally empty
                policy_hits=bundle.policy_hits,
                regulation_hits=bundle.regulation_hits,
            )

        fields_list = "\n".join(
            f"  - `{name}`: {label}" for name, label in fields
        ) or "_(لم تُحدَّد حقول مطلوبة)_"

        if known_fields:
            known_block = "\n".join(
                f"  - **{name}**: {value}" for name, value in known_fields.items()
            )
        else:
            known_block = "_(لم يُدخل المستخدم أي قيم بعد)_"

        # Render the evidence block. The prompt for the LLM includes
        # the citation labels so it knows what to fill `[source: ...]`
        # with.
        from rag.evidence import format_bundle_for_prompt
        evidence_block = format_bundle_for_prompt(bundle)

        user_prompt = USER_PROMPT_TEMPLATE.format(
            user_request=user_request,
            letter_type_ar=letter_type_ar or "_(not detected)_",
            fields_list=fields_list,
            known_fields=known_block,
            evidence_block=evidence_block,
        )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 6000,
            # z-ai/glm-flash and other reasoning models REQUIRE
            # `reasoning` to be set. Setting it to a low effort caps
            # the internal chain-of-thought so it doesn't eat the
            # entire output budget (which is what happens at default
            # effort — we observed 1997 reasoning_tokens out of 2000
            # max_tokens, leaving content=null).
            "reasoning": {"effort": "low"},
        }
        resp = await _post_with_backoff(
            self._client,
            api_key=self.api_key,
            payload=payload,
        )
        data = resp.json()
        body = _coerce_content(data["choices"][0]["message"]["content"])

        # Post-generation cleanup: strip duplicate consecutive
        # paragraphs and obvious model artifacts before splitting.
        body = _cleanup_model_output(body)

        # Split the body from the sources block
        body_only, sources_block = _split_sources_block(body)
        body_only = _cleanup_model_output(body_only)
        body_only = enforce_official_structure(
            body_only,
            known_fields=known_fields,
            fallback_subject=letter_type_ar,
        )
        body = f"{body_only}\n\n{sources_block}".strip()
        citations = _parse_citations(sources_block)

        return GeneratedDraft(
            body=body,
            body_only=body_only,
            sources_block=sources_block,
            parsed_citations=citations,
            raw_response=data,
            model=self.model,
            prompt_tokens=data.get("usage", {}).get("prompt_tokens", 0),
            completion_tokens=data.get("usage", {}).get("completion_tokens", 0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def correct(
        self,
        draft: GeneratedDraft,
        bundle: EvidenceBundle,
        *,
        suggestions: str,
        letter_type_ar: str | None = None,
        known_fields: dict[str, str] | None = None,
        # Phase 3: same defense-in-depth for the correction pass.
        no_template_mode: bool = False,
    ) -> GeneratedDraft:
        """Re-write the draft applying the compliance reviewer's suggestions.

        Used by the letter pipeline when the first compliance pass
        returns ``fixable``. The model is given:

          * the original draft body
          * the reviewer's ``suggested_corrections`` (Arabic)
          * the same evidence bundle (with templates stripped if
            ``no_template_mode`` is True)

        and asked to emit a revised version that addresses every
        suggestion while keeping every original claim grounded in a
        citation. Unfixable claims are again written as
        ``> لم يتم العثور على مصدر يثبت هذا``.
        """
        from rag.evidence import format_bundle_for_prompt

        known_fields = known_fields or {}
        known_block = "\n".join(
            f"  - **{k}**: {v}" for k, v in known_fields.items()
        ) or "_(لا توجد)_"

        if no_template_mode and bundle.template_hits:
            from rag.evidence import EvidenceBundle
            bundle = EvidenceBundle(
                user_request=bundle.user_request,
                template_hits=[],
                policy_hits=bundle.policy_hits,
                regulation_hits=bundle.regulation_hits,
            )

        evidence_block = format_bundle_for_prompt(bundle)
        user_prompt = CORRECTION_USER_PROMPT.format(
            letter_type_ar=letter_type_ar or "_(not detected)_",
            known_fields=known_block,
            suggestions=suggestions or "(لم تُقترح تعديلات)",
            draft_body=draft.body_only or draft.body,
            evidence_block=evidence_block,
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": CORRECTION_SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 6000,
            "reasoning": {"effort": "low"},
        }
        resp = await _post_with_backoff(
            self._client, api_key=self.api_key, payload=payload,
        )
        data = resp.json()
        body = _coerce_content(data["choices"][0]["message"]["content"])
        # Apply same cleanup as generate(): strip duplicates + artifacts
        body = _cleanup_model_output(body)
        body_only, sources_block = _split_sources_block(body)
        body_only = _cleanup_model_output(body_only)
        body_only = enforce_official_structure(
            body_only,
            known_fields=known_fields,
            fallback_subject=letter_type_ar,
        )
        body = f"{body_only}\n\n{sources_block}".strip()
        citations = _parse_citations(sources_block)
        return GeneratedDraft(
            body=body,
            body_only=body_only,
            sources_block=sources_block,
            parsed_citations=citations,
            raw_response=data,
            model=self.model,
            prompt_tokens=data.get("usage", {}).get("prompt_tokens", 0),
            completion_tokens=data.get("usage", {}).get("completion_tokens", 0),
        )


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _coerce_content(content: Any) -> str:
    """Coerce the `message.content` field to a plain string.

    Some OpenRouter chat models (notably the new z-ai/glm-flash family)
    return content as a list of structured parts:
        [{"type": "text", "text": "..."}, ...]
    instead of a plain string. Some include reasoning blocks, refusals,
    or tool calls. We extract the concatenated text parts and ignore
    everything else (we don't use tools, and the reasoning stays in
    the model).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
                continue
            if not isinstance(part, dict):
                continue
            # OpenAI structured part: {"type": "text", "text": "..."}
            if part.get("type") in ("text", None) and "text" in part:
                out.append(str(part["text"]))
                continue
            # Some models put the text under "content" instead of "text"
            if "content" in part and isinstance(part["content"], str):
                out.append(part["content"])
        return "\n".join(out)
    if content is None:
        return ""
    return str(content)


def _cleanup_model_output(text: str) -> str:
    """Post-generation cleanup for the LLM output.

    Even with strong prompt rules, models occasionally produce:
      * consecutive duplicate paragraphs / bullet points
      * non-consecutive duplicates within a small window (e.g. a
        list of contact fields where the same placeholder appears
        twice separated by a blank line or two)
      * near-duplicates (e.g. same sentence with a small change)
      * trailing "..." or empty bullet markers
      * consecutive blank lines (> 2)

    This pass removes the obvious artifacts so the compliance
    reviewer and the final DOCX never see them. Heuristic-only —
    the compliance reviewer still has the final say.
    """
    if not text:
        return text
    lines = text.split("\n")
    out: list[str] = []
    prev_norm: str | None = None
    # Sliding window of recently-seen normalised lines, used to
    # catch non-consecutive duplicates (e.g. the model writes a
    # contact-fields list twice, separated by other content).
    recent_norms: list[str] = []
    WINDOW = 12
    blank_run = 0
    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        # Collapse 3+ blank lines down to 1
        if not stripped:
            blank_run += 1
            if blank_run > 1:
                continue
            out.append("")
            continue
        blank_run = 0
        # Drop obvious "...." tail-only lines
        if stripped in ("...", "…", ". . ."):
            continue
        # Drop empty bullet markers
        if stripped in ("-", "*", "•", "- ", "* ", "• "):
            continue
        # Dedupe consecutive duplicates (normalized: ignore spaces
        # and citation tags so `[source: x]` doesn't defeat dedup).
        norm = re.sub(r"\s+", " ", re.sub(r"\[source:\s*[^\]]+\]", "", stripped)).strip()
        if not norm:
            out.append(line)
            continue
        if prev_norm is not None and norm == prev_norm:
            continue
        # Also dedupe near-duplicates where one line is a prefix of
        # the next (e.g. "تعزيز التعاون..." then same again).
        if prev_norm and (
            norm.startswith(prev_norm[:40]) and len(prev_norm) >= 40
        ):
            continue
        # Catch non-consecutive duplicates within the sliding window
        if norm in recent_norms:
            continue
        out.append(line)
        prev_norm = norm
        recent_norms.append(norm)
        if len(recent_norms) > WINDOW:
            recent_norms.pop(0)
    # Strip leading/trailing blank lines
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def _resolved_entity_type(known_fields: dict[str, str]) -> str:
    """Return the canonical addressee type, with a compatibility fallback."""
    raw = (known_fields.get("entity_type") or "").strip().lower()
    if raw in {"individual", "person", "شخص", "فرد", "مسؤول"}:
        return "individual"
    if raw in {"organization", "company", "entity", "شركة", "مؤسسة", "جهة"}:
        return "organization"

    # Existing API clients predate entity_type. Preserve sensible output for
    # obviously personal titles while treating the common institutional case
    # as the default.
    recipient = known_fields.get("recipient_name", "")
    personal_markers = ("الأستاذ", "الدكتور", "الشيخ", "رئيس", "مدير", "سعادة", "معالي")
    return "individual" if any(marker in recipient for marker in personal_markers) else "organization"


def build_recipient_line(known_fields: dict[str, str]) -> str:
    """Build the locked opening line from entity_type + recipient_name."""
    recipient = (known_fields.get("recipient_name") or "{{recipient_name}}").strip()
    if _resolved_entity_type(known_fields) == "individual":
        return f"إلى سعادة {recipient} حفظه الله"

    # Avoid "شركة / شركة ..." when older callers include the legal form in
    # recipient_name. The fixed prefix already supplies it.
    recipient = re.sub(r"^(?:شركة|مؤسسة|جمعية|جهة)\s*/?\s*", "", recipient).strip()
    recipient = recipient or "{{recipient_name}}"
    return f"المكرمون شركة / {recipient} سلمهم الله"


def _extract_subject(text: str, known_fields: dict[str, str], fallback: str | None) -> str:
    for key in ("subject", "letter_subject", "project_name"):
        value = (known_fields.get(key) or "").strip()
        if value:
            return value
    match = re.search(r"^\s*(?:\*\*)?الموضوع\s*:\s*(.+?)(?:\*\*)?\s*$", text, re.MULTILINE)
    if match:
        return re.sub(r"\s*\[source:\s*[^\]]+\]", "", match.group(1)).strip()
    return (fallback or "{{موضوع_الخطاب}}").strip()


def _extract_main_content(text: str) -> str:
    """Remove any model-generated envelope while keeping grounded body text."""
    working = text.strip()
    after = re.search(r"أما\s+بعد\s*[،,:]?", working)
    if after:
        working = working[after.end():]
    closing = re.search(
        r"(?:وتفضلوا\s+بقبول\s+خالص\s+الشكر\s+والتقدير|"
        r"وتقبلوا\s+وافر\s+التحية\s+والتقدير)",
        working,
    )
    if closing:
        working = working[:closing.start()]

    ignored = (
        "إلى سعادة", "المكرمون شركة", "الموضوع:",
        "السلام عليكم ورحمة الله وبركاته", "أما بعد",
        FIXED_CLOSING, FIXED_FOOTER, "# مسودة", "بسم الله الرحمن الرحيم",
    )
    lines = [
        line.rstrip() for line in working.splitlines()
        if line.strip() and not any(line.strip().startswith(prefix) for prefix in ignored)
    ]
    return "\n".join(lines).strip() or "> لم يتم العثور على مصدر يثبت هذا."


def enforce_official_structure(
    text: str,
    *,
    known_fields: dict[str, str] | None = None,
    fallback_subject: str | None = None,
) -> str:
    """Deterministically enforce the organization's official letter layout.

    Prompt instructions are necessary but not sufficient for a hard product
    requirement. This formatter ensures every model response and correction
    has exactly one opening, subject, greeting, transition, closing and footer.
    """
    fields = known_fields or {}
    subject = _extract_subject(text, fields, fallback_subject)
    content = _extract_main_content(text)
    return "\n\n".join((
        build_recipient_line(fields),
        f"الموضوع: {subject}",
        "السلام عليكم ورحمة الله وبركاته،",
        "أما بعد،",
        content,
        FIXED_CLOSING,
        FIXED_FOOTER,
    ))


def _split_sources_block(text: str) -> tuple[str, str]:
    """Split the LLM output into (body, sources_block).

    The model is told to write `## المصادر` after the body. We split
    on that heading. If the model didn't include it, the body is
    the full text and the sources block is empty.
    """
    m = re.search(r"^##\s*المصادر\s*$", text, re.MULTILINE)
    if not m:
        return text, ""
    return text[: m.start()].rstrip(), text[m.start():].strip()


_CITATION_LINE = re.compile(
    r"^[\s\-\u2022\*]*\[(?P<label>[^\]]+)\]\s*:?\s*"
    r"(?P<title>.+?)\s*[\u2014\-]\s*chunk\s*#(?P<chunk>\d+)\s*[\u2014\-]\s*"
    r"similarity=(?P<sim>[\d.]+)",
    re.MULTILINE,
)


def _parse_citations(sources_block: str) -> list[dict[str, str]]:
    if not sources_block:
        return []
    out: list[dict[str, str]] = []
    for m in _CITATION_LINE.finditer(sources_block):
        out.append({
            "label":       m.group("label").strip(),
            "title":       m.group("title").strip(),
            "chunk_index": m.group("chunk"),
            "similarity":  m.group("sim"),
        })
    return out


def from_env(model: str | None = None) -> LetterGenerator:
    return LetterGenerator(model=model)


# -- backoff helper ---------------------------------------------------------

async def _post_with_backoff(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    payload: dict[str, Any],
    max_attempts: int = 6,
) -> httpx.Response:
    """POST to OpenRouter chat/completions with retry on 429 / 5xx.

    Backoff: 1.5s, 3s, 6s, 12s, 24s, 30s (capped). Honours the
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
            await asyncio.sleep(wait)
            delay = min(delay * 2.0, 30.0)
            last_exc = httpx.HTTPStatusError(
                f"{resp.status_code}", request=resp.request, response=resp,
            )
            continue
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError:
            logger.error(
                "openrouter HTTP %d body: %s",
                resp.status_code, resp.text[:600],
            )
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("openrouter: exhausted retries without a response")


__all__ = [
    "LetterGenerator", "GeneratedDraft", "from_env",
    "build_recipient_line", "enforce_official_structure",
    "FIXED_CLOSING", "FIXED_FOOTER",
]
