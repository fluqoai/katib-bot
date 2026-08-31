"""Persist a draft into the `drafts` table once the user approves it."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes


async def persist_draft(update, context, sess) -> None:
    """Insert the current draft into Supabase and confirm to the user."""
    assert update.effective_user and update.effective_chat
    supabase = context.application.bot_data["supabase"]

    payload = {
        "session_id": None,  # session store is in-memory; nothing to FK to
        "user_id": update.effective_user.id,
        "title": sess.context.get("document_type", "مسودة"),
        "body": sess.last_draft or "",
        "placeholders": sess.context.get("placeholders", []),
        "sources": sess.last_sources,
        "status": "approved",
    }
    import asyncio
    try:
        await asyncio.to_thread(
            lambda: supabase.table("drafts").insert(payload).execute()
        )
    except Exception as e:  # noqa: BLE001
        await context.application.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"تعذّر حفظ المسودة: `{e}`",
            parse_mode="Markdown",
        )
        return

    await context.application.bot.send_message(
        chat_id=update.effective_chat.id,
        text="✅ تم حفظ المسودة في جدول `drafts`. يمكنك مراجعتها لاحقًا من Supabase.",
        parse_mode="Markdown",
    )
