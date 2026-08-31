"""Intent / letter-type detection.

The first stage of the AI letter pipeline. Given a free-form Arabic
user request (e.g. "اكتب لي خطاب طلب شراكة مع وزارة الثقافة"), this
module returns a structured intent with:

  * `letter_type`      — the kind of letter ("partnership_request",
                         "thank_you", "board_meeting_invitation", …,
                         or ``"general"`` if nothing specific matches)
  * `recipient_hint`   — anything the user mentioned ("وزارة الثقافة")
  * `key_topics`       — extracted noun phrases used to expand the
                         retrieval query
  * `fields`           — required form fields inferred from the type
  * `template_query`   — a refined search string for template retrieval
  * `policy_query`     — a refined search string for policy retrieval
  * `regulation_query` — a refined search string for regulation retrieval

Detection strategy
==================

Two strategies, in order of preference:

  1. **Deterministic Arabic-keyword matching** — fast, free, no LLM
     call. Picks the LETTER_TYPES entry with the most keyword hits in
     the request text. If at least one keyword matches, this is the
     result.

  2. **LLM-based classification** — used only when keyword matching
     finds nothing. We ask the LLM (a cheap model) to pick the closest
     of the known types, or ``"general"`` if the request describes a
     kind of letter our catalogue does not anticipate. The result is
     returned with low confidence.

If both strategies fail (LLM timeout, etc.) we fall back to a
``"general"`` intent with confidence 0.0. The pipeline never
short-circuits to ``no_template`` based on intent alone — even an
unmatched request falls through to the closest available template
which is used purely as a style source.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

from bot.config import get_settings


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class LetterIntent:
    """Structured output of intent detection."""
    letter_type: Optional[str] = None
    letter_type_ar: Optional[str] = None
    matched_keywords: list[str] = field(default_factory=list)
    recipient_hint: Optional[str] = None
    key_topics: list[str] = field(default_factory=list)
    fields: list[tuple[str, str]] = field(default_factory=list)
    confidence: float = 0.0
    # refined search queries
    template_query: str = ""
    policy_query: str = ""
    regulation_query: str = ""

    def is_specific(self) -> bool:
        """True if the intent is one of the explicit letter types
        (not the ``"general"`` fallback)."""
        return bool(self.letter_type) and self.letter_type != "general"


# "general" is the catch-all used when no specific type matches.
# It has no keywords of its own (the keyword path never picks it);
# it only appears as the result of the LLM fallback or the final
# safety net. We give it no form fields — the generator should
# fall back to a generic structure.
GENERAL_TYPE = {
    "type": "general",
    "ar":   "خطاب رسمي عام",
    "keywords": [],
    "fields": [
        ("recipient_name", "الجهة المُراسَلة"),
        ("subject",         "موضوع الخطاب"),
        ("signer_name",     "اسم المُوقِّع"),
        ("signer_title",    "صفة المُوقِّع"),
    ],
}

# ---------------------------------------------------------------------------
# Known letter types
# ---------------------------------------------------------------------------

LETTER_TYPES: list[dict] = [
    {
        "type": "partnership_request",
        "ar":   "طلب شراكة",
        "keywords": ["شراكة", "شراكه", "تعاون", "شراكات", "مشاركة", "مشترك"],
        "fields": [
            ("recipient_name", "الجهة المُراسَلة"),
            ("partner_org",    "اسم الجهة الشريكة"),
            ("project_name",   "اسم المشروع المقترح"),
            ("contact_name",   "اسم مسؤول التواصل"),
            ("contact_phone",  "رقم التواصل"),
            ("contact_email",  "البريد الإلكتروني"),
        ],
    },
    {
        "type": "thank_you",
        "ar":   "خطاب شكر",
        "keywords": ["شكر", "تقدير", "امتنان", "عرفان"],
        "fields": [
            ("recipient_name", "الجهة المُراسَلة"),
            ("achievement",     "الإنجاز أو المُسبِّب"),
            ("signer_name",     "اسم المُوقِّع"),
            ("signer_title",    "صفة المُوقِّع"),
        ],
    },
    {
        "type": "board_meeting_invitation",
        "ar":   "دعوة لاجتماع مجلس الإدارة",
        "keywords": ["مجلس الإدارة", "مجلس الادارة", "اجتماع مجلس", "انعقاد مجلس", "دعوة مجلس"],
        "fields": [
            ("recipient_name", "اسم المدعو"),
            ("meeting_date",   "تاريخ الاجتماع"),
            ("meeting_time",   "وقت الاجتماع"),
            ("meeting_place",  "مكان الاجتماع"),
            ("agenda",          "جدول الأعمال"),
        ],
    },
    {
        "type": "general_assembly_invitation",
        "ar":   "دعوة لاجتماع الجمعية العمومية",
        "keywords": ["الجمعية العمومية", "الجمعيه العموميه", "اجتماع الجمعية", "الجمعية غير العادية"],
        "fields": [
            ("recipient_name", "اسم المدعو"),
            ("meeting_date",   "تاريخ الاجتماع"),
            ("meeting_time",   "وقت الاجتماع"),
            ("meeting_place",  "مكان الاجتماع"),
            ("agenda",          "جدول الأعمال"),
        ],
    },
    {
        "type": "meeting_minutes",
        "ar":   "محضر اجتماع",
        "keywords": ["محضر", "محضر اجتماع", "محرر", "وقائع", "سجل الاجتماعات"],
        "fields": [
            ("meeting_title",  "عنوان الاجتماع"),
            ("meeting_date",   "التاريخ"),
            ("attendees",       "الحضور"),
            ("agenda",          "جدول الأعمال"),
            ("decisions",       "القرارات"),
        ],
    },
    {
        "type": "delegation",
        "ar":   "تفويض / إنابة",
        "keywords": ["تفويض", "إنابة", "انابه", "نيابة عن", "مفوّض", "بتفويض"],
        "fields": [
            ("delegator_name",  "اسم المُفوِّض"),
            ("delegate_name",   "اسم المُفوَّض إليه"),
            ("scope",           "نطاق التفويض"),
            ("duration",        "المدة"),
        ],
    },
    {
        "type": "ministry_notification",
        "ar":   "إشعار للوزارة",
        "keywords": ["إشعار", "اشعار", "تبليغ", "إبلاغ", "نود إبلاغكم", "نحيطكم علماً"],
        "fields": [
            ("recipient_name", "الجهة المُراسَلة"),
            ("subject",         "موضوع الإشعار"),
            ("effective_date",  "تاريخ النفاذ"),
        ],
    },
    {
        "type": "marketing_clearance_request",
        "ar":   "طلب إذن تسويق",
        "keywords": ["إذن تسويق", "اذن تسويق", "موافقة تسويقية", "تخليص", "تصريح تسويقي"],
        "fields": [
            ("recipient_name", "الجهة المُراسَلة"),
            ("product_name",   "اسم المنتج / البرنامج"),
            ("target_audience", "الفئة المستهدفة"),
        ],
    },
    {
        "type": "representative_declaration",
        "ar":   "إقرار ممثل",
        "keywords": ["إقرار", "اقرار", "إقرار ممثل", "تعيين ممثل", "يمثلنا"],
        "fields": [
            ("representative_name", "اسم الممثل"),
            ("representative_id",   "رقم الهوية"),
            ("scope",                "نطاق التمثيل"),
        ],
    },
    # The general fallback is LAST so it never wins a tie against a
    # real type. It's matched only by the LLM classifier or as a final
    # safety net when no keyword matches.
    GENERAL_TYPE,
]


# ---------------------------------------------------------------------------
# Keyword helpers
# ---------------------------------------------------------------------------

_ARABIC_RE = re.compile(r"[\u0600-\u06FF]")


def _keywords_in(text: str, keywords: list[str]) -> list[str]:
    """Return the keywords that appear in text. A keyword matches when
    it appears as a whole word (or a close variant) in the text."""
    found: list[str] = []
    norm = f" {text} "
    for kw in keywords:
        # allow loose match: keyword surrounded by space, comma, or
        # Arabic punctuation
        if re.search(rf"(?:^|\s|[،,:;]){re.escape(kw)}(?:$|\s|[،,:;.])",
                     norm, flags=re.UNICODE):
            found.append(kw)
    return found


def _extract_recipient(text: str) -> Optional[str]:
    """Try to extract the recipient (e.g. "وزارة الثقافة") from a
    request like "اكتب لي خطاب إلى وزارة الثقافة". Returns None if
    not found."""
    patterns = [
        r"إلى\s+([\u0600-\u06FF\s]{3,80}?)(?:\s+(?:بشأن|بخصوص|حول|لطلب|لتنظيم|لإقامة|لعقد|لتأكيد)|[\.\؟!]|$)",
        r"لـ?\s*([\u0600-\u06FF\s]{3,80}?)(?:\s+(?:بشأن|بخصوص|حول|لطلب|لتنظيم|لإقامة|لعقد|لتأكيد)|[\.\؟!]|$)",
        r"ل\s+([\u0600-\u06FF\s]{3,80}?)(?:\s+(?:بشأن|بخصوص|حول|لطلب|لتنظيم|لإقامة|لعقد|لتأكيد)|[\.\؟!]|$)",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            # First pattern has the phrase as the full match; second has a group
            if m.lastindex:
                return m.group(1).strip()
            return m.group(0).strip()
    return None


def _extract_topics(text: str, intent_keywords: list[str]) -> list[str]:
    """Return the matched keywords as the topic seed list."""
    return list(dict.fromkeys(intent_keywords))[:6]


# ---------------------------------------------------------------------------
# LLM-based fallback classifier
# ---------------------------------------------------------------------------

_LLM_TYPES_LIST = "\n".join(
    f"- {t['type']} — {t['ar']}" for t in LETTER_TYPES if t["type"] != "general"
)


_LLM_SYSTEM_PROMPT = f"""\
أنت مُصنِّف طلبات خطابات رسمية باللغة العربية. مهمتك: قراءة طلب
المستخدم واختيار نوع الخطاب الأنسب من القائمة أدناه.

