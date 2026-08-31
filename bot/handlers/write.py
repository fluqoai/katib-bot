"""The main writing flow.

Flow:

1. User sends a free-text message like "اكتب لي خطاب طلب شراكة مع وزارة الثقافة".
2. We confirm what they want with a quick-pick keyboard.
3. We run RAG (search across the three required categories).
4. We list the missing fields the user must fill.
5. We generate the draft and present it + a checklist.
6. The user can edit, regenerate, approve, or discard.
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from bot.handlers.start import cancel_cmd
from bot.keyboards import document_type_keyboard, draft_actions_keyboard, main_menu
from bot.states import SessionStore, State


logger = logging.getLogger(__name__)


_TYPE_LABELS_AR = {
    "partnership_request": "طلب شراكة",
    "official_reply": "رد رسمي",
    "internal_circular": "تعميم داخلي",
    "internal_memo": "مذكرة داخلية",
    "formal_request": "طلب رسمي",
    "cooperation": "خطاب تعاون",
    "other": "نوع آخر",
}


# ---- entry points -----------------------------------------------------------

async def write_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/write` — same as sending a fresh request, but explicit."""
    assert update.effective_message
    await update.effective_message.reply_text(
        "تمام، أرسل لي وصفًا لما تريد كتابته (مثلًا: "
        "_اكتب لي خطاب طلب شراكة مع وزارة الثقافة_).",
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )


async def on_free_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all for any text message that isn't a menu button.

    Treats the message as a draft request, then asks the user to pick the
    document type via inline keyboard.
    """
    assert update.effective_user and update.effective_chat and update.effective_message
    store: SessionStore = context.application.bot_data["sessions"]
    sess = await store.get_or_create(
        update.effective_user.id, update.effective_chat.id,
    )
    async with sess.lock:
        sess.context["request"] = update.effective_message.text.strip()
        sess.context["messages"] = sess.context.get("messages", [])
        sess.context["messages"].append(
            {"role": "user", "content": sess.context["request"]}
        )
        sess.state = State.WAITING_FOR_TYPE

    await update.effective_message.reply_text(
        "اختر نوع الخطاب من القائمة أدناه:",
        reply_markup=document_type_keyboard(),
    )


# ---- callback handlers ------------------------------------------------------

async def on_type_picked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User picked a type. Run RAG and figure out missing fields."""
    assert update.callback_query and update.effective_user and update.effective_chat
    await update.callback_query.answer()
    payload = update.callback_query.data or ""
    _, type_key = payload.split(":", 1)
    type_label = _TYPE_LABELS_AR.get(type_key, type_key)

    store: SessionStore = context.application.bot_data["sessions"]
    sess = await store.get_or_create(
        update.effective_user.id, update.effective_chat.id,
    )
    async with sess.lock:
        sess.context["document_type"] = type_label
        sess.context["type_key"] = type_key
        sess.state = State.WAITING_FOR_FIELDS

    # Show a typing indicator while we retrieve
    if update.effective_chat:
        await context.application.bot.send_chat_action(
            chat_id=update.effective_chat.id, action=ChatAction.TYPING,
        )

    # Run RAG search
    searcher = context.application.bot_data["searcher"]
    request = sess.context.get("request", "")
    try:
        ctx = await searcher.search(request)
    except Exception as e:  # noqa: BLE001
        logger.exception("RAG search failed: %s", e)
        await update.callback_query.edit_message_text(
            f"تعذّر البحث في الأرشيف: `{e}`\nأعد المحاولة أو تواصل مع الدعم.",
            parse_mode="Markdown",
        )
        return

    sess.context["retrieved"] = _serialise_context(ctx)
    sess.context["messages"].append(
        {"role": "system", "content": f"retrieved: {ctx.total} chunks"}
    )

    if ctx.is_empty():
        await update.callback_query.edit_message_text(
            "لم أجد مراجع مشابهة في أرشيفك. سأكتب المسودة وفق هيكل عام، "
            "وستحتاج إلى تعبئة كل قيمة يدويًا قبل الإرسال.\n\n"
            "أرسل لي الحقول التي تملؤها الآن (مثلًا: اسم المرسل، التاريخ، "
            "رقم الصادر)، أو أرسل `متابعة` لترك كل شيء كقوالب `{{…}}`."
        )
        return

    matched = ", ".join(
        f"`{r.title}` ({r.category}, {r.similarity:.0%})"
        for cat in ("templates", "national_regulations", "internal_policies", "examples")
        for r in ctx.by_category(cat)[:2]
    )
    await update.callback_query.edit_message_text(
        f"وجدت هذه المراجع المشابهة:\n{matched}\n\n"
        "أرسل لي القيم التي تريد تعبئتها (مثلًا:\n"
        "`المرسل: خالد الأحمري`\n"
        "`التاريخ: 1447/02/15 هـ`\n"
        "`الجهة: وزارة الثقافة`)\n\n"
        "أو أرسل `متابعة` لترك كل الحقول كقوالب.",
        parse_mode="Markdown",
    )


