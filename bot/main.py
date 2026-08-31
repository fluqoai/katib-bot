"""Bot entry point — wires the application and starts polling.

Run directly:
    python -m bot.main
or via the convenience script:
    python scripts/run_bot.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Make the project root importable when running as a module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telegram.ext import (  # noqa: E402
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot.config import get_settings  # noqa: E402
from bot.handlers.start import cancel_cmd, help_cmd, start_cmd  # noqa: E402
from bot.handlers.write import (  # noqa: E402
    on_field_value,
    on_free_text,
    on_type_picked,
    write_cmd,
)
from bot.states import SessionStore  # noqa: E402


logger = logging.getLogger(__name__)


def _build_application() -> Application:
    settings = get_settings()

    # --- logging ---
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
    )

    # --- RAG / search / generator (lazy init so missing creds fail fast) ---
    from rag.embeddings import from_env as emb_from_env
    from rag.generator import from_env as gen_from_env
    from rag.search import from_env as search_from_env

    supabase_client, searcher = search_from_env()
    embedder = emb_from_env()
    generator = gen_from_env()

    # --- the Application ---
    app = ApplicationBuilder().token(settings.telegram_bot_token).build()
    app.bot_data["sessions"] = SessionStore()
    app.bot_data["supabase"] = supabase_client
    app.bot_data["searcher"] = searcher
    app.bot_data["embeddings"] = embedder
    app.bot_data["generator"] = generator
    app.bot_data["settings"] = settings

    # --- handlers ---
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("write", write_cmd))
    app.add_handler(CommandHandler("search", _search_cmd))  # type: ignore[arg-type]

    # Inline-keyboard callbacks for document-type picker
    app.add_handler(CallbackQueryHandler(on_type_picked, pattern=r"^type:"))
    # Inline-keyboard callbacks for draft actions
    app.add_handler(
        CallbackQueryHandler(_on_draft_action, pattern=r"^draft:")
    )

    # Catch-all for free text — anything that isn't a command
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & ~filters.Regex(r"^🚪 إلغاء$"),
            on_free_text,
        )
    )
    # "🚪 إلغاء" button + the "متابعة" path both go through the field-value handler
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            on_field_value,
        )
    )

    return app


async def _search_cmd(update, context):
    """/search <query> — direct search of the document index, no generation."""
    from bot.handlers.start import access_guard
    if not await access_guard(update, context):
        return
    text = update.effective_message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.effective_message.reply_text(
            "استخدم: `/search <كلمات البحث>`", parse_mode="Markdown",
        )
        return
    query = parts[1].strip()
    searcher = context.application.bot_data["searcher"]
    try:
        ctx = await searcher.search(query)
    except Exception as e:  # noqa: BLE001
        await update.effective_message.reply_text(f"خطأ في البحث: `{e}`",
                                                  parse_mode="Markdown")
        return
    if ctx.is_empty():
        await update.effective_message.reply_text("لا نتائج.")
        return
    lines = [f"**نتائج البحث عن:** _{query}_\n"]
    for cat in ("templates", "national_regulations",
                "internal_policies", "examples", "other"):
        for r in ctx.by_category(cat)[:3]:
            snippet = r.content[:200].replace("\n", " ")
            lines.append(
                f"• `{r.category}` — *{r.title}* "
                f"({r.similarity:.0%})\n  > {snippet}…"
            )
    await update.effective_message.reply_text("\n".join(lines),
                                              parse_mode="Markdown")


async def _on_draft_action(update, context):
    """Handles the draft-actions keyboard: edit / regenerate / approve / discard."""
    assert update.callback_query and update.effective_user
    await update.callback_query.answer()
    action = (update.callback_query.data or "").split(":", 1)[-1]
    store: SessionStore = context.application.bot_data["sessions"]
    sess = await store.get_or_create(
        update.effective_user.id, update.effective_chat.id,
    )
    if action == "regenerate":
        # Drop provided_fields and re-run generation
        sess.context.pop("provided_fields", None)
        # re-trigger field entry; we approximate by jumping to WAITING_FOR_FIELDS
        from bot.states import State
        sess.state = State.WAITING_FOR_FIELDS
        await update.callback_query.edit_message_text(
            "حسنًا، أعد التوليد. أرسل الحقول مرة أخرى (أو أرسل `متابعة`)."
        )
    elif action == "discard":
        await store.reset(update.effective_user.id)
        await update.callback_query.edit_message_text("تم تجاهل المسودة.")
    elif action == "edit":
        from bot.states import State
        sess.state = State.WAITING_FOR_REFINEMENT
        await update.callback_query.edit_message_text(
            "أرسل التعديلات التي تريدها (نص حر) وسأعيد التوليد."
        )
    elif action == "approve":
        # Persist into `drafts` table
        from bot.handlers.draft_persist import persist_draft
        await persist_draft(update, context, sess)
        await store.reset(update.effective_user.id)


def main() -> None:
    app = _build_application()
    logger.info("Kateb starting (env=%s)", get_settings().app_env)
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