الأنواع المتاحة:
{_LLM_TYPES_LIST}

إذا كان الطلب لا ينطبق على أي نوع من القائمة (مثلاً طلب خطة إخلاء
أو تعميم داخلي أو خطاب تعزية أو ما شابه)، أعد "general".

أعد JSON صالحاً فقط، بدون أي شرح:
{{"type": "<one of the types above, or 'general'>", "confidence": <0.0–1.0>}}
"""


async def _classify_with_llm(user_request: str, *, timeout: float = 15.0) -> Optional[tuple[str, float]]:
    """Ask the LLM to pick the closest letter type. Returns
    ``(type, confidence)`` or ``None`` on any failure. Falls back
    gracefully on timeout / network errors so a missing LLM response
    doesn't block the pipeline.
    """
    try:
        s = get_settings()
        if not s.openrouter_api_key:
            return None
        payload = {
            "model": s.openrouter_model,
            "messages": [
                {"role": "system", "content": _LLM_SYSTEM_PROMPT},
                {"role": "user",   "content": user_request},
            ],
            "temperature": 0.0,
            "max_tokens": 80,
            "response_format": {"type": "json_object"},
            "reasoning": {"effort": "low"},
        }
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {s.openrouter_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if r.status_code >= 400:
            logger.warning("LLM intent classifier HTTP %d: %s", r.status_code, r.text[:200])
            return None
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        # Some models return content as a list of structured parts
        if isinstance(content, list):
            content = "".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        parsed = json.loads(content)
        t = parsed.get("type")
        c_val = float(parsed.get("confidence", 0.5))
        if t and (t == "general" or t in {e["type"] for e in LETTER_TYPES}):
            return t, max(0.0, min(1.0, c_val))
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM intent classifier failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_intent(user_request: str) -> LetterIntent:
    """Synchronous intent detection.

    Tries keyword matching first; if that fails, returns a
    ``"general"`` fallback so the pipeline never gets a None
    letter_type. The LLM-based fallback is async-only — call
    :func:`detect_intent_async` from inside an async pipeline for
    better accuracy.
    """
    if not user_request or not user_request.strip():
        return _general_intent("", confidence=0.0)

    text = user_request.strip()
    intent = _match_keywords(text)
    if intent is not None:
        return intent
    return _general_intent(text, confidence=0.0)


async def detect_intent_async(user_request: str) -> LetterIntent:
    """Async intent detection: keyword match, then LLM fallback, then
    ``"general"`` as a final safety net.

    The LLM fallback is best-effort and never raises. If the LLM call
    fails or times out, we fall through to the ``"general"`` intent.
    """
    if not user_request or not user_request.strip():
        return _general_intent("", confidence=0.0)

    text = user_request.strip()
    intent = _match_keywords(text)
    if intent is not None:
        return intent

    # Keyword matching failed — ask the LLM.
    llm = await _classify_with_llm(text)
    if llm is not None:
        llm_type, llm_conf = llm
        if llm_type == "general":
            return _general_intent(text, confidence=llm_conf)
        # find the entry
        for entry in LETTER_TYPES:
            if entry["type"] == llm_type:
                return _build_intent_from_entry(text, entry, llm_conf)
        # LLM returned an unknown type — ignore
    return _general_intent(text, confidence=0.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _match_keywords(text: str) -> Optional[LetterIntent]:
    """Return a LetterIntent based on keyword matching, or None if no
    type matched."""
    best: dict | None = None
    best_hits: list[str] = []
    for entry in LETTER_TYPES:
        if not entry["keywords"]:
            continue  # skip the "general" entry in keyword pass
        hits = _keywords_in(text, entry["keywords"])
        if not hits:
            continue
        if best is None or len(hits) >= len(best_hits):
            best = entry
            best_hits = hits
    if best is None:
        return None
    confidence = min(1.0, 0.5 + 0.1 * len(best_hits))
    return _build_intent_from_entry(text, best, confidence)


def _build_intent_from_entry(text: str, entry: dict, confidence: float) -> LetterIntent:
    """Build a LetterIntent from a matched LETTER_TYPES entry."""
    topics = _extract_topics(text, _keywords_in(text, entry["keywords"]))
    recipient = _extract_recipient(text)
    template_query = f"نموذج {entry['ar']}"
    if recipient:
        template_query += f" {recipient}"
    policy_query = f"سياسة {entry['ar']}"
    regulation_query = f"نظام {entry['ar']}"
    if recipient:
        regulation_query += f" {recipient}"
    extras = " ".join(topics)
    if extras:
        policy_query = f"{policy_query} {extras}"
        regulation_query = f"{regulation_query} {extras}"
    return LetterIntent(
        letter_type=entry["type"],
        letter_type_ar=entry["ar"],
        matched_keywords=_keywords_in(text, entry["keywords"]),
        recipient_hint=recipient,
        key_topics=topics,
        fields=list(entry["fields"]),
        confidence=confidence,
        template_query=template_query,
        policy_query=policy_query,
        regulation_query=regulation_query,
    )


def _general_intent(text: str, *, confidence: float) -> LetterIntent:
    """The catch-all "general" intent used when no type matches."""
    recipient = _extract_recipient(text) if text else None
    template_query = text or "خطاب رسمي"
    if recipient:
        template_query = f"{template_query} {recipient}"
    return LetterIntent(
        letter_type="general",
        letter_type_ar=GENERAL_TYPE["ar"],
        matched_keywords=[],
        recipient_hint=recipient,
        key_topics=[],
        fields=list(GENERAL_TYPE["fields"]),
        confidence=confidence,
        template_query=template_query,
        policy_query=text or "سياسات ولوائح الجمعيات الأهلية",
        regulation_query=text or "نظام الجمعيات والمؤسسات الأهلية",
    )


# ---------------------------------------------------------------------------
# Per-intent default style (Phase 1)
#
# Maps each letter type to a ``LetterStyle`` used by the style-driven
# DOCX export (``rag.export.build_letter_from_style``) when no template
# is available. Defaults are conservative; the user can override any
# field per-request via ``GenerateRequest.style_overrides``.
#
# This is purely additive: existing intent detection logic is untouched.
# The mapping is only consulted by the new style-driven export path.
# ---------------------------------------------------------------------------

# Imported lazily inside ``default_style_for`` to avoid a circular import
# (rag.export imports nothing from rag.intent, but keeping the import
# lazy keeps the option to inline the dataclass if needed).


def default_style_for(letter_type: str | None) -> "LetterStyle":
    """Return the default ``LetterStyle`` for a given letter type.

    Falls back to ``DEFAULT_LETTER_STYLE`` for unknown types or None.
    """
    from rag.export import LetterStyle  # lazy import
    base = {
        # Standard formal invitation — Basmala + date row + signature
        "general_assembly_invitation": LetterStyle(
            include_basmala=True,
            include_date_row=True,
            include_recipient_block=True,
            include_signature_block=True,
            signature_title="رئيس مجلس الإدارة",
        ),
        "board_meeting_invitation": LetterStyle(
            include_basmala=True,
            include_date_row=True,
            include_recipient_block=True,
            include_signature_block=True,
            signature_title="رئيس مجلس الإدارة",
        ),
        # Partnership / cooperation — no Basmala, formal opening
        "partnership_request": LetterStyle(
            include_basmala=False,
            include_date_row=True,
            include_recipient_block=True,
            include_signature_block=True,
            signature_title="الرئيس التنفيذي",
        ),
        # Thank you / appreciation — no Basmala
        "thank_you": LetterStyle(
            include_basmala=False,
            include_date_row=True,
            include_recipient_block=True,
            include_signature_block=True,
            signature_title="رئيس مجلس الإدارة",
        ),
        # Meeting minutes — minimal skeleton, body carries the structure
        "meeting_minutes": LetterStyle(
            include_basmala=True,
            include_date_row=True,
            include_recipient_block=False,
            include_signature_block=True,
            signature_title="سكرتير الاجتماع",
        ),
        # Delegation / power of attorney — focused on授权 content
        "delegation": LetterStyle(
            include_basmala=True,
            include_date_row=True,
            include_recipient_block=True,
            include_signature_block=True,
            signature_title="رئيس مجلس الإدارة",
        ),
        # Ministry notification — formal
        "ministry_notification": LetterStyle(
            include_basmala=False,
            include_date_row=True,
            include_recipient_block=True,
            include_signature_block=True,
            signature_title="رئيس مجلس الإدارة",
        ),
        # Marketing clearance request — business-formal
        "marketing_clearance_request": LetterStyle(
            include_basmala=False,
            include_date_row=True,
            include_recipient_block=True,
            include_signature_block=True,
            signature_title="المدير العام",
        ),
        # Representative declaration — formal
        "representative_declaration": LetterStyle(
            include_basmala=True,
            include_date_row=True,
            include_recipient_block=True,
            include_signature_block=True,
            signature_title="رئيس مجلس الإدارة",
        ),
    }
    from rag.export import DEFAULT_LETTER_STYLE
    return base.get(letter_type or "", DEFAULT_LETTER_STYLE)


__all__ = [
    "LetterIntent",
    "LETTER_TYPES",
    "GENERAL_TYPE",
    "detect_intent",
    "detect_intent_async",
    "default_style_for",  # NEW: per-intent style mapping
]
