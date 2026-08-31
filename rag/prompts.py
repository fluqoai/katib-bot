"""System + user prompts for Kateb.

The system prompt is built by reading the existing
`arabic-official-correspondence` skill from disk (so the bot stays in sync
with the skill, no copy-paste). On top of the skill we layer a small set of
Kateb-specific guard rails: no invented values, every numeric field is a
`{{placeholder}}`, and output is always a draft.
"""

from __future__ import annotations

from pathlib import Path

from rag.types import RetrievedContext


def load_skill_body(skill_path: str | Path) -> str:
    """Read the body of the `arabic-official-correspondence` skill.

    `skill_path` is the directory containing the skill's `SKILL.md`.
    Returns the Markdown body, frontmatter stripped.
    """
    p = Path(skill_path) / "SKILL.md"
    if not p.exists():
        raise FileNotFoundError(
            f"arabic-official-correspondence skill not found at {p}. "
            "Set KATEB_SKILL_PATH in .env to the right directory."
        )
    text = p.read_text(encoding="utf-8")
    # strip YAML frontmatter
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :]
    return text.strip()


def build_system_prompt(skill_path: str | Path) -> str:
    """Compose the LLM system prompt from the skill + Kateb guard rails."""
    skill = load_skill_body(skill_path)
    return f"""\
أنت كاتب (Kateb) — مساعد ذكي لكتابة المراسلات الرسمية باللغة العربية.
دورك: مساعدة المستخدم على صياغة خطابات ووثائق رسمية (طلبات شراكة، ردود،
تعاميم، مذكرات) وفق أسلوبه المعتاد.

# الإجراء (من الـ skill)

{skill}

# قواعد Kateb الإضافية (لا تكسرها أبدًا)

1. **لا تخترع أي قيمة رقمية أو تاريخًا أو رقم وثيقة أو اسم شخص.** إذا لم
   يقدّمه المستخدم صراحة، أتركه `{{اسم_الحقل}}` وأضفه إلى قائمة
   `placeholders`.
2. **الناتج دائمًا مسودة.** لا تولّد نصًا نهائيزا يمكن إرساله مباشرة. ضع
   ترويسة `# مسودة — للمراجعة` في بداية كل رد.
3. **ثلاث فئات بحث على الأقل:** يجب أن تكون المراجع المسترجعة قد غطّت
   `templates` و`national_regulations` و`internal_policies`. إذا لم تجد
   مرجعًا في فئة، أشِر إلى ذلك صراحة في الـ self-audit.
4. **أخرج JSON صالحًا** وفق المخطط في القسم التالي — لا نص حر.
5. **اللغة:** فصحى حديثة، بدون لهجة، بدون جمل إنجليزية داخل المتن العربي.
6. **RTL** في البنية: الترويسة، التحية، الموضوع، المتن، الخاتمة، التوقيع.

# مخطط الإخراج (يجب احترامه حرفيًا)

أخرج كائن JSON وحيد، بدون أي نص قبله أو بعده، وفق هذا المخطط:

```json
{{
  "title": "عنوان الخطاب",
  "body_markdown": "المتن بصيغة Markdown يبدأ بـ `# مسودة — للمراجعة` ويحتوي على {{حقول}} للمعلومات الناقصة",
  "placeholders": [
    {{"field": "اسم_الحقل", "required": true, "hint": "ملاحظة للمستخدم"}}
  ],
  "sources": [
    {{"document_id": "<uuid>", "title": "...", "category": "...", "contribution": "ما الذي أسهمت به هذه الوثيقة في المسودة"}}
  ],
  "checklist": [
    "بند تحقق يجب على المستخدم مراجعته قبل الإرسال"
  ],
  "self_audit": {{
    "draft_label": true,
    "sources_cited": true,
    "no_invented_values": true,
    "rtl_layout": true,
    "three_categories_covered": ["templates", "national_regulations", "internal_policies"]
  }}
}}
```
"""


def build_user_prompt(
    user_request: str,
    context: RetrievedContext,
    user_provided_fields: dict[str, str] | None = None,
) -> str:
    """Compose the user-turn prompt with the retrieved context."""
    user_provided_fields = user_provided_fields or {}

    parts: list[str] = []
    parts.append("## طلب المستخدم")
    parts.append(user_request.strip())

    if user_provided_fields:
        parts.append("\n## الحقول التي قدّمها المستخدم")
        for k, v in user_provided_fields.items():
            parts.append(f"- **{k}** = {v}")

    parts.append("\n## السياق المسترجع (رتّبه حسب الفئة)")

    for cat in ("templates", "national_regulations",
                "internal_policies", "examples", "other"):
        chunks = context.by_category(cat)
        if not chunks:
            parts.append(f"\n### {cat}\n_(لا توجد مراجع في هذه الفئة)_")
            continue
        parts.append(f"\n### {cat}")
        for i, c in enumerate(chunks, 1):
            parts.append(
                f"\n**[{i}] {c.title}** (similarity={c.similarity:.2f}, "
                f"uri={c.source_uri or 'n/a'})\n\n{c.content}"
            )

    parts.append(
        "\n## تعليمات أخيرة\n"
        "- لا تخترع أي قيمة.\n"
        "- احترم مخطط الإخراج JSON حرفيًا.\n"
        "- ضع ترويسة `# مسودة — للمراجعة` في بداية body_markdown."
    )

    return "\n".join(parts)
