"""/start, /help, /cancel, and the persistent main menu."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from bot.config import get_settings
from bot.keyboards import main_menu
from bot.states import SessionStore, State


WELCOME = (
    "👋 أهلًا بك في *كاتب* — مساعدك الذكي لكتابة المراسلات الرسمية.\n\n"
    "أرسل لي ما تريد كتابته (مثلًا: *اكتب لي خطاب طلب شراكة مع وزارة الثقافة*) "
    "وسأبحث في أرشيفك، أطرح عليك الأسئلة الناقصة، وأجهّز لك مسودة جاهزة للمراجعة.\n\n"
    "استخدم الأزرار بالأسفل أو أرسل طلبك مباشرة."
)


HELP = (
    "*الأوامر المتاحة:*\n"
    "• `/start` — القائمة الرئيسية\n"
    "• `/write` — ابدأ جلسة كتابة جديدة\n"
    "• `/search <كلمات>` — بحث في الأرشيف\n"
    "• `/cancel` — إلغاء المحادثة الحالية\n\n"
    "*القواعد:*\n"
    "• الناتج دائمًا مسودة للمراجعة — لن يُرسل شيء بدون موافقتك.\n"
    "• كل قيمة رقمية أو تاريخ لا تعطينيه لي سيظهر كـ `{{حقل}}` لتعبئته.\n"
    "• اللغة: فصحى حديثة، بدون لهجة."
)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_user and update.effective_chat
    store: SessionStore = context.application.bot_data["sessions"]
    sess = await store.get_or_create(
        update.effective_user.id, update.effective_chat.id,
    )
    async with sess.lock:
        sess.state = State.IDLE
        sess.context = {}
    await update.effective_message.reply_text(
        WELCOME, parse_mode="Markdown", reply_markup=main_menu(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_message is not None
    await update.effective_message.reply_text(HELP, parse_mode="Markdown")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_user and update.effective_message
    store: SessionStore = context.application.bot_data["sessions"]
    sess = await store.get_or_create(
        update.effective_user.id, update.effective_chat.id,
    )
    await store.reset(update.effective_user.id)
    await update.effective_message.reply_text(
        "تم الإلغاء. أرسل لي طلبًا جديدًا متى ما أردت ✍️",
        reply_markup=main_menu(),
    )


async def access_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Reject updates from chat IDs not in the allow-list (if any)."""
    settings = get_settings()
    allowed = settings.allowed_chat_ids
    if not allowed:
        return True
    chat = update.effective_chat
    if chat is None or chat.id not in allowed:
        if update.effective_message:
            await update.effective_message.reply_text(
                "⛔ هذا البوت مقيّد على مستخدمين محدّدين."
            )
        return False
    return True