async def on_field_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User is providing the missing field values. Parse and accumulate."""
    assert update.effective_user and update.effective_chat and update.effective_message
    store: SessionStore = context.application.bot_data["sessions"]
    sess = await store.get_or_create(
        update.effective_user.id, update.effective_chat.id,
    )
    text = update.effective_message.text.strip()
    if text in {"متابعة", "متابعة.", "اكمل", "تابع"}:
        return await _generate_and_send_draft(update, context, sess)
    if text in {"إلغاء", "/cancel"}:
        return await cancel_cmd(update, context)

    # Parse "key: value" lines. Anything that doesn't parse goes into a freeform bag.
    provided: dict[str, str] = sess.context.setdefault("provided_fields", {})
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip()
            v = v.strip()
            if k and v:
                provided[k] = v
        else:
            provided.setdefault("notes", "")
            provided["notes"] = (provided["notes"] + "\n" + line).strip()

    await update.effective_message.reply_text(
        f"تم استلام {len(provided)} حقل. أرسل المزيد، أو أرسل `متابعة` للتوليد.",
        reply_markup=main_menu(),
    )


# ---- helpers ----------------------------------------------------------------

async def _generate_and_send_draft(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    sess,
) -> None:
    """Call the generator, store the draft, and present it to the user."""
    assert update.effective_chat
    await context.application.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING,
    )

    generator = context.application.bot_data["generator"]
    retriever = context.application.bot_data["searcher"]

    # Reconstruct the RetrievedContext from the serialised form
    from rag.types import RetrievedContext, SearchResult
    raw = sess.context.get("retrieved", {})
    ctx = RetrievedContext()
    for cat in ("templates", "national_regulations",
                "internal_policies", "examples", "other"):
        for r in raw.get(cat, []):
            getattr(ctx, cat).append(
                SearchResult(
                    id=r["id"],
                    document_id=r["document_id"],
                    chunk_index=r["chunk_index"],
                    content=r["content"],
                    category=r["category"],
                    title=r["title"],
                    source_uri=r.get("source_uri"),
                    similarity=r["similarity"],
                )
            )

    provided = sess.context.get("provided_fields", {})
    request = sess.context.get("request", "")

    try:
        draft = await generator.generate(request, ctx, provided)
    except Exception as e:  # noqa: BLE001
        logger.exception("Draft generation failed: %s", e)
        await context.application.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"تعذّر توليد المسودة: `{e}`",
            parse_mode="Markdown",
        )
        return

    sess.last_draft = draft.body
    sess.last_sources = draft.sources
    sess.context["placeholders"] = [p.field for p in draft.placeholders]
    sess.state = State.CONFIRMING_DRAFT

    # Build the user-facing message
    msg_parts: list[str] = [draft.body, "\n---\n"]
    if draft.placeholders:
        msg_parts.append("**حقول تحتاج إلى تعبئتك:**\n")
        for p in draft.placeholders:
            mark = "❗" if p.required else "•"
            msg_parts.append(f"{mark} `{p.field}`" + (f" — {p.hint}" if p.hint else ""))
        msg_parts.append("")
    if draft.sources:
        msg_parts.append("**المصادر:**")
        for i, s in enumerate(draft.sources, 1):
            msg_parts.append(
                f"{i}. {s.get('title', '?')} "
                f"({s.get('category', '?')}) — {s.get('contribution', '')}"
            )
    msg_parts.append(
        "\n⚠️ تذكير: هذه *مسودة* للمراجعة. لن تُرسل قبل أن تعتمدها أنت."
    )

    await context.application.bot.send_message(
        chat_id=update.effective_chat.id,
        text="\n".join(msg_parts),
        parse_mode="Markdown",
        reply_markup=draft_actions_keyboard(),
    )


def _serialise_context(ctx) -> dict[str, Any]:
    """Convert a RetrievedContext into a JSON-safe dict for session storage."""
    out: dict[str, list[dict[str, Any]]] = {}
    for cat in ("templates", "national_regulations",
                "internal_policies", "examples", "other"):
        out[cat] = [
            {
                "id": r.id,
                "document_id": r.document_id,
                "chunk_index": r.chunk_index,
                "content": r.content,
                "category": r.category,
                "title": r.title,
                "source_uri": r.source_uri,
                "similarity": r.similarity,
            }
            for r in ctx.by_category(cat)
        ]
    return out
