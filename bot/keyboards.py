"""Inline + reply keyboards for the bot, in Arabic."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup


def main_menu() -> ReplyKeyboardMarkup:
    """The persistent bottom-row menu shown after /start."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("✍️ ابدأ كتابة")],
            [KeyboardButton("🔍 بحث في الأرشيف"), KeyboardButton("❓ مساعدة")],
            [KeyboardButton("🚪 إلغاء")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def document_type_keyboard() -> InlineKeyboardMarkup:
    """Quick-pick list of common Arabic document types."""
    rows = [
        [
            InlineKeyboardButton("طلب شراكة", callback_data="type:partnership_request"),
            InlineKeyboardButton("رد رسمي", callback_data="type:official_reply"),
        ],
        [
            InlineKeyboardButton("تعميم داخلي", callback_data="type:internal_circular"),
            InlineKeyboardButton("مذكرة داخلية", callback_data="type:internal_memo"),
        ],
        [
            InlineKeyboardButton("طلب رسمي", callback_data="type:formal_request"),
            InlineKeyboardButton("خطاب تعاون", callback_data="type:cooperation"),
        ],
        [
            InlineKeyboardButton("نوع آخر (سأصفه)", callback_data="type:other"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def draft_actions_keyboard() -> InlineKeyboardMarkup:
    """Actions available on a produced draft."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ تعديل المسودة", callback_data="draft:edit"),
                InlineKeyboardButton("🔁 إعادة التوليد", callback_data="draft:regenerate"),
            ],
            [
                InlineKeyboardButton("✅ اعتماد وحفظ", callback_data="draft:approve"),
                InlineKeyboardButton("🗑️ تجاهل", callback_data="draft:discard"),
            ],
        ]
    )
